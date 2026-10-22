"""Recording rollout §3.4-§3.6 (design v9; §8 manual ownership, emergency paths, recording-release, status), offline.

The manual live commands (place, live-probe, reconcile, settle-market) under the P1-08 ownership lifecycle through
cli.main: owner manual:<pid>, the stated refusals, the clean-end release, the resting-bet rule, interrupts, the printed
--continue-admission N; Yu's typed recording-release in both forms; status's recording block; and the emergency paths
(kill, kill --scope account, cancel) that never touch ownership or admission. Temporary unoverridden state roots
(gw.build_live_gateway), SimExchange over a routed fake transport, a fake clock, a fake ProcessTable, a fake Keychain
runner and the fake confirmer only: the fake confirmer stands in for every typed line; nothing here has a tty.
"""

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission, cli, pilot_run
from betangel_research.betfair import paths as paths_module
from betangel_research.betfair.credentials import ANCHOR_SERVICE
from betangel_research.betfair.errors import TransportError
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, file_lock

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_run as sr
from test_betfair_admission import FakeProcesses, ledger_rows

PID = 77
OWNER = f"manual:{PID}"
FAILURE_ROWS = ({"kind": "login_attempt", "attempt_id": "f1", "at": "2020-01-01T11:00:00.000000Z",
                 "method": "interactive", "jurisdiction": "com", "emergency": False, "coexistence_ack": False},
                {"kind": "login_outcome", "attempt_id": "f1", "at": "2020-01-01T11:00:01.000000Z",
                 "method": "interactive", "jurisdiction": "com", "outcome": "auth_error", "outcome_status": "FAIL",
                 "outcome_error": None, "emergency": False, "coexistence_ack": False, "ban_until": None})


class ManualCase(unittest.TestCase):
    def setUp(self):
        self.env = gw.build_live_gateway(self, **self.build_options())
        self.env.journal.close()
        self.paths = self.env.paths
        self.processes = FakeProcesses(pid=PID)
        self.confirmer = gw.FakeConfirmer()

    def build_options(self):
        return {}

    def cli(self, *argv, confirmer=None):
        out, err = io.StringIO(), io.StringIO()
        deps = {"keychain_runner": self.env.runner, "http_transport": self.env.transport, "clock": self.env.clock,
                "confirmer": confirmer or self.confirmer, "processes": self.processes}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), deps=deps)
        return code, out.getvalue(), err.getvalue()

    def place(self, *extra, confirmer=None):
        return self.cli("place", "--market", "1.1", "--selection", str(gw.SELECTION), "--side", "BACK", "--price",
                        "3.50", "--size", "2.00", "--mode", "live", "--confirm-live", self.env.token, *extra,
                        confirmer=confirmer)

    def reconcile(self, *extra, confirmer=None):
        return self.cli("reconcile", "--mode", "live", "--confirm-live", self.env.token, *extra, confirmer=confirmer)

    def settle(self, *extra):
        return self.cli("settle-market", "--market", "1.1", "--confirm-live", self.env.token, *extra)

    def cancel(self, bet):
        return self.cli("cancel", "--market", "1.1", "--bet-id", bet, "--mode", "live", "--confirm-live",
                        self.env.token)

    def release(self, confirmer=None):
        return self.cli("recording-release", "--confirm-live", self.env.token, confirmer=confirmer)

    def view(self):
        return admission.admission_status(self.paths)

    def files(self):
        found = {}
        for path in (self.paths.admission_file, self.paths.inhibit_file, self.paths.admission_generation_file,
                     self.paths.recorder_pid_file):
            found[path.name] = path.read_bytes() if path.exists() else None
        return found

    def matched(self):
        """placeOrders matches each order in full (EXECUTION_COMPLETE, nothing remaining)."""
        sr.fills(self.env, complete=True)

    def unknown_place(self):
        """The next placeOrders reaches the exchange (the bet exists) but its response is lost, and every read after it
        fails too, so the order stays UNKNOWN (as tests/test_betfair_cli.py's lost-reply case)."""
        exchange = self.env.exchange

        def placed_then_lost(params):
            exchange.place_handler = None
            placed = exchange.place(params)
            self.lost_bet = placed["instructionReports"][0]["betId"]
            for method in ("listCurrentOrders", "listClearedOrders"):
                exchange.read_failures[method] = [TransportError("read_timeout", phase="receive",
                                                                 may_have_arrived=True) for _ in range(500)]
            raise TransportError("read_timeout", phase="receive", may_have_arrived=True)
        exchange.place_handler = placed_then_lost

    def unknown_order(self):
        """A manual place whose outcome is UNKNOWN: exit 5, its ADMISSION manual:<pid> N retained admitted."""
        self.unknown_place()
        code, out, err = self.place()
        self.assertEqual((code, json.loads(out)["orders"][0]["state"]), (5, "UNKNOWN"), err)
        return err

    def reads_back(self, matched=True):
        """Reads work again; the lost bet is fully matched (EXECUTION_COMPLETE, nothing remaining) when matched."""
        self.env.exchange.read_failures.clear()
        if matched:
            self.env.exchange.orders[self.lost_bet].update(status="EXECUTION_COMPLETE", sizeMatched=2.0,
                                                           sizeRemaining=0.0, averagePriceMatched=3.5)

    def rewrite_admission(self, **changes):
        """Fixture only: the retained ADMISSION as another end would have left it (another owner, or pending)."""
        record = json.loads(self.paths.admission_file.read_text())
        admission.atomic_write_json(self.paths.admission_file, dict(record, **changes))

    def retain(self, owner="pilot-session:s20270104-0a1b2c3d", state="admitted"):
        """An earlier owner's ADMISSION, never released (its process gone)."""
        result = admission.admit(self.paths, owner=owner, purpose="earlier owner", config=self.env.config,
                                 clock=self.env.clock, processes=FakeProcesses(pid=1))
        if state == "pending":
            record = json.loads(self.paths.admission_file.read_text())
            admission.atomic_write_json(self.paths.admission_file, dict(record, state="pending"))
        return result["generation"]

    def journal_events(self):
        from betangel_research.betfair.journal import OrderJournal
        journal = OrderJournal(self.paths.journal_path("live"), mode="live", clock=self.env.clock)
        try:
            return list(journal.events())
        finally:
            journal.close()

    def credential_reads(self):
        return [c for c in self.env.runner.calls if "-w" in c["argv"]
                and c["argv"][c["argv"].index("-s") + 1] != ANCHOR_SERVICE]


