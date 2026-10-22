"""The launchd recording wrapper (design v9 §4; §8 Wrapper), offline: a fake clock whose sleep moves wall time, fake
children that write synthetic session receipts and run-ledger rows, a fake disk, git and DNS, a recording fake strict
writer that can fail at any step, and temporary state roots. No process is spawned and nothing touches launchd, the
network, the real state root or a real recording. Plus the plist templates, and the native drain test (§4.9 step 2),
which starts one local Python child holding a real recorder admission and drains it through the real /bin/ps.
"""

import contextlib
from datetime import date
import io
import json
import os
from pathlib import Path
import platform
import plistlib
import secrets
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission, recording_day as rd
from betangel_research.betfair.config import config_sha256, load_config
from betangel_research.betfair.paths import FORBID_NETWORK_ENV
from betangel_research.betfair.receipts import append_jsonl, atomic_write_json, end_torn_line, utc_compact_from_ns
from betangel_research.betfair.recorder import RECEIPT_STATUS

import test_betfair_support as support
from test_betfair_admission import FakeProcesses, ledger_rows

REPO = support.REPO_ROOT
COMMIT = "c0ffee" + "0" * 34
CODE = {"betangel_research/betfair/recorder.py": "ab" * 32}
START = "2026-10-26T08:00:00.000000Z"           # GMT: London 08:00; the end instant is 23:00Z
END_ISO = "2026-10-26T23:00:00Z"
NS = rd.NS
FAILURE_ROWS = ({"kind": "login_attempt", "attempt_id": "f1", "at": "2026-10-26T07:00:00.000000Z",
                 "method": "interactive", "jurisdiction": "com", "emergency": False, "coexistence_ack": False},
                {"kind": "login_outcome", "attempt_id": "f1", "at": "2026-10-26T07:00:01.000000Z",
                 "method": "interactive", "jurisdiction": "com", "outcome": "unknown", "outcome_status": "transport",
                 "outcome_error": None, "emergency": False, "coexistence_ack": False, "ban_until": None})
NOT_SENT_ROWS = ({"kind": "login_attempt", "attempt_id": "n1", "at": "2026-10-26T07:00:00.000000Z",
                  "method": "interactive", "jurisdiction": "com", "emergency": False, "coexistence_ack": False},
                 {"kind": "login_outcome", "attempt_id": "n1", "at": "2026-10-26T07:00:01.000000Z",
                  "method": "interactive", "jurisdiction": "com", "outcome": "not_sent", "outcome_status": "transport",
                  "outcome_error": "connect_failed", "emergency": False, "coexistence_ack": False, "ban_until": None},
                 {"kind": "login_attempt", "attempt_id": "n2", "at": "2026-10-26T07:00:02.000000Z",
                  "method": "interactive", "jurisdiction": "com", "emergency": False, "coexistence_ack": False},
                 {"kind": "login_outcome", "attempt_id": "n2", "at": "2026-10-26T07:00:03.000000Z",
                  "method": "interactive", "jurisdiction": "com", "outcome": "proceed", "outcome_status": "SUCCESS",
                  "outcome_error": None, "emergency": False, "coexistence_ack": False, "ban_until": None})


class Death(BaseException):
    """The wrapper process dies here (raised out of the wrapper loop)."""


class Writer(rd.StrictWriter):
    """The real strict write on the temp root, recording each write and able to fail a chosen step of a chosen write."""

    def __init__(self, world):
        self.world = world
        self.count = 0
        self.fail = {}              # write number -> step ("temp", "replace", "dir_open", "dir_fsync", "death")
        self.after = {}             # write number -> callable run after that write returned

    def write(self, path, value):
        self.count += 1
        number = self.count
        step = self.fail.get(number)
        if step == "death":
            raise Death("before the write")
        real_open, real_fsync, real_replace = os.open, os.fsync, os.replace
        directory = str(Path(path).parent)

        def opener(target, flags, mode=0o777):
            if step == "temp" and target != directory:
                raise OSError("EIO")
            if step == "dir_open" and target == directory:
                raise OSError("EACCES")
            return real_open(target, flags, mode)

        def syncer(fd):
            if step == "dir_fsync" and os.fstat(fd).st_ino == os.stat(directory).st_ino:
                raise OSError("EIO")
            return real_fsync(fd)

        def replacer(src, dst):
            if step == "replace":
                raise OSError("EIO")
            return real_replace(src, dst)
        self.open, self.fsync, self.replace = opener, syncer, replacer
        try:
            super().write(path, value)
        finally:
            del self.open, self.fsync, self.replace
        self.world.events.append(("write", number))
        hook = self.after.get(number)
        if hook is not None:
            hook()


class FakeChild:
    """A record child: writes its receipt.partial.json at spawn (unless told not to), runs for `polls` polls, then
    finishes as its plan says; SIGTERM makes it finish with stop_requested (or with no session)."""

    def __init__(self, world, argv, plan):
        self.world, self.argv, self.plan = world, argv, plan
        self.invocation_id = argv[argv.index("--invocation-id") + 1]
        self.pid = 9000 + len(world.children)
        self.refused_outcome = plan.get("refused")
        self.stop_before_start = plan.get("stop_before_start", False)
        self.code = None
        self.terminated = False
        self.polls_left = plan.get("polls", 2)
        self.dir = None
        if plan.get("during"):
            plan["during"](world)
        if plan.get("partial", True):
            self.dir = world.session(self.invocation_id, **plan.get("partial_fields", {}))

    def poll(self):
        if self.code is not None:
            return self.code
        if self.terminated:
            return self.end(("finalized", "stop_requested") if self.dir else ("none",))
        self.polls_left -= 1
        if self.polls_left > 0:
            return None
        return self.end(self.plan.get("result", ("finalized", "end_at_reached")))

    def end(self, result):
        kind = result[0]
        if kind == "finalized":
            self.world.finalize(self.dir, self.invocation_id, result[1])
        elif kind == "no_row":
            self.world.finalize(self.dir, self.invocation_id, result[1], row=False)
        if self.plan.get("after"):
            self.plan["after"](self.world)
        self.world.events.append(("exit", self.invocation_id))
        self.code = 0 if kind != "none" or self.refused_outcome is None else 2
        return self.code

    def terminate(self):
        self.world.events.append(("sigterm", self.invocation_id))
        self.terminated = True

    def wait(self):
        while self.poll() is None:
            pass
        return self.code


