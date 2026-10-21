"""P1-08 §9 gap tests (after the stage-4 report): the cases test_betfair_pilot_run.py left open, offline and synthetic,
on the same harness (test_betfair_support_run: temporary state root, fake clock and sleep, fake Keychain, transport,
git, catalogue and process table, and a fake confirmer that types what each prompt asks; no pty, no network).

Most run pilot-run end to end through cli.main. A few drive one pilot-run method on a stand-in run object, because
the gateway state they need (an A17 report with no HALT; a static NotArmed with a chosen cause list) cannot be
reached through the real gateway without inventing journal rows; those say so in their docstrings. They show what
pilot-run journals, prints, admits and releases; they do not show Betfair's real behaviour or a real terminal.
"""

import json
import os
import signal
import unittest
from unittest.mock import patch

from betangel_research.betfair import auth, pilot, pilot_gate, pilot_run
from betangel_research.betfair.credentials import ANCHOR_SERVICE
from betangel_research.betfair.errors import JournalError, LoginBudgetExceeded, NotArmed, TransportError
from betangel_research.betfair.gateway import OrderGateway
from betangel_research.betfair.journal import OrderJournal

import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
import test_betfair_support_run as sr
from test_betfair_pilot_gateway import contract_bytes
from test_betfair_pilot_run import RunCase, cancels, kill_event, places

S = sr.S
OFF = sr.OFF


def failed_logins(env):
    """Ledger login outcomes that are not a success (the failed and unknown attempts this run left)."""
    rows = []
    if env.paths.login_ledger.exists():
        for line in env.paths.login_ledger.read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "login_outcome" and row.get("outcome") not in ("proceed", "reads_only"):
                rows.append(row["outcome"])
    return rows


def unresolved_attempts(env):
    attempts, outcomes = set(), set()
    if env.paths.login_ledger.exists():
        for line in env.paths.login_ledger.read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "login_attempt":
                attempts.add(row["attempt_id"])
            elif row.get("kind") == "login_outcome":
                outcomes.add(row["attempt_id"])
    return attempts - outcomes


# --- §9.6 signals ---------------------------------------------------------------------------------------------


