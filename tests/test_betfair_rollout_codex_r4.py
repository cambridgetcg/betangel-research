"""PR #41 Codex findings (2026-10-22, on 853ee78 / c6e9c92): proposed regression tests, offline.

Each test asserts the FIXED behaviour, so on c6e9c92 it fails, and after the matching fix sketch it passes.
- r4351297987 (recorder.py:515): the recorder's step-8 ledger check must hold under the ledger lock that reserves.
- r4351297998 (recording_day.py:386-389): an incomplete day file is day_file_unreadable before any spawn.
- r4351298006 (recordings.py:1002-1004): an orphan recv file beside a partial receipt fails verify-day.
Synthetic credentials, a fake clock, scripted HTTP and stream, temporary state roots only.
"""

import unittest

from betangel_research.betfair.auth import SessionManager, ledger_status
from betangel_research.betfair.receipts import atomic_write_json

import test_betfair_support as support
import test_betfair_support_stream as fx
from test_betfair_recording_day import WrapperCase
from test_betfair_recordings import beats, definition, image, seg, t
from test_betfair_rollout_r3 import R3Case, login_ok
from test_betfair_rollout_recordings import Case as RecordingsCase

NS = 1_000_000_000


# --- r4351297987: the ledger check and the reservation under one lock ---------------------------------------------


class RacingSession(SessionManager):
    """The recorder's session. `between` runs once, after the run's login guard has read the ledger and before this
    login's _reserve takes the ledger lock (auth.py:520-526): another process's reservation lands in that window."""
    between = None

    def _login_request(self):
        request = super()._login_request()
        hook, self.between = self.between, None
        if hook is not None:
            hook()
        return request


class LockedLedgerCheckTests(R3Case):
    def race(self):
        kill, _ = self.session([])                 # kill's own SessionManager: another process, no guard
        held = {}
        session, transport = self.session([support.transport_failure("receive"), login_ok()], cls=RacingSession)
        session.between = lambda: held.setdefault("attempt", kill._reserve(emergency=True, coexistence_ack=False))
        receipt = self.record(session, [fx.handshake()])
        # kill's emergency attempt then fails too (a common cause: SSO down, an expired certificate, a network drop).
        kill._record(held["attempt"], outcome="unknown", outcome_status="transport_receive",
                     outcome_error="scripted_failure", emergency=True, coexistence_ack=False)
        return receipt, transport

    def test_a_reservation_after_the_guard_read_refuses_the_recorders_login(self):
        receipt, transport = self.race()
        self.assertEqual(transport.requests, [])                                   # the recorder sent nothing
        self.assertEqual(receipt["stop_reason"], "login_ledger_not_clean")
        self.assertEqual(self.outcomes(), ["unknown"])                             # kill's failure only
        self.assertTrue(ledger_status(self.config, self.paths, clock=self.clock)["emergency_login_allowed"])
        self.kill_login()                                                          # kill's retry still goes


# --- r4351297998: the complete day-file schema before any spawn -----------------------------------------------------


class IncompleteDayFileTests(WrapperCase):
    def base(self):
        return {"schema_version": 1, "date": "2026-10-26", "state": "open", "close_code": None,
                "close_invocation_id": None, "counts": {"first": 0, "restart": 0, "resumption": 0}, "attempts": [],
                "summary": {"outcomes": [], "finalized": [], "review": []}}

    def finished_entry(self, **drop):
        entry = {"kind": "first", "kind_count": 1, "invocation_id": "d20261026-a1-0000abcd",
                 "spawn_wall_ns": self.world.clock.time_ns() - 3600 * NS, "state": "finished",
                 "outcome": "finalized", "stop_reason": "reconnect_budget_exhausted", "refusal_code": None,
                 "decision": "restart", "codes": [], "session_id": None, "receipt_sha256": None, "touched": False}
        for key in drop:
            del entry[key]
        return entry

    def write(self, day):
        atomic_write_json(self.dir / "2026-10-26.json", day)

    def assert_unreadable_without_spawn(self):
        self.assertEqual(self.run_day({}), 2)
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.world.codes("stopped")[-1]["code"], "day_file_unreadable")

    def test_control_a_complete_open_day_runs(self):
        self.write(self.base())
        self.assertEqual(self.run_day({}), 0)
        self.assertEqual(len(self.spawns()), 1)

    def test_open_day_without_summary(self):
        day = self.base()
        del day["summary"]
        self.write(day)
        self.assert_unreadable_without_spawn()

    def test_summary_without_a_list(self):
        day = self.base()
        del day["summary"]["finalized"]
        self.write(day)
        self.assert_unreadable_without_spawn()

    def test_finished_attempt_without_decision(self):
        day = self.base()
        day["counts"]["first"] = 1
        day["attempts"] = [self.finished_entry(decision=None)]
        self.write(day)
        self.assert_unreadable_without_spawn()

    def test_finished_attempt_without_outcome(self):
        day = self.base()
        day["counts"]["first"] = 1
        day["attempts"] = [self.finished_entry(outcome=None)]
        day["attempts"][0]["decision"] = "done"           # no close_code: next_kind_on_entry reads the outcome
        self.write(day)
        self.assert_unreadable_without_spawn()


# --- r4351298006: recv files beside a partial receipt ---------------------------------------------------------------


class PartialRecvTests(RecordingsCase):
    def crashed(self):
        """One listed segment (seg-000) and the canonical open crash segment seg-001 (data and recv)."""
        closed_part = [image(t("14:20:00"), ("1.101", definition("15:00:00")))] + beats(t("14:20:00"), t("14:29:00"))
        crashed = [image(t("14:30:00"), ("1.101", definition("15:00:00")))] + beats(t("14:30:00"), t("15:00:00"))
        return self.writer.session(t("14:20:00"), [seg(closed_part)], crash_open=seg(crashed))

    def test_control_the_canonical_crash_pair_verifies(self):
        self.crashed()
        self.assertEqual(self.verify()["status"], "verified", self.verify()["failures"])

    def test_control_a_data_file_without_its_recv_mate_verifies(self):
        """_SegmentWriter.__init__ opens the data file first (recorder.py:214-216): a crash between leaves this."""
        directory = self.crashed()
        (directory / "seg-001.recv.jsonl").unlink()
        self.assertEqual(self.verify()["status"], "verified", self.verify()["failures"])

    def test_orphan_recv_at_an_impossible_index_fails(self):
        directory = self.crashed()
        (directory / "seg-005.recv.jsonl").write_bytes(b"")
        self.assertEqual(self.failed(), {(directory.name, "recv_not_in_receipt", 5)})

    def test_orphan_recv_at_the_open_index_without_its_data_file_fails(self):
        directory = self.crashed()
        (directory / "seg-001.jsonl").unlink()           # the crash segment's data is gone; its recv rows remain
        self.assertEqual(self.failed(), {(directory.name, "recv_not_in_receipt", 1)})

    def test_padded_recv_alias_fails(self):
        directory = self.crashed()
        (directory / "seg-0001.recv.jsonl").write_bytes((directory / "seg-001.recv.jsonl").read_bytes())
        self.assertEqual(self.failed(), {(directory.name, "recv_not_in_receipt", 1)})


if __name__ == "__main__":
    unittest.main()
