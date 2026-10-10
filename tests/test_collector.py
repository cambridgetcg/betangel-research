import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from betangel_research.collector import epoch, file_lock, load_config, retry_delay, run_tick, status


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"schema_version": 1, "sources": [{
            "source_id": "test-feed", "enabled": True, "url": "https://example.org/feed",
            "allowed_hosts": ["example.org"], "poll_interval_seconds": 3600,
            "policy_status": "permitted", "policy_url": "https://example.org/policy",
            "attribution": "Synthetic test fixture only"}]}
        self.result = {"request_started_at": "2026-10-10T08:00:00.000000Z",
                       "received_at": "2026-10-10T08:00:01.000000Z",
                       "normalization_completed_at": "2026-10-10T08:00:01.200000Z",
                       "status": "ok", "http_status": 200, "headers": {"etag": '"one"'},
                       "items": [{"provider_id": "one", "url": "https://example.org/one",
                                  "title": "Invented fixture", "published_at": None}],
                       "parser_error_count": 0, "flags": [], "parser_version": "test-v1",
                       "response_bytes": 100, "payload_sha256": "f" * 64}
        self.now = epoch(self.result["request_started_at"])

    def tick(self, result=None, now=None):
        self.fetcher = Mock(return_value=copy.deepcopy(self.result if result is None else result))
        return run_tick(self.config, self.root, fetcher=self.fetcher, now=self.now if now is None else now)

    def count(self, table):
        with sqlite3.connect(self.root / "metadata.sqlite") as db:
            return db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def test_receipt_and_persistent_cadence_survive_restart(self):
        rows = self.tick()
        self.assertEqual(rows[0]["accepted_count"], 1)
        self.assertEqual(self.count("news_receipts"), 1)
        self.assertEqual(self.tick(now=self.now + 100), [])
        self.fetcher.assert_not_called()
        self.assertEqual(status(self.root)["sources"][0]["last_success"], self.result["received_at"])

    def test_crash_after_metadata_commit_replays_exact_pending_receipt(self):
        from betangel_research.collection import ingest_metadata
        def commit_then_crash(*args, **kwargs):
            ingest_metadata(*args, **kwargs)
            raise RuntimeError("simulated crash after metadata commit")
        with patch("betangel_research.collector.ingest_metadata", side_effect=commit_then_crash):
            with self.assertRaises(RuntimeError):
                self.tick()
        self.assertEqual(self.count("news_receipts"), 1)
        self.assertTrue(status(self.root)["sources"][0]["pending_receipt"])
        rows = self.tick(now=self.now + 100)
        self.fetcher.assert_not_called()
        self.assertTrue(rows[0]["idempotent_replay"])
        self.assertEqual(self.count("news_receipts"), 1)
        self.assertFalse(status(self.root)["sources"][0]["pending_receipt"])

    def test_crash_before_fetch_result_does_not_tight_loop(self):
        with self.assertRaises(RuntimeError):
            run_tick(self.config, self.root, fetcher=Mock(side_effect=RuntimeError("crash")), now=self.now)
        self.assertEqual(self.tick(now=self.now + 10), [])
        self.fetcher.assert_not_called()
        self.assertTrue(status(self.root)["sources"][0]["in_flight"])
        later = copy.deepcopy(self.result)
        later["request_started_at"] = "2026-10-10T09:00:00Z"
        later["received_at"] = "2026-10-10T09:00:01Z"
        rows = self.tick(later, now=self.now + 3600)
        self.assertIn("prior_attempt_interrupted_coverage_unknown", rows[0]["flags"])

    def test_retry_after_dominates_restart_schedule(self):
        failed = {**self.result, "items": [], "status": "error", "http_status": 429,
                  "headers": {"retry-after": "172800"}}
        rows = self.tick(failed)
        self.assertIn("coverage_unknown", rows[0]["coverage"])
        saved = status(self.root)["sources"][0]
        self.assertGreaterEqual(saved["next_due"], epoch(failed["received_at"]) + 172800)
        self.assertEqual(saved["failures"], 1)
        self.assertEqual(self.tick(now=self.now + 86400), [])
        self.fetcher.assert_not_called()

    def test_304_reuses_only_committed_validators_and_has_no_new_items(self):
        self.tick()
        unchanged = {**self.result, "request_started_at": "2026-10-10T10:00:00Z",
                     "received_at": "2026-10-10T10:00:01Z", "items": [],
                     "status": "not_modified", "http_status": 304, "headers": {}}
        self.tick(unchanged, now=self.now + 7200)
        self.assertEqual(self.fetcher.call_args.kwargs["etag"], '"one"')
        self.assertEqual(self.count("news_receipts"), 2)
        self.assertEqual(self.count("news_versions"), 1)

    def test_unsolicited_304_is_error_not_success(self):
        unchanged = {**self.result, "items": [], "status": "not_modified", "http_status": 304}
        rows = self.tick(unchanged)
        self.assertEqual(rows[0]["status"], "error")
        self.assertIsNone(status(self.root)["sources"][0]["last_success"])

    def test_partial_parse_clears_conditional_validators(self):
        self.tick({**self.result, "parser_error_count": 1, "flags": ["invalid_entry"]})
        later = {**self.result, "request_started_at": "2026-10-10T10:00:00Z",
                 "received_at": "2026-10-10T10:00:01Z"}
        self.tick(later, now=self.now + 7200)
        self.assertIsNone(self.fetcher.call_args.kwargs["etag"])

    def test_failure_does_not_erase_prior_success_or_publish_body(self):
        self.tick()
        failed = {**self.result, "request_started_at": "2026-10-10T10:00:00Z",
                  "received_at": "2026-10-10T10:00:01Z", "items": [], "status": "error",
                  "http_status": 503, "headers": {}}
        rows = self.tick(failed, now=self.now + 7200)
        self.assertEqual(status(self.root)["sources"][0]["last_success"], self.result["received_at"])
        self.assertNotIn("items", rows[0])

    def test_quarantined_dates_refetch_unconditionally_and_accept_only_later(self):
        future = copy.deepcopy(self.result)
        future["items"][0]["published_at"] = "2026-10-10T09:00:00Z"
        self.assertEqual(self.tick(future)[0]["quarantined_count"], 1)
        self.assertIsNone(status(self.root)["sources"][0]["last_success"])
        future.update(request_started_at="2026-10-10T10:00:00Z", received_at="2026-10-10T10:00:01Z")
        row = self.tick(future, now=self.now + 7200)[0]
        self.assertIsNone(self.fetcher.call_args.kwargs["etag"])
        self.assertEqual(row["accepted_count"], 1)
        self.assertEqual(self.count("news_versions"), 1)
        with sqlite3.connect(self.root / "metadata.sqlite") as db:
            first = db.execute("SELECT first_accepted_at FROM news_accepted_versions").fetchone()[0]
        self.assertEqual(first, "2026-10-10T10:00:01.000000Z")

    def test_source_requiring_original_title_rejects_transformation(self):
        self.config["sources"][0]["require_unmodified_title"] = True
        row = self.tick({**self.result, "flags": ["title_normalized"]})[0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(self.count("news_versions"), 0)

    def test_extreme_retry_after_pauses_without_poisoning_pending_replay(self):
        failed = {**self.result, "items": [], "status": "error", "http_status": 429,
                  "headers": {"retry-after": "9" * 400}}
        rows = self.tick(failed)
        self.assertIn("retry_after_outside_supported_horizon_source_paused", rows[0]["flags"])
        self.assertFalse(status(self.root)["sources"][0]["pending_receipt"])
        self.assertEqual(self.tick(now=self.now + 86400), [])
        self.fetcher.assert_not_called()

    def test_source_url_change_rejected_and_disabled_source_never_fetches(self):
        self.tick()
        self.config["sources"][0]["url"] = "https://example.org/different"
        with self.assertRaises(ValueError):
            self.tick(now=self.now + 7200)
        self.config["sources"][0]["enabled"] = False
        self.assertEqual(self.tick(now=self.now + 7200), [])
        self.fetcher.assert_not_called()

    def test_storage_cap_stops_without_deleting_data(self):
        self.tick()
        self.config["max_state_bytes"] = 1
        with self.assertRaisesRegex(ValueError, "storage cap"):
            self.tick(now=self.now + 7200)
        self.assertEqual(self.count("news_receipts"), 1)

    def test_lock_blocks_second_process_and_releases(self):
        path = self.root / "lock"
        with file_lock(path):
            with self.assertRaises(ValueError):
                with file_lock(path):
                    self.fail("second owner acquired lock")
        with file_lock(path):
            pass

    def test_config_requires_policy_and_minimum_cadence(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        self.assertEqual(load_config(path), self.config)
        for key, value in [("policy_status", "unknown"), ("poll_interval_seconds", 1),
                           ("enabled", 1), ("url", "http://example.org/feed")]:
            config = copy.deepcopy(self.config)
            config["sources"][0][key] = value
            path.write_text(json.dumps(config))
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_config(path)

    def test_retry_after_http_date_and_invalid_value(self):
        self.assertEqual(retry_delay({"retry-after": "Sat, 10 Oct 2026 09:00:01 GMT"}, self.result["received_at"]), 3600)
        self.assertEqual(retry_delay({"retry-after": "nonsense"}, self.result["received_at"]), 0)

    def test_error_backoff_never_shortens_long_source_interval(self):
        self.config["sources"][0]["poll_interval_seconds"] = 604800
        self.tick({**self.result, "status": "error", "items": [], "http_status": 503})
        self.assertGreaterEqual(status(self.root)["sources"][0]["next_due"],
                                epoch(self.result["received_at"]) + 604800)


if __name__ == "__main__":
    unittest.main()
