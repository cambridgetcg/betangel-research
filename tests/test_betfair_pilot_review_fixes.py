"""Astra's P1-08 code review round 1 (pilot-1/astra-review-p1-08-code.md) and the send-window timing finding: one
witness per finding, each built on Astra's probe, offline and synthetic on the existing harnesses (fake clock and
sleep, fake Keychain, transport, git, catalogue and process table; a fake confirmer typing what each prompt asks).

P108-C1 signal deferral before the scope lookup; P108-C2 the review occurrence before any session stop when no
durable carrier precedes the reservation; P108-C3 the guard at every pilot read attempt and login; P108-C4 an
unreadable initial journal refuses a standalone settle; and the send window (the Codex bot's P1 on PR #34): no full
journal fold between the send book and the first byte, a slow path that still fails closed, and a concurrent
journal change that still refuses. They do not show Betfair's real behaviour, real signal timing or real disk speed.
"""

import json
import signal
import time
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission, cli, gateway as gateway_module, journal as journal_module
from betangel_research.betfair import pilot, pilot_run
from betangel_research.betfair.errors import JournalError, LoginBudgetExceeded
from betangel_research.betfair.gateway import OrderGateway
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.paths import file_lock

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
import test_betfair_support_run as sr
import test_betfair_pilot_carriers as tc
from test_betfair_pilot_run import RunCase, places

S = sr.S
OFF = sr.OFF


class GatewayFixCase(tc.GatewayCase):
    test_happy_path_order_of_events = None        # PilotCase's own test runs in its own module


def died(kinds=("stop", "refusal")):
    """OrderJournal.append dies (sp.Death) right after the first durable pilot note of one of kinds."""
    original = OrderJournal.append

    def append(journal, kind, payload, **columns):
        event = original(journal, kind, payload, **columns)
        inner = payload.get("pilot") if isinstance(payload, dict) else None
        if kind == "note" and isinstance(inner, dict) and inner.get("type") in kinds:
            raise sp.Death(inner["type"])
        return event

    return patch.object(OrderJournal, "append", append)


# --- P108-C1 ------------------------------------------------------------------------------------------------------


class SignalScopeTests(RunCase):
    def early_twice(self):
        """The open wakes before off - 300 s twice: pilot_place returns pilot_reserve_too_early both times."""
        original = pilot_run.Run.sleep_until
        return patch.object(pilot_run.Run, "sleep_until", lambda run, target, outside_open=True: True
                            if not outside_open else original(run, target, outside_open=outside_open))

    def test_a_sigint_after_the_review_scope_is_selected(self):
        """Astra's signal_scope_probe: SIGINT lands while pilot-run looks up the second too-early's scope (before its
        refusal note). The note is still journaled; the run exits 5 with no release, and the next-day ARM refuses
        pilot_review_stop_active until a caused ACK. At 3e73bbb the run exited 0, released, and the next ARM went on."""
        env = self.one_market()
        original_after, original_scope = pilot_run.Run.after_place, pilot.scope_of
        inside, fired = [], []

        def after_place(run, *a, **kw):
            inside.append(True)
            try:
                return original_after(run, *a, **kw)
            finally:
                inside.clear()

        def scope_of(*a, **kw):
            if inside and not fired:
                fired.append(True)
                sr.fire(signal.SIGINT)
            return original_scope(*a, **kw)

        with self.early_twice(), patch.object(pilot_run.Run, "after_place", after_place), \
                patch.object(pilot, "scope_of", scope_of):
            code, out, err = sr.arm_run(env)
        self.assertEqual(fired, [True])
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["pilot_reserve_too_early"])
        self.assertEqual(json.loads(out)["signal"], "SIGINT")
        self.assertEqual(places(env), [])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        sr.typed_release(env, 1)
        self.next_day_arm(env)

    def test_the_first_too_early_retry_stays_interruptible(self):
        """A SIGINT during the first too-early's retry wait still raises SignalStop: no second pilot_place, nothing
        journaled, an orderly end that releases."""
        env = self.one_market()
        original = pilot_run.Run.sleep_until
        calls, places_ = [], []
        original_place = OrderGateway.pilot_place

        def sleep_until(run, target, outside_open=True):
            if not outside_open:
                calls.append(True)
                if len(calls) == 1:
                    return True
                sr.fire(signal.SIGINT)
            return original(run, target, outside_open=outside_open)

        def pilot_place(gateway, *a, **kw):
            places_.append(True)
            return original_place(gateway, *a, **kw)

        with patch.object(pilot_run.Run, "sleep_until", sleep_until), \
                patch.object(OrderGateway, "pilot_place", pilot_place):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(places_), 1)
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.released(env)

    def test_a_sigint_while_an_unarmed_open_decides_its_note(self):
        """§4.5 step 3 has the same window: an open reports A17 with no HALT (the gateway leaves its note to
        pilot-run) and SIGINT lands while pilot-run checks whether the open is covered. The A17 note is journaled and
        the next-day ARM refuses until a caused ACK. At 3e73bbb the SignalStop skipped the note."""
        env = self.one_market()
        original_dynamic = gateway_module.dynamic_arming
        original_covered = pilot_run.Run.covered
        fired = []

        def dynamic(*a, **kw):
            report = original_dynamic(*a, **kw)
            if kw.get("command") == "pilot-place":
                report = dict(report, armed=False,
                              failures=list(report["failures"]) + ["unresolved_or_foreign_orders"])
            return report

        def covered(run, ctx):
            if not fired:
                fired.append(True)
                sr.fire(signal.SIGINT)
            return original_covered(run, ctx)

        with patch.object(gateway_module, "dynamic_arming", dynamic), patch.object(pilot_run.Run, "covered", covered):
            code, out, err = sr.arm_run(env)
        self.assertEqual(fired, [True])
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["unresolved_or_foreign_orders"])
        self.assertEqual(json.loads(out)["signal"], "SIGINT")
        sr.typed_release(env, 1)
        self.next_day_arm(env)


# --- P108-C2 ------------------------------------------------------------------------------------------------------


class ReviewBeforeKillTests(GatewayFixCase):
    def test_a_death_after_the_first_append_keeps_the_review_occurrence(self):
        """Astra's carriers-mixed-crash probe: a clock regression, and a KILL that lands after the early KILL check
        and before the reservation's risk prediction, so both are predicted. The process dies right after the first
        stop or refusal note: the review occurrence is that first note, so with the KILL removed the next day still
        has an active review stop. At 3e73bbb the first note was the KILL session stop and nothing else survived."""
        env = self.build()
        market = self.market()
        self.open()
        self.regression()
        evaluate = env.guard.evaluate_pilot_reservation
        predicted = []

        def kill_then_evaluate(*a, **kw):
            engage(env.paths, "synthetic probe", clock=env.clock)
            found = evaluate(*a, **kw)
            predicted.extend(found)
            return found

        with patch.object(env.guard, "evaluate_pilot_reservation", side_effect=kill_then_evaluate), died(), \
                self.assertRaises(sp.Death):
            self.place(market)
        env.gateway._release()
        rules = {f["rule"] for f in predicted}
        self.assertIn("halted", rules)
        (first,) = [n for n in self.notes() if n["type"] in ("stop", "refusal")]
        self.assertEqual(first["type"], "refusal")                           # the review occurrence came first
        env.paths.kill_file.unlink()
        state = self.next_day()
        self.assertTrue([s for s in state.active_stops if s["scope"] == "review"], state.active_stops)
        self.assertEqual(env.router.order_requests, [])

    def test_the_same_causes_without_a_death_journal_both(self):
        env = self.build()
        market = self.market()
        self.open()
        self.regression()
        evaluate = env.guard.evaluate_pilot_reservation

        def kill_then_evaluate(*a, **kw):
            engage(env.paths, "synthetic probe", clock=env.clock)
            return evaluate(*a, **kw)

        with patch.object(env.guard, "evaluate_pilot_reservation", side_effect=kill_then_evaluate):
            result = self.place(market)
        self.assertEqual(result["pilot"]["latch"], "review")
        kinds = [(n["type"], n.get("scope")) for n in self.notes() if n["type"] in ("stop", "refusal")]
        self.assertEqual(kinds, [("refusal", None), ("stop", "session")])


class StaticReviewBeforeKillTests(RunCase):
    def test_a_death_after_the_first_static_not_armed_append(self):
        """pilot-run's static NotArmed: the wall steps back 300 s and a KILL file appears as the open starts (inside
        the factory, before static arming), so A7 holds both causes. The process dies right after its first stop or
        refusal note: the review occurrence (not_armed_before_lock) is that note, so once the KILL is gone and Yu has
        released, the next-day ARM still refuses until a caused ACK. At 3e73bbb the kill_file stop came first."""
        env = self.one_market()
        original = cli._config
        armed = []
        original_loop = pilot_run.Run.loop

        def loop(run):
            armed.append(True)
            return original_loop(run)

        def config(args, paths):
            if armed and len(armed) == 1 and args.command == "pilot-run" and not paths.kill_file.exists():
                armed.append("stepped")
                env.clock.step_wall(-300)
                paths.kill_file.write_text("")
            return original(args, paths)

        with patch.object(cli, "_config", config), patch.object(pilot_run.Run, "loop", loop), \
                died(), self.assertRaises(sp.Death):
            sr.arm_run(env)
        self.assertIn("stepped", armed)
        notes = [n for n in sr.notes(env) if n["type"] in ("stop", "refusal")]
        self.assertEqual([(n["type"], n.get("code")) for n in notes], [("refusal", "not_armed_before_lock")])
        env.clock.step_wall(300)
        env.paths.kill_file.unlink()
        self.assertIn(sr.end_session(env)[0], (0, 5))
        sr.typed_release(env, 1)
        self.next_day_arm(env)