class SignalGapTests(RunCase):
    def test_a_second_sigint_while_the_handler_reads_the_journal(self):
        """A deferred SIGINT during the send, an exception after the submit (UNKNOWN), and a second SIGINT while the
        §3.7 handler reads the journal: the handoff prints before any write and before close(), unknown_unresolved
        is journaled, the end is abnormal, and the flag comes from the pending signals."""
        env = self.one_market()
        order = []

        def boom(record):
            raise RuntimeError("synthetic transport fault after the request")

        env.router.next_response[gw.PLACE_SUFFIX] = [boom]
        env.transport.before_gate = lambda: sr.fire(signal.SIGINT)                    # deferred: the send started
        original_read, original_after = pilot_run.Run.read, pilot_run.Run.after_exception
        original_line, original_close = pilot_run.Terminal.line, OrderGateway.close
        handling = []

        def read(run, since=0):
            if handling and not handling[0]:
                handling[0] = True
                sr.fire(signal.SIGINT)                                                 # the second SIGINT
            return original_read(run, since)

        def after(run, exc, ctx, *, in_cancel=False):
            handling.append(False)
            return original_after(run, exc, ctx, in_cancel=in_cancel)

        def line(terminal, text):
            order.append(("line", text, sr.events(env)[-1]["seq"]))
            return original_line(terminal, text)

        def close(gateway):
            order.append(("close", None, sr.events(env)[-1]["seq"]))
            return original_close(gateway)

        with patch.object(pilot_run.Run, "read", read), patch.object(pilot_run.Run, "after_exception", after), \
                patch.object(pilot_run.Terminal, "line", line), patch.object(OrderGateway, "close", close):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(handling, [True])
        handoff = next(i for i, item in enumerate(order) if item[0] == "line" and "own-scope kill" in item[1])
        first_close = next(i for i, item in enumerate(order) if item[0] == "close")
        self.assertLess(handoff, first_close)                                          # before close()
        stop_seqs = [e["seq"] for e in sr.events(env) if (e["payload"].get("pilot") or {}).get("type") in
                     ("stop", "refusal")]
        self.assertLess(order[handoff][2], min(stop_seqs))                             # before any write
        self.assertIn("unknown_unresolved", [s["reason"] for s in sr.stops(env, "session")])
        summary = json.loads(out)
        self.assertEqual((summary["stop_requested"], summary["signal"]), (True, "SIGINT"))
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_a_sigint_at_the_evidence_not_stored_review_append(self):
        """pilot_evidence_not_stored (latch None): SIGINT lands while pilot-run appends its review note: the note is
        journaled, no further pilot_place, no release, and the next-day ARM refuses until a caused ACK."""
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        calls = []
        original_place, original_append = OrderGateway.pilot_place, OrderJournal.append

        def pilot_place(gateway, *a, **kw):
            calls.append(True)
            return original_place(gateway, *a, **kw)

        def append(journal, kind, payload, **columns):
            inner = payload.get("pilot") if isinstance(payload, dict) else None
            if isinstance(inner, dict) and inner.get("code") == "pilot_evidence_not_stored":
                sr.fire(signal.SIGINT)
            return original_append(journal, kind, payload, **columns)

        with patch.object(pilot_gate, "store_evidence", side_effect=OSError("disk full")), \
                patch.object(OrderGateway, "pilot_place", pilot_place), patch.object(OrderJournal, "append", append):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["pilot_evidence_not_stored"])
        self.assertEqual((len(calls), places(env)), (1, []))
        self.assertEqual(json.loads(out)["signal"], "SIGINT")
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        sr.typed_release(env, 1)
        self.next_day_arm(env)

    def test_the_handler_mid_write_reentrant_then_eio(self):
        """The handler runs while ctx.err is mid-write (the write in progress would raise a reentrant RuntimeError if
        the handler wrote too), and then fd 2 raises EIO: the flag is set, nothing escapes the handler, the admit
        drain completes and §4.3 step 4 refuses pilot_arm_stopped; no executor_exception or login_failed note, no
        session_start, no open, and release_own releases generation 1."""
        env = self.one_market()
        signalled = sr.hold_recorder(env)
        stop_recorder = env.processes.on_terminate

        class MidWrite(sr.BrokenStream):
            writing = False
            reentrant = []

            def write(self, text):
                if self.writing:
                    self.reentrant.append(text)
                    raise RuntimeError("reentrant call inside <_io.BufferedWriter name='<stderr>'>")
                self.writing = True
                try:
                    if text.startswith("drain"):
                        sr.fire(signal.SIGINT)                      # the signal lands mid-write
                        self.broken = OSError(5, "EIO")             # and fd 2 is gone from here on
                    return super().write(text)
                finally:
                    self.writing = False

        err = MidWrite()

        def drained(pid):
            try:
                err.write("drain: SIGTERM sent\n")                  # a write in progress when the signal lands
            except OSError:
                pass
            stop_recorder(pid)

        env.processes.on_terminate = drained
        code, out, _ = sr.arm_run(env, err=err)
        self.assertEqual(code, 2)
        self.assertEqual(MidWrite.reentrant, [])
        self.assertEqual(signalled, [4242])                         # the drain ran to the end
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual(sr.stops(env), [])
        self.assertEqual(places(env), [])
        self.released(env)


# --- §9.7 a JournalError on the place report append, end to end ---------------------------------------------------