class ParserTests(unittest.TestCase):
    def test_place_and_live_probe_reject_the_flag_and_the_others_accept_it(self):
        parser = cli.build_parser()
        for argv in (["place", "--market", "1.1", "--selection", "1", "--side", "BACK", "--price", "2", "--size", "2",
                      "--continue-admission", "3"],
                     ["live-probe", "--market", "1.1", "--selection", "1", "--continue-admission", "3"]):
            with self.subTest(command=argv[0]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        for argv in (["reconcile", "--continue-admission", "3"], ["reconcile", "--accept", "r1",
                                                                  "--continue-admission", "3"],
                     ["settle-market", "--market", "1.1", "--continue-admission", "3"]):
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(argv).continue_admission, 3)


class FreshOwnershipTests(ManualCase):
    def test_fresh_place_admits_manual_pid_ends_clean_and_releases(self):
        self.matched()
        seen = []
        test = self

        class Look(gw.FakeConfirmer):
            def confirm_line(self, expected, *, banner):
                seen.append((test.view()["admission"], admission.owner_lock_state(test.paths)))
                return super().confirm_line(expected, banner=banner)
        code, out, err = self.place(confirmer=Look())
        self.assertEqual(code, 0, err)
        (current, lock), = seen
        self.assertEqual((current["owner"], current["purpose"], current["state"], current["generation"], lock),
                         (OWNER, "manual place", "admitted", 1, "held"))       # the typed prompt is inside the lock
        self.assertIn("Betfair recording: recording released (generation 1)", err)
        view = self.view()
        self.assertEqual((view["admission"], view["inhibit"], view["last_generation"]), (None, False, 1))
        self.assertEqual(admission.owner_lock_state(self.paths), "free")

    def test_place_refused_admission_held_with_no_credential_read(self):
        n = self.retain()
        self.env.runner.calls.clear()
        before = self.files()
        code, out, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("Betfair order refused: admission_held", err)
        self.assertIn(f"admission held by pilot-session:s20270104-0a1b2c3d, generation {n}, state admitted", err)
        self.assertNotIn("--continue-admission", err)                  # place never continues
        self.assertEqual(self.env.runner.calls, [])                     # no Keychain item, credential or anchor
        self.assertEqual(self.env.transport.requests, [])
        self.assertEqual(self.files(), before)

    def test_reconcile_suggests_the_flag_only_for_an_admitted_generation(self):
        n = self.retain()
        code, out, err = self.reconcile()
        self.assertEqual(code, 2)
        self.assertIn(f"to continue it, rerun with --continue-admission {n}", err)
        self.paths.admission_file.unlink()
        self.paths.inhibit_file.unlink()
        n = self.retain(state="pending")
        code, out, err = self.reconcile()
        self.assertEqual(code, 2)
        self.assertNotIn("rerun with --continue-admission", err)
        self.assertIn(f"recording-release (generation {n})", err)

    def test_continuation_mismatch_refuses_with_nothing_written_and_no_drain(self):
        recorder = admission.recorder_admission(self.paths, invocation_id="rec-1", clock=self.env.clock,
                                                processes=FakeProcesses())
        recorder.__enter__()
        self.addCleanup(lambda: recorder.__exit__(None, None, None))
        self.processes = FakeProcesses(pid=PID, starts={PID: "start-self", 4242: "start-A"})
        for label, setup in (("no_admission", lambda: None), ("other_generation", self.write_admission)):
            with self.subTest(case=label):
                setup()
                before = self.files()
                for run in (lambda: self.reconcile("--continue-admission", "5"),
                            lambda: self.settle("--continue-admission", "5")):
                    code, out, err = run()
                    self.assertEqual(code, 2, err)
                    self.assertIn("continuation_generation_mismatch", err)
                    self.assertEqual(self.files(), before)
                self.assertEqual(self.processes.signals, [])            # no drain

    def write_admission(self):
        admission.atomic_write_json(self.paths.admission_generation_file, {
            "schema_version": 1, "generation": 2, "updated_at": "2020-01-01T12:00:00.000000Z"})
        admission.atomic_write_json(self.paths.admission_file, {
            "schema_version": 1, "generation": 2, "owner": "pilot-session:s-old", "purpose": "earlier",
            "created_at": "2020-01-01T12:00:00.000000Z", "state": "admitted"})
        self.paths.inhibit_file.write_text("")


