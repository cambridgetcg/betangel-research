"""Recording rollout (design v9 §3.3, §3.8, §8 recordings.py and run-ledger torn line), offline.

The run-ledger torn-line guard (receipts.end_torn_line before every run-ledger append), run_ledger_torn_line with its
line index, Yu's typed append-only `recordings ledger-repair` (Astra Q15) and what it does not do (RV7-2: the torn
session stays failed/review, only later sessions classify normally), and classify_invocation's outcomes (§4.4 rules
2-6, the partial-less directory attribution). Temporary state roots, synthetic sessions, a fake clock, the fake
confirmer, a fake tty and a fake Keychain runner only; nothing here types a line for Yu outside a fake confirmer.
"""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest
from unittest.mock import patch

from betangel_research.betfair import auth, cli, receipts
from betangel_research.betfair import paths as paths_module
from betangel_research.betfair.arming import COMMAND_CHECKS
from betangel_research.betfair.credentials import ANCHOR_SERVICE
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, file_lock
from betangel_research.betfair.receipts import append_jsonl, atomic_write_json, utc_compact_from_ns
from betangel_research.betfair.recorder import StreamRecorder
from betangel_research.betfair.recordings import (REPAIR_ROW_KEYS, classify_invocation, read_run_ledger,
                                                  run_ledger_problem, verify_day)

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_stream as fx
from test_betfair_recordings import DAY, SessionWriter, beats, definition, image, seg, t

NEXT_DAY = "2026-10-22"


