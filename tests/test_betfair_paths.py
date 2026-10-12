"""State-root, worktree-detection, kill-switch and lock tests. Nothing is created under the real home."""

import contextlib
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import paths as paths_module
from betangel_research.betfair.errors import ConfigError, LockBusy
from betangel_research.betfair.paths import (APP_PARTS, FORBID_NETWORK_ENV, StatePaths, file_lock,
                                             is_inside_git_worktree, kill_engaged, network_forbidden,
                                             overrides_allowed, require_outside_worktrees)

import test_betfair_support as support


class PathsTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()

    def test_fixed_root_uses_pwd_home(self):
        expected_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        with patch.dict(os.environ, {"HOME": str(self.base / "elsewhere")}):
            fixed = StatePaths(env={})
        self.assertEqual(fixed.root, expected_home.joinpath(*APP_PARTS))
        self.assertEqual(fixed.root.parts[-4:], ("Library", "Application Support", "betangel-research", "betfair"))
        self.assertFalse(fixed.overridden)
        self.assertEqual(fixed.kill_file, fixed.root / "KILL")
        self.assertEqual(fixed.config_path, fixed.root / "profiles" / "default" / "config.json")
        self.assertEqual(fixed.journal_path("live"), fixed.root / "profiles/default/orders/journal-live.sqlite")
        described = fixed.describe()
        self.assertEqual(described["root"], str(fixed.root))
        self.assertIs(described["overridden"], False)
        with self.assertRaises(ConfigError):
            fixed.journal_path("demo")
        with self.assertRaisesRegex(ConfigError, "invalid_profile"):
            StatePaths(profile="../escape", env={})

    def test_override_refused_without_forbid_network(self):
        with self.assertRaisesRegex(ConfigError, "state_root_override_not_allowed"):
            StatePaths(root_override=self.base, env={})
        with self.assertRaisesRegex(ConfigError, "state_root_override_not_allowed"):
            StatePaths(home=self.base, env={"BETANGEL_BETFAIR_FORBID_NETWORK": "0"})
        # Regression: a passed env alone must not unlock overrides while the process network is on.
        with patch.dict(os.environ):
            os.environ.pop(FORBID_NETWORK_ENV, None)
            for kwargs in ({"root_override": self.base / "state"}, {"home": self.base / "home"}):
                with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                    with self.assertRaisesRegex(ConfigError, "state_root_override_not_allowed"):
                        StatePaths(env=dict(support.FORBID_ENV), **kwargs)
            self.assertFalse(overrides_allowed(dict(support.FORBID_ENV)))
            self.assertFalse(network_forbidden())
            self.assertTrue(network_forbidden(dict(support.FORBID_ENV)))

    def test_forbid_switch_process_env_always_wins(self):
        # Either source turns the network off; overrides need both, so they imply the network is off.
        self.assertTrue(network_forbidden())
        self.assertTrue(network_forbidden({}))
        self.assertTrue(network_forbidden({FORBID_NETWORK_ENV: "0"}))
        self.assertTrue(overrides_allowed())
        self.assertTrue(overrides_allowed(dict(support.FORBID_ENV)))
        self.assertFalse(overrides_allowed({}))
        with patch.dict(os.environ):
            os.environ.pop(FORBID_NETWORK_ENV, None)
            self.assertFalse(network_forbidden({}))
            self.assertFalse(overrides_allowed())
            self.assertFalse(overrides_allowed(dict(support.FORBID_ENV)))

    def test_override_root_inside_worktree_refused(self):
        repo = self.base / "repo"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(ConfigError, "state_root_inside_git_worktree"):
            StatePaths(root_override=repo / "state", env=dict(support.FORBID_ENV))
        with self.assertRaisesRegex(ConfigError, "state_root_inside_git_worktree"):
            StatePaths(home=repo, env=dict(support.FORBID_ENV))
        with self.assertRaisesRegex(ConfigError, "state_root_inside_git_worktree"):
            StatePaths(root_override=repo / "nested" / "betfair-state", env=dict(support.FORBID_ENV))
        if support.IN_GIT_CHECKOUT:              # the real checkout too, when the tree is one (not an export)
            with self.assertRaisesRegex(ConfigError, "state_root_inside_git_worktree"):
                StatePaths(root_override=support.REPO_ROOT / "betfair-state", env=dict(support.FORBID_ENV))

    def test_override_allowed_under_test_env(self):
        overridden = StatePaths(profile="alt-1", root_override=self.base / "state", home=self.base / "home",
                                env=dict(support.FORBID_ENV))
        self.assertTrue(overridden.overridden)
        self.assertEqual(overridden.root, self.base / "state")
        overridden.ensure()
        for directory in (overridden.root, overridden.profile_dir, overridden.orders_dir, overridden.recordings_dir,
                          overridden.auth_dir):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        home_only = StatePaths(home=self.base / "home", env=dict(support.FORBID_ENV))
        self.assertEqual(home_only.root, (self.base / "home").joinpath(*APP_PARTS))
        self.assertTrue(home_only.overridden)

    def test_is_inside_git_worktree_dir_and_file_git(self):
        repo = self.base / "repo"
        (repo / ".git").mkdir(parents=True)
        self.assertTrue(is_inside_git_worktree(repo / "deep" / "missing" / "file.key"))
        linked = self.base / "linked"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: /somewhere/.git/worktrees/linked\n")
        self.assertTrue(is_inside_git_worktree(linked / "client.crt"))
        plain = self.base / "plain"
        plain.mkdir()
        self.assertFalse(is_inside_git_worktree(plain / "client.crt"))
        (repo / "betangel_research").mkdir()
        self.assertTrue(is_inside_git_worktree(repo / "betangel_research"))
        if support.IN_GIT_CHECKOUT:
            self.assertTrue(is_inside_git_worktree(support.REPO_ROOT / "betangel_research"))

        def git_says_true(argv, **kwargs):
            self.assertEqual(argv[:2], ["git", "-C"])
            self.assertFalse(any(k.startswith("GIT_") for k in kwargs["env"]))
            return subprocess.CompletedProcess(argv, 0, "true\n", "")

        self.assertTrue(is_inside_git_worktree(plain / "x", runner=git_says_true))

        def no_git(argv, **kwargs):
            raise FileNotFoundError("git")

        def hung_git(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 5)

        self.assertFalse(is_inside_git_worktree(plain / "x", runner=no_git))
        self.assertTrue(is_inside_git_worktree(plain / "x", runner=hung_git))
        with self.assertRaisesRegex(ConfigError, "cert_inside_git_worktree"):
            require_outside_worktrees(repo / "c.crt", what="cert")
        self.assertEqual(require_outside_worktrees(plain / "c.crt", what="cert"), plain / "c.crt")

    def test_kill_engaged_fail_closed_on_permission_error(self):
        state = support.temp_state_paths(self)
        self.assertFalse(kill_engaged(state))
        with patch.object(paths_module.os, "lstat", side_effect=PermissionError("denied")):
            self.assertTrue(kill_engaged(state))
        with patch.object(paths_module.os, "lstat", side_effect=NotADirectoryError("odd")):
            self.assertTrue(kill_engaged(state))
        state.kill_file.write_text("{}")
        self.assertTrue(kill_engaged(state))

    def test_kill_engaged_dangling_symlink_engages(self):
        # Regression: os.stat follows the link and reports FileNotFoundError; any KILL entry engages.
        state = support.temp_state_paths(self)
        os.symlink(self.base / "missing-target", state.kill_file)
        self.assertTrue(os.path.lexists(state.kill_file))
        self.assertFalse(state.kill_file.exists())
        self.assertTrue(kill_engaged(state))
        state.kill_file.unlink()
        self.assertFalse(kill_engaged(state))

    def test_kill_global_across_profiles(self):
        root = self.base / "state"
        first = StatePaths(profile="default", root_override=root, env=dict(support.FORBID_ENV))
        second = StatePaths(profile="other", root_override=root, env=dict(support.FORBID_ENV))
        first.ensure()
        second.ensure()
        self.assertEqual(first.kill_file, second.kill_file)
        self.assertEqual(first.gateway_lock, second.gateway_lock)
        self.assertEqual(first.accounts_file, second.accounts_file)
        self.assertNotEqual(first.journal_path("live"), second.journal_path("live"))
        first.kill_file.write_text("{}")
        self.assertTrue(kill_engaged(second))

    def test_file_lock_timeout(self):
        clock = support.FakeClock()
        lock = self.base / "locks" / ".gateway.lock"
        with file_lock(lock, clock=clock):
            started = clock.monotonic_ns()
            with self.assertRaisesRegex(LockBusy, "lock_busy:.gateway.lock"):
                with file_lock(lock, timeout_s=1.0, clock=clock, poll_s=0.2):
                    self.fail("second holder must not enter")
            waited = (clock.monotonic_ns() - started) / 1e9
            self.assertGreaterEqual(waited, 1.0)
            self.assertLess(waited, 1.5)
            self.assertTrue(clock.sleeps)
            with self.assertRaises(LockBusy):
                with file_lock(lock, clock=clock):
                    pass
        with file_lock(lock, clock=clock):
            pass
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_file_lock_held_only_while_the_path_names_the_locked_file(self):
        """Regression (audit): flock locks an inode; once .gateway.lock was unlinked a second process
        created a new file and locked it too, and the first could not tell. held() can now tell."""
        lock = self.base / "locks" / ".gateway.lock"
        with file_lock(lock) as first:
            self.assertTrue(first.held())
            os.unlink(lock)
            self.assertFalse(first.held())
            with file_lock(lock) as second:                       # the new file is a different lock
                self.assertTrue(second.held())
                self.assertFalse(first.held())
            lock.write_text("")                                   # replaced by another file of the same name
            self.assertFalse(first.held())
        self.assertFalse(first.held())                            # released

    def test_file_lock_retakes_a_path_replaced_while_waiting(self):
        """A waiter whose flock succeeds on a file that no longer has the lock's name drops it and locks
        the file the path names now (the standard discipline for flock lock files)."""
        lock = self.base / "locks" / ".paper.lock"
        holder = contextlib.ExitStack()
        holder.enter_context(file_lock(lock))
        clock = support.FakeClock()
        original = clock.sleep

        def release_and_replace(seconds):
            holder.close()                                        # the holder releases ...
            os.unlink(lock)                                       # ... and its file is removed
            original(seconds)

        clock.sleep = release_and_replace
        with file_lock(lock, timeout_s=5.0, clock=clock) as handle:
            self.assertTrue(lock.exists())
            self.assertTrue(handle.held())


if __name__ == "__main__":
    unittest.main()