# --- P108-C3 ------------------------------------------------------------------------------------------------------


def verbs(requests):
    found = []
    for r in requests:
        if r["url"].endswith(("/api/login", "/api/certlogin", "/api/logout")):
            found.append(r["url"].split("/")[-1])
        else:
            found.append(json.loads(r["body"])["method"].split("/")[-1])
    return found


class ReadGuardTests(RunCase):
    def scheduler_case(self, *, kill, error):
        """Astra's ownership-read-login-probe: the scheduler's catalogue answer carries error, and a KILL (or SIGINT)
        lands while it is served. -> (exit, out, err, the requests after that answer)."""
        env = self.registered()
        seen = []

        def response(record):
            if kill:
                env.paths.kill_file.write_text("probe")
            else:
                sr.fire(signal.SIGINT)
            seen.append(len(env.transport.requests))
            return support.http_response({"jsonrpc": "2.0", "id": json.loads(record["body"])["id"],
                                          "error": {"code": -32099, "data": {"exceptionname": "APINGException",
                                                                             "APINGException": {"errorCode": error}}}})

        env.router.next_response["listMarketCatalogue"] = [response]
        code, out, err = sr.arm_run(env)
        return env, code, out, err, verbs(env.transport.requests[seen[0]:])

    def test_a_kill_with_a_session_error_neither_relogs_nor_retries(self):
        env, code, out, err, after = self.scheduler_case(kill=True, error="INVALID_SESSION_INFORMATION")
        self.assertEqual(set(after) - {"logout"}, set(), after)
        self.assertEqual(code, 5, err)
        self.assertEqual(json.loads(out)["abnormal"], "kill")
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual(len(sr.logins(env)), 1)

    def test_a_sigint_with_a_session_error_neither_relogs_nor_retries(self):
        env, code, out, err, after = self.scheduler_case(kill=False, error="INVALID_SESSION_INFORMATION")
        self.assertEqual(set(after) - {"logout"}, set(), after)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["signal"], "SIGINT")
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual(admission.ledger_problems(env.config, env.paths, clock=env.clock), [])
        self.released(env)

    def test_a_kill_with_a_rate_limit_does_not_retry(self):
        env, code, out, err, after = self.scheduler_case(kill=True, error="TOO_MANY_REQUESTS")
        self.assertEqual(set(after) - {"logout"}, set(), after)
        self.assertEqual(code, 5, err)

    def test_a_standalone_capture_with_a_kill_during_its_first_login(self):
        env = self.registered()
        market = sr.add_market(env)
        sr.closes_after_off(env, market)
        env.exchange.hide_cancelled_unmatched = True
        self.assertEqual(sr.arm_run(env)[0], 0)
        env.clock.advance(3600)
        original = env.transport.router
        seen = []

        def router(record):
            response = original(record)
            if record["url"].endswith(("/api/login", "/api/certlogin")) and not seen:
                env.paths.kill_file.write_text("probe")
                seen.append(len(env.transport.requests))
            return response

        env.transport.router = router
        code, out, err = sr.capture(env)
        self.assertEqual(set(verbs(env.transport.requests[seen[0]:])) - {"logout"}, set())
        result = json.loads(out)
        self.assertEqual((result["stopped"], result["written"]), ("skipped_kill", 0))
        self.assertEqual(code, 5, err)
        self.assertNotIn("recording released", err)

    def test_a_kill_during_the_settle_open_login_reads_nothing_more(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 3600 * S)
        sr.settling(env, self.market)
        original = env.transport.router
        settling, seen = [], []
        original_settle = pilot_run.Run.settle_one

        def settle_one(run, *a, **kw):
            settling.append(True)
            return original_settle(run, *a, **kw)

        def router(record):
            response = original(record)
            if settling and not seen and record["url"].endswith(("/api/login", "/api/certlogin")):
                env.paths.kill_file.write_text("probe")
                seen.append(len(env.transport.requests))
            return response

        env.transport.router = router
        with patch.object(pilot_run.Run, "settle_one", settle_one):
            code, out, err = sr.arm_run(env)
        self.assertTrue(seen)
        self.assertEqual(set(verbs(env.transport.requests[seen[0]:])) - {"logout"}, set())
        self.assertEqual([e for e in sr.events(env) if e["kind"] == "settle"], [])
        self.assertEqual(code, 5, err)
        self.assertEqual(json.loads(out)["abnormal"], "kill")
        self.assertEqual(sr.notes(env, "refusal"), [])


# --- P108-C4 ------------------------------------------------------------------------------------------------------


class SettleHeadTests(RunCase):
    def held_cleanup(self, *, unreadable):
        """Astra's ownership-settle-head-probe: the session ended, pilot-run still holds its owner lock with ADMISSION
        named for it (its cleanup), and a standalone settle-market runs; unreadable makes only its first journal read
        fail. -> (result, new logins)."""
        env = self.registered()
        market = sr.add_market(env)
        sr.settling(env, market)
        self.assertEqual(sr.arm_run(env)[0], 0)
        env.clock.advance(600)
        session_id = sr.notes(env, "session_start")[-1]["session_id"]
        admission.atomic_write_json(env.paths.admission_file, {
            "schema_version": 1, "generation": 2, "owner": "pilot-session:" + session_id, "purpose": "cleanup",
            "created_at": env.clock.utc_now(), "state": "admitted"})
        env.paths.inhibit_file.write_text("cleanup")
        original = pilot_run.pilot_events_after
        used = []

        def read(*a, **kw):
            if unreadable and not used:
                used.append(True)
                return {"readable": False, "events": [], "kill_seq": None, "kill_incomplete_seq": None,
                        "place_submit": None}
            return original(*a, **kw)

        before = len(sr.logins(env))
        with file_lock(env.paths.live_owner_lock, timeout_s=0, clock=env.clock), \
                patch.object(pilot_run, "pilot_events_after", side_effect=read):
            result = sr.run_cli(env, "settle-market", "--market", market.market_id, "--confirm-live", sr.token(env))
        return env, result, len(sr.logins(env)) - before

    def test_a_transient_unreadable_first_read_still_refuses(self):
        for unreadable in (False, True):
            with self.subTest(unreadable_first_read=unreadable):
                env, result, logins = self.held_cleanup(unreadable=unreadable)
                err = self.refused(result, "pilot_session_armed")
                self.assertIn(cli.PILOT_SETTLE_ADVICE, err)
                self.assertEqual(logins, 0)
                self.assertEqual([e for e in sr.events(env) if e["kind"] == "settle"], [])


# --- the send window (Codex P1 on PR #34; Astra's timing note) ------------------------------------------------------


