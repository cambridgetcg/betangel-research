"""P1-08's shared core, pure parts (design v4 §3.1, §3.2, §4.5 step 2, §4.11, §7; §9.1, §9.5a), offline and synthetic.

These show the one outcome/scope table in pilot.py (its rows, its three reason rules, AUTHORITY_CODES and the one
NO_NEW_STOP set built from it), the moved code tables being the same objects under their pilot_gate names, the shared
stop-id builders and the place-report extractor with its markers, what each table entry leaves the next day's ARM
(through the pure fold), the public session_armed predicate, gap_resolve_check, arming.a7_causes and the trigger
helper the gateway and the scheduler share. Journals are synthetic event dicts (test_betfair_pilot.Pilot); no
journal file, clock, network or Keychain is touched.
"""

import os
import unittest
from unittest.mock import patch

from betangel_research.betfair import arming, pilot, pilot_gate
from betangel_research.betfair.arming import a7_causes, static_arming

import test_betfair_support as support
from test_betfair_pilot import C, DAY0, DAY_NS, HOUR, Pilot, S

REASONS = ("placing_ended", "session_not_armed", "session_ended", "session_expired", "stop_active", "record_unknown",
           "record_not_decision", "record_open_ended", "record_submitted")


def durable(code, reason=None):
    scope = pilot.scope_of(code, reason)
    return scope.action, scope.durable