class RestingBetTests(ManualCase):
    def test_resting_bet_retains_admission_and_refuses_the_next_fresh_place(self):
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        self.assertIn("order_executable", err)
        self.assertIn("Betfair recording: recording stays off until Yu's typed recording-release (generation 1)", err)
        current = self.view()["admission"]
        self.assertEqual((current["owner"], current["generation"], current["state"]), (OWNER, 1, "admitted"))
        code, out, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn(f"admission held by {OWNER}, generation 1, state admitted", err)

    def test_stray_inhibit_then_a_clean_place_keeps_its_generation(self):
        self.paths.inhibit_file.write_text("")
        self.matched()
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        self.assertIn("INHIBIT predates this command (or ownership is retained); recording stays off until Yu's typed "
                      "recording-release (generation 1)", err)
        self.assertEqual(self.view()["admission"]["generation"], 1)
        code, out, err = self.place()
        self.assertEqual(code, 2)
        self.assertIn("admission_held", err)

    def test_resting_bet_fully_cancelled_then_typed_release_succeeds_while_the_market_is_open(self):
        code, out, err = self.place()
        bet = json.loads(out)["orders"][0]["bet_id"]
        code, out, err = self.cancel(bet)
        self.assertEqual(code, 0, err)
        order = self.env.exchange.orders[bet]
        self.assertEqual((order["status"], order["sizeCancelled"], order["sizeRemaining"]),
                         ("EXECUTION_COMPLETE", 2.0, 0.0))
        self.assertEqual(self.env.exchange.market_status, "OPEN")
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.confirmer.prompts[-1]["expected"], "RELEASE RECORDING GENERATION 1")
        self.assertIn("released generation 1", err)
        self.assertIsNone(self.view()["admission"])

    def test_control_kill_cancelled_bet_needs_the_settle_continuation(self):
        code, out, err = self.place()
        bet = json.loads(out)["orders"][0]["bet_id"]
        code, out, err = self.cli("kill", "--reason", "rollout test")
        self.assertEqual(code, 0, err)
        self.paths.kill_file.unlink()
        self.assertEqual(self.cli("resume", "--mode", "live", "--confirm-live", self.env.token)[0], 0)
        code, out, err = self.release()
        self.assertEqual(code, 2)
        self.assertIn("order_executable", err)
        self.env.exchange.market_status = "CLOSED"
        self.env.exchange.hide_cancelled_unmatched = True
        code, out, err = self.settle("--continue-admission", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.view()["admission"]["generation"], 1)          # a continuation never releases
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.view()["admission"])