class ReportAppendTests(RunCase):
    def refuse_first_report(self):
        original = OrderJournal.append
        failed = []

        def append(journal, kind, payload, **columns):
            if kind == "report" and payload.get("action") == "place" and not failed:
                failed.append(True)
                raise JournalError("journal_write_failed")
            return original(journal, kind, payload, **columns)

        return failed, patch.object(OrderJournal, "append", append)

    def test_a_sent_order_whose_report_append_fails(self):
        """The order went out; its report's moves are refused (the gateway's fallback report, then its HALT): the
        order folds UNKNOWN, so pilot-run prints the handoff, journals unknown_unresolved, sends no cancel and does
        not release."""
        env = self.one_market()
        failed, patcher = self.refuse_first_report()
        with patcher:
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertTrue(failed)
        reports = [e for e in sr.events(env) if e["kind"] == "report" and e["payload"].get("action") == "place"]
        self.assertIn("refused_moves", reports[0]["payload"])
        self.assertEqual([e["payload"]["reason"] for e in sr.events(env) if e["kind"] == "halt"],
                         ["journal_refused_report_moves"])
        self.assertEqual((len(places(env)), cancels(env)), (1, []))
        self.assertIn("own-scope kill", err.splitlines()[2])                # the handoff, first after the banners
        self.assertEqual([s["reason"] for s in sr.stops(env, "session")], ["unknown_unresolved"])
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_a_refused_instruction_whose_report_append_fails(self):
        """RUNNER_REMOVED, its first report append refused: the gateway journals the carried review stop keyed on
        the fallback report's seq (not the HALT's); nothing is due; pilot-run adds nothing and does not release."""
        from test_betfair_pilot_gateway import failure_report
        env = self.one_market()
        env.exchange.place_handler = failure_report("RUNNER_REMOVED")
        failed, patcher = self.refuse_first_report()
        with patcher:
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        report = [e for e in sr.events(env) if e["kind"] == "report" and e["payload"].get("action") == "place"][0]
        res = [e["seq"] for e in sr.events(env) if (e["payload"].get("pilot") or {}).get("type") == "reservation"][0]
        self.assertEqual([s["stop_id"] for s in sr.stops(env, "review")],
                         [pilot.carried_review_stop_id("RUNNER_REMOVED", res, report["seq"])])
        halt = [e for e in sr.events(env) if e["kind"] == "halt"][-1]
        self.assertGreater(halt["seq"], report["seq"])
        self.assertEqual(sr.fold(env).due_stops, ())
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)


# --- §9.8 logins -------------------------------------------------------------------------------------------------


