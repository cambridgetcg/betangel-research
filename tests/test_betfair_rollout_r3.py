"""Recording rollout R3 (design v9 §3.7, §8 R3; Astra Q8): the recorder login stop, offline.

First the reproduction Astra's fake-transport probe found, through StreamRecorder.run with a real SessionManager over a
scripted FakeHttpTransport and with R3 switched off by a patch: a keepAlive NO_SESSION relogin that fails, then the
precount's lazy login that fails, leave two consecutive failures in the login ledger, and kill's emergency login is
then refused (login_failures_need_operator). Then the fix: the same script stops the run after the first failed or
unknown login (login_failed_this_run:<outcome>), so the ledger keeps one failure and the emergency login still goes;
a login ledger that is not clean stops the run before any login (login_ledger_not_clean); the guard is in place
before the precount's first lazy login; and the per-run flag is sticky across an external login-reset. Synthetic
credentials, a fake clock, a scripted stream and scripted HTTP only.
"""

import json
import random
import unittest
from unittest.mock import patch

from betangel_research.betfair import auth, recorder as recorder_module
from betangel_research.betfair.auth import SessionManager, append_login_reset, ledger_review, ledger_status
from betangel_research.betfair.config import load_config
from betangel_research.betfair.errors import AuthError, LoginStopped
from betangel_research.betfair.recorder import StreamRecorder
from betangel_research.betfair.secret import Secret

import test_betfair_support as support
import test_betfair_support_stream as fx

LOGIN = "https://identitysso.betfair.com/api/login"
KEEPALIVE = "https://identitysso.betfair.com/api/keepAlive"
INTERACTIVE = {"credentials": {"login_method": "interactive", "cert_path": None, "key_path": None}}


def login_ok():
    return support.http_response({"token": "synthetic-session-token-r3", "product": "x", "status": "SUCCESS",
                                  "error": ""})


def login_refused():
    return support.http_response({"token": "", "product": "x", "status": "FAIL",
                                  "error": "INVALID_USERNAME_OR_PASSWORD"})


def no_session():
    return support.http_response({"token": "", "product": "x", "status": "FAIL", "error": "NO_SESSION"})