class ScopeTableTests(unittest.TestCase):
    """§3.1: one table, unlisted means review, and the reason rules."""

    def test_authority_codes_are_the_nine_reasons_and_the_exempt_set_is_built_from_them(self):
        self.assertEqual(set(pilot.AUTHORITY_CODES), set(REASONS))
        self.assertEqual(pilot.NO_NEW_STOP_CODES, frozenset({"pa4_pilot_stop", "pa4_placing_ended",
                                                             "pa3_session_not_armed", "pa3_session_ended",
                                                             "pa3_session_expired"}))
        for code in pilot.NO_NEW_STOP_CODES:
            with self.subTest(code=code):
                self.assertEqual(durable(code), ("end", "none"))
        for code in ("pilot_record_unknown", "pilot_record_not_decision", "pilot_record_open_ended",
                     "pilot_record_submitted"):
            with self.subTest(code=code):
                self.assertEqual(durable(code), ("end", "review"))      # the refusal note gateway.py journals

    def test_the_moved_tables_are_one_definition(self):
        for name in ("AUTHORITY_CODES", "NO_NEW_STOP_CODES", "MARKET_LATCH", "MARKET_V9_REASONS", "SESSION_LATCH",
                     "MANUAL_BET_OPEN", "NO_NOTE_LATCH", "KILL_REASONS", "FIRST_BYTE_CODES",
                     "HALT_IMPLYING_OPEN_CODES"):
            with self.subTest(name=name):
                self.assertIs(getattr(pilot_gate, name), getattr(pilot, name))
        self.assertIs(pilot_gate.refusal_code, pilot.refusal_code)
        self.assertIs(pilot_gate.place_report_failure, pilot.place_report_failure)

    def test_halted_is_keyed_on_its_reason(self):
        cases = {("halted", "kill_engaged"): ("end", "session", "kill_file"),
                 ("halted", "kill_state_unknown"): ("end", "session", "kill_file"),
                 ("halted", "journal_halted"): ("end", "none", "halted"),
                 ("halted", "clock_regression"): ("end", "review", "clock_regression"),
                 ("halted", None): ("end", "review", "halted"),
                 ("halted", "something_else"): ("end", "review", "halted"),
                 ("kill_engaged", None): ("end", "session", "kill_file"),
                 ("kill_engaged_before_send", None): ("end", "session", "kill_file")}
        for (code, reason), expected in cases.items():
            with self.subTest(code=code, reason=reason):
                scope = pilot.scope_of(code, reason)
                self.assertEqual((scope.action, scope.durable, scope.code), expected)

    def test_every_row_and_the_review_default(self):
        market = ["validation:V13", "validation:V9b", "rate_minute", "rate_hour", "pilot_send_book_off_differs",
                  "journal_changed_since_decision", "book_fetch_failed", "market_exposure", "total_exposure",
                  "MARKET_NOT_OPEN_FOR_BETTING", "MARKET_SUSPENDED", *sorted(pilot.FIRST_BYTE_CODES)]
        session = ["rate_day", "daily_loss", "unresolved_orders", "lock_busy", "kill_file", "login_failed",
                   "unknown_unresolved", "remainder_cancel_failed", "remainder_left_executable", "session_end",
                   "session_abandoned"]
        none = ["unexplained_account_exposure", "manual_bet_open_during_session"]
        review = ["pilot_market_already_reserved", "pilot_trigger_before_deadline", "pilot_reserve_end_margin",
                  "pilot_reserve_too_late", "login_budget", "pilot_fold_failed", "pilot_reserve_head_unstable",
                  "pilot_evidence_not_stored", "pilot_decision_too_late", "pilot_decision_not_journaled",
                  "pilot_ref_reused", "pilot_intent_not_journaled", "pilot_submit_too_late", "send_gate_failed",
                  "capability_expired", "capability_decision_stale", "connect_failed", "tls_handshake_failed",
                  "send_failed", "validation:V10", "validation:V0", "pa2_tree_dirty", "r13_body_differs",
                  "admission_lost", "recorder_running", "executor_exception", "not_armed_before_lock",
                  "RUNNER_REMOVED", "BET_TAKEN_OR_LAPSED", "ERROR_IN_MATCHER", "INVALID_SESSION_INFORMATION",
                  "transport_not_sent", "never_seen_before_code"]
        for code in market:
            with self.subTest(code=code):
                self.assertEqual(durable(code), ("skip_market", "market"))
        for reason in ("market_not_open", "market_inplay"):
            self.assertEqual(durable("validation:V9", reason), ("skip_market", "market"))
        for reason in ("runner_not_active", "catalogue_missing", None):
            self.assertEqual(durable("validation:V9", reason), ("end", "review"))
        for code in session:
            with self.subTest(code=code):
                self.assertEqual(durable(code), ("end", "session"))
        for code in none:
            with self.subTest(code=code):
                self.assertEqual(durable(code), ("end", "none"))
        for code in review:
            with self.subTest(code=code):
                self.assertEqual(durable(code), ("end", "review"))
        self.assertEqual(durable("unresolved_or_foreign_orders"), ("end", "review_unless_halt"))
        self.assertEqual(durable("pilot_reserve_too_early"), ("retry", "none"))
        for code in ("settle_point_closed", "operator_confirmation_missing", "settle_market_not_closed",
                     "settle_market_bets_still_current", "settle_market_journal_changed"):
            with self.subTest(code=code):
                self.assertEqual(durable(code), ("skip_offer", "none"))
        self.assertNotIn("send_gate_failed", pilot.FIRST_BYTE_CODES)
        self.assertEqual(len(pilot.FIRST_BYTE_CODES), 7)
        self.assertNotIn("transport_not_sent", pilot.SCOPE_TABLE)              # the FAILED move's code, never a rule
        self.assertEqual(pilot.scope_of("validation:V10").code, "validation:V10")
        self.assertEqual(pilot.scope_of(None), pilot.Scope("end", "review", "refused"))

    def test_decision_skips(self):
        """A SKIP's latch is the gateway's, derived from the stops the classifier returned (pause: review; the day
        limit's cap: session; any other SKIP: market); there is no second copy here (SCOPE-3; pinned end to end in
        test_betfair_pilot_review_fixes.SkipLatchTests)."""
        self.assertFalse(hasattr(pilot, "decision_skip_scope"))
        for code in ("operator_echo_mismatch", "operator_echo_timeout", "settle_point_closed",
                     "operator_confirmation_missing", "settle_market_not_closed"):
            self.assertEqual(pilot.scope_of(code).action, "skip_offer")
        self.assertEqual(pilot.strongest_scope(["market", "session", "review"]), "review")
        self.assertEqual(pilot.strongest_scope(["none", "market"]), "market")
        self.assertIsNone(pilot.strongest_scope([]))


