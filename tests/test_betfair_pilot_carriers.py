"""P1-08's gateway changes (design v4 §3.1, §3.2, §4.13, §14; §9.1, §9.2, §9.20, §9.24), offline and synthetic.

These drive the real OrderGateway over the P1-06 harness (test_betfair_support_pilot) and show: every pre-reservation
cause through the one scope table (P108-R2-2: a clock regression beside the day cap keeps its review occurrence); the
sites that bypass latch_for agreeing with the table; durable carriers (a refusing risk decision, FAILED_NOT_SENT,
not_sent_kill_engaged, FAILED_REJECTED, a refused place report) whose stops the pure fold implies with the ids the
gateway journals, so a process death between the carrier and its latch leaves the stop due rather than lost; §14
R5-1's persistent wall step back; and the second-process settle guard under the lock. A death is simulated right
after one journal append (support_pilot.Death); a re-fold stands in for the next process. The executor's own reads of
these (ARM, pilot-end-session, ACK) are in test_betfair_pilot_run.
"""

import json
import os
import unittest
from unittest.mock import patch

from betangel_research.betfair import gateway as gateway_module
from betangel_research.betfair import pilot, pilot_gate
from betangel_research.betfair.errors import JournalError, OrderRefused
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.receipts import utc_from_ns

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
from test_betfair_pilot_gateway import PilotCase, aping_error, contract_bytes, failure_report, risk_failures

S = sp.S
OFF = sp.OFF


def open_hex(gateway):
    return gateway.open_id.split("-", 1)[1].lower()


class GatewayCase(PilotCase):
    def foreign(self, *, ahead_s=0):
        """Another process's journal connection whose clock is ahead_s seconds off ours (negative: behind)."""
        clock = support.FakeClock(utc_from_ns(self.env.clock.time_ns() + int(ahead_s * S)))
        return sp.other_writer(self.env, clock=clock)

    def foreign_submits(self, count, *, ago_s):
        """count place instructions another process submitted ago_s seconds ago (no order moved): rate windows."""
        self.foreign(ahead_s=-ago_s).append("submit", {"action": "place", "instruction_count": count,
                                                       "body_sha256": None, "decision_id": "dec-foreign",
                                                       "params": None, "transitions": []})

    def regression(self):
        """A foreign event stamped 10 s ahead: the core fold's clock regression (more than 5 s), after the open."""
        self.foreign(ahead_s=10).append("note", {"reason": "another_process_clock_ahead"})

    def review_stops(self, state=None):
        state = state or sp.fold(self.env)
        return [s for s in state.active_stops if s["scope"] == "review"]

    def next_day(self):
        """The next UTC day, armed again (the clock and the day recovered): what the next ARM's fold sees."""
        env = self.env
        env.gateway.close()
        sp.set_time(env.clock, OFF + 86400 * S - 299 * S - 3600 * S)
        env.session_id = sp.arm_session(env, until_ns=sp.UNTIL + 86400 * S)
        return sp.fold(env)

    def dies_after(self, market, kind, pilot_type=None):
        """Place, with the process dying right after the first append of kind; its lock is gone."""
        env = self.env
        with sp.die_after(env.journal, kind, pilot_type), self.assertRaises(sp.Death):
            self.place(market)
        env.gateway._release()
        return [e for e in sp.events(env) if e["kind"] == kind][-1]


