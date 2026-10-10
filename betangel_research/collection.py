"""Local, append-only article *metadata* receipts; no fetching or news inference.

Receipt clocks are explicitly supplied by the collector/importer. This module
does not authenticate them or manufacture historical receipt/publication times.
Metadata is never converted into entity links or P03 context assessments.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
TIMESTAMP_PROVENANCE = "caller_supplied_collector_receipt_not_independently_verified"
SOURCE_KINDS = {"synthetic", "supplied_metadata"}
COVERAGE = {
    "ok": "response_metadata_only_no_absence_inference",
    "error": "collection_failed_coverage_unknown",
    "not_modified": "reported_unchanged_response_no_absence_inference",
}
ITEM_FIELDS = {"provider_id", "url", "title", "published_at", "revised_at"}
REQUIRED_TABLES = {"news_sources", "news_receipts", "news_articles", "news_versions", "news_observations"}
REQUIRED_TRIGGERS = {f"{table}_no_{action.lower()}": table
                     for table in REQUIRED_TABLES for action in ("UPDATE", "DELETE")}

SCHEMA = """
CREATE TABLE news_sources (
    source_id TEXT PRIMARY KEY, data_kind TEXT NOT NULL, description TEXT NOT NULL
);
CREATE TABLE news_receipts (
    receipt_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES news_sources,
    request_started_at TEXT NOT NULL, received_at TEXT NOT NULL,
    status TEXT NOT NULL, parser_version TEXT NOT NULL, synthetic INTEGER NOT NULL,
    timestamp_provenance TEXT NOT NULL, coverage TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL, quarantined_count INTEGER NOT NULL,
    new_articles INTEGER NOT NULL, new_versions INTEGER NOT NULL
);
CREATE TABLE news_articles (
    source_id TEXT NOT NULL REFERENCES news_sources, identity_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, first_receipt_id TEXT NOT NULL REFERENCES news_receipts,
    PRIMARY KEY (source_id, identity_key)
);
CREATE TABLE news_versions (
    source_id TEXT NOT NULL, identity_key TEXT NOT NULL, version_sha256 TEXT NOT NULL,
    provider_id TEXT, url TEXT NOT NULL, title TEXT NOT NULL,
    published_at TEXT, revised_at TEXT,
    first_received_at TEXT NOT NULL, first_receipt_id TEXT NOT NULL REFERENCES news_receipts,
    PRIMARY KEY (source_id, identity_key, version_sha256),
    FOREIGN KEY (source_id, identity_key) REFERENCES news_articles
);
CREATE TABLE news_observations (
    receipt_id TEXT NOT NULL REFERENCES news_receipts, source_id TEXT NOT NULL,
    identity_key TEXT NOT NULL, version_sha256 TEXT NOT NULL,
    disposition TEXT NOT NULL, quality_flags_json TEXT NOT NULL,
    PRIMARY KEY (receipt_id, identity_key),
    FOREIGN KEY (source_id, identity_key, version_sha256) REFERENCES news_versions
);
CREATE INDEX news_receipt_chronology ON news_receipts(source_id, received_at);
CREATE VIEW news_accepted_versions AS
    SELECT v.*, MIN(r.received_at) AS first_accepted_at
    FROM news_versions AS v
    JOIN news_observations AS o USING (source_id, identity_key, version_sha256)
    JOIN news_receipts AS r USING (receipt_id)
    WHERE o.disposition = 'accepted_metadata'
    GROUP BY v.source_id, v.identity_key, v.version_sha256;