class World:
    """recording_day.System for the tests."""

    def __init__(self, test, paths, start=START):
        self.test, self.paths = test, paths
        self.clock = support.FakeClock(start=start)
        self.writer = Writer(self)
        self.plans = []
        self.children = []
        self.events = []
        self.runs = []
        self.free = 10 ** 13
        self.git = {"git_revision": COMMIT, "working_tree_dirty": False}
        self.resolvable = True
        self.interp = (sys.executable, platform.python_version())
        self.package = (REPO / "betangel_research" / "__init__.py").resolve()
        self.lines = []
        self.on_sleep = None

    # System
    def spawn(self, argv, *, cwd, stderr_path):
        self.events.append(("spawn", argv[argv.index("--invocation-id") + 1]))
        if not self.plans:
            raise Death("no plan left: the test did not expect another child")
        plan = self.plans.pop(0)
        if plan == "death":
            raise Death("before the spawn")
        child = FakeChild(self, argv, plan)
        child.cwd, child.stderr_path = cwd, stderr_path
        self.children.append(child)
        return child

    def run(self, argv, *, cwd):
        self.runs.append((argv, cwd))
        self.events.append(("run", argv[argv.index("--day") - 1]))
        return 0

    def disk_free(self, path):
        return self.free

    def git_state(self, root):
        return dict(self.git)

    def resolves(self, host):
        return self.resolvable

    def interpreter(self):
        return self.interp

    def package_path(self):
        return self.package

    def install_signals(self, handler):
        return {}

    def restore_signals(self, previous):
        pass

    def log(self, line):
        self.lines.append(json.loads(line))

    # session fixtures (what a record child leaves on disk)
    def session(self, invocation_id, **fields):
        stamp = utc_compact_from_ns(self.clock.time_ns())
        directory = self.paths.recordings_dir / f"rec-{stamp}-{secrets.token_hex(4)}"
        directory.mkdir(mode=0o700, parents=True)
        config = load_config(self.paths.config_path, paths=self.paths)
        partial = {"schema_version": 1, "status": RECEIPT_STATUS, "complete": False, "segments": [], "gaps": [],
                   "session_id": directory.name, "invocation_id": invocation_id, "key_type": "delayed",
                   "data_freshness": {"label": "delayed_conflated"}, "git_revision": COMMIT,
                   "working_tree_dirty": False, "code_sha256": dict(CODE), "python": platform.python_version(),
                   "config_sha256": config_sha256(config), "stop_reason": None}
        partial.update(fields)
        atomic_write_json(directory / "receipt.partial.json", partial)
        return directory

    def finalize(self, directory, invocation_id, stop_reason, *, row=True):
        partial = json.loads((directory / "receipt.partial.json").read_text())
        receipt = dict(partial, complete=True, stop_reason=stop_reason)
        digest = atomic_write_json(directory / "receipt.json", receipt)
        if row:
            ledger = self.paths.recordings_dir / "run-ledger.jsonl"
            end_torn_line(ledger)
            append_jsonl(ledger, {"created_at": self.clock.utc_now(), "session_id": directory.name,
                                  "receipt_path": str(directory / "receipt.json"), "receipt_sha256": digest,
                                  "segments": 0, "status": RECEIPT_STATUS, "invocation_id": invocation_id})

    def codes(self, event):
        return [line for line in self.lines if line["event"] == event]


def first_wait_only():
    """Patch Wrapper.wait_for_start: the first start runs as usual, the next wait ends the run (Death)."""
    original = rd.Wrapper.wait_for_start
    calls = []

    def once(self, kind, end_ns):
        calls.append(kind)
        if len(calls) > 1:
            raise Death("the test stops at the next wait")
        return original(self, kind, end_ns)
    return patch.object(rd.Wrapper, "wait_for_start", once)


def inhibit(world):
    world.paths.inhibit_file.write_text("")


def admit_and_release(world):
    """Another owner admits and releases while the child runs: only last_generation moves."""
    config = load_config(world.paths.config_path, paths=world.paths)
    admission.admit(world.paths, owner="manual:5", purpose="manual place", config=config, clock=world.clock,
                    processes=FakeProcesses(pid=5))
    admission.release(world.paths, config=config, generation=admission.admission_status(world.paths)[
        "admission"]["generation"], clock=world.clock)


class WrapperCase(unittest.TestCase):
    start = START

    def setUp(self):
        support.block_network(self)
        self.paths = support.temp_state_paths(self)
        support.write_test_config(self.paths)
        self.world = World(self, self.paths, start=self.start)
        self.dir = Path(self.paths.root) / "recording-wrapper"
        (self.dir / "logs").mkdir(mode=0o700, parents=True)
        self.pinned = {"commit": COMMIT, "worktree": str(REPO), "interpreter": sys.executable,
                       "python": platform.python_version(), "code_sha256": dict(CODE)}
        atomic_write_json(self.dir / "pinned.json", self.pinned)

    def wrapper(self, mode="production", **kw):
        return rd.Wrapper(self.paths, mode=mode, system=self.world, **kw)

    def run_day(self, *plans, **kw):
        self.world.plans.extend(plans)
        self.wrapper_obj = self.wrapper(**kw)
        return self.wrapper_obj.run()

    def day(self, text="2026-10-26"):
        path = self.dir / f"{text}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def spawns(self):
        return [event for event in self.world.events if event[0] == "spawn"]

    def attempts(self):
        return self.day()["attempts"]


# --- preflight -----------------------------------------------------------------------------------------------------


class PreflightTests(WrapperCase):
    def refused(self, code):
        self.assertEqual(self.run_day({}), 2)
        self.assertEqual(self.spawns(), [])
        day = self.day()
        self.assertEqual((day["state"], day["close_code"], day["attempts"], day["counts"]),
                         ("stopped_for_review", code, [], {"first": 0, "restart": 0, "resumption": 0}))

    def test_interpreter_and_package_against_pinned(self):
        cases = [("interpreter_mismatch", lambda: setattr(self.world, "interp", ("/usr/bin/python3",
                                                                                 platform.python_version()))),
                 ("python_version_mismatch", lambda: setattr(self.world, "interp", (sys.executable, "3.9.6"))),
                 ("package_not_in_pinned_worktree",
                  lambda: setattr(self.world, "package", Path("/elsewhere/betangel_research/__init__.py")))]
        for code, change in cases:
            with self.subTest(code=code):
                self.setUp()
                change()
                self.refused(code)

    def test_pinned_worktree_head_and_tree(self):
        for code, git in (("pinned_head_mismatch", {"git_revision": "f" * 40, "working_tree_dirty": False}),
                          ("pinned_tree_dirty", {"git_revision": COMMIT, "working_tree_dirty": True}),
                          ("pinned_git_state_unknown", {"git_revision": None, "working_tree_dirty": None})):
            with self.subTest(code=code):
                self.setUp()
                self.world.git = git
                self.refused(code)

    def test_config_live_key_and_unreadable(self):
        support.write_test_config(self.paths, {"credentials": {"login_method": "interactive", "cert_path": None,
                                                               "key_path": None},
                                               "stream": {"app_key_slot": "live"},
                                               "recorder": {"allow_live_key": True}})
        self.refused("config_live_key")
        self.setUp()
        self.paths.config_path.write_text("{")
        self.refused("config_unreadable")

    def test_pinned_values_unreadable(self):
        (self.dir / "pinned.json").write_text("{")
        self.refused("pinned_values_unreadable")

    def test_logs_dir_missing(self):
        (self.dir / "logs").rmdir()
        self.refused("logs_dir_missing")

    def test_admission_state_unknown_login_ledger_and_torn_run_ledger(self):
        self.paths.admission_file.write_text("garbage")
        self.refused("admission_state_unknown")
        self.setUp()
        ledger_rows(self.paths, *FAILURE_ROWS)
        self.refused("login_ledger_not_clean")
        self.setUp()
        (self.paths.recordings_dir / "run-ledger.jsonl").write_bytes(b'{"torn')
        self.refused("run_ledger_torn_line")

    def test_disk_floor(self):
        floor = load_config(self.paths.config_path, paths=self.paths)["recorder"]["max_bytes"] + 5_000_000_000
        self.world.free = floor - 1
        self.refused("disk_reserve_low")
        self.setUp()
        self.world.free = floor
        self.assertEqual(self.run_day({}), 0)
        self.assertEqual(len(self.spawns()), 1)

    def test_optional_dns_wait_first_start_only_then_starts_anyway(self):
        self.world.resolvable = False
        before = self.world.clock.time_ns()
        self.assertEqual(self.run_day({}), 0)
        self.assertGreaterEqual(self.attempts()[0]["spawn_wall_ns"] - before, 120 * NS)
        self.assertEqual(len(self.spawns()), 1)


