"""Keychain and certificate-file tests. The real /usr/bin/security is never run: FakeKeychainRunner only."""

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import config as cfg
from betangel_research.betfair import credentials as creds
from betangel_research.betfair import secret as secret_module
from betangel_research.betfair.errors import CredentialError
from betangel_research.betfair.secret import Secret

import test_betfair_support as support


class CredentialTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.runner = support.FakeKeychainRunner()
        self.paths = support.temp_state_paths(self)
        self.config = cfg.load_config(support.write_test_config(self.paths), paths=self.paths)
        guard = patch.object(creds.subprocess, "run", side_effect=AssertionError("real security must not run"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_keychain_argv_absolute_path_no_secret(self):
        value = creds.keychain_read("password", "default", runner=self.runner)
        self.assertIsInstance(value, Secret)
        self.assertEqual(value.reveal(), "synthetic-password-0001")
        call = self.runner.calls[-1]
        self.assertEqual(call["argv"], ["/usr/bin/security", "find-generic-password", "-s",
                                        "betangel-research.betfair.password", "-a", "default", "-w"])
        self.assertNotIn("synthetic-password-0001", " ".join(call["argv"]))
        self.assertTrue(call["kwargs"]["capture_output"])
        self.assertEqual(call["kwargs"]["timeout"], 120)
        self.assertTrue(creds.keychain_present("app_key_live", "default", runner=self.runner))
        self.assertNotIn("-w", self.runner.calls[-1]["argv"])
        self.assertFalse(creds.keychain_present("cert_key_passphrase", "default", runner=self.runner))
        with self.assertRaises(CredentialError):
            creds.keychain_read("token", "default", runner=self.runner)

    def test_minimal_env_passed(self):
        creds.keychain_read("username", "default", runner=self.runner)
        env = self.runner.calls[-1]["env"]
        self.assertEqual(env, creds.minimal_env())
        self.assertEqual(set(env), {"PATH", "HOME", "USER", "LOGNAME"})
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertNotIn("BETANGEL_BETFAIR_FORBID_NETWORK", env)

    def test_missing_item_error_no_stdout_stderr_from_none(self):
        empty = support.FakeKeychainRunner(items={})
        with self.assertRaises(CredentialError) as caught:
            creds.keychain_read("password", "default", runner=empty)
        exc = caught.exception
        self.assertEqual(str(exc), "keychain_item_unavailable:password")
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)
        self.assertNotIn(support.PLANTED_SECRET, repr(exc.args))
        with self.assertRaises(CredentialError) as timed_out:
            creds.keychain_read("password", "default", runner=support.FakeKeychainRunner(timeout=True))
        self.assertEqual(str(timed_out.exception), "keychain_item_unavailable:password")
        self.assertIsNone(timed_out.exception.__context__)

    def test_non_darwin_refused(self):
        with patch.object(creds.sys, "platform", "linux"):
            with self.assertRaisesRegex(CredentialError, "keychain_requires_macos"):
                creds.load_credentials(self.config, purpose="read", profile="default", runner=self.runner)
        self.assertEqual(self.runner.calls, [])

    def test_key_file_group_readable_rejected(self):
        folder = self.paths.home / ".config" / "betangel-research" / "betfair"
        folder.mkdir(parents=True)
        cert, key = folder / "client-2048.crt", folder / "client-2048.key"
        cert.write_text("synthetic certificate\n")
        key.write_text("synthetic key\n")
        os.chmod(key, 0o600)
        result = creds.check_cert_files(cert, key)
        self.assertEqual(result["status"], "cert_files_ok")
        self.assertEqual(result["key_mode"], "0o600")
        for mode in (0o640, 0o604, 0o660):
            with self.subTest(mode=oct(mode)):
                os.chmod(key, mode)
                with self.assertRaisesRegex(CredentialError, "key_group_or_world_accessible") as caught:
                    creds.check_cert_files(cert, key)
                self.assertIn("key_group_or_world_accessible", caught.exception.failures)
        os.chmod(key, 0o600)
        with self.assertRaisesRegex(CredentialError, "cert_file_missing"):
            creds.check_cert_files(folder / "absent.crt", key)
        with self.assertRaisesRegex(CredentialError, "cert_not_regular_file"):
            creds.check_cert_files(folder, key)
        repo = self.paths.home / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "pkg").mkdir()
        inside = repo / "pkg" / "__init__.py"
        inside.write_text("")
        with self.assertRaisesRegex(CredentialError, "cert_inside_git_worktree"):
            creds.check_cert_files(inside, key)
        if support.IN_GIT_CHECKOUT:              # the real checkout too, when the tree is one (not an export)
            with self.assertRaisesRegex(CredentialError, "cert_inside_git_worktree"):
                creds.check_cert_files(support.REPO_ROOT / "betangel_research" / "__init__.py", key)

    def test_setup_commands_w_last_live_key_T_empty(self):
        lines = creds.keychain_setup_commands("default")
        self.assertEqual(lines, [
            "security add-generic-password -U -s betangel-research.betfair.username -a default -w",
            "security add-generic-password -U -s betangel-research.betfair.password -a default -w",
            "security add-generic-password -U -s betangel-research.betfair.app-key.delayed -a default -w",
            'security add-generic-password -U -s betangel-research.betfair.app-key.live -a default -T "" -w',
        ])
        for line in creds.keychain_setup_commands("default", include_passphrase=True):
            self.assertTrue(line.endswith(" -w"))
            self.assertEqual(line.count('-T ""'), 1 if "app-key.live" in line else 0)
        with self.assertRaises(Exception):
            creds.keychain_setup_commands("default; rm -rf /")

    def test_live_purpose_requires_live_slot(self):
        loaded = []

        def loader(kind, profile):
            loaded.append((kind, profile))
            return Secret(f"synthetic-{kind}-value")

        read = creds.load_credentials(self.config, purpose="read", profile="default", loader=loader)
        self.assertEqual(read["app_key_slot"], "delayed")
        self.assertEqual(read["app_key"].reveal(), "synthetic-app_key_delayed-value")
        live = creds.load_credentials(self.config, purpose="live_orders", profile="default", loader=loader)
        self.assertEqual(live["app_key_slot"], "live")
        self.assertEqual(live["app_key"].reveal(), "synthetic-app_key_live-value")
        self.assertIn(("app_key_live", "default"), loaded)
        self.assertEqual(live["cert_path"], Path(self.config["credentials"]["cert_path"]))
        self.assertIsNone(live["key_passphrase"])
        self.assertEqual(live["source"], "keychain")
        via_runner = creds.load_credentials(self.config, purpose="stream", profile="default", runner=self.runner)
        self.assertEqual(via_runner["app_key"].reveal(), "synthetic-delayed-key-0001")
        with self.assertRaisesRegex(CredentialError, "unknown_credential_purpose"):
            creds.load_credentials(self.config, purpose="orders", profile="default", loader=loader)
        with self.assertRaisesRegex(CredentialError, "keychain_item_unavailable:app_key_live"):
            creds.load_credentials(self.config, purpose="live_orders", profile="default",
                                   runner=support.FakeKeychainRunner(
                                       fail_services={creds.SERVICES["app_key_live"]}))

    def test_username_is_not_a_substring_scanned_secret(self):
        """Regression (audit): the username was registered for assert_clean's substring scan, so a profile
        name or home path containing it made accounts.json, the journal and the recorder ledger raise
        SecretLeak (and blocked the kill path). It stays a Secret, but is not scanned for."""
        items = support.default_keychain_items()
        items[(creds.SERVICES["username"], "default")] = "audit-username-0001"
        items[(creds.SERVICES["password"], "default")] = "audit-password-0001"
        loaded = creds.load_credentials(self.config, purpose="read", profile="default",
                                        runner=support.FakeKeychainRunner(items=items))
        self.assertIsInstance(loaded["username"], Secret)
        self.assertEqual(repr(loaded["username"]), "<redacted>")
        self.assertFalse(secret_module.contains_registered("audit-username-0001"))
        self.assertTrue(secret_module.contains_registered("audit-password-0001"))
        via_loader = creds.load_credentials(self.config, purpose="read", profile="default",
                                            loader=lambda kind, profile: f"audit-{kind}-value-0002")
        self.assertFalse(secret_module.contains_registered(via_loader["username"].reveal()))
        self.assertTrue(secret_module.contains_registered(via_loader["password"].reveal()))

    def test_short_key_passphrase_never_refuses_journal_writes(self):
        """Regression (audit): a 4-character key passphrase was registered for the substring scan, so
        every journal event carrying a betId or hash that contained it raised SecretLeak, including the
        report after a real send and the kill sweep's cancel_intent. The passphrase only decrypts the
        key file in this process, so it is not scanned; short values are never scanned at all."""
        from betangel_research.betfair.journal import OrderJournal
        items = support.default_keychain_items()
        items[(creds.SERVICES["cert_key_passphrase"], "default")] = "1234"
        passphrase = creds.keychain_read("cert_key_passphrase", "default",
                                         runner=support.FakeKeychainRunner(items=items))
        self.assertIsInstance(passphrase, Secret)
        self.assertEqual(repr(passphrase), "<redacted>")
        items[(creds.SERVICES["cert_key_passphrase"], "default")] = "audit-passphrase-0003"
        long_passphrase = creds.keychain_read("cert_key_passphrase", "default",
                                              runner=support.FakeKeychainRunner(items=items))
        self.assertFalse(secret_module.contains_registered(long_passphrase.reveal()))
        Secret("5678")                                       # any short registered value is not scanned either
        self.assertFalse(secret_module.contains_registered("31234567890 5678"))
        journal = OrderJournal(self.paths.root / "journal-test.sqlite", mode="live", clock=support.FakeClock())
        self.addCleanup(journal.close)
        journal.append("note", {"reason": "after", "bet_id": "31234567890"})
        journal.append("halt", {"reason": "kill", "scope": "global", "body_sha256": "ab1234cd" * 8})
        self.assertEqual(journal.anchor()["seq"], 2)

    def test_anchor_write_only_anchor_service(self):
        anchor = {"genesis_sha256": "a" * 64, "seq": 7, "last_sha256": "b" * 64,
                  "updated_at": "2020-01-01T12:00:00.123457Z"}
        self.assertIsNone(creds.anchor_read("default", runner=self.runner))
        creds.anchor_write("default", anchor, runner=self.runner)
        writes = [c for c in self.runner.calls if c["argv"][1] == "add-generic-password"]
        self.assertEqual(len(writes), 1)
        argv = writes[0]["argv"]
        self.assertEqual(argv[:7], ["/usr/bin/security", "add-generic-password", "-U", "-s", creds.ANCHOR_SERVICE,
                                    "-a", "default"])
        self.assertEqual(argv[7], "-w")
        for service in creds.SERVICES.values():
            self.assertNotIn(service, argv)
        self.assertEqual(creds.anchor_read("default", runner=self.runner), anchor)
        with self.assertRaisesRegex(CredentialError, "journal_anchor_invalid"):
            creds.anchor_write("default", dict(anchor, token="x"), runner=self.runner)
        self.runner.items[(creds.ANCHOR_SERVICE, "default")] = "not json"
        with self.assertRaisesRegex(CredentialError, "journal_anchor_invalid"):
            creds.anchor_read("default", runner=self.runner)
        locked = support.FakeKeychainRunner(items={(creds.ANCHOR_SERVICE, "default"): "{}"},
                                            fail_services=set())

        def read_fails_but_item_exists(argv, **kwargs):
            if argv[-1] == "-w":
                return support.FakeKeychainRunner(items={})(argv, **kwargs)
            return locked(argv, **kwargs)

        with self.assertRaisesRegex(CredentialError, "journal_anchor_unreadable"):
            creds.anchor_read("default", runner=read_fails_but_item_exists)
        with self.assertRaisesRegex(CredentialError, "journal_anchor_write_failed"):
            creds.anchor_write("default", anchor, runner=support.FakeKeychainRunner(add_fails=True))

    def test_publish_anchor_reads_the_head_only_under_its_lock(self):
        """Re-review R1: publish_anchor, the one anchor writer, reads the journal head only once it holds
        paths.anchor_lock. While another publisher holds the lock it waits wait_s on the injected clock, reads
        nothing, writes nothing and raises journal_anchor_publish_busy; a lock path replaced while held is
        refused (journal_anchor_lock_unavailable); an empty journal publishes nothing."""
        from betangel_research.betfair.journal import OrderJournal
        from betangel_research.betfair.paths import file_lock
        clock = support.FakeClock()
        journal = OrderJournal(self.paths.journal_path("live"), mode="live", clock=clock)
        self.addCleanup(journal.close)
        self.assertIsNone(creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock))
        journal.append("note", {"reason": "seed"})
        reads = []
        original = journal.anchor

        def counted():
            reads.append(1)
            return original()

        with patch.object(journal, "anchor", side_effect=counted):
            with file_lock(self.paths.anchor_lock):                     # another publisher, mid-publication
                started = clock.mono_ns
                with self.assertRaisesRegex(CredentialError, "journal_anchor_publish_busy"):
                    creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock, wait_s=2)
                self.assertEqual(clock.mono_ns - started, 2 * 10**9)
            self.assertEqual(reads, [])
            self.assertFalse(any(c["argv"][1] == "add-generic-password" for c in self.runner.calls))
            published = creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock)
        self.assertEqual(reads, [1])
        self.assertEqual(creds.anchor_read("default", runner=self.runner), published)
        self.assertEqual(dict(original(), updated_at=clock.utc_now()), published)

        def replaced_while_held():
            os.unlink(self.paths.anchor_lock)                            # a "stale lock" clean-up
            return original()

        journal.append("note", {"reason": "later"})
        with patch.object(journal, "anchor", side_effect=replaced_while_held):
            with self.assertRaisesRegex(CredentialError, "journal_anchor_lock_unavailable"):
                creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock)
        self.assertEqual(creds.anchor_read("default", runner=self.runner), published)    # nothing written

    def test_publish_anchor_writes_only_a_head_that_extends_the_keychain_anchor(self):
        """Re-review of R1 (rollback under a live session): publish_anchor wrote whatever head the journal showed
        inside its lock. A journal restored in place to an older copy (sqlite3 ".restore", which every open
        connection then reads) made the session publish the older head, and the rolled-back journal passed A13.
        It now reads the Keychain anchor inside the lock too and writes only a head that extends it (A13 rule):
        an older head, a head of another chain or a fork at the anchor's seq is refused
        (journal_anchor_not_extended) and the anchor is left as it was; an unreadable anchor writes nothing; no
        anchor at all still takes the first one."""
        import sqlite3
        from betangel_research.betfair import arming
        from betangel_research.betfair.journal import OrderJournal
        clock = support.FakeClock()
        live = self.paths.journal_path("live")
        journal = OrderJournal(live, mode="live", clock=clock)
        self.addCleanup(journal.close)
        key = (creds.ANCHOR_SERVICE, "default")

        def copy(source, target):
            src, dst = sqlite3.connect(str(source)), sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()

        def writes():
            return sum(1 for c in self.runner.calls if c["argv"][1] == "add-generic-password")

        journal.append("note", {"reason": "seed"})
        self.assertEqual(creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock)["seq"], 1)
        backup = self.paths.root / "journal-backup.sqlite"
        copy(live, backup)                                               # the journal at seq 1
        journal.append("halt", {"reason": "safety_history", "scope": "global"})
        anchored = creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock)
        self.assertEqual(anchored["seq"], 2)
        stored = self.runner.items[key]
        copy(backup, live)                                               # restored in place under the session
        self.assertEqual(journal.anchor()["seq"], 1)                     # the session's connection reads it
        before = writes()
        with self.assertRaisesRegex(CredentialError, "journal_anchor_not_extended"):
            creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock)
        journal.append("note", {"reason": "fork"})                      # back to seq 2, another last hash
        self.assertEqual(journal.anchor()["seq"], 2)
        with self.assertRaisesRegex(CredentialError, "journal_anchor_not_extended"):
            creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock)
        journal.append("note", {"reason": "past_the_anchor"})           # seq 3, but seq 2 is not the anchored one
        with self.assertRaisesRegex(CredentialError, "journal_anchor_not_extended"):
            creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock)
        other = OrderJournal(self.paths.root / "journal-other.sqlite", mode="live", clock=clock)
        self.addCleanup(other.close)
        for reason in ("a", "b", "c", "d"):
            other.append("note", {"reason": reason})                     # another chain, higher seq
        with self.assertRaisesRegex(CredentialError, "journal_anchor_not_extended"):
            creds.publish_anchor(other, self.paths, runner=self.runner, clock=clock)
        self.assertEqual((writes(), self.runner.items[key]), (before, stored))          # nothing written
        state = journal.fold(now_ns=clock.time_ns())
        self.assertFalse(state["halted"])                                # the rolled-back journal lost the HALT
        self.assertEqual(arming.anchor_failures(journal.anchor(), creds.anchor_read("default", runner=self.runner),
                                                journal=journal, journal_state=state), ["journal_anchor_mismatch"])
        locked = support.FakeKeychainRunner(items=dict(self.runner.items), find_exit=36)
        with self.assertRaisesRegex(CredentialError, "journal_anchor_unreadable"):
            creds.publish_anchor(other, self.paths, runner=locked, clock=clock)
        self.assertFalse(any(c["argv"][1] == "add-generic-password" for c in locked.calls))
        fresh = support.FakeKeychainRunner(items={})                    # no anchor yet: the first one is written
        self.assertEqual(creds.publish_anchor(other, self.paths, runner=fresh, clock=clock)["seq"], 4)

    def test_publish_anchor_publishes_again_when_its_lock_is_replaced_during_the_write(self):
        """Re-review of R1 (lock replaced during the Keychain write): held() was checked only before anchor_write.
        The lock path is unlinked while A's security add runs; B (another process: its own journal connection)
        locks the new file, journals a HALT and publishes the newer head; then A's write lands with the older
        head. A returned success with the Keychain behind B's head, so a snapshot without B's HALT passed A13. A
        now finds its lock gone after the write and publishes again under a freshly taken lock: the anchor ends
        at the newest head and the snapshot fails A13. A lock lost on every attempt is
        journal_anchor_lock_unavailable."""
        import json
        import sqlite3
        import threading
        from betangel_research.betfair import arming
        from betangel_research.betfair.journal import OrderJournal
        clock = support.FakeClock()
        live = self.paths.journal_path("live")
        journal = OrderJournal(live, mode="live", clock=clock)
        self.addCleanup(journal.close)
        key = (creds.ANCHOR_SERVICE, "default")
        journal.append("note", {"reason": "seed"})
        creds.publish_anchor(journal, self.paths, runner=self.runner, clock=clock)
        journal.append("note", {"reason": "a_head"})
        snapshot = self.paths.root / "snapshot-without-b.sqlite"
        db = sqlite3.connect(str(snapshot))
        try:
            journal._db.backup(db)                                       # the journal as A reads it
        finally:
            db.close()
        written, started, b = [], [], {}

        def writer_b():
            other = OrderJournal(live, mode="live", clock=clock)
            try:
                other.append("halt", {"reason": "newer_safety_history", "scope": "global"})
                b["published"] = creds.publish_anchor(other, self.paths, runner=runner, clock=support.FakeClock(),
                                                      wait_s=2)
            except BaseException as exc:                                 # reported to the assertions below
                b["error"] = repr(exc)
            finally:
                other.close()

        def runner(argv, **kwargs):
            if argv[1] == "add-generic-password" and not started:        # A's write: B runs in the middle of it
                started.append(True)
                os.unlink(self.paths.anchor_lock)                         # a "stale lock" clean-up
                thread = threading.Thread(target=writer_b)
                thread.start()
                thread.join(30)
            answer = self.runner(argv, **kwargs)
            if argv[1] == "add-generic-password":
                written.append(json.loads(self.runner.items[key])["seq"])
            return answer

        published = creds.publish_anchor(journal, self.paths, runner=runner, clock=clock)
        self.assertNotIn("error", b)
        head = journal.anchor()
        self.assertEqual((b["published"]["seq"], head["seq"]), (3, 3))
        self.assertEqual(written, [3, 2, 3])                             # B, A's late older head, A again
        self.assertEqual((published["seq"], published["last_sha256"]), (head["seq"], head["last_sha256"]))
        keychain = creds.anchor_read("default", runner=self.runner)
        self.assertEqual((keychain["seq"], keychain["last_sha256"]), (head["seq"], head["last_sha256"]))
        with OrderJournal(snapshot, mode="live", clock=clock) as old:
            old_state = old.fold(now_ns=clock.time_ns())
            self.assertFalse(old_state["halted"])
            self.assertEqual(arming.anchor_failures(old.anchor(), keychain, journal=old, journal_state=old_state),
                             ["journal_anchor_mismatch"])

        def always_replaced(argv, **kwargs):
            if argv[1] == "add-generic-password":
                os.unlink(self.paths.anchor_lock)
            return self.runner(argv, **kwargs)

        journal.append("note", {"reason": "later"})
        before = len([c for c in self.runner.calls if c["argv"][1] == "add-generic-password"])
        with self.assertRaisesRegex(CredentialError, "journal_anchor_lock_unavailable"):
            creds.publish_anchor(journal, self.paths, runner=always_replaced, clock=clock)
        after = len([c for c in self.runner.calls if c["argv"][1] == "add-generic-password"])
        self.assertEqual(after - before, creds.ANCHOR_PUBLISH_ATTEMPTS)

    def test_anchor_read_fails_closed_unless_definitely_absent(self):
        # Regression: only "both lookups ran and exited 44" may mean no anchor. A timeout, a security
        # that cannot start, a locked or denied Keychain, or any other exit must not pass A13.
        absent = support.FakeKeychainRunner(items={})
        self.assertIsNone(creds.anchor_read("default", runner=absent))
        self.assertEqual([c["argv"][-1] for c in absent.calls], ["-w", "default"])
        failing = {
            "timeout": support.FakeKeychainRunner(items={}, timeout=True),
            "cannot_start": support.FakeKeychainRunner(items={}, raise_error=PermissionError("denied")),
            "missing_binary": support.FakeKeychainRunner(items={}, raise_error=FileNotFoundError("security")),
            "locked_36": support.FakeKeychainRunner(items={}, find_exit=36),
            "denied_51": support.FakeKeychainRunner(items={}, find_exit=51),
            "cancelled_128": support.FakeKeychainRunner(items={}, find_exit=128),
            "generic_1": support.FakeKeychainRunner(items={}, find_exit=1),
        }
        for name, runner in failing.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(CredentialError, "journal_anchor_unreadable") as caught:
                    creds.anchor_read("default", runner=runner)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn(support.PLANTED_SECRET, repr(caught.exception.args))

        def not_found_then_presence_fails(exit_code=None, error=None):
            def run(argv, **kwargs):
                if argv[-1] == "-w":
                    return support.FakeKeychainRunner(items={})(argv, **kwargs)
                if error is not None:
                    raise error
                return support.FakeKeychainRunner(items={}, find_exit=exit_code)(argv, **kwargs)
            return run

        for name, runner in (("presence_timeout", not_found_then_presence_fails(
                                 error=subprocess.TimeoutExpired(["security"], 30))),
                             ("presence_oserror", not_found_then_presence_fails(error=OSError("exec"))),
                             ("presence_locked", not_found_then_presence_fails(exit_code=36))):
            with self.subTest(case=name):
                with self.assertRaisesRegex(CredentialError, "journal_anchor_unreadable"):
                    creds.anchor_read("default", runner=runner)

    def test_default_runner_resolved_at_call_time(self):
        # Regression: runner defaults are looked up per call, so a patched subprocess.run guards
        # every Keychain function a caller invokes without runner=.
        anchor = {"genesis_sha256": "a" * 64, "seq": 1, "last_sha256": "b" * 64,
                  "updated_at": "2020-01-01T12:00:00.123457Z"}
        calls = [
            lambda: creds.keychain_read("password", "default"),
            lambda: creds.keychain_present("password", "default"),
            lambda: creds.anchor_read("default"),
            lambda: creds.anchor_write("default", anchor),
        ]
        for function in (creds.keychain_read, creds.keychain_present, creds.anchor_read, creds.anchor_write):
            self.assertIsNone(function.__kwdefaults__["runner"])
        for index, call in enumerate(calls):
            with self.subTest(case=index):
                with self.assertRaisesRegex(AssertionError, "real security must not run"):
                    call()
        # block_network's own guard: subprocess.run refuses /usr/bin/security with a BaseException.
        with patch.object(creds.subprocess, "run", support._guarded_run):
            for index, call in enumerate(calls):
                with self.subTest(guard=index):
                    with self.assertRaises(support.OfflineViolation):
                        call()
        self.assertTrue(support._is_security_command(["/usr/bin/security", "find-generic-password"]))
        self.assertTrue(support._is_security_command("security find-generic-password -s x"))
        self.assertFalse(support._is_security_command(["git", "status"]))


if __name__ == "__main__":
    unittest.main()
