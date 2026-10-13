"""Betfair CLI: argument handling, exit codes and sanitised errors. Offline: in-process runs inject fakes
through deps; subprocess runs always pass --state-root and --config under BETANGEL_BETFAIR_FORBID_NETWORK=1.
These show the command line's refusals and outputs, not anything about live Betfair behaviour."""

import contextlib
import io
import json
import os
from pathlib import Path
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import cli as cli_module
from betangel_research.betfair.cli import HELP, main
from betangel_research.betfair.errors import TransportError
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, StatePaths
from betangel_research.temporal import timestamp_ms

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_stream as fx



# The coexistence refusal names the flag to add (round 4), so a printed command copied without it says what to do.
COEXISTENCE_REFUSAL = ("Betfair input error: bet_angel_coexistence_unverified; add --bet-angel-paused (you state Bet "
                       "Angel is logged out or not managing positions); kill needs no flag")

def gw_failure(code, exec_code=None):
    """An exchange handler answering FAILURE with one instruction error code (place or cancel)."""
    def handler(params):
        instruction = params["instructions"][0]
        return {"customerRef": params["customerRef"], "status": "FAILURE", "errorCode": exec_code,
                "marketId": params["marketId"],
                "instructionReports": [{"status": "FAILURE", "errorCode": code, "instruction": instruction}]}
    return handler


def run_main(argv, deps=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, deps=deps)
    return code, out.getvalue(), err.getvalue()


def run_cli(*args, timeout=120):
    return subprocess.run([sys.executable, "-m", "betangel_research.betfair", *map(str, args)],
                          cwd=str(support.REPO_ROOT), capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, **{FORBID_NETWORK_ENV: "1", "PYTHONDONTWRITEBYTECODE": "1"}))


class InProcessCliTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / "home"
        self.home.mkdir()
        self.clock = support.FakeClock()
        self.runner = support.FakeKeychainRunner(items={})
        self.transport = support.FakeHttpTransport()
        self.deps = {"home": self.home, "keychain_runner": self.runner, "clock": self.clock,
                     "http_transport": self.transport, "confirmer": gw.FakeConfirmer()}
        self.paths = StatePaths(home=self.home, env=dict(support.FORBID_ENV))
        support.write_test_config(self.paths)

    def secret_reads(self):
        return [c for c in self.runner.calls if "-w" in c["argv"] and c["argv"][3].startswith(
            "betangel-research.betfair.") and not c["argv"][3].endswith("journal-anchor")]

    def place_args(self, *extra, price="3.60", size="2.00"):
        book_path = self.home / "book.json"
        book_path.write_text(json.dumps(gw.paper_book(self.clock)))
        return ["place", "--market", "1.1", "--selection", "12345", "--side", "BACK", "--price", price, "--size", size,
                "--book", str(book_path), *extra]

    def test_doctor_offline_without_credentials(self):
        code, out, err = run_main(["doctor"], self.deps)
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["status"], "doctor_report_not_health_guarantee")
        self.assertTrue(report["config"]["valid"])
        self.assertTrue(all(value is False for value in report["keychain_present"].values()))
        self.assertTrue(report["keychain_env_ok"])                 # every query ran, with the minimal environment
        self.assertEqual(report["journal_anchor"]["status"], "absent")
        self.assertEqual(report["cert_files"]["status"], "cert_files_unsafe")
        self.assertIn("*.crt", report["gitignore"]["covered"])
        self.assertTrue(report["paths"]["overridden"])
        self.assertIn("state_root_invalid", report["live_arming_static"]["failures"])
        self.assertEqual(self.secret_reads(), [])
        self.assertEqual(self.transport.requests, [])
        # When security cannot even start, "absent" is not an answer: keychain_env_ok says so.
        deps = {k: v for k, v in self.deps.items() if k != "keychain_runner"}
        code, out, err = run_main(["doctor"], deps)
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertFalse(report["keychain_env_ok"])
        self.assertEqual(report["keychain_queries"]["ran"], 0)

    def test_keychain_setup_prints_only(self):
        code, out, err = run_main(["keychain-setup"], self.deps)
        self.assertEqual(code, 0, err)
        commands = json.loads(out)["commands"]
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertTrue(command.startswith("security add-generic-password -U -s betangel-research.betfair."))
            self.assertTrue(command.endswith(" -w"))
        self.assertIn('-T "" -w', commands[3])
        self.assertEqual(self.runner.calls, [])

    def test_place_paper_from_book_fixture(self):
        code, out, err = run_main(self.place_args(), self.deps)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["mode"], result["decision"]), ("paper", "accepted"))
        self.assertEqual(result["orders"][0]["state"], "EXECUTABLE")
        self.assertTrue(self.paths.journal_path("paper").exists())
        self.assertFalse(self.paths.journal_path("live").exists())
        self.assertEqual(self.transport.requests, [])
        # A refused paper order exits 2 with the rule.
        code, out, err = run_main(self.place_args(size="5.00"), self.deps)
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "Betfair order refused: stake_cap")
        self.assertEqual(json.loads(out)["decision"], "refused")

    def test_live_without_token_exit2_no_traceback(self):
        argv = ["place", "--market", "1.1", "--selection", "12345", "--side", "BACK", "--price", "3.50", "--size",
                "2.00", "--mode", "live", "--bet-angel-paused"]
        code, out, err = run_main(argv, self.deps)
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("Betfair order refused: "), err)
        self.assertNotIn("Traceback", err + out)
        report = json.loads(out)
        self.assertFalse(report["armed"])
        for failure in ("not_armed", "confirm_token_mismatch", "keychain_credentials_missing", "state_root_invalid"):
            self.assertIn(failure, report["failures"])
        self.assertEqual(self.secret_reads(), [])
        self.assertEqual(self.transport.requests, [])
        self.assertFalse(self.paths.journal_path("live").exists())     # a refused live command creates nothing
        code, out, err = run_main(argv[:-1], self.deps)        # coexistence unverified, no flag
        self.assertEqual((code, err.strip()), (2, COEXISTENCE_REFUSAL))

    def test_live_book_flag_refused(self):
        code, out, err = run_main(self.place_args("--mode", "live"), self.deps)
        self.assertEqual((code, err.strip()), (2, "Betfair order refused: book_source_not_allowed_live"))
        self.assertFalse(self.paths.journal_path("live").exists())
        self.assertEqual(self.runner.calls, [])

    def test_kill_engages_file_then_incomplete_without_login(self):
        code, out, err = run_main(["kill", "--reason", "test stop", "--bet-angel-paused"], self.deps)
        self.assertEqual(code, 3)
        self.assertIn("kill_incomplete_not_armed", err)
        self.assertIn("Betfair website or in Bet Angel", err)
        self.assertTrue(self.paths.kill_file.exists())
        result = json.loads(out)
        self.assertEqual(result["kill"]["status"], "kill_engaged")
        self.assertEqual(result["status"], "kill_incomplete_not_armed")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.secret_reads(), [])
        # A config that cannot be read still leaves the kill file engaged (exit 3).
        self.paths.config_path.write_text("{")
        code, out, err = run_main(["kill", "--reason", "again"], self.deps)
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["kill"]["status"], "kill_already_engaged")

    def test_kill_repairs_unwritable_root_and_never_exits_4_on_os_error(self):
        """Regression (audit): engage() ran outside any error handling, so a state root left at mode 0500
        made kill exit 4 (no JSON, no advice, no KILL file, no HALT) while place and cancel repaired the
        root through paths.ensure() and kept working."""
        self.paths.ensure()
        os.chmod(self.paths.root, 0o500)
        self.addCleanup(os.chmod, self.paths.root, 0o700)
        code, out, err = run_main(["kill", "--reason", "test stop"], self.deps)
        self.assertEqual(code, 3)
        self.assertTrue(self.paths.kill_file.exists())
        self.assertEqual(oct(self.paths.root.stat().st_mode & 0o777), oct(0o700))
        result = json.loads(out)
        self.assertEqual(result["kill"]["status"], "kill_engaged")
        self.assertTrue(result["kill_file_engaged"])
        self.assertIn("Betfair website or in Bet Angel", err)
        # When the KILL file cannot be created at all, the kill is still exit 3 with its JSON and advice.
        self.paths.kill_file.unlink()
        from betangel_research.betfair import paths as paths_module
        with patch("betangel_research.betfair.killswitch.engage", side_effect=PermissionError("denied")), \
                patch.object(paths_module.StatePaths, "ensure", side_effect=PermissionError("denied")):
            code, out, err = run_main(["kill", "--reason", "test stop"], self.deps)
        self.assertEqual(code, 3)
        self.assertNotIn("Betfair stopped", err)
        self.assertIn("could NOT be created", err)
        self.assertIn("Betfair website or in Bet Angel", err)
        result = json.loads(out)
        self.assertEqual(result["kill"]["status"], "kill_file_not_engaged")
        self.assertEqual(result["state_root"], "failed:PermissionError")
        self.assertFalse(result["kill_file_engaged"])
        self.assertFalse(self.paths.kill_file.exists())

    def test_safe_text_scans_before_folding_and_truncating(self):
        """Regression (audit): _safe_text folded whitespace and cut at 240 characters before scanning, so a
        secret holding a tab, or one straddling the cut, was printed whole or as a prefix."""
        from betangel_research.betfair.cli import _safe_text
        from betangel_research.betfair.secret import Secret
        password = Secret("correct\thorse battery-audit")
        key = Secret("AbCdEfGh12345678-audit")
        spaced = Secret("two words-audit-0001")
        cases = [ValueError("form " + password.reveal()), ValueError("x" * 220 + " value " + key.reveal()),
                 ValueError("bad header value " + key.reveal()), ValueError("two  words-audit-0001")]
        for exc in cases:
            with self.subTest(text=str(exc)[:40]):
                self.assertEqual(_safe_text(exc), "ValueError: redacted")
        self.assertNotIn(spaced.reveal(), _safe_text(ValueError("two  words-audit-0001")))
        self.assertEqual(_safe_text(ValueError("plain  text\there")), "ValueError: plain text here")
        self.assertEqual(len(_safe_text(ValueError("y" * 500))), 240)

    def test_overrides_refused_without_forbid_env(self):
        support.scripted_network_only(self)
        for argv in (["--state-root", str(self.home / "s"), "status"], ["--config", str(self.home / "c.json"), "status"]):
            with self.subTest(argv=argv[0]):
                code, out, err = run_main(argv)
                self.assertEqual((code, err.strip()), (2, "Betfair input error: override_not_allowed"))
                self.assertEqual(out, "")

    def test_deps_refused_without_forbid_env(self):
        code, _, err = run_main(["status"], {"unknown": 1})
        self.assertEqual((code, err.strip()), (2, "Betfair input error: deps_not_allowed"))
        code, _, err = run_main(["status"], {"env": {}, "home": self.home})
        self.assertEqual((code, err.strip()), (2, "Betfair input error: deps_not_allowed"))
        support.scripted_network_only(self)
        code, out, err = run_main(["status"], {"clock": self.clock})
        self.assertEqual((code, err.strip()), (2, "Betfair input error: deps_not_allowed"))
        self.assertEqual(out, "")

    def test_money_flags_regex(self):
        for size in ("nan", "inf", "1e1", "-1", "0", "0.00", "1.005", " 2.00", "2.00\n", "+2", "２.00", "1,00"):
            with self.subTest(size=size):
                code, out, err = run_main(self.place_args(size=size), self.deps)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("Betfair input error: "), err)
                self.assertNotIn("Traceback", err)
        for price in ("3.51", "NaN", "3.5e0", "1.00"):
            with self.subTest(price=price):
                code, _, err = run_main(self.place_args(price=price), self.deps)
                self.assertEqual(code, 2, err)
        self.assertFalse(self.paths.journal_path("paper").exists())

    def test_status_and_journal_offline(self):
        code, out, err = run_main(["status"], self.deps)
        self.assertEqual(code, 0, err)
        status = json.loads(out)
        self.assertFalse(status["kill_engaged"])
        self.assertEqual(status["journals"]["live"]["status"], "journal_absent")
        self.assertEqual(len(status["confirm_token_inputs"]["config_sha256_prefix"]), 12)
        self.assertNotIn("live-", json.dumps(status["confirm_token_inputs"]["config_sha256_prefix"]))
        run_main(self.place_args(), self.deps)
        code, out, _ = run_main(["journal", "--verify"], self.deps)
        journal = json.loads(out)
        self.assertEqual((code, journal["verify"]["status"]), (0, "ok"))
        self.assertEqual(len(journal["orders"]), 1)
        code, out, _ = run_main(["journal", "--state", "REFUSED"], self.deps)
        self.assertEqual(json.loads(out)["orders"], [])

    def test_offline_and_paper_commands(self):
        target = self.home / "fresh-config.json"
        fresh = StatePaths(home=self.home / "other", env=dict(support.FORBID_ENV))
        code, out, err = run_main(["init-config"], dict(self.deps, home=self.home / "other"))
        self.assertEqual(code, 0, err)
        created = json.loads(out)
        self.assertEqual(created["status"], "config_created")
        self.assertEqual(Path(created["path"]), fresh.config_path)
        self.assertEqual(fresh.config_path.stat().st_mode & 0o777, 0o600)
        code, out, _ = run_main(["init-config"], dict(self.deps, home=self.home / "other"))
        self.assertEqual(json.loads(out)["status"], "config_exists")
        self.assertFalse(target.exists())
        # login-reset needs the typed phrase.
        self.deps["confirmer"] = gw.FakeConfirmer(["RESET"])
        code, _, err = run_main(["login-reset"], self.deps)
        self.assertEqual((code, err.strip()), (2, "Betfair order refused: operator_echo_mismatch"))
        self.deps["confirmer"] = gw.FakeConfirmer()
        code, out, err = run_main(["login-reset"], self.deps)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["kind"], "login_reset")
        self.assertEqual(self.deps["confirmer"].prompts[0]["expected"], "RESET LOGIN FAILURES")
        # Paper place, replace, cancel, step and reconcile through the CLI.
        placed = json.loads(run_main(self.place_args(), self.deps)[1])
        bet_id = placed["orders"][0]["bet_id"]
        book = self.home / "book.json"
        code, out, err = run_main(["replace", "--market", "1.1", "--bet-id", bet_id, "--new-price", "3.70", "--book",
                                   str(book)], self.deps)
        self.assertEqual(code, 0, err)
        child = [o for o in json.loads(out)["orders"] if o["state"] == "EXECUTABLE"][0]
        code, out, err = run_main(["cancel", "--market", "1.1", "--bet-id", child["bet_id"]], self.deps)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["orders"][0]["state"], "EXECUTION_COMPLETE")
        code, _, err = run_main(["cancel", "--market", "1.1", "--bet-id", "999999", "--size-reduction", "nan"],
                                self.deps)
        self.assertEqual(code, 2)
        code, out, err = run_main(["paper-step", "--market", "1.1", "--book", str(book)], self.deps)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["decision"], "advanced")
        code, out, err = run_main(["reconcile"], self.deps)
        self.assertEqual((code, json.loads(out)["status"]), (0, "paper_has_no_exchange_to_reconcile"))
        # Paper resume after the kill file is removed by hand.
        run_main(["kill", "--reason", "x", "--bet-angel-paused"], self.deps)
        code, _, err = run_main(["resume"], self.deps)
        self.assertEqual((code, err.strip()), (2, "Betfair order refused: kill_file_still_present"))
        os.unlink(self.paths.kill_file)
        code, out, err = run_main(["resume"], self.deps)
        self.assertEqual((code, json.loads(out)["status"]), (0, "resumed"))
        self.assertEqual(self.transport.requests, [])

    def test_paper_place_on_a_closed_book_closes_the_market_for_later_processes(self):
        """Re-review R2 follow-up through the CLI, one gateway per command: a paper place on a CLOSED book is refused
        as before (validation:V9, exit 2), and a later place on the pre-close book, inside max_snapshot_age_s, is
        refused too (paper_market_closed, exit 2) instead of matching 2.00 after the result was shown."""
        pre = self.home / "pre.json"
        pre.write_text(json.dumps(gw.paper_book(self.clock)))
        self.clock.advance(1)
        book = gw.paper_book(self.clock, status="CLOSED")
        book["runners"][str(gw.SELECTION)]["status"] = "WINNER"
        book["runners"][str(gw.OTHER_SELECTION)]["status"] = "LOSER"
        closed = self.home / "closed.json"
        closed.write_text(json.dumps(book))
        place = ["place", "--market", "1.1", "--selection", str(gw.SELECTION), "--side", "BACK", "--price", "3.50",
                 "--size", "2.00", "--book"]
        code, out, err = run_main(place + [str(closed)], self.deps)
        self.assertEqual((code, err.strip(), json.loads(out)["refusal"]["rule"]),
                         (2, "Betfair order refused: validation:V9", "validation:V9"))
        self.clock.advance(1)
        code, out, err = run_main(place + [str(pre)], self.deps)
        self.assertEqual((code, err.strip()), (2, "Betfair order refused: paper_market_closed"))
        code, out, err = run_main(["paper-step", "--market", "1.1", "--book", str(closed)], self.deps)
        self.assertEqual((code, json.loads(out)["decision"]), (0, "advanced"))
        code, out, _ = run_main(["journal", "--state", "SETTLED"], self.deps)
        self.assertEqual((code, json.loads(out)["orders"]), (0, []))
        self.assertEqual(self.transport.requests, [])

    def test_live_resume_reads_no_secret(self):
        env = gw.build_live_gateway(self)
        env.journal.append("halt", {"reason": "kill", "scope": "global"})
        env.journal.close()
        deps = {"keychain_runner": env.runner, "clock": env.clock, "confirmer": env.confirmer,
                "http_transport": env.transport}
        before = len(env.runner.calls)          # the fixture itself loaded credentials while building
        code, _, err = run_main(["resume", "--mode", "live"], deps)
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: confirm_token_mismatch"))
        code, out, err = run_main(["resume", "--mode", "live", "--confirm-live", env.token], deps)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["status"], result["halted"], result["anchor_written"]), ("resumed", False, True))
        reads = [c for c in env.runner.calls[before:] if c["argv"][1] == "find-generic-password" and "-w" in c["argv"]
                 and not c["argv"][3].endswith("journal-anchor")]
        self.assertEqual(reads, [])
        self.assertEqual(env.transport.requests, [])

    def live_deps(self, fixture, **changes):
        deps = {"keychain_runner": fixture.runner, "http_transport": fixture.transport, "clock": fixture.clock,
                "confirmer": fixture.confirmer}
        deps.update(changes)
        return deps

    def live_place(self, env, price="3.50"):
        return ["place", "--market", "1.1", "--selection", "12345", "--side", "BACK", "--price", price, "--size",
                "2.00", "--mode", "live", "--confirm-live", env.token]

    def test_live_place_end_to_end_with_fakes(self):
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        self.assertEqual(code, 0, err)
        # MODE=LIVE once for the command, and again right before the prompt.
        self.assertEqual(err.count("MODE=LIVE"), 2)
        self.assertEqual([p["expected"] for p in env.confirmer.prompts], ["1.1 12345 BACK 3.50 2.00"])
        result = json.loads(out)
        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(len(env.router.order_requests), 1)
        self.assertTrue(env.router.rpc_methods)
        logout = [r for r in env.transport.requests if r["url"].endswith("/api/logout")]
        self.assertEqual(len(logout), 1)
        anchor = json.loads(env.runner.items[("betangel-research.betfair.journal-anchor", "default")])
        reopened = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        self.addCleanup(reopened.close)
        self.assertEqual(anchor["seq"], reopened.anchor()["seq"])

    def test_live_kill_while_another_live_prompt_is_pending(self):
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        self.assertEqual(code, 0, err)
        resting = json.loads(out)["orders"][0]["bet_id"]
        inner = {}

        class KillWhilePrompting(gw.FakeConfirmer):
            """The operator, at the prompt of a second order, runs kill from another terminal."""

            def confirm_line(confirmer, expected, *, banner):
                if not inner:
                    inner["result"] = run_main(["kill", "--reason", "emergency"], self.live_deps(env))
                return super().confirm_line(expected, banner=banner)

        code, out, err = run_main(self.live_place(env, price="3.60"),
                                  self.live_deps(env, confirmer=KillWhilePrompting()))
        kill_code, kill_out, kill_err = inner["result"]
        self.assertEqual(kill_code, 0, kill_err)
        self.assertEqual(json.loads(kill_out)["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[resting]["status"], "EXECUTION_COMPLETE")
        # The order being typed is then refused (the kill file and HALT are in force); nothing more is sent.
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: halted"))
        names = [r["method"].split("/")[-1] for r in env.router.order_requests]
        self.assertEqual(names, [gw.PLACE_SUFFIX, gw.CANCEL_SUFFIX])

    def test_cloud_environment_refuses_live(self):
        env = gw.build_live_gateway(self)
        env.journal.close()
        deps = self.live_deps(env, env=dict(support.FORBID_ENV, CI="true"))
        code, out, err = run_main(self.live_place(env), deps)
        self.assertEqual((code, err.strip()), (2, "Betfair order refused: egress_unattested_or_cloud"))
        self.assertEqual(json.loads(out)["failures"], ["egress_unattested_or_cloud"])
        self.assertEqual(env.router.order_requests, [])

    def safe_signals(self, *signums):
        """Handlers that ignore stray signals, so none can end the test run; restored at cleanup."""
        def safety(signum, frame):
            return None

        for signum in signums:
            self.addCleanup(signal.signal, signum, signal.signal(signum, safety))
        return safety

    def test_sigterm_during_a_live_order_still_closes_and_logs_out(self):
        """Regression R3#16: SIGTERM (or SIGHUP, a closed terminal) during the send used to exit 4 with no JSON,
        exactly as a Ctrl-C at the prompt before anything was sent, so a rerun placed a second bet. From the
        first order request on the CLI defers the signal: the report is journaled, the result JSON is printed,
        the gateway still closes (anchor) and logs out, and the exit is 5 with "do not resend"."""
        safety = self.safe_signals(signal.SIGTERM, signal.SIGHUP)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=signum.name):
                env = gw.build_live_gateway(self)
                env.journal.close()
                exchange = env.exchange

                def place_then_signal(params, exchange=exchange, signum=signum):
                    exchange.place_handler = None
                    try:
                        return exchange.place(params)
                    finally:
                        os.kill(os.getpid(), signum)     # held by the gateway until the report is journaled

                exchange.place_handler = place_then_signal
                code, out, err = run_main(self.live_place(env), self.live_deps(env))
                self.assertEqual((code, err.strip().splitlines()[-1]),
                                 (5, f"Betfair stopped after an order request was sent ({signum.name}); result above; "
                                     "do not resend"))
                self.assertNotIn("Betfair stopped: SignalStop", err)
                result = json.loads(out)                                     # the result is printed
                self.assertEqual((result["decision"], result["orders"][0]["state"]), ("accepted", "EXECUTABLE"))
                journal = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
                self.addCleanup(journal.close)
                self.assertEqual([e["kind"] for e in journal.events()][-2:], ["submit", "report"])
                anchor = json.loads(env.runner.items[("betangel-research.betfair.journal-anchor", "default")])
                self.assertEqual(anchor["seq"], journal.anchor()["seq"])          # close() still wrote the anchor
                self.assertEqual(len([r for r in env.transport.requests if r["url"].endswith("/api/logout")]), 1)
                self.assertIs(signal.getsignal(signum), safety)                  # the command restored the handler

    def test_signal_during_an_unknown_send_still_reconciles_it(self):
        """R3#16: a Ctrl-C held during a send whose outcome is unknown used to skip step 15 (reconcile). Now the
        order is still found by its ref after the send, printed as EXECUTABLE with its betId, and the exit is 5."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        exchange = env.exchange

        def placed_then_lost(params):
            exchange.place_handler = None
            exchange.place(params)                                           # Betfair took the order...
            os.kill(os.getpid(), signal.SIGINT)
            raise TransportError("connection_lost", phase="receive")         # ...but the answer was lost

        exchange.place_handler = placed_then_lost
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        self.assertEqual((code, err.strip().splitlines()[-1]),
                         (5, "Betfair stopped after an order request was sent (SIGINT); result above; do not resend"))
        order = json.loads(out)["orders"][0]
        self.assertEqual(order["state"], "EXECUTABLE")
        self.assertEqual(exchange.orders[order["bet_id"]]["customerOrderRef"], order["customer_order_ref"])
        self.assertEqual(len(env.router.order_requests), 1)

    def test_signal_before_send_is_exit_4(self):
        """R3#16: a signal before any order request (here Ctrl-C at the prompt) stays exit 4: nothing was sent."""
        env = gw.build_live_gateway(self)
        env.journal.close()

        class CtrlCAtPrompt(gw.FakeConfirmer):
            def confirm_line(confirmer, expected, *, banner):
                os.kill(os.getpid(), signal.SIGINT)
                return super().confirm_line(expected, banner=banner)

        code, out, err = run_main(self.live_place(env), self.live_deps(env, confirmer=CtrlCAtPrompt()))
        self.assertEqual((code, out, err.strip().splitlines()[-1]),
                         (4, "", "Betfair stopped: SignalStop; inspect local configuration/state"))
        self.assertEqual(env.router.order_requests, [])

    def test_exception_after_a_send_is_exit_5(self):
        """An unexpected error after an order request went out is never reported with the "nothing sent" exits 2
        or 4: exit 5, do not resend, run status or reconcile."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        from betangel_research.betfair.gateway import OrderGateway
        with patch.object(OrderGateway, "_reconcile_refs", side_effect=RuntimeError("synthetic")):
            code, out, err = run_main(self.live_place(env), self.live_deps(env))
        self.assertEqual((code, out, err.strip().splitlines()[-1]),
                         (5, "", "Betfair stopped after an order request was sent (RuntimeError); do not resend; run "
                                 "status or reconcile --mode live"))
        self.assertEqual(len(env.router.order_requests), 1)

    def test_live_place_failed_exits_5(self):
        """Regression R3#15: a live place that Betfair rejected (FAILED) or whose outcome stayed unresolved exited 0
        with "decision": "accepted". Now the JSON is printed (decision is the risk decision; the order view adds
        size_remaining and size_cancelled), then exit 5 with one line per order: FAILED says nothing is live and
        a fresh place is safe (naming the journaled cause of a send that never happened); an unresolved outcome
        says do not resend."""
        cases = (
            (gw_failure("MARKET_NOT_OPEN_FOR_BETTING", "MARKET_SUSPENDED"), "FAILED",
             "FAILED MARKET_NOT_OPEN_FOR_BETTING; nothing is live for this order; a fresh place is safe"),
            (gw.raises(TransportError("connect_failed", phase="connect")), "FAILED",
             "FAILED transport_not_sent (connect_failed); nothing is live for this order; a fresh place is safe"),
            (gw.raises(TransportError("connection_lost", phase="receive")), "ABSENT_UNCONFIRMED",
             "ABSENT_UNCONFIRMED"),
        )
        for handler, state, tail in cases:
            with self.subTest(state=state, tail=tail[:30]):
                env = gw.build_live_gateway(self)
                env.journal.close()
                env.exchange.place_handler = handler
                code, out, err = run_main(self.live_place(env), self.live_deps(env))
                result = json.loads(out)
                order = result["orders"][0]
                self.assertEqual((code, result["decision"], order["state"]), (5, "accepted", state))
                self.assertEqual((order["size_remaining"], order["size_cancelled"]), (0.0 if state == "FAILED" else 2.0,
                                                                                      0.0))
                line = err.strip().splitlines()[-1]
                ref = order["customer_order_ref"]
                if state == "FAILED":
                    self.assertEqual(line, f"Betfair order not live: {ref} {tail}")
                else:
                    self.assertTrue(line.startswith(f"Betfair order sent; outcome not as requested: {ref} {tail} "),
                                    line)
                    self.assertTrue(line.endswith("; do not resend; run status or reconcile --mode live"), line)
        # A place that rests as asked still exits 0.
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        self.assertEqual((code, json.loads(out)["orders"][0]["size_remaining"]), (0, 2.0))

    def test_live_cancel_rejected_by_exchange_exits_5(self):
        """Regression R3#15: a live cancel that Betfair refused (the bet still EXECUTABLE with 2.00 remaining)
        exited 0 with "decision": "accepted". Now exit 5, naming the bet's ref, state and error code; a cancel that
        took the bet off exits 0 and shows size_cancelled."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        self.assertEqual(code, 0, err)
        placed = json.loads(out)["orders"][0]
        cancel = ["cancel", "--market", "1.1", "--bet-id", placed["bet_id"], "--mode", "live"]
        env.exchange.cancel_handler = gw_failure("MARKET_NOT_OPEN_FOR_BETTING")
        code, out, err = run_main(cancel, self.live_deps(env))
        order = json.loads(out)["orders"][0]
        self.assertEqual((code, order["state"], order["size_remaining"]), (5, "EXECUTABLE", 2.0))
        # A cancel never says "do not resend": the bet is still live and the line says to cancel it again.
        self.assertEqual(err.strip().splitlines()[-1],
                         f"Betfair cancel not done: {placed['customer_order_ref']} bet {placed['bet_id']} is still "
                         "live with 2.00 unmatched (MARKET_NOT_OPEN_FOR_BETTING); run cancel again now, or cancel it "
                         "on the Betfair website or in Bet Angel")
        self.assertEqual(env.exchange.orders[placed["bet_id"]]["status"], "EXECUTABLE")
        # An unknown cancel outcome is exit 5 too, even once reconcile finds the bet still resting (not cancelled).
        env.exchange.cancel_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        code, out, err = run_main(cancel, self.live_deps(env))
        order = json.loads(out)["orders"][0]
        self.assertEqual((code, order["state"], order["size_remaining"]), (5, "EXECUTABLE", 2.0))
        self.assertEqual(err.strip().splitlines()[-1],
                         f"Betfair cancel not done: {placed['customer_order_ref']} bet {placed['bet_id']} is still "
                         "live with 2.00 unmatched; run cancel again now, or cancel it on the Betfair website or in "
                         "Bet Angel")
        env.exchange.cancel_handler = None
        code, out, err = run_main(cancel, self.live_deps(env))
        order = json.loads(out)["orders"][0]
        self.assertEqual((code, order["state"], order["size_remaining"], order["size_cancelled"]),
                         (0, "EXECUTION_COMPLETE", 0.0, 2.0))

    def test_live_cancel_not_sent_says_the_bet_is_still_live(self):
        """Regression (operator review): a live cancel that never left the machine (connect or preflight failure,
        zero bytes) printed "Betfair order sent; outcome not as requested ... do not resend; run status or
        reconcile" while the bet stayed live, and reconcile does nothing for an EXECUTABLE order. It now says the
        bet is still live and to cancel it again (a cancel can always be resent), never "sent" or "do not
        resend"; exit 5 keeps "did not end as asked"."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        placed = json.loads(out)["orders"][0]
        cancel = ["cancel", "--market", "1.1", "--bet-id", placed["bet_id"], "--mode", "live"]
        for phase in ("connect", "preflight"):
            with self.subTest(phase=phase):
                env.router.next_response[gw.CANCEL_SUFFIX] = [support.transport_failure(phase, code="refused")]
                code, out, err = run_main(cancel, self.live_deps(env))
                order = json.loads(out)["orders"][0]
                self.assertEqual((code, order["state"], order["error_code"], order["size_remaining"]),
                                 (5, "EXECUTABLE", "cancel_not_processed", 2.0))
                self.assertEqual(env.transport.bytes_written[-1], 0)
                self.assertEqual(err.strip().splitlines()[-1],
                                 f"Betfair cancel not done: {placed['customer_order_ref']} bet {placed['bet_id']} is "
                                 "still live with 2.00 unmatched (cancel_not_processed); run cancel again now, or "
                                 "cancel it on the Betfair website or in Bet Angel")
                self.assertNotIn("order sent", err)
                self.assertNotIn("do not resend", err)
                self.assertEqual(env.exchange.orders[placed["bet_id"]]["status"], "EXECUTABLE")
        # The live probe's cancel that is not processed: the per-order line and the probe line agree (cancel it).
        env.router.next_response[gw.CANCEL_SUFFIX] = [support.transport_failure("connect", code="refused")]
        code, out, err = run_main(["live-probe", "--market", "1.1", "--selection", "12345", "--confirm-live",
                                   env.token], self.live_deps(env))
        self.assertEqual(code, 5, err)
        self.assertNotIn("do not resend", err)
        self.assertIn("Betfair cancel not done: ", err)
        self.assertIn("may still be live; cancel it now", err)

    def test_place_refused_at_the_first_byte_by_a_kill_says_so(self):
        """Regression (operator review): a live place refused at the first-byte gate because a kill was engaged
        printed "nothing is live for this order; a fresh place is safe" with only transport_not_sent; the cause
        was in the journal alone. The line now names the journaled cause and, for a kill, never calls a fresh
        place safe. Nothing was sent (zero order requests)."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        original = cli_module._SignalGuard.order_sent

        def kill_in_another_terminal(guard):
            original(guard)
            engage(env.paths, "other terminal", clock=env.clock)

        with patch.object(cli_module._SignalGuard, "order_sent", kill_in_another_terminal):
            code, out, err = run_main(self.live_place(env), self.live_deps(env))
        result = json.loads(out)
        ref = result["orders"][0]["customer_order_ref"]
        self.assertEqual((code, result["orders"][0]["state"], result["send_exception"]["code"]),
                         (5, "FAILED", "kill_engaged_before_send"))
        self.assertEqual(err.strip().splitlines()[-1],
                         f"Betfair order not live: {ref} FAILED transport_not_sent (kill_engaged_before_send); nothing "
                         "was sent: a kill is engaged, so do not place again until the kill has been dealt with")
        self.assertNotIn("a fresh place is safe", err)
        self.assertEqual(env.router.order_requests, [])

    def test_bets_betfair_already_reduced_are_explained(self):
        """Regression (operator review): after a kill exited 0, its cancelled bets looked like resting bets in
        status (EXECUTABLE, full remaining, no code) and the kill listed none; cancelling one again gave "run
        status or reconcile" (both no-ops); a place whose lost reply resolved to a lapsed bet exited 0; reconcile
        printed final_state EXECUTABLE with no code. Exposure is unchanged (D3/D4), but the kill now lists the
        bets it asked Betfair to cancel and says they stay counted until settle-market; an order Betfair shows
        reduced (exchange_reduction_withheld_until_settle) gets a line pointing to settle-market; reconcile
        entries carry the order's error_code."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        bet = json.loads(out)["orders"][0]["bet_id"]
        code, out, err = run_main(["kill", "--reason", "drill"], self.live_deps(env))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["cancel_requested_bet_ids"], [bet])
        self.assertIn("Betfair kill: cancels were requested for 1 bet(s) of ours", err)
        self.assertIn("until you settle their market with settle-market after it closes", err)
        status = json.loads(run_main(["status"], self.live_deps(env))[1])["journals"]["live"]
        self.assertEqual([o["state"] for o in status["orders"]], ["EXECUTABLE"])          # still counted
        self.assertEqual(status["exposure_pence_total"], 200)
        code, out, err = run_main(["cancel", "--market", "1.1", "--bet-id", bet, "--mode", "live"],
                                  self.live_deps(env))
        order = json.loads(out)["orders"][0]
        self.assertEqual((code, order["state"], order["error_code"]),
                         (5, "EXECUTABLE", "exchange_reduction_withheld_until_settle"))
        self.assertEqual(err.strip().splitlines()[-1],
                         f"Betfair order {order['customer_order_ref']} bet {bet}: Betfair shows part or all of it "
                         "cancelled, lapsed or voided (exchange_reduction_withheld_until_settle); it stays counted at "
                         "worst case and shows as EXECUTABLE until you settle its market with settle-market after "
                         "the market closes; status and reconcile change nothing; cancel it again only if "
                         "orders-current still shows an unmatched part")
        # A place whose reply is lost while Betfair lapses the bet: exit 5 with the same explanation.
        fresh = gw.build_live_gateway(self)
        fresh.journal.close()
        exchange = fresh.exchange

        def lapsed_then_lost(params):
            exchange.place_handler = None
            placed = exchange.place(params)
            exchange.orders[placed["instructionReports"][0]["betId"]].update(
                status="EXECUTION_COMPLETE", sizeLapsed=2.0, sizeRemaining=0.0)
            raise TransportError("read_timeout", phase="receive", may_have_arrived=True)

        exchange.place_handler = lapsed_then_lost
        code, out, err = run_main(self.live_place(fresh), self.live_deps(fresh))
        order = json.loads(out)["orders"][0]
        self.assertEqual((code, order["state"], order["error_code"]),
                         (5, "EXECUTABLE", "exchange_reduction_withheld_until_settle"))
        self.assertIn("until you settle its market with settle-market", err.strip().splitlines()[-1])
        # reconcile --mode live names the evidence in each entry.
        third = gw.build_live_gateway(self)
        third.journal.close()
        lost = {}

        def lost_reply(params):
            third.exchange.place_handler = None
            placed = third.exchange.place(params)
            lost["bet"] = placed["instructionReports"][0]["betId"]
            for method in ("listCurrentOrders", "listClearedOrders"):
                third.exchange.read_failures[method] = [TransportError("read_timeout", phase="receive",
                                                                       may_have_arrived=True) for _ in range(500)]
            raise TransportError("read_timeout", phase="receive", may_have_arrived=True)

        third.exchange.place_handler = lost_reply
        code, out, err = run_main(self.live_place(third), self.live_deps(third))
        self.assertEqual((code, json.loads(out)["orders"][0]["state"]), (5, "UNKNOWN"))
        third.exchange.read_failures.clear()
        third.exchange.orders[lost["bet"]].update(status="EXECUTION_COMPLETE", sizeLapsed=2.0, sizeRemaining=0.0)
        code, out, err = run_main(["reconcile", "--mode", "live"], self.live_deps(third))
        self.assertEqual(code, 0, err)
        entry = json.loads(out)["reconciled"][0]
        self.assertEqual((entry["final_state"], entry["error_code"]),
                         ("EXECUTABLE", "exchange_reduction_withheld_until_settle"))

    def test_cancel_of_a_bet_that_part_matched_first_exits_0(self):
        """Regression (operator review): a full cancel of a bet that part-matched just before the cancel arrived
        ended EXECUTION_COMPLETE (matched 0.50, cancelled 1.50, nothing remaining) but kept
        cancel_size_short_refresh_needed and exited 5 telling the operator to reconcile. It now exits 0 with no
        stale code and an informational line."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        placed = json.loads(out)["orders"][0]
        exchange = env.exchange

        def part_matched_first(params):
            exchange.cancel_handler = None
            exchange.orders[placed["bet_id"]].update(sizeMatched=0.5, sizeRemaining=1.5, averagePriceMatched=3.5)
            return exchange.cancel(params)

        exchange.cancel_handler = part_matched_first
        code, out, err = run_main(["cancel", "--market", "1.1", "--bet-id", placed["bet_id"], "--mode", "live"],
                                  self.live_deps(env))
        order = json.loads(out)["orders"][0]
        self.assertEqual((code, order["state"], order["size_matched"], order["size_cancelled"], order["size_remaining"],
                          order["error_code"]), (0, "EXECUTION_COMPLETE", 0.5, 1.5, 0.0, None), err)
        self.assertEqual(err.strip().splitlines()[-1],
                         f"Betfair cancel: {placed['customer_order_ref']} bet {placed['bet_id']} matched 0.50 before "
                         "the cancel; nothing of it remains unmatched")

    def test_live_probe_cancel_rejected_exits_5(self):
        """Regression R3#15: a live-probe whose immediate cancel Betfair rejected exited 0 and left the LAY 1.01 on
        the exchange. Now exit 5 with a line naming the probe bet and how to cancel it."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        env.exchange.cancel_handler = gw_failure("MARKET_NOT_OPEN_FOR_BETTING")
        code, out, err = run_main(["live-probe", "--market", "1.1", "--selection", "12345", "--confirm-live",
                                   env.token], self.live_deps(env))
        result = json.loads(out)
        bet = result["place"]["orders"][0]["bet_id"]
        self.assertEqual((code, result["cancel"]["orders"][0]["state"]), (5, "EXECUTABLE"))
        self.assertEqual(err.strip().splitlines()[-1],
                         f"Betfair live-probe: probe bet {bet} (LAY 1.00 @ 1.01) may still be live; cancel it now: "
                         f"cancel --mode live --market 1.1 --bet-id {bet}, or on the Betfair website or in Bet Angel")
        self.assertEqual(env.exchange.orders[bet]["status"], "EXECUTABLE")
        # A probe whose cancel works exits 0.
        env.exchange.cancel_handler = None
        code, out, err = run_main(["live-probe", "--market", "1.1", "--selection", "12345", "--confirm-live",
                                   env.token], self.live_deps(env))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["cancel"]["orders"][0]["state"], "EXECUTION_COMPLETE")

    def lapsed_row(self, env, bet, ref):
        """A cleared LAPSED row for one of our bets (lapsed and cancelled bets show there before settlement, F36)."""
        env.exchange.cleared["LAPSED"].append({"betId": bet, "marketId": "1.1", "selectionId": gw.SELECTION,
                                               "handicap": 0.0, "side": "BACK", "priceRequested": 3.5,
                                               "sizeCancelled": 0.0, "customerOrderRef": ref,
                                               "customerStrategyRef": "bar-manual", "settledDate": env.clock.utc_now(),
                                               "betCount": 1})

    def test_orders_betfair_lists_only_as_cleared_are_explained(self):
        """Round 4: an order resolved from a cleared row (EXECUTABLE, cleared_row_bet_exists_worst_case: Betfair
        lists the bet only among its cleared orders) made a place exit 0 as if it were resting, and a cancel say
        "is still live ... run cancel again now", which only looped into a consecutive_unknown_outcomes HALT. Both
        now print the cleared-row line (not resting, counted at worst case until settle-market, cancel again only
        if orders-current shows an unmatched part) and exit 5."""
        def cleared_line(order):
            return (f"Betfair order {order['customer_order_ref']} bet {order['bet_id']}: Betfair lists this bet only "
                    "among its cleared orders (lapsed, cancelled, voided or settled), not as a current bet "
                    "(cleared_row_bet_exists_worst_case); it is not resting; it stays counted at worst case and shows "
                    "as EXECUTABLE until you settle its market with settle-market after the market closes; status "
                    "and reconcile change nothing; cancel it again only if orders-current still shows an unmatched "
                    "part")

        # F25: a marketVersion change answered FAILURE / BET_TAKEN_OR_LAPSED with the betId echoed; Betfair lapsed it.
        env = gw.build_live_gateway(self)
        env.journal.close()
        exchange = env.exchange

        def lapsed_on_version(params):
            instruction = params["instructions"][0]
            bet = exchange.new_bet_id()
            self.lapsed_row(env, bet, instruction["customerOrderRef"])
            return {"customerRef": params["customerRef"], "status": "FAILURE", "errorCode": "BET_ACTION_ERROR",
                    "marketId": params["marketId"],
                    "instructionReports": [{"status": "FAILURE", "errorCode": "BET_TAKEN_OR_LAPSED",
                                            "instruction": instruction, "betId": bet}]}

        exchange.place_handler = lapsed_on_version
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        order = json.loads(out)["orders"][0]
        self.assertEqual((code, order["state"], order["error_code"], order["size_remaining"]),
                         (5, "EXECUTABLE", "cleared_row_bet_exists_worst_case", 2.0))
        self.assertEqual(err.strip().splitlines()[-1], cleared_line(order))
        # A resting bet lapses at the off (the journal, never refreshed in live, still says EXECUTABLE): cancel.
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        placed = json.loads(out)["orders"][0]
        env.exchange.orders.pop(placed["bet_id"])
        self.lapsed_row(env, placed["bet_id"], placed["customer_order_ref"])
        code, out, err = run_main(["cancel", "--market", "1.1", "--bet-id", placed["bet_id"], "--mode", "live"],
                                  self.live_deps(env))
        order = json.loads(out)["orders"][0]
        self.assertEqual((code, order["state"], order["error_code"]),
                         (5, "EXECUTABLE", "cleared_row_bet_exists_worst_case"))
        self.assertEqual(err.strip().splitlines()[-1], cleared_line(order))
        self.assertNotIn("still live", err)
        self.assertNotIn("run cancel again now", err)

    def test_a_stopped_cancel_never_says_do_not_resend(self):
        """Round 4: a signal or an unexpected exception after a live cancel request printed "do not resend" (the
        advice for a place) while the bet could still be live; and a cancel whose capability mint refused (nothing
        sent) was reported as stopped "after an order request was sent". Now a cancel's stop line says the bet may
        still be live and to cancel it again, and on_send runs after the mint, right before the request."""
        self.safe_signals(signal.SIGINT)
        env = gw.build_live_gateway(self)
        env.journal.close()
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        placed = json.loads(out)["orders"][0]
        cancel = ["cancel", "--market", "1.1", "--bet-id", placed["bet_id"], "--mode", "live"]
        rejected = gw_failure("MARKET_NOT_OPEN_FOR_BETTING")

        def rejected_with_ctrl_c(params):
            os.kill(os.getpid(), signal.SIGINT)
            return rejected(params)

        env.exchange.cancel_handler = rejected_with_ctrl_c
        code, out, err = run_main(cancel, self.live_deps(env))
        self.assertEqual(code, 5, err)
        self.assertNotIn("do not resend", err)
        self.assertEqual(err.strip().splitlines()[-1],
                         "Betfair stopped after a cancel request was sent (SIGINT); result above; the bet may still "
                         "be live: check orders-current and cancel it again if it is, or cancel it on the Betfair "
                         "website or in Bet Angel")
        env.exchange.cancel_handler = rejected
        from betangel_research.betfair.gateway import OrderGateway
        with patch.object(OrderGateway, "_reconcile_refs", side_effect=RuntimeError("synthetic")):
            env.exchange.cancel_handler = gw.raises(TransportError("connection_lost", phase="receive"))
            code, out, err = run_main(cancel, self.live_deps(env))
        self.assertEqual((code, out), (5, ""))
        self.assertEqual(err.strip().splitlines()[-1],
                         "Betfair stopped after a cancel request was sent (RuntimeError); the bet may still be live: "
                         "check orders-current and cancel it again if it is, or cancel it on the Betfair website or "
                         "in Bet Angel")
        self.assertEqual(env.exchange.orders[placed["bet_id"]]["status"], "EXECUTABLE")
        # A mint refusal sends nothing: a signal then is the plain exit 4, never "after an order request was sent".
        env.exchange.cancel_handler = None
        from betangel_research.betfair.errors import OrderRefused
        requests = len(env.router.order_requests)

        def refuse_mint(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGINT)
            raise OrderRefused("capability_expired")

        with patch.object(env.guard.__class__, "issue", side_effect=refuse_mint):
            code, out, err = run_main(cancel, self.live_deps(env))
        self.assertEqual((code, err.strip().splitlines()[-1]),
                         (4, "Betfair stopped: SignalStop; inspect local configuration/state"))
        self.assertEqual(len(env.router.order_requests), requests)

    def test_printed_live_commands_carry_bet_angel_paused_while_unverified(self):
        """Round 4: with coexistence.bet_angel unverified (the example config, until T3), every printed live
        command left out --bet-angel-paused, so copying it gave exit 2 while the probe bet stayed live. Printed
        commands now carry the flag, the coexistence refusal names it, and the probe's cancel line, run as
        printed, cancels the bet. A cancel run without --mode live says it ran in paper mode."""
        env = gw.build_live_gateway(self, overrides={"coexistence": {"bet_angel": "unverified"}})
        env.journal.close()
        env.exchange.cancel_handler = gw_failure("MARKET_NOT_OPEN_FOR_BETTING")
        code, out, err = run_main(["live-probe", "--market", "1.1", "--selection", "12345", "--confirm-live",
                                   env.token, "--bet-angel-paused"], self.live_deps(env))
        self.assertEqual(code, 5, err)
        bet = json.loads(out)["place"]["orders"][0]["bet_id"]
        line = err.strip().splitlines()[-1]
        printed = f"cancel --mode live --bet-angel-paused --market 1.1 --bet-id {bet}"
        self.assertIn(printed, line)
        env.exchange.cancel_handler = None
        code, out, err = run_main(printed.split(), self.live_deps(env))          # run exactly as printed
        self.assertEqual(code, 0, err)
        self.assertEqual(env.exchange.orders[bet]["status"], "EXECUTION_COMPLETE")
        # An unknown place outcome: the advice's reconcile carries the flag and works as printed.
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        code, out, err = run_main(self.live_place(env) + ["--bet-angel-paused"], self.live_deps(env))
        self.assertEqual(code, 5, err)
        self.assertTrue(err.strip().endswith("do not resend; run status or reconcile --mode live --bet-angel-paused"),
                        err)
        code, out, err = run_main("reconcile --mode live --bet-angel-paused".split(), self.live_deps(env))
        self.assertNotIn("bet_angel_coexistence_unverified", err)
        # The paper default: a live bet id is unknown to the paper journal, and the line says why.
        code, out, err = run_main(["cancel", "--market", "1.1", "--bet-id", bet], self.live_deps(env))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["mode"], "paper")
        self.assertIn("this ran in paper mode (the default): for a live bet add --mode live", err)

    def test_place_after_an_unknown_order_resolves_live_at_open_is_refused(self):
        """Round 4 (major): after an UNKNOWN place the operator is told "do not resend". A reconcile that could not
        resolve it exited 0 "reconciled" with nothing on stderr; the same order typed again was then sent once its
        open() found the first one live, silently lifting the unresolved-orders gate: two bets. Now reconcile
        exits 5 with one line per order still unresolved, and a place (or live-probe) whose open() found an
        earlier UNKNOWN order live is refused (exit 2, nothing sent) with a line naming that order."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        exchange = env.exchange

        def placed_then_lost(params):
            exchange.place_handler = None
            exchange.place(params)
            for method in ("listCurrentOrders", "listClearedOrders"):
                exchange.read_failures[method] = [TransportError("read_timeout", phase="receive",
                                                                 may_have_arrived=True) for _ in range(500)]
            raise TransportError("read_timeout", phase="receive", may_have_arrived=True)

        exchange.place_handler = placed_then_lost
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        first = json.loads(out)["orders"][0]
        self.assertEqual((code, first["state"]), (5, "UNKNOWN"))
        code, out, err = run_main(["reconcile", "--mode", "live"], self.live_deps(env))
        result = json.loads(out)
        self.assertEqual((code, result["status"], [u["state"] for u in result["unresolved"]]),
                         (5, "reconciled", ["UNKNOWN"]))
        self.assertTrue(err.strip().splitlines()[-1].startswith(
            f"Betfair reconcile: {first['customer_order_ref']} in market 1.1 is still UNKNOWN ("), err)
        self.assertIn("new orders stay blocked; do not resend; run reconcile --mode live again later", err)
        exchange.read_failures.clear()
        # The operator types the same order again; its open() finds the first one live: refused, nothing sent.
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        result = json.loads(out)
        self.assertEqual((code, result["decision"], result["refusal"]["rule"]),
                         (2, "refused", "earlier_unknown_resolved_at_open"))
        bet = [b for b, o in exchange.orders.items() if o["customerOrderRef"] == first["customer_order_ref"]][0]
        self.assertEqual(err.strip().splitlines()[-2:],
                         ["Betfair order refused: earlier_unknown_resolved_at_open",
                          f"Betfair: earlier order {first['customer_order_ref']} in market 1.1 had an unknown outcome "
                          f"and Betfair shows it as a bet: EXECUTABLE bet {bet} matched 0.00 unmatched 2.00; this "
                          "order was not sent: check status and orders-current, then run the command again only if "
                          "you still want it"])
        self.assertEqual(len(exchange.orders), 1)
        self.assertEqual(len(env.router.order_requests), 1)
        # Run again on purpose: nothing earlier is unresolved now, so it is sent.
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        self.assertEqual((code, json.loads(out)["decision"]), (0, "accepted"), err)
        self.assertEqual(len(exchange.orders), 2)

    def test_accept_and_resume_banners_say_nothing_is_sent(self):
        """Round 4: reconcile --accept and live resume showed LIVE_BANNER ("this sends a real order") though they
        send nothing, and accept showed only the ref while it stops counting the order's worst case. Their banners
        now say no order is sent; accept's names the order, its state and the worst case that stops counting."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        lost = json.loads(out)["orders"][0]
        self.assertEqual((code, lost["state"]), (5, "ABSENT_UNCONFIRMED"))
        ref = lost["customer_order_ref"]
        code, out, err = run_main(["reconcile", "--mode", "live", "--accept", ref, "--confirm-live", env.token],
                                  self.live_deps(env))
        self.assertEqual(code, 0, err)
        prompt = env.confirmer.prompts[-1]
        self.assertEqual(prompt["expected"], f"ACCEPT ABSENT {ref}")
        self.assertEqual(prompt["banner"],
                         f"MODE=LIVE: ACCEPT ABSENT sends no order. It stops counting order {ref} (market 1.1 sel "
                         "12345 BACK @ 3.50 size 2.00, now ABSENT_UNCONFIRMED, worst case \u00a32.00) as possibly "
                         "live, and lets new orders through. Do this only after the Betfair website or Bet Angel shows "
                         "no such bet.")
        self.assertNotIn("sends a real order", prompt["banner"])
        code, out, err = run_main(["kill", "--reason", "drill"], self.live_deps(env))
        os.unlink(env.paths.kill_file)
        code, out, err = run_main(["resume", "--mode", "live", "--confirm-live", env.token], self.live_deps(env))
        self.assertEqual(code, 0, err)
        banner = env.confirmer.prompts[-1]["banner"]
        self.assertTrue(banner.startswith("MODE=LIVE: RESUME sends no order. It clears the journal HALT as of journal "
                                          "seq "), banner)
        self.assertNotIn("sends a real order", banner)

    def test_live_partial_cancel_refused_with_message_and_no_side_effects(self):
        """Amendment A3 (D1): live cancels are full cancels; --size-reduction with --mode live is refused first,
        before the gateway, the Keychain, the journal, the lock or any request. Paper keeps reductions."""
        expected = ("Betfair order refused: live_partial_cancel_removed_v1: live cancels are full cancels in v1; "
                    "nothing was sent. To reduce a bet, cancel it and place a new one.")
        for reduction in ("1.00", "not-money"):
            with self.subTest(reduction=reduction):
                code, out, err = run_main(["cancel", "--mode", "live", "--market", "1.1", "--bet-id", "300000000002",
                                           "--size-reduction", reduction], self.deps)
                self.assertEqual((code, err.strip(), out), (2, expected, ""))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.transport.requests, [])
        self.assertFalse(self.paths.journal_path("live").exists())
        self.assertFalse(self.paths.gateway_lock.exists())
        placed = json.loads(run_main(self.place_args(), self.deps)[1])["orders"][0]
        code, out, err = run_main(["cancel", "--market", "1.1", "--bet-id", placed["bet_id"], "--size-reduction",
                                   "1.00"], self.deps)
        self.assertEqual(code, 0, err)
        self.assertEqual((json.loads(out)["orders"][0]["size_cancelled"], json.loads(out)["orders"][0]["state"]),
                         (1.0, "EXECUTABLE"))

    def test_confirm_live_with_paper_mode_refused(self):
        """Regression R3#18: `resume --confirm-live TOKEN` without --mode live resumed the paper journal (exit 0,
        "resumed") while the live journal stayed halted. --confirm-live with a paper mode is now refused, exit 2,
        before anything runs."""
        run_main(["kill", "--reason", "x", "--bet-angel-paused"], self.deps)
        os.unlink(self.paths.kill_file)
        paper_before = run_main(["journal"], self.deps)[1]
        calls = len(self.runner.calls)
        for argv in (["resume", "--confirm-live", "live-x"], ["resume", "--mode", "paper", "--confirm-live", "live-x"],
                     self.place_args("--confirm-live", "live-x"),
                     ["cancel", "--market", "1.1", "--bet-id", "1", "--confirm-live", "live-x"],
                     ["reconcile", "--confirm-live", "live-x"],
                     ["replace", "--market", "1.1", "--bet-id", "1", "--new-price", "3.60", "--confirm-live", "x"]):
            with self.subTest(argv=argv[:3]):
                code, out, err = run_main(argv, self.deps)
                self.assertEqual((code, out, err.strip()), (2, "", "Betfair input error: confirm_live_needs_mode_live"))
        self.assertEqual(run_main(["journal"], self.deps)[1], paper_before)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(self.transport.requests, [])
        self.assertFalse(self.paths.journal_path("live").exists())

    def test_help_texts_state_live_limits(self):
        """Regression R3#18: cancel's help promised a typed confirmation it never asks for, and resume's omitted
        --mode live. The help now states each command's real live gates."""
        self.assertNotIn("typing", HELP["cancel"])
        for phrase in ("live needs --mode live and A4, A5, A8, A10, A12, A13 plus a login", "allowed while halted or "
                       "killed", "full cancels (no --size-reduction)", "sent at once with no typed confirmation"):
            self.assertIn(phrase, HELP["cancel"])
        self.assertIn("live needs --mode live --confirm-live TOKEN, then typing RESUME LIVE", HELP["resume"])
        self.assertIn("without waiting for any lock", HELP["kill"])
        self.assertIn("exit 3 means cancel open bets on the Betfair website or in Bet Angel now", HELP["kill"])
        code, out, _ = run_main(["cancel", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("sent at once with no typed confirmation", " ".join(out.split()))
        doc = " ".join(cli_module.__doc__.split())
        for phrase in ("a live cancel is a full cancel of our own bets", "is sent at once with no typed line",
                       "confirm_live_needs_mode_live", "5 (live) the command began an order request", "SIGHUP"):
            self.assertIn(phrase, doc)

    def test_live_probe_takes_no_mode_flag(self):
        from betangel_research.betfair.cli import build_parser
        parsed = build_parser().parse_args(["live-probe", "--market", "1.1", "--selection", "1"])
        self.assertFalse(hasattr(parsed, "mode"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["live-probe", "--market", "1.1", "--selection", "1", "--mode", "paper"])

    def test_live_replace_refused_with_message_and_no_side_effects(self):
        """Regression R3#2 (moot by D2, amendment A3): live replace is removed. The CLI refuses it first, with
        one operator line, before parsing the price, the coexistence check, arming, the Keychain, the
        journal, the lock or any request."""
        expected = ("Betfair order refused: live_replace_removed_v1: live replace is not available in v1; nothing "
                    "was sent. To move a bet, cancel it (cancel --mode live --market M --bet-id B) and place a new "
                    "one (place --mode live ...).")
        for price, extra in (("3.60", ["--confirm-live", "live-x", "--bet-angel-paused"]), ("not-a-price", [])):
            with self.subTest(price=price):
                code, out, err = run_main(["replace", "--mode", "live", "--market", "1.1", "--bet-id", "300000000001",
                                           "--new-price", price, *extra], self.deps)
                self.assertEqual((code, err.strip(), out), (2, expected, ""))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.transport.requests, [])
        self.assertFalse(self.paths.journal_path("live").exists())
        self.assertFalse(self.paths.gateway_lock.exists())
        self.assertEqual(self.deps["confirmer"].prompts, [])
        # A real live fixture is refused the same way: no order request, no Keychain secret read.
        env = gw.build_live_gateway(self)
        env.journal.close()
        before = (len(env.runner.calls), len(env.transport.requests))
        code, _, err = run_main(["replace", "--mode", "live", "--market", "1.1", "--bet-id", "300000000001",
                                 "--new-price", "3.60", "--confirm-live", env.token], self.live_deps(env))
        self.assertEqual((code, err.strip()), (2, expected))
        self.assertEqual((len(env.runner.calls), len(env.transport.requests)), before)
        self.assertEqual(env.router.order_requests, [])

    def test_live_probe_has_no_replace_step_flag(self):
        from betangel_research.betfair.cli import build_parser
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["live-probe", "--market", "1.1", "--selection", "1", "--replace-step"])
        code, out, err = run_main(["live-probe", "--market", "1.1", "--selection", "12345", "--replace-step"],
                                  self.deps)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unrecognized arguments: --replace-step", err)
        self.assertEqual(self.transport.requests, [])
        self.assertIn("the replace step was removed with live replace in v1", HELP["live-probe"])

    def test_settle_market_cli_end_to_end(self):
        env = gw.build_live_gateway(self, overrides={"coexistence": {"bet_angel": "unverified"}})
        env.journal.close()
        code, out, err = run_main(self.live_place(env) + ["--bet-angel-paused"], self.live_deps(env))
        self.assertEqual(code, 0, err)
        bet = json.loads(out)["orders"][0]["bet_id"]
        settle = ["settle-market", "--market", "1.1", "--confirm-live", env.token]
        # Bet Angel coexistence is unverified until T3: the flag is required, checked before anything else.
        code, out, err = run_main(settle, self.live_deps(env))
        self.assertEqual((code, err.strip(), out), (2, COEXISTENCE_REFUSAL, ""))
        # The market is not settled on Betfair yet: our bet is still current (refused with advice, exit 2).
        env.exchange.market_status = "CLOSED"
        code, out, err = run_main(settle + ["--bet-angel-paused"], self.live_deps(env))
        self.assertEqual(code, 2)
        self.assertEqual(err.strip().splitlines()[-1],
                         "Betfair order refused: settle_market_bets_still_current; check these bets' status on the "
                         "Betfair website or in Bet Angel and settle again once Betfair has settled them")
        refused = json.loads(out)
        self.assertEqual((refused["status"], refused["rule"], refused["detail"]["bet_ids"]),
                         ("refused", "settle_market_bets_still_current", [bet]))
        env.exchange.orders.clear()
        code, out, err = run_main(settle + ["--bet-angel-paused"], self.live_deps(env))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["status"], result["booked_realized_loss_pence"]), ("settled", 200))
        self.assertEqual(env.confirmer.prompts[-1]["expected"], "SETTLE MARKET 1.1")
        self.assertIn("MODE=LIVE", err)
        self.assertEqual(len(env.router.order_requests), 1)            # the place; settling sends nothing
        self.assertEqual(len([r for r in env.transport.requests if r["url"].endswith("/api/logout")]), 3)
        journal = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        self.addCleanup(journal.close)
        self.assertEqual([e["kind"] for e in journal.events()].count("settle"), 1)
        anchor = json.loads(env.runner.items[("betangel-research.betfair.journal-anchor", "default")])
        self.assertEqual(anchor["seq"], journal.anchor()["seq"])
        # Nothing is left to settle: refused before the prompt.
        prompts = len(env.confirmer.prompts)
        code, out, err = run_main(settle + ["--bet-angel-paused"], self.live_deps(env))
        self.assertEqual((code, err.strip().splitlines()[-1]),
                         (2, "Betfair order refused: settle_market_nothing_to_settle"))
        self.assertEqual(len(env.confirmer.prompts), prompts)

    def test_settle_market_unresolved_advice_names_accept(self):
        """Regression (operator review): settle_market_unresolved_orders advised only "resolve them first with
        reconcile --mode live", but an order still absent after that stays ABSENT_UNCONFIRMED (still_absent) and
        only reconcile --accept clears it, so following the advice looped. The line names both steps."""
        env = gw.build_live_gateway(self)
        env.journal.close()
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        code, out, err = run_main(self.live_place(env), self.live_deps(env))
        lost = json.loads(out)["orders"][0]
        self.assertEqual((code, lost["state"]), (5, "ABSENT_UNCONFIRMED"))
        env.exchange.market_status = "CLOSED"
        code, out, err = run_main(["reconcile", "--mode", "live"], self.live_deps(env))
        self.assertEqual(json.loads(out)["reconciled"][0]["final_state"], "ABSENT_UNCONFIRMED")      # still absent
        prompts = len(env.confirmer.prompts)
        code, out, err = run_main(["settle-market", "--market", "1.1", "--confirm-live", env.token],
                                  self.live_deps(env))
        self.assertEqual(code, 2)
        self.assertEqual(err.strip().splitlines()[-1],
                         "Betfair order refused: settle_market_unresolved_orders; resolve them first with reconcile "
                         "--mode live; an order still ABSENT_UNCONFIRMED after that needs reconcile --mode live "
                         "--accept <ref> --confirm-live TOKEN; nothing was settled")
        self.assertEqual(json.loads(out)["detail"], {"refs": [lost["customer_order_ref"]]})
        self.assertEqual(len(env.confirmer.prompts), prompts)          # refused before anything is typed

    def test_status_reports_rate_counters(self):
        run_main(self.place_args(), self.deps)
        code, out, err = run_main(["status"], self.deps)
        self.assertEqual(code, 0, err)
        rates = json.loads(out)["journals"]["paper"]["rates"]
        self.assertEqual((rates["submitted_instructions_last_minute"], rates["submitted_instructions_last_hour"],
                          rates["orders_today"]), (1, 1, 1))
        self.assertEqual(sum(rates["transactions_by_clock_hour"].values()), 1)