def write_session(writer, start, invocation_id, *, row="whole", stop_reason="end_at_reached", day=DAY,
                  complete=True):
    """A finalized session as the recorder leaves it, with an invocation id: receipt.partial.json, receipt.json and
    (row "whole") its run-ledger row, appended with the recorder's guard; "torn" leaves the row's first half with no
    newline, "none" no row. -> (directory, the row)."""
    lines = [image(t("12:30:00", day), ("1.101", definition("14:00:00", day=day)))] + \
        beats(t("12:30:00", day), t("13:20:00", day))
    directory = writer.session(start, [seg(lines, close=stop_reason)], ledger=False)
    receipt = json.loads((directory / "receipt.json").read_text())
    receipt.update(invocation_id=invocation_id, stop_reason=stop_reason, complete=complete)
    atomic_write_json(directory / "receipt.partial.json", dict(receipt, complete=False, closed_at=None,
                                                               stop_reason=None))
    digest = atomic_write_json(directory / "receipt.json", receipt)
    ledger = writer.paths.recordings_dir / "run-ledger.jsonl"
    entry = {"created_at": receipt["created_at"], "session_id": directory.name,
             "receipt_path": str(directory / "receipt.json"), "receipt_sha256": digest, "segments": 1,
             "status": receipt["status"], "invocation_id": invocation_id}
    if row == "whole":
        receipts.end_torn_line(ledger)
        append_jsonl(ledger, entry)
    elif row == "torn":
        text = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        receipts.end_torn_line(ledger)
        with ledger.open("ab") as handle:
            handle.write(text[:len(text) // 2])
    return directory, entry


class LedgerCase(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        random.seed(38)
        self.paths = support.temp_state_paths(self)
        self.config = {"recorder": {"root": None}}
        self.writer = SessionWriter(self.paths)
        self.ledger = self.paths.recordings_dir / "run-ledger.jsonl"

    def verify(self, day=DAY):
        return verify_day(self.paths, root=self.paths.recordings_dir, day=day)

    def classify(self, invocation_id, spawn_wall_ns=0):
        return classify_invocation(self.paths, self.config, invocation_id, spawn_wall_ns)

    def lines(self):
        return self.ledger.read_bytes().split(b"\n")

    def repair_row(self, *items):
        """A repair row as ledger-repair writes it (tests of the read side only)."""
        receipts.end_torn_line(self.ledger)
        append_jsonl(self.ledger, {"kind": "run_ledger_repair", "created_at": "2026-10-21T23:00:00.000000Z",
                                   "reviewed_ledger_sha256": hashlib.sha256(self.ledger.read_bytes()).hexdigest(),
                                   "exempt_lines": [{"line": i, "sha256": hashlib.sha256(self.lines()[i]).hexdigest()}
                                                    for i in items]})


class TornLineReadTests(LedgerCase):
    def test_before_repair_verify_day_names_the_index(self):
        write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001")
        write_session(self.writer, t("15:30:00"), "d20261021-a2-00000002", row="torn")
        result = self.verify()
        self.assertIn({"session_id": None, "check": "run_ledger_torn_line", "line": 1}, result["failures"])
        self.assertEqual(run_ledger_problem(self.paths), "run_ledger_torn_line:1")
        self.assertEqual(self.classify("d20261021-a1-00000001")["outcome"], "review")
        self.assertIn("run_ledger_torn_line:1", self.classify("d20261021-a1-00000001")["codes"])

    def test_unreadable_ledger_is_its_own_code(self):
        self.ledger.mkdir()
        self.assertEqual(run_ledger_problem(self.paths), "run_ledger_unreadable")

    def test_unlisted_bad_line_before_or_after_the_barrier_fails_closed(self):
        write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001")
        with self.ledger.open("ab") as handle:
            handle.write(b"garbage-one\ngarbage-two\n")
        self.repair_row(1)                                   # lists line 1 only
        self.assertEqual(read_run_ledger(self.paths)["exempt"], [1])
        self.assertEqual(run_ledger_problem(self.paths), "run_ledger_torn_line:2")
        with self.ledger.open("ab") as handle:
            handle.write(b"garbage-after\n")
        self.assertEqual(read_run_ledger(self.paths)["problems"], ["run_ledger_torn_line:2",
                                                                   "run_ledger_torn_line:4"])

    def test_listed_index_whose_bytes_changed_fails_closed(self):
        write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001")
        with self.ledger.open("ab") as handle:
            handle.write(b"garbage-one\n")
        self.repair_row(1)
        self.assertIsNone(run_ledger_problem(self.paths))
        data = self.ledger.read_bytes()
        self.ledger.write_bytes(data.replace(b"garbage-one", b"garbage-0ne", 1))
        self.assertEqual(run_ledger_problem(self.paths), "run_ledger_torn_line:1")

    def test_repair_row_keys_never_satisfy_a_session_row(self):
        self.assertFalse(REPAIR_ROW_KEYS & {"session_id", "receipt_path", "receipt_sha256"})


class TornAppendTests(LedgerCase):
    def test_torn_tail_then_a_new_append_leaves_the_new_row_whole(self):
        """The recorder's own run-ledger append, after a torn tail, through StreamRecorder.run (fake stream)."""
        self.ledger.write_bytes(b'{"created_at": "2026-10-21T12:00:00.000000Z", "sess')
        clock = support.FakeClock(start=f"{DAY}T14:30:00.000000Z")
        session = fx.FakeSession()
        script = fx.handshake() + [fx.change(lambda sid: fx.image(sid, t("14:30:00") // 1_000_000,
                                                                  market_id="1.120"))]
        server = fx.FakeStreamServer([script], clock)
        StreamRecorder(fx.recorder_config(self.paths), paths=self.paths, session=session,
                       stream_factory=lambda: fx.make_client(server, session, clock), market_lister=fx.lister(["1.120"]),
                       clock=clock, rng=random.Random(5), emit=lambda e: None,
                       invocation_id="d20261021-a1-0000000a").run(stop_requested=server.exhausted)
        pieces = self.lines()
        self.assertEqual(pieces[0], b'{"created_at": "2026-10-21T12:00:00.000000Z", "sess')     # unchanged bytes
        row = json.loads(pieces[1])
        self.assertEqual(row["invocation_id"], "d20261021-a1-0000000a")
        self.assertEqual(pieces[2:], [b""])
        self.assertEqual(run_ledger_problem(self.paths), "run_ledger_torn_line:0")
        self.assertEqual(read_run_ledger(self.paths)["by_invocation"]["d20261021-a1-0000000a"], [row])

    def test_login_ledger_guard_is_the_shared_helper_and_unchanged(self):
        self.assertIs(auth._end_torn_line, receipts.end_torn_line)
        self.paths.auth_dir.mkdir(parents=True, exist_ok=True)
        self.paths.login_ledger.write_bytes(b'{"kind": "note", "at"')
        auth._append_ledger(self.paths.login_ledger, {"kind": "note", "at": "2026-10-21T12:00:00.000000Z"})
        self.assertEqual(self.paths.login_ledger.read_bytes().split(b"\n")[0], b'{"kind": "note", "at"')
        self.assertEqual(json.loads(self.paths.login_ledger.read_bytes().split(b"\n")[1])["kind"], "note")
        receipts.end_torn_line(self.paths.recordings_dir / "absent.jsonl")         # nothing to do, nothing made
        self.assertFalse((self.paths.recordings_dir / "absent.jsonl").exists())


class RepairCliCase(unittest.TestCase):
    """`recordings ledger-repair` through cli.main, on an armed fake (gw.build_live_gateway's state, token, fake
    Keychain runner); the fake confirmer stands in for Yu's typed line and terminal."""

    def setUp(self):
        random.seed(39)
        self.env = gw.build_live_gateway(self)
        self.env.journal.close()
        self.paths = self.env.paths
        self.writer = SessionWriter(self.paths)
        self.ledger = self.paths.recordings_dir / "run-ledger.jsonl"
        self.confirmer = gw.FakeConfirmer()

    def run_cli(self, *argv, confirmer=None, token=None):
        out, err = io.StringIO(), io.StringIO()
        deps = {"keychain_runner": self.env.runner, "http_transport": self.env.transport, "clock": self.env.clock,
                "confirmer": confirmer or self.confirmer}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["recordings", "ledger-repair", "--confirm-live", token or self.env.token, *argv],
                            deps=deps)
        return code, out.getvalue(), err.getvalue()

    def torn(self):
        """S1 (DAY) finalized, then its row torn; returns S1's directory."""
        directory, _ = write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001", row="torn")
        return directory

    def digest(self):
        return hashlib.sha256(self.ledger.read_bytes()).hexdigest()

    def typed(self, digest=None):
        return gw.FakeConfirmer(lines=[f"REPAIR RECORDING LEDGER {digest or self.digest()}"])


class RepairCommandTests(RepairCliCase):
    def test_command_checks_are_exactly_the_resume_set(self):
        self.assertEqual(COMMAND_CHECKS["recordings-ledger-repair"], ("A2", "A11", "A13"))
        self.torn()
        before = self.ledger.read_bytes()
        code, out, err = self.run_cli(token="live-000000000000-20200101")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["failures"], ["confirm_token_mismatch"])
        absent = gw.FakeConfirmer(present=False)
        code, out, err = self.run_cli(confirmer=absent)
        self.assertEqual((code, json.loads(out)["failures"]), (2, ["operator_tty_required"]))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.env.runner.calls.clear()
        code, out, err = self.run_cli(confirmer=self.typed())
        self.assertEqual(code, 0, err)
        services = [call["argv"][call["argv"].index("-s") + 1] for call in self.env.runner.calls]
        self.assertTrue(services)
        self.assertEqual(set(services), {ANCHOR_SERVICE}, services)                     # the anchor only
        self.assertEqual(self.env.transport.requests, [])                              # no login, no network

    def test_never_writes_without_the_typed_line(self):
        self.torn()
        before = self.ledger.read_bytes()
        for confirmer in (gw.FakeConfirmer(lines=["no"]), gw.FakeConfirmer(lines=[None]),
                          self.typed("0" * 64)):
            with self.subTest(lines=confirmer.lines):
                code, out, err = self.run_cli(confirmer=confirmer)
                self.assertEqual((code, err.strip()), (2, "Betfair order refused: operator_echo_mismatch"))
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_nothing_to_repair_exits_0_with_no_prompt(self):
        write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001")
        code, out, err = self.run_cli()
        self.assertEqual((code, json.loads(out)["status"]), (0, "nothing_to_repair"))
        self.assertEqual(self.confirmer.prompts, [])

    def test_show_is_codes_only(self):
        self.torn()
        code, out, err = self.run_cli(confirmer=self.typed())
        shown = json.loads(out.split("\n}\n")[0] + "\n}")
        self.assertEqual(set(shown), {"status", "ledger_sha256", "bad_lines"})
        self.assertEqual([item["line"] for item in shown["bad_lines"]], [0])

    def test_recorder_lock_held_by_another_process_refuses_recorder_running(self):
        self.torn()
        before = self.ledger.read_bytes()
        child_code = ("import sys, time\n"
                      "from betangel_research.betfair.paths import file_lock\n"
                      "with file_lock(sys.argv[1]):\n"
                      "    print('ready', flush=True)\n"
                      "    time.sleep(60)\n")
        env = dict(os.environ, PYTHONPATH=str(support.REPO_ROOT), PYTHONDONTWRITEBYTECODE="1",
                   **{FORBID_NETWORK_ENV: "1"})
        child = subprocess.Popen([sys.executable, "-c", child_code, str(self.paths.recorder_lock)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(), child.stdout.close(),
                                 child.stderr.close()))
        self.assertEqual(child.stdout.readline().strip(), "ready")
        code, out, err = self.run_cli(confirmer=self.typed())
        self.assertEqual(code, 2)
        self.assertIn("recorder_running", err)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_recorder_lock_oserror_refuses_recorder_state_unknown(self):
        self.torn()
        before = self.ledger.read_bytes()

        @contextlib.contextmanager
        def broken(*args, **kwargs):
            raise OSError("io")
            yield
        with patch.object(paths_module, "file_lock", broken):
            code, out, err = self.run_cli(confirmer=self.typed())
        self.assertEqual(code, 2)
        self.assertIn("recorder_state_unknown", err)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_lock_not_held_during_the_prompt(self):
        self.torn()
        seen = []
        paths = self.paths

        class Probe(gw.FakeConfirmer):
            def confirm_line(self, expected, *, banner):
                with file_lock(paths.recorder_lock, timeout_s=0):
                    seen.append("free")
                return super().confirm_line(expected, banner=banner)
        code, out, err = self.run_cli(confirmer=Probe(lines=[f"REPAIR RECORDING LEDGER {self.digest()}"]))
        self.assertEqual((code, seen), (0, ["free"]), err)

    def test_ledger_changed_between_show_and_append_writes_nothing(self):
        for change in (b'{"appended": "row"}\n', b"another-bad-line\n"):
            with self.subTest(change=change):
                self.ledger.unlink(missing_ok=True)
                self.torn()
                ledger, digest = self.ledger, self.digest()

                class Moving(gw.FakeConfirmer):
                    def confirm_line(self, expected, *, banner):
                        result = super().confirm_line(expected, banner=banner)
                        receipts.end_torn_line(ledger)
                        with ledger.open("ab") as handle:
                            handle.write(change)
                        return result
                code, out, err = self.run_cli(confirmer=Moving(lines=[f"REPAIR RECORDING LEDGER {digest}"]))
                self.assertEqual(code, 2)
                self.assertIn("ledger_changed", err)
                self.assertNotIn(b"run_ledger_repair", self.ledger.read_bytes())

    def test_clean_run_writes_exactly_one_repair_row_and_lets_go_of_the_lock(self):
        self.torn()
        before = self.ledger.read_bytes()
        digest = self.digest()
        code, out, err = self.run_cli(confirmer=self.typed())
        self.assertEqual(code, 0, err)
        after = self.ledger.read_bytes()
        self.assertTrue(after.startswith(before))                      # append-only: nothing edited or deleted
        added = [line for line in after[len(before):].split(b"\n") if line.strip()]
        self.assertEqual(len(added), 1)
        row = json.loads(added[0])
        self.assertEqual(set(row), REPAIR_ROW_KEYS)
        self.assertEqual((row["kind"], row["reviewed_ledger_sha256"], [i["line"] for i in row["exempt_lines"]]),
                         ("run_ledger_repair", digest, [0]))
        with file_lock(self.paths.recorder_lock, timeout_s=0):
            pass                                                         # let go afterwards
        self.assertIsNone(run_ledger_problem(self.paths))

    def test_help_names_ledger_repair_as_the_one_typed_subcommand(self):
        self.assertIn("ledger-repair, the one typed, append-only subcommand", cli.HELP["recordings"])


class AfterRepairTests(RepairCliCase):
    """RV7-2: repair unblocks later appends and sessions only."""

    def setUp(self):
        super().setUp()
        self.s1 = self.torn()
        self.s1_receipt = (self.s1 / "receipt.json").read_bytes()
        code, out, err = self.run_cli(confirmer=self.typed())
        self.assertEqual(code, 0, err)

    def test_the_torn_session_stays_failed_and_review(self):
        failures = verify_day(self.paths, root=self.paths.recordings_dir, day=DAY)["failures"]
        self.assertEqual(failures, [{"session_id": self.s1.name, "check": "receipt_without_ledger_row"}])
        code, out, err = self.cli_verify_day(DAY)
        self.assertEqual(code, 2)
        result = classify_invocation(self.paths, {"recorder": {"root": None}}, "d20261021-a1-00000001", 0)
        self.assertEqual((result["outcome"], result["session_id"]), ("receipt_without_ledger_row", self.s1.name))
        self.assertNotIn(self.s1.name, read_run_ledger(self.paths)["rows"])          # no row for S1 afterwards
        self.assertEqual((self.s1 / "receipt.json").read_bytes(), self.s1_receipt)  # the receipt unchanged

    def cli_verify_day(self, day):
        out, err = io.StringIO(), io.StringIO()
        deps = {"keychain_runner": self.env.runner, "clock": self.env.clock}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["verify-day", "--day", day], deps=deps)
        return code, out.getvalue(), err.getvalue()

    def test_a_later_intact_session_classifies_normally(self):
        s2, _ = write_session(self.writer, t("12:30:00", NEXT_DAY), "d20261022-a1-00000002", day=NEXT_DAY)
        result = classify_invocation(self.paths, {"recorder": {"root": None}}, "d20261022-a1-00000002", 0)
        self.assertEqual((result["outcome"], result["stop_reason"], result["session_id"]),
                         ("finalized", "end_at_reached", s2.name))
        day = verify_day(self.paths, root=self.paths.recordings_dir, day=NEXT_DAY)
        self.assertEqual((day["status"], day["run_ledger_exempt_lines"]), ("verified", [0]))
        code, out, err = self.cli_verify_day(NEXT_DAY)
        self.assertEqual(code, 0, out + err)

    def test_exact_one_row_finality_still_holds(self):
        s2, row = write_session(self.writer, t("12:30:00", NEXT_DAY), "d20261022-a1-00000002", day=NEXT_DAY)
        append_jsonl(self.ledger, row)
        checks = {f["check"] for f in verify_day(self.paths, root=self.paths.recordings_dir, day=NEXT_DAY)["failures"]}
        self.assertEqual(checks, {"ledger_rows_duplicated"})

    def test_a_second_tear_after_the_barrier_fails_closed_again(self):
        write_session(self.writer, t("12:30:00", NEXT_DAY), "d20261022-a1-00000002", day=NEXT_DAY, row="torn")
        problem = run_ledger_problem(self.paths)
        self.assertTrue(problem.startswith("run_ledger_torn_line:"))
        self.assertNotEqual(problem, "run_ledger_torn_line:0")


