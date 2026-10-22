"""P1-08's one ownership lifecycle (design v4 §3.4; §9.4; Astra P108-4 to P108-6, P108-R2-3, P108-R2-4), offline.

These show the owner lock taken before any admission and held through release, owner_live while it is held, the
held-owner-lock before-admit phase that refuses with nothing admitted, written or drained, a fresh ticket refusing a
retained ADMISSION and noting a stray INHIBIT, the restricted continuation (explicit generation, admitted only, never
places or releases, cleanup per command), release_own's own extra checks (the lock still held, ADMISSION still exactly
its own), the probe recovery release will use, the kernel letting go of a SIGKILLed holder, and the AST pin that
admit and release are called only from the lifecycle. Temporary state roots, a fake clock and fake process tables;
the subprocess test starts a local Python child that takes the owner lock and nothing else.
"""

import ast
import json
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission
from betangel_research.betfair.admission import (OwnershipRefused, admission_status, live_ownership,
                                                 owner_lock_state, recorder_admission, recorder_lock_held,
                                                 release_own)
from betangel_research.betfair.config import validate_config
from betangel_research.betfair.errors import AdmissionRefused
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, file_lock

import test_betfair_support as support
from test_betfair_admission import FakeProcesses, ledger_rows

REPO = support.REPO_ROOT
PACKAGE = REPO / "betangel_research"
BETFAIR = PACKAGE / "betfair"


