"""Recording admission (admission.py), the step 8 ledger helper (auth.ledger_review) and the read-only journal fold.

Offline and synthetic: temporary state roots, a fake clock and fake process tables; the subprocess tests start
local Python children that take .recorder.lock and nothing else. They show the admission protocol of recording
design v3 section 2 as amended by v4 and v5 (RD2-1, RD2-3, RD3-1), not anything about Betfair.
"""

import ast
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission
from betangel_research.betfair.admission import (SYSTEM_PROCESSES, admission_status, admit, recorder_admission,
                                                 recorder_lock_held, release)
from betangel_research.betfair.auth import append_login_reset, ledger_review
from betangel_research.betfair.config import validate_config
from betangel_research.betfair.errors import AdmissionRefused, JournalError, LockBusy, RecorderRunning
from betangel_research.betfair.journal import OrderJournal, read_only_fold
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, file_lock
from betangel_research.betfair.receipts import utc_from_ns
from betangel_research.betfair.recorder import StreamRecorder

import test_betfair_support as support
import test_betfair_support_orders as orders
import test_betfair_support_stream as fx

REPO = support.REPO_ROOT
BETFAIR = REPO / "betangel_research" / "betfair"


class FakeProcesses:
    """A process table: start times by pid, and the pids terminate() was asked to signal."""

    def __init__(self, pid=4242, starts=None, on_terminate=None):
        self.pid = pid
        self.starts = dict({pid: "start-A"} if starts is None else starts)
        self.signals = []
        self.on_terminate = on_terminate

    def current_pid(self):
        return self.pid

    def start_time(self, pid):
        return self.starts.get(pid)

    def terminate(self, pid):
        self.signals.append(pid)
        if self.on_terminate is not None:
            self.on_terminate(pid)


def ledger_rows(paths, *rows):
    paths.auth_dir.mkdir(parents=True, exist_ok=True)
    with paths.login_ledger.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


class AdmissionTestCase(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock()
        self.config = validate_config(support.make_test_config(self.paths), paths=self.paths)

    def admit(self, owner, generation=None, processes=None, **kwargs):
        return admit(self.paths, owner=owner, purpose="synthetic test", config=self.config, generation=generation,
                     clock=self.clock, processes=processes or FakeProcesses(pid=1), **kwargs)

    def release(self, generation, **kwargs):
        return release(self.paths, config=self.config, generation=generation, clock=self.clock, **kwargs)

    def refused(self, outcome, call, *args, **kwargs):
        with self.assertRaises(AdmissionRefused) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.outcome, outcome, str(caught.exception))
        return caught.exception

    def ago(self, seconds):
        return utc_from_ns(self.clock.time_ns() - int(seconds * 1_000_000_000))

    def live_journal(self):
        journal = OrderJournal(self.paths.journal_path("live"), mode="live", clock=self.clock)
        self.addCleanup(journal.close)
        return journal

    def place(self, journal, ref=orders.REF, intent_id="intent-1"):
        journal.append("intent", orders.intent_payload(orders.instruction()), intent_id=intent_id,
                       customer_order_ref=ref, customer_ref=orders.CREF, market_id=support.MARKET_ID,
                       state_after="RECORDED")
        journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                  "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                       intent_id=intent_id, market_id=support.MARKET_ID)