# --- the child, its argv and its output ----------------------------------------------------------------------------


class ChildTests(WrapperCase):
    def test_argv_interpreter_cwd_and_no_isolated_flag(self):
        self.assertEqual(self.run_day({}), 0)
        child = self.world.children[0]
        self.assertEqual(child.argv[:6], [sys.executable, "-E", "-s", "-m", "betangel_research.betfair", "record"])
        self.assertEqual(child.argv[6:9], ["--require-delayed-key", "--end-at", END_ISO])
        self.assertNotIn("-I", child.argv)
        self.assertNotIn("--bet-angel-paused", child.argv)
        self.assertNotIn("--max-seconds", child.argv)
        self.assertEqual(Path(child.cwd), REPO)
        self.assertEqual(child.stderr_path.parent, self.dir / "logs")

    def test_invocation_id_shape(self):
        self.assertEqual(self.run_day({}), 0)
        invocation_id = self.attempts()[0]["invocation_id"]
        self.assertRegex(invocation_id, r"^d20261026-a1-[0-9a-f]{8}$")
        self.assertTrue(admission.INVOCATION_ID_RE.fullmatch(invocation_id))

    def test_stdout_is_a_drained_pipe_keeping_two_codes_only(self):
        seen = {}

        class FakePopen:
            def __init__(self, argv, **kwargs):
                seen.update(kwargs)
                self.pid = 1
                self.stdout = io.StringIO('{"event":"segment_opened","market":"1.1"}\n'
                                          '{"refused":{"outcome":"inhibited","detail":null}}\n'
                                          'not json\n'
                                          '{"summary":{"stop_reason":"stop_requested_before_start"}}\n')

            def poll(self):
                return 0

            def wait(self):
                return 0
        stderr_path = self.dir / "logs" / "child-x.stderr"
        with patch.object(rd.subprocess, "Popen", FakePopen):
            child = rd.System().spawn(["python"], cwd=REPO, stderr_path=stderr_path)
            child.wait()
        self.assertEqual(seen["stdout"], subprocess.PIPE)
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertNotIn("env", seen)
        self.assertEqual((child.refused_outcome, child.stop_before_start), ("inhibited", True))
        self.assertEqual(oct(stderr_path.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(sorted(vars(child)), ["_thread", "pid", "process", "refused_outcome", "stop_before_start"])


# --- outcomes and the stop-reason mapping (§4.4) ----------------------------------------------------------------


class OutcomeTests(WrapperCase):
    def outcome(self, plan):
        self.run_day(plan)
        return self.attempts()[0], self.day()

    def test_day_done_stops(self):
        for stop in ("end_at_reached", "max_duration_reached", "max_seconds_reached"):
            with self.subTest(stop=stop):
                self.setUp()
                entry, day = self.outcome({"result": ("finalized", stop)})
                self.assertEqual((entry["decision"], day["state"], len(self.spawns())), ("done", "done", 1))
                self.assertTrue(day["summary"]["finalized"][0]["receipt_sha256"])

    def test_review_stops(self):
        for stop in ("max_bytes_reached", "subscription_limit_exceeded", "market_lister_failed:unexpected_result_shape",
                     "market_lister_failed:app_key_invalid", "session_unavailable:LoginBudgetExceeded",
                     "relogin_failed:auth_error", "transport:tls_verification_failed", "app_key:INVALID_APP_KEY",
                     "stream_fatal:x", "unrecognized:x", "stream_factory_mismatch:host",
                     "login_failed_this_run:unknown", "login_ledger_not_clean", "stop_requested"):
            with self.subTest(stop=stop):
                self.setUp()
                entry, day = self.outcome({"result": ("finalized", stop)})
                self.assertEqual((entry["decision"], day["state"], len(self.spawns())),
                                 ("review", "stopped_for_review", 1))
                self.assertEqual(day["close_code"], f"finalized:{stop}")

    def test_incomplete_and_receipt_without_row_and_partial_less_directory(self):
        for plan, outcome in (({"result": ("none",)}, "incomplete"),
                              ({"result": ("no_row", "end_at_reached")}, "receipt_without_ledger_row")):
            with self.subTest(outcome=outcome):
                self.setUp()
                entry, day = self.outcome(plan)
                self.assertEqual((entry["outcome"], entry["decision"], day["state"]),
                                 (outcome, "review", "stopped_for_review"))
                self.assertEqual(day["summary"]["review"][0]["outcome"], outcome)
        self.setUp()

        def bare_dir(world):
            stamp = utc_compact_from_ns(world.clock.time_ns())
            (world.paths.recordings_dir / f"rec-{stamp}-0badf00d").mkdir(parents=True)
        entry, day = self.outcome({"partial": False, "during": bare_dir, "polls": 1, "result": ("none",)})
        self.assertEqual((entry["outcome"], entry["decision"]), ("partial_less_directory", "review"))

    def test_an_older_partial_less_directory_is_not_attributed(self):
        """A rec-* directory with neither receipt file whose stamp is earlier than the spawn wall time minus 1 s is
        not this attempt's: with a touched child that printed inhibited the outcome is rule 1 (b), wait."""
        stamp = utc_compact_from_ns(self.world.clock.time_ns() - 5 * NS)
        (self.paths.recordings_dir / f"rec-{stamp}-0badf00d").mkdir(parents=True)
        self.world.plans.append({"partial": False, "during": inhibit, "refused": "inhibited", "polls": 1,
                                 "result": ("none",)})
        with first_wait_only(), contextlib.suppress(Death):
            self.wrapper().run()
        entry = self.attempts()[0]
        self.assertEqual((entry["outcome"], entry["decision"]), ("no_session", "wait"))

    def test_rule_2_keeps_the_refused_code_and_changes_nothing_else(self):
        entry, day = self.outcome({"partial": False, "refused": "recorder_identity_unknown", "polls": 1,
                                   "result": ("none",)})
        self.assertEqual((entry["outcome"], entry["refusal_code"], entry["decision"], day["state"]),
                         ("preflight_refusal", "recorder_identity_unknown", "review", "stopped_for_review"))
        self.assertEqual(day["counts"], {"first": 1, "restart": 0, "resumption": 0})

    def test_rule_1_qualifiers(self):
        cases = [("partial only", {"after": inhibit, "result": ("none",)}, "review"),
                 ("receipt, no row", {"after": inhibit, "result": ("no_row", "stop_requested")}, "review"),
                 ("finalized stop_requested", {"after": inhibit, "result": ("finalized", "stop_requested")}, "wait"),
                 ("no session, stop before start", {"partial": False, "during": inhibit, "stop_before_start": True,
                                                    "polls": 1, "result": ("none",)}, "wait"),
                 ("no session, inhibited", {"partial": False, "during": inhibit, "refused": "inhibited", "polls": 1,
                                            "result": ("none",)}, "wait")]
        for label, plan, decision in cases:
            with self.subTest(case=label):
                self.setUp()
                self.world.plans.append(plan)
                wrapper = self.wrapper()
                with first_wait_only(), contextlib.suppress(Death):
                    wrapper.run()
                self.assertEqual(self.attempts()[0]["decision"], decision)

    def test_last_generation_qualifies_a_completed_admit_and_release(self):
        for label, plan, decision in (
                ("admit and release, stop_requested",
                 {"after": admit_and_release, "result": ("finalized", "stop_requested")}, "wait"),
                ("inhibited at start, released before the read",
                 {"partial": False, "during": admit_and_release, "refused": "inhibited", "polls": 1,
                  "result": ("none",)}, "wait"),
                ("generation unchanged, stop_requested", {"result": ("finalized", "stop_requested")}, "review")):
            with self.subTest(case=label):
                self.setUp()
                self.world.plans.extend([plan, {"result": ("finalized", "end_at_reached")}])
                self.wrapper().run()
                kinds = [(a["kind"], a["decision"]) for a in self.attempts()]
                if decision == "wait":
                    self.assertEqual(kinds, [("first", "wait"), ("resumption", "done")])
                    self.assertEqual(self.day()["counts"], {"first": 1, "restart": 0, "resumption": 1})
                else:
                    self.assertEqual(kinds, [("first", "review")])

    def test_mismatch_wins_over_the_disk_classification(self):
        self.run_day({"partial_fields": {"key_type": "live"}, "polls": 50})
        entry = self.attempts()[0]
        self.assertEqual((entry["outcome"], entry["decision"]), ("post_start_mismatch:key_type", "review"))
        self.assertIn(("sigterm", entry["invocation_id"]), self.world.events)


class PostStartTests(WrapperCase):
    def test_each_field(self):
        cases = {"key_type": {"key_type": "live"}, "label": {"data_freshness": {"label": "live_conflated"}},
                 "git_revision": {"git_revision": "f" * 40}, "working_tree_dirty": {"working_tree_dirty": True},
                 "code_sha256": {"code_sha256": {"betangel_research/betfair/recorder.py": "cd" * 32}},
                 "python": {"python": "3.9.6"}, "config_sha256": {"config_sha256": "0" * 64}}
        for field, fields in cases.items():
            with self.subTest(field=field):
                self.setUp()
                self.run_day({"partial_fields": fields, "polls": 50})
                entry = self.attempts()[0]
                self.assertEqual(entry["outcome"], f"post_start_mismatch:{field}")
                self.assertEqual(self.day()["state"], "stopped_for_review")
                order = [e[0] for e in self.world.events if e[0] in ("sigterm", "exit")]
                self.assertEqual(order, ["sigterm", "exit"])            # SIGTERM, then the wait for its exit

    def test_inhibited_refusal_inside_the_poll_is_rule_1b_not_a_mismatch(self):
        self.world.plans.append({"partial": False, "during": inhibit, "refused": "inhibited", "polls": 3,
                                 "result": ("none",)})
        with first_wait_only(), contextlib.suppress(Death):
            self.wrapper().run()
        entry = self.attempts()[0]
        self.assertEqual((entry["outcome"], entry["decision"]), ("no_session", "wait"))

    def test_no_partial_at_120_s_is_sigterm_and_review(self):
        self.run_day({"partial": False, "polls": 10_000})
        entry = self.attempts()[0]
        self.assertEqual((entry["outcome"], entry["decision"]), ("post_start_mismatch:no_partial", "review"))
        self.assertIn(("sigterm", entry["invocation_id"]), self.world.events)
        self.assertGreaterEqual(self.world.clock.time_ns() - entry["spawn_wall_ns"], 120 * NS)


# --- restarts, the transient whitelist and resumptions (§4.5, §4.6) ----------------------------------------------


class RestartTests(WrapperCase):
    def test_transient_whitelist_restarts_after_15_minutes(self):
        for stop in ("market_lister_failed:connect_failed", "market_lister_failed:tls_failed",
                     "market_lister_failed:deadline_exceeded", "reconnect_budget_exhausted"):
            with self.subTest(stop=stop):
                self.setUp()
                self.run_day({"result": ("finalized", stop)}, {"result": ("finalized", "end_at_reached")})
                first, second = self.attempts()
                self.assertEqual((first["decision"], second["kind"], second["kind_count"]), ("restart", "restart", 1))
                self.assertGreaterEqual(second["spawn_wall_ns"] - first["spawn_wall_ns"], 15 * 60 * NS)
                self.assertEqual(self.day()["state"], "done")

    def test_not_sent_login_row_stays_clean_and_restarts(self):
        ledger_rows(self.paths, *NOT_SENT_ROWS)
        self.run_day({"result": ("finalized", "market_lister_failed:connect_failed")},
                     {"result": ("finalized", "end_at_reached")})
        self.assertEqual([a["kind"] for a in self.attempts()], ["first", "restart"])

    def test_outside_the_whitelist_and_a_dirty_ledger_go_to_review(self):
        for stop in ("market_lister_failed:unexpected_result_shape", "market_lister_failed:AuthError",
                     "market_lister_failed:certificate_verify_failed", "transport:tls_verification_failed",
                     "session_unavailable:LoginBudgetExceeded"):
            with self.subTest(stop=stop):
                self.setUp()
                self.run_day({"result": ("finalized", stop)})
                self.assertEqual((len(self.spawns()), self.day()["state"]), (1, "stopped_for_review"))
        self.setUp()
        self.run_day({"result": ("finalized", "market_lister_failed:connect_failed"),
                      "after": lambda world: ledger_rows(world.paths, *FAILURE_ROWS)})
        self.assertEqual((len(self.spawns()), self.day()["close_code"]), (1, "login_ledger_not_clean"))

    def test_shared_budget_two_restarts_then_review(self):
        self.run_day(*[{"result": ("finalized", "market_lister_failed:connect_failed")} for _ in range(4)])
        self.assertEqual([a["kind"] for a in self.attempts()], ["first", "restart", "restart"])
        self.assertEqual((self.day()["state"], self.day()["close_code"]), ("stopped_for_review",
                                                                           "restart_cap_reached"))
        self.assertEqual(self.day()["counts"]["restart"], 2)

    def test_less_than_30_minutes_left_records_window_exhausted(self):
        self.world.clock.advance(14 * 3600 + 20 * 60)           # 22:20Z: the restart's 15 min end after 22:30
        self.run_day({"result": ("finalized", "reconnect_budget_exhausted")})
        self.assertEqual((len(self.spawns()), self.day()["state"]), (1, "window_exhausted"))

    def test_a_three_hour_wall_jump_counts_toward_the_spacing(self):
        def sleep_through(world):
            world.clock.wall_ns += 3 * 3600 * NS                 # the Mac slept: wall time moved, monotonic did not
        self.run_day({"result": ("finalized", "reconnect_budget_exhausted"), "after": sleep_through},
                     {"result": ("finalized", "end_at_reached")})
        first, second = self.attempts()
        self.assertLess(second["spawn_wall_ns"] - first["spawn_wall_ns"], 3 * 3600 * NS + 60 * NS)
        self.assertEqual(self.world.clock.sleeps.count(rd.ADMISSION_POLL_S), 0)


class ResumptionTests(WrapperCase):
    def stop_requested_wait(self):
        return {"after": admit_and_release, "result": ("finalized", "stop_requested")}

    def test_ninth_resumption_refused(self):
        self.run_day(*[self.stop_requested_wait() for _ in range(10)])
        self.assertEqual(self.day()["counts"], {"first": 1, "restart": 0, "resumption": 8})
        self.assertEqual((self.day()["state"], self.day()["close_code"]), ("stopped_for_review",
                                                                           "resumption_cap_reached"))
        self.assertEqual(len(self.spawns()), 9)

    def test_five_minutes_after_the_observed_release(self):
        def release_later(world):
            inhibit(world)
            world.release_at = world.clock.time_ns() + 600 * NS
        self.world.plans.extend([{"after": release_later, "result": ("finalized", "stop_requested")},
                                 {"result": ("finalized", "end_at_reached")}])
        original = self.world.clock.sleep

        def sleep(seconds):
            original(seconds)
            if getattr(self.world, "release_at", None) and self.world.clock.time_ns() >= self.world.release_at:
                self.world.release_at = None
                self.paths.inhibit_file.unlink()
                self.world.released_ns = self.world.clock.time_ns()
        self.world.clock.sleep = sleep
        self.wrapper().run()
        first, second = self.attempts()
        self.assertEqual(second["kind"], "resumption")
        self.assertGreaterEqual(second["spawn_wall_ns"], self.world.released_ns + 5 * 60 * NS)

    def test_resumption_charges_no_restart_and_touched_failure_is_a_restart(self):
        self.run_day(self.stop_requested_wait(), {"result": ("finalized", "end_at_reached")})
        self.assertEqual(self.day()["counts"], {"first": 1, "restart": 0, "resumption": 1})
        self.setUp()
        self.run_day({"after": admit_and_release, "result": ("finalized", "reconnect_budget_exhausted")},
                     {"result": ("finalized", "end_at_reached")})
        self.assertEqual(self.day()["counts"], {"first": 1, "restart": 1, "resumption": 0})

    def test_every_resumption_gets_the_full_preflight(self):
        def break_git(world):
            world.git = {"git_revision": COMMIT, "working_tree_dirty": True}
        self.run_day({"after": lambda world: (admit_and_release(world), break_git(world)),
                      "result": ("finalized", "stop_requested")})
        self.assertEqual(len(self.spawns()), 1)
        self.assertEqual((self.day()["state"], self.day()["close_code"]), ("stopped_for_review", "pinned_tree_dirty"))
        self.assertEqual(self.day()["counts"]["resumption"], 0)


# --- the day file: reservation before spawn, re-entry, strict writes (RV7-1, RV8-1, RV8-2) -------------------------


class ReservationTests(WrapperCase):
    def relaunch(self, *plans):
        self.world.plans[:] = list(plans)
        self.world.events.clear()
        return self.wrapper().run()

    def test_death_before_spawn_then_relaunch(self):
        self.world.plans.append("death")
        with self.assertRaises(Death):
            self.wrapper().run()
        entry = self.attempts()[0]
        self.assertEqual((entry["state"], self.day()["counts"]["first"]), ("reserved", 1))
        self.assertEqual(self.relaunch({}), 2)
        self.assertEqual(self.spawns(), [])
        day = self.day()
        self.assertEqual((day["state"], day["close_code"], day["close_invocation_id"], day["counts"]["first"]),
                         ("stopped_for_review", "unfinished_attempt", entry["invocation_id"], 1))

    def test_death_after_the_second_restart_exits_before_its_finish_write(self):
        restart = {"result": ("finalized", "market_lister_failed:connect_failed")}
        self.world.plans.extend([dict(restart), dict(restart), dict(restart)])
        self.world.writer.fail[6] = "death"            # writes: reserve, finish, reserve, finish, reserve, (finish)
        with self.assertRaises(Death):
            self.wrapper().run()
        self.assertEqual(self.day()["counts"]["restart"], 2)
        self.assertEqual(self.attempts()[-1]["state"], "reserved")
        self.assertEqual(self.relaunch(dict(restart)), 2)
        self.assertEqual(self.spawns(), [])
        self.assertEqual((self.day()["close_code"], self.day()["counts"]["restart"]), ("unfinished_attempt", 2))

    def test_same_date_relaunch_reads_the_counts(self):
        restart = {"result": ("finalized", "reconnect_budget_exhausted")}
        self.world.plans.extend([dict(restart), dict(restart)])
        self.world.writer.fail[5] = "death"                     # dies at the third reserve: two finished, day open
        with self.assertRaises(Death):
            self.wrapper().run()
        self.assertEqual(self.day()["counts"]["restart"], 1)
        self.assertEqual(self.relaunch(dict(restart), dict(restart)), 2)
        self.assertEqual(self.day()["counts"]["restart"], 2)     # read, not reset: one more restart only
        self.assertEqual(self.day()["close_code"], "restart_cap_reached")
        self.assertEqual(len(self.spawns()), 1)

    def test_eight_resumptions_charged_no_ninth_on_relaunch(self):
        waits = [ResumptionTests.stop_requested_wait(self) for _ in range(8)]
        self.world.plans.extend(waits)
        self.world.writer.fail[17] = "death"                     # dies reserving the ninth child (resumption 8)
        with self.assertRaises(Death):
            self.wrapper().run()
        day = self.day()
        self.assertEqual(day["counts"]["resumption"], 7)
        self.assertEqual(self.relaunch(ResumptionTests.stop_requested_wait(self),
                                       ResumptionTests.stop_requested_wait(self)), 2)
        day = self.day()
        self.assertEqual((day["counts"]["resumption"], day["close_code"]), (8, "resumption_cap_reached"))
        self.assertEqual(len(self.spawns()), 1)

    def charged_open_day(self, kind, count, decision):
        """A day file left open with `count` finished attempts of kind and the last one's decision."""
        self.run_day({"result": ("finalized", "max_bytes_reached")})
        day = self.day()
        template = dict(day["attempts"][0])
        attempts = [dict(template, kind="first", kind_count=1, decision=decision)]
        for n in range(1, count + 1):
            attempts.append(dict(template, kind=kind, kind_count=n, decision=decision,
                                 invocation_id=f"d20261026-a{n + 1}-0000000{n}"))
        day.update(state="open", close_code=None, close_invocation_id=None, attempts=attempts,
                   counts={"first": 1, "restart": count if kind == "restart" else 0,
                           "resumption": count if kind == "resumption" else 0})
        atomic_write_json(self.dir / "2026-10-26.json", day)

    def test_relaunch_with_two_restarts_charged_never_starts_a_third(self):
        self.charged_open_day("restart", 2, "restart")
        self.assertEqual(self.relaunch({}), 2)
        self.assertEqual(self.spawns(), [])
        self.assertEqual((self.day()["state"], self.day()["close_code"], self.day()["counts"]["restart"]),
                         ("stopped_for_review", "restart_cap_reached", 2))

    def test_relaunch_with_eight_resumptions_charged_never_starts_a_ninth(self):
        self.charged_open_day("resumption", 8, "wait")
        self.assertEqual(self.relaunch({}), 2)
        self.assertEqual(self.spawns(), [])
        self.assertEqual((self.day()["close_code"], self.day()["counts"]["resumption"]), ("resumption_cap_reached", 8))

    def test_closed_and_unreadable_days(self):
        self.run_day({})
        self.assertEqual(self.day()["state"], "done")
        self.assertEqual(self.relaunch({}), 0)
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.world.codes("stopped")[-1]["code"], "day_already_closed")
        (self.dir / "2026-10-26.json").write_text("{")
        self.assertEqual(self.relaunch({}), 2)
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.world.codes("stopped")[-1]["code"], "day_file_unreadable")
        self.assertEqual((self.dir / "2026-10-26.json").read_text(), "{")       # left as it is

    def test_failing_reserve_write_spawns_nothing(self):
        for step in ("temp", "replace", "dir_open", "dir_fsync"):
            with self.subTest(step=step):
                self.setUp()
                self.world.writer.fail[1] = step
                self.assertEqual(self.run_day({}), 2)
                self.assertEqual(self.spawns(), [])
                self.assertEqual(self.world.codes("stopped")[-1]["code"], "day_file_write_failed")

    def test_failing_finish_write_both_sides_of_the_rename(self):
        for step, left in (("replace", "reserved"), ("dir_fsync", "finished")):
            with self.subTest(step=step):
                self.setUp()
                self.world.writer.fail[2] = step
                self.assertEqual(self.run_day({"result": ("finalized", "reconnect_budget_exhausted")},
                                              {}), 2)
                self.assertEqual(len(self.spawns()), 1)                  # nothing more started
                self.assertEqual(self.world.codes("stopped")[-1]["code"], "day_file_write_failed")
                self.assertEqual(self.attempts()[0]["state"], left)      # whichever version is on disk stays
                code = self.relaunch({"result": ("finalized", "end_at_reached")})
                if left == "reserved":
                    self.assertEqual((code, self.day()["close_code"]), (2, "unfinished_attempt"))
                    self.assertEqual(self.spawns(), [])
                else:                                                    # normal re-entry: the restart it allows
                    self.assertEqual([a["kind"] for a in self.attempts()], ["first", "restart"])
                self.assertEqual(self.day()["counts"]["first"], 1)       # never refunded

    def test_open_day_whose_last_outcome_is_review_closes_on_relaunch(self):
        self.run_day({"result": ("finalized", "max_bytes_reached")})
        day = self.day()
        day.update(state="open", close_code=None, close_invocation_id=None)       # as a death before it was written
        atomic_write_json(self.dir / "2026-10-26.json", day)
        self.assertEqual(self.relaunch({}), 0)
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.day()["state"], "stopped_for_review")
        self.assertEqual(self.world.codes("stopped")[-1]["code"], "day_already_closed")

    def test_wait_with_eight_resumptions_closes_in_the_finish_write(self):
        self.run_day(*[ResumptionTests.stop_requested_wait(self) for _ in range(9)])
        writes = [e for e in self.world.events if e[0] == "write"]
        self.assertEqual(len(writes), 18)                                 # 9 reserves + 9 finishes, nothing else
        self.assertEqual(self.day()["close_code"], "resumption_cap_reached")

    def test_review_outcome_sets_the_day_state_in_the_finish_write(self):
        self.run_day({"result": ("finalized", "max_bytes_reached")})
        self.assertEqual(len([e for e in self.world.events if e[0] == "write"]), 2)
        self.assertEqual(self.day()["state"], "stopped_for_review")

    def test_sigterm_between_reserve_and_spawn(self):
        self.world.writer.after[1] = lambda: self.wrapper_obj.handler(signal.SIGTERM, None)
        self.world.plans.append({})
        self.wrapper_obj = self.wrapper()
        self.assertEqual(self.wrapper_obj.run(), 2)
        self.assertEqual(self.spawns(), [])
        entry = self.attempts()[0]
        self.assertEqual((entry["state"], entry["outcome"]), ("finished", "wrapper_signalled_before_spawn"))
        self.assertEqual(self.day()["state"], "stopped_for_review")

    def test_sigterm_during_a_wait(self):
        self.paths.inhibit_file.write_text("")
        wrapper = self.wrapper()
        original = self.world.clock.sleep

        def sleep(seconds):
            wrapper.handler(signal.SIGTERM, None)
            return original(seconds)
        self.world.clock.sleep = sleep
        self.assertEqual(wrapper.run(), 2)
        self.assertEqual(self.spawns(), [])
        self.assertEqual((self.day()["state"], self.day()["close_code"]), ("stopped_for_review", "wrapper_signalled"))

    def test_reserve_write_returns_before_the_spawn(self):
        self.run_day({})
        self.assertEqual(self.world.events[0], ("write", 1))
        self.assertEqual(self.world.events[1][0], "spawn")
        self.assertEqual(self.world.events[-1][0] in ("write", "run"), True)