class MixedCausesTests(GatewayCase):
    """§9.1 (P108-R2-2, mandatory): every (rule, reason) through scope_of before a reservation."""

    def test_a_clock_regression_beside_the_day_cap_keeps_its_review_occurrence(self):
        env = self.build()
        market = self.market()
        self.open()
        self.foreign_submits(25, ago_s=7200)
        self.regression()
        result = self.place(market)
        self.assertEqual(result["pilot"]["latch"], "review")
        self.assertEqual(risk_failures(result), [("halted", "clock_regression"), ("rate_day", "orders_per_day_cap")])
        hex_id = open_hex(env.gateway)
        self.assertEqual([(n["scope"], n["stop_id"]) for n in self.notes("stop")],
                         [("session", f"rate-day-{hex_id}-session")])
        self.assertEqual([n["code"] for n in self.notes("refusal")], ["clock_regression"])
        self.assertEqual((self.notes("reservation"), env.router.order_requests), ([], []))
        refusal_seq = sp.pilot_notes(env, "refusal")[-1]["seq"]
        # The next day, with the clock and the day recovered, the review occurrence still holds the ARM.
        state = self.next_day()
        self.assertEqual([s["stop_id"] for s in self.review_stops(state)],
                         [pilot.carried_review_stop_id("clock_regression", None, refusal_seq)])
        self.clean()

    def test_b_the_day_cap_alone_is_the_session_stop_only(self):
        env = self.build()
        market = self.market()
        self.open()
        self.foreign_submits(25, ago_s=7200)
        result = self.place(market)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("rate_day", "session"))
        self.assertEqual([(n["scope"], n["reason"]) for n in self.notes("stop")], [("session", "rate_day")])
        self.assertEqual(self.notes("refusal"), [])
        self.assertEqual(self.next_day().active_stops, ())               # the next ARM proceeds with no ACK
        self.clean()

    def test_c_a_market_only_failure_journals_nothing(self):
        env = self.build()
        market = self.market()
        self.open()
        before = self.kinds()
        self.foreign_submits(12, ago_s=1800)
        result = self.place(market)
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("rate_hour", "market"))
        self.assertEqual(risk_failures(result), [("rate_hour", "orders_per_hour_cap")])
        self.assertEqual(self.kinds(), before + ["submit"])               # only the foreign submit
        self.clean()

    def test_d_a_kill_beside_a_clock_regression(self):
        """The KILL appears after the early KILL check and before R1's own read: both stops, the stronger named."""
        env = self.build()
        market = self.market()
        self.open()
        self.regression()
        evaluate = env.guard.evaluate_pilot_reservation

        def kill_then_evaluate(*args, **kwargs):
            engage(env.paths, "test", clock=env.clock)
            return evaluate(*args, **kwargs)

        with patch.object(env.guard, "evaluate_pilot_reservation", side_effect=kill_then_evaluate):
            result = self.place(market)
        self.assertEqual(result["pilot"]["latch"], "review")
        self.assertIn(("halted", "kill_engaged"), risk_failures(result))
        self.assertEqual([n["stop_id"] for n in self.notes("stop")], [f"kill-file-{open_hex(env.gateway)}-session"])
        self.assertEqual([n["code"] for n in self.notes("refusal")], ["clock_regression"])
        self.clean()


