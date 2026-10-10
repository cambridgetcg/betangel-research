"""Manual, verified SQLite snapshot bundles; optional Tigris upload.

The worker must hold the same .snapshot.lock throughout each full collection tick.
This script never fetches articles, reads credential files, schedules itself,
prunes backups, or replaces a live database. Python 3.9+ for local operations;
the pinned optional cloud SDK in Docker needs Python 3.10+.
"""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tarfile
import time
from urllib.parse import urlsplit
import uuid


MAX_BYTES = 4 * 1024 ** 3  # Bounded single-object backup, below S3's 5 GiB PUT limit.
DB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.sqlite\Z")


class BackupError(Exception):
    """Expected, safe-to-display failure without provider response/secret text."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


@contextmanager
def snapshot_lock(state_dir):
    lock_path = Path(state_dir) / ".snapshot.lock"
    if lock_path.is_symlink():
        raise BackupError("snapshot lock must not be a symlink")
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("snapshot lock is busy; retry after the current collector tick") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def sqlite_integrity(path):
    # A finished backup has no WAL dependency. Immutable read avoids modifying it.
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("SQLite integrity_check failed")
        return connection.execute("PRAGMA user_version").fetchone()[0]


def copy_database(source, destination, deadline):
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise BackupError("SQLite snapshot exceeded the 60-second total deadline")

    uri = source.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as origin:
        with closing(sqlite3.connect(destination, timeout=5)) as target:
            origin.backup(target, pages=256, progress=progress, sleep=0.05)
    return sqlite_integrity(destination)


def create_backup(state_dir, config_path, output_dir):
    state = Path(state_dir)
    config = Path(config_path)
    out = Path(output_dir)
    if state.is_symlink() or not state.is_dir():
        raise BackupError("state directory must be an existing real directory")
    if config.is_symlink() or not config.is_file():
        raise BackupError("source config must be an existing regular file")
    if out.exists() or out.is_symlink():
        raise BackupError("output directory already exists; use a new directory")
    # Reserve a fresh private directory; no existing backup can be overwritten.
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(mode=0o700)
    try:
        with snapshot_lock(state):
            databases = sorted(state.glob("*.sqlite"))
            if not databases:
                raise BackupError("no top-level .sqlite databases found; no backup created")
            if any(p.is_symlink() or not p.is_file() or not DB_NAME.fullmatch(p.name) for p in databases):
                raise BackupError("database files must be regular files with simple .sqlite names")
            if sum(p.stat().st_size for p in databases) > MAX_BYTES:
                raise BackupError("database snapshots exceed the 4 GiB safety limit")
            manifest = {"schema_version": 1, "backup_id": uuid.uuid4().hex,
                        "snapshot_started_at": utc_now(),
                        "consistency": "all_databases_under_cooperative_snapshot_lock",
                        "source_config": {"name": config.name, "sha256": sha256(config)},
                        "files_copied": "top_level_sqlite_only; no separate article response files",
                        "databases": []}
            deadline = time.monotonic() + 60
            for source in databases:
                destination = out / source.name
                version = copy_database(source, destination, deadline)
                size = destination.stat().st_size
                if size > MAX_BYTES:
                    raise BackupError("SQLite snapshot exceeds the 4 GiB safety limit")
                manifest["databases"].append({"name": source.name, "size_bytes": size,
                    "sha256": sha256(destination), "sqlite_user_version": version,
                    "integrity_check": "ok"})
            manifest["snapshot_completed_at"] = utc_now()
        # Compression/upload do not hold up the next collector tick.
        write_json(out / "manifest.json", manifest)
        if sum(d["size_bytes"] for d in manifest["databases"]) + (out / "manifest.json").stat().st_size > MAX_BYTES:
            raise BackupError("complete snapshot bundle exceeds the 4 GiB safety limit")
        archive = out / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name in ["manifest.json"] + [d["name"] for d in manifest["databases"]]:
                bundle.add(out / name, arcname=name, recursive=False)
        if archive.stat().st_size > MAX_BYTES:
            raise BackupError("compressed backup exceeds the 4 GiB upload limit")
        result = {"status": "local_snapshot_integrity_verified", "backup_id": manifest["backup_id"],
                  "archive": str(archive), "archive_sha256": sha256(archive),
                  "archive_size_bytes": archive.stat().st_size,
                  "databases": len(databases), "remote_verified": False}
        write_json(out / "backup.json", result)
        return result
    except Exception:
        shutil.rmtree(out)
        raise


def verify_backup(backup_dir):
    directory = Path(backup_dir)
    if (directory.is_symlink() or not directory.is_dir()
            or (directory / "manifest.json").is_symlink()):
        raise BackupError("backup directory and manifest must not be symlinks")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1 or not isinstance(manifest.get("databases"), list)
            or not manifest["databases"]):
        raise BackupError("unsupported or empty backup manifest")
    names = set()
    for item in manifest["databases"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not DB_NAME.fullmatch(item["name"]):
            raise BackupError("unsafe database name in manifest")
        name = item["name"]
        if name in names:
            raise BackupError("duplicate database name in manifest")
        names.add(name)
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise BackupError("backup database missing or not a regular file")
        if any((directory / (name + suffix)).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise BackupError("finished backup must not contain SQLite sidecars")
        if path.stat().st_size != item.get("size_bytes") or sha256(path) != item.get("sha256"):
            raise BackupError("backup database size/hash mismatch")
        if sqlite_integrity(path) != item.get("sqlite_user_version"):
            raise BackupError("backup SQLite version mismatch")
    actual = {p.name for p in directory.glob("*.sqlite")}
    if actual != names:
        raise BackupError("backup contains unmanifested databases")
    return {"status": "local_restore_integrity_verified", "databases": len(names),
            "source_config_sha256": manifest.get("source_config", {}).get("sha256"),
            "live_state_replaced": False}


def restore_check(archive_path, output_dir, expected_sha256):
    archive = Path(archive_path)
    out = Path(output_dir)
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256) or sha256(archive) != expected_sha256:
        raise BackupError("archive hash does not match the separately recorded backup receipt")
    if out.exists() or out.is_symlink():
        raise BackupError("restore-check requires a new output directory")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            names = [item.name for item in members]
            if (len(names) != len(set(names)) or "manifest.json" not in names
                    or sum(item.size for item in members) > MAX_BYTES
                    or any(not item.isfile() or not (item.name == "manifest.json" or DB_NAME.fullmatch(item.name))
                           for item in members)):
                raise BackupError("archive has unsafe, duplicated, unexpected or oversized members")
            for item in members:
                # Never use extractall: no path traversal, links, devices or executable modes.
                with bundle.extractfile(item) as source, (out / item.name).open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        return verify_backup(out)
    except Exception:
        shutil.rmtree(out)
        raise


def tigris_settings(environ):
    required = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3", "BUCKET_NAME")
    if any(not environ.get(name) for name in required):
        raise BackupError("Tigris upload requires AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3 and BUCKET_NAME")
    endpoint = urlsplit(environ["AWS_ENDPOINT_URL_S3"])
    if (endpoint.scheme != "https" or endpoint.hostname not in {"t3.storage.dev", "fly.storage.tigris.dev"}
            or endpoint.username or endpoint.password or endpoint.port not in (None, 443)
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise BackupError("upload endpoint must be an approved HTTPS Tigris endpoint")
    bucket = environ["BUCKET_NAME"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise BackupError("invalid Tigris bucket name")
    return environ["AWS_ENDPOINT_URL_S3"], bucket


def upload_backup(backup_dir, *, environ=None, client=None, prefix="metadata-backups"):
    """Upload one archive and read it back; a successful PUT alone is insufficient."""
    environ = os.environ if environ is None else environ
    endpoint, bucket = tigris_settings(environ)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,127}", prefix) or "//" in prefix:
        raise BackupError("invalid backup key prefix")
    directory = Path(backup_dir)
    verify_backup(directory)
    receipt = json.loads((directory / "backup.json").read_text(encoding="utf-8"))
    archive = directory / "bundle.tar.gz"
    digest = sha256(archive)
    if digest != receipt.get("archive_sha256") or archive.stat().st_size != receipt.get("archive_size_bytes"):
        raise BackupError("local backup archive changed after creation")
    if client is None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise BackupError("optional upload requires the pinned boto3 SDK in the Docker image") from exc
        client = boto3.client("s3", endpoint_url=endpoint, region_name=environ.get("AWS_REGION", "auto"),
            aws_access_key_id=environ["AWS_ACCESS_KEY_ID"], aws_secret_access_key=environ["AWS_SECRET_ACCESS_KEY"],
            config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30,
                          retries={"mode": "standard", "total_max_attempts": 3}))
    key = prefix.rstrip("/") + "/" + digest + "/bundle.tar.gz"
    try:
        try:
            with archive.open("rb") as data:
                # Content-addressed immutable key; retries must not overwrite anything.
                client.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/gzip",
                                  Metadata={"sha256": digest}, IfNoneMatch="*")
        except Exception as exc:
            response = getattr(exc, "response", {})
            if (response.get("Error", {}).get("Code") != "PreconditionFailed"
                    and response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412):
                raise
            # A previous PUT may have succeeded before its read-back failed. The
            # existing object still needs full verification; never overwrite it.
        response = client.get_object(Bucket=bucket, Key=key)
        remote = hashlib.sha256()
        size = 0
        with closing(response["Body"]) as body:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                size += len(chunk)
                if size > receipt["archive_size_bytes"]:
                    raise BackupError("remote backup size differs from local archive")
                remote.update(chunk)
        if size != receipt["archive_size_bytes"] or remote.hexdigest() != digest:
            raise BackupError("remote backup read-back checksum mismatch")
    except BackupError:
        raise
    except Exception as exc:
        # Provider errors may contain URLs/request details; do not emit their text.
        raise BackupError("Tigris upload/read-back failed; no verified remote backup recorded") from exc
    result = {"status": "remote_archive_readback_verified", "bucket": bucket,
              "key": key, "sha256": digest, "bytes": size, "verified_at": utc_now()}
    receipt_path = directory / "upload.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        if receipt_path.is_symlink():
            raise BackupError("upload receipt must not be a symlink")
        prior = json.loads(receipt_path.read_text(encoding="utf-8"))
        if any(prior.get(field) != result[field] for field in ("status", "bucket", "key", "sha256", "bytes")):
            raise BackupError("existing upload receipt refers to another destination or archive")
        # Preserve the first successful verification time on an exact retry.
        return prior
    write_json(receipt_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--state-dir", required=True)
    create.add_argument("--config", required=True)
    create.add_argument("--out", required=True)
    create.add_argument("--upload", action="store_true")
    upload = commands.add_parser("upload")
    upload.add_argument("--backup-dir", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--backup-dir", required=True)
    restore = commands.add_parser("restore-check")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--sha256", required=True)
    restore.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_backup(args.state_dir, args.config, args.out)
            if args.upload:
                result["remote"] = upload_backup(args.out)
                result["remote_verified"] = True
        elif args.command == "upload":
            result = upload_backup(args.backup_dir)
        elif args.command == "verify":
            result = verify_backup(args.backup_dir)
        else:
            result = restore_check(args.archive, args.out, args.sha256)
    except BackupError as exc:
        print("Backup error: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("Backup error: local validation or storage operation failed; no success claimed", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