class TornLedgerTests(WrapperCase):
    def torn(self):
        ledger = self.paths.recordings_dir / "run-ledger.jsonl"
        with ledger.open("ab") as handle:
            handle.write(b'{"torn')

    def test_restart_resumption_and_next_day_refused(self):
        """The tear lands after the attempt was classified and finished (write 2), before the next start."""
        for plan in ({"result": ("finalized", "reconnect_budget_exhausted")},
                     {"result": ("finalized", "stop_requested"), "after": admit_and_release}):
            with self.subTest(plan=plan["result"]):
                self.setUp()
                self.world.writer.after[2] = self.torn
                self.run_day(plan)
                self.assertEqual(len(self.spawns()), 1)
                self.assertEqual(self.day()["close_code"], "run_ledger_torn_line")
                self.assertEqual(sum(self.day()["counts"].values()), 1)
        self.setUp()
        self.torn()
        self.world.clock.advance(86400)
        self.assertEqual(self.run_day({}), 2)
        self.assertEqual((self.spawns(), self.day("2026-10-27")["close_code"]), ([], "run_ledger_torn_line"))

    def test_after_the_repair_row(self):
        self.torn()
        self.run_day({})
        self.assertEqual(self.day()["close_code"], "run_ledger_torn_line")
        from betangel_research.betfair.recordings import run_ledger_scan
        ledger = self.paths.recordings_dir / "run-ledger.jsonl"
        bad = run_ledger_scan(ledger.read_bytes())["bad"]
        end_torn_line(ledger)
        append_jsonl(ledger, {"kind": "run_ledger_repair", "created_at": "2026-10-26T09:00:00.000000Z",
                              "reviewed_ledger_sha256": "0" * 64,
                              "exempt_lines": [{"line": i, "sha256": h} for i, h in bad]})
        self.world.events.clear()
        self.assertEqual(self.wrapper().run(), 0)
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.world.codes("stopped")[-1]["code"], "day_already_closed")
        self.world.clock.advance(86400)
        self.world.plans.append({})
        self.assertEqual(self.wrapper().run(), 0)
        self.assertEqual(len(self.spawns()), 1)
        self.assertEqual(self.day("2026-10-27")["state"], "done")