class AgreementTests(GatewayCase):
    """§9.1: the sites that bypass latch_for agree with the scope table."""

    def test_the_early_kill_check(self):
        env = self.build()
        market = self.market()
        self.open()
        engage(env.paths, "test", clock=env.clock)
        result = self.place(market)
        scope = pilot.scope_of("kill_engaged")
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"], scope.durable),
                         ("kill_engaged", "session", "session"))
        self.assertEqual([n["stop_id"] for n in self.notes("stop")],
                         [pilot.session_stop_id(scope.code, open_hex(env.gateway))])
        self.clean()

    def test_a_manual_bet_and_a_journal_halt_are_none(self):
        env = self.build()
        self.open()
        with patch.object(pilot_gate, "open_manual_refs", return_value=["bar-1"]):
            result = self.place(self.market())
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]),
                         (pilot.MANUAL_BET_OPEN, pilot.scope_of(pilot.MANUAL_BET_OPEN).durable))
        self.assertEqual((self.notes("stop"), self.notes("refusal")), ([], []))
        env2 = self.build()
        self.open()
        self.foreign().append("halt", {"reason": "test", "scope": "global"})
        result = self.place(self.market())
        self.assertEqual((result["refusal"]["rule"], result["refusal"]["detail"], result["pilot"]["latch"]),
                         ("halted", {"reason": "journal_halted"}, "none"))
        self.assertEqual(pilot.scope_of("halted", "journal_halted").durable, "none")
        self.assertEqual((self.notes("stop"), self.notes("refusal")), ([], []))
        self.assertIsNotNone(env2)

    def test_first_byte_codes_and_a_kill_at_the_gate(self):
        env = self.build()
        market = self.market()
        self.open()
        env.transport.before_gate = lambda: self.foreign().append("note", {"reason": "another_process"})
        result = self.place(market)
        self.assertEqual((result["pilot"]["latch"], pilot.scope_of("pilot_gate_head_moved").durable),
                         ("market", "market"))
        self.assertEqual(sp.fold(env).due_stops, ())
        env = self.build()
        market = self.market()
        self.open()
        env.transport.before_gate = lambda: engage(env.paths, "test", clock=env.clock)
        result = self.place(market)
        self.assertEqual(result["pilot"]["latch"], "session")
        res = result["pilot"]["reservation_seq"]
        self.assertEqual([(n["scope"], n["stop_id"]) for n in self.notes("stop")],
                         [("session", pilot.session_stop_id("kill_file", res))])
        self.assertEqual(sp.fold(env).due_stops, ())                       # the fold implies the same session stop


