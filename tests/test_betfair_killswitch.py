"""Kill switch: the global KILL file, the two-leg target set, the lock-free sweep with its re-sweeps, account-wide
cancel and resume (amendment A3, D4). Offline against a simulated exchange. These show what the kill path does
and refuses; matched bets are never undone, and nothing here shows Betfair's real cancel or in-play timings."""

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from betangel_research.betfair import arming
from betangel_research.betfair import gateway as gateway_module
from betangel_research.betfair import killswitch
from betangel_research.betfair.api import JsonRpcClient, ReadClient
from betangel_research.betfair.auth import SessionManager
from betangel_research.betfair.constants import IN_PLAY_DELAY_MAX_S, ORDER_TIMEOUT_S, ORDER_VISIBILITY_S
from betangel_research.betfair.credentials import (ANCHOR_SERVICE, SERVICES, anchor_read, anchor_write,
                                                    load_credentials)
from betangel_research.betfair.errors import ConfigError, Halted, NotArmed, OrderRefused, TransportError
from betangel_research.betfair.gateway import OrderGateway
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.killswitch import engage, sweep, target_set
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, file_lock, kill_engaged
from betangel_research.betfair.receipts import parse_utc_ns, utc_from_ns
from betangel_research.betfair.risk import RiskGuard
from betangel_research.betfair.secret import Secret

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_orders as orders


def run_cli_main(env, argv):
    """cli.main in process over env's fakes: (exit code, stdout, stderr)."""
    import contextlib
    import io
    from betangel_research.betfair.cli import main
    out, err = io.StringIO(), io.StringIO()
    deps = {"keychain_runner": env.runner, "http_transport": env.transport, "clock": env.clock,
            "confirmer": env.confirmer}
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, deps=deps)
    return code, out.getvalue(), err.getvalue()


class KillSwitchTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.clock = support.FakeClock()
        self.exchange = gw.SimExchange(self.clock)
        self.read = gw.FakeReadClient(self.exchange)

    def cancel_on_exchange(self, market_id, bet_ids, kill_targets):
        for bet in bet_ids:
            self.exchange._cancel_one(bet)
        return {"decision": "accepted"}

    def test_engage_without_lock_or_login(self):
        paths = support.temp_state_paths(self)
        with file_lock(paths.gateway_lock):                 # another process holds the gateway lock
            with patch("subprocess.run", side_effect=AssertionError("no Keychain")):
                first = engage(paths, "operator stop", clock=self.clock)
        self.assertEqual(first["status"], "kill_engaged")
        self.assertTrue(kill_engaged(paths))
        self.assertEqual(paths.kill_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(paths.kill_file.read_text()), {"at": self.clock.utc_now(), "reason": "operator stop"})
        second = engage(paths, "again", clock=self.clock)
        self.assertEqual(second["status"], "kill_already_engaged")
        self.assertEqual(json.loads(paths.kill_file.read_text())["reason"], "operator stop")
        other = support.temp_state_paths(self)
        engage(other, support.PLANTED_SECRET + "\n<script>", clock=self.clock)
        self.assertEqual(json.loads(other.kill_file.read_text())["reason"], "reason_not_recorded")
        Secret(support.PLANTED_SECRET)
        third = support.temp_state_paths(self)
        engage(third, support.PLANTED_SECRET, clock=self.clock)
        self.assertNotIn(support.PLANTED_SECRET, third.kill_file.read_text())

    def test_kill_file_blocks_other_process_gateway(self):
        env = gw.build_paper_gateway(self)
        env.gateway.open(command="place")
        self.addCleanup(env.gateway.close)
        accepted = env.gateway.place("1.1", [gw.instruction()], book=gw.paper_book(env.clock), source="api")
        self.assertEqual(accepted["decision"], "accepted")
        config_path = env.paths.config_path
        completed = subprocess.run(
            [sys.executable, "-m", "betangel_research.betfair", "--state-root", str(env.paths.root), "--config",
             str(config_path), "kill", "--reason", "from another process"],
            cwd=str(support.REPO_ROOT), capture_output=True, text=True, timeout=120,
            env=dict(os.environ, **{FORBID_NETWORK_ENV: "1"}))
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertTrue(kill_engaged(env.paths))
        refused = env.gateway.place("1.1", [gw.instruction()], book=gw.paper_book(env.clock), source="api")
        self.assertEqual(refused["decision"], "refused")
        self.assertEqual(refused["refusal"]["rule"], "halted")

    def test_kill_rechecked_after_submit_journal(self):
        env = gw.build_live_gateway(self, clock=self.clock)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        original = env.journal.append

        def append_then_kill(kind, payload, **columns):
            event = original(kind, payload, **columns)
            if kind == "submit":
                engage(env.paths, "concurrent kill", clock=self.clock)
            return event

        with patch.object(env.journal, "append", append_then_kill):
            result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual(result["decision"], "refused")
        self.assertEqual(result["refusal"]["detail"]["reason"], "kill_engaged_after_submit_journal")
        self.assertEqual(env.router.order_requests, [])
        self.assertEqual(result["orders"][0]["state"], "REFUSED")
        fold = env.journal.fold(now_ns=self.clock.time_ns())
        self.assertIn("kill_engaged_after_submit_journal", fold["halt_reasons"])
        # The same holds in paper mode.
        paper = gw.build_paper_gateway(self)
        paper.gateway.open(command="place")
        self.addCleanup(paper.gateway.close)
        original_paper = paper.journal.append

        def paper_append(kind, payload, **columns):
            event = original_paper(kind, payload, **columns)
            if kind == "submit":
                engage(paper.paths, "concurrent kill", clock=self.clock)
            return event

        with patch.object(paper.journal, "append", paper_append):
            refused = paper.gateway.place("1.1", [gw.instruction()], book=gw.paper_book(paper.clock), source="api")
        self.assertEqual(refused["orders"][0]["state"], "REFUSED")

    def test_target_set_is_our_refs_and_journal_bet_ids_only(self):
        """Amendment A3, D4: the kill's legs are our strategy refs and our journal's betIds. A bet visible only
        through its sourceIdKey (our app key, no ref of ours, unknown to the journal) is neither targeted nor
        cancelled: only the explicit account-wide scope cancels bets that are not certainly ours."""
        by_ref = self.exchange.add_order(strategy_ref="bar-manual", source_key="vendor-key")
        by_bet = self.exchange.add_order(strategy_ref=None, source_key="vendor-key")
        by_key = self.exchange.add_order(strategy_ref=None, source_key=gw.LIVE_KEY)
        done = self.exchange.add_order(strategy_ref="bar-manual", status="EXECUTION_COMPLETE", matched=2.0, average=3.5)
        state = orders.state_with([orders.order_record(mode="live", bet_id=by_bet)], mode="live")
        found = target_set(self.read, state, strategy_refs=["bar-manual"])
        self.assertEqual(found["targets"], {"1.1": sorted([by_ref, by_bet])})
        self.assertEqual(found["sources"], {by_ref: ["strategy_ref"], by_bet: ["bet_id"]})
        self.assertNotIn(by_key, json.dumps(found))
        self.assertNotIn(done, found["sources"])
        self.assertEqual(found["read_failures"], [])
        self.assertFalse(any(kw.get("include_source_id") for name, kw in self.exchange.calls))
        self.assertEqual(killswitch.LEGS, ("strategy_ref", "bet_id"))
        cancelled = []

        def cancel_fn(market_id, bet_ids, kill_targets):
            cancelled.extend(bet_ids)
            for bet in bet_ids:
                self.exchange._cancel_one(bet)
            return {"decision": "accepted"}

        result = sweep(self.read, state, cancel_fn=cancel_fn, strategy_refs=["bar-manual"], clock=self.clock)
        self.assertEqual((result["status"], sorted(cancelled)), ("kill_complete", sorted([by_ref, by_bet])))
        self.assertEqual(self.exchange.orders[by_key]["status"], "EXECUTABLE")
        self.exchange.read_failures["listCurrentOrders"] = [TransportError("connection_lost", phase="receive")]
        failed = target_set(self.read, state, strategy_refs=["bar-manual"])
        self.assertEqual(failed["read_failures"], [{"leg": "strategy_ref", "code": "connection_lost"}])

    def test_cancel_by_bet_id_sequential_max60(self):
        bets = [self.exchange.add_order(strategy_ref="bar-manual") for _ in range(130)]
        other = [self.exchange.add_order(strategy_ref="bar-manual", market_id="1.2") for _ in range(5)]
        calls = []

        def cancel_fn(market_id, bet_ids, kill_targets):
            calls.append((market_id, list(bet_ids)))
            self.assertEqual({t["bet_id"] for t in kill_targets}, set(bet_ids))
            for bet in bet_ids:
                self.exchange._cancel_one(bet)
            return {"decision": "accepted"}

        result = sweep(self.read, orders.empty_state("live"), cancel_fn=cancel_fn, strategy_refs=["bar-manual"],
                       clock=self.clock)
        self.assertEqual(result["status"], "kill_complete")
        self.assertEqual([len(c[1]) for c in calls], [60, 60, 10, 5])
        self.assertEqual([c[0] for c in calls], ["1.1", "1.1", "1.1", "1.2"])
        self.assertEqual(sorted(b for c in calls for b in c[1]), sorted(bets + other))
        self.assertEqual(result["passes"], 3)                  # the re-sweeps always run (nothing left to cancel)
        self.assertEqual(result["remaining"], [])

    def test_resweeps_run_from_the_kill_file_and_cover_the_bound(self):
        """Amendment A3, D4: passes at T0, T0 + 20 s and T0 + 35 s on the monotonic clock, T0 being when the
        KILL file was engaged, with no journal submit at all; then the final check. The last offset covers
        F28's 5 s order timeout and 15 s visibility plus F42's 12 s in-play delay with 3 s for transit. The
        wall clock is never read, so a stepped wall clock changes nothing."""
        self.assertEqual(killswitch.RESWEEP_OFFSETS_S, (20, 35))
        self.assertGreaterEqual(killswitch.RESWEEP_OFFSETS_S[-1],
                                ORDER_TIMEOUT_S + ORDER_VISIBILITY_S + IN_PLAY_DELAY_MAX_S + 3)
        self.assertEqual((ORDER_TIMEOUT_S, ORDER_VISIBILITY_S, IN_PLAY_DELAY_MAX_S), (5, 15, 12))
        self.exchange.add_order(strategy_ref="bar-manual")
        reads = []
        original = self.exchange.current

        def timed(**kw):
            reads.append(self.clock.mono_ns)
            return original(**kw)

        lines = []
        engaged = self.clock.mono_ns
        self.clock.advance(4)                      # the CLI's journal appends, Keychain reads and login took 4 s
        with patch.object(self.exchange, "current", timed):
            result = sweep(self.read, orders.empty_state("live"), cancel_fn=self.cancel_on_exchange,
                           strategy_refs=["bar-manual"], clock=MonotonicOnly(self.clock), engaged_monotonic_ns=engaged,
                           progress=lines.append)
        self.assertEqual((result["status"], result["passes"]), ("kill_complete", 3))
        self.assertEqual(self.clock.sleeps, [16.0, 15.0])
        self.assertEqual([(r - engaged) // 10**9 for r in reads], [4, 20, 35, 35])
        self.assertEqual(lines, [
            "pass 1 of 3; next pass in 16 s (orders sent just before the kill can still appear; bound 35 s after the "
            "KILL file)",
            "pass 2 of 3; next pass in 15 s (orders sent just before the kill can still appear; bound 35 s after the "
            "KILL file)",
            "final check"])
        # A pass whose time has already passed runs at once; no wait is ever longer than the last offset.
        self.clock.sleeps.clear()
        sweep(self.read, orders.empty_state("live"), cancel_fn=lambda m, b, k: {}, strategy_refs=["bar-manual"],
              clock=self.clock, engaged_monotonic_ns=self.clock.mono_ns - 60 * 10**9)
        self.assertEqual(self.clock.sleeps, [])
        sweep(self.read, orders.empty_state("live"), cancel_fn=lambda m, b, k: {}, strategy_refs=["bar-manual"],
              clock=self.clock, engaged_monotonic_ns=self.clock.mono_ns + 3600 * 10**9)
        self.assertEqual(self.clock.sleeps, [35.0, 35.0])
        # Without a T0 the sweep times its passes from its own start.
        self.clock.sleeps.clear()
        sweep(self.read, orders.empty_state("live"), cancel_fn=lambda m, b, k: {}, strategy_refs=["bar-manual"],
              clock=self.clock)
        self.assertEqual(self.clock.sleeps, [20.0, 15.0])

    def test_resweep_covers_the_longest_in_flight_order(self):
        """A place request whose bytes left before the KILL file can surface up to 32 s later (5 s order
        timeout, 15 s visibility, 12 s in-play delay): the 35 s pass still finds and cancels it."""
        env = gw.build_live_gateway(self)
        engaged = env.clock.mono_ns
        late = {}
        sleep = env.clock.sleep

        def surfaces_at_32s(seconds):
            since = (env.clock.mono_ns - engaged) / 10**9
            if not late and since < 32 <= since + seconds:
                sleep(32 - since)
                late["bet"] = env.exchange.add_order(strategy_ref="bar-manual")
                late["at_s"] = (env.clock.mono_ns - engaged) / 10**9
                sleep(since + seconds - 32)
            else:
                sleep(seconds)

        gw.open_live(self, env, "kill")
        with patch.object(env.clock, "sleep", side_effect=surfaces_at_32s):
            out = env.gateway.kill(scope="own", reason="test", engaged_monotonic_ns=engaged)
        self.assertEqual(late["at_s"], 32)
        self.assertEqual(out["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[late["bet"]]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(len(env.router.order_requests), 1)

    def test_resweeps_are_timed_from_the_engage_that_created_the_kill_file(self):
        """A T0 from before the KILL file existed (the CLI's engage failed, or the file was removed meanwhile)
        would end the re-sweeps too early: when the gateway's own engage creates the file, T0 is now."""
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "kill")
        long_ago = env.clock.mono_ns - 3600 * 10**9
        self.assertFalse(kill_engaged(env.paths))
        env.gateway.kill(scope="own", reason="drill", engaged_monotonic_ns=long_ago)
        self.assertEqual(env.clock.sleeps, [20.0, 15.0])
        env.clock.sleeps.clear()
        env.gateway.kill(scope="own", reason="drill", engaged_monotonic_ns=long_ago)     # already engaged
        self.assertEqual(env.clock.sleeps, [])

    def test_a_cancel_that_raises_never_stops_the_sweep(self):
        """Regression (live-money review): _cancel_targets caught BetfairError only, so one market's cancel that
        raised anything else (sqlite3 'database is locked' from a kill append past its busy timeout, or an
        unexpected error _send re-raises) ended the whole sweep: the other market was never cancelled and no
        re-sweep or final check ran. Now every chunk is isolated (Exception, not BaseException), all three
        passes and the final check run, and a cancel that raised makes the kill incomplete even when the final
        check is clean."""
        first = self.exchange.add_order(strategy_ref="bar-manual", market_id="1.1")
        second = self.exchange.add_order(strategy_ref="bar-manual", market_id="1.2")
        calls = []

        def cancel_fn(market_id, bet_ids, kill_targets):
            calls.append(market_id)
            if market_id == "1.1":
                for bet in bet_ids:
                    self.exchange._cancel_one(bet)                 # sent, then the report append fails
                raise sqlite3.OperationalError("database is locked")
            return self.cancel_on_exchange(market_id, bet_ids, kill_targets)

        reads = []
        original = self.read.iter_current_orders

        def counting(**kw):
            reads.append(kw)
            return original(**kw)

        self.read.iter_current_orders = counting
        result = sweep(self.read, orders.empty_state("live"), cancel_fn=cancel_fn, strategy_refs=["bar-manual"],
                       clock=self.clock)
        self.assertEqual(calls[:2], ["1.1", "1.2"])
        self.assertEqual((self.exchange.orders[first]["status"], self.exchange.orders[second]["status"]),
                         ("EXECUTION_COMPLETE", "EXECUTION_COMPLETE"))
        self.assertEqual(result["passes"], 3)
        self.assertEqual(len(reads), 4)                            # three passes and the final check
        self.assertEqual(self.clock.sleeps, [20.0, 15.0])
        self.assertEqual(result["remaining"], [])
        self.assertEqual(result["status"], "kill_incomplete")
        self.assertEqual(result["notes"], ["kill_cancel_raised"])
        self.assertEqual(result["cancel_failures"],
                         [{"market_id": "1.1", "bet_ids": [first], "raised": True, "code": "OperationalError"}])
        self.assertEqual(result["cancel_requested_bet_ids"], sorted([first, second], key=int))
        # A signal or Ctrl-C (BaseException) still ends the kill at once (exit 3 in the CLI).
        self.exchange.add_order(strategy_ref="bar-manual")

        def interrupted(market_id, bet_ids, kill_targets):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            sweep(self.read, orders.empty_state("live"), cancel_fn=interrupted, strategy_refs=["bar-manual"],
                  clock=self.clock)

    def test_kill_cancels_every_market_when_its_journal_writes_fail(self):
        """Regression (live-money review), through the gateway: the first kill cancel_intent append raised
        sqlite3.OperationalError and the second market's bet was never cancelled. The kill's journal writes are
        best effort now (its events move no order): after the first one that fails none is attempted, every
        cancel is still sent, and the kill ends kill_incomplete naming the failure."""
        env = gw.build_live_gateway(self)
        first = env.exchange.add_order(strategy_ref="bar-manual", market_id="1.1")
        second = env.exchange.add_order(strategy_ref="bar-manual", market_id="1.2")
        gw.open_live(self, env, "kill")
        real_append = env.journal.append
        kinds = []

        def locked_once(kind, payload, **columns):
            kinds.append(kind)
            if kind == "cancel_intent" and kinds.count("cancel_intent") == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_append(kind, payload, **columns)

        with patch.object(env.journal, "append", side_effect=locked_once):
            out = env.gateway.kill(scope="own", reason="drill")
        self.assertEqual((env.exchange.orders[first]["status"], env.exchange.orders[second]["status"]),
                         ("EXECUTION_COMPLETE", "EXECUTION_COMPLETE"))
        self.assertEqual(out["passes"], 3)
        self.assertEqual((out["status"], out["journal_write_failed"], out["journal_halt_kill_incomplete"]),
                         ("kill_incomplete", "OperationalError", "failed"))
        self.assertIn("kill_journal_write_failed", out["notes"])
        self.assertEqual(kinds[-1], "cancel_intent")                  # nothing attempted after the failure
        self.assertEqual(len([r for r in env.router.order_requests if r["method"].endswith(gw.CANCEL_SUFFIX)]), 2)
        # Any other command's append still raises: only the live kill writes best effort.
        env.gateway.close()
        gw.open_live(self, env, "cancel")
        with patch.object(env.journal, "append", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                env.gateway.cancel("1.1", [first], source="cli_manual")

    def test_kill_incomplete_keeps_halt_exit3(self):
        self.exchange.add_order(strategy_ref="bar-manual")

        def refusing(market_id, bet_ids, kill_targets):
            return {"decision": "refused", "refusal": {"rule": "validation:V2"}}

        result = sweep(self.read, orders.empty_state("live"), cancel_fn=refusing, strategy_refs=["bar-manual"],
                       clock=self.clock)
        self.assertEqual(result["status"], "kill_incomplete")
        self.assertEqual(len(result["remaining"]), 1)
        self.assertEqual(result["cancel_failures"][0]["code"], "validation:V2")
        # Through the gateway: a read failure in the verify pass keeps the HALT (kill_incomplete).
        env = gw.build_live_gateway(self)
        env.exchange.add_order(strategy_ref="bar-manual")
        env.gateway.open(command="kill")
        self.addCleanup(env.gateway.close)
        original = env.exchange.current
        counter = {"n": 0}

        def flaky(**kw):
            counter["n"] += 1
            if counter["n"] > 3:
                raise TransportError("connection_lost", phase="receive")
            return original(**kw)

        with patch.object(env.exchange, "current", flaky):
            killed = env.gateway.kill(scope="own", reason="test")
        self.assertEqual(killed["status"], "kill_incomplete")
        fold = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertTrue(fold["halted"])
        self.assertIn("kill_incomplete", fold["halt_reasons"])
        self.assertEqual(len(env.router.order_requests), 1)
        env.gateway.close()
        # Through the CLI a sweep that ends kill_incomplete exits 3 (our bet stays: every cancel fails).
        os.unlink(env.paths.kill_file)
        stuck = env.exchange.add_order(strategy_ref="bar-manual")
        env.exchange.cancel_handler = lambda params: {"customerRef": params["customerRef"], "status": "FAILURE",
                                                      "errorCode": "ERROR_IN_MATCHER", "marketId": params["marketId"],
                                                      "instructionReports": []}
        code, out, err = run_cli_main(env, ["kill", "--reason", "test"])
        self.assertEqual(code, 3, err)
        self.assertIn("Betfair kill incomplete: kill_incomplete;", err)
        self.assertEqual(json.loads(out)["status"], "kill_incomplete")
        self.assertEqual(json.loads(out)["remaining"], [stuck])
        self.assertEqual(env.exchange.orders[stuck]["status"], "EXECUTABLE")

    def test_kill_login_failure_kill_incomplete_no_session(self):
        from betangel_research.betfair.cli import main
        env = gw.build_live_gateway(self)
        env.router.login_status = "FAIL"

        class Sink:
            def __init__(self):
                self.text = ""

            def write(self, value):
                self.text += value

            def flush(self):
                pass

        stdout, stderr = Sink(), Sink()
        deps = {"keychain_runner": env.runner, "http_transport": env.transport, "clock": env.clock,
                "confirmer": env.confirmer}
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            code = main(["kill", "--reason", "test"], deps=deps)
        self.assertEqual(code, 3, stderr.text)
        self.assertIn("kill_incomplete_no_session", stderr.text)
        self.assertIn("Betfair website or in Bet Angel", stderr.text)
        self.assertNotIn("Traceback", stderr.text)
        self.assertTrue(kill_engaged(env.paths))
        self.assertEqual(json.loads(stdout.text)["status"], "kill_incomplete_no_session")
        self.assertEqual(env.router.order_requests, [])

    def test_account_wide_needs_flag_phrase_runs_once(self):
        env = gw.build_live_gateway(self)
        bet_angel = env.exchange.add_order(strategy_ref=None, source_key="bet-angel-key")
        ours = env.exchange.add_order(strategy_ref="bar-manual")

        def account_calls():
            return [r for r in env.router.order_requests if set(r["params"]) == {"customerRef"}]

        # Without the flag: our own bets are still swept; the account-wide step is reported as not sent.
        gw.open_live(self, env, "kill")
        result = env.gateway.kill(scope="account", reason="test")
        self.assertEqual(result["status"], "kill_incomplete")
        self.assertEqual(result["account_wide"]["reason"], "account_wide_needs_confirm_flag")
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(env.exchange.orders[bet_angel]["status"], "EXECUTABLE")
        self.assertEqual(account_calls(), [])
        self.assertIn("kill_incomplete", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])
        env.gateway.close()
        # A mistyped phrase: the own sweep has run, nothing account-wide is sent, the kill is incomplete.
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        gw.open_live(self, env, "kill-account")
        env.confirmer.lines = ["CANCEL ALL"]
        result = env.gateway.kill(scope="account", reason="test", confirm_account_wide=True)
        self.assertEqual((result["status"], result["account_wide"]["reason"]),
                         ("kill_incomplete", "operator_echo_mismatch"))
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(account_calls(), [])
        env.confirmer.lines = None
        result = env.gateway.kill(scope="account", reason="test", confirm_account_wide=True)
        self.assertEqual(len(account_calls()), 1)
        self.assertEqual(env.confirmer.prompts[-1]["expected"],
                         "CANCEL ALL UNMATCHED ON ACCOUNT INCLUDING BET ANGEL STOPS")
        self.assertEqual(result["status"], "kill_complete")
        self.assertEqual(result["account_wide"]["status"], "SUCCESS")
        self.assertEqual(env.exchange.orders[bet_angel]["status"], "EXECUTION_COMPLETE")
        # No journal scan for an earlier unreported cancel-all (amendment A3, D4): a crashed kill-account no
        # longer blocks every later one. A second account-wide cancel is sent; when Betfair rejects it as a
        # concurrent cancel-all (F31: FAILURE / ERROR_IN_MATCHER) the kill is incomplete, never "harmless".
        env.journal.append("submit", {"action": "cancel_all", "instruction_count": 0, "body_sha256": None},
                           intent_id="int-unreported")
        env.exchange.cancel_handler = lambda params: {"customerRef": params["customerRef"], "status": "FAILURE",
                                                      "errorCode": "ERROR_IN_MATCHER", "instructionReports": []}
        result = env.gateway.kill(scope="account", reason="test", confirm_account_wide=True)
        self.assertEqual(len(account_calls()), 2)
        self.assertEqual((result["status"], result["account_wide"]["status"], result["account_wide"]["error_code"]),
                         ("kill_incomplete", "FAILURE", "ERROR_IN_MATCHER"))
        self.assertIn("kill_incomplete", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])

    def test_account_wide_failure_is_kill_incomplete(self):
        exchange_cancel = gw.SimExchange.cancel
        cases = {"failure": lambda params: {"customerRef": params["customerRef"], "status": "FAILURE",
                                             "errorCode": "ERROR_IN_MATCHER", "instructionReports": []},
                 "transport": gw.raises(TransportError("connection_lost", phase="receive"))}
        for name, failing in cases.items():
            with self.subTest(case=name):
                env = gw.build_live_gateway(self)
                bet_angel = env.exchange.add_order(strategy_ref=None, source_key="bet-angel-key")

                def cancel(params, env=env, failing=failing):
                    if "instructions" not in params:           # only the account-wide request fails
                        return failing(params)
                    env.exchange.cancel_handler = None
                    try:
                        return exchange_cancel(env.exchange, params)
                    finally:
                        env.exchange.cancel_handler = cancel

                env.exchange.cancel_handler = cancel
                gw.open_live(self, env, "kill-account")
                result = env.gateway.kill(scope="account", reason="test", confirm_account_wide=True)
                self.assertEqual(result["status"], "kill_incomplete")
                self.assertEqual(env.exchange.orders[bet_angel]["status"], "EXECUTABLE")
                self.assertIn("kill_incomplete", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])
                env.gateway.close()
                # Through the CLI: exit 3, never kill_complete.
                os.unlink(env.paths.kill_file)
                code, out, err = run_cli_main(env, ["kill", "--scope", "account", "--confirm-account-wide",
                                                    "--reason", "x"])
                self.assertEqual(code, 3, err)
                self.assertEqual(json.loads(out)["status"], "kill_incomplete")

    def test_cli_kill_journals_the_halt_and_needs_no_bet_angel_flag(self):
        env = gw.build_live_gateway(self, overrides={"coexistence": {"bet_angel": "unverified"}})
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        before = env.journal.anchor()["seq"]
        code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])          # no --bet-angel-paused
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["status"], result["journal_halt"]), ("kill_complete", "appended"))
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        events = list(env.journal.events(since_seq=before))
        self.assertEqual([e["kind"] for e in events[:2]], ["kill", "halt"])
        self.assertEqual(events[1]["payload"], {"reason": "kill", "scope": "global"})

    def test_kill_with_an_invalid_profile_still_engages_the_kill_file(self):
        """Round 4: cmd_kill built the profile's paths before engaging, so a profile name mistyped under stress
        (--profile Main) exited 2 invalid_profile with no KILL file and no advice while gateways could keep
        sending. STATE_ROOT/KILL is the same for every profile, so it is engaged all the same, and the kill ends
        exit 3 with the advice (no journal, config, login or sweep: those belong to a profile)."""
        env = gw.build_live_gateway(self)
        env.exchange.add_order(strategy_ref="bar-manual")
        for profile in ("Main", "default ", "x" * 33):
            with self.subTest(profile=profile):
                if env.paths.kill_file.exists():
                    os.unlink(env.paths.kill_file)
                code, out, err = run_cli_main(env, ["--profile", profile, "kill", "--reason", "drill"])
                self.assertEqual(code, 3, err)
                result = json.loads(out)
                self.assertEqual((result["status"], result["kill"]["status"], result["kill_file_engaged"]),
                                 ("kill_incomplete_invalid_profile", "kill_engaged", True))
                self.assertTrue(kill_engaged(env.paths))
                self.assertIn("the KILL file is engaged; cancel open bets on the Betfair website or in Bet Angel now",
                              err)
                self.assertEqual(env.router.order_requests, [])
        self.assertEqual(sorted(path.name for path in env.paths.profiles_dir.iterdir()), ["default"])

    def test_kill_cancels_own_orders_false_is_a_config_error(self):
        """Amendment A3, D4: there is no kill_file_only kill any more. The key stays (older configs load) but
        must be true; false is a config error, and the kill ends kill_incomplete_config_unreadable, exit 3,
        with the KILL file engaged, the HALTs (kill and kill_incomplete) journaled, the config error named in
        the JSON and on stderr, and the website / Bet Angel advice."""
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        with self.assertRaises(ConfigError) as caught:
            gw.live_config(env.paths, {"orders": {"live": {"kill_cancels_own_orders": False}}})
        self.assertEqual(str(caught.exception), "orders.live.kill_cancels_own_orders_must_be_true")
        code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        self.assertEqual(code, 3, err)
        result = json.loads(out)
        self.assertEqual((result["status"], result["error"], result["journal_halt_kill_incomplete"]),
                         ("kill_incomplete_config_unreadable", "orders.live.kill_cancels_own_orders_must_be_true",
                          "appended"))
        self.assertIn("Betfair kill incomplete: kill_incomplete_config_unreadable "
                      "(orders.live.kill_cancels_own_orders_must_be_true); the KILL file is engaged; cancel open bets "
                      "on the Betfair website or in Bet Angel now", err)
        self.assertTrue(kill_engaged(env.paths))
        halts = env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"]
        self.assertIn("kill", halts)
        self.assertIn("kill_incomplete", halts)
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTABLE")          # no sweep without a config
        self.assertEqual(env.router.order_requests, [])

    def test_cli_kill_with_an_unreadable_journal_exits_3(self):
        """Round 4 (major): a live journal that cannot be opened at all ended the kill before any login, so our
        resting bets stayed live. It is now the extreme untrusted journal: the sweep still runs on the config
        strategy ref (the gateway holds an UnreadableJournal stand-in), nothing is written to or replaces the
        file, no anchor is written and the kill ends exit 3 kill_incomplete_journal_unreadable."""
        def garbage(path):
            path.write_bytes(b"not a database " * 100)

        def no_permission(path):
            os.chmod(path, 0)
            self.addCleanup(os.chmod, path, 0o600)

        def other_schema(path):
            db = sqlite3.connect(str(path))
            try:
                db.execute("PRAGMA user_version=99")
            finally:
                db.close()

        for name, damage in (("garbage", garbage), ("no_permission", no_permission), ("other_schema", other_schema)):
            with self.subTest(case=name):
                env = gw.build_live_gateway(self)
                ours = env.exchange.add_order(strategy_ref="bar-manual")
                env.journal.close()
                path = env.paths.journal_path("live")
                for suffix in ("-wal", "-shm"):
                    if os.path.exists(str(path) + suffix):
                        os.unlink(str(path) + suffix)
                damage(path)
                anchor = env.runner.items[(ANCHOR_SERVICE, "default")]
                mode = path.stat().st_mode & 0o777
                os.chmod(path, 0o600)
                damaged = path.read_bytes()
                os.chmod(path, mode)
                code, out, err = run_cli_main(env, ["kill", "--reason", "x"])
                self.assertEqual(code, 3, err)
                self.assertIn("kill_incomplete_journal_unreadable", err)
                self.assertIn("Betfair website or in Bet Angel", err)
                self.assertNotIn("Traceback", err)
                result = json.loads(out)
                self.assertEqual((result["status"], result["journal"]), ("kill_incomplete_journal_unreadable",
                                                                         "unreadable"))
                self.assertIn(gateway_module.UNTRUSTED_SWEEP_NOTE, result["notes"])
                self.assertTrue(kill_engaged(env.paths))
                self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")     # swept
                self.assertGreater(result["passes"], 1)
                self.assertEqual(env.runner.items[(ANCHOR_SERVICE, "default")], anchor)       # no anchor written
                os.chmod(path, 0o600)
                self.assertEqual(path.read_bytes(), damaged)                                  # never replaced

    def test_kill_after_a_refused_live_command_never_locks_live_mode(self):
        env = gw.build_live_gateway(self, roundtrip=False)          # no anchor, journal file empty
        env.journal.close()
        os.unlink(env.paths.journal_path("live"))
        place = ["place", "--market", "1.1", "--selection", "12345", "--side", "BACK", "--price", "3.50", "--size",
                 "2.00", "--mode", "live"]
        code, _, err = run_cli_main(env, place)                        # no token: refused by static arming
        self.assertEqual(code, 2, err)
        self.assertFalse(env.paths.journal_path("live").exists())     # nothing created before arming passed
        # An empty live journal left behind (as older code did) gets no CLI events without an anchor.
        OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock).close()
        env.runner.add_fails = True                                    # and the Keychain refuses the anchor write
        code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["journal_halt"], "live_journal_empty_gateway_journals_it")
        self.assertIn("Betfair warning: journal_anchor_write_failed", err)
        # The journal holds the kill and no order: it may take its first anchor, so resume works.
        env.runner.add_fails = False
        os.unlink(env.paths.kill_file)
        code, out, err = run_cli_main(env, ["resume", "--mode", "live", "--confirm-live", env.token])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["anchor_written"])
        code, _, err = run_cli_main(env, ["cancel", "--market", "1.1", "--bet-id", "300000000002", "--mode", "live"])
        self.assertNotIn("journal_anchor", err)
        code, _, err = run_cli_main(env, place + ["--confirm-live", env.token])
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: ref_roundtrip_unverified"))

    def test_resume_requires_file_removed_and_token(self):
        env = gw.build_live_gateway(self)
        engage(env.paths, "test", clock=env.clock)
        env.journal.append("kill", {"reason": "test", "scope": "own"})
        env.journal.append("halt", {"reason": "kill", "scope": "global"})
        with self.assertRaises(Halted) as caught:
            env.gateway.resume(confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "kill_file_still_present")
        os.unlink(env.paths.kill_file)
        with self.assertRaises(NotArmed) as caught:
            env.gateway.resume(confirm_token=None)
        self.assertEqual(caught.exception.rule, "confirm_token_mismatch")
        env.confirmer.lines = ["resume"]
        with self.assertRaises(OrderRefused):
            env.gateway.resume(confirm_token=env.token)
        self.assertTrue(env.journal.fold(now_ns=env.clock.time_ns())["halted"])
        env.confirmer.lines = None
        result = env.gateway.resume(confirm_token=env.token)
        self.assertEqual(result["status"], "resumed")
        self.assertFalse(result["halted"])
        self.assertEqual(env.confirmer.prompts[-1]["expected"], "RESUME LIVE")
        paper = gw.build_paper_gateway(self)
        engage(paper.paths, "test", clock=paper.clock)
        with self.assertRaises(Halted):
            paper.gateway.resume()
        os.unlink(paper.paths.kill_file)
        self.assertEqual(paper.gateway.resume()["status"], "resumed")

    def test_resume_never_clears_what_arrived_during_its_prompt(self):
        """Regression (audit): resume checked the kill file and the HALT before the 120 s RESUME LIVE
        prompt and then appended a global resume with no lock and no re-check, so a kill (and any halt)
        journaled while the prompt was open was cleared too, and removing KILL by hand re-armed live."""
        env = gw.build_live_gateway(self)
        env.journal.append("halt", {"reason": "old_reason_operator_reviewed", "scope": "global"})

        def other_process():
            other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
            try:
                other.append("halt", {"reason": "terminal_account", "scope": "global"})
                engage(env.paths, "operator kill during resume prompt", clock=env.clock)
                other.append("kill", {"reason": "operator kill during resume prompt", "scope": "own"})
                other.append("halt", {"reason": "kill", "scope": "global"})
            finally:
                other.close()

        class Racing(gw.FakeConfirmer):
            def confirm_line(self, expected, *, banner):
                if expected == "RESUME LIVE":
                    other_process()
                return super().confirm_line(expected, banner=banner)

        env.gateway.confirmer = racing = Racing()
        with self.assertRaises(Halted) as caught:
            env.gateway.resume(confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "kill_engaged_during_resume_prompt")
        self.assertIn("old_reason_operator_reviewed", racing.prompts[-1]["banner"])     # shown before the phrase
        state = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertTrue(state["halted"])
        self.assertEqual(state["halt_reasons"], ["old_reason_operator_reviewed", "terminal_account", "kill", "kill"])
        self.assertNotIn("resume", [e["kind"] for e in env.journal.events()])
        # The operator removes KILL by hand: live placing stays refused until a resume of what they saw.
        os.unlink(env.paths.kill_file)
        env.gateway.confirmer = gw.FakeConfirmer()
        env.gateway.preconfirm("place", market_id="1.1", instructions=[gw.instruction()])
        with self.assertRaises(NotArmed) as caught:
            env.gateway.open(command="place", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "halted")
        self.assertEqual(env.router.order_requests, [])

    def test_resume_refused_when_the_journal_moved_during_its_prompt(self):
        env = gw.build_live_gateway(self)
        env.journal.append("halt", {"reason": "seen", "scope": "global"})

        class Racing(gw.FakeConfirmer):
            def confirm_line(self, expected, *, banner):
                other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
                try:
                    other.append("halt", {"reason": "unattributed_candidate_order", "scope": "global"})
                finally:
                    other.close()
                return super().confirm_line(expected, banner=banner)

        env.gateway.confirmer = Racing()
        with self.assertRaises(Halted) as caught:
            env.gateway.resume(confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "journal_changed_during_resume_prompt")
        self.assertEqual(env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"],
                         ["seen", "unattributed_candidate_order"])
        # A resume taken while nothing moves clears exactly what it showed.
        env.gateway.confirmer = gw.FakeConfirmer()
        result = env.gateway.resume(confirm_token=env.token)
        self.assertEqual((result["status"], result["halted"]), ("resumed", False))
        self.assertEqual(result["cleared_halt_reasons"], ["seen", "unattributed_candidate_order"])
        resume = [e for e in env.journal.events() if e["kind"] == "resume"][-1]
        self.assertEqual(resume["payload"], {"scope": "global", "cleared_through_seq": resume["seq"] - 1})
        # The resume runs under the global lock: with another gateway holding it, it waits, then refuses.
        env.journal.append("halt", {"reason": "again", "scope": "global"})
        with file_lock(env.paths.gateway_lock):
            with self.assertRaises(Exception) as busy:
                env.gateway.resume(confirm_token=env.token)
        self.assertEqual(type(busy.exception).__name__, "LockBusy")
        self.assertTrue(env.journal.fold(now_ns=env.clock.time_ns())["halted"])

    def test_halt_persists_restart(self):
        env = gw.build_live_gateway(self)
        env.gateway.open(command="kill")
        result = env.gateway.kill(scope="own", reason="stop now")
        self.assertEqual(result["status"], "kill_complete")
        env.gateway.close()
        env.journal.close()
        reopened = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        self.addCleanup(reopened.close)
        state = reopened.fold(now_ns=env.clock.time_ns())
        self.assertTrue(state["halted"])
        self.assertIn("kill", state["halt_reasons"])
        os.unlink(env.paths.kill_file)
        guard = env.guard
        decision = guard.evaluate_place("1.1", [gw.instruction()], book=orders.live_book(env.clock), state=state,
                                        arming={"armed": True, "failures": []}, now_ns=env.clock.time_ns(),
                                        customer_ref=orders.CREF, customer_order_refs=[orders.REF])
        self.assertFalse(decision["approved"])
        self.assertIn("journal_halted", [f["reason"] for f in decision["detail"]["failures"]])