class ExtractorTests(unittest.TestCase):
    """§3.1 "The place-report extractor" (§13 ruling 11; P108-R3-2, P108-R4-2): markers set aside, the stronger code
    counts, scope never inferred from the outer marker."""

    PAIRS = {
        ("MARKET_NOT_OPEN_FOR_BETTING", "BET_ACTION_ERROR"): ("MARKET_NOT_OPEN_FOR_BETTING", "market"),
        ("MARKET_SUSPENDED", "BET_ACTION_ERROR"): ("MARKET_SUSPENDED", "market"),
        ("RUNNER_REMOVED", "BET_ACTION_ERROR"): ("RUNNER_REMOVED", "review"),
        ("BET_TAKEN_OR_LAPSED", "BET_ACTION_ERROR"): ("BET_TAKEN_OR_LAPSED", "review"),
        ("ERROR_IN_MATCHER", "BET_ACTION_ERROR"): ("ERROR_IN_MATCHER", "review"),
        ("A_CODE_NOBODY_KNOWS", "BET_ACTION_ERROR"): ("A_CODE_NOBODY_KNOWS", "review"),
        (None, "BET_ACTION_ERROR"): ("BET_ACTION_ERROR", "review"),
        ("ERROR_IN_ORDER", "MARKET_SUSPENDED"): ("MARKET_SUSPENDED", "market"),
        ("RUNNER_REMOVED", "MARKET_SUSPENDED"): ("RUNNER_REMOVED", "review"),
        ("MARKET_NOT_OPEN_FOR_BETTING", "ERROR_IN_MATCHER"): ("ERROR_IN_MATCHER", "review"),
        ("ERROR_IN_ORDER", None): ("ERROR_IN_ORDER", "review"),
        ("MARKET_NOT_OPEN_FOR_BETTING", "MARKET_SUSPENDED"): ("MARKET_NOT_OPEN_FOR_BETTING", "market"),
    }

    @staticmethod
    def report(instruction, whole):
        return {"status": "FAILURE", "error_code": whole, "market_id": "1.5", "source": "betfair",
                "instructions": [{"customer_order_ref": "ref-1", "status": "FAILURE", "error_code": instruction,
                                  "order_status": None, "bet_id": None, "placed_date": None,
                                  "average_price_matched_h": None, "size_matched_p": None}]}

    def test_pairs(self):
        for (instruction, whole), (code, scope) in self.PAIRS.items():
            with self.subTest(instruction=instruction, report=whole):
                found = pilot.place_report_failure(self.report(instruction, whole))
                self.assertEqual((found, pilot.scope_of(found).durable), (code, scope))
                self.assertEqual(pilot_gate.place_report_failure(self.report(instruction, whole)), found)
        success = dict(self.report(None, None), status="SUCCESS")
        success["instructions"][0]["status"] = "SUCCESS"
        self.assertIsNone(pilot.place_report_failure(success))
        self.assertEqual(pilot.place_report_failure(dict(self.report(None, None))), "place_report_failure")
        self.assertIsNone(pilot.place_report_failure(None))