class OwnershipCase(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock()
        self.config = validate_config(support.make_test_config(self.paths), paths=self.paths)
        self.processes = FakeProcesses(pid=1)

    def own(self, owner="pilot-session:s20270104-0a1b2c3d", **kwargs):
        kwargs.setdefault("purpose", None if kwargs.get("continue_generation") else "RS-19 pilot synthetic")
        return live_ownership(self.paths, owner=owner, config=self.config, clock=self.clock,
                              processes=kwargs.pop("processes", self.processes), **kwargs)

    def files(self):
        """The recording state files' bytes (None when absent)."""
        found = {}
        for path in (self.paths.admission_file, self.paths.inhibit_file, self.paths.admission_generation_file,
                     self.paths.recorder_pid_file):
            found[path.name] = path.read_bytes() if path.exists() else None
        return found

    def retained(self, owner="pilot-session:s-old"):
        """An abnormal earlier owner: admitted, never released (its process gone)."""
        return admission.admit(self.paths, owner=owner, purpose="earlier session", config=self.config,
                               clock=self.clock, processes=self.processes)

    def hold_recorder(self):
        context = recorder_admission(self.paths, invocation_id="rec-1", clock=self.clock,
                                     processes=FakeProcesses())
        context.__enter__()
        self.addCleanup(lambda: context.__exit__(None, None, None))

    def retained_beside_a_recorder(self):
        """A retained admitted generation written beside a running recorder (an old recorder the rollout has not
        retired yet): the state a before-admit refusal must leave exactly as it found it. -> its generation."""
        admission.atomic_write_json(self.paths.admission_generation_file, {
            "schema_version": 1, "generation": 4, "updated_at": "2020-01-01T12:00:00.000000Z"})
        admission.atomic_write_json(self.paths.admission_file, {
            "schema_version": 1, "generation": 4, "owner": "pilot-session:s-old", "purpose": "earlier session",
            "created_at": "2020-01-01T12:00:00.000000Z", "state": "admitted"})
        self.paths.inhibit_file.write_text("")
        return 4


class FreshTests(OwnershipCase):
    def test_owner_lock_before_admit_and_held_through_release(self):
        seen = []
        real_admit = admission.admit

        def spy(*args, **kwargs):
            seen.append(("admit", self.lock_state_from_another_description()))
            return real_admit(*args, **kwargs)

        with patch.object(admission, "admit", side_effect=spy):
            with self.own() as ticket:
                self.assertEqual((ticket.kind, ticket.placement_allowed, ticket.inhibit_preexisting),
                                 ("fresh", True, False))
                self.assertTrue(ticket.holds())
                self.assertEqual(admission_status(self.paths)["admission"]["state"], "admitted")
                result = release_own(ticket, self.paths, config=self.config, clock=self.clock)
                self.assertEqual((result["released"], result["generation"]), (True, ticket.generation))
                self.assertTrue(ticket.holds())                       # still held after its own release
            self.assertFalse(ticket.holds())
        self.assertEqual(seen, [("admit", "held")])
        self.assertEqual(owner_lock_state(self.paths), "free")
        self.assertEqual((self.paths.admission_file.exists(), self.paths.inhibit_file.exists()), (False, False))

    def lock_state_from_another_description(self):
        """The lock seen by a second open file description (what another process would see)."""
        return owner_lock_state(self.paths)

    def test_owner_live_while_held(self):
        with self.own():
            before = self.files()
            with self.assertRaises(OwnershipRefused) as caught:
                with self.own(owner="pilot-capture:77"):
                    pass
            self.assertEqual(caught.exception.code, "owner_live")
            self.assertEqual(self.files(), before)

    def test_admission_held_with_a_retained_owner(self):
        self.retained()
        before = self.files()
        with self.assertRaises(OwnershipRefused) as caught:
            with self.own():
                pass
        self.assertEqual((caught.exception.code, caught.exception.owner, caught.exception.generation,
                          caught.exception.state), ("admission_held", "pilot-session:s-old", 1, "admitted"))
        self.assertEqual(self.files(), before)
        self.assertEqual(owner_lock_state(self.paths), "free")

    def test_inhibit_preexisting_blocks_release_own_and_names_n(self):
        self.paths.inhibit_file.write_text("")                       # a stray INHIBIT: no ADMISSION
        with self.own() as ticket:
            self.assertTrue(ticket.inhibit_preexisting)
            self.assertEqual(admission_status(self.paths)["admission"]["generation"], ticket.generation)
            with patch.object(admission, "release", side_effect=AssertionError("release called")):
                result = release_own(ticket, self.paths, config=self.config, clock=self.clock)
        self.assertEqual((result["released"], result["outcome"], result["generation"]),
                         (False, "inhibit_preexisting", 1))
        self.assertTrue(self.paths.admission_file.exists())
        self.assertTrue(self.paths.inhibit_file.exists())

    def test_release_own_refuses_a_substituted_admission(self):
        with self.own() as ticket:
            record = json.loads(self.paths.admission_file.read_text())
            record["owner"] = "manual:4242"                              # another owner, the same generation N
            admission.atomic_write_json(self.paths.admission_file, record)
            before = self.files()
            result = release_own(ticket, self.paths, config=self.config, clock=self.clock)
        self.assertEqual((result["released"], result["outcome"]), (False, "release_refused"))
        self.assertIn("admission_owner_changed", result["problems"])
        self.assertEqual(self.files(), before)

    def test_release_own_refuses_once_the_lock_is_lost(self):
        with self.own() as ticket:
            os.unlink(self.paths.live_owner_lock)                         # replaced under the holder
            result = release_own(ticket, self.paths, config=self.config, clock=self.clock)
        self.assertIn("owner_lock_lost", result["problems"])
        self.assertTrue(self.paths.admission_file.exists())

    def test_release_own_never_probes_its_own_lock(self):
        with self.own() as ticket, patch.object(admission, "owner_lock_state",
                                                side_effect=AssertionError("probed its own lock")):
            self.assertTrue(release_own(ticket, self.paths, config=self.config, clock=self.clock)["released"])

    def test_owner_state_unknown_and_purpose(self):
        with patch.object(admission, "file_lock", side_effect=PermissionError("denied")):
            with self.assertRaises(OwnershipRefused) as caught:
                with self.own():
                    pass
        self.assertEqual(caught.exception.code, "owner_state_unknown")
        with self.assertRaises(ValueError):
            with self.own(purpose=None):
                pass


class BeforeAdmitTests(OwnershipCase):
    """P108-R2-4: a before-admit refusal changes nothing: no admit, no drain, the recorder still running."""

    def test_refusal_changes_nothing(self):
        for retained in (False, True):
            with self.subTest(retained_admission=retained):
                self.setUp()
                self.hold_recorder()
                if retained:
                    generation = self.retained_beside_a_recorder()
                before = self.files()
                calls = []
                processes = FakeProcesses(pid=1, starts={4242: "start-A"},
                                          on_terminate=lambda pid: calls.append(pid))
                with patch.object(admission, "admit", side_effect=AssertionError("admit called")):
                    with self.assertRaises(OwnershipRefused) as caught:
                        kwargs = {"continue_generation": generation, "owner": "pilot-capture:9"} if retained else {}
                        with self.own(before_admit=lambda paths: ["pilot_session_armed", "kill_in_progress"],
                                      processes=processes, **kwargs):
                            pass
                self.assertEqual((caught.exception.code, caught.exception.problems),
                                 ("pilot_session_armed", ["pilot_session_armed", "kill_in_progress"]))
                self.assertEqual((self.files(), calls), (before, []))
                self.assertTrue(recorder_lock_held(self.paths, clock=self.clock))
                self.assertEqual(owner_lock_state(self.paths), "free")

    def test_runs_with_the_owner_lock_held_and_before_any_admission(self):
        seen = []

        def check(paths):
            seen.append((owner_lock_state(paths), admission_status(paths)["admission"]))
            return []

        with self.own(before_admit=check) as ticket:
            self.assertEqual(ticket.kind, "fresh")
        self.assertEqual(seen, [("held", None)])


class ContinuationTests(OwnershipCase):
    def test_explicit_generation_admitted_only_and_never_releases(self):
        generation = self.retained()["generation"]
        before = self.files()
        with self.own(owner="pilot-capture:9", continue_generation=generation, cleanup=False) as ticket:
            self.assertEqual((ticket.kind, ticket.placement_allowed, ticket.owner, ticket.command_owner,
                              ticket.generation), ("continuation", False, "pilot-session:s-old", "pilot-capture:9",
                                                   generation))
            with patch.object(admission, "release", side_effect=AssertionError("release called")):
                result = release_own(ticket, self.paths, config=self.config, clock=self.clock)
            self.assertEqual((result["released"], result["outcome"]), (False, "continuation_never_releases"))
        self.assertEqual(self.files(), before)                         # ADMISSION and INHIBIT unchanged
        for wrong in (generation + 1,):
            with self.subTest(generation=wrong), self.assertRaises(OwnershipRefused) as caught:
                with self.own(owner="pilot-capture:9", continue_generation=wrong):
                    pass
            self.assertEqual(caught.exception.code, "continuation_generation_mismatch")
        with self.assertRaises(ValueError):
            with self.own(owner="pilot-capture:9", continue_generation=0):
                pass

    def test_a_pending_generation_is_refused(self):
        record = {"schema_version": 1, "generation": 3, "owner": "pilot-session:s-dead", "purpose": "died",
                  "created_at": "2020-01-01T12:00:00.000000Z", "state": "pending"}
        admission.atomic_write_json(self.paths.admission_file, record)
        self.paths.inhibit_file.write_text("")
        with self.assertRaises(OwnershipRefused) as caught:
            with self.own(owner="pilot-capture:9", continue_generation=3):
                pass
        self.assertEqual((caught.exception.code, caught.exception.state),
                         ("continuation_needs_admitted_generation", "pending"))
        with self.assertRaises(OwnershipRefused) as caught:
            with self.own(owner="pilot-capture:9", continue_generation=3):
                pass

    def test_cleanup_true_skips_step_8_and_false_runs_it(self):
        generation = self.retained()["generation"]
        ledger_rows(self.paths, {"kind": "login_attempt", "attempt_id": "a1", "at": "2020-01-01T11:00:00.000000Z",
                                 "method": "interactive", "jurisdiction": "com", "emergency": False,
                                 "coexistence_ack": False},
                    {"kind": "login_outcome", "attempt_id": "a1", "at": "2020-01-01T11:00:01.000000Z",
                     "method": "interactive", "jurisdiction": "com", "outcome": "auth_error",
                     "outcome_status": "FAIL", "outcome_error": None, "emergency": False, "coexistence_ack": False,
                     "ban_until": None})
        before = self.files()
        with self.assertRaises(AdmissionRefused) as caught:
            with self.own(owner="pilot-capture:9", continue_generation=generation, cleanup=False):
                pass
        self.assertEqual(caught.exception.outcome, "login_ledger_not_clean")
        self.assertEqual(self.files(), before)
        with self.own(owner="reconcile:9", continue_generation=generation, cleanup=True) as ticket:
            self.assertEqual(ticket.kind, "continuation")
        self.assertEqual(self.files(), before)


class ProbeAndKernelTests(OwnershipCase):
    def test_the_recovery_probe_sees_both_p108_6_gaps(self):
        """Yu's typed recording-release (admission.recording_release) takes the owner lock itself with LOCK_NB, never
        probing first (design v9 §3.5 step 1): it refuses owner_live while an owner is paused before journaling
        (admitted, lock held) or logging out after a clean result (released, lock held). status's probe here,
        owner_lock_state, sees both as held, and an unreadable lock as unknown."""
        with self.own() as ticket:
            self.assertEqual(owner_lock_state(self.paths), "held")               # paused before its intent
            release_own(ticket, self.paths, config=self.config, clock=self.clock)
            self.assertEqual(owner_lock_state(self.paths), "held")               # logging out after release
        self.assertEqual(owner_lock_state(self.paths), "free")
        with patch.object(admission, "file_lock", side_effect=OSError("io")):
            self.assertEqual(owner_lock_state(self.paths), "unknown")

    def test_kernel_lets_go_after_a_sigkill(self):
        child_code = (
            "import sys, time\n"
            "from betangel_research.betfair.paths import StatePaths, file_lock\n"
            "paths = StatePaths(home=sys.argv[1], root_override=sys.argv[2], env={'%s': '1'})\n"
            "with file_lock(paths.live_owner_lock):\n"
            "    print('ready', flush=True)\n"
            "    time.sleep(60)\n" % FORBID_NETWORK_ENV)
        env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1", **{FORBID_NETWORK_ENV: "1"})
        child = subprocess.Popen([sys.executable, "-c", child_code, str(self.paths.home), str(self.paths.root)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(), child.stdout.close(),
                                 child.stderr.close()))
        self.assertEqual(child.stdout.readline().strip(), "ready", child.stderr.read() if child.poll() else "")
        self.assertEqual(owner_lock_state(self.paths), "held")
        with self.assertRaises(OwnershipRefused):
            with self.own():
                pass
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=20)
        self.assertEqual(owner_lock_state(self.paths), "free")
        with self.own() as ticket:
            self.assertTrue(ticket.holds())