class ScaledClock:
    """A real clock scaled down (1 clock second = SCALE real seconds), shared by threads, so a kill and a
    lock holder really run at once (as in the round-3 audit's concurrent repro)."""

    SCALE = 0.01

    def __init__(self):
        self._wall0 = parse_utc_ns(support.BASE_UTC)
        self._mono0 = 5_000_000_000
        self._real0 = time.monotonic_ns()
        self._stopped = None

    def _elapsed(self):
        if self._stopped is not None:
            return self._stopped
        return int((time.monotonic_ns() - self._real0) / self.SCALE)

    @contextlib.contextmanager
    def stopped(self):
        """Hold the clock still for a set-up step, then go on from where it stopped. At this scale a book is
        2 clock seconds old after 20 ms of real time, so a set-up place on a loaded machine could otherwise
        miss its send window (amendment A3, C2) for reasons that have nothing to do with the test."""
        self._stopped = self._elapsed()
        try:
            yield
        finally:
            self._real0 = time.monotonic_ns() - int(self._stopped * self.SCALE)
            self._stopped = None

    def time_ns(self):
        return self._wall0 + self._elapsed()

    def monotonic_ns(self):
        return self._mono0 + self._elapsed()

    def sleep(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError("sleep seconds must be a non-negative number")
        time.sleep(seconds * self.SCALE)

    def utc_now(self):
        return utc_from_ns(self.time_ns())


def place_names(env):
    return [r for r in env.router.order_requests if r["method"].endswith(gw.PLACE_SUFFIX)]


class LockFreeKillTests(unittest.TestCase):
    """Amendment A3, D4: the kill never takes or waits for .gateway.lock, journals no order moves, re-sweeps from
    the KILL file, and every way it can end short of kill_complete is exit 3 with the website / Bet Angel advice."""

    def setUp(self):
        support.block_network(self)

    def place_resting(self, env, **changes):
        """A bet placed earlier through the live gateway, resting EXECUTABLE (journal and exchange)."""
        ins = gw.instruction(**changes)
        gw.open_live(self, env, "place", instructions=[ins])
        result = env.gateway.place("1.1", [ins], source="cli_manual", confirm_token=env.token)
        env.gateway.close()
        self.assertEqual(result["orders"][0]["state"], "EXECUTABLE")
        return result["orders"][0]

    def test_kill_never_takes_the_gateway_lock(self):
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        with file_lock(env.paths.gateway_lock) as held:                 # a holder keeps the lock all along
            started = env.clock.mono_ns
            with patch.object(gateway_module, "file_lock", side_effect=AssertionError("kill used .gateway.lock")):
                code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
            self.assertTrue(held.held())
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(env.clock.mono_ns - started, 35 * 10**9)        # the re-sweeps, never a lock wait

    def test_kill_sweeps_while_anchor_publication_is_blocked(self):
        """Re-review R1: Keychain anchor publications are serialized under paths.anchor_lock, which the kill must
        never wait for before its sweep. Another publisher holds that lock all along (say, a Keychain dialog
        nobody answers): the kill writes no anchor before or during its sweep, cancels our bet and completes;
        only after the sweep does it wait, at most killswitch.JOURNAL_BUSY_TIMEOUT_S, and then it writes no
        anchor and only warns (exit 0: every cancel was sent; the anchor stays behind the journal head)."""
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        before = env.runner.items[(ANCHOR_SERVICE, "default")]
        calls_before = len(env.runner.calls)
        cancelled_at = []
        cancel_one = env.exchange._cancel_one

        def timed_cancel(bet_id, reduction=None):
            cancelled_at.append(env.clock.mono_ns)
            return cancel_one(bet_id, reduction)

        with file_lock(env.paths.anchor_lock) as held, patch.object(env.exchange, "_cancel_one", timed_cancel):
            started = env.clock.mono_ns
            code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
            self.assertTrue(held.held())
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(cancelled_at, [started])                   # the first pass cancels at once: no wait first
        self.assertIn("Betfair warning: journal_anchor_publish_busy", err)
        self.assertEqual(env.runner.items[(ANCHOR_SERVICE, "default")], before)          # no anchor written
        self.assertFalse(any(call["argv"][1] == "add-generic-password" for call in env.runner.calls[calls_before:]))
        # The re-sweeps, then the one bounded wait for the anchor lock after the sweep.
        self.assertEqual(env.clock.mono_ns - started, (35 + killswitch.JOURNAL_BUSY_TIMEOUT_S) * 10**9)
        self.assertLess(anchor_read(env.paths.profile, runner=env.runner)["seq"], env.journal.anchor()["seq"])
        # The lock free again, the next publication catches the anchor up (A13 accepts the lag meanwhile).
        report = env.gateway.open(command="cancel")
        self.addCleanup(env.gateway.close)
        self.assertNotIn("journal_anchor_write_failed", report["notes"])
        self.assertEqual(anchor_read(env.paths.profile, runner=env.runner)["seq"], env.journal.anchor()["seq"])

    def test_kill_publishes_its_anchor_only_after_the_sweep(self):
        """Re-review R1: the kill's one anchor publication comes after its whole sweep (the 35 s re-sweep and the
        final check), so no Keychain write or wait for another publisher delays a cancel; it then anchors the
        journal head, the kill's own events included."""
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        at_anchor_write = []

        def runner(argv, **kwargs):
            if argv[1] == "add-generic-password" and ANCHOR_SERVICE in argv:
                cancels = [r for r in env.router.order_requests if r["method"].endswith(gw.CANCEL_SUFFIX)]
                at_anchor_write.append((len(cancels), env.clock.mono_ns))
            return env.runner(argv, **kwargs)

        started = env.clock.mono_ns
        code, out, err = run_cli_main(SimpleEnv(env, runner=runner), ["kill", "--reason", "drill"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(len(at_anchor_write), 1)                       # one publication, none before the sweep
        cancels, when = at_anchor_write[0]
        self.assertEqual(cancels, 1)
        self.assertGreaterEqual(when - started, killswitch.RESWEEP_OFFSETS_S[-1] * 10**9)
        head = env.journal.anchor()
        anchor = anchor_read(env.paths.profile, runner=env.runner)
        self.assertEqual((anchor["seq"], anchor["last_sha256"]), (head["seq"], head["last_sha256"]))
        self.assertIn("kill", [event["kind"] for event in env.journal.events()])

    def test_kill_never_anchors_a_journal_rolled_back_during_its_sweep(self):
        """Regression (re-review of R1, rollback under a live session): the journal restored in place to an older
        copy during the kill's sweep (sqlite3 ".restore", which the kill's open connection then reads; its own
        events then fork the restored chain) was published by the kill's close(), so the Keychain anchor no
        longer named the journal's real history and the rolled-back journal (the earlier HALT gone) passed A13.
        The publication now writes only a head that extends the Keychain anchor: the kill still cancels our bet
        and only warns (journal_anchor_not_extended), the anchor is left as it was, and A13 refuses the
        rolled-back journal."""
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        live = env.paths.journal_path("live")
        backup = env.paths.orders_dir / "journal-before-halt.sqlite"

        def copy(source, target):
            src, dst = sqlite3.connect(str(source)), sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()

        copy(live, backup)                                              # seq 1
        env.journal.append("halt", {"reason": "earlier_safety_history", "scope": "global"})
        env.journal.append("note", {"reason": "earlier_history"})
        anchor_write(env.paths.profile, dict(env.journal.anchor(), updated_at=env.clock.utc_now()), runner=env.runner)
        before = env.runner.items[(ANCHOR_SERVICE, "default")]
        cancel_one = env.exchange._cancel_one
        restored = []

        def restore_then_cancel(bet_id, reduction=None):
            if not restored:
                restored.append(True)
                copy(backup, live)
            return cancel_one(bet_id, reduction)

        with patch.object(env.exchange, "_cancel_one", restore_then_cancel):
            code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        self.assertEqual(restored, [True])
        self.assertEqual(env.runner.items[(ANCHOR_SERVICE, "default")], before)         # the anchor stayed
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")     # the sweep still ran
        self.assertIn("Betfair warning: journal_anchor_not_extended", err)
        self.assertIn("the anchor was left ahead, so the next live command refuses (journal_anchor_mismatch)", err)
        with OrderJournal(live, mode="live", clock=env.clock) as journal:
            state = journal.fold(now_ns=env.clock.time_ns())
            self.assertNotIn("earlier_safety_history", state["halt_reasons"])
            self.assertEqual(arming.anchor_failures(journal.anchor(), anchor_read(env.paths.profile, runner=env.runner),
                                                    journal=journal, journal_state=state), ["journal_anchor_mismatch"])

    def test_kill_completes_its_sweep_while_a_flaky_holder_keeps_the_lock(self):
        """Regression R3#0 (round-3 major): a live place holding .gateway.lock on a flaky link (every read's
        first attempt times out, the retry works) kept the lock longer than the kill waited, so the kill
        exited kill_incomplete_no_session and its sweep never ran. Now the kill, in its own thread with its
        own gateway, session and journal connection, never waits: it sweeps while the holder still holds the
        lock, the earlier resting bet is cancelled, the holder's place is refused and no place request is
        sent after the KILL file."""
        clock = ScaledClock()
        env = gw.build_live_gateway(self, clock=clock)
        with clock.stopped():
            resting = self.place_resting(env)["bet_id"]
        holder_thread = threading.current_thread()
        kill_pressed, kill_engaged_event, kill_done = threading.Event(), threading.Event(), threading.Event()
        flaky = {"fail_next": True}
        original = env.transport.request
        lock_held_during_sweep = []

        def request(method, url, headers, body, *, deadline_s, idempotent, **kwargs):
            if threading.current_thread() is holder_thread and idempotent and body and b"jsonrpc" in body:
                if not kill_pressed.is_set():
                    kill_pressed.set()                 # the operator sees place hanging and runs kill
                    kill_engaged_event.wait(30)        # ... whose KILL file exists before the holder goes on
                if flaky["fail_next"]:
                    flaky["fail_next"] = False
                    clock.sleep(deadline_s)            # the attempt times out after its whole deadline
                    raise TransportError("deadline_exceeded", phase="receive", may_have_arrived=True)
                flaky["fail_next"] = True
            return original(method, url, headers, body, deadline_s=deadline_s, idempotent=idempotent, **kwargs)

        env.transport.request = request
        result = {}

        def run_kill():
            """cmd_kill's steps on their own connections: KILL file, T0, kill + halt, open(kill), sweep."""
            journal = None
            try:
                kill_pressed.wait(30)
                engage(env.paths, "operator kill", clock=clock)
                result["t0"] = clock.monotonic_ns()
                kill_engaged_event.set()
                journal = OrderJournal(env.paths.journal_path("live"), mode="live", clock=clock)
                journal.append("kill", {"reason": "operator kill", "scope": "own"})
                journal.append("halt", {"reason": "kill", "scope": "global"})
                transport = gw.RoutingTransport(env.router, clock=clock)
                credentials = load_credentials(env.config, purpose="live_orders", profile=env.paths.profile,
                                               runner=env.runner)
                session = SessionManager(env.config, credentials, paths=env.paths, transport=transport, clock=clock)
                rpc = JsonRpcClient(env.config, session, clock=clock)
                killer = OrderGateway(env.config, paths=env.paths, mode="live", journal=journal,
                                      guard=RiskGuard(env.config, mode="live", clock=clock, paths=env.paths),
                                      rpc=rpc, read=ReadClient(rpc, paths=env.paths, clock=clock),
                                      confirmer=gw.FakeConfirmer(), clock=clock, env={}, keychain_runner=env.runner)
                killer.open(command="kill")
                lock_held_during_sweep.append(not _lock_free(env.paths.gateway_lock))
                out = killer.kill(scope="own", reason="operator kill", engaged_monotonic_ns=result["t0"])
                killer.close()
                result.update(status=out["status"], done=clock.monotonic_ns())
            except BaseException as exc:          # reported to the main thread's assertions
                result["error"] = repr(exc)
            finally:
                if journal is not None:
                    journal.close()
                kill_done.set()

        thread = threading.Thread(target=run_kill, name="kill")
        thread.start()
        ins = gw.instruction(price_h=360)
        holder = None
        try:
            env.gateway.preconfirm("place", market_id="1.1", instructions=[ins])
            env.gateway.open(command="place", confirm_token=env.token)
            holder = env.gateway.place("1.1", [ins], source="cli_manual", confirm_token=env.token)
            kill_done.wait(60)                         # the holder still holds the lock while the kill ends
        finally:
            env.gateway.close()
            thread.join(60)
        self.assertNotIn("error", result)
        self.assertEqual(result["status"], "kill_complete")
        self.assertEqual(lock_held_during_sweep, [True])
        self.assertLess(result["done"] - result["t0"], (killswitch.RESWEEP_OFFSETS_S[-1] + 25) * 10**9)
        self.assertEqual(env.exchange.orders[resting]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(holder["decision"], "refused")
        self.assertIn(holder["refusal"]["rule"], ("halted", "journal_changed_since_decision"))
        self.assertEqual(len(place_names(env)), 1)          # the earlier place only; nothing after the KILL file

    def test_kill_sweeps_when_a_holder_reanchors_between_reads(self):
        """Regression R3#0, second route: a holder that journals and re-anchors the Keychain between the kill's
        two A13 reads made the kill's static A13 fail (journal_anchor_mismatch), so no sweep ran. The kill's
        static A13 now reads no anchor, and its one anchor check reads the Keychain first and the journal head
        after it: no mismatch, the sweep runs, exit 0."""
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        raced = []

        def racing_runner(argv, **kwargs):
            answer = env.runner(argv, **kwargs)
            if argv[1] == "find-generic-password" and ANCHOR_SERVICE in argv and "-w" in argv and not raced:
                raced.append(1)
                other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
                try:
                    other.append("note", {"reason": "holder_report"})
                    anchor_write(env.paths.profile, dict(other.anchor(), updated_at=env.clock.utc_now()),
                                 runner=env.runner)
                finally:
                    other.close()
            return answer

        code, out, err = run_cli_main(SimpleEnv(env, runner=racing_runner), ["kill", "--reason", "drill"])
        self.assertEqual(raced, [1])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "kill_complete")
        self.assertNotIn("journal_anchor_mismatch", json.dumps(result))
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        anchor = anchor_read(env.paths.profile, runner=env.runner)
        self.assertEqual(anchor["seq"], env.journal.anchor()["seq"])          # trusted: the anchor follows

    def test_kill_with_an_untrusted_journal_sweeps_the_config_ref_and_exits_3(self):
        """An unfoldable journal, a broken chain or a Keychain anchor ahead of the journal never stops the
        sweep, but the journal cannot list all our bets: the sweep uses the config strategy ref only, the
        journal gets a note when writable, no Keychain anchor is written and the kill ends exit 3."""

        def unfoldable(env):
            db = sqlite3.connect(str(env.paths.journal_path("live")), isolation_level=None)
            try:
                head = db.execute("SELECT seq, event_sha256 FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                payload = {"transitions": [{"customer_order_ref": "bro200101120000-00000000000000ff",
                                            "state": "UNKNOWN"}]}
                event = {"seq": head[0] + 1, "event_id": "ev-" + "0" * 32, "recorded_at": env.clock.utc_now(),
                         "monotonic_ns": env.clock.mono_ns, "kind": "note", "mode": "live", "intent_id": None,
                         "customer_order_ref": None, "customer_ref": None, "bet_id": None, "market_id": None,
                         "state_after": None, "payload": payload, "prev_sha256": head[1]}
                from betangel_research.betfair import journal as journal_module
                from betangel_research.betfair.receipts import canonical_json
                event["event_sha256"] = journal_module._event_sha256(event)
                db.execute(journal_module._INSERT_SQL, (event["seq"], event["event_id"], event["recorded_at"],
                                                        event["monotonic_ns"], "note", "live", None, None, None,
                                                        None, None, None, canonical_json(payload).decode(), head[1],
                                                        event["event_sha256"]))
            finally:
                db.close()
            anchor_write(env.paths.profile, dict(env.journal.anchor(), updated_at=env.clock.utc_now()),
                         runner=env.runner)

        def broken_chain(env):
            env.journal.append("note", {"reason": "seed"})
            anchor_write(env.paths.profile, dict(env.journal.anchor(), updated_at=env.clock.utc_now()),
                         runner=env.runner)
            db = sqlite3.connect(str(env.paths.journal_path("live")), isolation_level=None)
            try:
                trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='events_no_update'").fetchone()[0]
                db.execute("DROP TRIGGER events_no_update")
                db.execute("UPDATE events SET payload_json='{\"reason\":\"edited\"}' WHERE kind='note'")
                db.execute(trigger)                    # the schema still opens; only the hash chain is broken
            finally:
                db.close()
            self.assertEqual(env.journal.verify()["status"], "broken")

        def anchor_ahead(env):
            head = env.journal.anchor()
            anchor_write(env.paths.profile, dict(head, seq=head["seq"] + 5, last_sha256="e" * 64,
                                                 updated_at=env.clock.utc_now()), runner=env.runner)

        def edited_row(column, value):
            """One committed note row edited in place (the trigger dropped and restored): a byte flip in a TEXT
            cell, which SQLite has no checksum for, or a hand edit."""
            def damage(env):
                env.journal.append("note", {"reason": "seed"})
                anchor_write(env.paths.profile, dict(env.journal.anchor(), updated_at=env.clock.utc_now()),
                             runner=env.runner)
                db = sqlite3.connect(str(env.paths.journal_path("live")), isolation_level=None)
                try:
                    trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='events_no_update'").fetchone()[0]
                    db.execute("DROP TRIGGER events_no_update")
                    if value is None:          # an event_id that is not UTF-8: reading the row raises sqlite3.Error
                        db.execute(f"UPDATE events SET {column}=CAST(x'65762dff' AS TEXT) WHERE kind='note'")
                    else:
                        db.execute(f"UPDATE events SET {column}=? WHERE kind='note'", (value,))
                    db.execute(trigger)
                finally:
                    db.close()
            return damage

        def note_reasons(env):
            """The reasons of the journal's readable note payloads, read past any damaged row."""
            db = sqlite3.connect(str(env.paths.journal_path("live")))
            try:
                rows = db.execute("SELECT payload_json FROM events WHERE kind='note'").fetchall()
            finally:
                db.close()
            reasons = []
            for (text,) in rows:
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    reasons.append(payload.get("reason"))
            return reasons

        # Round 4: a row that is not JSON (or not an object) and a row SQLite cannot decode stopped the kill before
        # its sweep (the kill caught JournalError only): our bet stayed EXECUTABLE.
        for name, damage in (("unfoldable", unfoldable), ("broken_chain", broken_chain),
                             ("anchor_ahead", anchor_ahead),
                             ("invalid_json_row", edited_row("payload_json", '{"reason":"edi')),
                             ("payload_not_an_object", edited_row("payload_json", "[]")),
                             ("sqlite_error_on_read", edited_row("event_id", None))):
            with self.subTest(case=name):
                env = gw.build_live_gateway(self)
                journal_only = self.place_resting(env) if name == "anchor_ahead" else None
                if journal_only is not None:
                    # Known to the journal only: the exchange no longer shows our strategy ref on it.
                    env.exchange.orders[journal_only["bet_id"]]["customerStrategyRef"] = None
                ours = env.exchange.add_order(strategy_ref="bar-manual")
                damage(env)
                before = env.runner.items[(ANCHOR_SERVICE, "default")]
                code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
                self.assertEqual(code, 3, err)
                result = json.loads(out)
                self.assertEqual(result["status"], "kill_incomplete")
                self.assertIn(gateway_module.UNTRUSTED_SWEEP_NOTE, result["notes"])
                self.assertIn("cancel open bets on the Betfair website or in Bet Angel now", err)
                self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
                self.assertEqual(env.runner.items[(ANCHOR_SERVICE, "default")], before)     # no anchor written
                if name == "sqlite_error_on_read":
                    # Every append reads the committed rows first, so none fits; the cancels went out regardless.
                    self.assertIn("kill_journal_write_failed", result["notes"])
                else:
                    self.assertIn(gateway_module.UNTRUSTED_SWEEP_NOTE, note_reasons(env))
                if journal_only is not None:
                    self.assertEqual(env.exchange.orders[journal_only["bet_id"]]["status"], "EXECUTABLE")
                    self.assertIn("journal_anchor_mismatch", result["notes"])

    def test_kill_journals_no_order_moves(self):
        """The kill's cancels run on a no-orders stand-in: cancel_intent, submit and report carry no order
        moves, so the journal keeps counting our bet at its worst case (amendment A3, A6) until an order
        report, reconcile --accept or settle-market says otherwise."""
        env = gw.build_live_gateway(self)
        order = self.place_resting(env)
        exposure = env.gateway.status()["exposure_pence_total"]
        self.assertGreater(exposure, 0)
        before = env.journal.anchor()["seq"]
        gw.open_live(self, env, "kill")
        out = env.gateway.kill(scope="own", reason="drill")
        self.assertEqual(out["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[order["bet_id"]]["status"], "EXECUTION_COMPLETE")
        events = list(env.journal.events(since_seq=before))
        kinds = [e["kind"] for e in events]
        self.assertIn("cancel_intent", kinds)
        self.assertIn("submit", kinds)
        self.assertIn("report", kinds)
        for event in events:
            self.assertEqual(event["payload"].get("transitions", []), [], event["kind"])
            self.assertIsNone(event["customer_order_ref"])
        state = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertEqual(state["orders"][order["customer_order_ref"]]["state"], "EXECUTABLE")
        self.assertEqual(env.gateway.status()["exposure_pence_total"], exposure)

    def test_kill_cancel_is_sent_over_a_slow_reconnect(self):
        """A kill cancel carries no send-by and no gate, and its capability is minted right before the send,
        after the submit append: an 8 s reconnect or a slow journal never expires it."""
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        gw.open_live(self, env, "kill")
        env.transport.connect_delay_s = 8
        original = env.journal.append

        def slow_submit(kind, payload, **columns):
            event = original(kind, payload, **columns)
            if kind == "submit":
                env.clock.advance(8)           # a whole-journal fold behind another connection's write
            return event

        with patch.object(env.journal, "append", side_effect=slow_submit):
            out = env.gateway.kill(scope="own", reason="drill")
        self.assertEqual(out["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        cancel = [r for r in env.transport.requests if r["order_digest"]]
        self.assertEqual(len(cancel), 1)
        self.assertEqual((cancel[0]["send_by_monotonic_ns"], cancel[0]["send_gate"]), (None, False))

    def test_kill_writes_no_account_registry(self):
        """The kill has no A15 and takes no lock: an unlocked read-modify-write of accounts.json could lose
        another profile's registration (F76), so the kill writes neither the registry nor the journal's
        account fingerprint."""
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        with patch.object(gateway_module, "register_account", side_effect=AssertionError("kill wrote accounts.json")), \
                patch.object(env.journal, "set_account_fingerprint",
                             side_effect=AssertionError("kill wrote the journal fingerprint")):
            report = env.gateway.open(command="kill")
            self.addCleanup(env.gateway.close)
            self.assertTrue(report["armed"], report)
            out = env.gateway.kill(scope="own", reason="drill")
        self.assertEqual(out["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        self.assertFalse(env.paths.accounts_file.exists())
        self.assertIsNone(env.journal.account_fingerprint())

    def test_signal_during_kill_is_exit_3_with_advice(self):
        """Regression R3#17: SIGTERM or SIGINT during a kill (here during the login and during a re-sweep wait)
        or a KeyboardInterrupt before the gateway (in load_config) used to exit 4 with no JSON and no advice.
        Now it is kill_incomplete_interrupted: JSON, exit 3, the website / Bet Angel advice, a HALT
        kill_incomplete in the journal, and the process's own signal handlers are restored."""
        def safety(signum, frame):            # never let a stray SIGTERM or SIGHUP end the test run
            return None

        previous = signal.signal(signal.SIGTERM, safety)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        self.addCleanup(signal.signal, signal.SIGHUP, signal.signal(signal.SIGHUP, safety))
        sigint_before = signal.getsignal(signal.SIGINT)

        def during_login(env, signum=signal.SIGTERM):
            router = env.transport.router

            def routed(record):
                if record["url"].endswith("/api/login"):
                    os.kill(os.getpid(), signum)
                return router(record)

            env.transport.router = routed
            return contextlib.nullcontext()

        def sighup_during_login(env):           # a closed terminal (R3#12/R3#13): handled like SIGTERM
            return during_login(env, signal.SIGHUP)

        def during_resweep_wait(env):
            sleep = env.clock.sleep

            def interrupted(seconds):
                os.kill(os.getpid(), signal.SIGINT)
                sleep(seconds)

            return patch.object(env.clock, "sleep", side_effect=interrupted)

        def in_load_config(env):
            return patch("betangel_research.betfair.config.load_config", side_effect=KeyboardInterrupt)

        for name, setup, cancelled in (("login", during_login, False), ("resweep", during_resweep_wait, True),
                                       ("load_config", in_load_config, False),
                                       ("sighup_login", sighup_during_login, False)):
            with self.subTest(case=name):
                env = gw.build_live_gateway(self)
                ours = env.exchange.add_order(strategy_ref="bar-manual")
                with setup(env):
                    code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
                self.assertEqual(code, 3, err)
                self.assertNotIn("Betfair stopped", err)
                self.assertIn("Betfair kill incomplete: kill_incomplete_interrupted; the KILL file is engaged; "
                              "cancel open bets on the Betfair website or in Bet Angel now", err)
                result = json.loads(out)
                self.assertEqual(result["status"], "kill_incomplete_interrupted")
                self.assertEqual(result["journal_halt_kill_incomplete"], "appended")
                self.assertTrue(kill_engaged(env.paths))
                self.assertIn("kill_incomplete", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])
                self.assertEqual(env.exchange.orders[ours]["status"],
                                 "EXECUTION_COMPLETE" if cancelled else "EXECUTABLE")
                self.assertIs(signal.getsignal(signal.SIGTERM), safety)
                self.assertIs(signal.getsignal(signal.SIGHUP), safety)
                self.assertIs(signal.getsignal(signal.SIGINT), sigint_before)

    def test_kill_prints_progress_advice_and_f67_only_when_unverified(self):
        """Regression R3#17 (silent waits): one stderr line per phase, the advice at the start, and the F67
        warning only while Bet Angel coexistence is unverified; the JSON names the session the gateway logged in
        with, the bets the kill asked Betfair to cancel, and why status still shows them as EXECUTABLE."""
        for coexistence in ("unverified", "not_running_on_account"):
            with self.subTest(coexistence=coexistence):
                env = gw.build_live_gateway(self, overrides={"coexistence": {"bet_angel": coexistence}})
                bet = env.exchange.add_order(strategy_ref="bar-manual")
                code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
                self.assertEqual(code, 0, err)
                result = json.loads(out)
                self.assertEqual(result["session"], {"logged_in": True, "login": "emergency",
                                                     "coexistence": coexistence})
                self.assertEqual(result["cancel_requested_bet_ids"], [bet])
                lines = [line for line in err.splitlines() if line.startswith("Betfair kill")]
                expected = ['Betfair kill: KILL file engaged; no gateway sends a new order from now on. If this kill '
                            'does not end with "kill complete", cancel open bets on the Betfair website or in Bet '
                            'Angel.',
                            "Betfair kill: reading credentials from the Keychain; if macOS asks, click Allow (not "
                            "Always Allow)",
                            "Betfair kill: logging in (emergency session)"]
                if coexistence == "unverified":
                    expected.append("Betfair kill: F67 (inferred, unverified): if a new login ends other sessions, "
                                    "Bet Angel and any running gateway may be logged out; check Bet Angel after the "
                                    "kill.")
                expected += ["Betfair kill: pass 1 of 3; next pass in 20 s (orders sent just before the kill can "
                             "still appear; bound 35 s after the KILL file)",
                             "Betfair kill: pass 2 of 3; next pass in 15 s (orders sent just before the kill can "
                             "still appear; bound 35 s after the KILL file)",
                             "Betfair kill: final check",
                             # Only what the final check shows: no claim about what orders-current keeps listing
                             # (unverified, F77), which round 4 found stated as fact here.
                             "Betfair kill complete: none of our bets has an unmatched (EXECUTABLE) part on the final "
                             "check. A request delayed in the network can still arrive later: if an order was being "
                             "sent when you killed, check Bet Angel or the website.",
                             "Betfair kill: cancels were requested for 1 bet(s) of ours (cancel_requested_bet_ids "
                             "above); the kill moves no order in the journal, so status still shows those it knows as "
                             "EXECUTABLE, counted at worst case, until you settle their market with settle-market"
                             + (" --bet-angel-paused" if coexistence == "unverified" else "")
                             + " after it closes; none has an unmatched part left, so do not cancel them again"]
                self.assertEqual(lines, expected)

    def test_kill_login_refused_by_the_ledger_names_login_reset(self):
        """The exit-3 line says what the operator can do about each login-ledger refusal (round 4): only the
        failure cap is cleared by login-reset; a ban is not (its end is named); an unreadable ledger (a line torn
        by a crash) is repaired by login-reset, which starts a 20-minute ban that also blocks the kill."""
        from betangel_research.betfair.auth import SessionManager as Manager
        from betangel_research.betfair.errors import AuthError, LoginBanned
        cases = (("failures", AuthError("login_failures_need_operator"),
                  "the login ledger refused (login_failures_need_operator): check why logins failed, then "
                  "login-reset"),
                 ("ban", LoginBanned("2026-10-10T13:00:00Z"),
                  "the login ledger refused (login_banned): logins, the kill's included, are banned until "
                  "2026-10-10T13:00:00Z; login-reset does not lift a ban"),
                 ("torn_line", None,
                  "the login ledger is unreadable (login_ledger_unreadable; a line torn by a crash?): login-reset "
                  "repairs it but starts a 20-minute login ban that also blocks the kill's login"))
        for name, refusal, advice in cases:
            with self.subTest(case=name):
                env = gw.build_live_gateway(self)
                ours = env.exchange.add_order(strategy_ref="bar-manual")
                if refusal is None:
                    env.paths.login_ledger.parent.mkdir(parents=True, exist_ok=True)
                    with open(env.paths.login_ledger, "ab") as ledger:
                        ledger.write(b'{"kind": "login_attempt", "attempt_id": "ab')          # torn by a crash
                    code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
                else:
                    with patch.object(Manager, "_reserve", side_effect=refusal):
                        code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
                self.assertEqual(code, 3, err)
                self.assertEqual(json.loads(out)["status"], "kill_incomplete_no_session")
                self.assertIn(f"Betfair kill incomplete: kill_incomplete_no_session; {advice}; the KILL file is "
                              f"engaged; cancel open bets on the Betfair website or in Bet Angel now", err)
                self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTABLE")          # no session, no sweep
        # login-reset's banner says what a repair does to the kill.
        env = gw.build_live_gateway(self)
        code, out, err = run_cli_main(env, ["login-reset"])
        self.assertEqual(code, 0, err)
        self.assertIn("A login ban is not lifted", env.confirmer.prompts[-1]["banner"])
        self.assertIn("20-minute login ban that blocks every login, the kill's emergency login included",
                      env.confirmer.prompts[-1]["banner"])
        # Any other login failure keeps the plain line.
        env = gw.build_live_gateway(self)
        env.router.login_status = "FAIL"
        code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        self.assertEqual(code, 3, err)
        self.assertNotIn("login ledger", err)


    def test_kill_never_waits_for_a_locked_journal(self):
        """Regression (live-money review): another connection holding the live journal's write lock (a holder
        stopped inside journal.append, or a slow whole-journal fold) made the kill's kill/halt appends wait the
        30 s busy timeout, raise sqlite3.OperationalError and end kill_incomplete_journal_unreadable with no
        login and no cancel. Opening an existing journal now reads only, the kill's connections wait at most
        killswitch.JOURNAL_BUSY_TIMEOUT_S, a write they cannot make is skipped, and the sweep cancels our bet;
        the kill still ends exit 3 because its journal record is incomplete."""
        env = gw.build_live_gateway(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        holder = sqlite3.connect(str(env.paths.journal_path("live")), isolation_level=None, timeout=1)
        holder.execute("BEGIN IMMEDIATE")                       # a writer stuck inside its append transaction
        started = time.monotonic()
        try:
            code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        elapsed = time.monotonic() - started
        self.assertEqual(code, 3, err)
        result = json.loads(out)
        self.assertEqual((result["status"], result["journal_halt"], result["journal_write_failed"]),
                         ("kill_incomplete", "failed:OperationalError", "OperationalError"))
        self.assertIn("kill_journal_write_failed", result["notes"])
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(result["passes"], 3)
        self.assertIn("cancel open bets on the Betfair website or in Bet Angel now", err)
        # Three short waits at most (the CLI's append, the gateway's first, the HALT retry); the old code waited
        # 30 s before giving up without a sweep.
        self.assertLess(elapsed, 3 * killswitch.JOURNAL_BUSY_TIMEOUT_S + 15)
        # Once the writer is gone the journal is whole and the KILL file still blocks every send.
        self.assertEqual(env.journal.verify()["status"], "ok")
        self.assertTrue(kill_engaged(env.paths))

    def test_kill_on_another_account_than_the_journal_is_incomplete(self):
        """Regression (live-money review): the lock-free kill never compared the account it logged into with the
        journal's. With the profile's Keychain login now on another Betfair account, both legs came back empty
        and the kill exited 0 ("none of our bets is current") while the journal's bets stayed live on their
        account. The kill compares fingerprints read-only (no registry or journal write): it still sweeps (the
        config ref, on the account it reached) but always ends kill_incomplete, exit 3, saying why."""
        env = gw.build_live_gateway(self)
        env.journal.set_account_fingerprint("a" * 16)                # the journal's bets live on account A
        here = env.exchange.add_order(strategy_ref="bar-manual")      # a bet with our ref on the account reached
        with patch.object(gateway_module, "register_account", side_effect=AssertionError("kill wrote accounts.json")):
            report = env.gateway.open(command="kill")
            self.addCleanup(env.gateway.close)
            self.assertIn("account_differs_from_journal", report["notes"])
            out = env.gateway.kill(scope="own", reason="drill")
        self.assertEqual(out["status"], "kill_incomplete")
        self.assertIn("account_differs_from_journal", out["notes"])
        self.assertIn(gateway_module.UNTRUSTED_SWEEP_NOTE, out["notes"])
        self.assertEqual(env.exchange.orders[here]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(env.journal.account_fingerprint(), "a" * 16)
        env.gateway.close()
        os.unlink(env.paths.kill_file)
        code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        self.assertEqual(code, 3, err)
        self.assertIn("Betfair kill incomplete: kill_incomplete; the Keychain login is not the journal's Betfair "
                      "account (account_differs_from_journal), so our bets on the journal's account were not swept; "
                      "the KILL file is engaged; cancel open bets on the Betfair website or in Bet Angel now", err)
        # The same account as the journal's is not a failure.
        same = gw.build_live_gateway(self)
        same.session.login()
        same.journal.set_account_fingerprint(same.session.account_fingerprint())
        gw.open_live(self, same, "kill")
        self.assertEqual(same.gateway.kill(scope="own", reason="drill")["status"], "kill_complete")

    def test_account_wide_kill_without_a_terminal_still_sweeps_our_bets(self):
        """Regression (live-money review): kill --scope account --confirm-account-wide without an operator
        terminal failed A11 in static arming and ended kill_incomplete_not_armed before the gateway, so our own
        resting bet was never cancelled. Our bets are swept as a plain kill; only the account-wide part fails
        closed (not sent), and the kill ends exit 3 saying so."""
        env = gw.build_live_gateway(self, confirmer=gw.FakeConfirmer(present=False))
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        bet_angel = env.exchange.add_order(strategy_ref=None, source_key="bet-angel-key")
        code, out, err = run_cli_main(env, ["kill", "--reason", "drill", "--scope", "account",
                                            "--confirm-account-wide"])
        self.assertEqual(code, 3, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "kill_incomplete")
        self.assertEqual(result["account_wide"], {"status": "not_sent", "reason": "operator_tty_required",
                                                  "exception": None})
        self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(env.exchange.orders[bet_angel]["status"], "EXECUTABLE")
        self.assertEqual([r for r in env.router.order_requests if set(r["params"]) == {"customerRef"}], [])
        self.assertIn("the account-wide cancel was not sent: it needs a terminal (operator_tty_required)", err)
        self.assertIn("kill_incomplete", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])

    def test_resweeps_are_timed_from_the_gateway_engage_when_the_cli_engage_failed(self):
        """Regression (review): when the CLI's engage failed, cmd_kill still passed its own (earlier) T0, and a
        gateway engage that found the file already created by someone else (kill_already_engaged) kept it, so
        the last pass came 25 s after the KILL file appeared instead of 35 s. Without its own KILL file the CLI
        passes no T0, and the gateway times the passes from its engage."""
        env = gw.build_live_gateway(self)
        real_engage, real_sweep = killswitch.engage, gateway_module.sweep
        seen = {}

        def cli_engage(*args, **kwargs):
            raise OSError("transient")

        def gateway_engage(paths, reason, clock=None):
            env.clock.advance(10)                                   # login etc.; meanwhile another process
            real_engage(paths, "other terminal", clock=clock)       # creates the KILL file
            seen["file_created"] = env.clock.monotonic_ns()
            return real_engage(paths, reason, clock=clock)           # kill_already_engaged

        def spy_sweep(*args, **kwargs):
            seen["t0"] = kwargs["engaged_monotonic_ns"]
            result = real_sweep(*args, **kwargs)
            seen["final"] = env.clock.monotonic_ns()
            return result

        with patch.object(killswitch, "engage", side_effect=cli_engage), \
                patch.object(gateway_module, "engage", side_effect=gateway_engage), \
                patch.object(gateway_module, "sweep", side_effect=spy_sweep):
            code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["kill_retry"]["status"], "kill_already_engaged")
        self.assertGreaterEqual(seen["t0"], seen["file_created"])
        self.assertGreaterEqual(seen["final"] - seen["file_created"], killswitch.RESWEEP_OFFSETS_S[-1] * 10**9)

    def test_every_kill_exit_3_journals_halt_kill_incomplete(self):
        """Regression (review): only gateway.kill() and the signal path journaled HALT kill_incomplete; a failed
        login (the likeliest real failure), a kill that was not armed, an unreadable config or a KILL file gone
        at the end exited 3 with the website advice but left only 'kill' in the journal. Every exit 3 now
        journals it when the journal can be written (journal_halt_kill_incomplete in the JSON). The JSON's
        session says whether a login happened."""
        def no_session(env):
            env.router.login_status = "FAIL"

        def not_armed(env):                                          # A4: the live app key is missing
            del env.runner.items[(SERVICES["app_key_live"], "default")]

        for name, damage, status in (("no_session", no_session, "kill_incomplete_no_session"),
                                     ("not_armed", not_armed, "kill_incomplete_not_armed")):
            with self.subTest(case=name):
                env = gw.build_live_gateway(self)
                ours = env.exchange.add_order(strategy_ref="bar-manual")
                damage(env)
                code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
                self.assertEqual(code, 3, err)
                result = json.loads(out)
                self.assertEqual((result["status"], result["journal_halt_kill_incomplete"]), (status, "appended"))
                self.assertEqual(result["session"], {"logged_in": False, "coexistence": "not_running_on_account"})
                self.assertIn("kill_incomplete", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])
                self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTABLE")
        # The KILL file removed while the sweep ran: the gateway's kill was complete, the CLI's exit 3 halts.
        env = gw.build_live_gateway(self)
        real_sweep = gateway_module.sweep

        def sweep_then_remove(*args, **kwargs):
            result = real_sweep(*args, **kwargs)
            os.unlink(env.paths.kill_file)
            return result

        with patch.object(gateway_module, "sweep", side_effect=sweep_then_remove):
            code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        self.assertEqual(code, 3, err)
        self.assertEqual(json.loads(out)["status"], "kill_incomplete_kill_file_not_engaged")
        self.assertEqual(json.loads(out)["journal_halt_kill_incomplete"], "appended")
        self.assertIn("kill_incomplete", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])

    def test_kill_logs_in_with_an_emergency_login(self):
        """Amendment A3 section 4 step 7: the kill's login is an emergency login, which skips the
        successful-login cap. Checked on the login ledger (not on a JSON label), with the cap already used up:
        both scopes still log in and sweep. The JSON's session.login is what the gateway used."""
        for argv in (["kill", "--reason", "drill"],
                     ["kill", "--reason", "drill", "--scope", "account", "--confirm-account-wide"]):
            with self.subTest(scope=argv[3:4]):
                env = gw.build_live_gateway(self, overrides={"session": {"max_successful_logins_per_10min": 1}})
                ours = env.exchange.add_order(strategy_ref="bar-manual")
                env.session.login()                                   # the cap is now used up for normal logins
                code, out, err = run_cli_main(env, argv)
                self.assertEqual(code, 0, err)
                result = json.loads(out)
                self.assertEqual((result["status"], result["session"]["login"]), ("kill_complete", "emergency"))
                self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
                rows = [json.loads(line) for line in env.paths.login_ledger.read_text().splitlines() if line.strip()]
                self.assertEqual([r["emergency"] for r in rows if r["kind"] == "login_attempt"], [False, True])
        self.assertIn("kill", gateway_module.EMERGENCY_COMMANDS)
        self.assertIn("kill-account", gateway_module.EMERGENCY_COMMANDS)


def _lock_free(path):
    """True when nobody holds the flock on path (probed with a non-blocking lock that is released at once)."""
    try:
        with file_lock(path, timeout_s=0):
            return True
    except Exception:
        return False


class MonotonicOnly:
    """A clock for the sweep alone: monotonic time and sleep; reading the wall clock fails the test."""

    def __init__(self, clock):
        self._clock = clock

    def monotonic_ns(self):
        return self._clock.monotonic_ns()

    def sleep(self, seconds):
        self._clock.sleep(seconds)

    def time_ns(self):
        raise AssertionError("the sweep read the wall clock")

    def utc_now(self):
        raise AssertionError("the sweep read the wall clock")


class SimpleEnv:
    """env with another Keychain runner for run_cli_main (the rest shared)."""

    def __init__(self, env, *, runner):
        self.__dict__.update(env.__dict__)
        self.runner = runner


if __name__ == "__main__":
    unittest.main()
