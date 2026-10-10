import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.collection import configure_sources, ingest_metadata


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "nested" / "metadata.sqlite"
        self.source = {"source_id": "fixture-news", "data_kind": "synthetic",
                       "description": "Invented metadata for engineering checks"}
        configure_sources(self.db, [self.source])
        self.article = {"provider_id": "story-1", "url": "https://example.invalid/story-1",
                        "title": "Synthetic race preview", "published_at": "2026-10-10T08:00:00Z"}

    def ingest(self, items=None, minute=0, **kwargs):
        envelope = {"request_started_at": f"2026-10-10T09:{minute:02d}:00Z",
                    "received_at": f"2026-10-10T09:{minute:02d}:01Z", "synthetic": True}
        envelope.update(kwargs)
        return ingest_metadata(self.db, "fixture-news", [self.article] if items is None else items, **envelope)

    def rows(self, query, parameters=()):
        with sqlite3.connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, parameters)]

    def count(self, table):
        return self.rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]

    def test_configured_sources_parent_creation_and_immutable_classification(self):
        self.assertTrue(self.db.is_file())
        configure_sources(self.db, [self.source])
        self.assertEqual(self.count("news_sources"), 1)
        with self.assertRaisesRegex(ValueError, "immutable"):
            configure_sources(self.db, [{**self.source, "data_kind": "supplied_metadata"}])
        with self.assertRaisesRegex(ValueError, "unknown source"):
            ingest_metadata(self.db, "unknown", [], request_started_at="2026-10-10T09:00:00Z",
                            received_at="2026-10-10T09:00:01Z", synthetic=True)
        with self.assertRaisesRegex(ValueError, "synthetic flag"):
            self.ingest(synthetic=False)
        self.assertEqual(self.count("news_receipts"), 0)

    def test_first_seen_is_receipt_not_publication_and_no_context_is_created(self):
        receipt = self.ingest()
        version = self.rows("SELECT * FROM news_accepted_versions")[0]
        article = self.rows("SELECT * FROM news_articles")[0]
        self.assertEqual(version["published_at"], "2026-10-10T08:00:00.000000Z")
        self.assertEqual(version["first_accepted_at"], "2026-10-10T09:00:01.000000Z")
        self.assertEqual(article["first_seen_at"], version["first_accepted_at"])
        self.assertIn("not_independently_verified", receipt["timestamp_provenance"])
        names = {row["name"] for row in self.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any("context" in name or "entity" in name for name in names))

    def test_exact_receipt_retry_remains_idempotent_after_later_poll(self):
        first = self.ingest()
        second = self.ingest(minute=1)
        retry = self.ingest()
        self.assertEqual(first["receipt_id"], retry["receipt_id"])
        self.assertTrue(retry["idempotent_replay"])
        self.assertIs(retry["synthetic"], True)
        self.assertEqual(second["new_versions"], 0)
        self.assertEqual(self.count("news_receipts"), 2)
        self.assertEqual(self.count("news_versions"), 1)
        self.assertEqual(self.count("news_observations"), 2)

    def test_changed_title_same_url_appends_revision_without_retroactive_update(self):
        self.ingest()
        original = self.rows("SELECT * FROM news_versions")[0]
        changed = {**self.article, "title": "Synthetic preview updated",
                   "revised_at": "2026-10-10T09:00:30Z"}
        result = self.ingest([changed], minute=1)
        self.assertEqual((result["new_articles"], result["new_versions"]), (0, 1))
        self.assertEqual(self.rows("SELECT * FROM news_versions WHERE version_sha256=?",
                                   (original["version_sha256"],))[0], original)
        available = self.rows("SELECT title FROM news_accepted_versions WHERE first_accepted_at <= ?",
                              ("2026-10-10T09:00:59.000000Z",))
        self.assertEqual(available, [{"title": self.article["title"]}])

    def test_out_of_order_and_equal_conflicting_receipts_rejected(self):
        self.ingest(minute=2)
        for minute in (1, 2):
            with self.subTest(minute=minute), self.assertRaisesRegex(ValueError, "out-of-order"):
                self.ingest([{**self.article, "title": "Changed"}], minute=minute)
        self.assertEqual(self.count("news_receipts"), 1)
        self.assertEqual(self.count("news_versions"), 1)

    def test_future_publication_quarantine_then_reobservation_does_not_backdate_acceptance(self):
        future = {**self.article, "published_at": "2026-10-10T09:02:00Z"}
        result = self.ingest([future])
        self.assertEqual((result["accepted_count"], result["quarantined_count"]), (0, 1))
        self.assertEqual(self.count("news_accepted_versions"), 0)
        original = self.rows("SELECT * FROM news_versions")[0]
        result = self.ingest([future], minute=3)
        self.assertEqual((result["accepted_count"], result["new_versions"]), (1, 0))
        self.assertEqual(self.rows("SELECT * FROM news_versions")[0], original)
        accepted = self.rows("SELECT * FROM news_accepted_versions")[0]
        self.assertEqual(accepted["first_received_at"], "2026-10-10T09:00:01.000000Z")
        self.assertEqual(accepted["first_accepted_at"], "2026-10-10T09:03:01.000000Z")
        self.assertEqual(self.count("news_observations"), 2)

    def test_future_revision_and_revision_before_publication_quarantined(self):
        for index, revised in enumerate(("2026-10-10T10:00:00Z", "2026-10-10T07:00:00Z")):
            result = self.ingest([{**self.article, "revised_at": revised}], minute=index)
            self.assertEqual(result["quarantined_count"], 1)
        self.assertEqual(self.count("news_accepted_versions"), 0)

    def test_unknown_publication_remains_unknown(self):
        item = {"url": "https://example.invalid/unknown-time", "title": "No publication clock"}
        self.ingest([item])
        version = self.rows("SELECT * FROM news_versions")[0]
        flags = self.rows("SELECT quality_flags_json FROM news_observations")[0]
        self.assertIsNone(version["published_at"])
        self.assertIsNone(version["revised_at"])
        self.assertEqual(json.loads(flags["quality_flags_json"]), ["publication_unknown"])

    def test_empty_error_and_unchanged_polls_are_distinct_coverage_receipts(self):
        for minute, status in enumerate(("ok", "error", "not_modified")):
            receipt = self.ingest([], minute=minute, status=status)
            self.assertEqual(receipt["item_count"], 0)
        records = self.rows("SELECT status, coverage FROM news_receipts ORDER BY received_at")
        self.assertEqual([r["status"] for r in records], ["ok", "error", "not_modified"])
        self.assertEqual(len({r["coverage"] for r in records}), 3)
        self.assertEqual(self.count("news_articles"), 0)

    def test_status_and_envelope_shapes_rejected_without_receipt(self):
        invalid = [{"status": "timeout"}, {"status": []}, {"status": "error"},
                   {"status": "not_modified"}, {"synthetic": 1},
                   {"parser_version": ""}, {"received_at": "2026-10-10T08:00:00Z"},
                   {"received_at": "2026-10-10T09:00:01"},
                   {"received_at": "2026-10-10T09:00:01.0000001Z"}]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.ingest(**kwargs)
        self.assertEqual(self.count("news_receipts"), 0)

    def test_entire_batch_rejected_for_malformed_second_item_no_full_content_stored(self):
        for invalid in (None, {**self.article, "provider_id": "story-2", "body": "Full article"},
                        {**self.article, "provider_id": "story-2", "title": []},
                        {**self.article, "provider_id": "story-2", "published_at": "yesterday"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.ingest([self.article, invalid])
        for table in ("news_receipts", "news_articles", "news_versions", "news_observations"):
            self.assertEqual(self.count(table), 0)
        columns = {row["name"] for row in self.rows("PRAGMA table_info(news_versions)")}
        self.assertFalse(columns & {"body", "html", "content", "raw_payload"})

    def test_duplicate_identity_in_one_poll_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate article"):
            self.ingest([self.article, {**self.article, "title": "Ambiguous second version"}])
        self.assertEqual(self.count("news_receipts"), 0)

    def test_mid_transaction_storage_failure_rolls_back_receipt_and_article(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON news_versions "
                               "BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ingest()
        for table in ("news_receipts", "news_articles", "news_versions", "news_observations"):
            self.assertEqual(self.count(table), 0)

    def test_bootstrap_trigger_failure_rolls_back_schema_and_version_then_reopens_cleanly(self):
        fresh = Path(self.temp.name) / "failed-bootstrap.sqlite"
        original_connect = sqlite3.connect

        def fail_last_trigger(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_authorizer(
                lambda action, name, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_CREATE_TRIGGER and name == "news_versions_no_delete"
                else sqlite3.SQLITE_OK)
            return connection

        with patch("betangel_research.collection.sqlite3.connect", side_effect=fail_last_trigger):
            with self.assertRaises(sqlite3.DatabaseError):
                configure_sources(fresh, [self.source])
        with original_connect(fresh) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master").fetchall(), [])
        configure_sources(fresh, [self.source])
        with original_connect(fresh) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 10)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE news_sources SET description='rewritten'")

    def test_reopen_rejects_missing_view_or_guard_without_autohealing(self):
        for kind, name in (("VIEW", "news_accepted_versions"),
                           ("TRIGGER", "news_versions_no_update"),
                           ("TRIGGER", "news_receipts_no_delete")):
            with self.subTest(missing=name):
                database = Path(self.temp.name) / f"missing-{name}.sqlite"
                configure_sources(database, [self.source])
                with sqlite3.connect(database) as connection:
                    connection.execute(f"DROP {kind} {name}")
                with self.assertRaisesRegex(ValueError, "complete collection schema"):
                    configure_sources(database, [self.source])
                with self.assertRaisesRegex(ValueError, "complete collection schema"):
                    ingest_metadata(database, "fixture-news", [], request_started_at="2026-10-10T09:00:00Z",
                                    received_at="2026-10-10T09:00:01Z", synthetic=True)
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchall(), [])
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM news_sources").fetchone()[0], 1)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM news_receipts").fetchone()[0], 0)

    def test_body_aliases_and_invalid_urls_are_rejected(self):
        invalid = [{**self.article, key: "do not persist"} for key in ("content", "html", "raw_payload")]
        invalid.extend({**self.article, "url": url} for url in
                       ("file:///private/article", "https://name:secret@example.invalid/story", "https://example.invalid/a b"))
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.ingest([item])
        self.assertEqual(self.count("news_receipts"), 0)

    def test_source_scoped_provider_id_survives_url_revision(self):
        self.ingest()
        result = self.ingest([{**self.article, "url": "https://example.invalid/new-location"}], minute=1)
        self.assertEqual((result["new_articles"], result["new_versions"]), (0, 1))
        configure_sources(self.db, [{**self.source, "source_id": "another-source"}])
        ingest_metadata(self.db, "another-source", [self.article], request_started_at="2026-10-10T09:00:00Z",
                        received_at="2026-10-10T09:00:01Z", synthetic=True)
        self.assertEqual(self.count("news_articles"), 2)

    def test_exact_url_identity_does_not_guess_aliases(self):
        first = {"url": "https://example.invalid/item?a=1", "title": "Synthetic title"}
        second = {**first, "url": "https://example.invalid/item?a=2"}
        result = self.ingest([first, second])
        self.assertEqual(result["new_articles"], 2)

    def test_timezone_and_optional_field_normalisation_give_stable_hashes(self):
        first = self.ingest()
        same = {**self.article, "published_at": "2026-10-10T09:00:00+01:00", "revised_at": None}
        receipt = self.ingest([same], request_started_at="2026-10-10T10:00:00+01:00",
                              received_at="2026-10-10T10:00:01+01:00")
        self.assertEqual(first["receipt_id"], receipt["receipt_id"])
        self.assertTrue(receipt["idempotent_replay"])

    def test_database_enforces_revision_and_receipt_immutability(self):
        self.ingest()
        with sqlite3.connect(self.db) as connection:
            for sql in ("UPDATE news_versions SET title='rewritten'", "DELETE FROM news_receipts",
                        "UPDATE news_articles SET first_seen_at='past'"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)

    def test_source_configuration_transaction_rolls_back_on_conflict(self):
        with self.assertRaises(ValueError):
            configure_sources(self.db, [{**self.source, "source_id": "would-be-new"},
                                        {**self.source, "description": "Conflicting replacement"}])
        self.assertEqual(self.count("news_sources"), 1)

    def test_checked_in_fixture_can_be_replayed_without_new_receipts(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "research/fixtures/news-metadata.json").read_text())
        self.assertEqual(fixture["data_kind"], "synthetic")
        configure_sources(self.db, fixture["sources"])
        for replay in (False, True):
            for poll in fixture["polls"]:
                result = ingest_metadata(self.db, **copy.deepcopy(poll), synthetic=True)
                self.assertEqual(result["idempotent_replay"], replay)
        self.assertEqual(self.count("news_receipts"), 8)
        self.assertEqual(self.count("news_articles"), 3)
        self.assertEqual(self.count("news_versions"), 4)
        self.assertEqual(self.count("news_observations"), 6)


if __name__ == "__main__":
    unittest.main()