class CarrierTests(GatewayCase):
    """§3.2 and §9.2 (P108-2): the carrier implies its stops; the gateway journals exactly those ids."""

    def stalled_capability(self):
        self.env.gateway.on_send = lambda: self.env.clock.advance(6)

    def test_capability_expired_report_then_death(self):
        env = self.build()
        market = self.market()
        self.open()
        self.stalled_capability()
        report = self.dies_after(market, "report")
        res = sp.pilot_notes(env, "reservation")[-1]["seq"]
        state = sp.fold(env)
        expected = pilot.carried_review_stop_id("capability_expired", res, report["seq"])
        self.assertEqual([(s["stop_id"], s["scope"], s["reason"]) for s in state.due_stops],
                         [(expected, "review", "capability_expired")])
        self.assertEqual(expected, f"capability-expired-{res}-{report['seq']}")
        self.assertEqual(state.sessions[-1]["ended_seq"], report["seq"])      # ended at the carrier itself
        self.assertEqual(self.notes("stop"), [])

    def test_with_the_latch_present_nothing_is_due_and_one_ack_suffices(self):
        env = self.build()
        market = self.market()
        self.open()
        self.stalled_capability()
        result = self.place(market)
        self.assertEqual(result["pilot"]["latch"], "review")
        state = sp.fold(env)
        self.assertEqual(state.due_stops, ())
        (stop,) = self.review_stops(state)
        state = self.next_day()
        self.assertEqual([s["stop_id"] for s in self.review_stops(state)], [stop["stop_id"]])
        sp.note(env, "ack", stop_id=stop["stop_id"], cause_written=True)
        self.assertEqual(sp.fold(env).active_stops, ())

    def test_a_refusing_risk_decision_with_mixed_rules(self):
        """Pure fold: a refusing risk_decision carrying a session rule and two review rules implies the session stop
        and one review stop keyed on the first review code; then a real V9 refusal dies after its decision."""
        env = self.build()
        market = self.market()
        self.open()
        funds = env.gateway._funds

        def removed_then_funds():
            market.statuses[sp.FAV] = "REMOVED"
            return funds()

        with patch.object(env.gateway, "_funds", side_effect=removed_then_funds):
            decision = self.dies_after(market, "risk_decision")
        res = sp.pilot_notes(env, "reservation")[-1]["seq"]
        failures = decision["payload"]["detail"]["failures"]
        self.assertEqual(failures[0]["rule"], "validation:V9")
        due = sp.fold(env).due_stops
        self.assertEqual([s["stop_id"] for s in due],
                         [pilot.carried_review_stop_id("validation:V9", res, decision["seq"])])
        # The same carrier with a session cause added: both, one review only.
        events = sp.events(env)
        mixed = json.loads(json.dumps(events))
        mixed[decision["seq"] - 1]["payload"]["detail"]["failures"] = [
            {"rule": "rate_day", "reason": "orders_per_day_cap", "instruction": None},
            {"rule": "validation:V0", "reason": "x", "instruction": None},
            {"rule": "funds", "reason": "y", "instruction": None}]
        state = pilot.fold(mixed, contract=env.contract, now_ns=env.clock.time_ns())
        self.assertEqual(sorted((s["scope"], s["stop_id"]) for s in state.due_stops),
                         [("review", pilot.carried_review_stop_id("validation:V0", res, decision["seq"])),
                          ("session", f"rate-day-{res}-session")])

    def test_not_sent_kill_engaged_then_death(self):
        env = self.build()
        market = self.market()
        self.open()
        append = env.journal.append

        def kill_after_the_submit(kind, payload, **columns):
            event = append(kind, payload, **columns)
            if kind == "submit":
                engage(env.paths, "test", clock=env.clock)
            return event

        with patch.object(env.journal, "append", side_effect=kill_after_the_submit):
            report = self.dies_after(market, "report")
        self.assertEqual(report["payload"]["outcome"], "not_sent_kill_engaged")
        res = sp.pilot_notes(env, "reservation")[-1]["seq"]
        self.assertEqual([(s["stop_id"], s["scope"]) for s in sp.fold(env).due_stops],
                         [(pilot.session_stop_id("kill_file", res), "session")])

    def test_first_byte_kill_before_send_and_connect_failed(self):
        env = self.build()
        market = self.market()
        self.open()
        env.transport.before_gate = lambda: env.clock.advance(3)          # send_window_expired: market only
        self.assertEqual(self.place(market)["pilot"]["latch"], "market")
        self.assertEqual((sp.fold(env).due_stops, self.review_stops()), ((), []))
        env = self.build()
        market = self.market()
        self.open()
        env.transport.before_gate = lambda: engage(env.paths, "test", clock=env.clock)
        result = self.place(market)
        res = result["pilot"]["reservation_seq"]
        self.assertEqual(result["send_exception"]["code"], "kill_engaged_before_send")
        self.assertEqual([n["stop_id"] for n in self.notes("stop")], [pilot.session_stop_id("kill_file", res)])
        self.assertEqual(sp.fold(env).due_stops, ())
        env = self.build()
        market = self.market()
        self.open()
        env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("connect", "connect_failed")]
        result = self.place(market)
        report = [e for e in sp.events(env) if e["kind"] == "report"][-1]
        self.assertEqual([n["stop_id"] for n in self.notes("stop")],
                         [pilot.carried_review_stop_id("connect_failed", result["pilot"]["reservation_seq"],
                                                       report["seq"])])
        self.assertEqual(sp.fold(env).due_stops, ())

    def test_send_gate_failed_then_death(self):
        env = self.build()
        market = self.market()
        self.open()
        with patch.object(pilot_gate, "first_byte_refusal", side_effect=RuntimeError("fault")):
            report = self.dies_after(market, "report")
        res = sp.pilot_notes(env, "reservation")[-1]["seq"]
        self.assertEqual(report["payload"]["exception"]["code"], "send_gate_failed")
        self.assertEqual([s["stop_id"] for s in sp.fold(env).due_stops],
                         [pilot.carried_review_stop_id("send_gate_failed", res, report["seq"])])

    def test_journal_error_on_the_first_report_append(self):
        """The fallback report (refused_moves) is the carrier, not the HALT appended after it."""
        for death in (False, True):
            with self.subTest(death_after_the_fallback=death):
                env = self.build()
                market = self.market()
                self.open()
                env.exchange.place_handler = failure_report("RUNNER_REMOVED")
                append, refused = env.journal.append, []

                def refuse_first_report(kind, payload, **columns):
                    if kind == "report" and payload.get("transitions") and not refused:
                        refused.append(True)
                        raise JournalError("synthetic_fold_refusal")
                    event = append(kind, payload, **columns)
                    if death and kind == "report":
                        raise sp.Death("report")
                    return event

                with patch.object(env.journal, "append", side_effect=refuse_first_report):
                    if death:
                        with self.assertRaises(sp.Death):
                            self.place(market)
                        env.gateway._release()
                    else:
                        result = self.place(market)
                report = [e for e in sp.events(env) if e["kind"] == "report"][-1]
                self.assertIn("refused_moves", report["payload"])
                res = sp.pilot_notes(env, "reservation")[-1]["seq"]
                expected = pilot.carried_review_stop_id("RUNNER_REMOVED", res, report["seq"])
                state = sp.fold(env)
                if death:
                    self.assertEqual([s["stop_id"] for s in state.due_stops], [expected])
                else:
                    self.assertEqual(result["pilot"]["latch"], "review")
                    self.assertEqual([n["stop_id"] for n in self.notes("stop")], [expected])
                    self.assertEqual(state.due_stops, ())
                    halts = [e for e in sp.events(env) if e["kind"] == "halt"]
                    self.assertGreater(halts[-1]["seq"], report["seq"])

    def test_failed_rejected_and_refused_reports_then_death(self):
        cases = {"INVALID_SESSION_INFORMATION": ("aping", "INVALID_SESSION_INFORMATION"),
                 "RUNNER_REMOVED": ("report", "RUNNER_REMOVED"), "MARKET_SUSPENDED": ("report", None),
                 "INSUFFICIENT_FUNDS": ("report", None), "ACCESS_DENIED": ("aping", None)}
        for code, (how, expected_code) in cases.items():
            with self.subTest(code=code):
                env = self.build()
                market = self.market()
                self.open()
                if how == "aping":
                    env.router.next_response[gw.PLACE_SUFFIX] = [aping_error(code)]
                else:
                    env.exchange.place_handler = failure_report(code)
                report = self.dies_after(market, "report")
                res = sp.pilot_notes(env, "reservation")[-1]["seq"]
                due = [s["stop_id"] for s in sp.fold(env).due_stops]
                self.assertEqual(due, [pilot.carried_review_stop_id(expected_code, res, report["seq"])]
                                 if expected_code else [])
                if code in ("INSUFFICIENT_FUNDS", "ACCESS_DENIED"):
                    self.assertTrue(report["payload"]["halts"])                  # the HALT already ends it

    def test_marker_pairs_then_death(self):
        cases = {("RUNNER_REMOVED", "BET_ACTION_ERROR"): "RUNNER_REMOVED",
                 ("MARKET_NOT_OPEN_FOR_BETTING", "BET_ACTION_ERROR"): None,
                 ("MARKET_SUSPENDED", "BET_ACTION_ERROR"): None,
                 (None, "BET_ACTION_ERROR"): "BET_ACTION_ERROR",
                 ("ERROR_IN_ORDER", "MARKET_SUSPENDED"): None}
        for (code, exec_code), expected in cases.items():
            with self.subTest(instruction=code, report=exec_code):
                env = self.build()
                market = self.market()
                self.open()
                env.exchange.place_handler = failure_report(code, exec_code)
                report = self.dies_after(market, "report")
                res = sp.pilot_notes(env, "reservation")[-1]["seq"]
                due = [s["stop_id"] for s in sp.fold(env).due_stops]
                self.assertEqual(due, [pilot.carried_review_stop_id(expected, res, report["seq"])] if expected else [])

    def test_a_kill_adds_its_stop_after_the_carried_review_stop(self):
        for cause in ("capability_expired", "connect_failed"):
            for death in (False, True):
                with self.subTest(cause=cause, death_after_the_report=death):
                    env = self.build()
                    market = self.market()
                    self.open()
                    if cause == "capability_expired":
                        def stall_then_kill():
                            env.clock.advance(6)
                            engage(env.paths, "test", clock=env.clock)
                        env.gateway.on_send = stall_then_kill
                    else:
                        def kill_then_connect_failure(record):
                            engage(env.paths, "test", clock=env.clock)
                            return support.transport_failure("connect", "connect_failed")
                        env.router.next_response[gw.PLACE_SUFFIX] = [kill_then_connect_failure]
                    if death:
                        report = self.dies_after(market, "report")
                        res = sp.pilot_notes(env, "reservation")[-1]["seq"]
                        self.assertEqual([s["stop_id"] for s in sp.fold(env).due_stops],
                                         [pilot.carried_review_stop_id(cause, res, report["seq"])])
                        continue
                    result = self.place(market)
                    report = [e for e in sp.events(env) if e["kind"] == "report"][-1]
                    res = result["pilot"]["reservation_seq"]
                    self.assertEqual(result["pilot"]["latch"], "review")
                    self.assertEqual([(n["scope"], n["stop_id"]) for n in self.notes("stop")],
                                     [("review", pilot.carried_review_stop_id(cause, res, report["seq"])),
                                      ("session", pilot.session_stop_id("kill_file", res))])
                    self.assertEqual(sp.fold(env).due_stops, ())