class StopIdTests(unittest.TestCase):
    """§3.2 builders (P108-R3-1): ids normalised, so a raw Betfair or validation code still gives a valid stop id."""

    def test_session_stop_id_and_the_gateway_wrapper(self):
        self.assertEqual(pilot.session_stop_id("rate_day", 77), "rate-day-77-session")
        self.assertEqual(pilot.session_stop_id("validation:V13", 5), "validation-v13-5-session")
        self.assertEqual(pilot_gate.session_stop_id("kill_file", reservation_seq=None, open_id="open-0A1b"),
                         pilot.session_stop_id("kill_file", "0a1b"))
        self.assertEqual(pilot_gate.session_stop_id("lock_busy", reservation_seq=9, open_id="open-x"),
                         "lock-busy-9-session")
        self.assertEqual(pilot.session_stop_id("session_end", 12), "session-end-12-session")

    def test_carried_review_stop_id_normalises_the_code(self):
        for code, expected in (("RUNNER_REMOVED", "runner-removed-41-57"), ("validation:V10", "validation-v10-41-57"),
                               ("send_gate_failed", "send-gate-failed-41-57"),
                               ("capability_expired", "capability-expired-41-57")):
            with self.subTest(code=code):
                stop_id = pilot.carried_review_stop_id(code, 41, 57)
                self.assertEqual(stop_id, expected)
                self.assertRegex(stop_id, pilot.STOP_ID_RE.pattern)
        # The raw text is what the normalisation is for: it fails STOP_ID_RE.
        self.assertIsNone(pilot.STOP_ID_RE.fullmatch(pilot.stop_id_for("RUNNER_REMOVED", 41) + "-57"))
        self.assertEqual(pilot.carried_review_stop_id("login_budget", None, 9), "login-budget-9")

    def test_a_refusal_note_is_its_own_carrier(self):
        journal = Pilot(pt4=True)
        session = journal.arm(0)
        seq = journal.note("refusal", session_id=session, open_id="open-1", reservation_seq=None,
                           code="executor_exception", row_sha256=None, book_sha256=None)
        stops = [s for s in journal.fold().active_stops if s["scope"] == "review"]
        self.assertEqual([(s["stop_id"], s["reason"]) for s in stops],
                         [(pilot.carried_review_stop_id("executor_exception", None, seq), "executor_exception")])


class NextDayAuthorityTests(unittest.TestCase):
    """§9.1: what each kind of entry leaves the next day's ARM, through the pure fold. Review: the ARM pre-check sees an
    active review stop until a caused ACK. Session, market and none: nothing survives the new session_start."""

    def latched(self, durable_kind, code):
        journal = Pilot(pt4=True)
        session = journal.arm(0, until_h=20)
        if durable_kind == "review":
            journal.note("refusal", session_id=session, open_id="open-1", reservation_seq=None,
                         code=pilot.refusal_code(code), row_sha256=None, book_sha256=None)
        elif durable_kind == "session":
            anchor = journal.events[-1]["seq"]
            journal.stop(pilot.session_stop_id(pilot.scope_of(code).code, anchor), "session",
                         pilot.refusal_code(pilot.scope_of(code).code), source=anchor)
        journal.arm(1, until_h=20)
        return journal

    def review_ids(self, journal):
        return [s["stop_id"] for s in journal.fold().active_stops if s["scope"] == "review"]

    def test_each_kind(self):
        for code in ("pilot_fold_failed", "pilot_reserve_head_unstable", "pilot_evidence_not_stored",
                     "pilot_decision_too_late", "pilot_decision_not_journaled", "pilot_reserve_too_late",
                     "pilot_market_already_reserved", "login_budget", "executor_exception", "never_seen_code"):
            with self.subTest(code=code):
                self.assertEqual(pilot.scope_of(code).durable, "review")
                journal = self.latched("review", code)
                (stop_id,) = self.review_ids(journal)
                journal.journal_due(journal.fold())                   # ARM flushes it first (pre-check 7c)
                journal.ack(stop_id, cause=True)
                self.assertEqual(self.review_ids(journal), [])
        for code in ("rate_day", "lock_busy", "login_failed", "session_end", "kill_engaged"):
            with self.subTest(code=code):
                self.assertEqual(pilot.scope_of(code).durable, "session")
                state = self.latched("session", code).fold()
                self.assertEqual((state.active_stops, state.sessions[-1]["ended_seq"]), ((), None))
        for code in ("rate_minute", "pilot_gate_deadline", "pa4_pilot_stop", "manual_bet_open_during_session"):
            with self.subTest(code=code):
                self.assertIn(pilot.scope_of(code).durable, ("market", "none"))
                self.assertEqual(self.latched("none", code).fold().active_stops, ())