# --- dates, the end instant and the start bound (§4.2) -----------------------------------------------------------


class DateTests(unittest.TestCase):
    def ns(self, text):
        from betangel_research.betfair.receipts import parse_utc_ns
        return parse_utc_ns(text)

    def test_end_instants(self):
        self.assertEqual(rd.utc_iso(rd.end_instant_ns(date(2026, 10, 24))), "2026-10-24T22:00:00Z")
        for now in ("2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z"):        # before and after the 01:00Z switch
            with self.subTest(now=now):
                day = rd.london_date(self.ns(now))
                self.assertEqual((day, rd.utc_iso(rd.end_instant_ns(day))), (date(2026, 10, 25), "2026-10-25T23:00:00Z"))
        self.assertEqual(rd.utc_iso(rd.end_instant_ns(date(2026, 10, 26))), "2026-10-26T23:00:00Z")
        self.assertEqual(rd.utc_iso(rd.end_instant_ns(date(2027, 3, 28))), "2027-03-28T22:00:00Z")

    def test_london_date_whatever_the_process_tz(self):
        if not hasattr(time, "tzset"):
            self.skipTest("no tzset")
        previous = os.environ.get("TZ")
        self.addCleanup(lambda: (os.environ.__setitem__("TZ", previous) if previous is not None
                                 else os.environ.pop("TZ", None), time.tzset()))
        os.environ["TZ"] = "Pacific/Kiritimati"                             # UTC+14
        time.tzset()
        self.assertEqual(rd.london_date(self.ns("2026-10-26T23:30:00Z")), date(2026, 10, 26))
        os.environ["TZ"] = "Pacific/Pago_Pago"                              # UTC-11
        time.tzset()
        self.assertEqual(rd.london_date(self.ns("2026-10-26T00:30:00Z")), date(2026, 10, 26))