class LoginGapTests(RunCase):
    def one_failure(self, env, code, out, err):
        self.assertEqual(code, 5, err)
        self.assertEqual([s["reason"] for s in sr.stops(env, "session")], ["login_failed"])
        (note,) = [e for e in sr.events(env) if (e["payload"].get("pilot") or {}).get("reason") == "login_failed"]
        self.assertEqual(pilot.validate_pilot_payload(note["payload"]), "stop")         # the stop validates
        self.assertEqual(len(failed_logins(env)) + len(unresolved_attempts(env)), 1)
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        self.assertIn(pilot_run.LOGIN_RESET_LINE, err)

    def test_a_login_ban(self):
        env = self.one_market()
        env.router.login_status = "TEMPORARY_BAN_TOO_MANY_REQUESTS"
        code, out, err = sr.arm_run(env)
        self.one_failure(env, code, out, err)
        self.assertEqual(failed_logins(env), ["login_ban"])
        self.assertEqual(len(sr.logins(env)), 1)
        self.assertEqual(places(env), [])

    def test_a_login_whose_answer_may_have_arrived(self):
        env = self.one_market()
        original = env.transport.request
        failed = []

        def request(method, url, headers, body, **kw):
            if (url.endswith("/api/certlogin") or url.endswith("/api/login")) and not failed:
                failed.append(True)
                env.transport.requests.append({"url": url})
                raise TransportError("connection_lost", phase="receive", may_have_arrived=True)
            return original(method, url, headers, body, **kw)

        env.transport.request = request
        code, out, err = sr.arm_run(env)
        self.one_failure(env, code, out, err)
        self.assertEqual(len(sr.logins(env)), 1)
        self.assertEqual(places(env), [])

    def failing_inside(self, env, method, *, expire=False):
        """Logins fail from the start of Run.<method> on (expire: the scheduler's read session, which capture reuses,
        has lost its token, so the next read logs in); the transport requests after the failure are recorded."""
        seen = {}
        original = getattr(pilot_run.Run, method)

        def wrapped(run, *a, **kw):
            if "requests" not in seen:
                env.router.login_status = "FAIL"
                seen["requests"] = len(env.transport.requests)
                if expire and run.scheduler is not None:
                    run.scheduler[0]._token = None
            return original(run, *a, **kw)

        return seen, patch.object(pilot_run.Run, method, wrapped)

    def after_failure(self, env, seen):
        """Requests after the failed login: only the end path's logout of sessions already logged in."""
        later = env.transport.requests[seen["requests"]:]
        logins = [r for r in later if r["url"].endswith("/api/certlogin") or r["url"].endswith("/api/login")]
        self.assertEqual(len(logins), 1)                              # the failed one, never a retry
        rest = [r for r in later if r not in logins and not r["url"].endswith("/api/logout")]
        self.assertEqual(rest, [])

    def test_a_capture_session_login_failure(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        seen, patcher = self.failing_inside(env, "capture_batch", expire=True)
        with patcher:
            code, out, err = sr.arm_run(env)
        self.one_failure(env, code, out, err)
        self.after_failure(env, seen)
        self.assertEqual(len(places(env)), 1)
        self.assertEqual(sr.notes(env, "capture_c"), [])

    def test_a_settle_open_login_failure(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 3600 * S)
        sr.settling(env, self.market)
        seen, patcher = self.failing_inside(env, "settle_one")
        with patcher:
            code, out, err = sr.arm_run(env)
        self.one_failure(env, code, out, err)
        self.after_failure(env, seen)
        self.assertEqual([e for e in sr.events(env) if e["kind"] == "settle"], [])
        self.assertEqual(len(places(env)), 1)


class CaptureBudgetTests(RunCase):
    def after_a_session_with_c_pending(self):
        env = self.one_market()
        sr.closes_after_off(env, self.market)
        env.exchange.hide_cancelled_unmatched = True                  # C stays pending into pilot-capture
        self.assertEqual(sr.arm_run(env)[0], 0)
        env.clock.advance(3600)
        return env

    def test_a_deferred_read_stays_queued_and_does_not_block_release(self):
        env = self.after_a_session_with_c_pending()
        before = self.head(env)
        with patch.object(pilot_run, "cap_blocks", return_value=True):
            code, out, err = sr.capture(env)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["stopped"], result["written"]), ("login_budget_deferred", 0))
        self.assertEqual(self.head(env), before)                      # nothing journaled
        projection = pilot_run.recovery_projection(sr.events(env), contract=env.contract, now_ns=env.clock.time_ns())
        self.assertEqual(len(projection["pending_capture"]), 1)       # the C entry stays queued
        self.assertIn("recording released (generation 2)", err)
        self.nothing_admitted(env)

    def test_a_thrown_login_budget(self):
        env = self.after_a_session_with_c_pending()
        logins = len(sr.logins(env))
        with patch.object(auth.SessionManager, "login", side_effect=LoginBudgetExceeded("login_budget")):
            code, out, err = sr.capture(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["login_budget"])
        self.assertEqual(len(sr.logins(env)), logins)
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 2)          # no release
        self.assertIn("recording-release (generation 2)", err)
        sr.typed_release(env, 2)
        self.next_day_arm(env)


# --- §9.12 a manual place and an ARM are serialized ---------------------------------------------------------------


class SerializationTests(RunCase):
    def test_a_manual_place_blocked_in_its_funds_read_cannot_send_across_an_arm(self):
        """An ARM typed while a manual place holds .gateway.lock (blocked in its funds read) cannot append its
        session_start: step 6 times out on the lock and refuses, releasing what it admitted. The manual place then
        sends before any session exists, and the next ARM refuses manual_order_open."""
        env = self.one_market()
        arm = {}
        accept = env.exchange.funds

        def funds():
            if not arm:
                arm["result"] = sr.arm_run(env)                    # Yu types PILOT ARM meanwhile, in another process
                arm["sessions"] = len(sr.notes(env, "session_start"))
            return accept()

        env.exchange.funds = funds
        sp.manual_place(env, self.market)
        self.refused(arm["result"], "lock_busy")
        self.assertEqual(arm["sessions"], 0)
        self.assertEqual(len(places(env)), 1)                      # the manual order was sent, outside a session
        self.assertIsNone(sr.admission_view(env)["admission"])     # the refused ARM released generation 1
        env.clock.advance(1)
        self.refused(sr.arm_run(env), "manual_order_open")

    def test_a_manual_place_during_an_armed_session_is_refused(self):
        from betangel_research.betfair.errors import OrderRefused
        env = self.one_market()
        seen = {}

        def manual():
            try:
                sp.manual_place(env, self.market)
            except (OrderRefused, NotArmed) as exc:
                seen["rule"] = getattr(exc, "rule", str(exc))

        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 520 * S, manual)
        code, out, err = sr.arm_run(env)
        self.assertIn("rule", seen)
        self.assertEqual(len(places(env)), 1)                      # the pilot order only
        self.assertEqual(code, 0, err)


