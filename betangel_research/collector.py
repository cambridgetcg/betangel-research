"""Bounded direct-feed collector. Run one process per state directory."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import signal
import sqlite3
import time
from urllib.parse import urlsplit

from .collection import configure_sources, ingest_metadata

MAX_NEXT_DUE = datetime(9999, 1, 1, tzinfo=timezone.utc).timestamp()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def load_config(path):
    config = json.loads(Path(path).read_text())
    if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise ValueError("collector config requires schema_version=1")
    if not isinstance(config.get("sources"), list) or not config["sources"]:
        raise ValueError("explicit source registry required")
    seen = set()
    for source in config["sources"]:
        sid = source.get("source_id", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", sid) or sid in seen:
            raise ValueError("invalid or repeated source_id")
        seen.add(sid)
        if type(source.get("enabled")) is not bool:
            raise ValueError("source enabled must be boolean")
        url = urlsplit(source.get("url", ""))
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.fragment or url.port not in (None, 443)
                or url.hostname not in source.get("allowed_hosts", [])):
            raise ValueError("source requires an allowlisted credential-free HTTPS URL")
        interval = source.get("poll_interval_seconds")
        if type(interval) is not int or not 300 <= interval <= 604800:
            raise ValueError("poll interval must be between 5 minutes and 7 days")
        if source["enabled"] and (source.get("policy_status") != "permitted"
                                  or not source.get("policy_url") or not source.get("attribution")):
            raise ValueError("enabled source requires documented policy and attribution")
    limit = config.get("max_state_bytes", 1_000_000_000)
    if type(limit) is not int or limit < 1_000_000:
        raise ValueError("max_state_bytes must be at least 1000000")
    return config


@contextmanager
def file_lock(path, *, blocking=False):
    with Path(path).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ValueError("another collector or snapshot operation holds the lock") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def open_state(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS source_state (
            source_id TEXT PRIMARY KEY, url TEXT NOT NULL,
            next_due REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
            etag TEXT, last_modified TEXT, pending_json TEXT,
            in_flight INTEGER NOT NULL DEFAULT 0, last_success TEXT,
            last_attempt TEXT, config_sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS poll_audit (
            receipt_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
            received_at TEXT NOT NULL, audit_json TEXT NOT NULL
        );
    """)
    return db


def retry_delay(headers, received_at):
    """Retry-After is a minimum, including valid future HTTP dates."""
    value = headers.get("retry-after")
    if not value:
        return 0
    try:
        maximum = max(0, MAX_NEXT_DUE - epoch(received_at))
        if str(value).isdigit():
            return min(int(value), maximum)
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return 0
        return min(maximum, max(0, date.timestamp() - epoch(received_at)))
    except (ValueError, TypeError, OverflowError):
        return 0


def finish_pending(db, metadata_path, source, pending):
    result, prior_failures = pending["result"], pending["prior_failures"]
    receipt = ingest_metadata(
        metadata_path, source["source_id"], result["items"],
        request_started_at=result["request_started_at"], received_at=result["received_at"],
        status=result["status"], parser_version=result["parser_version"], synthetic=False)
    failed = result["status"] == "error"
    partial = bool(result.get("parser_error_count")) or "item_limit_reached" in result.get("flags", [])
    quarantined = receipt["quarantined_count"] > 0
    failures = min(prior_failures + 1, 20) if failed else 0
    interval = source["poll_interval_seconds"]
    backoff = max(interval, min(86400, interval * 2 ** min(failures, 10))) if failed else interval
    # Jitter only adds delay; never polls earlier than source cadence/Retry-After.
    delay = max(backoff, retry_delay(result["headers"], result["received_at"]))
    delay += pending["jitter_seconds"]
    next_due = min(MAX_NEXT_DUE, epoch(result["received_at"]) + delay)
    if next_due == MAX_NEXT_DUE:
        result["flags"].append("retry_after_outside_supported_horizon_source_paused")
    row = db.execute("SELECT * FROM source_state WHERE source_id=?", (source["source_id"],)).fetchone()
    etag, modified = row["etag"], row["last_modified"]
    if result["status"] == "ok":
        etag = result["headers"].get("etag") if not partial and not quarantined else None
        modified = result["headers"].get("last-modified") if not partial and not quarantined else None
    elif result["status"] == "not_modified":
        etag = result["headers"].get("etag", etag)
        modified = result["headers"].get("last-modified", modified)
    audit = {k: v for k, v in result.items() if k != "items"}
    audit.update({"receipt_id": receipt["receipt_id"], "source_id": source["source_id"],
                  "item_count": receipt["item_count"], "accepted_count": receipt["accepted_count"],
                  "quarantined_count": receipt["quarantined_count"],
                  "new_versions": receipt["new_versions"], "idempotent_replay": receipt["idempotent_replay"],
                  "coverage": receipt["coverage"], "partial_parse": partial,
                  "next_due": datetime.fromtimestamp(next_due, timezone.utc).isoformat(),
                  "config_sha256": pending["config_sha256"]})
    with db:
        db.execute("INSERT OR IGNORE INTO poll_audit VALUES (?, ?, ?, ?)",
                   (receipt["receipt_id"], source["source_id"], result["received_at"], json.dumps(audit)))
        db.execute("""UPDATE source_state SET next_due=?, failures=?, etag=?, last_modified=?,
                   pending_json=NULL, in_flight=0, last_success=?, last_attempt=? WHERE source_id=?""",
                   (next_due, failures, etag, modified,
                    result["received_at"] if not failed and not partial and not quarantined else row["last_success"],
                    result["received_at"], source["source_id"]))
    return audit