class SendWindowTests(GatewayFixCase):
    def large(self, min_events):
        """494 PLACE records and at least min_events events, a session armed and the 495th market ready; the clock
        moves with real time once started (sp.RealTimeClock)."""
        with patch.object(support, "FakeClock", sp.RealTimeClock):
            env = self.build(start="2026-12-14T08:00:00.000000Z", arm=False)
        events, today = sp.build_history(env, place_records=494, skips_per_place=1, min_events=min_events)
        self.assertGreaterEqual(events, min_events)
        off = today + 19 * 3600 * S
        sp.set_time(env.clock, off - 299 * S - 60 * S)
        env.session_id = sp.arm_session(env, until_ns=today + 21 * 3600 * S)
        sp.set_time(env.clock, off - 299 * S)
        return env, self.market(off_ns=off)

    def window(self, env, market, *, inside=None, real=False):
        """Place market, recording the send book's fetch and the first byte (placeOrders reaching the router: after
        the gate and the transport's final send-by check) in real time, and every full journal fold (fold_events over
        the whole journal) between them. inside() runs once, right after the send book is fetched.
        -> (result, seconds or None when nothing was sent, full folds in the window)."""
        marks, folds = {}, []
        snapshot = env.read.market_snapshot

        def observed(*a, **kw):
            book = snapshot(*a, **kw)
            if "book" not in marks:
                marks["book"] = time.perf_counter()
                if inside is not None:
                    inside()
            return book

        sp.first_byte_marks(env, marks)
        real_fold = journal_module.fold_events

        def counted(*a, **kw):
            if "book" in marks and "first_byte" not in marks:
                folds.append(True)
            return real_fold(*a, **kw)

        if real:
            env.clock.start_real()
        with patch.object(env.read, "market_snapshot", side_effect=observed), \
                patch.object(journal_module, "fold_events", counted), \
                patch.object(gateway_module, "fold_events", counted):
            result = self.place(market)
        seconds = (marks["first_byte"] - marks["book"]) if "first_byte" in marks else None
        return result, seconds, len(folds)

    def test_large_journal_placements_send_inside_the_window(self):
        """On a clock that moves with real time (so the risk decision and the gate judge the real duration): the 495th
        placement sends with 20,000 and with 60,000 events in the journal, under max_book_age_s from book to first
        byte, and no full journal fold runs in that window (at 3e73bbb three ran: _live_arming's state, _place_core's
        state and the state before the send, each rebuilt from event 1)."""
        for min_events in (20_000, 60_000):
            with self.subTest(events=min_events):
                env, market = self.large(min_events)
                self.open()
                result, seconds, folds = self.window(env, market, real=True)
                self.assert_sent(result)
                self.assertEqual(folds, 0)
                self.assertLess(seconds, env.config["orders"]["caps"]["max_book_age_s"])
                self.clean()

    def test_a_slow_fold_still_fails_closed(self):
        """A journal fold made slow (2.5 s on the clock, which also moves with real time): the stale send book is
        refused, by the risk decision's V10 or else the first-byte gate, and no placeOrders byte reaches Betfair."""
        with patch.object(support, "FakeClock", sp.RealTimeClock):
            env = self.build()
        market = self.market()
        self.open()
        fold = OrderJournal.fold
        slow = []

        def slow_fold(journal, **kw):
            state = fold(journal, **kw)
            if not slow:
                slow.append(True)
                env.clock.advance(2.5)
            return state

        snapshot = env.read.market_snapshot
        armed = []

        def observed(*a, **kw):
            book = snapshot(*a, **kw)
            armed.append(True)
            return book

        with patch.object(env.read, "market_snapshot", side_effect=observed), \
                patch.object(OrderJournal, "fold", lambda journal, **kw: slow_fold(journal, **kw) if armed
                             else fold(journal, **kw)):
            env.clock.start_real()
            result = self.place(market)
        self.assertTrue(slow)
        self.assertEqual(env.router.order_requests, [])
        self.assertIn(result["pilot"]["state"], ("REFUSED", "FAILED"), result.get("refusal"))

    def test_a_concurrent_journal_change_in_the_window_still_refuses(self):
        for when in ("after the send book", "after the submit"):
            with self.subTest(change=when):
                env = self.build()
                market = self.market()
                self.open()
                writer = sp.other_writer(env)
                if when == "after the submit":
                    env.transport.before_gate = lambda: writer.append("note", {"reason": "another_process"})
                    result, seconds, _ = self.window(env, market)
                else:
                    result, seconds, _ = self.window(
                        env, market, inside=lambda: writer.append("note", {"reason": "another_process"}))
                self.assertIsNone(seconds)
                self.assertEqual(env.router.order_requests, [])
                if when == "after the send book":           # the next compare-and-append sees the foreign event
                    self.assertEqual(result["refusal"]["rule"], "journal_changed_since_decision")
                else:                                       # the gate's fresh head read sees it
                    self.assertEqual(result["pilot"]["state"], "FAILED")
                    self.assertIn("pilot_gate_head_moved", json.dumps(sp.events(env)[-6:]))
                self.clean()


class ScriptedClock(support.FakeClock):
    """A FakeClock whose next time_ns readings can be scripted (then it reads as usual)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.script = []

    def time_ns(self):
        return self.script.pop(0) if self.script else super().time_ns()


class IncrementalFoldTests(unittest.TestCase):
    """OrderJournal.fold catches up from the connection's committed-fold cache (fix A): it equals a fresh fold of
    every event, for both submitting_as_unknown values, through appends from several connections, refused and
    rolled-back appends, the before_ns refusal after the insert, and an unfoldable journal (where both raise)."""

    def assert_same(self, journal, now_ns=None):
        now = journal.clock.time_ns() if now_ns is None else now_ns
        for unknown in (True, False):
            fresh = journal_module.fold_events(list(journal.events()), mode="live", now_ns=now,
                                               submitting_as_unknown=unknown)
            self.assertEqual(journal.fold(now_ns=now, submitting_as_unknown=unknown), fresh)

    def test_every_append_of_a_pilot_run(self):
        """Every connection a pilot run opens (the run's, each gateway's, other writers'), after every append and
        after every refused one: the cached fold equals the fresh fold on that connection."""
        env = sr.build_run(self)
        self.assertEqual(sr.register(env)[0], 0)
        market = sr.add_market(env, off_ns=OFF)
        sr.add_market(env, off_ns=OFF + 3600 * S)
        sr.settling(env, market)
        original = OrderJournal.append
        checked = []

        def append(journal, kind, payload, **columns):
            try:
                return original(journal, kind, payload, **columns)
            finally:
                if journal.mode == "live":
                    self.assert_same(journal)
                    checked.append(kind)

        with patch.object(OrderJournal, "append", append):
            self.assertEqual(sr.arm_run(env)[0], 0)
        self.assertGreater(len(checked), 20)
        self.assertIn("settle", checked)
        self.assert_same(env.journal)                          # the harness's own, long-lived connection

    def test_other_connections_rollbacks_and_refusals(self):
        env = sr.build_run(self)
        self.assertEqual(sr.register(env)[0], 0)
        self.assertEqual(sr.arm_run(env)[0], 0)
        clock = ScriptedClock(env.clock.utc_now())
        mine = OrderJournal(env.paths.journal_path("live"), mode="live", clock=clock)
        other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        self.addCleanup(mine.close)
        self.addCleanup(other.close)
        self.assert_same(mine)                                     # built from event 1
        other.append("note", {"reason": "another_process"})
        self.assert_same(mine)                                     # caught up past another connection's append
        mine.append("note", {"reason": "mine"})
        other.append("note", {"reason": "another_process_again"})
        self.assert_same(mine)
        self.assert_same(other)
        # A refused append (the fold refuses the event: a report for an order the journal never had): no write.
        with self.assertRaises(JournalError):
            mine.append("report", {"action": "place", "outcome": "reported", "transitions": [
                {"customer_order_ref": "no-such-ref-1", "state": "EXECUTABLE"}]})
        self.assert_same(mine)
        # An expect_head refusal and a before_ns refusal after the insert (the clock crosses before COMMIT): rolled
        # back, and the cache still names the committed head.
        with self.assertRaises(JournalError):
            mine.append("note", {"reason": "stale"}, expect_head={"seq": 1, "last_sha256": "0" * 64})
        now = clock.time_ns()
        clock.script = [now, now + 10 * S]              # under the lock: before; just before COMMIT: after
        with self.assertRaises(JournalError) as caught:
            mine.append("note", {"reason": "late"}, before_ns=now + 5 * S)
        self.assertIn("journal_append_too_late", str(caught.exception))
        self.assertEqual([e for e in mine.events() if e["payload"].get("reason") == "late"], [])
        self.assert_same(mine)
        self.assert_same(mine, now_ns=now + 3 * 86400 * S)        # another day: the same state from the same folder

    def test_an_unfoldable_journal_raises_as_before(self):
        env = sr.build_run(self)
        self.assertEqual(sr.register(env)[0], 0)
        journal = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        self.addCleanup(journal.close)
        self.assert_same(journal)
        sp.raw_insert(env, "report", {"action": "place", "outcome": "reported", "transitions": [
            {"customer_order_ref": "no-such-ref-2", "state": "EXECUTABLE"}]}, recorded_at=env.clock.utc_now())
        now = env.clock.time_ns()
        with self.assertRaises(Exception) as fresh:
            journal_module.fold_events(list(journal.events()), mode="live", now_ns=now)
        with self.assertRaises(Exception) as cached:
            journal.fold(now_ns=now)
        self.assertEqual((type(cached.exception), str(cached.exception)),
                         (type(fresh.exception), str(fresh.exception)))


# --- SIG-1 (the refute workflow's superset of P108-C1) -------------------------------------------------------------


def race_reservation(env, market):
    """Another writer reserves market while the open waits for off - 300 s: pilot_place returns
    pilot_market_already_reserved with latch None (the scheduler's pre-check had passed)."""
    def race():
        other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        try:
            session_id = sr.fold(env).sessions[-1]["session_id"]
            pilot_run.append_pilot(other, pilot.pilot_note("reservation", env.contract.sha256, session_id=session_id,
                                                           open_id="other-open-1"), market_id=market.market_id)
        finally:
            other.close()

    sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 310 * S, race)


class OwedWriteTests(RunCase):
    def assert_review_kept(self, env, code, out, err, note_code):
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], [note_code])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        sr.typed_release(env, 1)
        self.next_day_arm(env)

    def test_site_1_a_sigint_inside_scope_of(self):
        """SIG-1 site 1 (pilot_market_already_reserved, the finding's witness): SIGINT inside scope_of."""
        env = self.one_market()
        race_reservation(env, self.market)
        original_after, original_scope = pilot_run.Run.after_place, pilot.scope_of
        inside, fired = [], []

        def after_place(run, *a, **kw):
            inside.append(True)
            return original_after(run, *a, **kw)

        def scope_of(*a, **kw):
            if inside and not fired:
                fired.append(True)
                sr.fire(signal.SIGINT)
            return original_scope(*a, **kw)

        with patch.object(pilot_run.Run, "after_place", after_place), patch.object(pilot, "scope_of", scope_of):
            code, out, err = sr.arm_run(env)
        self.assertEqual(fired, [True])
        self.assert_review_kept(env, code, out, err, "pilot_market_already_reserved")

    def test_site_1_a_sigint_before_deferral_is_finished_by_the_helper(self):
        """SIG-1 (d): SIGINT on the statement that starts the deferral (the first point a signal can run after
        pilot_place returns). It raises SignalStop; the orderly path's helper still journals the owed note."""
        import inspect
        import sys
        env = self.one_market()
        race_reservation(env, self.market)
        source, first = inspect.getsourcelines(pilot_run.Run.place_once)
        target = first + next(i for i, line in enumerate(source) if "self.deferring(True)" in line)
        fired = []

        def trace(frame, event, arg):
            if (event == "line" and frame.f_code is pilot_run.Run.place_once.__code__ and frame.f_lineno == target
                    and not fired):
                fired.append(True)
                sr.fire(signal.SIGINT)
            return trace

        sys.settrace(trace)
        try:
            code, out, err = sr.arm_run(env)
        finally:
            sys.settrace(None)
        self.assertEqual(fired, [True])
        self.assert_review_kept(env, code, out, err, "pilot_market_already_reserved")

    def test_site_2_a_sigterm_during_the_a17_halt_read(self):
        env = self.one_market()
        original_dynamic = gateway_module.dynamic_arming
        original_fold = pilot_run.fold_events
        inside, fired = [], []

        def dynamic(*a, **kw):
            report = original_dynamic(*a, **kw)
            if kw.get("command") == "pilot-place":
                report = dict(report, armed=False,
                              failures=list(report["failures"]) + ["unresolved_or_foreign_orders"])
            return report

        original_at_open = pilot_run.Run.not_armed_at_open

        def at_open(run, report, ctx):
            inside.append(True)
            try:
                return original_at_open(run, report, ctx)
            finally:
                inside.clear()

        def fold_events(*a, **kw):
            if inside and not fired:
                fired.append(True)
                sr.fire(signal.SIGTERM)
            return original_fold(*a, **kw)

        with patch.object(gateway_module, "dynamic_arming", dynamic), \
                patch.object(pilot_run.Run, "not_armed_at_open", at_open), \
                patch.object(pilot_run, "fold_events", fold_events):
            code, out, err = sr.arm_run(env)
        self.assertEqual(fired, [True])
        self.assert_review_kept(env, code, out, err, "unresolved_or_foreign_orders")

    def test_site_3_a_sigint_in_settle_confirm_after_admission_intact(self):
        """SIG-1 site 3: ADMISSION is lost after the SETTLE line, and SIGINT lands right after admission_intact
        returns its problems, before the admission_lost note: the note is still journaled and no settle happens."""
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 3600 * S)
        sr.settling(env, self.market)
        lost, fired = [], []
        original = pilot_run.admission_intact

        def intact(paths, ticket, *, clock):
            problems = original(paths, ticket, clock=clock)
            if lost and problems and not fired:
                fired.append(True)
                sr.fire(signal.SIGINT)
            return problems

        def lose():
            lost.append(True)
            env.paths.admission_file.unlink()

        confirmer = HookConfirmerShim({"SETTLE MARKET": lose})
        with patch.object(pilot_run, "admission_intact", intact):
            code, out, err = sr.arm_run(env, confirmer=confirmer)
        self.assertEqual(fired, [True])
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["admission_lost"])
        self.assertEqual([e for e in sr.events(env) if e["kind"] == "settle"], [])
        self.assertTrue(sr.active_review(env))

    def test_a_signal_during_the_settle_recheck_never_logs_in(self):
        """SIG-1 (c): a SIGINT deferred during the settle re-check raises SignalStop("outer_stop") inside confirm, so
        the settle open never logs in after Yu's signal."""
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 3600 * S)
        sr.settling(env, self.market)
        typed, fired = [], []
        original = pilot_run.Run.settle_orders_clear

        def clear(projection):
            if typed and not fired:
                fired.append(True)
                sr.fire(signal.SIGINT)
            return original(projection)

        confirmer = HookConfirmerShim({"SETTLE MARKET": lambda: (typed.append(True),
                                                                setattr(confirmer, "logins", len(sr.logins(env))))})
        with patch.object(pilot_run.Run, "settle_orders_clear", staticmethod(clear)):
            code, out, err = sr.arm_run(env, confirmer=confirmer)
        self.assertEqual(fired, [True])
        self.assertEqual(len(sr.logins(env)), confirmer.logins)
        self.assertEqual([e for e in sr.events(env) if e["kind"] == "settle"], [])
        self.assertEqual(json.loads(out)["signal"], "SIGINT")
        self.assertEqual(code, 0, err)