class RecorderSideTests(AdmissionTestCase):
    def test_recorder_admission_writes_pid_holds_lock_and_cleans_up(self):
        processes = FakeProcesses()
        with recorder_admission(self.paths, invocation_id="inv-1", clock=self.clock, processes=processes) as ticket:
            record = json.loads(self.paths.recorder_pid_file.read_text())
            self.assertEqual({k: record[k] for k in ("pid", "start_time", "invocation_id")},
                             {"pid": 4242, "start_time": "start-A", "invocation_id": "inv-1"})
            self.assertEqual(oct(self.paths.recorder_pid_file.stat().st_mode & 0o777), oct(0o600))
            self.assertTrue(ticket.holds(self.paths))
            self.assertTrue(recorder_lock_held(self.paths, clock=self.clock))
            with self.assertRaises(LockBusy):                       # .recorder.lock stays held after admission.lock
                with file_lock(self.paths.recorder_lock):
                    pass
            with file_lock(self.paths.admission_lock):             # admission.lock is already free again
                pass
        self.assertFalse(self.paths.recorder_pid_file.exists())
        self.assertFalse(recorder_lock_held(self.paths, clock=self.clock))

    def test_inhibited_and_unknown_states_refuse_a_recorder(self):
        result = self.admit("arm-live:100")
        refusal = self.refused("inhibited", lambda: recorder_admission(self.paths, clock=self.clock,
                                                                       processes=FakeProcesses()).__enter__())
        self.assertEqual((refusal.owner, refusal.generation), ("arm-live:100", result["generation"]))
        self.assertFalse(self.paths.recorder_pid_file.exists())
        self.assertFalse(recorder_lock_held(self.paths, clock=self.clock))
        self.release(result["generation"])
        # INHIBIT alone (any directory entry of that name) refuses too.
        self.paths.inhibit_file.write_text("")
        self.refused("inhibited", lambda: recorder_admission(self.paths, clock=self.clock,
                                                             processes=FakeProcesses()).__enter__())
        self.paths.inhibit_file.unlink()
        # Unknown state: an ADMISSION that does not parse refuses everything.
        self.paths.admission_file.write_text("{torn")
        for call in (lambda: recorder_admission(self.paths, clock=self.clock, processes=FakeProcesses()).__enter__(),
                     lambda: self.admit("manual:1"), lambda: self.release(None)):
            with self.subTest(call=call):
                self.refused("admission_state_unknown", call)
        self.paths.admission_file.unlink()
        # A recorder that cannot read its own start time could never be checked: it refuses to start.
        self.refused("recorder_identity_unknown", lambda: recorder_admission(
            self.paths, clock=self.clock, processes=FakeProcesses(starts={})).__enter__())
        with recorder_admission(self.paths, clock=self.clock, processes=FakeProcesses()):
            pass

    def test_second_recorder_refused_and_first_pid_file_kept(self):
        with recorder_admission(self.paths, invocation_id="first", clock=self.clock, processes=FakeProcesses()):
            before = self.paths.recorder_pid_file.read_bytes()
            with self.assertRaises(RecorderRunning) as caught:
                with recorder_admission(self.paths, invocation_id="second", clock=self.clock,
                                        processes=FakeProcesses(pid=5555, starts={5555: "start-B"})):
                    pass
            self.assertIsInstance(caught.exception, LockBusy)
            self.assertEqual(caught.exception.outcome, "recorder_running")
            self.assertEqual(self.paths.recorder_pid_file.read_bytes(), before)

    def test_invalid_invocation_id_refused(self):
        for value in ("", "has space", "x" * 65, "-leading", 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with recorder_admission(self.paths, invocation_id=value, clock=self.clock, processes=FakeProcesses()):
                    pass

    def test_real_process_table_start_times(self):
        first = SYSTEM_PROCESSES.start_time(os.getpid())
        self.assertTrue(first)
        self.assertEqual(SYSTEM_PROCESSES.start_time(os.getpid()), first)
        child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True,
                               timeout=30, check=True)
        self.assertIsNone(SYSTEM_PROCESSES.start_time(int(child.stdout)))          # gone, so nothing to signal
        self.assertIsNone(SYSTEM_PROCESSES.start_time(0))

    def test_stream_recorder_started_directly_takes_admission(self):
        """However a recorder is started, it goes through admission: StreamRecorder.run without a ticket takes
        its own, so INHIBIT refuses it before any pre-count or connection, and while it runs .recorder.lock is
        held and recorder.pid names it."""
        session = fx.FakeSession()
        server = fx.FakeStreamServer([fx.handshake() + [fx.change(lambda sid: fx.image(sid, fx.pt_at(-300)))]],
                                     self.clock)
        lister = fx.lister()
        seen = []

        def emit(event):
            if event.get("event") == "segment_opened":
                seen.append((recorder_lock_held(self.paths, clock=self.clock),
                             json.loads(self.paths.recorder_pid_file.read_text())["invocation_id"]))
        recorder = StreamRecorder(fx.recorder_config(self.paths), paths=self.paths, session=session,
                                  stream_factory=lambda: fx.make_client(server, session, self.clock),
                                  market_lister=lister, clock=self.clock, rng=random.Random(3), emit=emit,
                                  invocation_id="direct-1")
        generation = self.admit("manual:7")["generation"]
        self.refused("inhibited", recorder.run, stop_requested=server.exhausted)
        self.assertEqual((lister.calls, server.calls, session.calls), ([], [], []))
        self.release(generation)
        receipt = recorder.run(stop_requested=server.exhausted)
        self.assertTrue(receipt["complete"])
        self.assertEqual(seen, [(True, "direct-1")])
        self.assertFalse(self.paths.recorder_pid_file.exists())
        self.assertFalse(recorder_lock_held(self.paths, clock=self.clock))

    def test_recorder_never_waits_for_or_takes_the_gateway_lock(self):
        session = fx.FakeSession()
        server = fx.FakeStreamServer([fx.handshake() + [fx.change(lambda sid: fx.image(sid, fx.pt_at(-300)))]],
                                     self.clock)
        recorder = StreamRecorder(fx.recorder_config(self.paths), paths=self.paths, session=session,
                                  stream_factory=lambda: fx.make_client(server, session, self.clock),
                                  market_lister=fx.lister(), clock=self.clock, rng=random.Random(4), emit=lambda e: None)
        with file_lock(self.paths.gateway_lock):
            self.assertTrue(recorder.run(stop_requested=server.exhausted)["complete"])
        for name in ("recorder.py", "admission.py", "recordings.py"):
            with self.subTest(module=name):
                text = (BETFAIR / name).read_text(encoding="utf-8")
                self.assertNotIn("gateway_lock", text)
                self.assertNotIn(".gateway.lock\"", text)