class R3Case(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        from betangel_research.betfair import admission
        identity = patch.object(admission.ProcessTable, "start_time", lambda table, pid: "synthetic-start")
        identity.start()                    # a synthetic process identity: no /bin/ps (native drain covers ps)
        self.addCleanup(identity.stop)
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock()
        self.config = load_config(support.write_test_config(self.paths, INTERACTIVE), paths=self.paths)

    def credentials(self):
        return {"username": Secret("synthetic-user-r3"), "password": Secret("synthetic-password-r3"),
                "app_key": Secret("synthetic-delayed-key-r3"), "app_key_slot": "delayed", "cert_path": None,
                "key_path": None, "key_passphrase": None, "source": "keychain"}

    def session(self, responses, cls=SessionManager):
        transport = support.FakeHttpTransport(responses)
        return cls(self.config, self.credentials(), paths=self.paths, transport=transport, clock=self.clock), transport

    def outcomes(self):
        if not self.paths.login_ledger.exists():
            return []
        rows = [json.loads(line) for line in self.paths.login_ledger.read_text().splitlines() if line.strip()]
        return [row["outcome"] for row in rows if row["kind"] == "login_outcome"]

    def kill_login(self):
        """kill's emergency login on its own session (a fresh process's SessionManager)."""
        session, _ = self.session([login_ok()])
        return session.login(emergency=True)

    def record(self, session, connections, lister=None):
        """One StreamRecorder.run over the scripted stream; the lister logs in lazily as ReadClient's REST does."""
        server = fx.FakeStreamServer(connections, self.clock)
        self.server = server

        def market_lister(rest_filter):
            session.token()
            return [support.MARKET_ID]
        recorder = StreamRecorder(fx.recorder_config(self.paths, INTERACTIVE), paths=self.paths, session=session,
                                  stream_factory=lambda: fx.make_client(server, session, self.clock),
                                  market_lister=lister or market_lister, clock=self.clock, rng=random.Random(8),
                                  emit=lambda event: None)
        receipt = recorder.run(stop_requested=lambda: False, max_seconds=4 * 3600)
        self.dir = self.paths.recordings_dir / receipt["session_id"]
        return receipt

    def events(self, name):
        rows = [json.loads(line) for line in (self.dir / "events.jsonl").read_text().splitlines()]
        return [row for row in rows if row["event"] == name]

    def keepalive_due(self, session):
        """A stream script item: make the session's keepAlive due (as an hour of streaming would), then a heartbeat."""
        def build(sock):
            session._last_alive_ns = self.clock.monotonic_ns() - 3601 * 1_000_000_000
            ids = [m.get("id") for m in sock.sent_messages() if m.get("op") == "marketSubscription"]
            return fx.line(fx.heartbeat(ids[-1], fx.pt_at(-200)))
        return build

    def probe_script(self, session):
        """Astra's probe shape: login, stream, a keepAlive NO_SESSION whose relogin fails (unknown), then the stream
        drops, so the next connection's precount logs in again."""
        stream = fx.handshake() + [fx.change(lambda sid: fx.image(sid, fx.pt_at(-300))), self.keepalive_due(session),
                                   fx.change(lambda sid: fx.heartbeat(sid, fx.pt_at(-199))), fx.EOF]
        return [stream, fx.handshake()]


class ReproductionTests(R3Case):
    def test_without_r3_two_failures_and_kill_emergency_login_refused(self):
        """Reproduce first, with R3 switched off: keepAlive relogin failure, precount login failure, then the kill's
        emergency login is refused."""
        session, transport = self.session([login_ok(), no_session(), support.transport_failure("receive"),
                                           support.transport_failure("receive")])
        with patch.object(recorder_module._Session, "_login_outcome", lambda self, outcome: None), \
                patch.object(recorder_module._Session, "_login_guard", lambda self: self._stop_reason()), \
                patch.object(recorder_module._Session, "_ledger_stop", lambda self: None):
            receipt = self.record(session, self.probe_script(session))
        self.assertEqual([r["url"] for r in transport.requests], [LOGIN, KEEPALIVE, LOGIN, LOGIN])
        self.assertEqual(self.outcomes(), ["proceed", "unknown", "unknown"])
        self.assertTrue(receipt["stop_reason"].startswith("market_lister_failed:"))
        with self.assertRaises(AuthError) as caught:
            self.kill_login()
        self.assertEqual(caught.exception.status, "login_failures_need_operator")

    def test_session_level_chain(self):
        """The same chain on one SessionManager: two unknown logins refuse every later login, emergency included."""
        session, _ = self.session([login_ok(), no_session(), support.transport_failure("receive"),
                                   support.transport_failure("receive")])
        session.login()
        with self.assertRaises(Exception):
            session.keep_alive()
        with self.assertRaises(Exception):
            session.token()
        self.assertFalse(ledger_status(self.config, self.paths, clock=self.clock)["emergency_login_allowed"])


class LoginStopTests(R3Case):
    def test_r3_stops_the_run_after_the_keepalive_relogin_failure(self):
        session, transport = self.session([login_ok(), no_session(), support.transport_failure("receive"),
                                           login_ok()])
        receipt = self.record(session, self.probe_script(session))
        self.assertEqual(receipt["stop_reason"], "login_failed_this_run:unknown")
        self.assertEqual([r["url"] for r in transport.requests], [LOGIN, KEEPALIVE, LOGIN])   # no later login
        self.assertEqual(len(transport.responses), 1)
        self.assertEqual(self.outcomes(), ["proceed", "unknown"])
        self.assertEqual(len(self.server.sockets), 1)                  # no reconnect loop continued
        self.assertTrue(ledger_status(self.config, self.paths, clock=self.clock)["emergency_login_allowed"])
        self.kill_login()                                               # kill's emergency login still goes

    def test_a_failed_login_stops_the_run_too(self):
        session, transport = self.session([login_ok(), no_session(), login_refused(), login_ok()])
        receipt = self.record(session, self.probe_script(session))
        self.assertEqual(receipt["stop_reason"], "login_failed_this_run:auth_error")
        self.assertEqual(self.outcomes(), ["proceed", "auth_error"])

    def test_not_sent_stays_neutral(self):
        """A proven not_sent login keeps its neutral ledger treatment and does not set the run's flag (Q14): the run
        ends on the precount's own code, never login_failed_this_run."""
        session, transport = self.session([support.transport_failure("connect"), login_ok()])
        receipt = self.record(session, [fx.handshake()])
        self.assertEqual(self.outcomes(), ["not_sent"])
        self.assertTrue(receipt["stop_reason"].startswith("market_lister_failed:"), receipt["stop_reason"])
        self.assertTrue(ledger_review(self.config, self.paths, clock=self.clock)["clean"])

    def test_guard_installed_before_the_precounts_first_lazy_login(self):
        session, transport = self.session([support.transport_failure("receive"), login_ok()])
        receipt = self.record(session, [fx.handshake()])
        self.assertEqual(receipt["stop_reason"], "login_failed_this_run:unknown")
        self.assertEqual([r["url"] for r in transport.requests], [LOGIN])          # the run's last login attempt
        self.assertEqual(self.server.sockets, [])                                    # never connected

    def test_ledger_not_clean_stops_before_any_login_or_authenticate(self):
        append = auth._append_ledger
        self.paths.ensure()
        append(self.paths.login_ledger, {"kind": "login_attempt", "attempt_id": "x1", "at": support.BASE_UTC,
                                         "method": "interactive", "jurisdiction": "com", "emergency": False,
                                         "coexistence_ack": False})
        append(self.paths.login_ledger, {"kind": "login_outcome", "attempt_id": "x1", "at": support.BASE_UTC,
                                         "method": "interactive", "jurisdiction": "com", "outcome": "auth_error",
                                         "outcome_status": "FAIL", "outcome_error": None, "emergency": False,
                                         "coexistence_ack": False, "ban_until": None})
        session, transport = self.session([login_ok()])
        receipt = self.record(session, [fx.handshake()])
        self.assertEqual(receipt["stop_reason"], "login_ledger_not_clean")
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.server.sockets, [])                                    # authenticate never ran
        self.assertEqual(self.events("login_refused")[0]["problems"], ["login_failure_after_barrier"])

    def test_sticky_flag_survives_an_external_login_reset(self):
        test = self
        seen = {}

        class Probe(SessionManager):
            def maybe_keep_alive(self):
                try:
                    return super().maybe_keep_alive()
                finally:
                    if self.current_token() is None and "after" not in seen:
                        append_login_reset(test.paths, clock=test.clock)            # another process resets
                        seen["ledger_clean"] = ledger_review(test.config, test.paths, clock=test.clock)["clean"]
                        try:
                            self.token()
                            seen["after"] = "logged_in"
                        except LoginStopped as exc:
                            seen["after"] = exc.reason
        session, transport = self.session([login_ok(), no_session(), support.transport_failure("receive"),
                                           login_ok()], cls=Probe)
        receipt = self.record(session, self.probe_script(session))
        self.assertEqual(seen, {"ledger_clean": True, "after": "login_failed_this_run:unknown"})
        self.assertEqual(receipt["stop_reason"], "login_failed_this_run:unknown")
        self.assertEqual([r["url"] for r in transport.requests], [LOGIN, KEEPALIVE, LOGIN])

    def test_observer_needs_a_guard(self):
        session, _ = self.session([])
        with self.assertRaises(TypeError):
            session.guard_logins(None, on_outcome=lambda outcome: None)
        session.guard_logins(lambda: None, on_outcome=lambda outcome: None)
        session.guard_logins(None)
        self.assertIsNone(session._login_observer)


if __name__ == "__main__":
    unittest.main()