class HookConfirmerShim(gw.FakeConfirmer):
    def __init__(self, hooks):
        super().__init__()
        self.hooks = dict(hooks)

    def confirm_line(self, expected, *, banner):
        super().confirm_line(expected, banner=banner)
        for prefix in list(self.hooks):
            if expected.startswith(prefix):
                self.hooks.pop(prefix)()


# --- SIG-3, SIG-4, SIG-5 -----------------------------------------------------------------------------------------


class EndPathTests(RunCase):
    def test_sig3_an_unexpected_error_in_the_end_append_still_prints_the_end_block(self):
        import sqlite3
        for with_order in (False, True):
            with self.subTest(sent_order=with_order):
                env = self.one_market() if with_order else self.registered()
                original = pilot_run.Run.owe

                def owe(run, payload, **columns):
                    if (payload.get("pilot") or {}).get("reason") == "session_end":
                        raise sqlite3.OperationalError("disk I/O error")
                    return original(run, payload, **columns)

                with patch.object(pilot_run.Run, "owe", owe):
                    code, out, err = sr.arm_run(env)
                self.assertEqual(code, 5, err)
                self.assertEqual(json.loads(out)["status"], "pilot_session_ended")
                self.assertIn("run pilot-end-session, then Yu's typed recording-release (generation 1)", err)
                self.assertNotIn("Betfair input error", err)
                self.assertNotIn("Betfair stopped", err)
                self.assertTrue(sr.armed_file(env).exists())
                view = sr.admission_view(env)
                self.assertEqual((view["admission"]["generation"], view["inhibit"]), (1, True))

    def test_sig4_a_recorded_order_names_reconcile_and_reconcile_clears_it(self):
        env = self.one_market()
        original = OrderJournal.append

        def append(journal, kind, payload, **columns):
            event = original(journal, kind, payload, **columns)
            if kind == "intent" and payload.get("source") == "pilot":
                sr.fire(signal.SIGINT)                       # the inner guard: SignalStop before the decision
            return event

        with patch.object(OrderJournal, "append", append):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("release refused", err)
        self.assertIn("order_recorded", err)
        self.assertIn("then run reconcile --mode live; its open refuses the order recorded before its submit", err)
        result = sr.run_cli(env, "reconcile", "--mode", "live", "--confirm-live", sr.token(env))
        self.assertEqual(result[0], 0, result[2])
        sr.typed_release(env, 1)
        self.nothing_admitted(env)

    def test_sig5_a_signal_after_the_submit_is_not_an_executor_exception(self):
        env = self.one_market()
        original = OrderJournal.append

        def append(journal, kind, payload, **columns):
            event = original(journal, kind, payload, **columns)
            if kind == "submit" and payload.get("action") == "place":
                raise cli.SignalStop("SIGINT")
            return event

        with patch.object(OrderJournal, "append", append):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(json.loads(out)["abnormal"], "signal_after_submit")
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual([s["reason"] for s in sr.stops(env, "session")], ["unknown_unresolved"])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)


# --- OWN-2 to OWN-5 -----------------------------------------------------------------------------------------------