PRAGMA user_version = 1;
"""


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(value, name, limit=2048):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError(f"{name} must be a bounded, nonempty string without surrounding whitespace or controls")
    return value


def _source_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("source_id must be an explicit 1-128 character source identifier")
    return value


def _timestamp(value, name, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError(f"{name} requires an ISO timestamp with timezone and at most microsecond precision")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"invalid {name}") from exc


def _normalise_item(item):
    if not isinstance(item, dict) or set(item) - ITEM_FIELDS:
        raise ValueError("items must contain only provider_id, url, title, published_at and revised_at; no article body")
    provider = item.get("provider_id")
    if provider is not None:
        _text(provider, "provider_id", 512)
    url = _text(item.get("url"), "url", 4096)
    if any(c.isspace() for c in url):
        raise ValueError("article url cannot contain whitespace")
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
            raise ValueError("article url requires http(s), a hostname and no embedded credentials")
    except ValueError as exc:
        raise ValueError("invalid article url") from exc
    result = {"provider_id": provider, "url": url,
              "title": _text(item.get("title"), "title"),
              "published_at": _timestamp(item.get("published_at"), "published_at", optional=True),
              "revised_at": _timestamp(item.get("revised_at"), "revised_at", optional=True)}
    identity = ("provider:" + provider) if provider is not None else ("url:" + url)
    return identity, result


def _open(database_path):
    if not isinstance(database_path, (str, Path)) or not str(database_path).strip() or str(database_path) == ":memory:":
        raise ValueError("database_path must name an explicit local SQLite file")
    path = Path(database_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute("SELECT type, name, tbl_name FROM sqlite_master").fetchall()
        if not objects and version == 0:
            # executescript normally commits a pending transaction before running.
            # Put BEGIN inside the script so all DDL, user_version and subsequent
            # trigger creation share one transaction, explicitly rolled back below.
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            for table in sorted(REQUIRED_TABLES):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                                       "BEGIN SELECT RAISE(ABORT, 'collection records are immutable'); END")
            _validate_schema(connection)
            connection.commit()
        else:
            _validate_schema(connection)
        return connection
    except Exception:
        connection.rollback()
        connection.close()
        raise


def _validate_schema(connection):
    """Reject incomplete existing schemas; never silently repair missing guards."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    objects = {(row["type"], row["name"]): row["tbl_name"] for row in
               connection.execute("SELECT type, name, tbl_name FROM sqlite_master")}
    tables = {name for kind, name in objects if kind == "table"}
    if (version != SCHEMA_VERSION or tables != REQUIRED_TABLES
            or ("view", "news_accepted_versions") not in objects
            or any(objects.get(("trigger", name)) != table for name, table in REQUIRED_TRIGGERS.items())):
        raise ValueError("database is not the supported complete collection schema")


