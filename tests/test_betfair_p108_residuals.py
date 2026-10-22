"""The four P3 residuals carried from PR #35 (P1-08 merge comment). None failed open; each test pins the fix.

1. A signal inside OrderGateway._open_live, after its protected try and before open() returns, must not leak
   .gateway.lock: the CLI's gateway context closes a live gateway whatever line the signal lands on.
2. A fold whose ROLLBACK or COMMIT failed must not fall back to reading through a connection still inside a
   transaction; set_account_fingerprint has append's transaction shape.
3. With no orders.pilot, settle-market's pre-gateway guard over an unreadable journal behaves as at 2d3b092: an
   owner-lock state it cannot read is free, and probing never creates recordings/.live-owner.lock.
4. A pilot read's attempt_guard is asked again after the connect, before the first request byte, as send_refusal
   is for orders: a stop that lands during DNS, the TCP connect or the TLS handshake sends nothing.
"""
import contextlib
import io
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission, cli, gateway as gateway_module, pilot_run
from betangel_research.betfair.errors import JournalError
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.paths import file_lock

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_run as sr
from test_betfair_pilot_review_fixes import fire_on_line
from test_betfair_pilot_run import RunCase


class OpenLiveLockLeakTests(RunCase):
    """Residual 1: a signal on the first line after _open_live's try (pilot-place) left the gateway holding
    .gateway.lock, because the CLI kept the gateway only after open() returned. Run.end's flush then met LockBusy
    and no session_end stop was written."""

    def test_a_signal_after_the_open_try_releases_the_lock_and_ends_the_session(self):
        env = self.one_market()
        trace, fired = fire_on_line(gateway_module.OrderGateway._open_live, "report = combine(static, dynamic)",
                                    when=lambda frame: frame.f_locals.get("command") == "pilot-place")
        seen = {}
        original_end = pilot_run.Run.end

        def end(run):
            try:
                with file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock):
                    seen["lock_free_at_end"] = True
            except Exception as exc:
                seen["lock_free_at_end"] = type(exc).__name__
            return original_end(run)

        sys.settrace(trace)
        try:
            with patch.object(pilot_run.Run, "end", end):
                code, out, err = sr.arm_run(env)
        finally:
            sys.settrace(None)
        self.assertTrue(fired)
        self.assertEqual(seen.get("lock_free_at_end"), True, err)
        self.assertTrue(any(s.startswith("session-end-") for s in self.session_stops(env)), err)


class FailingConnection:
    """The journal's sqlite3 connection, except that the first statement in fail_on raises a disk error. With
    end_first, the transaction is really rolled back before the error, as a failed COMMIT can leave it."""

    def __init__(self, real, fail_on, *, end_first=False):
        self.real, self.fail_on, self.end_first, self.failed = real, fail_on, end_first, []

    @property
    def in_transaction(self):
        return self.real.in_transaction

    def execute(self, sql, *params):
        if sql in self.fail_on and not self.failed:
            self.failed.append(sql)
            if self.end_first:
                self.real.execute("ROLLBACK")
            raise sqlite3.OperationalError("disk I/O error")
        return self.real.execute(sql, *params)

    def __getattr__(self, name):
        return getattr(self.real, name)