class OwnershipAdviceTests(RunCase):
    def after_a_session_with_c_pending(self):
        env = self.one_market()
        sr.closes_after_off(env, self.market)
        env.exchange.hide_cancelled_unmatched = True
        self.assertEqual(sr.arm_run(env)[0], 0)
        env.clock.advance(3600)
        return env

    def test_own2_a_capture_without_credentials_releases_or_names_n(self):
        for preset in (False, True):
            with self.subTest(inhibit_preset=preset):
                env = self.after_a_session_with_c_pending()
                if preset:
                    env.paths.inhibit_file.write_text("")
                env.runner.items = {key: value for key, value in env.runner.items.items()
                                    if key[0] not in sr.CREDENTIAL_SERVICES}       # the Keychain anchor stays
                logins = len(sr.logins(env))
                code, out, err = sr.capture(env)
                self.assertEqual(code, 5, err)
                self.assertEqual(len(sr.logins(env)), logins)
                self.assertIn("credentials unavailable", err)
                if preset:
                    self.assertIn("recording stays off until Yu's typed recording-release (generation 2)", err)
                    self.assertEqual(sr.admission_view(env)["admission"]["generation"], 2)
                else:
                    self.assertIn("recording released (generation 2)", err)
                    self.nothing_admitted(env)
                    ticket = admission.recorder_admission(env.paths, invocation_id="rec-3", clock=env.clock,
                                                          processes=sr.FakeProcesses())
                    ticket.__enter__()
                    ticket.__exit__(None, None, None)

    def test_own3_a_pending_generation_is_never_offered_a_continuation(self):
        env = self.after_a_session_with_c_pending()
        admission.atomic_write_json(env.paths.admission_generation_file, {
            "schema_version": 1, "generation": 2, "updated_at": env.clock.utc_now()})
        admission.atomic_write_json(env.paths.admission_file, {
            "schema_version": 1, "generation": 2, "owner": "pilot-capture:77", "purpose": "capture",
            "created_at": env.clock.utc_now(), "state": "pending"})
        env.paths.inhibit_file.write_text("")
        err = self.refused(sr.capture(env), "admission_held")
        self.assertIn("state pending", err)
        self.assertIn("recording-release (generation 2)", err)
        self.assertNotIn("--continue-admission", err)
        before = sr.recording_files(env)
        err = self.refused(sr.capture(env, "--continue-admission", "2"), "continuation_needs_admitted_generation")
        self.assertIn("recording-release (generation 2)", err)
        self.assertEqual(sr.recording_files(env), before)

    def test_own4_an_unexpected_arm_error_names_n_and_pilot_end_session(self):
        env = self.one_market()
        original = pilot_run.append_pilot

        def append_pilot(journal, payload, **kw):
            if (payload.get("pilot") or {}).get("type") == "session_start":
                raise pilot_run.JournalError("journal_event_unfoldable")
            return original(journal, payload, **kw)

        with patch.object(pilot_run, "append_pilot", append_pilot):
            code, out, err = sr.arm_run(env)
        self.assertNotEqual(code, 0)
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        self.assertTrue(sr.armed_file(env).exists())
        self.assertIn("generation 1", err)
        self.assertIn("pilot-end-session", err)
        code, out, err = sr.end_session(env)
        self.assertIn("recording-release (generation 1)", err)

    def test_own5_an_armed_file_that_cannot_be_deleted_blocks_release(self):
        import os
        for how in ("replaced", "symlink"):
            with self.subTest(file=how):
                env = self.registered()

                def loop(run, how=how):
                    path = pilot_run.armed_session_path(run.paths)
                    if how == "replaced":
                        path.write_bytes(path.read_bytes() + b" ")
                    else:
                        target = path.with_name("elsewhere.json")
                        os.replace(path, target)
                        os.symlink(target, path)
                    run.clock.advance(11 * 3600)                    # the loop reaches UNTIL

                with patch.object(pilot_run.Run, "loop", loop):
                    code, out, err = sr.arm_run(env)
                self.assertEqual(code, 5, err)
                view = sr.admission_view(env)
                self.assertEqual((view["admission"]["generation"], view["inhibit"]), (1, True))
                self.assertTrue(os.path.lexists(sr.armed_file(env)))
                self.assertIn("run pilot-end-session, then Yu's typed recording-release (generation 1)",
                              err.strip().splitlines()[-1])


# --- SCOPE-1 to SCOPE-4 -------------------------------------------------------------------------------------------


class ScopeGapTests(RunCase):
    def test_scope1_an_unreadable_a17_halt_read_fails_closed(self):
        env = self.one_market()
        original_dynamic = gateway_module.dynamic_arming

        def dynamic(*a, **kw):
            report = original_dynamic(*a, **kw)
            if kw.get("command") == "pilot-place":
                report = dict(report, armed=False,
                              failures=list(report["failures"]) + ["unresolved_or_foreign_orders"])
            return report

        original_at_open = pilot_run.Run.not_armed_at_open

        def at_open(run, report, ctx):
            with patch.object(pilot_run, "fold_events", side_effect=pilot_run.JournalError("unfoldable")):
                return original_at_open(run, report, ctx)

        with patch.object(gateway_module, "dynamic_arming", dynamic), \
                patch.object(pilot_run.Run, "not_armed_at_open", at_open):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["unresolved_or_foreign_orders"])
        self.assertEqual(json.loads(out)["abnormal"], "journal_unreadable")
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_scope3_a_mismatched_or_timed_out_settle_line_skips_the_offer(self):
        from betangel_research.betfair.errors import OrderRefused
        for rule in ("operator_echo_mismatch", "operator_echo_timeout"):
            with self.subTest(rule=rule):
                env = self.one_market()
                sr.add_market(env, off_ns=OFF + 4200 * S)
                sr.settling(env, self.market)

                class Once(gw.FakeConfirmer):
                    failed = []

                    def confirm_line(self, expected, *, banner, rule=rule):
                        if expected.startswith("SETTLE") and not self.failed:
                            self.failed.append(True)
                            self.prompts.append({"expected": expected, "banner": banner})
                            raise OrderRefused(rule)
                        return super().confirm_line(expected, banner=banner)

                confirmer = Once()
                code, out, err = sr.arm_run(env, confirmer=confirmer)
                self.assertEqual(code, 0, err)
                self.assertIn(f"settle {self.market.market_id}: {rule}", err)
                self.assertEqual(sr.notes(env, "refusal"), [])
                settles = [p for p in confirmer.prompts if p["expected"].startswith("SETTLE")]
                self.assertEqual(len(settles), 2)                    # offered again in a later gap
                self.assertEqual(len([e for e in sr.events(env) if e["kind"] == "settle"]), 1)

    def test_scope4_a_v10_carried_stop_through_the_real_ack(self):
        """R3-1's V10 pin end to end: a wall step back of 3 s just before the funds read (more than max_book_age_s,
        less than the clock-regression limit): the refusing risk decision carries validation:V10
        book_stale_wall_clock; with or without a death after it, one review stop validation-v10-<res>-<seq>, zero
        placeOrders, cleared by one real caused ACK the next day."""
        for death in (False, True):
            with self.subTest(death_after_the_risk_decision=death):
                env = self.one_market()
                original = OrderGateway._funds
                stepped = []

                def funds(gateway):
                    if not stepped:
                        stepped.append(True)
                        env.clock.step_wall(-3)
                    return original(gateway)

                with patch.object(OrderGateway, "_funds", funds):
                    if death:
                        with sr.sigkill_after("risk_decision"), self.assertRaises(sp.Death):
                            sr.arm_run(env)
                    else:
                        self.assertEqual(sr.arm_run(env)[0], 5)
                decision = [e for e in sr.events(env) if e["kind"] == "risk_decision"][-1]
                self.assertEqual([(f["rule"], f.get("reason")) for f in decision["payload"]["detail"]["failures"]],
                                 [("validation:V10", "book_stale_wall_clock")])
                res = [e["seq"] for e in sr.events(env) if (e["payload"].get("pilot") or {}).get("type")
                       == "reservation"][-1]
                expected = pilot.carried_review_stop_id("validation:V10", res, decision["seq"])
                self.assertEqual(places(env), [])
                if death:
                    self.assertEqual([s["stop_id"] for s in sr.fold(env).due_stops], [expected])
                    before = self.head(env)
                    sr.end_session(env)
                    self.assertIn(expected, [e["payload"]["pilot"].get("stop_id") for e in sr.events(env)
                                             if e["seq"] > before])
                else:
                    self.assertEqual(sr.notes(env, "refusal"), [])
                self.assertEqual(sr.active_review(env), [expected])
                env.clock.step_wall(3)
                sr.typed_release(env, 1)
                self.next_day_arm(env)


class ReportHaltsTests(GatewayFixCase):
    def test_scope2_a_skipped_report_move_halts_and_journals_no_carried_stop(self):
        """A second gateway's open reconcile moves our SUBMITTING order to UNKNOWN while the place request is in
        flight, so the report's own moves are skipped: the durable report carries report_moves_skipped, the fold
        implies nothing, and the gateway (judging the durable halts, SCOPE-2) journals no carried stop."""
        from test_betfair_pilot_gateway import aping_error, failure_report
        for kind in ("aping INVALID_SESSION_INFORMATION", "RUNNER_REMOVED"):
            for death in (False, True):
                with self.subTest(kind=kind, death_after_the_report=death):
                    env = self.build()
                    market = self.market()
                    self.open()

                    def stale():
                        sp.new_gateway(env)._stale_to_unknown()

                    if kind == "RUNNER_REMOVED":
                        handler = failure_report("RUNNER_REMOVED")

                        def place(params, handler=handler):
                            stale()
                            return handler(params)

                        env.exchange.place_handler = place
                    else:
                        error = aping_error("INVALID_SESSION_INFORMATION")

                        def respond(record, error=error):
                            stale()
                            return error(record)

                        env.router.next_response[gw.PLACE_SUFFIX] = [respond]
                    if death:
                        report = self.dies_after(market, "report")
                    else:
                        result = self.place(market)
                        self.assertEqual(result["pilot"]["latch"], "none")
                        report = [e for e in sp.events(env) if e["kind"] == "report"][-1]
                    self.assertIn("report_moves_skipped", report["payload"]["halts"])
                    self.assertEqual(self.notes("stop"), [])
                    self.assertEqual(sp.fold(env).due_stops, ())