class PinTests(ManualCase):
    """The P1-08 §8 pins for the rollout PR (P1-08 spec :796-805), the non-pilot half."""

    def test_pilot_unknown_retained_reconcile_continuation_then_typed_release(self):
        self.unknown_order()
        n = self.view()["admission"]["generation"]
        self.rewrite_admission(owner="pilot-session:s20270104-0a1b2c3d")      # a pilot's abnormal end retains N
        self.assertIn("order_unknown", admission.live_state_problems(self.paths, clock=self.env.clock))
        self.reads_back()
        code, out, err = self.reconcile("--continue-admission", str(n))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.view()["admission"]["owner"], "pilot-session:s20270104-0a1b2c3d")
        self.assertIn("continuation_never_releases", err)
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.view()["admission"])

    def test_reconcile_continuation_runs_with_one_standing_login_failure(self):
        n = self.retain()
        ledger_rows(self.paths, *FAILURE_ROWS)
        code, out, err = self.reconcile("--continue-admission", str(n))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.view()["admission"]["generation"], n)

    def test_abnormal_manual_owner_is_continued_not_released(self):
        err = self.unknown_order()
        self.assertIn("do not resend; run status or reconcile --mode live --continue-admission 1", err)
        self.assertIn("run reconcile --mode live --continue-admission 1", err)          # the abnormal-end line
        self.assertNotIn("no reviewed command continues pending", err)
        self.assertNotIn("recording released", err)
        current = self.view()["admission"]
        self.assertEqual((current["owner"], current["state"]), (OWNER, "admitted"))
        self.reads_back()
        code, out, err = self.reconcile("--continue-admission", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.view()["admission"]["generation"], 1)                    # continued, not released
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.view()["admission"])

    def test_recording_release_refuses_while_a_manual_owner_is_paused_or_logging_out(self):
        self.matched()
        test, seen = self, []

        class Paused(gw.FakeConfirmer):
            def confirm_line(self, expected, *, banner):
                if expected.startswith("PLACE") or "1.1" in expected:
                    seen.append(test.release(confirmer=gw.FakeConfirmer()))
                return super().confirm_line(expected, banner=banner)
        code, out, err = self.place(confirmer=Paused())
        self.assertEqual(code, 0, err)
        (refused,) = seen
        self.assertEqual(refused[0], 2)
        self.assertIn("Betfair order refused: owner_live", refused[2])
        self.assertIn(f"ADMISSION owner {OWNER}", refused[2])
        # Logging out after a clean result: the owner lock is still held until the release and the logout are done.
        router = self.env.router
        original = type(router).__call__
        seen.clear()

        def at_logout(record):
            if record["url"].endswith("/api/logout") and not seen:
                seen.append(test.release(confirmer=gw.FakeConfirmer()))
            return original(router, record)
        self.env.transport.router = at_logout
        code, out, err = self.place()
        self.assertEqual(code, 0, err)
        self.assertEqual(seen[0][0], 2)
        self.assertIn("owner_live", seen[0][2])

    def test_rd3_1_interleaving_manual_owner_and_pilot_arm(self):
        """A (a manual owner) admits, B (pilot ARM's ownership) is refused, A releases, only then B succeeds."""
        a = admission.live_ownership(self.paths, owner=OWNER, purpose="manual place", config=self.env.config,
                                     clock=self.env.clock, processes=self.processes)
        ticket = a.__enter__()
        try:
            with self.assertRaises(admission.OwnershipRefused) as caught:
                with admission.live_ownership(self.paths, owner="pilot-session:s1", purpose="RS-19 pilot x",
                                              config=self.env.config, clock=self.env.clock, processes=self.processes):
                    pass
            self.assertEqual(caught.exception.code, "owner_live")
            self.assertTrue(admission.release_own(ticket, self.paths, config=self.env.config,
                                                  clock=self.env.clock)["released"])
        finally:
            a.__exit__(None, None, None)
        with admission.live_ownership(self.paths, owner="pilot-session:s1", purpose="RS-19 pilot x",
                                      config=self.env.config, clock=self.env.clock, processes=self.processes) as b:
            self.assertEqual((b.kind, b.generation), ("fresh", 2))