# --- §9.13 a pending pause at the day limit ---------------------------------------------------------------------


class PauseAtTheDayLimitTests(RunCase):
    def test_the_pause_is_not_pre_empted_by_the_day_limit(self):
        """Pause step 1200p: five filled bets (-1000p) use the PT4 day limit; the sixth would reach -1200p, so the
        scheduler still reserves it and the gateway journals SKIP(pause) with its stops, rather than ending at the
        day limit first."""
        env = self.build(contract_raw=contract_bytes(**{"pause.step_p": 1200}))
        self.assertEqual(sr.register(env)[0], 0)
        for i in range(6):
            sr.add_market(env, off_ns=OFF + i * 900 * S)
        sr.fills(env, complete=True)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        summary = json.loads(out)
        self.assertEqual(summary["place_records"], 5)
        self.assertEqual(summary["skips"], {"pause": 1})
        self.assertEqual(sorted(s["stop_id"] for s in sr.stops(env)), ["pause-1200", "pause-1200-session"])
        self.assertEqual(len(places(env)), 5)


# --- §9.14 A17, note counts, static NotArmed, timing codes ---------------------------------------------------------


class StandIn:
    """A stand-in run for one pilot-run method: real paths, journal reads and Run helpers, with refusal and
    stop_note recorded instead of journaled."""

    def __init__(self, env, session_seq_=3):
        self.env, self.paths, self.contract, self.clock = env, env.paths, env.contract, env.clock
        self.session = {"session_id": "s20270104-0a0b0c0d", "seq": session_seq_}
        self.w = pilot_run.Terminal(sr.BrokenStream(), sr.BrokenStream())
        self.written = []

    def now(self):
        return self.clock.time_ns()

    def read(self, since=0):
        return pilot_run.pilot_events_after(self.paths.journal_path("live"), since)

    def covered(self, ctx):
        return pilot_run.Run.covered(self, ctx)

    def refusal(self, code, *, open_id=None, reservation_seq=None):
        self.written.append(("refusal", code))

    def stop_note(self, code, anchor, *, source_seq=None):
        self.written.append(("stop", code))


def refusal_note(env, code, *, open_id):
    return pilot_run.append_pilot(env.journal, pilot.pilot_note(
        "refusal", env.contract.sha256, session_id="s20270104-0a0b0c0d", open_id=open_id, reservation_seq=None,
        code=code, row_sha256=None, book_sha256=None))