# --- SCHED-1 to SCHED-5 -------------------------------------------------------------------------------------------


class SettleScheduleTests(RunCase):
    def two_markets(self, gap_s=4200):
        env = self.one_market()
        self.second = sr.add_market(env, off_ns=OFF + gap_s * S)
        sr.settling(env, self.market)
        return env

    def test_sched1_the_budget_pre_check_skips_the_offer(self):
        env = self.two_markets()
        original = pilot_run.cap_blocks
        window = {}

        def cap_blocks(config, paths, *, clock):
            closed = any((e["payload"].get("pilot") or {}).get("type") == "capture_book" for e in sr.events(env))
            if closed and "from" not in window:
                window["from"] = clock.time_ns()
            if "from" in window and clock.time_ns() < window["from"] + 900 * S:
                return True
            return original(config, paths, clock=clock)

        prompted = []
        confirmer = HookConfirmerShim({"SETTLE MARKET": lambda: prompted.append(env.clock.time_ns())})
        with patch.object(pilot_run, "cap_blocks", cap_blocks):
            code, out, err = sr.arm_run(env, confirmer=confirmer)
        self.assertEqual(code, 0, err)
        prompts = [p for p in confirmer.prompts if p["expected"].startswith("SETTLE")]
        self.assertEqual(len(prompts), 1)
        self.assertGreaterEqual(prompted[0], window["from"] + 900 * S)   # offered only once the window cleared
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual(len(places(env)), 2)
        self.assertEqual(len([e for e in sr.events(env) if e["kind"] == "settle"]), 1)
        self.released(env)

    def test_sched1_a_cap_after_the_typed_line_skips_with_nothing_journaled(self):
        env = self.two_markets()
        typed = []
        original = pilot_run.cap_blocks
        confirmer = HookConfirmerShim({"SETTLE MARKET": lambda: typed.append(True)})

        def cap_blocks(config, paths, *, clock):
            if typed and len(typed) == 1:
                typed.append("blocked")
                return True
            return original(config, paths, clock=clock)

        with patch.object(pilot_run, "cap_blocks", cap_blocks):
            code, out, err = sr.arm_run(env, confirmer=confirmer)
        self.assertEqual(code, 0, err)
        self.assertIn("settle_point_closed", err)
        self.assertEqual(sr.notes(env, "refusal"), [])

    def test_sched2_the_order_condition_is_rechecked_after_the_typed_line(self):
        from betangel_research.betfair.gateway import OrderGateway as Gateway
        for state in ("UNKNOWN", "EXECUTABLE"):
            with self.subTest(order=state):
                env = self.two_markets()
                pending = []
                original_fold = pilot_run.Run.fold
                confirmer = HookConfirmerShim({"SETTLE MARKET": lambda: pending.append(True)})

                def fold(run, state=state):
                    projection = original_fold(run)
                    if pending and projection is not None:
                        pending.clear()
                        import copy
                        projection = dict(projection, core=copy.deepcopy(projection["core"]))
                        ref = projection["state"].place_records[0].ref
                        projection["core"]["orders"][ref].update(state=state, size_remaining_p=200)
                    return projection

                calls = []
                original_settle = Gateway.settle_market

                def settle_market(gateway, *a, **kw):
                    calls.append(True)
                    return original_settle(gateway, *a, **kw)

                with patch.object(pilot_run.Run, "fold", fold), patch.object(Gateway, "settle_market", settle_market):
                    code, out, err = sr.arm_run(env, confirmer=confirmer)
                self.assertIn("settle_point_closed", err)
                first_skip = [e for e in sr.events(env) if e["kind"] == "settle"]
                self.assertLessEqual(len(first_skip), 1)              # the later gap may settle it for real
                self.assertEqual(len(places(env)), 2)                 # placing continued
                self.assertEqual(code, 0, err)

    def test_sched3_the_last_order_gets_its_c_read_at_an_orderly_end(self):
        env = self.one_market()
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        notes = sr.notes(env, "capture_c")
        self.assertEqual([n["row"]["status"] for n in notes], ["EXECUTION_COMPLETE"])
        self.assertEqual(json.loads(out)["pending_c_reads"], 0)

    def test_sched3_a_day_limit_session_reads_each_c(self):
        env = self.registered()
        for i in range(6):
            sr.add_market(env, off_ns=OFF + i * 900 * S)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["end_code"], "day_limit")
        self.assertEqual(len(sr.notes(env, "capture_c")), 5)

    def test_sched3_no_final_batch_after_a_flag_and_a_deferred_one_is_named(self):
        env = self.one_market()
        original = pilot_run.Run.final_capture
        with patch.object(pilot_run.Run, "final_capture", lambda run: (setattr(run.stop, "requested", True),
                                                                       original(run))):
            code, out, err = sr.arm_run(env)
        self.assertEqual(sr.notes(env, "capture_c"), [])
        env = self.one_market()
        with patch.object(pilot_run.Run, "final_capture", lambda run: (patch.object(
                pilot_run, "cap_blocks", return_value=True).start(), original(run), patch.stopall())):
            code, out, err = sr.arm_run(env)
        self.assertEqual(json.loads(out)["pending_c_reads"], 1)
        self.assertIn(pilot_run.PENDING_CAPTURE_LINE, err)

    def test_sched4_no_capture_read_within_the_bound_of_the_next_open(self):
        from betangel_research.betfair.api import ReadClient
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 3600 * S)
        original_gap = pilot_run.Run.gap
        squeezed, reads = [], []

        def gap(run, projection, open_at):
            if not squeezed and run.c_queue:
                squeezed.append(len(reads))
                return original_gap(run, projection, run.now() + 20 * S)     # as if the next open were 20 s away
            return original_gap(run, projection, open_at)

        original_raw = ReadClient.raw_current_order

        def raw(client, bet_id, **kw):
            reads.append(env.clock.time_ns())
            return original_raw(client, bet_id, **kw)

        with patch.object(pilot_run.Run, "gap", gap), patch.object(ReadClient, "raw_current_order", raw):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(squeezed, [0])
        self.assertGreaterEqual(len(reads), 1)                       # read in a later gap, not the squeezed one
        self.assertEqual(len(sr.notes(env, "capture_c")), 2)

    def test_sched4_a_batch_stops_before_the_next_read_past_its_bound(self):
        env = self.one_market()
        reads = []

        class Client:
            def raw_current_order(self, bet_id, **kw):
                reads.append(bet_id)
                env.clock.advance(60)
                return {"status": 200, "body": b"{}", "error": None}

        entries = [("c", {"bet_id": "1", "ref": "r1", "market_id": "1.1", "selection_id": 1}),
                   ("c", {"bet_id": "2", "ref": "r2", "market_id": "1.1", "selection_id": 1})]
        out = pilot_run.capture(SimpleEnv(env), entries, read_client=lambda: Client(), journal=env.journal,
                                contract=env.contract, before_read=lambda: None, budget_blocks=lambda: False,
                                deadline_ns=env.clock.time_ns() + 30 * S)
        self.assertEqual(reads, ["1"])
        self.assertEqual(out["stopped"], "capture_deferred_open")

    def test_sched5_capture_batches_reuse_the_scheduler_session(self):
        env = self.one_market()
        sr.closes_after_off(env, self.market)
        sr.add_market(env, off_ns=OFF + 3 * 3600 * S)
        seen = sr.spy_run(self)
        original_batch = pilot_run.Run.capture_batch
        logins_in_batches = []

        def batch(run, **kw):
            before = len(sr.logins(env))
            try:
                return original_batch(run, **kw)
            finally:
                logins_in_batches.append(len(sr.logins(env)) - before)

        with patch.object(pilot_run.Run, "capture_batch", batch):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertGreater(len(logins_in_batches), 1)
        self.assertEqual(sum(logins_in_batches), 0)
        self.assertLessEqual(len(seen["run"].read_sessions), 2)


class SimpleEnv:
    def __init__(self, env):
        self.paths, self.config, self.clock = env.paths, env.config, env.clock


# --- NP-1 ---------------------------------------------------------------------------------------------------------


class SettleAfterUntilTests(RunCase):
    def three_days_after_a_clean_end(self):
        env = self.one_market()
        sr.settling(env, self.market)
        self.assertEqual(sr.arm_run(env)[0], 0)
        env.clock.advance(3 * 86400)
        return env

    def settle(self, env):
        return sr.run_cli(env, "settle-market", "--market", self.market.market_id, "--confirm-live", sr.token(env))

    def rewrite_config(self, env, change):
        from betangel_research.betfair.config import load_config
        data = json.loads(env.paths.config_path.read_text())
        change(data)
        env.paths.config_path.write_text(json.dumps(data))
        env.config = load_config(env.paths.config_path, paths=env.paths)

    def test_np1_no_pilot_section_an_unreadable_contract_or_another_sha(self):
        cases = {
            "orders.pilot removed": lambda env: self.rewrite_config(env, lambda d: d["orders"].pop("pilot")),
            "contract unreadable": lambda env: setattr(env.state, "contract_raw", OSError("unreadable")),
            "config sha mismatch": lambda env: self.rewrite_config(
                env, lambda d: d["orders"]["pilot"].update(contract_sha256="0" * 64)),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                env = self.three_days_after_a_clean_end()
                change(env)
                code, out, err = self.settle(env)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["status"], "settled")