class InterruptTests(ManualCase):
    def interrupted_place(self):
        with patch.object(admission, "_drain", side_effect=KeyboardInterrupt):
            return self.place()

    def test_interrupt_in_the_drain_leaves_pending_and_names_it(self):
        code, out, err = self.interrupted_place()
        self.assertEqual(code, 4)
        current = self.view()["admission"]
        self.assertEqual((current["generation"], current["state"]), (1, "pending"))
        self.assertIn("recording stays off until Yu's typed recording-release (generation 1, state pending)", err)

    def test_interrupt_over_an_open_order_stops_for_review_and_promises_nothing(self):
        self.unknown_order()
        self.paths.admission_file.unlink()                     # fixture: no ADMISSION, so the next place is fresh
        code, out, err = self.interrupted_place()
        self.assertEqual(code, 4)
        self.assertIn("recording-release will refuse while order_unknown, market_unresolved; no reviewed command "
                      "continues pending generation 2; stop for review (Yu, with Astra's review)", err)
        self.assertNotIn("until Yu's typed recording-release", err)

    def test_admitted_over_an_unclean_journal_names_the_reconcile_continuation(self):
        self.unknown_order()
        n = self.view()["admission"]["generation"]
        from betangel_research.betfair.gateway import OrderGateway
        with patch.object(OrderGateway, "reconcile", side_effect=KeyboardInterrupt):
            code, out, err = self.reconcile("--continue-admission", str(n))
        self.assertEqual(code, 4)
        self.assertIn(f"run reconcile --mode live --continue-admission {n}", err)
        self.assertIn(f"then Yu's typed recording-release (generation {n})", err)
        self.assertNotIn("no reviewed command continues pending", err)

    def test_pending_over_an_unclean_journal_has_no_reviewed_recovery(self):
        self.unknown_order()
        n = self.view()["admission"]["generation"]
        self.rewrite_admission(state="pending")               # as an interrupted fresh admit would leave it
        before, events = self.files(), len(self.journal_events())
        for run in (self.reconcile, self.settle):
            code, out, err = run()
            self.assertEqual(code, 2)
            self.assertIn("admission_held", err)
        code, out, err = self.reconcile("--continue-admission", str(n))
        self.assertEqual(code, 2)
        self.assertIn("continuation_needs_admitted_generation", err)
        code, out, err = self.release()
        self.assertEqual(code, 2)
        self.assertIn("release_refused", err)
        self.assertIn("order_unknown", err)
        self.assertEqual((self.files(), len(self.journal_events())), (before, events))