class A17Tests(RunCase):
    """A17 (unresolved_or_foreign_orders) with no HALT. The real gateway reports it only beside an unresolved order
    the open's reconcile could not settle; these drive Run.not_armed_at_open on a stand-in with the report."""

    def at_open(self, failures, *, halted=False):
        env = self.registered()
        if halted:
            env.journal.append("halt", {"reason": "synthetic", "scope": "global"})
        run = StandIn(env)
        ctx = {"seen_seq": self.head(env), "open_id": "open-00000000000000a1", "market_id": "1.5"}
        return env, run, ctx

    def test_a_and_b_one_a17_note_beside_a_session_level_failure(self):
        for other in ("pa4_pilot_stop", "pa3_session_expired"):
            with self.subTest(beside=other):
                env, run, ctx = self.at_open(None)
                if other == "pa4_pilot_stop":                   # a session stop the gateway flushed at this open
                    pilot_run.append_pilot(env.journal, pilot_run.session_stop(env.contract, "lock_busy", 2,
                                                                               source_seq=2))
                pilot_run.Run.not_armed_at_open(run, {"failures": ["unresolved_or_foreign_orders", other]}, ctx)
                self.assertEqual(run.written, [("refusal", "unresolved_or_foreign_orders")])

    def test_c_a_review_failure_the_gateway_noted_is_the_only_note(self):
        for noted in ("login_cannot_bet", "account_details_invalid"):
            with self.subTest(noted=noted):
                env, run, ctx = self.at_open(None)
                refusal_note(env, noted, open_id=ctx["open_id"])
                pilot_run.Run.not_armed_at_open(run, {"failures": [noted, "unresolved_or_foreign_orders"]}, ctx)
                self.assertEqual(run.written, [])

    def test_d_and_e_a_halt_or_an_earlier_a17_note_writes_nothing(self):
        env, run, ctx = self.at_open(None, halted=True)
        pilot_run.Run.not_armed_at_open(run, {"failures": ["unresolved_or_foreign_orders"]}, ctx)
        self.assertEqual(run.written, [])
        env, run, ctx = self.at_open(None)
        refusal_note(env, "unresolved_or_foreign_orders", open_id=ctx["open_id"])         # the first run's note
        pilot_run.Run.not_armed_at_open(run, {"failures": ["unresolved_or_foreign_orders"]}, ctx)
        self.assertEqual(run.written, [])

    def test_f_the_next_day_after_a17_and_an_overnight_resolution(self):
        """The A17 note pilot-run journaled is a review occurrence: after the order is resolved overnight (here:
        no order is left unresolved), the next-day ARM refuses pilot_review_stop_active until a caused ACK."""
        env = self.registered()
        self.assertEqual(sr.arm_run(env)[0], 0)
        refusal_note(env, "unresolved_or_foreign_orders", open_id="open-00000000000000a1")
        self.next_day_arm(env)


class OpenNoteCountTests(RunCase):
    def test_an_anchor_notarmed_under_the_lock_is_one_note(self):
        """The Keychain anchor read fails under the lock (after static arming passed): the gateway journals the one
        refusal note for the open; pilot-run adds none; one ACK clears it."""
        env = self.one_market()
        original_ok = OrderGateway._keychain_anchor_ok

        def anchor_ok(gateway):
            if gateway._open_id is not None:                     # the pilot-place open, under the lock
                env.runner.fail_services = {ANCHOR_SERVICE}
            try:
                return original_ok(gateway)
            finally:
                env.runner.fail_services = set()

        with patch.object(OrderGateway, "_keychain_anchor_ok", anchor_ok):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        notes = sr.notes(env, "refusal")
        self.assertEqual(len(notes), 1, notes)
        self.assertTrue(notes[0]["code"].startswith("journal_anchor") or "anchor" in notes[0]["code"], notes)
        self.assertEqual(len(sr.active_review(env)), 1)
        self.assertEqual(places(env), [])

    def test_a_dynamic_failure_at_open_is_one_note(self):
        env = self.one_market()
        from betangel_research.betfair import gateway as gateway_module
        original = gateway_module.dynamic_arming

        def dynamic(*a, **kw):
            report = original(*a, **kw)
            if kw.get("command") == "pilot-place":
                report = dict(report, armed=False, failures=list(report["failures"]) + ["login_cannot_bet"])
            return report

        with patch.object(gateway_module, "dynamic_arming", dynamic):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["login_cannot_bet"])
        self.assertEqual(len(sr.active_review(env)), 1)
        self.assertEqual(places(env), [])

    def test_a_flushed_due_stop_does_not_count_as_the_open_s_note(self):
        env = self.registered()
        run = StandIn(env)
        ctx = {"seen_seq": self.head(env), "open_id": "open-00000000000000b2", "market_id": "1.5"}
        pilot_run.append_pilot(env.journal, pilot_run.session_stop(env.contract, "lock_busy", 2, source_seq=2))
        self.assertFalse(pilot_run.Run.covered(run, ctx))
        refusal_note(env, "pa2_code_dirty", open_id=ctx["open_id"])
        self.assertTrue(pilot_run.Run.covered(run, ctx))