class SkipLatchTests(GatewayFixCase):
    """SCOPE-3: a SKIP's latch is derived in one place, the gateway, from the stops the classifier returned (the
    pause's review: test_betfair_pilot_gateway's pause SKIP test)."""

    def test_the_day_limit_cap_is_session_and_an_ordinary_skip_is_market(self):
        env = self.build()
        sp.preload_place_records(env, 5)                       # PT4: the day's five records are used
        market = self.market(off_ns=env.clock.time_ns() + 299 * S)
        self.open()
        result = self.place(market)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["code"], result["pilot"]["latch"]),
                         ("SKIP", "cap", "session"))
        env.gateway.close()
        env = self.build()
        market = self.market(off_ns=env.clock.time_ns() + 100 * S)              # inside off - 300 s: too late
        self.open()
        result = self.place(market)
        self.assertEqual((result["pilot"]["outcome"], result["pilot"]["latch"]), ("SKIP", "market"))
        self.assertNotIn(result["pilot"]["code"], ("pause", "cap"))


# --- Astra round 2: P108-R2-1 and P108-R2-2 -----------------------------------------------------------------------


def fire_on_line(function, text, *, when=lambda frame: True):
    """A trace that fires SIGINT once, as function reaches the first source line containing text (when(frame) True).
    -> (the trace function, the list it appends to when it fires)."""
    import inspect
    source, first = inspect.getsourcelines(function)
    target = first + next(i for i, line in enumerate(source) if text in line)
    fired = []

    def trace(frame, event, arg):
        if (event == "line" and frame.f_code is function.__code__ and frame.f_lineno == target and not fired
                and when(frame)):
            fired.append(target)
            sr.fire(signal.SIGINT)
        return trace

    return trace, fired


class ReturnBoundaryTests(RunCase):
    """P108-R2-1 (Astra's signal-return-boundary and unarmed-return-boundary witnesses): a signal on the first line
    after the gateway returned its result or its open report, before pilot-run saved either. The gateway keeps both
    (last_pilot_result, open_report) and pilot-run holds the gateway from before open(), so the owed note is written.
    At 0565a02 both exited 0, released and let the next-day ARM through with no ACK."""

    def traced(self, env, trace):
        import sys
        sys.settrace(trace)
        try:
            return sr.arm_run(env)
        finally:
            sys.settrace(None)

    def early_twice(self):
        original = pilot_run.Run.sleep_until
        return patch.object(pilot_run.Run, "sleep_until", lambda run, target, outside_open=True: True
                            if not outside_open else original(run, target, outside_open=outside_open))

    def test_a_signal_before_the_second_too_early_is_saved(self):
        env = self.one_market()
        trace, fired = fire_on_line(pilot_run.Run.place_once, 'ctx["result"] = result',
                                    when=lambda frame: frame.f_locals["ctx"]["retried"])
        with self.early_twice():
            code, out, err = self.traced(env, trace)
        self.assertTrue(fired)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["pilot_reserve_too_early"])
        self.assertEqual(json.loads(out)["signal"], "SIGINT")
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        sr.typed_release(env, 1)
        self.next_day_arm(env)

    def test_a_signal_before_the_first_too_early_is_saved_owes_nothing(self):
        """The first too-early is a retry, not an owed write: the same boundary there ends the run orderly, nothing
        journaled, and it releases (the retry stays interruptible)."""
        env = self.one_market()
        trace, fired = fire_on_line(pilot_run.Run.place_once, 'ctx["result"] = result',
                                    when=lambda frame: not frame.f_locals["ctx"]["retried"])
        with self.early_twice():
            code, out, err = self.traced(env, trace)
        self.assertTrue(fired)
        self.assertEqual(code, 0, err)
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.released(env)

    def test_a_signal_before_the_unarmed_report_is_saved(self):
        for line in ('report = gateway.open_report()', 'ctx["report"] = report'):
            with self.subTest(at=line):
                env = self.one_market()
                original_dynamic = gateway_module.dynamic_arming

                def dynamic(*a, **kw):
                    report = original_dynamic(*a, **kw)
                    if kw.get("command") == "pilot-place":
                        report = dict(report, armed=False,
                                      failures=list(report["failures"]) + ["unresolved_or_foreign_orders"])
                    return report

                trace, fired = fire_on_line(pilot_run.Run.in_open, line)
                with patch.object(gateway_module, "dynamic_arming", dynamic):
                    code, out, err = self.traced(env, trace)
                self.assertTrue(fired)
                self.assertEqual(code, 5, err)
                self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["unresolved_or_foreign_orders"])
                sr.typed_release(env, 1)
                self.next_day_arm(env)


class TransportRetryGuardTests(unittest.TestCase):
    """P108-R2-2 (Astra's guard-transport-retry-probe): the real HttpTransport over scripted in-memory sockets. The
    stop becomes true as the first connection answers EOF; the idempotent retry on a fresh connection asks the guard
    first and sends nothing. Without a guard (every non-pilot caller) the retry is unchanged."""

    def request(self, guarded):
        from betangel_research.betfair import api, transport
        from betangel_research.betfair.errors import ReadStopped
        from betangel_research.betfair.secret import Secret
        support.scripted_network_only(self)
        state = {"stopped": False, "guard_calls": 0}

        class StopThenDisconnect(support.ScriptedSocket):
            def recv_into(self, buffer):
                state["stopped"] = True
                return super().recv_into(buffer)

        first = StopThenDisconnect([b""])
        second = support.ScriptedSocket([support.http_bytes(200, b'{"jsonrpc":"2.0","result":[],"id":1}')])
        script = support.ConnectionScript(first, second)
        inner = transport.HttpTransport(allowed_hosts={"api.betfair.com"}, clock=support.FakeClock(),
                                        connection_factory=script, env={})
        self.addCleanup(inner.close)

        def guard():
            state["guard_calls"] += 1
            return "kill" if state["stopped"] else None

        target = api.GuardedTransport(inner, guard) if guarded else inner
        args = ("POST", "https://api.betfair.com/exchange/betting/json-rpc/v1",
                {"X-Application": Secret("synthetic-delayed-key-0001"),
                 "X-Authentication": Secret(support.PLANTED_SECRET), "Content-Type": "application/json"},
                b'{"jsonrpc":"2.0","method":"SportsAPING/v1.0/listMarketCatalogue","params":{},"id":1}')
        outcome = {}
        try:
            outcome["result"] = target.request(*args, deadline_s=10, idempotent=True)
        except ReadStopped as exc:
            outcome["stopped"] = exc.reason
        return outcome, state, script, first, second

    def test_the_retry_asks_the_guard_and_sends_nothing(self):
        outcome, state, script, first, second = self.request(guarded=True)
        self.assertEqual(outcome, {"stopped": "kill"})
        self.assertGreater(len(first.sent), 0)                    # the first attempt went out before the stop
        self.assertEqual(len(second.sent), 0)                     # nothing after the stop
        # The outer check, before each attempt, and once after the first attempt's connect (P1-08 residual 4); the
        # second attempt stops at its pre-attempt check, before any connect.
        self.assertEqual(state["guard_calls"], 4)

    def test_without_a_guard_the_retry_is_unchanged(self):
        outcome, state, script, first, second = self.request(guarded=False)
        self.assertEqual(outcome["result"]["status"], 200)
        self.assertEqual(len(script.calls), 2)
        self.assertGreater(len(second.sent), 0)
        self.assertEqual(state["guard_calls"], 0)


# --- the refute workflow on 0565a02 (fix-round-verify-wf_bcf9a98c) ------------------------------------------------