class EvidenceLineTests(unittest.TestCase):
    VIEW = {"customer_order_ref": "ref-1", "bet_id": "300000000002", "state": "EXECUTABLE", "size_remaining": 2.0,
            "size_matched": 0.0, "error_code": cli.WITHHELD_CODE}

    def test_place_lines_carry_the_flag(self):
        cont = " --continue-admission 3"
        self.assertIn("settle-market --bet-angel-paused --continue-admission 3 after",
                      cli._withheld_line(self.VIEW, " --bet-angel-paused", cont))
        cleared = dict(self.VIEW, error_code=cli.CLEARED_CODE)
        self.assertIn("settle-market --continue-admission 3 after", cli._cleared_line(cleared, "", cont))
        unknown = dict(self.VIEW, state="UNKNOWN", error_code="connection_lost")
        self.assertIn("reconcile --mode live --continue-admission 3", cli._outcome_line(unknown, cont=cont))
        self.assertIn("reconcile --mode live --continue-admission 3", cli._cancel_line(unknown, "", cont))
        self.assertIn("reconcile --mode live --continue-admission 3",
                      cli._unresolved_line({"customer_order_ref": "r", "market_id": "1.1", "state": "UNKNOWN",
                                            "error_code": None}, "", cont))
        self.assertIn("reconcile --mode live --continue-admission 3",
                      cli.SETTLE_ADVICE["settle_market_unresolved_orders"].format(flag="", cont=cont))
        self.assertIn("do not resend; run status or reconcile --mode live --continue-admission 3",
                      cli._stopped_line("SIGTERM", "place", "", result_printed=False, cont=cont))

    def test_cancel_lines_byte_identical(self):
        """Under cancel (and kill) the lines are exactly as at 63a1ab6 (strings copied from that commit)."""
        unknown = dict(self.VIEW, state="UNKNOWN", error_code="connection_lost")
        self.assertEqual(cli._cancel_line(unknown, " --bet-angel-paused"),
                         "Betfair cancel outcome unknown: ref-1 bet 300000000002 UNKNOWN connection_lost; it may still "
                         "be live: run reconcile --mode live --bet-angel-paused, then cancel it again if it is still "
                         "live, or cancel it on the Betfair website or in Bet Angel")
        self.assertEqual(cli._cancel_line(self.VIEW, ""),
                         "Betfair order ref-1 bet 300000000002: Betfair shows part or all of it cancelled, lapsed or "
                         "voided (exchange_reduction_withheld_until_settle); it stays counted at worst case and shows "
                         "as EXECUTABLE until you settle its market with settle-market after the market closes; status "
                         "and reconcile change nothing; cancel it again only if orders-current still shows an "
                         "unmatched part")
        self.assertEqual(cli._stopped_line("SIGTERM", "cancel", "", result_printed=False),
                         "Betfair stopped after a cancel request was sent (SIGTERM); the bet may still be live: check "
                         "orders-current and cancel it again if it is, or cancel it on the Betfair website or in Bet "
                         "Angel")
        self.assertEqual(cli._sent_advice(), "do not resend; run status or reconcile --mode live")