class LiveAdmissionTests(AdmissionTestCase):
    def test_admit_writes_inhibit_and_admission_then_release_clears_both(self):
        first = self.admit("arm-live:100")
        self.assertEqual(first, {"generation": 1, "owner": "arm-live:100", "purpose": "synthetic test",
                                 "continued": False, "state": "admitted", "ledger_checked": True, "recorder": "none"})
        stored = json.loads(self.paths.admission_file.read_text())
        self.assertEqual(set(stored), {"schema_version", "generation", "owner", "purpose", "created_at", "state"})
        self.assertEqual((stored["generation"], stored["owner"], stored["state"]), (1, "arm-live:100", "admitted"))
        self.assertTrue(self.paths.inhibit_file.exists())
        for path in (self.paths.admission_file, self.paths.inhibit_file, self.paths.admission_generation_file):
            self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600))
        status = admission_status(self.paths)
        self.assertEqual((status["inhibit"], status["admission"]["generation"]), (True, 1))
        self.assertEqual(self.release(1), {"released": True, "generation": 1, "owner": "arm-live:100",
                                           "status": "released"})
        self.assertFalse(self.paths.admission_file.exists())
        self.assertFalse(self.paths.inhibit_file.exists())
        self.assertEqual(self.release(None)["status"], "nothing_to_release")
        # Generations only grow, across releases.
        self.assertEqual(self.admit("pilot-session:abc")["generation"], 2)

    def test_rd3_1_one_owner_at_a_time_a_b_interleaving(self):
        """RD3-1 (Astra's third review): A admits as N+1; B tries before A publishes armed state and is refused
        (A's ADMISSION is never overwritten); A carries on and releases N+1; B succeeds only after that release."""
        a = self.admit("pilot-session:a")
        n1 = a["generation"]
        refusal = self.refused("admission_held", self.admit, "arm-live:200")
        self.assertEqual((refusal.owner, refusal.generation), ("pilot-session:a", n1))
        self.assertEqual(json.loads(self.paths.admission_file.read_text())["owner"], "pilot-session:a")
        # B presenting A's generation is still a different owner.
        self.refused("admission_held", self.admit, "arm-live:200", generation=n1)
        # A record cannot start a recorder meanwhile.
        self.refused("inhibited", lambda: recorder_admission(self.paths, clock=self.clock,
                                                             processes=FakeProcesses()).__enter__())
        # A carries on into its cleanup with its own generation.
        self.assertEqual(self.admit("pilot-session:a", generation=n1)["continued"], True)
        self.refused("admission_held", self.admit, "arm-live:200")
        self.assertEqual(self.release(n1)["released"], True)
        b = self.admit("arm-live:200")
        self.assertEqual(b["generation"], n1 + 1)
        self.assertEqual(json.loads(self.paths.admission_file.read_text())["owner"], "arm-live:200")

    def test_stale_release_refused(self):
        """An older generation's orderly end never releases a newer admission (RD2-1)."""
        a = self.admit("arm-live:100")["generation"]
        self.release(a)
        b = self.admit("pilot-session:b")["generation"]
        refusal = self.refused("release_generation_mismatch", self.release, a)
        self.assertEqual((refusal.owner, refusal.generation), ("pilot-session:b", b))
        self.refused("release_generation_mismatch", self.release, None)
        self.assertTrue(self.paths.admission_file.exists())
        self.assertTrue(self.paths.inhibit_file.exists())
        self.release(b)
        self.refused("admission_not_held", self.release, a)

    def test_same_owner_continues_only_with_its_generation(self):
        n = self.admit("manual:9")["generation"]
        self.refused("admission_held", self.admit, "manual:9")                    # no generation presented
        self.refused("admission_generation_mismatch", self.admit, "manual:9", generation=n + 5)
        self.paths.inhibit_file.unlink()                                          # continuation restores it
        self.assertTrue(self.admit("manual:9", generation=n)["continued"])
        self.assertTrue(self.paths.inhibit_file.exists())
        self.release(n)
        self.refused("admission_not_held", self.admit, "manual:9", generation=n)

    def test_owner_and_purpose_validated(self):
        for owner in ("", "nocolon", "Upper:1", "kind:", "kind:has space"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.admit(owner)
        with self.assertRaises(ValueError):
            admit(self.paths, owner="manual:1", purpose="bad\nline", config=self.config, clock=self.clock)


class DrainTests(AdmissionTestCase):
    def hold_recorder(self, processes, invocation_id="rec-1"):
        context = recorder_admission(self.paths, invocation_id=invocation_id, clock=self.clock, processes=processes)
        context.__enter__()
        state = {"open": True}

        def stop(*args):
            if state["open"]:
                state["open"] = False
                context.__exit__(None, None, None)
        self.addCleanup(stop)
        return stop

    def test_sigterm_after_start_time_check_then_wait_for_the_lock(self):
        stop = self.hold_recorder(FakeProcesses())
        admitter = FakeProcesses(pid=1, starts={4242: "start-A"}, on_terminate=lambda pid: stop())
        result = self.admit("arm-live:300", processes=admitter)
        self.assertEqual((result["recorder"], admitter.signals), ("stopped", [4242]))
        self.assertFalse(recorder_lock_held(self.paths, clock=self.clock))
        self.assertTrue(self.paths.inhibit_file.exists())

    def test_pid_reuse_refused_never_signalling_a_stranger(self):
        self.hold_recorder(FakeProcesses())
        for starts in ({4242: "start-B"}, {}):            # the pid now names another process, or none at all
            with self.subTest(starts=starts):
                admitter = FakeProcesses(pid=1, starts=starts)
                self.refused("recorder_pid_mismatch", self.admit, "arm-live:300", processes=admitter)
                self.assertEqual(admitter.signals, [])
                self.assertFalse(self.paths.admission_file.exists())     # the refused admission withdrew itself
                self.assertTrue(self.paths.inhibit_file.exists())        # but recording stays inhibited
                self.assertTrue(recorder_lock_held(self.paths, clock=self.clock))

    def test_bounded_wait_then_refuse(self):
        self.hold_recorder(FakeProcesses())
        admitter = FakeProcesses(pid=1, starts={4242: "start-A"})        # signalled, but never lets go
        started = self.clock.mono_ns
        self.refused("recorder_did_not_stop", self.admit, "arm-live:300", processes=admitter, drain_timeout_s=30)
        self.assertEqual(admitter.signals, [4242])                       # one SIGTERM, never repeated
        self.assertGreaterEqual(self.clock.mono_ns - started, 30 * 10**9)
        self.assertFalse(self.paths.admission_file.exists())
        self.assertTrue(self.paths.inhibit_file.exists())

    def test_lock_held_without_pid_is_waited_for_never_signalled(self):
        with file_lock(self.paths.recorder_lock):                         # an older recorder: no recorder.pid
            admitter = FakeProcesses(pid=1)
            self.refused("recorder_lock_held_without_pid", self.admit, "arm-live:300", processes=admitter,
                         drain_timeout_s=5)
            self.assertEqual(admitter.signals, [])

    def test_any_recorder_process_is_detected_and_stopped_through_the_lock(self):
        """Real processes: a child that went through recorder admission is found through .recorder.lock, checked
        by its real start time, sent SIGTERM and waited for; a child holding the lock without recorder.pid is
        never signalled and admission refuses."""
        child_code = (
            "import signal, sys, time\n"
            "from betangel_research.betfair.admission import recorder_admission\n"
            "from betangel_research.betfair.paths import StatePaths, file_lock\n"
            "paths = StatePaths(home=sys.argv[1], root_override=sys.argv[2], env={'%s': '1'})\n"
            "stop = []\n"
            "signal.signal(signal.SIGTERM, lambda *a: stop.append(1))\n"
            "context = recorder_admission(paths, invocation_id='child-1') if sys.argv[3] == 'admitted' \\\n"
            "    else file_lock(paths.recorder_lock)\n"
            "with context:\n"
            "    print('ready', flush=True)\n"
            "    deadline = time.monotonic() + 60\n"
            "    while not stop and time.monotonic() < deadline:\n"
            "        time.sleep(0.05)\n"
            "print('stopped' if stop else 'timeout', flush=True)\n" % FORBID_NETWORK_ENV)
        env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1", **{FORBID_NETWORK_ENV: "1"})

        def start(mode):
            child = subprocess.Popen([sys.executable, "-c", child_code, str(self.paths.home), str(self.paths.root), mode],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(), child.stdout.close(),
                                     child.stderr.close()))
            self.assertEqual(child.stdout.readline().strip(), "ready", child.stderr.read() if child.poll() else "")
            return child

        child = start("admitted")
        self.assertEqual(json.loads(self.paths.recorder_pid_file.read_text())["pid"], child.pid)
        result = admit(self.paths, owner="arm-live:%d" % os.getpid(), purpose="subprocess test", config=self.config,
                       drain_timeout_s=20)
        self.assertEqual(result["recorder"], "stopped")
        self.assertEqual(child.wait(timeout=20), 0)
        self.assertEqual(child.stdout.read().strip(), "stopped")
        self.assertFalse(self.paths.recorder_pid_file.exists())
        release(self.paths, config=self.config, generation=result["generation"])

        child = start("lock-only")
        self.refused("recorder_lock_held_without_pid", admit, self.paths, owner="arm-live:1", purpose="subprocess test",
                     config=self.config, drain_timeout_s=1)
        self.assertIsNone(child.poll())                                    # never signalled
        child.terminate()
        self.assertEqual(child.wait(timeout=20), 0)