class ThrownBeforeSubmitTests(RunCase):
    """Owed write after an exception (P2): pilot_place raises before any submit (RuntimeError, LoginBudgetExceeded)
    and SIGINT lands at the §3.7 handler's entry, or at Run.deferring's entry, before step 0. The open's handler now
    keeps the exception and sets the deferral with plain stores first, so the note is written; at f110312 the
    SignalStop replaced the exception, the run released and the next-day ARM passed with no ACK."""

    def thrown(self, exc, *, at):
        env = self.one_market()
        state = {"fire": True}
        original_after, original_deferring = pilot_run.Run.after_exception, pilot_run.Run.deferring

        def pilot_place(gateway, *a, **kw):
            raise exc

        def after_exception(run, e, ctx, **kw):
            if at == "handler" and state["fire"]:
                state["fire"] = False
                sr.fire(signal.SIGINT)
            return original_after(run, e, ctx, **kw)

        def deferring(run, value):
            if at == "deferring" and state["fire"] and value:
                state["fire"] = False
                sr.fire(signal.SIGINT)
            return original_deferring(run, value)

        with patch.object(OrderGateway, "pilot_place", pilot_place), \
                patch.object(pilot_run.Run, "after_exception", after_exception), \
                patch.object(pilot_run.Run, "deferring", deferring):
            code, out, err = sr.arm_run(env)
        self.assertFalse(state["fire"])
        return env, code, out, err

    def test_each_exception_and_entry_point(self):
        for exc, note in ((RuntimeError("probe_unexpected"), "executor_exception"),
                          (LoginBudgetExceeded("login_budget"), "login_budget")):
            for at in ("handler", "deferring"):
                with self.subTest(exception=type(exc).__name__, signal_at=at):
                    env, code, out, err = self.thrown(exc, at=at)
                    self.assertEqual(code, 5, err)
                    self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], [note])
                    self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
                    sr.typed_release(env, 1)
                    self.next_day_arm(env)

    def test_the_safety_net_when_the_handler_itself_is_cut_short(self):
        """If SignalStop still replaced the exception before §3.7's handler began, finish_owed runs that handler on
        the kept exception: the same note, written once."""
        env = self.one_market()
        state = {"cut": True}
        original_after = pilot_run.Run.after_exception

        def pilot_place(gateway, *a, **kw):
            raise RuntimeError("probe_unexpected")

        def after_exception(run, e, ctx, **kw):
            if state["cut"]:
                state["cut"] = False
                raise cli.SignalStop("SIGINT")
            return original_after(run, e, ctx, **kw)

        with patch.object(OrderGateway, "pilot_place", pilot_place), \
                patch.object(pilot_run.Run, "after_exception", after_exception):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["executor_exception"])


class FiringReport(dict):
    """An open report whose .get(key) fires SIGINT once, after the value is fetched (the post-call checkpoint)."""

    fire_on = None

    def get(self, key, default=None):
        value = dict.get(self, key, default)
        if key == FiringReport.fire_on:
            FiringReport.fire_on = None
            sr.fire(signal.SIGINT)
        return value


class UnarmedReportPinTests(RunCase):
    """Pins (f110312 already closes it): SIGINT on in_open's report reads, between open_report() and ctx["report"]
    (after .get("open_id") and after .get("armed") return), for an A17 report and a first-failure report: the
    gateway-kept report reaches finish_owed and the owed note is journaled."""

    def test_both_handler_points(self):
        for key in ("open_id", "armed"):
            for failures, note in ((["unresolved_or_foreign_orders"], "unresolved_or_foreign_orders"),
                                   (["pilot_session_not_armed"], "pilot_session_not_armed")):
                with self.subTest(after=key, failures=failures):
                    env = self.one_market()
                    original = OrderGateway.open_report
                    served = []

                    def open_report(gateway, failures=failures, key=key):
                        report = FiringReport(original(gateway) or {})
                        report.update(armed=False, failures=list(failures))
                        if not served:
                            served.append(True)
                            FiringReport.fire_on = key
                        return report

                    with patch.object(OrderGateway, "open_report", open_report):
                        code, out, err = sr.arm_run(env)
                    self.assertIsNone(FiringReport.fire_on)
                    self.assertEqual(code, 5, err)
                    self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], [note])
                    self.assertTrue(sr.active_review(env))


class BeginStopsAt(object):
    """A connection stand-in: after the real statement `stop_after` runs once, raise `exc` (as a signal handler
    raising SignalStop right after that call returns would)."""

    def __init__(self, db, stop_after, exc):
        self._db, self._stop_after, self._exc = db, stop_after, exc

    def execute(self, sql, *a):
        cursor = self._db.execute(sql, *a)
        if sql == self._stop_after and self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc
        return cursor

    @property
    def in_transaction(self):
        return self._db.in_transaction

    def __getattr__(self, name):
        return getattr(self._db, name)


class JournalTransactionTests(unittest.TestCase):
    """The incremental fold's BEGIN and append's BEGIN IMMEDIATE (P3): an exception that lands right after either
    returns leaves no transaction open, so the connection's next fold sees another connection's HALT and appends
    still go through on both connections. At f110312 the BEGIN sat outside the protected block."""

    class Stop(BaseException):
        pass

    def journals(self):
        paths = support.temp_state_paths(self)
        clock = support.FakeClock()
        mine = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
        other = OrderJournal(paths.journal_path("live"), mode="live", clock=clock, busy_timeout_s=0.2)
        self.addCleanup(mine.close)
        self.addCleanup(other.close)
        for _ in range(3):
            mine.append("note", {"reason": "n"})
        mine.fold(now_ns=clock.time_ns())
        return mine, other, clock

    def interrupted(self, mine, statement, call):
        real = mine._db
        mine._db = BeginStopsAt(real, statement, self.Stop())
        try:
            with self.assertRaises(self.Stop):
                call()
        finally:
            mine._db = real
        return real

    def test_a_stop_right_after_the_fold_begins(self):
        mine, other, clock = self.journals()
        other.append("note", {"reason": "catch_up"})
        real = self.interrupted(mine, "BEGIN", lambda: mine.fold(now_ns=clock.time_ns()))
        self.assertFalse(real.in_transaction)
        other.append("halt", {"reason": "after_stop"})
        state = mine.fold(now_ns=clock.time_ns())
        self.assertTrue(state["halted"])
        self.assertEqual(state, journal_module.fold_events(list(other.events()), mode="live", now_ns=clock.time_ns()))
        mine.append("note", {"reason": "owed_write"})

    def test_a_stop_right_after_append_begins(self):
        mine, other, clock = self.journals()
        real = self.interrupted(mine, "BEGIN IMMEDIATE", lambda: mine.append("note", {"reason": "cut"}))
        self.assertFalse(real.in_transaction)
        other.append("note", {"reason": "other_writer"})            # no write lock left behind
        mine.append("note", {"reason": "owed_write"})
        self.assertEqual([e["payload"]["reason"] for e in other.events()][-2:], ["other_writer", "owed_write"])

    def test_a_leftover_transaction_is_ended_on_entry(self):
        mine, other, clock = self.journals()
        mine._db.execute("BEGIN")
        mine._db.execute("SELECT count(*) FROM events").fetchone()     # a read snapshot from before
        other.append("halt", {"reason": "while_left_open"})
        self.assertTrue(mine.fold(now_ns=clock.time_ns())["halted"])
        self.assertFalse(mine._db.in_transaction)
        mine._db.execute("BEGIN")
        mine.append("note", {"reason": "after_leftover"})


class NonPilotSettleGuardTests(unittest.TestCase):
    """Settle-guard non-pilot invariance (P3): with no orders.pilot and a journal that is not a database, settle-market
    meets the gateway's own journal error as at 2d3b092 (exit 4, DatabaseError), not pilot_session_armed. Astra's C4
    witness (the pilot owner lock held, an unreadable head) still refuses: SettleHeadTests."""

    def test_a_garbage_journal_without_orders_pilot(self):
        import io as io_module
        import contextlib
        env = gw.build_live_gateway(self)
        env.journal.close()
        self.assertNotIn("pilot", env.config["orders"])
        env.paths.journal_path("live").write_bytes(b"this is not a database" * 40)
        deps = {"keychain_runner": env.runner, "http_transport": env.transport, "clock": env.clock,
                "confirmer": env.confirmer}
        out, err = io_module.StringIO(), io_module.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["settle-market", "--market", "1.1", "--confirm-live", env.token], deps=deps)
        self.assertEqual(code, 4, err.getvalue())
        self.assertIn("Betfair stopped: DatabaseError", err.getvalue())
        self.assertNotIn("pilot_session_armed", err.getvalue())


class FlaggedRecordedLineTests(RunCase):
    """ARM's refuse() prints the reconcile line with the coexistence flag (P3): an intent another process journals
    while the credential loader runs leaves an order RECORDED, so step 6 refuses and release_own reports
    order_recorded."""

    def test_the_flag_reaches_the_line(self):
        env = self.one_market(overrides={"coexistence": {"bet_angel": "unverified"}})

        def recorded():
            other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
            try:
                other.append("intent", {"order": gw.instruction(selection_id=sp.FAV, price_h=300),
                                        "customer_strategy_ref": "bar-manual", "is_probe": False, "parent_ref": None,
                                        "provisional_child": False, "source": "cli_manual"},
                             intent_id="int-other-1", customer_order_ref="other-ref-1", customer_ref="cref-o1",
                             market_id=self.market.market_id, state_after="RECORDED")
            finally:
                other.close()

        env.runner.on_credential = recorded
        code, out, err = sr.arm_run(env, "2000", "--bet-angel-paused")
        self.assertEqual(code, 2, err)
        self.assertIn("then run reconcile --mode live --bet-angel-paused; its open refuses the order recorded", err)
        self.assertNotIn("then run reconcile --mode live;", err)        # the release lines carry the flag too
        self.assertIn("release refused", err)


if __name__ == "__main__":
    unittest.main()