class ReadAndRecordCliTests(unittest.TestCase):
    """Read commands and the recorder through the real session, JSON-RPC and stream clients over fakes."""

    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / "home"
        self.home.mkdir()
        self.clock = support.FakeClock()
        self.exchange = gw.SimExchange(self.clock)
        self.router = gw.FakeBetfair(self.exchange)
        self.transport = gw.RoutingTransport(self.router)
        self.deps = {"home": self.home, "keychain_runner": support.FakeKeychainRunner(), "clock": self.clock,
                     "http_transport": self.transport}
        self.paths = StatePaths(home=self.home, env=dict(support.FORBID_ENV))
        support.write_test_config(self.paths, {"credentials": {"login_method": "interactive", "cert_path": None,
                                                               "key_path": None}})

    def test_read_commands_print_personal_use_status_and_out(self):
        commands = [["event-types"], ["catalogue", "--event-type", "7", "--countries", "GB", "--market-types", "WIN"],
                    ["book", "--market", "1.1"], ["book", "--market", "1.1", "--depth", "3"], ["orders-current"],
                    ["orders-cleared", "--status", "SETTLED", "--from", "2020-01-01T00:00:00Z"], ["funds"],
                    ["account", "--out"]]
        for argv in commands:
            with self.subTest(command=argv[0]):
                code, out, err = run_main(argv + ["--bet-angel-paused"], self.deps)
                self.assertEqual(code, 0, err)
                document = json.loads(out)
                self.assertEqual(document["status"], "betfair_read_response_personal_use_not_for_redistribution")
                self.assertEqual((document["key_type"], document["app_key_slot"]), ("delayed", "delayed"))
        books = json.loads(run_main(["book", "--market", "1.1", "--depth", "3", "--bet-angel-paused"], self.deps)[1])
        self.assertEqual(books["result"][0]["fetched_by"], "read_command")
        self.assertTrue(books["result"][0]["catalogue_present"])
        account = json.loads(run_main(["account", "--bet-angel-paused"], self.deps)[1])["result"]
        self.assertEqual(set(account), {"currencyCode", "countryCode", "discountRate", "pointsBalance"})
        rpc = [r for r in self.transport.requests if r["url"].endswith("json-rpc/v1")]
        self.assertTrue(rpc)
        self.assertEqual({self.transport.header_text(r, "X-Application") for r in rpc}, {"synthetic-delayed-key-0001"})
        self.assertEqual(self.router.order_requests, [])
        logins = [r for r in self.transport.requests if r["url"].endswith("/api/login")]
        logouts = [r for r in self.transport.requests if r["url"].endswith("/api/logout")]
        self.assertEqual(len(logins), len(logouts))
        written = sorted(p.name for p in self.paths.reads_dir.rglob("*.json"))
        self.assertEqual(written, ["receipt.json", "response.json"])
        for path in self.paths.root.rglob("*"):
            if path.is_file():
                text = path.read_bytes().decode("utf-8", "replace")
                for secret in (gw.SESSION_TOKEN, "synthetic-delayed-key-0001", "synthetic-password-0001",
                               "Firstname-never-kept", "Lastname-never-kept"):
                    self.assertNotIn(secret, text)
        code, _, err = run_main(["funds"], self.deps)           # coexistence unverified needs the flag
        self.assertEqual((code, err.strip()), (2, COEXISTENCE_REFUSAL))

    def test_paper_step_fetch_book_on_the_delayed_key_flags_warns_and_exits_0(self):
        """T8a through the CLI: paper --fetch-book uses the delayed key, whose REST tradedVolume [] gave
        paper-step flags {} and nothing on stderr. Now the order is flagged, one warning steers to record +
        paper-replay, the exit code stays 0 and nothing is sent."""
        self.exchange.delayed = True
        code, out, err = run_main(["place", "--market", "1.1", "--selection", str(gw.SELECTION), "--side", "BACK",
                                   "--price", "3.55", "--size", "2.00", "--fetch-book", "--bet-angel-paused"],
                                  self.deps)
        self.assertEqual((code, json.loads(out)["orders"][0]["state"], err), (0, "EXECUTABLE", ""))
        steps = []
        for _ in range(2):
            self.clock.advance(2)
            code, out, err = run_main(["paper-step", "--market", "1.1", "--fetch-book", "--bet-angel-paused"],
                                      self.deps)
            steps.append((code, list(json.loads(out)["flags"].values()), err.splitlines()))
        self.assertEqual(steps[0], (0, [["passive_fill_skipped_pre_activation_interval"]], []))
        self.assertEqual(steps[1], (0, [["passive_fill_unavailable_delayed_rest_trd_empty"]], [
            "Betfair warning: passive_fill_unavailable_delayed_rest_trd_empty (1 order(s)): the delayed-key book "
            "carried an empty traded ladder, which is not evidence that nothing traded; for passive fills run "
            "record, then paper-replay on its segments"]))
        self.assertEqual(self.router.order_requests, [])

    def test_doctor_network_statuses_only(self):
        code, out, err = run_main(["doctor", "--network", "--bet-angel-paused"], self.deps)
        self.assertEqual(code, 0, err)
        network = json.loads(out)["network"]
        self.assertEqual(network, {"login_status": "SUCCESS", "can_bet": True, "keep_alive_status": "SUCCESS",
                                   "keep_alive_error": None, "logout_status": "SUCCESS"})
        self.assertNotIn(gw.SESSION_TOKEN, out)

    def test_record_cli_wiring(self):
        script = fx.handshake() + [fx.change(lambda sid: fx.image(sid, fx.pt_at(-300)))]
        server = fx.FakeStreamServer([script], self.clock)
        deps = dict(self.deps, stream_socket_factory=server.factory)
        code, out, err = run_main(["record", "--max-seconds", "60", "--bet-angel-paused"], deps)
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(lines[-1]["summary"]["status"], "betfair_stream_recording_not_profitability_evidence")
        self.assertTrue(any(line.get("event") == "segment_opened" or "segment_opened" in json.dumps(line)
                            for line in lines))
        sessions = list(self.paths.recordings_dir.glob("rec-*"))
        self.assertEqual(len(sessions), 1)
        self.assertTrue((sessions[0] / "receipt.json").exists())
        self.assertEqual(self.router.order_requests, [])
        # The session goes to the stream server in the authentication line only, never into any file.
        self.assertEqual(server.all_sent().count(gw.SESSION_TOKEN.encode()), 1)
        for path in self.paths.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(gw.SESSION_TOKEN.encode(), path.read_bytes())
        self.assertNotIn(gw.SESSION_TOKEN, out + err)


class SubprocessCliTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = self.root / "state"
        self.paths = StatePaths(home=self.root / "home", root_override=self.state, env=dict(support.FORBID_ENV))
        (self.root / "home").mkdir()
        self.config = support.write_test_config(self.paths, path=self.root / "config.json")

    def base(self):
        return ["--state-root", self.state, "--config", self.config]

    def write_replay_inputs(self):
        t0 = timestamp_ms("2020-01-01T12:00:00Z")
        definition = {"marketTime": "2020-01-01T12:20:00.000Z", "status": "OPEN", "inPlay": False, "betDelay": 0,
                      "bettingType": "ODDS", "marketType": "WIN", "eventTypeId": "7", "numberOfWinners": 1,
                      "version": 1, "priceLadderDefinition": {"type": "CLASSIC"},
                      "runners": [{"id": 12345, "status": "ACTIVE"}]}
        line = {"op": "mcm", "pt": t0, "mc": [{"id": "1.1", "img": True, "marketDefinition": definition,
                                               "rc": [{"id": 12345, "batb": [[0, 3.5, 10.0]],
                                                       "batl": [[0, 3.55, 10.0]]}]}]}
        segment = self.root / "seg-000.jsonl"
        segment.write_text(json.dumps(line) + "\n")
        order_file = self.root / "orders.json"
        order_file.write_text(json.dumps({"schema_version": 1, "orders": [
            {"submit_pt": t0, "market_id": "1.1", "selection_id": 12345, "side": "BACK", "price": "3.50",
             "size": "2.00"}]}))
        return segment, order_file

    def test_help_lists_subcommands_with_limits(self):
        completed = run_cli("--help")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for name in HELP:
            self.assertIn(name, completed.stdout)
        for name, text in HELP.items():
            with self.subTest(command=name):
                code, out, _ = run_main([name, "--help"])
                self.assertEqual(code, 0)
                self.assertIn(" ".join(text.split()[:6]), " ".join(out.split()))
                self.assertTrue(any(word in text.lower() for word in ("paper", "read-only", "offline", "live",
                                                                      "runs nothing", "never")), text)
        # Amendment A3 (D2, D3): replace is paper only, live-probe has no replace step, settle-market is live only.
        self.assertTrue(HELP["replace"].startswith("Paper only"))
        self.assertIn("live replace was removed in v1", HELP["replace"])
        self.assertNotIn("--replace-step", run_main(["live-probe", "--help"])[1])
        self.assertTrue(HELP["settle-market"].startswith("Live only: settle one CLOSED market by hand"))
        self.assertIn("--bet-angel-paused until T3", HELP["settle-market"])
        self.assertIn("live cancels are full cancels (no --size-reduction)", HELP["cancel"])

    def test_paper_replay_writes_report_summary_and_ledger(self):
        segment, order_file = self.write_replay_inputs()
        out = self.root / "outputs" / "run-1"
        completed = run_cli(*self.base(), "paper-replay", "--input", segment, "--orders", order_file, "--out", out)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["status"], "paper_simulation_not_execution_evidence")
        self.assertIsNone(summary["economic_result"])
        for name in ("report.json", "summary.md", "journal.sqlite"):
            self.assertTrue((out / name).exists(), name)
        ledger = [json.loads(line) for line in (out.parent / "run-ledger.jsonl").read_text().splitlines()]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(set(ledger[0]), {"created_at", "experiment_id", "source_sha256", "config_sha256",
                                          "code_sha256", "report", "report_sha256", "status"})
        self.assertTrue(ledger[0]["experiment_id"].startswith("paper-replay:"))

    def test_existing_out_dir_refused(self):
        segment, order_file = self.write_replay_inputs()
        out = self.root / "outputs" / "exists"
        out.mkdir(parents=True)
        completed = run_cli(*self.base(), "paper-replay", "--input", segment, "--orders", order_file, "--out", out)
        self.assertEqual(completed.returncode, 2)
        self.assertTrue(completed.stderr.startswith("Betfair input error: "), completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(list(out.iterdir()), [])

    def test_verify_recording_cli(self):
        paths = support.temp_state_paths(self)
        clock = support.FakeClock()
        session = fx.FakeSession()
        script = fx.handshake() + [fx.change(lambda sid: fx.image(sid, fx.pt_at(-300)))]
        server = fx.FakeStreamServer([script], clock)
        from betangel_research.betfair.recorder import StreamRecorder
        recorder = StreamRecorder(fx.recorder_config(paths, None), paths=paths, session=session,
                                  stream_factory=lambda: fx.make_client(server, session, clock),
                                  market_lister=fx.lister(("1.1",)), clock=clock, rng=random.Random(7),
                                  emit=lambda event: None)
        receipt = recorder.run(stop_requested=server.exhausted)
        session_dir = paths.recordings_dir / receipt["session_id"]
        completed = run_cli("verify-recording", "--session", session_dir)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "verified")
        segment = session_dir / "seg-000.jsonl"
        segment.write_bytes(segment.read_bytes().replace(b'"pt"', b'"pT"', 1))
        tampered = run_cli("verify-recording", "--session", session_dir)
        self.assertEqual(tampered.returncode, 2)
        self.assertEqual(json.loads(tampered.stdout)["status"], "failed")
        self.assertEqual(tampered.stderr.strip(), "Betfair input error: recording_failed_verification")

    def test_journal_verify_cli(self):
        self.paths.ensure()
        journal = OrderJournal(self.paths.journal_path("paper"), mode="paper", clock=support.FakeClock())
        journal.append("note", {"text": "synthetic"})
        journal.append("note", {"text": "synthetic 2"})
        journal.close()
        completed = run_cli(*self.base(), "journal", "--verify")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["verify"]["status"], "ok")
        from betangel_research.betfair.journal import SCHEMA_STATEMENTS
        db = sqlite3.connect(str(self.paths.journal_path("paper")))
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET payload_json='{\"text\":\"forged\"}' WHERE seq=1")
        db.execute(next(s for s in SCHEMA_STATEMENTS if "events_no_update" in s))     # the edit is now hidden
        db.commit()
        db.close()
        broken = run_cli(*self.base(), "journal", "--verify")
        self.assertEqual(broken.returncode, 2)
        self.assertEqual(json.loads(broken.stdout)["verify"]["first_bad_seq"], 1)
        self.assertNotIn("Traceback", broken.stderr)

    def test_sanitized_stop_message(self):
        self.paths.ensure()
        self.paths.journal_path("paper").write_bytes(b"not a database " + support.PLANTED_SECRET.encode() * 200)
        completed = run_cli(*self.base(), "journal", "--verify")
        self.assertEqual(completed.returncode, 4)
        self.assertEqual(completed.stderr, "Betfair stopped: DatabaseError; inspect local configuration/state\n")
        self.assertNotIn(support.PLANTED_SECRET, completed.stdout + completed.stderr)
        self.assertNotIn("Traceback", completed.stdout + completed.stderr)

    def test_missing_config_never_falls_back_to_real_default(self):
        missing = self.root / "missing.json"
        book = self.root / "book.json"
        book.write_text(json.dumps(gw.paper_book(support.FakeClock())))
        completed = run_cli("--state-root", self.state, "--config", missing, "place", "--market", "1.1", "--selection",
                            "12345", "--side", "BACK", "--price", "3.60", "--size", "2.00", "--book", book)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr.strip(), "Betfair input error: config_missing")
        self.assertFalse((self.state / "profiles" / "default" / "orders" / "journal-paper.sqlite").exists())
        # In process: load_config is only ever handed the given path, never the real profile default.
        from betangel_research.betfair import config as config_module
        handed = []
        original = config_module.load_config

        def spy(path, *, paths):
            handed.append(Path(path))
            return original(path, paths=paths)

        with patch.object(config_module, "load_config", spy):
            code, out, err = run_main(["--state-root", str(self.state), "--config", str(missing), "status"],
                                      {"clock": support.FakeClock()})
        self.assertEqual(code, 0, err)
        self.assertEqual(handed, [missing])
        self.assertEqual(json.loads(out)["config"]["error"], "config_missing")

if __name__ == "__main__":
    unittest.main()