def run_tick(config, state_dir, *, fetcher=None, now=None):
    """Poll due sources once, recovering durable receipts before fetching again.

    Caller must hold .collector.lock for the process lifetime. Snapshot lock
    coordinates metadata and scheduler copies; failures leave pending receipts.
    """
    if fetcher is None:
        from .feeds import fetch_feed
        fetcher = fetch_feed
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    sources = [s for s in config["sources"] if s["enabled"]]
    if not sources:
        return []
    with file_lock(state_dir / ".snapshot.lock", blocking=True):
        size = sum(p.stat().st_size for p in state_dir.rglob("*") if p.is_file())
        if size > config.get("max_state_bytes", 1_000_000_000):
            raise ValueError("state storage cap reached; collection stopped without deleting evidence")
        metadata = state_dir / "metadata.sqlite"
        configure_sources(metadata, [{"source_id": s["source_id"], "data_kind": "supplied_metadata",
                                      "description": "Direct feed metadata: " + s["url"] + " | " +
                                      s["attribution"] + " | " + s["policy_url"]} for s in sources])
        db = open_state(state_dir / "scheduler.sqlite")
        outputs = []
        try:
            for source in sources:
                sid = source["source_id"]
                fingerprint = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
                with db:
                    db.execute("INSERT OR IGNORE INTO source_state (source_id,url,config_sha256) VALUES (?,?,?)",
                               (sid, source["url"], fingerprint))
                row = db.execute("SELECT * FROM source_state WHERE source_id=?", (sid,)).fetchone()
                if row["url"] != source["url"]:
                    raise ValueError("changed feed URL requires a new source_id")
                if row["pending_json"]:
                    pending = json.loads(row["pending_json"])
                    outputs.append(finish_pending(db, metadata, pending["source"], pending))
                    continue
                if row["next_due"] > now:
                    continue
                with db:
                    db.execute("UPDATE source_state SET in_flight=1,next_due=?,config_sha256=? WHERE source_id=?",
                               (now + source["poll_interval_seconds"], fingerprint, sid))
                result = fetcher(source["url"], allowed_hosts=source["allowed_hosts"],
                                 etag=row["etag"], last_modified=row["last_modified"],
                                 timeout=15, max_bytes=1_000_000, max_items=200)
                result.setdefault("normalization_completed_at", utc_now())
                if source.get("require_unmodified_title") and "title_normalized" in result["flags"]:
                    result.update(status="error", outcome="source_requires_unmodified_title", items=[])
                    result["flags"].append("source_requires_unmodified_title")
                if row["in_flight"]:
                    result["flags"].append("prior_attempt_interrupted_coverage_unknown")
                if result["status"] == "not_modified" and not (row["etag"] or row["last_modified"]):
                    result["status"] = "error"
                    result["flags"].append("unsolicited_304_without_validator")
                pending = {"result": result, "source": source, "prior_failures": row["failures"],
                           "config_sha256": fingerprint, "jitter_seconds": random.uniform(0, 30)}
                with db:
                    db.execute("UPDATE source_state SET pending_json=? WHERE source_id=?", (json.dumps(pending), sid))
                outputs.append(finish_pending(db, metadata, source, pending))
            return outputs
        finally:
            db.close()


def status(state_dir):
    path = Path(state_dir) / "scheduler.sqlite"
    if not path.exists():
        return {"status": "not_started", "sources": []}
    with sqlite3.connect("file:" + str(path.resolve()) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT source_id,next_due,failures,last_success,last_attempt,in_flight,
                          pending_json IS NOT NULL AS pending_receipt FROM source_state""").fetchall()
        return {"status": "local_scheduler_state_not_health_guarantee", "sources": [dict(r) for r in rows]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="research/collector-sources.json")
    parser.add_argument("--state-dir", default="data/private/collector")
    parser.add_argument("--once", action="store_true", help="one due-source tick; still respects saved cadence")
    parser.add_argument("--status", action="store_true", help="read scheduler state without networking")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(status(args.state_dir), indent=2))
        return
    stop = False

    def stopping(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    try:
        config = load_config(args.config)
        root = Path(args.state_dir)
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        with file_lock(root / ".collector.lock"):
            while not stop:
                rows = run_tick(config, root)
                for row in rows:
                    print(json.dumps(row, sort_keys=True), flush=True)
                if args.once:
                    break
                print(json.dumps({"heartbeat_at": utc_now(), "due_polls_completed": len(rows)}), flush=True)
                for _ in range(60):
                    if stop:
                        break
                    time.sleep(1)
    except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
        # Do not include raw remote response or credential-bearing exceptions.
        parser.exit(2, "Collector stopped: " + type(exc).__name__ + "; inspect local configuration/state\n")


if __name__ == "__main__":
    main()