class StartBoundTests(WrapperCase):
    start = "2026-10-26T00:30:00.000000Z"

    def test_a_fire_at_00_30_waits_until_07_00_and_keeps_the_23_00_end(self):
        self.assertEqual(self.run_day({}), 0)
        entry = self.attempts()[0]
        self.assertGreaterEqual(entry["spawn_wall_ns"], DateTests.ns(self, "2026-10-26T07:00:00Z"))
        self.assertIn(END_ISO, self.world.children[0].argv)

    def test_a_fire_later_than_end_minus_30_minutes(self):
        self.world.clock.advance((22 * 3600 + 15 * 60))                     # 22:45Z
        self.assertEqual(self.run_day({}), 0)
        self.assertEqual(self.spawns(), [])
        self.assertEqual(self.day()["state"], "window_exhausted")


# --- flags, attended runs and dry runs (§4.1, §4.9) --------------------------------------------------------------


class FlagTests(WrapperCase):
    def main(self, *argv):
        return rd.main(list(argv), paths=self.paths, system=self.world)

    def test_flag_refusals(self):
        self.assertEqual(self.main("--launchd", "--end-at", "2026-10-26T09:00:00Z"), 2)
        self.assertEqual(self.main("--attended", "--launchd", "--end-at", "2026-10-26T09:00:00Z"), 2)
        self.assertEqual(self.main(), 2)
        self.assertEqual([line.get("code") for line in self.world.lines],
                         ["end_at_needs_attended", "attended_with_launchd", "launchd_or_attended_required"])
        self.assertEqual(self.main("--attended", "--end-at", "2026-10-26T08:20:00Z"), 2)          # 20 min ahead
        self.assertEqual(self.world.codes("stopped")[-1]["code"], "end_at_too_soon")
        self.assertEqual(self.spawns(), [])

    def test_attended_runs_keep_their_own_record(self):
        self.world.plans.extend([{}, {}])
        self.assertEqual(self.main("--attended", "--end-at", "2026-10-26T08:45:00Z"), 0)
        self.assertEqual(self.main("--attended", "--end-at", "2026-10-26T09:45:00Z"), 0)
        self.assertIsNone(self.day())                                       # the production day file untouched
        records = sorted(self.dir.glob("2026-10-26-attended-*.json"))
        self.assertEqual(len(records), 2)
        attempt = json.loads(records[0].read_text())["attempt"]
        self.assertEqual((attempt["state"], attempt["decision"], attempt["stop_reason"]),
                         ("finished", "done", "end_at_reached"))
        self.assertIn("2026-10-26T08:45:00Z", self.world.children[0].argv)

    def test_dry_run_starts_nothing_and_writes_nothing(self):
        self.assertEqual(self.main("--launchd", "--dry-run"), 0)
        (line,) = self.world.codes("dry_run")
        self.assertEqual((line["preflight"], line["end_at"], line["interpreter"], line["next_kind"]),
                         ("ok", END_ISO, sys.executable, "first"))
        self.assertEqual(line["argv"][:3], [sys.executable, "-E", "-s"])
        self.assertEqual(self.spawns(), [])
        self.assertIsNone(self.day())