class ClockRegressionAtTheDecisionTests(GatewayCase):
    """§9.24 (a) (SX-9; scope-table F2): a wall step back past CLOCK_REGRESSION_NS after the PLACE record and before
    the risk decision: the decision carries halted/clock_regression; one review stop, keyed on the first review
    code; with a death after the risk_decision it is implied."""

    def step_back_before_the_funds(self):
        env = self.env
        funds = env.gateway._funds

        def stepped():
            env.clock.step_wall(-6)
            return funds()

        return patch.object(env.gateway, "_funds", side_effect=stepped)

    def test_the_decision_carries_it(self):
        for death in (False, True):
            with self.subTest(death_after_the_risk_decision=death):
                env = self.build()
                market = self.market()
                self.open()
                with self.step_back_before_the_funds():
                    if death:
                        decision = self.dies_after(market, "risk_decision")
                    else:
                        result = self.place(market)
                        decision = [e for e in sp.events(env) if e["kind"] == "risk_decision"][-1]
                failures = [(f["rule"], f["reason"]) for f in decision["payload"]["detail"]["failures"]]
                self.assertEqual(failures[0], ("halted", "clock_regression"))
                res = sp.pilot_notes(env, "reservation")[-1]["seq"]
                expected = pilot.carried_review_stop_id("clock_regression", res, decision["seq"])
                state = sp.fold(env)
                if death:
                    self.assertEqual([s["stop_id"] for s in state.due_stops], [expected])
                else:
                    self.assertEqual(result["pilot"]["latch"], "review")
                    self.assertEqual([s["stop_id"] for s in self.review_stops(state)], [expected])
                    self.assertEqual((self.notes("refusal"), state.due_stops), ([], ()))
                self.assertEqual(env.router.order_requests, [])


