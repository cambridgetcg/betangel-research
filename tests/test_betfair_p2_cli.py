"""Pilot 2's command line (cli.py: init-config --p2, the kill's extra ref, p2-run and p2-summary), offline.

These run cli.main in process over the gateway fakes (test_betfair_support_gateway) and show: init-config --p2 writes
config-p2.json once (O_EXCL) and leaves config.json alone; --config naming config-p2.json is refused outside the test
harness; the own-scope kill adds p2-lp to its sweep, even on an untrusted journal, only when config-p2.json exists,
and also when that check raises; and p2-run and p2-summary are live only and reach the executor with config-p2 and a
factory that opens an armed place gateway holding the Pilot 2 context. The executor (p2_run) is a stand-in here: its
own behaviour, p2-summary's counts and --cleared's figures among it, is tested with it (test_betfair_p2_run). No
/usr/bin/security, git or other child process runs, and nothing opens the network.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from betangel_research.betfair import cli, pilot_run
from betangel_research.betfair import config as cfg
from betangel_research.betfair.credentials import anchor_write, load_credentials
from betangel_research.betfair.errors import ConfigError
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, StatePaths

import test_betfair_support as support
import test_betfair_support_gateway as gw
from test_betfair_killswitch import run_cli_main
from test_betfair_p2_gateway import P2_EXAMPLE, arm_record, fake_git, no_children, p2_orders, p2_token

def run_main(argv, deps=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, deps=deps)
    return code, out.getvalue(), err.getvalue()


def stand_in_executor(test, **functions):
    """cli._p2_executor returns a stand-in p2_run holding only the given functions, for this test."""
    patcher = patch.object(cli, "_p2_executor", return_value=types.SimpleNamespace(**functions))
    patcher.start()
    test.addCleanup(patcher.stop)


def with_config_p2(env):
    support.write_test_config(env.paths, support.deep_merge(gw.LIVE_OVERRIDES, p2_orders()),
                              path=env.paths.config_p2_path)
    env.p2 = cfg.load_config_p2(paths=env.paths)
    return env


class InitConfig(unittest.TestCase):
    def setUp(self):
        fake_git(self)
        support.block_network(self)
        no_children(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / "home"
        self.home.mkdir()
        self.deps = {"home": self.home, "keychain_runner": support.FakeKeychainRunner(items={}),
                     "clock": support.FakeClock()}
        self.paths = StatePaths(home=self.home, env=dict(support.FORBID_ENV))

    def test_init_config_p2_excl(self):
        code, out, err = run_main(["init-config", "--p2"], self.deps)
        self.assertEqual(code, 0, err)
        created = json.loads(out)
        self.assertEqual((created["status"], Path(created["path"])), ("config_created", self.paths.config_p2_path))
        self.assertEqual(self.paths.config_p2_path.stat().st_mode & 0o777, 0o600)
        example = json.loads(P2_EXAMPLE.read_text(encoding="utf-8"))
        example["profile"] = self.paths.profile
        self.assertEqual(json.loads(self.paths.config_p2_path.read_text(encoding="utf-8")), example)
        self.assertEqual(created["config_sha256"], cfg.config_sha256(cfg.load_config_p2(paths=self.paths)))
        self.assertFalse(self.paths.config_path.exists())
        # O_EXCL: an existing config-p2.json is never overwritten, whatever it holds.
        kept = dict(example, schema_version=1)
        self.paths.config_p2_path.write_text(json.dumps(kept, indent=4), encoding="utf-8")
        before = self.paths.config_p2_path.read_bytes()
        code, out, err = run_main(["init-config", "--p2"], self.deps)
        self.assertEqual((code, json.loads(out)["status"]), (0, "config_exists"), err)
        self.assertEqual(self.paths.config_p2_path.read_bytes(), before)
        # The plain init-config still writes config.json alone.
        code, out, err = run_main(["init-config"], self.deps)
        self.assertEqual((code, Path(json.loads(out)["path"])), (0, self.paths.config_path), err)
        self.assertEqual(self.paths.config_p2_path.read_bytes(), before)

    def test_load_config_refuses_config_p2(self):
        run_main(["init-config", "--p2"], self.deps)
        argv = ["--config", str(self.paths.config_p2_path), "place", "--market", "1.1", "--selection", "1", "--side",
                "BACK", "--price", "3.5", "--size", "2"]
        # Outside the harness neither the CLI nor load_config takes config-p2.json for a manual command; load_config_p2
        # (p2-run's own loader) reads its own path only.
        with patch.dict(os.environ):
            os.environ.pop(FORBID_NETWORK_ENV, None)
            code, out, err = run_main(argv)
            self.assertEqual((code, out, err.strip()), (2, "", "Betfair input error: override_not_allowed"))
            with self.assertRaisesRegex(ConfigError, "config_override_not_allowed"):
                cfg.load_config(self.paths.config_p2_path, paths=self.paths)
            self.assertEqual(cfg.load_config_p2(paths=self.paths)["orders"]["customer_strategy_ref"], "p2-lp")
        self.assertFalse(self.paths.journal_path("paper").exists())


class Kill(unittest.TestCase):
    def setUp(self):
        fake_git(self)

    def kill(self, *, config_p2):
        """The own-scope kill over an untrusted journal (the Keychain anchor ahead of it): our bar-manual bet and a
        p2-lp bet the journal never saw rest on the exchange. -> (env, exit code, JSON out, the two bet ids)."""
        env = gw.build_live_gateway(self)
        no_children(self)
        ours = env.exchange.add_order(strategy_ref="bar-manual")
        leg = env.exchange.add_order(selection_id=11, strategy_ref="p2-lp")
        anchor = env.journal.anchor()
        anchor_write(env.paths.profile, dict(anchor, seq=anchor["seq"] + 5, last_sha256="e" * 64,
                                             updated_at=env.clock.utc_now()), runner=env.runner)
        if config_p2:
            env.paths.config_p2_path.write_text("{}", encoding="utf-8")        # its presence only: never loaded here
        code, out, err = run_cli_main(env, ["kill", "--reason", "drill"])
        self.assertIn("cancel open bets on the Betfair website or in Bet Angel now", err)
        return env, code, json.loads(out), ours, leg

    def test_kill_refs_with_and_without_config_p2(self):
        for present in (False, True):
            with self.subTest(config_p2=present):
                env, code, out, ours, leg = self.kill(config_p2=present)
                self.assertEqual((code, out["status"]), (3, "kill_incomplete"))
                self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
                self.assertEqual(env.exchange.orders[leg]["status"], "EXECUTION_COMPLETE" if present else "EXECUTABLE")

    def test_kill_sweeps_when_config_p2_check_raises(self):
        stat = Path.stat

        def denied(path, *args, **kwargs):
            if path.name == "config-p2.json":
                raise PermissionError(13, "denied")
            return stat(path, *args, **kwargs)

        with patch.object(Path, "stat", denied):
            env, code, out, ours, leg = self.kill(config_p2=False)
            self.assertTrue(cli._p2_configured(env.paths))
        self.assertEqual((code, out["status"]), (3, "kill_incomplete"))
        self.assertEqual({env.exchange.orders[bet]["status"] for bet in (ours, leg)}, {"EXECUTION_COMPLETE"})
        self.assertFalse(cli._p2_configured(env.paths))                         # missing: absent
        env.paths.config_p2_path.write_text("{}", encoding="utf-8")
        self.assertTrue(cli._p2_configured(env.paths))


class Commands(unittest.TestCase):
    def setUp(self):
        fake_git(self)
        self.env = with_config_p2(gw.build_live_gateway(self))
        no_children(self)
        self.deps = {"keychain_runner": self.env.runner, "http_transport": self.env.transport,
                     "clock": self.env.clock, "confirmer": self.env.confirmer}
        self.calls = []

    def run_p2(self, *argv):
        return run_main(["p2-run", "--until", "2000", "--races", "1", *argv], self.deps)

    def test_p2_commands_reach_the_executor_with_config_p2_and_the_lp_factory(self):
        env = self.env
        lp = {"arm_seq": arm_record(env), "session_id": "p2s-0a1b2c3d", "races": 1}

        def run(p2_env):
            self.assertEqual(p2_env.config, env.p2)
            self.assertNotEqual(cfg.config_sha256(p2_env.config), cfg.config_sha256(env.config))
            credentials = {"live_orders": load_credentials(p2_env.config, purpose="live_orders",
                                                           profile=p2_env.paths.profile, runner=p2_env.keychain_runner)}
            with p2_env.gateway_factory(lp, credentials=credentials, stop=types.SimpleNamespace(requested=False)) \
                    as gateway:
                self.calls.append(("run", gateway.open_report()["armed"], gateway.config == env.p2, gateway._lp,
                                   gateway.lp_legs()))
            self.assertTrue(callable(p2_env.read_session_factory))
            return 0

        def summary(p2_env):
            self.calls.append(("summary", p2_env.args.cleared, p2_env.config == env.p2))
            return 0

        stand_in_executor(self, run=run, summary=summary)
        code, out, err = self.run_p2("--mode", "live", "--confirm-live", p2_token(env))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [("run", True, True, lp, {})])
        for argv, cleared in ((["p2-summary", "--mode", "live"], False), (["p2-summary", "--mode", "live", "--cleared"],
                                                                         True)):
            with self.subTest(argv=argv):
                self.assertEqual(run_main(argv, self.deps)[0], 0)
                self.assertEqual(self.calls[-1], ("summary", cleared, True))
        self.assertEqual([request["method"] for request in env.router.order_requests], [])

    def test_p2_commands_refuse_paper_another_profile_and_a_refused_arm(self):
        def refused(p2_env):
            raise pilot_run.Refused("p2_arm_refused_here", ["a line before the refusal"])

        stand_in_executor(self, run=refused, summary=refused)
        for argv in (["p2-run", "--until", "2000", "--races", "1"], ["p2-summary"]):
            with self.subTest(argv=argv):
                code, out, err = run_main(argv, self.deps)
                self.assertEqual((code, err.strip()), (2, "Betfair order refused: p2_live_only (the Pilot 2 commands "
                                                          "run with --mode live only)"))
        code, _, err = run_main(["--profile", "other", "p2-run", "--mode", "live", "--until", "2000", "--races", "1"],
                                self.deps)
        self.assertEqual((code, err.strip()), (2, "Betfair order refused: p2_profile_not_recorder_profile"))
        code, out, err = self.run_p2("--mode", "live", "--confirm-live", p2_token(self.env))
        self.assertEqual(code, 2)
        self.assertEqual(err.strip().splitlines()[-1], "Betfair order refused: p2_arm_refused_here")
        self.assertIn("a line before the refusal", out + err)


if __name__ == "__main__":
    unittest.main()