class RecordingReleaseTests(ManualCase):
    def test_generation_form(self):
        n = self.retain()
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.confirmer.prompts[-1]["expected"], f"RELEASE RECORDING GENERATION {n}")
        shown = json.loads(out.split("\n}\n")[0] + "\n}")
        self.assertEqual(shown, {"form": "generation", "owner": "pilot-session:s20270104-0a1b2c3d", "generation": n,
                                 "state": "admitted", "inhibit": "present", "check_codes": []})
        self.assertIn(f"released generation {n}", err)
        self.assertEqual((self.view()["admission"], self.view()["inhibit"]), (None, False))

    def test_inhibit_only_form(self):
        self.paths.inhibit_file.write_text("")
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.confirmer.prompts[-1]["expected"], "RELEASE RECORDING INHIBIT")
        self.assertIn("released inhibit", err)
        self.assertFalse(self.paths.inhibit_file.exists())

    def test_nothing_to_release_exit_0_no_prompt(self):
        code, out, err = self.release()
        self.assertEqual((code, json.loads(out.split("\n}\n")[0] + "\n}")["status"]), (0, "nothing_to_release"))
        self.assertEqual(self.confirmer.prompts, [])

    def test_pending_generation_released_by_the_generation_form(self):
        n = self.retain(state="pending")
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.confirmer.prompts[-1]["expected"], f"RELEASE RECORDING GENERATION {n}")
        self.assertEqual((self.view()["admission"], self.view()["inhibit"]), (None, False))

    def test_inhibit_only_never_offered_while_an_admission_exists(self):
        n = self.retain()
        self.release(confirmer=gw.FakeConfirmer(lines=["RELEASE RECORDING INHIBIT"]))
        self.assertEqual(self.view()["admission"]["generation"], n)
        self.release()
        self.assertEqual(self.confirmer.prompts[-1]["expected"], f"RELEASE RECORDING GENERATION {n}")

    def test_owner_live_held_by_another_process_and_owner_state_unknown(self):
        self.retain()
        child_code = ("import sys, time\n"
                      "from betangel_research.betfair.paths import file_lock\n"
                      "with file_lock(sys.argv[1]):\n"
                      "    print('ready', flush=True)\n"
                      "    time.sleep(60)\n")
        env = dict(os.environ, PYTHONPATH=str(support.REPO_ROOT), PYTHONDONTWRITEBYTECODE="1",
                   **{FORBID_NETWORK_ENV: "1"})
        child = subprocess.Popen([sys.executable, "-c", child_code, str(self.paths.live_owner_lock)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            before = self.files()
            code, out, err = self.release()
            self.assertEqual(code, 2)
            self.assertIn("Betfair order refused: owner_live", err)
            self.assertEqual(self.files(), before)
            self.assertEqual(self.confirmer.prompts, [])
        finally:
            child.kill()
            child.wait()
            child.stdout.close()
            child.stderr.close()

        @contextlib.contextmanager
        def broken(*args, **kwargs):
            raise OSError("io")
            yield
        with patch.object(admission, "file_lock", broken):
            code, out, err = self.release()
        self.assertEqual(code, 2)
        self.assertIn("owner_state_unknown", err)

    def test_login_problem_prints_the_login_reset_line_first(self):
        self.paths.inhibit_file.write_text("")
        ledger_rows(self.paths, *FAILURE_ROWS)
        code, out, err = self.release()
        self.assertEqual(code, 2)
        lines = err.strip().splitlines()
        self.assertEqual(lines[-2], pilot_run.LOGIN_RESET_LINE)
        self.assertIn("release_refused (login_failure_after_barrier)", lines[-1])
        self.assertTrue(self.paths.inhibit_file.exists())

    def test_mistyped_line_releases_nothing(self):
        n = self.retain()
        before = self.files()
        code, out, err = self.release(confirmer=gw.FakeConfirmer(lines=[f"RELEASE RECORDING GENERATION {n + 1}"]))
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: operator_echo_mismatch"))
        self.assertEqual(self.files(), before)

    def test_no_credential_read_and_no_network(self):
        self.retain()
        self.env.runner.calls.clear()
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        services = {c["argv"][c["argv"].index("-s") + 1] for c in self.env.runner.calls}
        self.assertEqual(services, {ANCHOR_SERVICE})
        self.assertEqual(self.env.transport.requests, [])

    def test_static_arming_runs_before_the_owner_lock(self):
        self.retain()
        with patch.object(admission, "recording_release", side_effect=AssertionError("reached")):
            code, out, err = self.cli("recording-release", "--confirm-live", "live-000000000000-20200101")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["failures"], ["confirm_token_mismatch"])
            code, out, err = self.release(confirmer=gw.FakeConfirmer(present=False))
            self.assertEqual(json.loads(out)["failures"], ["operator_tty_required"])


class AbsentJournalReleaseTests(ManualCase):
    def build_options(self):
        return {"roundtrip": False}

    def setUp(self):
        super().setUp()
        for suffix in ("", "-wal", "-shm"):
            path = self.paths.journal_path("live").with_name(self.paths.journal_path("live").name + suffix)
            if path.exists():
                path.unlink()

    def test_status_live_state_clean_on_an_absent_journal_and_the_kill_code(self):
        code, out, err = self.cli("status")
        self.assertEqual((code, json.loads(out)["recording"]["live_state"]), (0, "clean"))
        self.paths.kill_file.write_text("")
        code, out, err = self.cli("status")
        self.assertEqual(json.loads(out)["recording"]["live_state"], ["kill_file_engaged"])

    def test_absent_journal_does_not_block_the_inhibit_only_form(self):
        self.paths.inhibit_file.write_text("")
        code, out, err = self.release()
        self.assertEqual(code, 0, err)

    def test_unreadable_journal_refuses(self):
        self.paths.inhibit_file.write_text("")
        self.paths.journal_path("live").symlink_to(self.paths.root / "nowhere.sqlite")
        code, out, err = self.release()
        self.assertEqual(code, 2)
        self.assertIn("live_journal_unreadable", err)
        self.assertEqual(self.confirmer.prompts, [])
        self.assertTrue(self.paths.inhibit_file.exists())


class StatusTests(ManualCase):
    def block(self):
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        return json.loads(out)["recording"]

    def test_every_field_and_the_clean_state(self):
        n = None
        with patch.object(admission, "recorder_lock_held", side_effect=AssertionError("never from status")):
            block = self.block()
            self.assertEqual(block, {"inhibit": "absent", "admission": "none", "owner_lock": "free",
                                     "armed_session": "absent", "recorder_pid": {"present": False},
                                     "live_state": "clean"})
        n = self.retain()
        with patch.object(admission, "recorder_lock_held", side_effect=AssertionError("never from status")):
            admission.atomic_write_json(self.paths.recorder_pid_file, {
                "schema_version": 1, "pid": 4242, "start_time": "start-A", "invocation_id": "d20200101-a1-0000000a",
                "started_at": "2020-01-01T11:00:00.000000Z"})
            pilot_run.armed_session_path(self.paths).parent.mkdir(parents=True, exist_ok=True)
            pilot_run.armed_session_path(self.paths).write_text("{}")
            with file_lock(self.paths.live_owner_lock, timeout_s=0):
                block = self.block()
            self.assertEqual(block, {"inhibit": "present",
                                     "admission": {"owner": "pilot-session:s20270104-0a1b2c3d", "generation": n,
                                                   "state": "admitted"},
                                     "owner_lock": "held", "armed_session": "pilot_session_armed",
                                     "recorder_pid": {"present": True, "invocation_id": "d20200101-a1-0000000a"},
                                     "live_state": "clean"})
            self.paths.admission_file.write_text("garbage")
            block = self.block()
            self.assertEqual((block["admission"], block["inhibit"]), ("admission_state_unknown", "present"))

    def test_live_state_codes_and_an_unreadable_journal(self):
        self.unknown_order()
        self.assertEqual(self.block()["live_state"], ["order_unknown", "market_unresolved"])
        journal = self.paths.journal_path("live")
        for suffix in ("-wal", "-shm"):
            extra = journal.with_name(journal.name + suffix)
            if extra.exists():
                extra.unlink()
        journal.unlink()
        journal.symlink_to(self.paths.root / "nowhere.sqlite")
        self.assertEqual(self.block()["live_state"], ["live_journal_unreadable"])


class EmergencyPathTests(ManualCase):
    PINNED = ("_gateway", "_live_gateway", "cmd_cancel", "_order_result", "_cancel_problems", "_cancel_line",
              "_evidence_line", "_withheld_line", "_cleared_line", "_exit_after_send", "_stopped_line")

    def test_static_pin_no_ownership_or_admission_reference(self):
        source = (support.REPO_ROOT / "betangel_research/betfair/cli.py").read_text()
        tree = ast.parse(source)
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for name in self.PINNED:
            node = functions[name]
            text = ast.get_source_segment(source, node)
            with self.subTest(function=name):
                for word in ("admission", "live_ownership", "release_own", "_manual_owned", "live_owner_lock"):
                    self.assertNotIn(word, text)

    def test_kill_and_cancel_never_touch_ownership_or_admission(self):
        code, out, err = self.place()                               # a resting bet, ADMISSION manual N retained
        bet = json.loads(out)["orders"][0]["bet_id"]
        child_code = ("import sys, time\n"
                      "from betangel_research.betfair.paths import file_lock\n"
                      "with file_lock(sys.argv[1]):\n"
                      "    print('ready', flush=True)\n"
                      "    time.sleep(60)\n")
        env = dict(os.environ, PYTHONPATH=str(support.REPO_ROOT), PYTHONDONTWRITEBYTECODE="1",
                   **{FORBID_NETWORK_ENV: "1"})
        child = subprocess.Popen([sys.executable, "-c", child_code, str(self.paths.live_owner_lock)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(), child.stdout.close(),
                                 child.stderr.close()))
        self.assertEqual(child.stdout.readline().strip(), "ready")
        opened = []
        real = paths_module.file_lock

        @contextlib.contextmanager
        def counting(path, *args, **kwargs):
            opened.append(os.path.basename(str(path)))
            with real(path, *args, **kwargs) as held:
                yield held
        boom = AssertionError("emergency path touched admission")
        patches = [patch.object(paths_module, "file_lock", counting), patch.object(admission, "file_lock", counting),
                   patch.object(pilot_run, "file_lock", counting),
                   patch.object(admission, "admission_status", side_effect=boom),
                   patch.object(admission, "live_state_problems", side_effect=boom),
                   patch.object(admission, "owner_lock_state", side_effect=boom),
                   patch.object(admission, "_state", side_effect=boom), patch.object(admission, "_load", side_effect=boom)]
        for item in patches:
            item.start()
        try:
            code, out, err = self.cancel(bet)
            self.assertEqual(code, 0, err)
            code, out, err = self.cli("kill", "--reason", "pin")
            self.assertEqual(code, 0, err)
            code, out, err = self.cli("kill", "--reason", "pin account", "--scope", "account", "--confirm-account-wide")
            self.assertIn(code, (0, 3), err)
            self.assertNotIn("emergency path touched admission", out + err)
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertNotIn("admission.lock", opened)
        self.assertNotIn(".live-owner.lock", opened)


if __name__ == "__main__":
    unittest.main()
