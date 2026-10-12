"""SessionManager and login-ledger tests over FakeHttpTransport; synthetic credentials, no network, no Keychain."""

import hashlib
import json
import os
import ssl
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

from betangel_research.betfair import auth
from betangel_research.betfair import config as cfg
from betangel_research.betfair.errors import (AuthError, ConfigError, CredentialError, LoginBanned,
                                              LoginBudgetExceeded, TransportError)
from betangel_research.betfair.secret import Secret, refuse_forbidden

import test_betfair_support as support

USERNAME = "synthetic-user"
PASSWORD = "synthetic-password-0001"
DELAYED_KEY = "synthetic-delayed-key-0001"
LIVE_KEY = "synthetic-live-key-0001"
TOKEN_2 = "synthetic-session-token-0002"
CERTLOGIN = "https://identitysso-cert.betfair.com/api/certlogin"
LOGIN = "https://identitysso.betfair.com/api/login"
KEEPALIVE = "https://identitysso.betfair.com/api/keepAlive"
LOGOUT = "https://identitysso.betfair.com/api/logout"
# 2 x http.timeout_s (10 s in the test config) + 10 s: how long an unresolved attempt may be in flight.
PENDING_S = 30
LEDGER_ROW_KEYS = {
    "login_attempt": {"kind", "attempt_id", "at", "method", "jurisdiction", "emergency", "coexistence_ack"},
    "login_outcome": {"kind", "attempt_id", "at", "method", "jurisdiction", "outcome", "outcome_status",
                      "outcome_error", "emergency", "coexistence_ack", "ban_until"},
    "login_reset": {"kind", "at", "reason", "skipped_lines", "ban_until"},
    "note": {"kind", "at", "note", "keepalive_interval_s"},
}


def cert_ok(token=support.PLANTED_SECRET):
    return support.http_response({"sessionToken": token, "loginStatus": "SUCCESS"})


def cert_fail(status="INVALID_USERNAME_OR_PASSWORD"):
    return support.http_response({"loginStatus": status})


def interactive(status="SUCCESS", error="", token=support.PLANTED_SECRET):
    body = {"token": token if status in ("SUCCESS", "LIMITED_ACCESS") else "", "product": DELAYED_KEY,
            "status": status, "error": error}
    return support.http_response(body)


def sso(status="SUCCESS", error="", token=support.PLANTED_SECRET):
    return support.http_response({"token": token, "product": DELAYED_KEY, "status": status, "error": error})


class AuthTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.clock = support.FakeClock()
        self.paths = support.temp_state_paths(self)
        self.tls_calls = []

        def fake_tls(*args, **kwargs):
            self.tls_calls.append(args)
            return ssl.create_default_context()

        tls = patch.object(auth, "make_tls_context", side_effect=fake_tls)
        tls.start()
        self.addCleanup(tls.stop)

    def config(self, overrides=None, paths=None):
        paths = paths or self.paths
        return cfg.load_config(support.write_test_config(paths, overrides), paths=paths)

    def credentials(self, *, slot="delayed", password=PASSWORD, cert=True):
        home = self.paths.home
        return {"username": Secret(USERNAME), "password": Secret(password),
                "app_key": Secret(LIVE_KEY if slot == "live" else DELAYED_KEY), "app_key_slot": slot,
                "cert_path": home / "client-2048.crt" if cert else None,
                "key_path": home / "client-2048.key" if cert else None, "key_passphrase": None, "source": "keychain"}

    def session(self, responses=(), *, config=None, paths=None, credentials=None):
        transport = support.FakeHttpTransport(responses)
        paths = paths or self.paths
        session = auth.SessionManager(config or self.config(paths=paths), credentials or self.credentials(),
                                      paths=paths, transport=transport, clock=self.clock)
        return session, transport

    def ledger(self, paths=None):
        path = (paths or self.paths).login_ledger
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def outcomes(self, paths=None):
        return [row["outcome"] for row in self.ledger(paths) if row["kind"] == "login_outcome"]

    def attempts(self, key, paths=None):
        return [row[key] for row in self.ledger(paths) if row["kind"] == "login_attempt"]

    def write_rows(self, *rows, raw=""):
        """Append rows (and raw text) to the ledger as another process would."""
        self.paths.ensure()
        with open(self.paths.login_ledger, "a") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
            handle.write(raw)

    @staticmethod
    def attempt_row(attempt_id="00", at=support.BASE_UTC):
        return {"kind": "login_attempt", "attempt_id": attempt_id, "at": at, "method": "cert",
                "jurisdiction": "com", "emergency": False, "coexistence_ack": False}

    def test_certlogin_success_token_secret(self):
        session, transport = self.session([cert_ok()])
        result = session.login(coexistence_ack=True)
        self.assertEqual(result, {"status": "SUCCESS", "method": "cert", "can_bet": True,
                                  "logged_in_at": support.BASE_UTC})
        self.assertNotIn(support.PLANTED_SECRET, json.dumps(result))
        token = session.token()
        self.assertIsInstance(token, Secret)
        self.assertEqual(token.reveal(), support.PLANTED_SECRET)
        self.assertEqual(repr(token), "<redacted>")
        self.assertEqual(len(transport.requests), 1, "token() must reuse the session, not log in again")
        request = transport.requests[0]
        self.assertEqual((request["method"], request["url"], request["idempotent"]), ("POST", CERTLOGIN, False))
        self.assertIsInstance(request["tls_context"], ssl.SSLContext)
        self.assertEqual(self.tls_calls, [(self.paths.home / "client-2048.crt", self.paths.home / "client-2048.key",
                                           None)])
        self.assertEqual(transport.header_text(request, "X-Application"), DELAYED_KEY)
        self.assertEqual(request["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertNotIn("X-Authentication", request["headers"])
        self.assertIsNone(request["order_digest"])
        self.assertTrue(session.can_bet())
        self.assertEqual(self.outcomes(), ["proceed"])

    def test_certlogin_failure_raises_status_only(self):
        config = self.config({"session": {"max_consecutive_failed_logins": 3}})
        session, transport = self.session([cert_fail("INVALID_USERNAME_OR_PASSWORD"),
                                           cert_fail("CERT_AUTH_REQUIRED " + support.PLANTED_SECRET),
                                           support.http_response({"loginStatus": "SUCCESS"})], config=config)
        with self.assertRaises(AuthError) as caught:
            session.login()
        self.assertEqual(str(caught.exception), "INVALID_USERNAME_OR_PASSWORD")
        self.assertEqual(caught.exception.status, "INVALID_USERNAME_OR_PASSWORD")
        with self.assertRaises(AuthError) as caught:
            session.login()
        text = str(caught.exception) + repr(caught.exception)
        self.assertEqual(caught.exception.status, "malformed_code")
        for secret in (support.PLANTED_SECRET, USERNAME, PASSWORD, DELAYED_KEY):
            self.assertNotIn(secret, text)
        with self.assertRaisesRegex(AuthError, "login_response_invalid"):
            session.login()          # SUCCESS without a token is not a session
        self.assertIsNone(session.current_token())
        self.assertFalse(session.can_bet())
        self.assertEqual(self.outcomes(), ["auth_error", "auth_error", "invalid_response"])
        self.assertEqual(len(transport.requests), 3)

    def test_certlogin_body_urlencoded_bytes_special_chars(self):
        password = "p&ss=w+rd %é/?#0001"
        session, transport = self.session([cert_ok()], credentials=self.credentials(password=password))
        session.login()
        body = transport.requests[0]["body"]
        self.assertIsInstance(body, bytes)
        self.assertEqual(body, urlencode({"username": USERNAME, "password": password}).encode("ascii"))
        body.decode("ascii")
        self.assertNotIn(b"&ss=", body)
        self.assertEqual(parse_qs(body.decode("ascii"), strict_parsing=True),
                         {"username": [USERNAME], "password": [password]})

    def test_interactive_requires_accept_json(self):
        config = self.config({"credentials": {"login_method": "interactive"}})
        session, transport = self.session([interactive()], config=config,
                                          credentials=self.credentials(cert=False))
        self.assertEqual(session.login()["method"], "interactive")
        request = transport.requests[0]
        self.assertEqual((request["method"], request["url"], request["idempotent"]), ("POST", LOGIN, False))
        self.assertEqual(request["headers"]["Accept"], "application/json")
        self.assertEqual(transport.header_text(request, "X-Application"), DELAYED_KEY)
        self.assertEqual(request["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertIsNone(request["tls_context"])
        self.assertEqual(self.tls_calls, [])
        self.assertTrue(session.can_bet())
        parsed = auth.parse_interactive_login(interactive()["body"])
        self.assertEqual(set(parsed), {"status", "error", "token", "product_present"})
        self.assertTrue(parsed["product_present"])
        self.assertNotIn(DELAYED_KEY, repr(parsed))

    def test_limited_access_cannot_bet(self):
        config = self.config({"credentials": {"login_method": "interactive"}})
        session, _ = self.session([interactive("LIMITED_ACCESS", "PENDING_AUTH")], config=config,
                                  credentials=self.credentials(cert=False))
        self.assertFalse(session.can_bet(), "no session is never can_bet")
        result = session.login()
        self.assertEqual((result["status"], result["can_bet"]), ("LIMITED_ACCESS", False))
        self.assertFalse(session.can_bet())
        self.assertEqual(session.token().reveal(), support.PLANTED_SECRET)
        self.assertEqual(self.outcomes(), ["reads_only"])

    def test_exceptional_codes_matched_outside_fail(self):
        cases = [("SUCCESS", "STRONG_CODE_FAIL", AuthError),
                 ("LIMITED_ACCESS", "MULTI_FACTOR_AUTHENTICATION_REQUIRED", AuthError),
                 ("SUCCESS", "TEMPORARY_BAN_TOO_MANY_REQUESTS", LoginBanned),
                 ("FAIL", "TEMPORARY_BAN_TOO_MANY_REQUESTS", LoginBanned),
                 ("LOGIN_RESTRICTED", "STRONG_AUTH_CODE_REQUIRED", AuthError),
                 ("FAIL", "INVALID_USERNAME_OR_PASSWORD", AuthError)]
        for status, error, expected in cases:
            with self.subTest(status=status, error=error):
                paths = support.temp_state_paths(self)
                config = self.config({"credentials": {"login_method": "interactive"}}, paths=paths)
                session, transport = self.session([interactive(status, error), sso()], config=config, paths=paths,
                                                  credentials=self.credentials(cert=False))
                with self.assertRaises(expected) as caught:
                    session.login()
                if expected is AuthError:
                    self.assertEqual(caught.exception.error_code, error)
                self.assertIsNone(session.current_token())
                self.assertFalse(session.can_bet())
                # A token issued beside a refusal is logged out at once and never kept; during a ban
                # nothing more is sent.
                issued = status in ("SUCCESS", "LIMITED_ACCESS") and expected is AuthError
                self.assertEqual([r["url"] for r in transport.requests], [LOGIN, LOGOUT] if issued else [LOGIN])
                if issued:
                    self.assertEqual(transport.header_text(transport.requests[1], "X-Authentication"),
                                     support.PLANTED_SECRET)
                    self.assertEqual(self.outcomes(paths), ["session_refused"])

    def test_keepalive_success_requires_status_success(self):
        session, transport = self.session([cert_ok(), sso("SUCCESS", ""), sso("SUCCESS", None),
                                           support.http_response({"status": "SUCCESS"}), sso("FAIL", "INTERNAL_ERROR"),
                                           support.http_response({"token": support.PLANTED_SECRET, "error": ""})])
        with self.assertRaisesRegex(AuthError, "not_logged_in"):
            session.keep_alive()
        session.login()
        self.assertEqual(session.keep_alive(), {"status": "SUCCESS", "error": None})
        self.assertEqual(session.keep_alive(), {"status": "SUCCESS", "error": None})
        self.assertEqual(session.keep_alive(), {"status": "SUCCESS", "error": None})
        self.assertEqual(session.keep_alive(), {"status": "FAIL", "error": "INTERNAL_ERROR"})
        self.assertEqual(session.keep_alive(), {"status": "FAIL", "error": "invalid_response"})
        request = transport.requests[1]
        self.assertEqual((request["url"], request["idempotent"], request["body"]), (KEEPALIVE, True, None))
        self.assertEqual(request["headers"]["Accept"], "application/json")
        self.assertEqual(transport.header_text(request, "X-Authentication"), support.PLANTED_SECRET)
        self.assertEqual(transport.header_text(request, "X-Application"), DELAYED_KEY)
        self.assertEqual(session.token().reveal(), support.PLANTED_SECRET, "a FAIL without NO_SESSION keeps the token")
        self.assertEqual(len(transport.requests), 6)

    def test_keepalive_no_session_halves_interval_and_relogins(self):
        session, transport = self.session([cert_ok(), sso("FAIL", "NO_SESSION"), cert_ok(TOKEN_2)])
        session.login(coexistence_ack=True)
        self.assertEqual(session.keepalive_interval_s, 3600)
        self.assertEqual(session.keep_alive(), {"status": "FAIL", "error": "NO_SESSION"})
        self.assertEqual(session.keepalive_interval_s, 1800)
        self.assertEqual(session.token().reveal(), TOKEN_2)
        self.assertEqual([r["url"] for r in transport.requests], [CERTLOGIN, KEEPALIVE, CERTLOGIN])
        notes = [row for row in self.ledger() if row["kind"] == "note"]
        self.assertEqual(notes, [{"kind": "note", "at": support.BASE_UTC, "note": "session_shorter_than_configured",
                                  "keepalive_interval_s": 1800}])
        attempts = [row for row in self.ledger() if row["kind"] == "login_attempt"]
        self.assertEqual([row["coexistence_ack"] for row in attempts], [True, True], "relogin keeps the flags")
        # The interval never drops below 60 s.
        config = self.config({"session": {"keepalive_interval_s": 100}})
        short, _ = self.session([cert_ok(), sso("FAIL", "NO_SESSION"), cert_ok(TOKEN_2)], config=config)
        short.login()
        short.keep_alive()
        self.assertEqual(short.keepalive_interval_s, 60)

    def test_maybe_keep_alive_uses_current_interval(self):
        session, transport = self.session([cert_ok(), sso()])
        self.assertIsNone(session.maybe_keep_alive())
        session.login()
        self.clock.advance(3599)
        self.assertIsNone(session.maybe_keep_alive())
        self.clock.advance(1)
        self.assertEqual(session.maybe_keep_alive(), {"status": "SUCCESS", "error": None})
        self.assertIsNone(session.maybe_keep_alive())
        self.assertEqual(len(transport.requests), 2)

    def test_keepalive_failure_waits_before_retry(self):
        # A keepAlive that keeps failing (here a 405 page, as a wrong sso_http_method would give) is
        # retried once per KEEPALIVE_RETRY_S, not on every poll.
        not_allowed = support.http_response(body=b"<html>405 Method Not Allowed</html>", status=405,
                                            headers={"content-type": "text/html"})
        session, transport = self.session([cert_ok(), not_allowed, not_allowed, sso()])
        session.login()
        self.clock.advance(3600)
        self.assertEqual(session.maybe_keep_alive(), {"status": "FAIL", "error": "invalid_response"})
        for _ in range(10):
            self.clock.advance(1)
            self.assertIsNone(session.maybe_keep_alive())
        self.assertEqual(len(transport.requests), 2, "one keepAlive in 10 polls after a failure")
        self.clock.advance(49)
        self.assertIsNone(session.maybe_keep_alive())
        self.clock.advance(1)
        self.assertEqual(session.maybe_keep_alive(), {"status": "FAIL", "error": "invalid_response"})
        self.clock.advance(60)
        self.assertEqual(session.maybe_keep_alive(), {"status": "SUCCESS", "error": None})
        self.clock.advance(60)
        self.assertIsNone(session.maybe_keep_alive(), "after a success the full interval applies again")
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(session.token().reveal(), support.PLANTED_SECRET)
        # A raised failure is held off the same way (the error still reaches the caller).
        raising, raising_transport = self.session([cert_ok(), support.transport_failure("receive"), sso()])
        raising.login()
        self.clock.advance(3600)
        with self.assertRaises(TransportError):
            raising.maybe_keep_alive()
        self.clock.advance(59)
        self.assertIsNone(raising.maybe_keep_alive())
        self.clock.advance(1)
        self.assertEqual(raising.maybe_keep_alive(), {"status": "SUCCESS", "error": None})
        self.assertEqual(len(raising_transport.requests), 3)

    def test_certlogin_limited_access_is_a_failure(self):
        config = self.config({"session": {"max_successful_logins_per_10min": 1, "max_consecutive_failed_logins": 3}})
        limited = support.http_response({"sessionToken": TOKEN_2, "loginStatus": "LIMITED_ACCESS"})
        session, transport = self.session([limited, sso(token=TOKEN_2), cert_ok()], config=config)
        with self.assertRaises(AuthError) as caught:
            session.login()
        self.assertEqual((str(caught.exception), caught.exception.status), ("LIMITED_ACCESS", "LIMITED_ACCESS"))
        self.assertIsNone(session.current_token())
        self.assertFalse(session.can_bet())
        self.assertEqual([r["url"] for r in transport.requests], [CERTLOGIN, LOGOUT])
        self.assertEqual(transport.header_text(transport.requests[1], "X-Authentication"), TOKEN_2,
                         "the issued session is logged out, never kept")
        self.assertEqual([(r["outcome"], r["outcome_status"]) for r in self.ledger() if r["kind"] == "login_outcome"],
                         [("session_refused", "LIMITED_ACCESS")])
        status = auth.ledger_status(config, self.paths, clock=self.clock)
        self.assertEqual((status["consecutive_failures"], status["successes_last_600s"]), (1, 1))
        with self.assertRaises(LoginBudgetExceeded):
            session.login()            # a session was created: it counts toward the cap
        self.assertEqual(auth.parse_cert_login(limited["body"]), {"status": "LIMITED_ACCESS", "token": None})
        # Without a token LIMITED_ACCESS is a plain failure, with nothing to log out.
        bare_paths = support.temp_state_paths(self)
        bare, bare_transport = self.session([support.http_response({"loginStatus": "LIMITED_ACCESS"})],
                                            paths=bare_paths)
        with self.assertRaisesRegex(AuthError, "^LIMITED_ACCESS$"):
            bare.login()
        self.assertEqual(self.outcomes(bare_paths), ["auth_error"])
        self.assertEqual(len(bare_transport.requests), 1)

    def test_sso_method_get_and_post(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                config = self.config({"session": {"sso_http_method": method}})
                session, transport = self.session([cert_ok(), sso(), sso()], config=config)
                session.login()
                session.keep_alive()
                session.logout()
                self.assertEqual([(r["method"], r["url"], r["body"]) for r in transport.requests[1:]],
                                 [(method, KEEPALIVE, None), (method, LOGOUT, None)])
                self.assertEqual(transport.requests[0]["method"], "POST", "logins are always POST")

    def test_logout_on_close(self):
        session, transport = self.session([cert_ok(), sso()])
        session.login()
        self.assertEqual(session.close(), {"status": "SUCCESS", "error": None})
        request = transport.requests[-1]
        self.assertEqual(request["url"], LOGOUT)
        self.assertEqual(transport.header_text(request, "X-Authentication"), support.PLANTED_SECRET)
        self.assertIsNone(session.current_token())
        self.assertFalse(session.can_bet())
        self.assertIsNone(session.close(), "nothing to log out the second time")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(transport.closed, 0, "an injected transport belongs to the caller")
        failing, failing_transport = self.session([cert_ok(), support.transport_failure("receive")])
        failing.login()
        result = failing.close()
        self.assertEqual(result, {"status": "logout_failed", "error": "scripted_failure"})
        self.assertIsNone(failing.current_token(), "the token is dropped whatever the logout outcome")
        owned = auth.SessionManager(self.config(), self.credentials(), paths=self.paths, clock=self.clock)
        with patch.object(owned.transport, "close") as closed:
            self.assertIsNone(owned.close())
        closed.assert_called_once_with()

    def test_ban_persisted_blocks_across_restart(self):
        session, transport = self.session([cert_fail("TEMPORARY_BAN_TOO_MANY_REQUESTS")])
        with self.assertRaises(LoginBanned) as caught:
            session.login()
        self.assertEqual(caught.exception.until_utc, "2020-01-01T12:21:00.123457Z")
        restarted, restarted_transport = self.session([cert_ok()])
        with self.assertRaises(LoginBanned):
            restarted.login()
        with self.assertRaises(LoginBanned):
            restarted.login(emergency=True)
        with self.assertRaises(LoginBanned):
            restarted.token()
        self.assertEqual(restarted_transport.requests, [])
        status = auth.ledger_status(self.config(), self.paths, clock=self.clock)
        self.assertEqual((status["banned_until"], status["login_allowed"], status["emergency_login_allowed"]),
                         ("2020-01-01T12:21:00.123457Z", False, False))
        self.clock.advance(1259)
        with self.assertRaises(LoginBanned):
            restarted.login()
        self.clock.advance(1)
        self.assertEqual(restarted.login()["status"], "SUCCESS")

    def test_successful_login_cap(self):
        config = self.config({"session": {"max_successful_logins_per_10min": 2}})
        session, transport = self.session([cert_ok(), cert_ok(), support.transport_failure("connect"), cert_ok()],
                                          config=config)
        session.login()
        session.login()
        with self.assertRaises(LoginBudgetExceeded):
            session.login()
        with self.assertRaises(LoginBudgetExceeded):
            session.invalidate_and_relogin()
        self.assertEqual(len(transport.requests), 2)
        self.clock.advance(600)
        with self.assertRaises(TransportError):
            session.login()          # never sent: counts neither as a success nor as a failure
        self.assertEqual(session.login()["status"], "SUCCESS")
        self.assertEqual(self.outcomes(), ["proceed", "proceed", "not_sent", "proceed"])

    def test_emergency_bypasses_success_cap_not_ban_or_failures(self):
        config = self.config({"session": {"max_successful_logins_per_10min": 1}})
        session, transport = self.session([cert_ok(), cert_ok()], config=config)
        session.login()
        with self.assertRaises(LoginBudgetExceeded):
            session.login()
        self.assertEqual(session.login(emergency=True)["status"], "SUCCESS")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual([r["emergency"] for r in self.ledger() if r["kind"] == "login_attempt"], [False, True])

        failures = support.temp_state_paths(self)
        failing, failing_transport = self.session([cert_fail(), cert_fail(), cert_ok()], paths=failures)
        for _ in range(2):
            with self.assertRaises(AuthError):
                failing.login()
        with self.assertRaisesRegex(AuthError, "login_failures_need_operator"):
            failing.login(emergency=True)
        self.assertEqual(len(failing_transport.requests), 2)

        banned = support.temp_state_paths(self)
        ban, ban_transport = self.session([cert_fail("TEMPORARY_BAN_TOO_MANY_REQUESTS"), cert_ok()], paths=banned)
        with self.assertRaises(LoginBanned):
            ban.login(emergency=True)
        with self.assertRaises(LoginBanned):
            ban.login(emergency=True)
        self.assertEqual(len(ban_transport.requests), 1)

    def test_consecutive_failures_block_until_reset(self):
        session, transport = self.session([cert_fail(), cert_fail(), cert_ok(), cert_fail(), cert_ok()])
        for _ in range(2):
            with self.assertRaises(AuthError):
                session.login()
        with self.assertRaisesRegex(AuthError, "login_failures_need_operator"):
            session.login()
        self.assertEqual(len(transport.requests), 2)
        self.assertFalse(auth.ledger_status(self.config(), self.paths, clock=self.clock)["login_allowed"])
        row = auth.append_login_reset(self.paths, clock=self.clock)
        self.assertEqual(row, {"kind": "login_reset", "at": support.BASE_UTC, "reason": "operator_login_reset",
                               "skipped_lines": 0, "ban_until": None})
        self.assertEqual(session.login()["status"], "SUCCESS")
        with self.assertRaises(AuthError):
            session.login()          # one failure after a success: still allowed next time
        self.assertEqual(session.login()["status"], "SUCCESS")
        self.assertEqual(len(transport.requests), 5)

    def test_unknown_outcome_counts_against_both_limits(self):
        config = self.config({"session": {"max_successful_logins_per_10min": 1}})
        session, transport = self.session([support.transport_failure("receive"), cert_ok()], config=config)
        with self.assertRaises(TransportError) as caught:
            session.login()
        self.assertTrue(caught.exception.may_have_arrived)
        self.assertEqual(len(transport.requests), 1, "a login is never resent")
        self.assertEqual(self.outcomes(), ["unknown"])
        with self.assertRaises(LoginBudgetExceeded):
            session.login()          # it may have been a success
        status = auth.ledger_status(config, self.paths, clock=self.clock)
        self.assertEqual(status["consecutive_failures"], 1)   # and it may have been a failure
        self.assertEqual(session.login(emergency=True)["status"], "SUCCESS")

    def test_crashed_attempt_counts_as_failure_and_corrupt_ledger_fails_closed(self):
        self.write_rows(self.attempt_row("00"), self.attempt_row("01"))
        self.clock.advance(PENDING_S)      # no longer possibly in flight: two crashed attempts
        session, transport = self.session([cert_ok()])
        with self.assertRaisesRegex(AuthError, "login_failures_need_operator"):
            session.login()
        self.assertEqual(transport.requests, [])
        self.write_rows(raw='{"kind": "login_reset", "at": "not-a-time"}\n')
        with self.assertRaisesRegex(AuthError, "login_ledger_unreadable"):
            session.login(emergency=True)
        self.assertEqual(transport.requests, [])

    def test_in_flight_login_elsewhere_is_not_a_failure(self):
        # One real failure, then another process reserves an attempt and is still waiting for Betfair.
        config = self.config({"session": {"max_successful_logins_per_10min": 2}})
        session, transport = self.session([cert_fail(), cert_ok()], config=config)
        with self.assertRaises(AuthError):
            session.login()
        self.clock.advance(1)
        self.write_rows(self.attempt_row("other-process", at=self.clock.utc_now()))
        status = auth.ledger_status(config, self.paths, clock=self.clock)
        self.assertEqual((status["consecutive_failures"], status["successes_last_600s"]), (1, 1),
                         "in flight: not a failure yet, but it may become a session")
        self.clock.advance(PENDING_S - 1)
        self.assertEqual(auth.ledger_status(config, self.paths, clock=self.clock)["consecutive_failures"], 1)
        self.assertEqual(session.login(emergency=True)["status"], "SUCCESS")
        # Once older than a login can take, an attempt without an outcome is a failure.
        crashed = support.temp_state_paths(self)
        self.paths = crashed
        session, transport = self.session([cert_fail(), cert_ok()], paths=crashed)
        with self.assertRaises(AuthError):
            session.login()
        self.write_rows(self.attempt_row("crashed", at=self.clock.utc_now()))
        self.clock.advance(PENDING_S)
        with self.assertRaisesRegex(AuthError, "login_failures_need_operator"):
            session.login(emergency=True)
        self.assertEqual(len(transport.requests), 1)
        # An attempt dated in the future (a wall clock stepped back) is not treated as in flight.
        future = support.temp_state_paths(self)
        self.paths = future
        session, transport = self.session([cert_fail(), cert_ok()], paths=future)
        with self.assertRaises(AuthError):
            session.login()
        self.write_rows(self.attempt_row("future", at="2020-01-01T13:00:00Z"))
        with self.assertRaisesRegex(AuthError, "login_failures_need_operator"):
            session.login(emergency=True)

    def test_unresolved_and_invalid_response_count_toward_cap(self):
        config = self.config({"session": {"max_successful_logins_per_10min": 1}})
        self.write_rows(self.attempt_row("crashed"))
        session, transport = self.session([support.http_response(body=b"<html>busy</html>", status=503),
                                           cert_ok()], config=config)
        with self.assertRaises(LoginBudgetExceeded):
            session.login()            # the unresolved attempt may have created a session
        self.assertEqual(transport.requests, [])
        other = support.temp_state_paths(self)
        self.paths = other
        session, transport = self.session([support.http_response(body=b"<html>busy</html>", status=503),
                                           cert_ok()], config=config, paths=other)
        with self.assertRaisesRegex(AuthError, "login_response_invalid"):
            session.login()
        self.assertEqual(self.outcomes(), ["invalid_response"])
        with self.assertRaises(LoginBudgetExceeded):
            session.login()            # an unreadable answer may have been a success
        self.assertEqual(len(transport.requests), 1)

    def test_malformed_ban_until_fails_closed(self):
        self.write_rows({"kind": "login_outcome", "attempt_id": "00", "at": support.BASE_UTC, "method": "cert",
                         "jurisdiction": "com", "outcome": "login_ban", "outcome_status": "FAIL",
                         "outcome_error": "TEMPORARY_BAN_TOO_MANY_REQUESTS", "emergency": False,
                         "coexistence_ack": False, "ban_until": "garbage"})
        session, transport = self.session([cert_ok()])
        for emergency in (False, True):
            with self.assertRaisesRegex(AuthError, "login_ledger_unreadable"):
                session.login(emergency=emergency)
        with self.assertRaisesRegex(AuthError, "login_ledger_unreadable"):
            auth.ledger_status(self.config(), self.paths, clock=self.clock)
        self.assertEqual(transport.requests, [])

    def test_torn_ledger_line_cleared_by_login_reset(self):
        session, transport = self.session([cert_ok(), cert_ok(TOKEN_2)])
        session.login()
        self.write_rows(raw='{"kind":"login_attempt","at":"2020-01')       # a crash mid-append: no newline
        for emergency in (False, True):
            with self.assertRaisesRegex(AuthError, "login_ledger_unreadable"):
                session.login(emergency=emergency)
        row = auth.append_login_reset(self.paths, clock=self.clock)
        self.assertEqual(row, {"kind": "login_reset", "at": support.BASE_UTC, "reason": "operator_login_reset",
                               "skipped_lines": 1, "ban_until": "2020-01-01T12:20:00.123457Z"})
        lines = self.paths.login_ledger.read_text().splitlines()
        self.assertEqual(lines[-2], '{"kind":"login_attempt","at":"2020-01', "the torn bytes stay, on their own line")
        self.assertEqual(json.loads(lines[-1]), row, "the reset row was not merged into the torn line")
        # The skipped line could have been a ban, so the repair holds every login for LOGIN_BAN_S.
        with self.assertRaises(LoginBanned):
            session.login(emergency=True)
        self.assertEqual(auth.ledger_status(self.config(), self.paths, clock=self.clock)["banned_until"],
                         row["ban_until"])
        self.clock.advance(1200)
        self.assertEqual(session.login(emergency=True)["status"], "SUCCESS")
        self.assertEqual(len(transport.requests), 2)
        # A second reset has nothing new to skip; a new torn line after the repair fails closed again.
        self.assertEqual(auth.append_login_reset(self.paths, clock=self.clock)["skipped_lines"], 0)
        self.write_rows(raw="garbage\n")
        with self.assertRaisesRegex(AuthError, "login_ledger_unreadable"):
            auth.ledger_status(self.config(), self.paths, clock=self.clock)
        self.assertEqual(auth.append_login_reset(self.paths, clock=self.clock)["skipped_lines"], 1)

    def test_relogin_reuses_emergency_flag(self):
        config = self.config({"session": {"max_successful_logins_per_10min": 1}})
        session, transport = self.session([cert_ok(), cert_ok(TOKEN_2)], config=config)
        session.login(emergency=True)
        self.assertEqual(session.invalidate_and_relogin()["status"], "SUCCESS",
                         "a kill/cancel/reconcile session meeting INVALID_SESSION mid-sweep is not capped")
        self.assertEqual(session.token().reveal(), TOKEN_2)
        self.assertEqual(self.attempts("emergency"), [True, True])
        self.assertEqual(len(transport.requests), 2)

    def test_lazy_login_after_a_failed_emergency_relogin_stays_emergency(self):
        """Round 4: token() logged in lazily with emergency=False even in a kill's session, so once an emergency
        relogin failed, the next read's login was refused by the successful-login cap (LoginBudgetExceeded) and
        the kill's re-sweeps and final check could read nothing. The lazy login now reuses the flags of the last
        successful login, as invalidate_and_relogin does; it still never skips a ban or the failure cap."""
        config = self.config({"session": {"max_successful_logins_per_10min": 1}})
        session, transport = self.session([cert_ok(), support.transport_failure("connect"), cert_ok(TOKEN_2)],
                                          config=config)
        session.login(emergency=True)
        with self.assertRaises(TransportError):
            session.invalidate_and_relogin()                       # the emergency relogin is not sent (a blip)
        self.assertIsNone(session.current_token())
        self.assertEqual(session.token().reveal(), TOKEN_2)          # an emergency login: not capped
        self.assertEqual(self.attempts("emergency"), [True, True, True])
        # A session whose last successful login was a normal one stays capped on its lazy login.
        other = support.temp_state_paths(self)
        normal, _ = self.session([cert_ok(), sso(), cert_ok(TOKEN_2)], config=self.config(
            {"session": {"max_successful_logins_per_10min": 1}}, paths=other), paths=other)
        normal.login()
        normal.logout()
        with self.assertRaises(LoginBudgetExceeded):
            normal.token()
        self.assertEqual(self.attempts("emergency", other), [False])

    def test_coexistence_ack_recorded_on_lazy_login_and_relogin(self):
        session, transport = self.session([cert_ok(), sso("FAIL", "NO_SESSION"), cert_ok(TOKEN_2)])
        session.close()
        acked = auth.SessionManager(self.config(), self.credentials(), paths=self.paths, transport=transport,
                                    clock=self.clock, coexistence_ack=True)
        self.assertEqual(acked.token().reveal(), support.PLANTED_SECRET)        # lazy login
        acked.keep_alive()                                                       # NO_SESSION -> relogin
        self.assertEqual(self.attempts("coexistence_ack"), [True, True])
        self.assertTrue(all(row["coexistence_ack"] for row in self.ledger() if row["kind"] == "login_outcome"))
        # An explicit acknowledged login carries over to a later lazy login in the same process.
        other = support.temp_state_paths(self)
        explicit, _ = self.session([cert_ok(), sso(), cert_ok(TOKEN_2)], paths=other)
        explicit.login(coexistence_ack=True)
        explicit.logout()
        self.assertEqual(explicit.token().reveal(), TOKEN_2)
        self.assertEqual(self.attempts("coexistence_ack", other), [True, True])
        # Without any acknowledgement the rows say false.
        plain_paths = support.temp_state_paths(self)
        plain, _ = self.session([cert_ok()], paths=plain_paths)
        plain.token()
        self.assertEqual(self.attempts("coexistence_ack", plain_paths), [False])
        with self.assertRaises(ValueError):
            auth.SessionManager(self.config(), self.credentials(), paths=self.paths, transport=transport,
                                coexistence_ack="yes")

    def test_relogin_never_nested(self):
        session, transport = self.session([cert_ok()])
        session.login()
        session._relogin_active = True
        with self.assertRaisesRegex(AuthError, "relogin_already_in_progress"):
            session.invalidate_and_relogin()
        self.assertEqual(len(transport.requests), 1)

    def test_account_fingerprint_never_username(self):
        session, _ = self.session([cert_ok()])
        with self.assertRaisesRegex(AuthError, "not_logged_in"):
            session.account_fingerprint()
        session.login()
        fingerprint = session.account_fingerprint()
        self.assertEqual(fingerprint, hashlib.sha256(b"betfair-account:" + USERNAME.encode()).hexdigest()[:16])
        self.assertRegex(fingerprint, r"^[0-9a-f]{16}$")
        self.assertNotIn(USERNAME, fingerprint)
        self.assertNotIn(USERNAME, self.paths.login_ledger.read_text())
        self.assertNotIn(USERNAME, json.dumps(session.describe()))

    def test_account_fingerprint_normalises_the_username(self):
        """Regression (audit): the fingerprint hashed the username verbatim, so " Name" and "name" were two
        accounts to the one-profile-per-account registry. Folding only ever makes it refuse more."""
        variants = {auth.account_fingerprint_for(Secret(name)) for name in ("YuHangExample", "yuhangexample",
                                                                       " YuHangExample", "YUHANGEXAMPLE\t")}
        self.assertEqual(variants, {hashlib.sha256(b"betfair-account:yuhangexample").hexdigest()[:16]})
        self.assertNotEqual(auth.account_fingerprint_for(Secret("yuhang-example")), variants.pop())

    def test_token_never_in_state_files(self):
        session, _ = self.session([cert_ok(), sso("FAIL", "NO_SESSION"), cert_ok(TOKEN_2), sso(token=TOKEN_2),
                                   sso(token=TOKEN_2)])
        session.login(coexistence_ack=True)
        session.keep_alive()
        session.keep_alive()
        session.close()
        secrets = (support.PLANTED_SECRET, TOKEN_2, USERNAME, PASSWORD, DELAYED_KEY)
        files = [path for path in self.paths.root.rglob("*") if path.is_file()]
        self.assertIn(self.paths.login_ledger, files)
        for path in files:
            data = path.read_bytes()
            for secret in secrets:
                self.assertNotIn(secret.encode(), data, f"{secret!r} found in {path.name}")
        for row in self.ledger():
            refuse_forbidden(row, where="ledger")
            self.assertEqual(set(row), LEDGER_ROW_KEYS[row["kind"]])
        self.assertEqual(oct(os.stat(self.paths.login_ledger).st_mode & 0o777), "0o600")

    def test_parsers_status_only_and_secret_wrapping(self):
        parsed = auth.parse_cert_login(cert_ok()["body"])
        self.assertEqual(parsed["status"], "SUCCESS")
        self.assertIsInstance(parsed["token"], Secret)
        self.assertEqual(auth.parse_cert_login(cert_fail()["body"]), {"status": "INVALID_USERNAME_OR_PASSWORD",
                                                                      "token": None})
        for body in (b"not json " + support.PLANTED_SECRET.encode(), b"[]", b'{"loginStatus": 5}', b"",
                     b'{"loginStatus": "SUCCESS", "loginStatus": "FAIL"}'):
            with self.subTest(body=body):
                with self.assertRaises(AuthError) as caught:
                    auth.parse_cert_login(body)
                self.assertEqual(str(caught.exception), "login_response_invalid")
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
        with self.assertRaises(AuthError):
            auth.parse_interactive_login(b'{"token": "x"}')

    def test_constructor_refuses_mismatched_profile_and_plain_credentials(self):
        other_profile = support.temp_state_paths(self, profile="other")
        with self.assertRaisesRegex(ConfigError, "session_profile_does_not_match_state_profile"):
            auth.SessionManager(self.config(), self.credentials(), paths=other_profile,
                                transport=support.FakeHttpTransport())
        credentials = self.credentials()
        credentials["password"] = PASSWORD
        with self.assertRaisesRegex(CredentialError, "credential_missing:password") as caught:
            auth.SessionManager(self.config(), credentials, paths=self.paths, transport=support.FakeHttpTransport())
        self.assertNotIn(PASSWORD, str(caught.exception))
        missing_cert = self.credentials(cert=False)
        with self.assertRaisesRegex(CredentialError, "certificate_login_needs_cert_and_key"):
            auth.SessionManager(self.config(), missing_cert, paths=self.paths, transport=support.FakeHttpTransport())


if __name__ == "__main__":
    unittest.main()