class R5WallStepBackTests(GatewayCase):
    """§14 R5-1: cap 2 a minute, orders at T - 62 s and T - 30 s, an open at T, then a wall step back of 3 s that
    stays: a review stop (V10, the stale wall clock), and the same ARM's reopen is refused."""

    def test_witness(self):
        env = self.build()
        start = env.clock.time_ns()                                      # OFF - 299 s
        t = start + 62 * S                                               # the third open, T
        markets = [self.market(off_ns=OFF), self.market(off_ns=OFF + 32 * S), self.market(off_ns=t + 295 * S)]
        gateways = [env.gateway, sp.new_gateway(env), sp.new_gateway(env)]
        for market, gateway in zip(markets[:2], gateways[:2]):           # orders at T - 62 s and T - 30 s
            if env.clock.time_ns() < market.off_ns - 299 * S:
                sp.set_time(env.clock, market.off_ns - 299 * S)
            self.open(gateway)
            self.assertEqual(self.place(market, gateway=gateway)["decision"], "accepted")
            gateway.close()
        sp.set_time(env.clock, t)
        self.open(gateways[2])
        env.clock.step_wall(-3)                                          # and it stays: the raw reading is T - 3 s
        result = self.place(markets[2], gateway=gateways[2])
        self.assertEqual((result["refusal"]["rule"], result["pilot"]["latch"]), ("validation:V10", "review"))
        self.assertEqual(risk_failures(result), [("validation:V10", "book_stale_wall_clock")])
        self.assertEqual([n["code"] for n in self.notes("refusal")], ["validation_v10"])
        self.assertEqual((len(env.router.order_requests), len(self.notes("reservation"))), (2, 2))
        gateways[2].close()
        later = self.market(off_ns=OFF + 600 * S)
        sp.set_time(env.clock, later.off_ns - 299 * S)
        reopened = sp.new_gateway(env)
        self.assertEqual(self.open(reopened)["failures"], ["pa3_session_ended"])
        self.clean()