class StaticNotArmedTests(RunCase):
    """Run.not_armed on a stand-in, for each A7 cause mix (the gateway raises static NotArmed before the lock, so
    the note is pilot-run's to write, P1-06 Q7). The KILL file, the journal and lstat are real."""

    def raise_(self, env, failures):
        run = StandIn(env)
        ctx = {"seen_seq": self.head(env), "open_id": None, "market_id": "1.5"}
        pilot_run.Run.not_armed(run, NotArmed(failures[0], detail={"failures": failures}), ctx)
        return run.written

    def test_each_cause_mix(self):
        static_review = sorted(c for c in pilot_run.STATIC_CODES if c != "halted")[0]
        cases = {
            "KILL plus a static review code": (["halted", static_review], "kill", None,          # review first
                                               [("refusal", "not_armed_before_lock"), ("stop", "kill_file")]),
            "KILL plus a journal HALT": (["halted"], "kill", "halt", [("stop", "kill_file")]),
            "an unfoldable journal": (["halted"], None, "unfoldable", [("refusal", "not_armed_before_lock")]),
            "a journal HALT with no KILL": (["halted"], None, "halt", []),
            "KILL removed before the probe": (["halted"], None, None, [("refusal", "not_armed_before_lock")]),
        }
        for name, (failures, kill, journal, expected) in cases.items():
            with self.subTest(case=name):
                env = self.registered()
                if kill:
                    env.paths.kill_file.write_text("")
                if journal == "halt":
                    env.journal.append("halt", {"reason": "synthetic", "scope": "global"})
                patcher = patch.object(pilot_run, "fold_events", side_effect=JournalError("unfoldable")) \
                    if journal == "unfoldable" else None
                if patcher:
                    patcher.start()
                try:
                    self.assertEqual(self.raise_(env, failures), expected)
                finally:
                    if patcher:
                        patcher.stop()

    def test_a_kill_whose_lstat_fails_counts_as_kill(self):
        env = self.registered()
        real = os.lstat
        target = str(env.paths.kill_file)

        def lstat(path, *a, **kw):
            if str(path) == target:
                raise PermissionError(13, "denied")
            return real(path, *a, **kw)

        with patch.object(os, "lstat", lstat):
            written = self.raise_(env, ["halted"])
        self.assertEqual(written[0], ("stop", "kill_file"))

    def test_a_repeated_static_kill_does_not_duplicate_kill_file(self):
        env = self.registered()
        env.paths.kill_file.write_text("")
        pilot_run.append_pilot(env.journal, pilot_run.session_stop(env.contract, "kill_file", 3, source_seq=3))
        self.assertEqual(self.raise_(env, ["halted"]), [])


class TimingCodeTests(RunCase):
    """Each timing refusal pilot_place returns past the scheduler's pre-checks (forced here by the named race):
    one review refusal note, no second pilot_place, no placeOrders, no release, next-day ARM refuses until ACK."""

    def check(self, env, code, patches, *, until="2000"):
        calls = []
        original = OrderGateway.pilot_place

        def pilot_place(gateway, *a, **kw):
            calls.append(True)
            return original(gateway, *a, **kw)

        patches = list(patches) + [patch.object(OrderGateway, "pilot_place", pilot_place)]
        for item in patches:
            item.start()
        try:
            exit_code, out, err = sr.arm_run(env, until)
        finally:
            for item in patches:
                item.stop()
        self.assertEqual(exit_code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], [code])
        self.assertEqual((len(calls), places(env)), (1, []))
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        sr.typed_release(env, 1)
        self.next_day_arm(env)

    def test_trigger_before_deadline(self):
        """The scheduler's trigger look-ahead saw no trigger; by the reservation one is due (a race)."""
        env = self.one_market()
        original = pilot_gate.trigger_blocks

        def trigger_blocks(state, **kw):
            if kw.get("look_ahead", True):
                return original(state, **kw)
            return "refuse"

        self.check(env, "pilot_trigger_before_deadline",
                   [patch.object(pilot_gate, "trigger_blocks", trigger_blocks)])

    def test_reserve_end_margin(self):
        """A market 30 s before the session's end: the scheduler's margin pre-check is skipped (as if its answer went
        stale); the gateway's own margin check refuses."""
        env = self.registered()
        sr.add_market(env, off_ns=OFF + 10 * 60 * S - 30 * S)        # off 10:09:30, session end 10:10
        original = pilot_run.Run.plan

        def plan(run, state, candidates):
            return candidates[0] if candidates else original(run, state, candidates)

        self.check(env, "pilot_reserve_end_margin", [patch.object(pilot_run.Run, "plan", plan)], until="1010")

    def test_reserve_too_late(self):
        """The reservation's compare-and-append boundary has passed by the time it commits."""
        env = self.one_market()
        self.check(env, "pilot_reserve_too_late",
                   [patch.object(OrderGateway, "_pilot_boundary", lambda gateway, state: gateway.clock.time_ns())])