class RepairLockAgainstAdmissionTests(unittest.TestCase):
    """RV8-3: a live admit's drain that meets the repair-held .recorder.lock with a stale recorder.pid (a SIGKILLed
    recorder's) refuses recorder_pid_mismatch, and nothing is deleted."""

    def test_stale_pid_refuses_recorder_pid_mismatch(self):
        from betangel_research.betfair import admission
        from betangel_research.betfair.config import validate_config
        from betangel_research.betfair.errors import AdmissionRefused
        from test_betfair_admission import FakeProcesses
        support.block_network(self)
        paths = support.temp_state_paths(self)
        config = validate_config(support.make_test_config(paths), paths=paths)
        clock = support.FakeClock()
        atomic_write_json(paths.recorder_pid_file, {"schema_version": 1, "pid": 4242, "start_time": "start-old",
                                                    "invocation_id": "d20261021-a1-00000001",
                                                    "started_at": "2020-01-01T11:00:00.000000Z"})
        before = paths.recorder_pid_file.read_bytes()
        processes = FakeProcesses(pid=1, starts={1: "start-1", 4242: "start-new"})
        with file_lock(paths.recorder_lock, timeout_s=0):                      # held as ledger-repair holds it
            with self.assertRaises(AdmissionRefused) as caught:
                admission.admit(paths, owner="manual:1", purpose="manual place", config=config, clock=clock,
                                processes=processes)
        self.assertEqual(caught.exception.outcome, "recorder_pid_mismatch")
        self.assertEqual(processes.signals, [])
        self.assertEqual(paths.recorder_pid_file.read_bytes(), before)          # nothing deleted


