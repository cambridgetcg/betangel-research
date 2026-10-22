"""Recording rollout (design v9 §3.1, §3.2, §8 recordings.py): the two recordings.py fixes, offline, on synthetic
sessions laid out as the recorder writes them (test_betfair_recordings.SessionWriter).

bot-33-578: an explicit absent segment that a dropped_explicit flag relied on is evidence too, so an unverified one
(a crashed or tampered session) flags the row unverified. bot-33-740: nothing unlisted may sit beside a final receipt
(segment_not_in_receipt, recv_not_in_receipt); a crashed session may leave exactly one unlisted segment, at index
len(listed). R2: the ledger row's invocation_id and segments count must match the strict-parsed receipt.
"""

import contextlib
import io
import json
import random
import unittest

from betangel_research.betfair.cli import main
from betangel_research.betfair.receipts import atomic_write_json
from betangel_research.betfair.recordings import verify_day

import test_betfair_support as support
from test_betfair_recordings import DAY, SessionWriter, beats, change, definition, image, seg, t


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, deps={"clock": support.FakeClock(start=f"{DAY}T23:00:00.000000Z")})
    return code, out.getvalue(), err.getvalue()


class Case(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        random.seed(33)
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock(start=f"{DAY}T23:00:00.000000Z")
        self.writer = SessionWriter(self.paths)

    def rows(self):
        from betangel_research.betfair.recordings import build_index
        build_index(self.paths, root=self.paths.recordings_dir, day=DAY, clock=self.clock)
        document = json.loads((self.paths.recordings_index_dir / f"{DAY}.json").read_text())
        return {row["market_id"]: row for row in document["markets"]}

    def verify(self):
        return verify_day(self.paths, root=self.paths.recordings_dir, day=DAY)

    def failed(self):
        return {(f["session_id"], f["check"], f.get("segment")) for f in self.verify()["failures"]}


class AbsenceEvidenceTests(Case):
    """bot-33-578 (§3.1): unverified absence evidence."""

    def three_sessions(self, middle_mode):
        """Verified 12:30-13:20 sees 1.101 (off 14:00); a crashed session 13:21-13:40 whose listed segment (mode
        middle_mode) leaves 1.101 out; verified 13:45-14:10 sees it again, in play and CLOSED."""
        first = [image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("13:20:00"))
        self.writer.session(t("12:30:00"), [seg(first)])
        middle = [image(t("13:21:00"), ("1.108", definition("16:00:00")))] + beats(t("13:21:00"), t("13:40:00"))
        tail = [image(t("13:41:00"), ("1.108", definition("16:00:00")))] + beats(t("13:41:00"), t("13:43:00"))
        self.writer.session(t("13:21:00"), [seg(middle, mode=middle_mode)], crash_open=seg(tail, mode=middle_mode))
        last = ([image(t("13:45:00"), ("1.101", definition("14:00:00")))] + beats(t("13:45:00"), t("14:10:00"))
                + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED")])
        self.writer.session(t("13:45:00"), [seg(last, close="max_seconds_reached")])

    def test_witness_a_crashed_explicit_segment_between_two_verified_sessions(self):
        self.three_sessions("explicit")
        row = self.rows()["1.101"]
        self.assertEqual((row["class"], set(row["flags"])), ("UNVERIFIED", {"dropped_explicit", "gap", "unverified"}))
        self.assertEqual(row["gaps"][0]["cause"], "dropped_explicit")
        self.assertTrue(all(c["verified"] for c in row["coverage"]))      # the observations themselves verify
        # verify-day still passes: a crashed session is classified, not failed; the index row carries the flag.
        self.assertEqual(self.verify()["status"], "verified", self.verify()["failures"])

    def test_control_coarse_crash_stays_gap(self):
        self.three_sessions("coarse")
        row = self.rows()["1.101"]
        self.assertEqual((row["class"], set(row["flags"])), ("GAP", {"gap"}))
        self.assertEqual(row["gaps"][0]["cause"], "between_sessions")

    def never_in_window(self, crashed, mode="explicit"):
        """Seen only 09:00-10:00 (off 16:00); a later session (crashed or closed) 14:00-18:00 leaves it out."""
        morning = [image(t("09:00:00"), ("1.160", definition("16:00:00")))] + beats(t("09:00:00"), t("10:00:00"))
        self.writer.session(t("09:00:00"), [seg(morning)])
        afternoon = [image(t("14:00:00"), ("1.161", definition("17:00:00")))] + beats(t("14:00:00"), t("18:00:00"))
        if crashed:
            tail = [image(t("18:01:00"), ("1.161", definition("17:00:00")))] + beats(t("18:01:00"), t("18:03:00"))
            self.writer.session(t("14:00:00"), [seg(afternoon, mode=mode)], crash_open=seg(tail, mode=mode))
        else:
            self.writer.session(t("14:00:00"), [seg(afternoon, mode=mode)])
        return self.rows()["1.160"]

    def test_witness_e_never_in_window_with_a_crashed_explicit_segment(self):
        row = self.never_in_window(crashed=True)
        self.assertEqual(row["class"], "UNVERIFIED")
        self.assertEqual(set(row["flags"]),
                         {"dropped_explicit", "late_start", "never_in_window", "no_close", "unverified"})

    def test_control_all_verified_stays_dropped_explicit(self):
        """As test_dropped_explicit (tests/test_betfair_recordings.py) and its never-in-window twin."""
        row = self.never_in_window(crashed=False)
        self.assertEqual(row["class"], "DROPPED_EXPLICIT")
        self.assertNotIn("unverified", row["flags"])
        self.writer = SessionWriter(self.paths)
        seg0 = [image(t("12:30:00"), ("1.101", definition("14:00:00")), ("1.108", definition("16:00:00")))] + \
            beats(t("12:30:00"), t("13:30:00"))
        seg1 = [image(t("13:30:01"), ("1.108", definition("16:00:00")))] + beats(t("13:30:01"), t("14:20:00"))
        self.writer.session(t("12:30:00"), [seg(seg0, mode="explicit"), seg(seg1, mode="explicit")])
        row = self.rows()["1.101"]
        self.assertEqual((row["class"], set(row["flags"])), ("DROPPED_EXPLICIT", {"dropped_explicit", "gap",
                                                                                 "no_close"}))

    def test_control_coarse_crash_never_in_window_stays_late_start(self):
        row = self.never_in_window(crashed=True, mode="coarse")
        self.assertEqual((row["class"], set(row["flags"])), ("LATE_START", {"late_start", "never_in_window",
                                                                            "no_close"}))

    def test_control_tampered_observation_stays_unverified(self):
        lines = ([image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("14:10:00"))
                 + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED")])
        directory = self.writer.session(t("12:30:00"), [seg(lines)])
        path = directory / "seg-000.jsonl"
        path.write_bytes(path.read_bytes().replace(b"9.87654321", b"9.87654322", 1))
        self.assertEqual(self.rows()["1.101"]["class"], "UNVERIFIED")


class UnlistedSegmentTests(Case):
    """bot-33-740 (§3.2): segments and recv files beside a final receipt that it does not list."""

    def setUp(self):
        super().setUp()
        lines = [image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("13:20:00"))
        more = [image(t("13:20:02"), ("1.101", definition("14:00:00")))] + beats(t("13:20:02"), t("14:10:00"))
        self.closed = self.writer.session(t("12:30:00"), [seg(lines), seg(more, close="max_seconds_reached")])
        self.assertEqual(self.verify()["status"], "verified")

    def test_extra_valid_seg_pair_fails_with_exit_2(self):
        for suffix in (".jsonl", ".recv.jsonl"):
            (self.closed / f"seg-002{suffix}").write_bytes((self.closed / f"seg-000{suffix}").read_bytes())
        self.assertEqual(self.failed(), {(self.closed.name, "segment_not_in_receipt", 2),
                                         (self.closed.name, "recv_not_in_receipt", 2)})
        support.write_test_config(self.paths)
        code, out, err = run_cli(["--state-root", str(self.paths.root), "--config", str(self.paths.config_path),
                                  "verify-day", "--day", DAY])
        self.assertEqual((code, err.strip()), (2, "Betfair input error: day_failed_verification"))

    def test_orphan_recv_fails(self):
        (self.closed / "seg-005.recv.jsonl").write_bytes(b"")
        self.assertEqual(self.failed(), {(self.closed.name, "recv_not_in_receipt", 5)})

    def test_garbage_seg_fails(self):
        (self.closed / "seg-003.jsonl").write_bytes(b"\x00garbage\n")
        self.assertEqual(self.failed(), {(self.closed.name, "segment_not_in_receipt", 3)})

    def crashed(self):
        closed_part = [image(t("14:20:00"), ("1.101", definition("15:00:00")))] + beats(t("14:20:00"), t("14:29:00"))
        crashed = [image(t("14:30:00"), ("1.101", definition("15:00:00")))] + beats(t("14:30:00"), t("15:00:00"))
        return self.writer.session(t("14:20:00"), [seg(closed_part)], crash_open=seg(crashed))

    def test_crashed_session_with_one_unlisted_segment_at_len_listed_is_counted(self):
        directory = self.crashed()
        self.assertTrue((directory / "seg-001.jsonl").exists())           # index len(listed) == 1
        result = self.verify()
        self.assertEqual(result["status"], "verified", result["failures"])
        self.assertEqual((result["sessions_by_status"]["crashed"], result["open_segments"]), (1, 1))

    def test_padded_alias_beside_the_canonical_open_segment_fails(self):
        """ROLL-3 / REC-1: seg-001 and seg-0001 both parse as index 1; only the recorder's own spelling
        seg-{len(listed):03d}.jsonl may stay unlisted."""
        directory = self.crashed()
        for suffix in (".jsonl", ".recv.jsonl"):
            (directory / f"seg-0001{suffix}").write_bytes((directory / f"seg-001{suffix}").read_bytes())
        self.assertEqual(self.failed(), {(directory.name, "segment_not_in_receipt", 1)})

    def test_a_lone_padded_alias_fails(self):
        directory = self.crashed()
        for suffix in (".jsonl", ".recv.jsonl"):
            (directory / f"seg-001{suffix}").rename(directory / f"seg-0001{suffix}")
        self.assertEqual(self.failed(), {(directory.name, "segment_not_in_receipt", 1)})

    def test_crashed_session_with_two_unlisted_segments_fails(self):
        directory = self.crashed()
        for suffix in (".jsonl", ".recv.jsonl"):
            (directory / f"seg-002{suffix}").write_bytes((directory / f"seg-001{suffix}").read_bytes())
        self.assertEqual(self.failed(), {(directory.name, "segment_not_in_receipt", 2)})


class LedgerRowTests(Case):
    """R2 (§3.2): the ledger row against the strict-parsed receipt.json."""

    def setUp(self):
        super().setUp()
        lines = [image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("13:20:00"))
        self.closed = self.writer.session(t("12:30:00"), [seg(lines, close="max_seconds_reached")])
        self.ledger = self.paths.recordings_dir / "run-ledger.jsonl"
        self.row = json.loads(self.ledger.read_text())

    def set_receipt_id(self, value):
        receipt = json.loads((self.closed / "receipt.json").read_text())
        receipt["invocation_id"] = value
        self.row["receipt_sha256"] = atomic_write_json(self.closed / "receipt.json", receipt)

    def write_row(self, **changes):
        row = {k: v for k, v in dict(self.row, **changes).items() if v is not None}
        self.ledger.write_text(json.dumps(row) + "\n")

    def checks(self):
        return {check for _, check, _ in self.failed()}

    def test_control_both_lack_the_id_and_the_day_passes(self):
        self.assertNotIn("invocation_id", self.row)
        self.assertEqual(self.verify()["status"], "verified")

    def test_row_has_an_id_and_the_receipt_lacks_it(self):
        self.write_row(invocation_id="d20261021-a1-0badf00d")
        self.assertEqual(self.checks(), {"ledger_invocation_mismatch"})

    def test_receipt_has_an_id_and_the_row_lacks_it(self):
        self.set_receipt_id("d20261021-a1-0badf00d")
        self.write_row()
        self.assertEqual(self.checks(), {"ledger_invocation_mismatch"})

    def test_ids_differ(self):
        self.set_receipt_id("d20261021-a1-0badf00d")
        self.write_row(invocation_id="d20261021-a2-0badf00e")
        self.assertEqual(self.checks(), {"ledger_invocation_mismatch"})
        self.write_row(invocation_id="d20261021-a1-0badf00d")
        self.assertEqual(self.verify()["status"], "verified")

    def test_segments_count_differs(self):
        self.write_row(segments=self.row["segments"] + 1)
        self.assertEqual(self.checks(), {"ledger_segments_mismatch"})


if __name__ == "__main__":
    unittest.main()