class SignalTests(WrapperCase):
    def test_sigterm_is_forwarded_to_the_child(self):
        wrapper = self.wrapper()
        original = self.world.clock.sleep

        def sleep(seconds):
            if self.world.children and not wrapper.signalled:
                wrapper.handler(signal.SIGTERM, None)
            return original(seconds)
        self.world.clock.sleep = sleep
        self.world.plans.append({"polls": 50})
        self.assertEqual(wrapper.run(), 2)
        child = self.world.children[0]
        self.assertTrue(child.terminated)
        entry = self.attempts()[0]
        self.assertEqual((entry["outcome"], entry["stop_reason"], entry["decision"]),
                         ("finalized", "stop_requested", "review"))


class NeverTouchesTests(WrapperCase):
    def test_no_lock_config_or_pinned_write_and_index_only_after_exit(self):
        guarded = {"admission.lock", ".recorder.lock", "config.json", "pinned.json", "recorder.pid"}
        writes = []
        real_os_open = os.open
        import builtins
        real_open = builtins.open

        def os_open(path, flags, *args, **kwargs):
            if Path(str(path)).name in guarded and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                writes.append(str(path))
            return real_os_open(path, flags, *args, **kwargs)

        def py_open(file, mode="r", *args, **kwargs):
            if Path(str(file)).name in guarded and any(c in mode for c in "wax+"):
                writes.append(str(file))
            return real_open(file, mode, *args, **kwargs)
        with patch.object(os, "open", os_open), patch.object(builtins, "open", py_open), \
                patch.object(io, "open", py_open), \
                patch.object(admission, "recorder_lock_held", side_effect=AssertionError("recorder_lock_held")):
            self.assertEqual(self.run_day({"result": ("finalized", "end_at_reached")}), 0)
        self.assertEqual(writes, [])
        kinds = [e[0] for e in self.world.events]
        self.assertLess(kinds.index("exit"), kinds.index("run"))
        commands = [argv[argv.index("-m") + 2:argv.index("--day")] for argv, _ in self.world.runs]
        self.assertEqual(commands, [["verify-day"], ["recordings", "index"]])
        self.assertEqual({str(cwd) for _, cwd in self.world.runs}, {str(REPO)})
        self.assertEqual(sorted(p.name for p in self.paths.recordings_dir.iterdir()
                                if not p.name.startswith("rec-")), ["run-ledger.jsonl"])


