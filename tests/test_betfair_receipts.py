"""Timestamp, hashing and durable-write tests in temporary directories."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from betangel_research import collection
from betangel_research.betfair import receipts
from betangel_research.betfair.errors import SecretLeak
from betangel_research.betfair.secret import Secret
from betangel_research.temporal import timestamp_ms

import test_betfair_support as support


class ReceiptsTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def test_parse_utc_ns_microseconds_and_offsets(self):
        base = timestamp_ms("2020-01-01T12:00:00Z") * 1_000_000
        self.assertEqual(receipts.parse_utc_ns("2020-01-01T12:00:00Z"), base)
        self.assertEqual(receipts.parse_utc_ns("2020-01-01T12:00:00.123457Z"), base + 123_457_000)
        self.assertEqual(receipts.parse_utc_ns("2020-01-01T12:00:00.123Z"), base + 123_000_000)
        self.assertEqual(receipts.parse_utc_ns("2020-01-01T13:00:00.5+01:00"), base + 500_000_000)
        self.assertEqual(receipts.parse_utc_ns("2020-01-01T07:30:00-04:30"), base)
        self.assertEqual(receipts.utc_from_ns(base + 123_457_999), "2020-01-01T12:00:00.123457Z")
        self.assertEqual(receipts.utc_ms_from_ns(base + 123_457_999), "2020-01-01T12:00:00.123Z")
        self.assertEqual(receipts.utc_compact_from_ns(base), "20200101T120000Z")
        clock = support.FakeClock()
        self.assertEqual(clock.utc_now(), support.BASE_UTC)
        # One clock read, round-tripped: deterministic whatever second boundary falls in between.
        now_ns = receipts.SYSTEM_CLOCK.time_ns()
        self.assertEqual(receipts.parse_utc_ns(receipts.utc_from_ns(now_ns)), now_ns // 1000 * 1000)
        self.assertRegex(receipts.SYSTEM_CLOCK.utc_now(), r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}\.[0-9]{6}Z$")

    def test_parse_utc_ns_rejects_naive(self):
        for text in ("2020-01-01T12:00:00", "2020-01-01 12:00:00Z", "2020-01-01T12:00:00.1234567Z",
                     "2020-01-01T12:00:00z", "2020-13-01T12:00:00Z", "", None, 1577880000, "2020-01-01",
                     # non-ASCII digits (Arabic-Indic, fullwidth) that \d and int() would accept
                     "2020-01-01T12:00:00.\u0661\u0662\u0663Z", "\uff12020-01-01T12:00:00Z",
                     "2020-01-01T12:00:00+\u0660\u0661:00"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    receipts.parse_utc_ns(text)

    def test_canonical_sha_equals_collection_digest(self):
        value = {"b": [1, 2.5, None, True], "a": "Ünïcødé £", "nested": {"z": 1, "y": {"x": "1.1"}}}
        self.assertEqual(receipts.canonical_sha256(value), collection._digest(value))
        self.assertEqual(receipts.canonical_json({"b": 1, "a": 2}), b'{"a":2,"b":1}')
        with self.assertRaises(ValueError):
            receipts.canonical_json({"x": float("nan")})
        path = self.dir / "blob.bin"
        path.write_bytes(b"x" * (3 * 1024 * 1024 + 7))
        self.assertEqual(receipts.file_sha256(path), receipts.bytes_sha256(path.read_bytes()))

    def test_atomic_write_full_fsync_called(self):
        target = self.dir / "receipt.json"
        with patch.object(receipts, "full_fsync", wraps=receipts.full_fsync) as spy:
            digest = receipts.atomic_write_json(target, {"b": 1, "a": "£"})
        self.assertGreaterEqual(spy.call_count, 1)
        data = target.read_bytes()
        self.assertEqual(digest, receipts.bytes_sha256(data))
        self.assertEqual(json.loads(data), {"a": "£", "b": 1})
        self.assertTrue(data.startswith(b'{\n  "a"'))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["receipt.json"])
        # F_FULLFSYNC refused: the temp file itself must still be fsynced (then the directory).
        opened = []
        real_open = os.open

        def recording_open(path, flags, *args):
            fd = real_open(path, flags, *args)
            opened.append((Path(path).name, fd))
            return fd

        with patch.object(receipts.fcntl, "fcntl", side_effect=OSError("unsupported")) as full, \
                patch.object(receipts.os, "open", side_effect=recording_open), \
                patch.object(receipts.os, "fsync") as fsync:
            receipts.atomic_write_json(target, {"v": 2})
        if hasattr(receipts.fcntl, "F_FULLFSYNC"):
            self.assertEqual(full.call_count, 1)
        temp_fd = opened[0][1]
        self.assertTrue(opened[0][0].startswith(".receipt.json.tmp-"))
        self.assertEqual(fsync.call_count, 2)                     # temp file, then its directory
        self.assertEqual(fsync.call_args_list[0].args, (temp_fd,))
        ledger = self.dir / "fallback.jsonl"
        opened.clear()
        with patch.object(receipts.fcntl, "fcntl", side_effect=OSError("unsupported")), \
                patch.object(receipts.os, "open", side_effect=recording_open), \
                patch.object(receipts.os, "fsync") as fsync:
            receipts.append_jsonl(ledger, {"v": 1})
        self.assertEqual([call.args for call in fsync.call_args_list], [(opened[0][1],)])
        with self.assertRaises(ValueError):
            receipts.atomic_write_json(target, {"v": float("inf")})
        self.assertEqual(json.loads(target.read_text()), {"v": 2})

    def test_append_jsonl_refuses_secret(self):
        Secret(support.PLANTED_SECRET)
        ledger = self.dir / "ledger.jsonl"
        receipts.append_jsonl(ledger, {"b": 2, "a": 1})
        receipts.append_jsonl(ledger, {"c": "£"})
        self.assertEqual(ledger.read_text(encoding="utf-8"), '{"a":1,"b":2}\n{"c":"£"}\n')
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        before = ledger.read_bytes()
        with self.assertRaises(SecretLeak):
            receipts.append_jsonl(ledger, {"note": support.PLANTED_SECRET})
        with self.assertRaises(SecretLeak):
            receipts.append_jsonl(ledger, {"sessionToken": "anything"})
        with self.assertRaises(ValueError):
            receipts.append_jsonl(ledger, ["not", "a", "row"])
        self.assertEqual(ledger.read_bytes(), before)

    def test_strict_json_loads_rejects_nan_infinity(self):
        for index, text in enumerate(('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}', '[NaN]', '{"a": 1, "a": 2}',
                                      # overflowing literals become inf without ever reaching parse_constant
                                      '1e400', '-1e400', '{"a": -1e999}', '[1E400]', '{"x": {"y": 1.5e309}}',
                                      # deep enough for RecursionError on every supported Python
                                      '[' * 1_000_000 + ']' * 1_000_000)):
            with self.subTest(case=index, text=text[:40]):
                with self.assertRaises(ValueError) as caught:
                    receipts.strict_json_loads(text)
                self.assertNotIsInstance(caught.exception.__context__, RecursionError)
        self.assertEqual(receipts.strict_json_loads('{"x": 1.5, "y": [1e3]}'), {"x": 1.5, "y": [1000.0]})

    def test_code_hashes_and_git_state(self):
        hashes = receipts.code_hashes(support.REPO_ROOT)
        self.assertIn("betangel_research/stream.py", hashes)
        self.assertIn("betangel_research/betfair/receipts.py", hashes)
        self.assertTrue(all(len(v) == 64 for v in hashes.values()))

        def fake_git(argv, **kwargs):
            self.assertFalse(any(k.startswith("GIT_") for k in kwargs["env"]))
            if argv[1] == "rev-parse":
                return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
            return subprocess.CompletedProcess(argv, 0, " M file.py\n", "")

        self.assertEqual(receipts.git_state(support.REPO_ROOT, runner=fake_git),
                         {"git_revision": "abc123", "working_tree_dirty": True})

        def no_git(argv, **kwargs):
            raise FileNotFoundError("git")

        self.assertEqual(receipts.git_state(self.dir, runner=no_git),
                         {"git_revision": None, "working_tree_dirty": None})


if __name__ == "__main__":
    unittest.main()