class ClassifyTests(LedgerCase):
    """classify_invocation (§3.3): one fixture per outcome of §4.4 rules 2-6."""

    def spawn_ns(self, clock_time="12:30:00"):
        return t(clock_time)

    def test_rule_4_finalized(self):
        directory, _ = write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001",
                                     stop_reason="reconnect_budget_exhausted")
        result = self.classify("d20261021-a1-00000001", self.spawn_ns())
        self.assertEqual(result, {"outcome": "finalized", "session_id": directory.name, "codes": [],
                                  "stop_reason": "reconnect_budget_exhausted",
                                  "receipt_sha256": hashlib.sha256((directory / "receipt.json").read_bytes()).hexdigest()})

    def test_rule_5_receipt_without_ledger_row(self):
        directory, _ = write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001", row="none")
        self.assertEqual(self.classify("d20261021-a1-00000001")["outcome"], "receipt_without_ledger_row")

    def test_rule_3_incomplete(self):
        directory, _ = write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001", row="none")
        (directory / "receipt.json").unlink()
        result = self.classify("d20261021-a1-00000001")
        self.assertEqual((result["outcome"], result["session_id"]), ("incomplete", directory.name))

    def test_rule_2_no_session(self):
        write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001")
        self.assertEqual(self.classify("d20261021-a9-00000009", self.spawn_ns("23:00:00"))["outcome"], "no_session")

    def test_rule_6_two_sessions_with_one_id(self):
        write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001")
        write_session(self.writer, t("15:30:00"), "d20261021-a1-00000001")
        result = self.classify("d20261021-a1-00000001")
        self.assertEqual((result["outcome"], result["codes"]), ("review", ["invocation_matches_several_sessions"]))

    def test_rule_6_torn_ledger_line(self):
        write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001", row="torn")
        result = self.classify("d20261021-a1-00000001")
        self.assertEqual((result["outcome"], result["codes"]), ("review", ["run_ledger_torn_line:0"]))

    def test_rule_6_complete_false(self):
        write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001", complete=False)
        result = self.classify("d20261021-a1-00000001")
        self.assertEqual(result["outcome"], "review")
        self.assertIn("receipt_not_complete", result["codes"])

    def test_rule_6_row_and_receipt_mismatch_and_one_sided_id(self):
        directory, row = write_session(self.writer, t("12:30:00"), "d20261021-a1-00000001", row="none")
        append_jsonl(self.ledger, {k: v for k, v in row.items() if k != "invocation_id"})
        result = self.classify("d20261021-a1-00000001")
        self.assertEqual((result["outcome"], result["codes"]), ("review", ["ledger_invocation_mismatch"]))
        self.ledger.write_text(json.dumps(dict(row, receipt_sha256="0" * 64)) + "\n")
        self.assertEqual(self.classify("d20261021-a1-00000001")["codes"], ["ledger_receipt_sha256_mismatch"])

    def partial_less(self, stamp_ns):
        name = f"rec-{utc_compact_from_ns(stamp_ns)}-0badf00d"
        (self.paths.recordings_dir / name).mkdir(mode=0o700)
        return name

    def test_partial_less_directory_at_or_after_spawn_minus_one_second_is_review(self):
        spawn = t("12:30:00") + 900_000_000                       # 12:30:00.9
        name = self.partial_less(t("12:30:00"))                   # stamp 12:30:00, within the 1 s margin
        result = self.classify("d20261021-a1-00000001", spawn)
        self.assertEqual((result["outcome"], result["session_id"]), ("partial_less_directory", name))

    def test_partial_less_directory_before_spawn_minus_one_second_is_not_attributed(self):
        self.partial_less(t("12:29:58"))
        result = self.classify("d20261021-a1-00000001", t("12:30:00"))
        self.assertEqual(result["outcome"], "no_session")

    def test_recorder_root_is_honoured(self):
        other = Path(self.paths.root).parent / "elsewhere"
        other.mkdir()
        writer = SessionWriter(self.paths, root=other)
        directory, _ = write_session(writer, t("12:30:00"), "d20261021-a1-00000001")
        result = classify_invocation(self.paths, {"recorder": {"root": str(other)}}, "d20261021-a1-00000001", 0)
        self.assertEqual((result["outcome"], result["session_id"]), ("finalized", directory.name))


if __name__ == "__main__":
    unittest.main()