# --- the plist templates (§4.1, §4.8) -----------------------------------------------------------------------------


class PlistTemplateTests(unittest.TestCase):
    def load(self, name, **values):
        text = (REPO / "ops" / "launchd" / name).read_text()
        values = dict({"INTERPRETER": "/opt/homebrew/bin/python3.14", "PINNED_WORKTREE": "/Users/x/pinned",
                       "STATE_ROOT": "/Users/x/Library/Application Support/betangel-research/betfair",
                       "END_AT": "2026-10-26T09:00:00Z"}, **values)
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        self.assertNotIn("{{", text)
        return plistlib.loads(text.encode())

    def test_production(self):
        plist = self.load("recording-day.plist.template")
        self.assertEqual(plist["ProgramArguments"], ["/opt/homebrew/bin/python3.14", "-E", "-s", "-m",
                                                     "betangel_research.betfair.recording_day", "--launchd"])
        self.assertEqual(plist["WorkingDirectory"], "/Users/x/pinned")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 7, "Minute": 0})
        self.assertEqual((plist["RunAtLoad"], plist["KeepAlive"], plist["ExitTimeOut"], plist["ProcessType"]),
                         (False, False, 180, "Standard"))
        for key in ("StandardOutPath", "StandardErrorPath"):
            self.assertTrue(plist[key].startswith("/Users/x/Library/Application Support/betangel-research/betfair/"
                                                  "recording-wrapper/logs/"))
        self.assertNotIn("EnvironmentVariables", plist)
        self.assertNotIn("-I", plist["ProgramArguments"])

    def test_test_label(self):
        plist = self.load("recording-day-test.plist.template")
        self.assertNotEqual(plist["Label"], self.load("recording-day.plist.template")["Label"])
        self.assertEqual(plist["ProgramArguments"][-3:], ["--attended", "--end-at", "2026-10-26T09:00:00Z"])
        self.assertNotIn("--launchd", plist["ProgramArguments"])
        self.assertNotIn("StartCalendarInterval", plist)
        self.assertEqual((plist["RunAtLoad"], plist["KeepAlive"], plist["ExitTimeOut"]), (False, False, 180))
        self.assertNotIn("EnvironmentVariables", plist)


# --- the native drain test (§4.9 step 2) --------------------------------------------------------------------------


class NativeDrainTest(unittest.TestCase):
    """A real child enters the real recorder_admission (real /bin/ps start time) and sleeps; admit drains it with
    SIGTERM within the 90 s bound. Offline: temp state root, no network."""

    def test_native_drain(self):
        if sys.platform == "darwin" and not os.path.exists(admission.PS_BIN):
            self.skipTest("/bin/ps unavailable")
        support.block_network(self)
        paths = support.temp_state_paths(self)
        config = load_config(support.write_test_config(paths), paths=paths)
        child_code = ("import signal, sys, time\n"
                      "from betangel_research.betfair.paths import StatePaths\n"
                      "from betangel_research.betfair.admission import recorder_admission\n"
                      "paths = StatePaths(home=sys.argv[1], root_override=sys.argv[2], env={'%s': '1'})\n"
                      "stop = []\n"
                      "signal.signal(signal.SIGTERM, lambda *a: stop.append(1))\n"
                      "with recorder_admission(paths, invocation_id='native-drain-1'):\n"
                      "    print('ready', flush=True)\n"
                      "    while not stop:\n"
                      "        time.sleep(0.05)\n" % FORBID_NETWORK_ENV)
        env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1", **{FORBID_NETWORK_ENV: "1"})
        child = subprocess.Popen([sys.executable, "-c", child_code, str(paths.home), str(paths.root)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(), child.stdout.close(),
                                 child.stderr.close()))
        self.assertEqual(child.stdout.readline().strip(), "ready", child.stderr.read() if child.poll() else "")
        record = json.loads(paths.recorder_pid_file.read_text())
        self.assertEqual(record["pid"], child.pid)
        self.assertEqual(admission.SYSTEM_PROCESSES.start_time(child.pid), record["start_time"])  # the real ps
        started = time.monotonic()
        result = admission.admit(paths, owner="manual:1", purpose="native drain", config=config,
                                 processes=admission.SYSTEM_PROCESSES)
        self.assertEqual(result["recorder"], "stopped")
        self.assertLess(time.monotonic() - started, 90)
        self.assertEqual(child.wait(timeout=30), 0)
        admission.release(paths, config=config, generation=result["generation"])


if __name__ == "__main__":
    unittest.main()