# --- §9.15 case 4 with an open events() generator --------------------------------------------------------------


class Case4GeneratorTests(RunCase):
    def test_an_open_generator_on_the_gateway_connection_does_not_hide_case_3(self):
        """An events() generator is held open on the gateway's own journal connection for the whole case-4 wait,
        while another connection appends kill and then kill_incomplete: pilot-run's fresh read-only reads still see
        the outcome (case 3), and once the wait is over it sends its own whole cancel. (The generator is closed when
        the wait ends: a read held open on the gateway's connection across the gateway's own cancel append is a
        different matter, outside pilot-run.)"""
        env = self.one_market()
        held, cases = {}, []
        original_remainder, original_case = pilot_run.Run.remainder, pilot_run.Run.kill_case

        def remainder(run, gateway, ctx, market_id, p):
            held["generator"] = gateway.journal.events()
            next(held["generator"])
            kill_event(env)                                        # a kill with no outcome yet: case 4 begins

            def outcome():                                         # another connection, during the case-4 wait
                other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
                try:
                    other.append("halt", {"reason": "kill_incomplete", "scope": "global"})
                finally:
                    other.close()

            sr.on_sleep(env, lambda clock: True, outcome)
            return original_remainder(run, gateway, ctx, market_id, p)

        def kill_case(run, submit_seq):
            try:
                found = original_case(run, submit_seq)
            finally:
                held.pop("generator").close()
            cases.append(found)
            return found

        with patch.object(pilot_run.Run, "remainder", remainder), patch.object(pilot_run.Run, "kill_case", kill_case):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(cases, ["kill"])
        self.assertEqual(len(cancels(env)), 1)                       # case 3: its own whole cancel
        self.assertNotIn("own-scope kill", err)
        self.assertEqual(json.loads(out)["abnormal"], "kill")


# --- §9.16 a day-cap session stop, §9.21 due stops after a capture batch --------------------------------------------


class DayCapTests(RunCase):
    def test_an_r6_day_cap_session_stop_blocks_release(self):
        """Another process's 25 submitted instructions today: R6's day cap refuses before the reservation; the
        gateway journals its session stop; pilot-run ends placing and does not release."""
        env = self.one_market()

        def foreign_submits():
            other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
            try:
                other.append("submit", {"action": "place", "instruction_count": 25, "body_sha256": None,
                                        "decision_id": "dec-foreign", "params": None, "transitions": []})
            finally:
                other.close()

        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 520 * S, foreign_submits)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        session = [s["stop_id"] for s in sr.stops(env, "session")]
        self.assertTrue(any(stop_id.startswith("rate-day-") for stop_id in session), session)
        self.assertEqual(sr.notes(env, "reservation"), [])
        self.assertEqual(places(env), [])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)


class CaptureDueStopTests(RunCase):
    def test_a_failed_capture_check_is_flushed_and_ends_placing(self):
        """A C read showing EXECUTION_COMPLETE whose sizes do not add up to £2.00: the fold implies a review stop;
        the batch flushes it under a brief lock and placing ends (no second placement, no release)."""
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        from betangel_research.betfair.api import ReadClient
        flag = []
        original_raw = ReadClient.raw_current_order

        def raw(client, bet_id, **kw):
            flag.append(True)
            try:
                return original_raw(client, bet_id, **kw)
            finally:
                flag.clear()

        original = env.exchange.current
        env.exchange.current = lambda **kw: [dict(r, sizeCancelled=1.0) for r in original(**kw)] if flag else \
            original(**kw)
        with patch.object(ReadClient, "raw_current_order", raw):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(len(sr.notes(env, "capture_c")), 1)
        self.assertEqual([s["reason"] for s in sr.stops(env, "review")], ["capture_check_failed"])
        self.assertEqual(sr.fold(env).due_stops, ())
        self.assertEqual(len(places(env)), 1)
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)


if __name__ == "__main__":
    unittest.main()
