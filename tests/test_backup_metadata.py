import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import backup_metadata as backup


class ConditionalExists(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}


class FakeS3:
    def __init__(self, *, fail_put=False, fail_get_once=False, corrupt_get=False):
        self.objects = {}
        self.put_calls = []
        self.get_calls = []
        self.fail_put = fail_put
        self.fail_get_once = fail_get_once
        self.corrupt_get = corrupt_get

    def put_object(self, **kwargs):
        self.put_calls.append({k: v for k, v in kwargs.items() if k != "Body"})
        if self.fail_put:
            raise RuntimeError("secret-provider-response-must-not-escape")
        key = (kwargs["Bucket"], kwargs["Key"])
        if key in self.objects:
            raise ConditionalExists("already exists")
        self.objects[key] = kwargs["Body"].read()

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        if self.fail_get_once:
            self.fail_get_once = False
            raise RuntimeError("secret-provider-response-must-not-escape")
        data = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        if self.corrupt_get:
            data = b"!" + data[1:]
        return {"Body": io.BytesIO(data)}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = self.root / "collector-sources.json"
        self.config.write_text('{"synthetic_config":"not-copied-to-backup"}', encoding="utf-8")
        self.out = self.root / "snapshot"
        for name, version in (("metadata.sqlite", 1), ("scheduler.sqlite", 7)):
            db = sqlite3.connect(self.state / name)
            self.addCleanup(db.close)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("CREATE TABLE sample (value TEXT)")
            db.execute("INSERT INTO sample VALUES (?)", (name,))
            db.execute("PRAGMA user_version=" + str(version))
            db.commit()
        self.environment = {"AWS_ACCESS_KEY_ID": "fake-access", "AWS_SECRET_ACCESS_KEY": "fake-secret",
                            "AWS_ENDPOINT_URL_S3": "https://t3.storage.dev", "BUCKET_NAME": "fixture-backups"}

    def create(self):
        return backup.create_backup(self.state, self.config, self.out)

    def test_wal_snapshots_restore_both_databases_and_copy_only_metadata_files(self):
        (self.state / "article-response.html").write_text("must not be copied", encoding="utf-8")
        self.assertTrue((self.state / "metadata.sqlite-wal").exists())
        receipt = self.create()
        self.assertFalse(receipt["remote_verified"])
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["source_config"]["sha256"], backup.sha256(self.config))
        with tarfile.open(receipt["archive"], "r:gz") as bundle:
            self.assertEqual(set(bundle.getnames()), {"manifest.json", "metadata.sqlite", "scheduler.sqlite"})
        restored = self.root / "restored"
        result = backup.restore_check(receipt["archive"], restored, receipt["archive_sha256"])
        self.assertEqual(result["databases"], 2)
        self.assertFalse(result["live_state_replaced"])
        for name in ("metadata.sqlite", "scheduler.sqlite"):
            with sqlite3.connect(restored / name) as db:
                self.assertEqual(db.execute("SELECT value FROM sample").fetchall(), [(name,)])
        self.assertEqual(backup.sqlite_integrity(restored / "scheduler.sqlite"), 7)

    def test_busy_cooperative_lock_fails_without_partial_output(self):
        with backup.snapshot_lock(self.state):
            with self.assertRaisesRegex(backup.BackupError, "busy"):
                self.create()
        self.assertFalse(self.out.exists())

    def test_existing_output_is_never_deleted(self):
        self.out.mkdir()
        marker = self.out / "keep"
        marker.write_text("existing")
        with self.assertRaisesRegex(backup.BackupError, "already exists"):
            self.create()
        self.assertEqual(marker.read_text(), "existing")

    def test_invalid_database_fails_without_partial_output(self):
        (self.state / "broken.sqlite").write_bytes(b"not a SQLite file")
        with self.assertRaises(sqlite3.DatabaseError):
            self.create()
        self.assertFalse(self.out.exists())

    def test_empty_state_and_database_symlink_rejected(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(backup.BackupError, "no top-level"):
            backup.create_backup(empty, self.config, self.out)
        (self.state / "linked.sqlite").symlink_to(self.state / "metadata.sqlite")
        with self.assertRaisesRegex(backup.BackupError, "regular files"):
            self.create()
        self.assertFalse(self.out.exists())

    def test_combined_wal_expansion_cannot_exceed_bundle_limit(self):
        # Main files are one page each; committed data is still in WAL and each
        # resulting snapshot has two pages. Neither snapshot alone is too large.
        source_sizes = sum(p.stat().st_size for p in self.state.glob("*.sqlite"))
        with patch.object(backup, "MAX_BYTES", source_sizes + 4096):
            with self.assertRaisesRegex(backup.BackupError, "complete snapshot bundle"):
                self.create()
        self.assertFalse(self.out.exists())

    def test_hash_change_and_unexpected_sidecars_fail_verification(self):
        self.create()
        sidecar = self.out / "metadata.sqlite-wal"
        sidecar.write_bytes(b"unexpected")
        with self.assertRaisesRegex(backup.BackupError, "sidecars"):
            backup.verify_backup(self.out)
        sidecar.unlink()
        with (self.out / "metadata.sqlite").open("ab") as handle:
            handle.write(b"changed")
        with self.assertRaisesRegex(backup.BackupError, "size/hash mismatch"):
            backup.verify_backup(self.out)

    def test_bad_archive_hash_cannot_create_restore_directory(self):
        receipt = self.create()
        restored = self.root / "restored"
        with self.assertRaisesRegex(backup.BackupError, "archive hash"):
            backup.restore_check(receipt["archive"], restored, "0" * 64)
        self.assertFalse(restored.exists())

    def test_restore_rejects_traversal_links_and_duplicate_members(self):
        for case in ("traversal", "link", "duplicate"):
            with self.subTest(case=case):
                archive = self.root / (case + ".tar.gz")
                with tarfile.open(archive, "w:gz") as bundle:
                    manifest = tarfile.TarInfo("manifest.json")
                    manifest.size = 2
                    bundle.addfile(manifest, io.BytesIO(b"{}"))
                    bad = tarfile.TarInfo("../outside.sqlite" if case == "traversal" else
                                          "manifest.json" if case == "duplicate" else "link.sqlite")
                    if case == "link":
                        bad.type = tarfile.SYMTYPE
                        bad.linkname = "/tmp/outside"
                    bundle.addfile(bad)
                restored = self.root / ("restored-" + case)
                with self.assertRaisesRegex(backup.BackupError, "unsafe"):
                    backup.restore_check(archive, restored, backup.sha256(archive))
                self.assertFalse(restored.exists())
        self.assertFalse((self.root / "outside.sqlite").exists())

    def test_upload_success_requires_byte_readback_and_is_immutable_retry_safe(self):
        receipt = self.create()
        client = FakeS3()
        result = backup.upload_backup(self.out, environ=self.environment, client=client)
        saved = (self.out / "upload.json").read_bytes()
        self.assertEqual(result["status"], "remote_archive_readback_verified")
        self.assertEqual(result["sha256"], receipt["archive_sha256"])
        self.assertEqual(client.put_calls[0]["IfNoneMatch"], "*")
        again = backup.upload_backup(self.out, environ=self.environment, client=client)
        self.assertEqual(again, result)
        self.assertEqual((self.out / "upload.json").read_bytes(), saved)
        self.assertEqual(len(client.objects), 1)
        self.assertEqual(len(client.get_calls), 2)

    def test_failed_readback_can_retry_existing_object_without_overwriting(self):
        self.create()
        client = FakeS3(fail_get_once=True)
        with self.assertRaisesRegex(backup.BackupError, "no verified remote backup") as caught:
            backup.upload_backup(self.out, environ=self.environment, client=client)
        self.assertNotIn("secret-provider", str(caught.exception))
        self.assertFalse((self.out / "upload.json").exists())
        result = backup.upload_backup(self.out, environ=self.environment, client=client)
        self.assertEqual(result["status"], "remote_archive_readback_verified")
        self.assertEqual(len(client.objects), 1)

    def test_put_failure_or_corrupt_remote_bytes_cannot_claim_verification(self):
        self.create()
        for client in (FakeS3(fail_put=True), FakeS3(corrupt_get=True)):
            with self.subTest(client=client), self.assertRaises(backup.BackupError) as caught:
                backup.upload_backup(self.out, environ=self.environment, client=client)
            self.assertNotIn("secret-provider", str(caught.exception))
            self.assertFalse((self.out / "upload.json").exists())
            if client.fail_put:
                self.assertFalse(client.get_calls)

    def test_changed_local_archive_is_not_uploaded(self):
        self.create()
        with (self.out / "bundle.tar.gz").open("ab") as handle:
            handle.write(b"changed")
        client = FakeS3()
        with self.assertRaisesRegex(backup.BackupError, "archive changed"):
            backup.upload_backup(self.out, environ=self.environment, client=client)
        self.assertFalse(client.put_calls)

    def test_missing_credentials_and_wrong_endpoint_do_not_reach_client(self):
        client = FakeS3()
        for environment in ({}, {**self.environment, "AWS_ENDPOINT_URL_S3": "http://t3.storage.dev"},
                            {**self.environment, "AWS_ENDPOINT_URL_S3": "https://example.invalid"}):
            with self.subTest(environment=environment), self.assertRaises(backup.BackupError):
                backup.upload_backup(self.out, environ=environment, client=client)
        self.assertFalse(client.put_calls)


if __name__ == "__main__":
    unittest.main()