class SessionArmedTests(unittest.TestCase):
    """§9.5a (C25): pilot.session_armed equals the fold's own rule, pinned by the manual-order stop the fold implies
    exactly when the public function says a session is armed."""

    def manual_intent(self, journal, at):
        return journal.add("intent", {"order": {"selection_id": 7, "handicap": 0, "side": "BACK", "price_h": 300,
                                                "size_p": 200, "persistence_type": "LAPSE"},
                                      "customer_strategy_ref": "bar-manual", "is_probe": False, "parent_ref": None,
                                      "provisional_child": False, "source": "cli_manual"},
                           at=at, intent_id=f"i-m{at}", customer_order_ref=f"m-{at % 100000}", customer_ref="c-m",
                           market_id="1.9", state_after="RECORDED")

    def test_before_at_and_after_until_and_after_a_stop(self):
        for label in ("before", "at", "after", "stopped"):
            with self.subTest(case=label):
                journal = Pilot(pt4=True)
                journal.arm(0, until_h=12)
                until = DAY0 + 12 * HOUR
                at = {"before": until - S, "at": until, "after": until + S, "stopped": until - S}[label]
                if label == "stopped":
                    journal.stop("yu-check-1", "review", "manual_check", at=until - 2 * S)
                before = journal.fold(now=at - 1)
                armed = pilot.session_armed(before, now_ns=at)
                seq = self.manual_intent(journal, at)
                implied = any(s["stop_id"] == f"manual-order-during-session-{seq}"
                              for s in journal.fold().active_stops)
                self.assertEqual(armed, implied)
                self.assertEqual(armed, label == "before")
        with self.assertRaises(pilot.PilotError):
            pilot.session_armed(Pilot().fold(), now_ns="soon")
        self.assertFalse(pilot.session_armed(Pilot().fold(), now_ns=DAY0))


class GapResolveTests(unittest.TestCase):
    """§4.11 (Q10): one known, unresolved ledger-gap occurrence."""

    def test_codes(self):
        journal = Pilot(pt4=True)
        journal.arm(0)
        journal.stop("cleared-figure-gap-5-9", "review", "cleared_figure_gap", source=5)
        journal.stop("yu-check-1", "review", "manual_check")
        state = journal.fold()
        self.assertIsNone(pilot.gap_resolve_check(state, "cleared-figure-gap-5-9"))
        self.assertEqual(pilot.gap_decision_seq(state, "cleared-figure-gap-5-9"), 5)
        self.assertEqual(pilot.gap_resolve_check(state, "yu-check-1"), "gap_not_a_ledger_gap")
        self.assertEqual(pilot.gap_resolve_check(state, "no-such-stop"), "gap_unknown_stop")
        self.assertEqual(pilot.gap_resolve_check(state, "Bad Id"), "gap_unknown_stop")
        journal.note("gap_resolved", stop_id="cleared-figure-gap-5-9", decision_seq=5, cause_written=True)
        self.assertEqual(pilot.gap_resolve_check(journal.fold(), "cleared-figure-gap-5-9"), "gap_already_resolved")