class SettleGuardTests(GatewayCase):
    """§4.13 and §9.20 (gateway half): a second-process settle-market refuses pilot_session_armed under the lock while
    the fold shows the session armed; the fallback is the UNTIL window; pilot-run's own settle point is exempt."""

    def settle_open(self, **kwargs):
        settler = sp.new_gateway(self.env, pilot_context=False, **kwargs)
        self.addCleanup(settler.close)
        try:
            report = settler.open(command="settle-market", confirm_token=sp.token(self.env))
        finally:
            settler.close()
        return report

    def test_armed_refused_ended_allowed(self):
        env = self.build()
        with self.assertRaises(OrderRefused) as caught:
            self.settle_open()
        self.assertEqual(caught.exception.rule, "pilot_session_armed")
        self.assertTrue(self.settle_open(in_session_settle=True)["armed"])
        session = sp.pilot_notes(env, "session_start")[-1]["seq"]
        sp.note(env, "stop", stop_id=pilot.session_stop_id("session_end", session), scope="session",
                reason="session_end", level_p=None, source_seq=session)
        self.assertTrue(self.settle_open()["armed"])                      # ended by its stop, before UNTIL

    def test_fallback_to_the_until_window(self):
        env = self.build()
        session = sp.pilot_notes(env, "session_start")[-1]["seq"]
        sp.note(env, "stop", stop_id=pilot.session_stop_id("session_end", session), scope="session",
                reason="session_end", level_p=None, source_seq=session)
        for label, reader in (("unreadable", OSError("gone")), ("mismatched", contract_bytes(
                **{"placement_limits.lifetime_place_records": 494})), ("invalid", b"{not json")):
            with self.subTest(contract=label):
                def read(relative, reader=reader):
                    if isinstance(reader, BaseException):
                        raise reader
                    return reader
                with self.assertRaises(OrderRefused) as caught:
                    self.settle_open(contract_reader=read)
                self.assertEqual(caught.exception.rule, "pilot_session_armed")
        sp.set_time(env.clock, sp.UNTIL)
        self.assertTrue(self.settle_open(contract_reader=lambda relative: (_ for _ in ()).throw(OSError()))["armed"])

    def test_no_session_start_no_check(self):
        self.build(arm=False)
        calls = []

        def read(relative):
            calls.append(relative)
            return sp.CONTRACT_RAW

        self.assertTrue(self.settle_open(contract_reader=read)["armed"])
        self.assertEqual(calls, [])
        self.assertTrue(os.path.exists(self.env.paths.gateway_lock))


if __name__ == "__main__":
    unittest.main()