def configure_sources(database_path, sources):
    """Explicit immutable source configuration; idempotent identical registration.

    Each source is {source_id, data_kind: synthetic|supplied_metadata, description}.
    Configuration does not assert permission, complete coverage or authenticity.
    """
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a nonempty list")
    validated = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"source_id", "data_kind", "description"}:
            raise ValueError("source configuration requires source_id, data_kind and description only")
        source_id = _source_id(source["source_id"])
        if not isinstance(source["data_kind"], str) or source["data_kind"] not in SOURCE_KINDS:
            raise ValueError("unsupported source data_kind")
        validated.append((source_id, source["data_kind"], _text(source["description"], "description")))
    if len({row[0] for row in validated}) != len(validated):
        raise ValueError("duplicate source configuration")
    connection = _open(database_path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            for source in validated:
                existing = connection.execute("SELECT * FROM news_sources WHERE source_id=?", (source[0],)).fetchone()
                if existing is not None and tuple(existing) != source:
                    raise ValueError("source configuration is immutable; use a new source identifier")
                if existing is None:
                    connection.execute("INSERT INTO news_sources VALUES (?, ?, ?)", source)
        return {"schema_version": SCHEMA_VERSION, "configured_sources": [row[0] for row in validated]}
    finally:
        connection.close()


def ingest_metadata(database_path, source_id, items, *, request_started_at, received_at,
                    status="ok", parser_version="metadata-v1", synthetic=False):
    """Atomically append one normalized metadata poll and its immutable revisions.

    Exact receipt retries return the existing receipt (even after later polls).
    Otherwise received_at must strictly increase per source. Unknown publication
    stays null; future publication/revision is quarantined per observation. The
    accepted view's first_accepted_at, never published_at or first_seen_at, is the
    earliest non-quarantined receipt. Even this is unauthenticated supplied time.
    """
    source_id = _source_id(source_id)
    if not isinstance(status, str) or status not in COVERAGE:
        raise ValueError("status must be ok, error or not_modified")
    if not isinstance(synthetic, bool):
        raise ValueError("synthetic must be boolean")
    parser_version = _text(parser_version, "parser_version", 128)
    started = _timestamp(request_started_at, "request_started_at")
    received = _timestamp(received_at, "received_at")
    if started > received:
        raise ValueError("request_started_at cannot follow received_at")
    if not isinstance(items, list) or (status != "ok" and items):
        raise ValueError("items must be a list; error and not_modified polls require an empty list")
    normalized = sorted((_normalise_item(item) for item in items), key=lambda row: row[0])
    if len({row[0] for row in normalized}) != len(normalized):
        raise ValueError("a poll cannot contain duplicate article identities")
    payload_hash = _digest([item for _, item in normalized])
    envelope = {"source_id": source_id, "request_started_at": started, "received_at": received,
                "status": status, "parser_version": parser_version, "synthetic": synthetic,
                "payload_sha256": payload_hash}
    receipt_id = _digest(envelope)
    observations = []
    for identity, item in normalized:
        flags = []
        if item["published_at"] is None:
            flags.append("publication_unknown")
        for key in ("published_at", "revised_at"):
            if item[key] is not None and item[key] > received:
                flags.append(key + "_after_receipt")
        if item["published_at"] and item["revised_at"] and item["revised_at"] < item["published_at"]:
            flags.append("revision_before_publication")
        disposition = "quarantined" if any(f != "publication_unknown" for f in flags) else "accepted_metadata"
        observations.append((identity, item, _digest(item), disposition, flags))
    connection = _open(database_path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute("SELECT * FROM news_sources WHERE source_id=?", (source_id,)).fetchone()
            if source is None:
                raise ValueError("unknown source_id; configure sources explicitly before ingest")
            if (source["data_kind"] == "synthetic") != synthetic:
                raise ValueError("synthetic flag must agree with configured source data_kind")
            existing = connection.execute("SELECT * FROM news_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
            if existing is not None:
                return {**dict(existing), "synthetic": bool(existing["synthetic"]), "idempotent_replay": True}
            latest = connection.execute("SELECT MAX(received_at) FROM news_receipts WHERE source_id=?", (source_id,)).fetchone()[0]
            if latest is not None and received <= latest:
                raise ValueError("out-of-order or ambiguous equal receipt time; exact retry is the only exception")
            new_articles = new_versions = 0
            for identity, _, version, _, _ in observations:
                new_articles += connection.execute("SELECT 1 FROM news_articles WHERE source_id=? AND identity_key=?",
                                                   (source_id, identity)).fetchone() is None
                new_versions += connection.execute("SELECT 1 FROM news_versions WHERE source_id=? AND identity_key=? AND version_sha256=?",
                                                   (source_id, identity, version)).fetchone() is None
            result = {**envelope, "receipt_id": receipt_id, "timestamp_provenance": TIMESTAMP_PROVENANCE,
                      "coverage": COVERAGE[status], "item_count": len(observations),
                      "accepted_count": sum(o[3] == "accepted_metadata" for o in observations),
                      "quarantined_count": sum(o[3] == "quarantined" for o in observations),
                      "new_articles": new_articles, "new_versions": new_versions}
            columns = ", ".join(result)
            connection.execute(f"INSERT INTO news_receipts ({columns}) VALUES ({','.join('?' for _ in result)})", tuple(result.values()))
            for identity, item, version, disposition, flags in observations:
                connection.execute("INSERT OR IGNORE INTO news_articles VALUES (?, ?, ?, ?)",
                                   (source_id, identity, received, receipt_id))
                connection.execute("INSERT OR IGNORE INTO news_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   (source_id, identity, version, item["provider_id"], item["url"], item["title"],
                                    item["published_at"], item["revised_at"], received, receipt_id))
                connection.execute("INSERT INTO news_observations VALUES (?, ?, ?, ?, ?, ?)",
                                   (receipt_id, source_id, identity, version, disposition, json.dumps(flags)))
            return {**result, "idempotent_replay": False}
    finally:
        connection.close()