class A7CausesTests(unittest.TestCase):
    """§4.5 step 2 and §7 arming.py: the cause helper, and A7's output unchanged."""

    def setUp(self):
        self.paths = support.temp_state_paths(self)

    def a7(self, journal_state):
        report = static_arming({"orders": {"live": {}}}, command="place", paths=self.paths, cli_mode="live",
                               confirm_token=None, bet_angel_paused=False, env={}, confirmer=None,
                               keychain_runner=None, journal_anchor={}, now_ns=0, journal_state=journal_state)
        return report["by_check"].get("A7", [])

    def test_causes_and_a7(self):
        clean = {"orders": {}, "halted": False, "clock_regression": False}
        self.assertEqual(a7_causes(self.paths, clean), {"kill": False, "halt": False, "clock_regression": False,
                                                        "unfoldable": False})
        cases = {"halt": dict(clean, halted=True), "clock_regression": dict(clean, clock_regression=True),
                 "unfoldable": {"halted": True}}
        for cause, state in cases.items():
            with self.subTest(cause=cause):
                found = a7_causes(self.paths, state)
                self.assertTrue(found[cause])
                self.assertEqual(sum(bool(v) for k, v in found.items() if k != "kill"), 1)
        self.assertEqual(a7_causes(self.paths, None)["unfoldable"], False)
        self.assertEqual(a7_causes(self.paths, {"halted": False, "clock_regression": False})["unfoldable"], False)
        self.paths.kill_file.write_text("x")
        self.assertIs(a7_causes(self.paths, clean)["kill"], True)
        self.paths.kill_file.unlink()
        real = os.lstat

        def denied(path, *args, **kwargs):
            if str(path) == str(self.paths.kill_file):
                raise PermissionError("denied")
            return real(path, *args, **kwargs)

        with patch.object(arming.os, "lstat", side_effect=denied):
            self.assertIsNone(a7_causes(self.paths, clean)["kill"])

    def test_a7_output_is_unchanged(self):
        """A7 still returns ["halted"] for any cause, and nothing when clean (as kill_engaged and the fold did)."""
        with patch.dict(arming.COMMAND_CHECKS, {"place": ("A7",)}):
            clean = {"orders": {}, "halted": False, "clock_regression": False}
            self.assertEqual(self.a7(clean), [])
            self.assertEqual(self.a7(None), [])
            self.assertEqual(self.a7({"halted": False, "clock_regression": False}), [])
            for state in (dict(clean, halted=True), dict(clean, clock_regression=True), {"halted": True},
                          {"halted": True, "clock_regression": False}):
                with self.subTest(state=state):
                    self.assertEqual(self.a7(state), ["halted"])
            self.paths.kill_file.write_text("x")
            self.assertEqual(self.a7(clean), ["halted"])


class TriggerBlocksTests(unittest.TestCase):
    """§4.4 (C23): the trigger rule as pilot_gate.trigger_blocks; the gateway's agreement is pinned with the gateway
    (test_betfair_pilot_run)."""

    def state(self, offs):
        journal = Pilot(pt4=True)
        session = journal.arm(0)
        for off in offs:
            journal.place(session, off=off)
        return journal.fold()

    def test_horizon_wait_and_refuse(self):
        off = DAY0 + 10 * HOUR
        state = self.state([off])
        cutoff = off + C.bsp_cutoff_s * S + 1
        deadline = 5 * S
        cases = {cutoff - 1: "wait", cutoff - 2 * S: "wait", cutoff - 2 * S - 1: "refuse", cutoff - 60 * S: "refuse",
                 cutoff - 70 * S: "refuse", cutoff - 70 * S - 1: None, cutoff - 3600 * S: None}
        for t0, expected in cases.items():
            with self.subTest(before_trigger_ns=cutoff - t0):
                self.assertEqual(pilot_gate.trigger_blocks(state, t0_ns=t0, evidence_deadline_ns=deadline), expected)
        self.assertEqual(pilot_gate.trigger_blocks(state, t0_ns=cutoff - 1, evidence_deadline_ns=deadline,
                                                   waited=True), "refuse")

    def test_every_trigger_counts_with_look_ahead(self):
        off = DAY0 + 10 * HOUR
        cutoff = off + C.bsp_cutoff_s * S + 1
        state = self.state([off, off + 30 * S])                  # a second trigger 30 s after the first
        t0 = cutoff - 1                                         # the first trigger at t0 + 1 ns
        self.assertEqual(pilot_gate.trigger_blocks(state, t0_ns=t0, evidence_deadline_ns=5 * S), "refuse")
        self.assertEqual(pilot_gate.trigger_blocks(state, t0_ns=t0, evidence_deadline_ns=5 * S, look_ahead=False),
                         "wait")
        far = self.state([off, off + 600 * S])
        self.assertEqual(pilot_gate.trigger_blocks(far, t0_ns=t0, evidence_deadline_ns=5 * S), "wait")
        self.assertIsNone(pilot_gate.trigger_blocks(self.state([]), t0_ns=t0, evidence_deadline_ns=5 * S))
        self.assertEqual(DAY_NS, 86400 * S)


if __name__ == "__main__":
    unittest.main()