class LifecycleBoundaryTests(unittest.TestCase):
    """§9 boundary tests (LC-8): calls to admit occur only in admission.live_ownership, calls to release only in
    admission.release_own, and no module but admission.py imports either name from admission. The allowed set is a
    literal; the rollout PR adds exactly one entry, for Yu's typed recording-release (design v9 §3.5)."""

    ALLOWED = {("admission.py", "live_ownership", "admit"), ("admission.py", "release_own", "release"),
               ("admission.py", "recording_release", "release")}

    def test_admit_and_release_called_only_from_the_lifecycle(self):
        found = set()
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef,
                                                                                   ast.AsyncFunctionDef))]:
                for call in [n for n in ast.walk(function) if isinstance(n, ast.Call)]:
                    func = call.func
                    name = func.id if isinstance(func, ast.Name) else (
                        func.attr if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                        and func.value.id == "admission" else None)
                    if name in ("admit", "release"):
                        found.add((path.name, function.name, name))
            if path != BETFAIR / "admission.py":
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("admission"):
                        names = {alias.name for alias in node.names}
                        with self.subTest(file=str(path.relative_to(REPO))):
                            self.assertFalse(names & {"admit", "release"})
        self.assertEqual(found, self.ALLOWED)
        self.assertEqual(len(self.ALLOWED), 3)


if __name__ == "__main__":
    unittest.main()