class LedgerStepEightTests(AdmissionTestCase):
    def attempt(self, attempt_id, seconds_ago):
        return {"kind": "login_attempt", "attempt_id": attempt_id, "at": self.ago(seconds_ago), "method": "cert",
                "jurisdiction": "uk", "emergency": False, "coexistence_ack": True}

    def outcome(self, attempt_id, outcome, seconds_ago, ban_until=None):
        return {"kind": "login_outcome", "attempt_id": attempt_id, "at": self.ago(seconds_ago), "method": "cert",
                "jurisdiction": "uk", "outcome": outcome, "outcome_status": outcome, "outcome_error": None,
                "emergency": False, "coexistence_ack": True, "ban_until": ban_until}

    def test_unresolved_attempt_blocks_admission_at_any_age(self):
        for seconds_ago in (10 * 86400, 1):          # long dead, and young enough for _summarise to call in flight
            with self.subTest(seconds_ago=seconds_ago):
                self.paths.login_ledger.unlink(missing_ok=True)
                ledger_rows(self.paths, self.attempt("ok", 3600), self.outcome("ok", "proceed", 3599),
                            self.attempt("lost", seconds_ago))
                review = ledger_review(self.config, self.paths, clock=self.clock)
                self.assertEqual((review["unresolved_attempts_after_barrier"], review["clean"]), (1, False))
                refusal = self.refused("login_ledger_not_clean", self.admit, "arm-live:400")
                self.assertEqual(refusal.detail, "login_attempt_unresolved")
                self.assertIn("login_attempt_unresolved", refusal.problems)
                self.assertFalse(self.paths.admission_file.exists())
                self.assertTrue(self.paths.inhibit_file.exists())
        # The operator's login-reset is the barrier: after it the ledger is clean and admission goes through.
        append_login_reset(self.paths, clock=self.clock)
        self.assertTrue(ledger_review(self.config, self.paths, clock=self.clock)["clean"])
        self.assertEqual(self.admit("arm-live:400")["continued"], False)

    def test_barrier_failures_bans_and_torn_lines(self):
        cases = [
            ("failure_after_success", [self.attempt("a", 300), self.outcome("a", "proceed", 299),
                                       self.attempt("b", 200), self.outcome("b", "auth_error", 199)],
             "login_failure_after_barrier"),
            ("unresolved_before_success_is_ignored", [self.attempt("a", 300), self.attempt("b", 200),
                                                      self.outcome("b", "proceed", 199)], None),
            ("not_sent_changes_nothing", [self.attempt("a", 300), self.outcome("a", "proceed", 299),
                                          self.attempt("b", 200), self.outcome("b", "not_sent", 199)], None),
            ("failure_then_success", [self.attempt("a", 300), self.outcome("a", "auth_error", 299),
                                      self.attempt("b", 200), self.outcome("b", "proceed", 199)], None),
        ]
        for name, rows, problem in cases:
            with self.subTest(case=name):
                self.paths.login_ledger.unlink(missing_ok=True)
                ledger_rows(self.paths, *rows)
                self.assertEqual(admission.ledger_problems(self.config, self.paths, clock=self.clock),
                                 [problem] if problem else [])
        # An active ban refuses whatever the barrier: a reset does not lift it.
        self.paths.login_ledger.unlink()
        ledger_rows(self.paths, self.attempt("a", 300),
                    self.outcome("a", "login_ban", 299, ban_until=utc_from_ns(self.clock.time_ns() + 600 * 10**9)))
        append_login_reset(self.paths, clock=self.clock)
        review = ledger_review(self.config, self.paths, clock=self.clock)
        self.assertEqual((review["barrier"]["kind"], review["banned_until"] is not None), ("login_reset", True))
        self.assertEqual(admission.ledger_problems(self.config, self.paths, clock=self.clock), ["login_banned"])
        # A torn line fails closed.
        self.paths.login_ledger.unlink()
        ledger_rows(self.paths, self.attempt("a", 300))
        with self.paths.login_ledger.open("a") as handle:
            handle.write('{"kind": "login_out')
        self.assertEqual(admission.ledger_problems(self.config, self.paths, clock=self.clock),
                         ["login_ledger_unreadable"])

    def test_only_an_admitted_generation_skips_step_8_and_only_for_cleanup(self):
        """REC-1: an admitted generation continuing for cleanup skips a new ledger refusal (its own logins); the same
        continuation without cleanup runs step 8 again and is refused, but its admission is kept."""
        n = self.admit("pilot-session:c")["generation"]
        ledger_rows(self.paths, self.attempt("own", 10), self.outcome("own", "auth_error", 9))
        result = self.admit("pilot-session:c", generation=n, cleanup=True)
        self.assertEqual((result["continued"], result["state"], result["ledger_checked"]), (True, "admitted", False))
        self.refused("login_ledger_not_clean", self.admit, "pilot-session:c", generation=n)
        self.assertEqual(json.loads(self.paths.admission_file.read_text())["state"], "admitted")    # kept
        refusal = self.refused("release_refused", self.release, n)         # but release needs the ledger clean
        self.assertIn("login_failure_after_barrier", refusal.problems)
        with self.assertRaises(ValueError):
            self.admit("pilot-session:c", generation=n, cleanup="yes")

    def test_interrupted_admission_stays_pending_until_it_passes_step_8(self):
        """REC-1 (Astra): the first admit() is interrupted after ADMISSION was written; its generation stays pending,
        and continuing it, even for cleanup, must finish the drain and step 8 before it is admitted."""
        ledger_rows(self.paths, self.attempt("lost", 1))
        with patch.object(admission, "_drain", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.admit("pilot-session:interrupted")
        saved = admission_status(self.paths)["admission"]
        self.assertEqual(saved["state"], "pending")
        refusal = self.refused("login_ledger_not_clean", self.admit, "pilot-session:interrupted",
                               generation=saved["generation"], cleanup=True)
        self.assertEqual(refusal.detail, "login_attempt_unresolved")
        self.assertFalse(self.paths.admission_file.exists())                # the pending generation was taken back
        self.assertTrue(self.paths.inhibit_file.exists())
        # With the ledger clean, a pending continuation passes both checks and is admitted.
        append_login_reset(self.paths, clock=self.clock)
        with patch.object(admission, "_drain", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.admit("pilot-session:interrupted")
        saved = admission_status(self.paths)["admission"]
        result = self.admit("pilot-session:interrupted", generation=saved["generation"])
        self.assertEqual((result["state"], result["ledger_checked"]), ("admitted", True))
        self.assertEqual(json.loads(self.paths.admission_file.read_text())["state"], "admitted")

    def test_death_after_the_admission_write_leaves_a_pending_generation(self):
        """REC-1: the admitting process is SIGKILLed after writing ADMISSION (no Python cleanup runs). Its generation
        is pending, never admitted, so continuing it runs step 8: refused while a login attempt is unresolved."""
        child_code = (
            "import os, signal, sys\n"
            "from unittest.mock import patch\n"
            "from betangel_research.betfair import admission\n"
            "from betangel_research.betfair.config import validate_config\n"
            "from betangel_research.betfair.paths import StatePaths\n"
            "import test_betfair_support as support\n"
            "paths = StatePaths(home=sys.argv[1], root_override=sys.argv[2], env={'%s': '1'})\n"
            "config = validate_config(support.make_test_config(paths), paths=paths)\n"
            "def die(*args, **kwargs):\n"
            "    os.kill(os.getpid(), signal.SIGKILL)\n"
            "with patch.object(admission, '_drain', die):\n"
            "    admission.admit(paths, owner='pilot-session:dead', purpose='death test', config=config)\n"
            % FORBID_NETWORK_ENV)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO), str(REPO / "tests")]),
                   PYTHONDONTWRITEBYTECODE="1", **{FORBID_NETWORK_ENV: "1"})

        def die_after_write():
            child = subprocess.run([sys.executable, "-c", child_code, str(self.paths.home), str(self.paths.root)],
                                   capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(child.returncode, -9, child.stderr[-2000:])
            return json.loads(self.paths.admission_file.read_text())

        ledger_rows(self.paths, self.attempt("lost", 3600))
        saved = die_after_write()
        self.assertEqual((saved["owner"], saved["state"]), ("pilot-session:dead", "pending"))
        self.refused("login_ledger_not_clean", self.admit, "pilot-session:dead", generation=saved["generation"],
                     cleanup=True)
        self.assertFalse(self.paths.admission_file.exists())
        append_login_reset(self.paths, clock=self.clock)
        saved = die_after_write()
        self.assertEqual(saved["state"], "pending")
        result = self.admit("pilot-session:dead", generation=saved["generation"])
        self.assertEqual((result["generation"], result["state"]), (saved["generation"], "admitted"))


class ReleaseCleanStateTests(AdmissionTestCase):
    def test_release_refused_until_the_live_state_is_clean(self):
        n = self.admit("pilot-session:r")["generation"]
        journal = self.live_journal()
        self.place(journal)                                              # SUBMITTING, read as UNKNOWN
        refusal = self.refused("release_refused", self.release, n)
        self.assertEqual(refusal.problems, ["order_unknown", "market_unresolved"])
        journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTABLE", "fields": {"bet_id": "1"}}]})
        self.assertEqual(self.refused("release_refused", self.release, n).problems, ["order_executable"])
        journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTION_COMPLETE",
             "fields": {"size_matched_p": 200, "average_price_matched_h": 350}}]})
        journal.append("halt", {"reason": "synthetic", "scope": "global"})
        self.assertEqual(self.refused("release_refused", self.release, n).problems, ["halt_active"])
        journal.append("resume", {"scope": "global"})
        # A historical kill row that a resume has cleared does not count; the KILL file does.
        journal.append("kill", {"reason": "synthetic"})
        journal.append("resume", {"scope": "global"})
        self.paths.kill_file.write_text("")
        self.assertEqual(self.refused("release_refused", self.release, n).problems, ["kill_file_engaged"])
        self.paths.kill_file.unlink()
        # P1-08's armed-session check plugs in as an extra check.
        self.assertEqual(self.refused("release_refused", self.release, n,
                                      extra_checks=[lambda paths: ["armed_session_open"]]).problems,
                         ["armed_session_open"])
        self.assertTrue(self.paths.inhibit_file.exists())
        self.assertEqual(self.release(n)["released"], True)

    def test_unreadable_journal_fails_closed(self):
        n = self.admit("pilot-session:u")["generation"]
        self.paths.journal_path("live").write_bytes(b"not a database at all " * 50)
        self.assertEqual(self.refused("release_refused", self.release, n).problems, ["live_journal_unreadable"])
        self.assertTrue(self.paths.admission_file.exists())

    def test_read_only_fold_never_writes_and_fails_closed(self):
        path = self.paths.journal_path("live")
        self.assertIsNone(read_only_fold(path, mode="live", now_ns=self.clock.time_ns()))
        self.assertFalse(path.exists())                                  # nothing created for an absent journal
        journal = self.live_journal()
        self.place(journal)
        state = read_only_fold(path, mode="live", now_ns=self.clock.time_ns())
        self.assertEqual(state["orders"][orders.REF]["state"], "UNKNOWN")
        with self.assertRaises(JournalError):
            read_only_fold(path, mode="paper", now_ns=self.clock.time_ns())
        # A connection holding the database in exclusive locking mode: the read-only reader is busy, and fails.
        import sqlite3
        journal.close()
        holder = sqlite3.connect(str(path), isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        with self.assertRaisesRegex(JournalError, "journal_unreadable_read_only"):
            read_only_fold(path, mode="live", now_ns=self.clock.time_ns(), busy_timeout_s=0.2)
        holder.execute("COMMIT")

    def test_journal_entry_that_is_not_a_plain_file_fails_closed(self):
        """REC-2: only a definite absence of the journal's directory entry means no journal. A dangling symlink, a
        symlink to a real journal, a loop, a directory or malformed SQLite are unreadable, so release refuses and
        INHIBIT stays."""
        n = self.admit("pilot-session:j")["generation"]
        path = self.paths.journal_path("live")
        elsewhere = self.paths.orders_dir / "elsewhere.sqlite"
        journal = OrderJournal(elsewhere, mode="live", clock=self.clock)
        journal.close()
        loop = self.paths.orders_dir / "loop"
        cases = [("dangling_symlink", lambda: path.symlink_to(self.paths.orders_dir / "missing.sqlite")),
                 ("symlink_to_a_journal", lambda: path.symlink_to(elsewhere)),
                 ("symlink_loop", lambda: (loop.symlink_to(path), path.symlink_to(loop))),
                 ("directory", lambda: path.mkdir()),
                 ("malformed_sqlite", lambda: path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 300))]
        for name, make in cases:
            with self.subTest(case=name):
                make()
                with self.assertRaisesRegex(JournalError, "journal_unreadable_read_only"):
                    read_only_fold(path, mode="live", now_ns=self.clock.time_ns())
                self.assertEqual(self.refused("release_refused", self.release, n).problems, ["live_journal_unreadable"])
                self.assertTrue(self.paths.inhibit_file.exists())
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink()
                if loop.is_symlink():
                    loop.unlink()
        self.assertEqual(self.release(n)["released"], True)               # truly absent: no journal, no orders

    def test_uninspectable_journal_path_fails_closed(self):
        """REC-2: EACCES on the journal's directory is not absence."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        self.assertEqual(admission.live_state_problems(self.paths, clock=self.clock), [])
        os.chmod(self.paths.orders_dir, 0)
        self.addCleanup(os.chmod, self.paths.orders_dir, 0o700)
        self.assertEqual(admission.live_state_problems(self.paths, clock=self.clock), ["live_journal_unreadable"])
        with self.assertRaisesRegex(JournalError, "journal_unreadable_read_only"):
            read_only_fold(self.paths.journal_path("live"), mode="live", now_ns=self.clock.time_ns())

    def test_orphan_inhibit_released_by_the_typed_path_only_when_clean(self):
        with file_lock(self.paths.recorder_lock):
            self.refused("recorder_lock_held_without_pid", self.admit, "arm-live:5", drain_timeout_s=1)
        self.assertFalse(self.paths.admission_file.exists())
        self.assertTrue(self.paths.inhibit_file.exists())
        self.refused("admission_not_held", self.release, 1)              # a generation cannot release it
        self.paths.kill_file.write_text("")
        self.refused("release_refused", self.release, None)
        self.paths.kill_file.unlink()
        self.assertEqual(self.release(None)["status"], "released")
        with recorder_admission(self.paths, clock=self.clock, processes=FakeProcesses()):
            pass


class BoundaryTests(unittest.TestCase):
    def test_emergency_kill_never_takes_or_waits_on_admission(self):
        """The kill path has no route into admission: killswitch.py, gateway.py and every kill helper in cli.py
        neither import admission nor name its lock or files."""
        for name in ("killswitch.py", "gateway.py"):
            text = (BETFAIR / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                for word in ("admission", "INHIBIT", "recorder_lock", "ADMISSION"):
                    self.assertNotIn(word, text)
        tree = ast.parse((BETFAIR / "cli.py").read_text(encoding="utf-8"))
        kill_functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                          and (node.name == "cmd_kill" or node.name.startswith("_kill") or node.name in (
                              "_journal_kill_incomplete", "_ledger_detail", "_existing_journal"))]
        self.assertGreaterEqual(len(kill_functions), 4)
        for function in kill_functions:
            source = ast.unparse(function)
            with self.subTest(function=function.name):
                for word in ("admission", "inhibit", "recorder_lock", "_record"):
                    self.assertNotIn(word, source.lower())
        # Recording rollout (design v9 §8 emergency paths): the shared gateway and the cancel path never reach the
        # admission module or the ownership lifecycle either (kill and cancel share _gateway and _live_gateway).
        pinned = ("_gateway", "_live_gateway", "cmd_cancel", "_order_result", "_cancel_problems", "_cancel_line",
                  "_evidence_line", "_withheld_line", "_cleared_line", "_exit_after_send", "_stopped_line")
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        for name in pinned:
            source = ast.unparse(functions[name])
            with self.subTest(function=name):
                for word in ("admission", "live_ownership", "release_own", "_manual_owned", "inhibit"):
                    self.assertNotIn(word, source.lower())

    def test_ps_start_time_is_rendered_in_utc_whatever_the_system_zone(self):
        """Workflow #1: lstart is printed in TZ (else /etc/localtime), so the identity pins TZ=UTC0 and LC_ALL=C; a
        change of the Mac's zone between a recorder's start and an admission cannot make it a stranger."""
        seen = []
        real = subprocess.run

        def spy(argv, *args, **kwargs):
            seen.append(dict(kwargs.get("env") or {}))
            return real(argv, *args, **kwargs)
        with patch.object(admission.subprocess, "run", spy):
            value = admission._ps_start(os.getpid())
        self.assertEqual((seen[0].get("TZ"), seen[0].get("LC_ALL")), ("UTC0", "C"))
        if sys.platform == "darwin":
            utc = real(["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True,
                       env={"LC_ALL": "C", "TZ": "UTC0", "PATH": "/bin:/usr/bin"}).stdout
            self.assertEqual(value, "ps-lstart:" + " ".join(utc.split()))
            with patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
                self.assertEqual(admission._ps_start(os.getpid()), value)

    def test_documented_release_rule_names_market_unresolved(self):
        """Workflow #7: the release rule the code enforces beyond v4 section E is documented where it applies."""
        integration = (REPO / "docs" / "betfair-integration.md").read_text(encoding="utf-8")
        release_item = next(line for line in integration.splitlines() if line.startswith("5. **Release**"))
        self.assertIn("`market_unresolved`", release_item)
        self.assertIn("market_unresolved", admission.__doc__ + admission.live_state_problems.__doc__)
        self.assertIn("halted or unresolved market", admission.__doc__)

    def test_record_traps_its_signals_as_its_first_statement(self):
        tree = ast.parse((BETFAIR / "cli.py").read_text(encoding="utf-8"))
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                        and node.name == "cmd_record")
        first = function.body[0]
        self.assertEqual(ast.unparse(first), "stop = _RecordStop.install()")
        record_stop = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
                           and node.name == "_RecordStop")
        self.assertIn("(signal.SIGTERM, signal.SIGINT, signal.SIGHUP)", ast.unparse(record_stop))


if __name__ == "__main__":
    unittest.main()