class JournalTransactionTests(unittest.TestCase):
    """Residual 2."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.clock = support.FakeClock()
        self.journal = OrderJournal(Path(temp.name) / "journal-live.sqlite", mode="live", clock=self.clock)
        self.addCleanup(self.journal.close)
        self.journal.append("note", {})

    def test_a_fold_whose_rollback_failed_fails_closed(self):
        self.journal._db.execute("BEGIN")                  # left open by an interrupted call
        self.journal._db.execute("SELECT 1 FROM events").fetchall()
        self.journal._db = FailingConnection(self.journal._db, {"ROLLBACK"})
        with self.assertRaisesRegex(JournalError, "journal_transaction_stuck"):
            self.journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(self.journal._db.failed, ["ROLLBACK"])
        # The next fold, with a ROLLBACK that works, reads normally.
        self.assertFalse(self.journal.fold(now_ns=self.clock.time_ns())["halted"])

    def test_a_fold_whose_commit_failed_fails_closed(self):
        self.journal._db = FailingConnection(self.journal._db, {"COMMIT"})
        with self.assertRaisesRegex(JournalError, "journal_transaction_stuck"):
            self.journal.fold(now_ns=self.clock.time_ns())

    def test_set_account_fingerprint_rolls_back_a_transaction_left_open(self):
        self.journal._db.execute("BEGIN")
        self.journal.set_account_fingerprint("0123456789abcdef")
        self.assertEqual(self.journal.account_fingerprint(), "0123456789abcdef")

    def test_set_account_fingerprint_keeps_the_commit_error(self):
        self.journal._db = FailingConnection(self.journal._db, {"COMMIT"}, end_first=True)
        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O error"):
            self.journal.set_account_fingerprint("0123456789abcdef")
        self.assertFalse(self.journal._db.in_transaction)


class NonPilotSettleProbeTests(unittest.TestCase):
    """Residual 3 (refute wf_c5e7a760: both divergences were new in P1-08 and failed closed)."""

    def garbage_journal_env(self):
        env = gw.build_live_gateway(self)
        env.journal.close()
        self.assertNotIn("pilot", env.config["orders"])
        env.paths.journal_path("live").write_bytes(b"this is not a database" * 40)
        return env

    def settle(self, env):
        deps = {"keychain_runner": env.runner, "http_transport": env.transport, "clock": env.clock,
                "confirmer": env.confirmer}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["settle-market", "--market", "1.1", "--confirm-live", env.token], deps=deps)
        return code, err.getvalue()

    def test_the_settle_path_never_probes_the_owner_lock(self):
        """Recording rollout (design v9 §3.4.4): settle-market takes ownership first, so its pilot check never probes
        the owner lock (the probe this residual pinned is gone from the settle path); a non-pilot unreadable journal
        still meets the gateway's own journal error, exit 4, as before P1-08."""
        env = self.garbage_journal_env()
        with patch.object(admission, "owner_lock_state", side_effect=AssertionError("probe in the settle path")):
            code, err = self.settle(env)
        self.assertEqual(code, 4, err)
        self.assertIn("Betfair stopped: DatabaseError", err)
        self.assertNotIn("pilot_session_armed", err)

    def test_an_unreadable_recordings_dir_reads_as_free(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        env = self.garbage_journal_env()
        env.paths.recordings_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        env.paths.recordings_dir.chmod(0)
        self.addCleanup(env.paths.recordings_dir.chmod, stat.S_IRWXU)
        code, err = self.settle(env)
        self.assertEqual(code, 4, err)
        self.assertIn("Betfair stopped: DatabaseError", err)
        self.assertNotIn("pilot_session_armed", err)

    def test_owner_lock_state_never_creates_the_file(self):
        env = gw.build_live_gateway(self)
        env.journal.close()
        self.assertEqual(admission.owner_lock_state(env.paths, clock=env.clock), "free")
        self.assertFalse(env.paths.live_owner_lock.exists())
        with file_lock(env.paths.live_owner_lock, timeout_s=0, clock=env.clock):
            self.assertEqual(admission.owner_lock_state(env.paths, clock=env.clock), "held")
        self.assertEqual(admission.owner_lock_state(env.paths, clock=env.clock), "free")


class GuardAfterConnectTests(unittest.TestCase):
    """Residual 4: the real HttpTransport over a scripted socket; the stop becomes true while the connection is made."""

    def test_a_stop_during_the_connect_sends_no_byte(self):
        from betangel_research.betfair import api, transport
        from betangel_research.betfair.errors import ReadStopped
        from betangel_research.betfair.secret import Secret
        support.scripted_network_only(self)
        state = {"stopped": False, "guard_calls": 0}
        sock = support.ScriptedSocket([support.http_bytes(200, b'{"jsonrpc":"2.0","result":[],"id":1}')])
        script = support.ConnectionScript(sock)

        def connecting(host, **kw):
            state["stopped"] = True                      # the stop lands during DNS, connect or the handshake
            return script(host, **kw)

        inner = transport.HttpTransport(allowed_hosts={"api.betfair.com"}, clock=support.FakeClock(),
                                        connection_factory=connecting, env={})
        self.addCleanup(inner.close)

        def guard():
            state["guard_calls"] += 1
            return "kill" if state["stopped"] else None

        with self.assertRaises(ReadStopped) as caught:
            api.GuardedTransport(inner, guard).request(
                "POST", "https://api.betfair.com/exchange/betting/json-rpc/v1",
                {"X-Application": Secret("synthetic-delayed-key-0001"),
                 "X-Authentication": Secret(support.PLANTED_SECRET), "Content-Type": "application/json"},
                b'{"jsonrpc":"2.0","method":"SportsAPING/v1.0/listMarketCatalogue","params":{},"id":1}',
                deadline_s=10, idempotent=True)
        self.assertEqual(caught.exception.reason, "kill")
        self.assertEqual(len(sock.sent), 0)                    # connected, but not one request byte written
        self.assertEqual(inner.last_bytes_written, 0)
        self.assertEqual(state["guard_calls"], 3)              # the outer check, before the attempt, after the connect


if __name__ == "__main__":
    unittest.main()
