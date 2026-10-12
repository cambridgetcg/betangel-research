"""HttpTransport, stream-socket and capability tests over scripted in-memory sockets; no network."""

import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import pickle
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from betangel_research.betfair import api, capability, transport
from betangel_research.betfair.errors import ConfigError, CredentialError, OrderRefused, TransportError
from betangel_research.betfair.lifecycle import outcome_from_exception
from betangel_research.betfair.secret import Secret

import test_betfair_support as support

API = "https://api.betfair.com/exchange/betting/json-rpc/v1"
ACCOUNT = "https://api.betfair.com/exchange/account/json-rpc/v1"
CERTLOGIN = "https://identitysso-cert.betfair.com/api/certlogin"
KEEPALIVE = "https://identitysso.betfair.com/api/keepAlive"
LOGIN = "https://identitysso.betfair.com/api/login"
HOSTS = frozenset({"identitysso.betfair.com", "identitysso-cert.betfair.com", "api.betfair.com"})
READ_METHOD = "SportsAPING/v1.0/listMarketBook"
# Built by concatenation so no order-method literal appears even in test source.
ORDER_METHOD = "SportsAPING/v1.0/" + "place" + "Orders"


def rpc_body(method="SportsAPING/v1.0/listMarketBook", params=None, rpc_id=1):
    return json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {"marketIds": ["1.1"]},
                       "id": rpc_id}, separators=(",", ":")).encode()


def consumed_digest(body, method=ORDER_METHOD):
    """body's sha256 after a capability was consumed for it, as JsonRpcClient.send_order does before sending."""
    digest = hashlib.sha256(body).hexdigest()
    cap = capability._mint("decision-transport-test", method, digest, "live", 0)
    cap.consume(method=method, body_sha256=digest, now_monotonic_ns=1)
    return digest


def ok(value=None, **headers):
    body = json.dumps(value if value is not None else {"jsonrpc": "2.0", "result": [], "id": 1}).encode()
    return support.http_bytes(200, body, headers or None)


class TransportTests(unittest.TestCase):
    def setUp(self):
        # Real HttpTransport over scripted sockets: the switch is lifted, the socket guards stay on.
        support.scripted_network_only(self)
        self.clock = support.FakeClock()
        self.headers = {"X-Application": Secret("synthetic-delayed-key-0001"),
                        "X-Authentication": Secret(support.PLANTED_SECRET),
                        "Accept": "application/json", "Content-Type": "application/json"}

    def make(self, *sockets, connect_error=None, **kwargs):
        script = support.ConnectionScript(*sockets, connect_error=connect_error)
        kwargs.setdefault("clock", self.clock)
        http_transport = transport.HttpTransport(allowed_hosts=HOSTS, connection_factory=script, env={}, **kwargs)
        self.addCleanup(http_transport.close)
        return http_transport, script

    def read(self, http_transport, body=None, url=API, idempotent=True, deadline_s=10, **kwargs):
        return http_transport.request("POST", url, dict(self.headers), body or rpc_body(), deadline_s=deadline_s,
                                      idempotent=idempotent, **kwargs)

    def order(self, http_transport, body=None, **kwargs):
        body = body or rpc_body(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        if "order_digest" not in kwargs:
            kwargs["order_digest"] = consumed_digest(body)
        return http_transport.request("POST", API, dict(self.headers), body, deadline_s=10, idempotent=False,
                                      **kwargs)

    def assert_refused(self, code, call, *, phase="preflight"):
        with self.assertRaises(TransportError) as caught:
            call()
        self.assertEqual((caught.exception.code, caught.exception.phase), (code, phase))
        return caught.exception

    def test_gzip_decoded_with_size_cap(self):
        payload = json.dumps({"jsonrpc": "2.0", "result": [{"marketId": "1.1"}] * 50, "id": 1}).encode()
        compressed = support.gzip_bytes(payload)
        sock = support.ScriptedSocket([support.http_bytes(200, compressed, {
            "Content-Encoding": "gzip", "Set-Cookie": "vid=" + support.PLANTED_SECRET, "X-Trace": "abc"})])
        http_transport, _ = self.make(sock)
        result = self.read(http_transport)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], payload)
        self.assertEqual(result["headers"]["content-encoding"], "gzip")
        self.assertEqual(result["headers"]["x-trace"], "abc")
        self.assertNotIn("set-cookie", result["headers"])
        small, _ = self.make(support.ScriptedSocket([support.http_bytes(200, compressed, {"Content-Encoding": "gzip"})]),
                             max_response_bytes=len(compressed) + 10)
        error = self.assert_refused("response_too_large", lambda: self.read(small), phase="decode")
        self.assertTrue(error.may_have_arrived)
        raw_cap, _ = self.make(support.ScriptedSocket([support.http_bytes(200, b"x" * 500)]), max_response_bytes=100)
        self.assert_refused("response_too_large", lambda: self.read(raw_cap), phase="receive")

    def test_decompression_bomb_capped(self):
        bomb = support.gzip_bytes(b"\0" * (40 * 1024 * 1024))
        self.assertLess(len(bomb), 100_000)
        http_transport, _ = self.make(support.ScriptedSocket([support.http_bytes(200, bomb, {"Content-Encoding": "gzip"})]),
                                      max_response_bytes=1024 * 1024)
        started = time.perf_counter()
        self.assert_refused("response_too_large", lambda: self.read(http_transport), phase="decode")
        self.assertLess(time.perf_counter() - started, 5)
        truncated, _ = self.make(support.ScriptedSocket([support.http_bytes(200, support.gzip_bytes(b"x" * 100)[:-6],
                                                                            {"Content-Encoding": "gzip"})]))
        self.assert_refused("decode_failed", lambda: self.read(truncated), phase="decode")
        odd, _ = self.make(support.ScriptedSocket([support.http_bytes(200, b"x", {"Content-Encoding": "br"})]))
        self.assert_refused("unsupported_content_encoding", lambda: self.read(odd), phase="decode")

    def test_idle_reconnect_before_send(self):
        first = support.ScriptedSocket([ok(), ok()])
        http_transport, script = self.make(first)
        self.read(http_transport)
        self.clock.advance(100)
        self.read(http_transport)
        self.assertEqual(len(script.calls), 1)          # warm keep-alive connection reused
        second = support.ScriptedSocket([ok()])
        script.sockets.append(second)
        self.clock.advance(170)
        self.order(http_transport)
        self.assertEqual(len(script.calls), 2)          # idle >= 170 s: reconnected before writing
        self.assertTrue(first.closed)
        self.assertEqual(second.sent.count(b"POST /exchange/betting/json-rpc/v1"), 1)
        closing = support.ScriptedSocket([ok(None, Connection="close")])
        third = support.ScriptedSocket([ok()])
        http_transport, script = self.make(closing, third)
        self.read(http_transport)
        self.read(http_transport)
        self.assertEqual(len(script.calls), 2)          # server asked to close: next call reconnects
        self.assertTrue(closing.closed)

    def test_read_retry_once_on_remote_disconnect(self):
        stale = support.ScriptedSocket([b""])
        fresh = support.ScriptedSocket([ok({"jsonrpc": "2.0", "result": ["fresh"], "id": 1})])
        http_transport, script = self.make(stale, fresh)
        result = self.read(http_transport)
        self.assertEqual(json.loads(result["body"])["result"], ["fresh"])
        self.assertEqual(len(script.calls), 2)
        both_dead, script = self.make(support.ScriptedSocket([b""]), support.ScriptedSocket([b""]),
                                      support.ScriptedSocket([ok()]))
        error = self.assert_refused("connection_lost", lambda: self.read(both_dead), phase="receive")
        self.assertTrue(error.may_have_arrived)
        self.assertEqual(len(script.calls), 2)          # one retry, never a third attempt
        partial = support.ScriptedSocket([b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n{\"x\":"])
        http_transport, script = self.make(partial, support.ScriptedSocket([ok()]))
        self.assert_refused("incomplete_response", lambda: self.read(http_transport), phase="receive")
        self.assertEqual(len(script.calls), 1)          # response bytes arrived: no retry

    def test_order_call_not_resent_auto_open_zero(self):
        dropped = support.ScriptedSocket([b""])
        http_transport, script = self.make(dropped, support.ScriptedSocket([ok()]))
        error = self.assert_refused("connection_lost", lambda: self.order(http_transport), phase="receive")
        self.assertTrue(error.may_have_arrived)
        self.assertEqual(len(script.calls), 1)
        self.assertEqual(script.connections[0].auto_open, 0)
        self.assertEqual(dropped.sent.count(b"POST "), 1)
        self.assertEqual(http_transport.last_bytes_written, len(dropped.sent))

    def test_byte_counter_may_have_arrived(self):
        nothing = support.ScriptedSocket([ok()], send_error=BrokenPipeError("pipe"), send_error_after=0)
        http_transport, script = self.make(nothing)
        error = self.assert_refused("connection_lost", lambda: self.order(http_transport), phase="send")
        self.assertFalse(error.may_have_arrived)
        self.assertEqual(len(script.calls), 1)
        some = support.ScriptedSocket([ok()], send_error=ConnectionResetError("reset"), send_error_after=10)
        http_transport, script = self.make(some)
        error = self.assert_refused("connection_lost", lambda: self.order(http_transport), phase="send")
        self.assertTrue(error.may_have_arrived)
        self.assertEqual(http_transport.last_bytes_written, 10)
        refused_connect, script = self.make(connect_error=ConnectionRefusedError("refused"))
        error = self.assert_refused("connect_failed", lambda: self.order(refused_connect), phase="connect")
        self.assertFalse(error.may_have_arrived)

    def test_deadline_spans_dribbled_response(self):
        body = b"x" * 2000
        dribble = support.ScriptedSocket([support.http_bytes(200, body)], recv_chunk=1, clock=self.clock,
                                         advance_per_recv_s=0.01)
        http_transport, _ = self.make(dribble)
        error = self.assert_refused("deadline_exceeded", lambda: self.read(http_transport, deadline_s=1),
                                    phase="receive")
        self.assertTrue(error.may_have_arrived)
        self.assertTrue(all(0 < t <= 1.0 for t in dribble.timeouts))
        self.assertLess(dribble.timeouts[-1], dribble.timeouts[0])
        quick = support.ScriptedSocket([support.http_bytes(200, b"{}")], recv_chunk=1, clock=self.clock,
                                       advance_per_recv_s=0.001)
        http_transport, _ = self.make(quick)
        self.assertEqual(self.read(http_transport, deadline_s=1)["body"], b"{}")

    def test_no_expect_100_continue(self):
        sock = support.ScriptedSocket([ok()])
        http_transport, _ = self.make(sock)
        headers = dict(self.headers, **{"Accept-Encoding": "identity", "Connection": "close"})
        http_transport.request("POST", API, headers, rpc_body(), deadline_s=10, idempotent=True)
        sent = bytes(sock.sent).decode("latin-1")
        self.assertNotIn("expect", sent.lower())
        self.assertIn("Accept-Encoding: gzip\r\n", sent)
        self.assertIn("Connection: keep-alive\r\n", sent)
        self.assertNotIn("identity", sent)
        self.assertIn("X-Authentication: " + support.PLANTED_SECRET, sent)     # revealed only on the wire
        self.assertEqual(sent.count("Accept-Encoding"), 1)
        for bad in ({"Expect": "100-continue"}, {"X-Bad": "a\r\nInjected: 1"}, {"Bad Name": "x"}):
            with self.subTest(bad=bad):
                self.assert_refused("invalid_request", lambda: http_transport.request(
                    "POST", API, dict(self.headers, **bad), rpc_body(), deadline_s=10, idempotent=True))

    def test_tls_context_verification_on(self):
        context = transport.make_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        with patch.object(ssl.SSLContext, "load_cert_chain") as load:
            client = transport.make_tls_context("/tmp/client.crt", "/tmp/client.key", Secret("synthetic-passphrase"))
        load.assert_called_once_with("/tmp/client.crt", "/tmp/client.key", password="synthetic-passphrase")
        self.assertEqual(client.verify_mode, ssl.CERT_REQUIRED)
        with patch.object(ssl.SSLContext, "load_cert_chain", side_effect=ssl.SSLError("bad /secret/path")):
            with self.assertRaises(CredentialError) as caught:
                transport.make_tls_context("/secret/path.crt", "/secret/path.key")
        self.assertEqual(str(caught.exception), "client_certificate_unloadable")
        self.assertIsNone(caught.exception.__context__)
        with self.assertRaises(CredentialError):
            transport.make_tls_context("/tmp/client.crt", None)
        sock = support.ScriptedSocket([ok()])
        http_transport, script = self.make(sock)
        self.read(http_transport, url=CERTLOGIN, body=b"username=u&password=p", tls_context=client, idempotent=False)
        self.assertIs(script.calls[0]["context"], client)

    def test_forbid_network_env(self):
        script = support.ConnectionScript(support.ScriptedSocket([ok()]))
        forbidden = transport.HttpTransport(allowed_hosts=HOSTS, connection_factory=script,
                                            env=dict(support.FORBID_ENV))
        self.assert_refused("network_forbidden", lambda: self.read(forbidden))
        # Regression: the process environment forbids whatever env the transport was given, so a
        # harness that overrides state paths can never also reach the network.
        with patch.dict(os.environ, support.FORBID_ENV):
            for env in (None, {}, {"BETANGEL_BETFAIR_FORBID_NETWORK": "0"}):
                with self.subTest(env=env):
                    http_transport = transport.HttpTransport(allowed_hosts=HOSTS, connection_factory=script, env=env)
                    self.assert_refused("network_forbidden", lambda: self.read(http_transport))
            self.assert_refused("network_forbidden", lambda: transport.preflight(
                "POST", KEEPALIVE, {}, None, allowed_hosts=HOSTS, idempotent=True, env={}))
            for env in (None, {}):
                error = self.assert_refused("network_forbidden", lambda: transport.open_stream_socket(
                    "stream-api.betfair.com", 443, 15, env=env))
                self.assertFalse(error.may_have_arrived)
        self.assertEqual(script.calls, [])

    def test_host_and_path_allowlist(self):
        http_transport, script = self.make(support.ScriptedSocket([ok()]))
        bad_urls = [
            "http://api.betfair.com/exchange/betting/json-rpc/v1",
            "https://api.example.invalid/exchange/betting/json-rpc/v1",
            "https://api.betfair.com:8443/exchange/betting/json-rpc/v1",
            "https://api.betfair.com/exchange/heartbeat/json-rpc/v1",
            "https://api.betfair.com/exchange/betting/rest/v1.0/listMarketBook/",
            "https://api.betfair.com/exchange/betting/json-rpc/v1?x=" + support.PLANTED_SECRET,
            "https://user:pw@api.betfair.com/exchange/betting/json-rpc/v1",
            "https://api.betfair.com/exchange/betting/json-rpc/v1#frag",
            "https://identitysso.betfair.com/api/certlogin",                 # cert login only on the cert host
            "https://api.betfair.com/api/keepAlive",
            "https://identitysso.betfair.it/api/keepAlive",                  # not in this transport's allowlist
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                error = self.assert_refused("url_not_allowed", lambda: self.read(http_transport, url=url))
                self.assertNotIn(support.PLANTED_SECRET, str(error))
        self.assertEqual(script.calls, [])
        with self.assertRaisesRegex(ConfigError, "transport_hosts"):
            transport.HttpTransport(allowed_hosts={"api.example.invalid"}, env={})
        self.assert_refused("url_not_allowed", lambda: transport.open_stream_socket("stream.example.invalid", 443, 15,
                                                                                   env={}))
        self.assert_refused("url_not_allowed", lambda: transport.open_stream_socket("stream-api.betfair.com", 8443, 15,
                                                                                   env={}))

        class FakeContext:
            def __init__(self):
                self.wrapped = []

            def wrap_socket(self, raw, server_hostname):
                self.wrapped.append(server_hostname)
                return ("tls", raw)

        fake_context = FakeContext()
        with patch.object(transport, "_stream_tls_context", return_value=fake_context), \
                patch.object(transport.socket, "create_connection", return_value="raw-socket") as dial:
            wrapped = transport.open_stream_socket("stream-api.betfair.com", 443, 15, env={})
        dial.assert_called_once_with(("stream-api.betfair.com", 443), timeout=15)
        self.assertEqual(wrapped, ("tls", "raw-socket"))
        self.assertEqual(fake_context.wrapped, ["stream-api.betfair.com"])
        stream_context = transport._stream_tls_context()
        self.assertEqual(stream_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(stream_context.check_hostname)

    def test_redirect_refused(self):
        sock = support.ScriptedSocket([support.http_bytes(302, b"", {"Location": "https://example.invalid/"},
                                                          reason="Found")])
        http_transport, script = self.make(sock, support.ScriptedSocket([ok()]))
        error = self.assert_refused("redirect_refused", lambda: self.read(http_transport), phase="receive")
        self.assertTrue(error.may_have_arrived)
        self.assertEqual(len(script.calls), 1)

    def test_unbound_rpc_refused(self):
        http_transport, script = self.make(support.ScriptedSocket([ok()]))
        body = rpc_body(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        digest = hashlib.sha256(body).hexdigest()
        cases = [
            lambda: self.order(http_transport, body, order_digest=None),
            lambda: self.order(http_transport, body, order_digest="0" * 64),
            lambda: self.order(http_transport, body, order_digest=digest.upper()),
            lambda: http_transport.request("POST", API, dict(self.headers), body, deadline_s=10, idempotent=True,
                                           order_digest=digest),
            lambda: http_transport.request("POST", ACCOUNT, dict(self.headers), body, deadline_s=10, idempotent=False,
                                           order_digest=digest),
            lambda: self.order(http_transport, rpc_body("AccountAPING/v1.0/transferFunds", {}),
                               order_digest=hashlib.sha256(rpc_body("AccountAPING/v1.0/transferFunds", {})).hexdigest()),
            lambda: self.read(http_transport, rpc_body("SportsAPING/v1.0/listMarketProfitAndLoss")),
            lambda: self.read(http_transport, b"not json"),
            lambda: self.read(http_transport, b'{"jsonrpc":"2.0","params":{}}'),
            lambda: http_transport.request("POST", KEEPALIVE, dict(self.headers), None, deadline_s=10, idempotent=False,
                                           order_digest=digest),
        ]
        for index, call in enumerate(cases):
            with self.subTest(case=index):
                self.assert_refused("unbound_rpc_refused", call)
        self.assertEqual(script.calls, [])
        result = self.order(http_transport, body, order_digest=consumed_digest(body))
        self.assertEqual(result["status"], 200)
        self.assertEqual(len(script.calls), 1)

    def test_digest_alone_does_not_pass_the_gate(self):
        """Regression (audit): the gate accepted any body whose digest was its own sha256, so any caller
        could send placeOrders (PERSIST, any size) with no capability at all. Now the digest must have
        been recorded by a capability consumed in this process, and only once."""
        body = rpc_body(ORDER_METHOD, {"marketId": "1.1", "instructions": [
            {"orderType": "LIMIT", "selectionId": 1, "side": "LAY",
             "limitOrder": {"size": 50, "price": 1000, "persistenceType": "PERSIST"}}]})
        digest = hashlib.sha256(body).hexdigest()
        with self.assertRaises(TransportError) as caught:
            transport.preflight("POST", API, {}, body, allowed_hosts=HOSTS, idempotent=False, digest=digest,
                                check_env=False)
        self.assertEqual(caught.exception.code, "unbound_rpc_refused")
        self.assertEqual(consumed_digest(body), digest)
        self.assertEqual(transport.preflight("POST", API, {}, body, allowed_hosts=HOSTS, idempotent=False,
                                             digest=digest, check_env=False),
                         ("api.betfair.com", "/exchange/betting/json-rpc/v1"))
        with self.assertRaises(TransportError):                     # single use
            transport.preflight("POST", API, {}, body, allowed_hosts=HOSTS, idempotent=False, digest=digest,
                                check_env=False)
        # A refused request never uses up a recorded digest.
        consumed_digest(body)
        with self.assertRaises(TransportError):
            transport.preflight("POST", API, {}, body, allowed_hosts=HOSTS, idempotent=True, digest=digest,
                                check_env=False)
        self.assertTrue(capability.take_consumed(digest))
        self.assertFalse(capability.take_consumed("not-a-digest"))

    def test_unbound_rpc_refused_duplicate_method_and_loose_envelopes(self):
        # Regression: a last-key-wins parse classified {"method": <order>, ..., "method": <read>} as a
        # read and sent it with no digest (and retried it). Every second reading of a body is refused.
        http_transport, script = self.make(support.ScriptedSocket([ok()]))
        order, read = ORDER_METHOD.encode(), READ_METHOD.encode()
        order_then_read = b'{"jsonrpc":"2.0","method":"' + order + b'","params":{},"id":1,"method":"' + read + b'"}'
        read_then_order = b'{"jsonrpc":"2.0","method":"' + read + b'","params":{},"id":1,"method":"' + order + b'"}'
        escaped_duplicate = (b'{"jsonrpc":"2.0","method":"' + order + b'","params":{},"id":1,"meth\\u006fd":"'
                             + read + b'"}')
        envelope = b'{"jsonrpc":"2.0","method":"' + read + b'",'
        loose = [
            order_then_read, read_then_order, escaped_duplicate,
            envelope + b'"params":{"marketIds":["1.1"],"marketIds":["1.2"]},"id":1}',     # nested duplicate
            envelope + b'"params":{"x":1e400},"id":1}',                                     # overflows to inf
            envelope + b'"params":{"x":NaN},"id":1}',
            envelope + b'"params":{},"id":1,"extra":true}',
            envelope + b'"params":{}}',
            envelope + b'"params":[],"id":1}',
            envelope + b'"params":{},"id":true}',
            envelope + b'"params":{},"id":1.0}',
            b'{"jsonrpc":"1.0","method":"' + read + b'","params":{},"id":1}',
            b"\xef\xbb\xbf" + rpc_body(),                                                  # UTF-8 BOM
            envelope + b'"params":{"x":' + b"[" * 1_000_000 + b"]" * 1_000_000 + b'},"id":1}',   # too deep
        ]
        for index, body in enumerate(loose):
            with self.subTest(case=index):
                self.assertEqual(body.count(b'"jsonrpc"'), 1)
                self.assert_refused("unbound_rpc_refused", lambda: self.read(http_transport, body, idempotent=True))
                self.assert_refused("unbound_rpc_refused", lambda: self.order(
                    http_transport, body, order_digest=hashlib.sha256(body).hexdigest()))
                with self.assertRaises(TransportError) as caught:
                    transport.preflight("POST", API, {}, body, allowed_hosts=HOSTS, idempotent=True, env={})
                self.assertEqual(caught.exception.code, "unbound_rpc_refused")
        good = rpc_body(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        for digest in ("\u00e9" * 64, "AB" * 32, "0" * 63, b"ab" * 32):
            with self.subTest(digest=digest):
                self.assert_refused("unbound_rpc_refused", lambda: self.order(http_transport, good, order_digest=digest))
        self.assertEqual(script.calls, [])

    def test_login_never_idempotent(self):
        # Regression (spec section 2.10 rule 8): a login whose response is lost may still have counted;
        # it is never resent, so the persistent login ledger stays exact.
        stale, fresh = support.ScriptedSocket([b""]), support.ScriptedSocket([ok()])
        http_transport, script = self.make(stale, fresh)
        form = b"username=u&password=synthetic-password-0001"
        for url in (CERTLOGIN, LOGIN):
            with self.subTest(url=url):
                self.assert_refused("invalid_request", lambda: http_transport.request(
                    "POST", url, dict(self.headers), form, deadline_s=10, idempotent=True))
        self.assertEqual(script.calls, [])
        error = self.assert_refused("connection_lost", lambda: http_transport.request(
            "POST", CERTLOGIN, dict(self.headers), form, deadline_s=10, idempotent=False), phase="receive")
        self.assertTrue(error.may_have_arrived)
        self.assertEqual(len(script.calls), 1)
        self.assertEqual(stale.sent.count(b"synthetic-password-0001"), 1)
        self.assertEqual(fresh.sent, bytearray())
        # keepAlive stays retryable once, like a read.
        stale, fresh = support.ScriptedSocket([b""]), support.ScriptedSocket([ok({"status": "SUCCESS"})])
        http_transport, script = self.make(stale, fresh)
        result = http_transport.request("POST", KEEPALIVE, dict(self.headers), None, deadline_s=10, idempotent=True)
        self.assertEqual(json.loads(result["body"]), {"status": "SUCCESS"})
        self.assertEqual(len(script.calls), 2)

    def test_tls_context_unsafe_refused(self):
        # Regression (spec section 6): verification stays on; Betfair's -k sample is never reproduced.
        http_transport, script = self.make(support.ScriptedSocket([ok()]))
        no_hostname = transport.make_tls_context()
        no_hostname.check_hostname = False
        old_floor = transport.make_tls_context()
        old_floor.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        for context in (ssl._create_unverified_context(), no_hostname, old_floor, "not-a-context"):
            with self.subTest(context=context):
                self.assert_refused("tls_context_unsafe", lambda: http_transport.request(
                    "POST", CERTLOGIN, dict(self.headers), b"username=u&password=p", deadline_s=10,
                    idempotent=False, tls_context=context))
        self.assertEqual(script.calls, [])
        tls13_only = transport.make_tls_context()
        tls13_only.minimum_version = ssl.TLSVersion.TLSv1_3
        self.assertIs(transport.check_tls_context(tls13_only), tls13_only)
        fake = support.FakeHttpTransport([support.http_response({})])
        with self.assertRaises(TransportError) as caught:
            fake.request("POST", CERTLOGIN, {}, b"username=u&password=p", deadline_s=10, idempotent=False,
                         tls_context=ssl._create_unverified_context())
        self.assertEqual(caught.exception.code, "tls_context_unsafe")

    def test_sslkeylogfile_never_read_and_keylog_context_refused(self):
        # Regression (audit): ssl.create_default_context() honours SSLKEYLOGFILE, so OpenSSL wrote every
        # Betfair session secret to a world-readable file the moment a context was built.
        with tempfile.TemporaryDirectory() as temp:
            keylog = Path(temp) / "keylog-probe.txt"
            with patch.dict(os.environ, {"SSLKEYLOGFILE": str(keylog)}):
                contexts = [transport.make_tls_context(), transport._stream_tls_context()]
                with patch.object(ssl.SSLContext, "load_cert_chain"):
                    contexts.append(transport.make_tls_context("/tmp/c.crt", "/tmp/c.key", Secret("synthetic-pass")))
            for context in contexts:
                self.assertIsNone(getattr(context, "keylog_filename", None))
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                self.assertIs(transport.check_tls_context(context), context)
            self.assertFalse(keylog.exists())
            if hasattr(ssl.SSLContext, "keylog_filename"):
                logging = transport.make_tls_context()
                logging.keylog_filename = str(Path(temp) / "explicit-keylog.txt")
                with self.assertRaises(TransportError) as caught:
                    transport.check_tls_context(logging)
                self.assertEqual(caught.exception.code, "tls_context_unsafe")
                http_transport, script = self.make(support.ScriptedSocket([ok()]))
                self.assert_refused("tls_context_unsafe", lambda: http_transport.request(
                    "POST", CERTLOGIN, dict(self.headers), b"username=u&password=p", deadline_s=10,
                    idempotent=False, tls_context=logging))
                self.assertEqual(script.calls, [])

    def test_encrypted_key_without_passphrase_never_prompts(self):
        # Regression: with password=None OpenSSL prompts on the terminal for an encrypted key.
        with self.assertRaisesRegex(CredentialError, "key_passphrase_must_be_secret"):
            transport.make_tls_context("/tmp/c.crt", "/tmp/c.key", "plain-text-passphrase")
        with patch.object(ssl.SSLContext, "load_cert_chain") as load:
            transport.make_tls_context("/tmp/c.crt", "/tmp/c.key")
        password = load.call_args.kwargs["password"]
        self.assertTrue(callable(password))
        with self.assertRaises(Exception):
            password()

        def openssl_needs_passphrase(certfile, keyfile, password=None):
            if password is None:
                raise AssertionError("OpenSSL would prompt on the terminal here")
            if callable(password):
                password()

        with patch.object(ssl.SSLContext, "load_cert_chain", openssl_needs_passphrase):
            with self.assertRaisesRegex(CredentialError, "client_certificate_unloadable") as caught:
                transport.make_tls_context("/tmp/c.crt", "/tmp/c.key")
        self.assertIsNone(caught.exception.__context__)

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_real_encrypted_key_loads_only_with_secret_passphrase(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        made = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(base / "k.key"),
                               "-out", str(base / "c.crt"), "-days", "1", "-subj", "/CN=synthetic.invalid",
                               "-passout", "pass:synthetic-passphrase"], capture_output=True, timeout=60)
        if made.returncode != 0 or b"ENCRYPTED" not in (base / "k.key").read_bytes():
            self.skipTest("openssl could not create a synthetic encrypted key")
        context = transport.make_tls_context(base / "c.crt", base / "k.key", Secret("synthetic-passphrase"))
        self.assertIs(transport.check_tls_context(context), context)
        with self.assertRaisesRegex(CredentialError, "client_certificate_unloadable"):
            transport.make_tls_context(base / "c.crt", base / "k.key", Secret("wrong-passphrase"))

    def test_retry_connect_failure_after_write_may_have_arrived(self):
        sockets = [support.ScriptedSocket([b""])]

        def factory(host, *, context, timeout):
            if sockets:
                return support.ScriptedConnection(host, sockets.pop(0), context=context, timeout=timeout)
            return support.ScriptedConnection(host, None, context=context, timeout=timeout,
                                              connect_error=ConnectionRefusedError("refused"))

        http_transport = transport.HttpTransport(allowed_hosts=HOSTS, connection_factory=factory, clock=self.clock,
                                                 env={})
        self.addCleanup(http_transport.close)
        error = self.assert_refused("connect_failed", lambda: self.read(http_transport), phase="connect")
        self.assertTrue(error.may_have_arrived)              # the first attempt wrote the request
        self.assertGreater(http_transport.last_bytes_written, 0)
        error = self.assert_refused("connect_failed", lambda: self.order(http_transport), phase="connect")
        self.assertFalse(error.may_have_arrived)
        self.assertEqual(http_transport.last_bytes_written, 0)   # never a stale count from the last request

    def test_slow_connect_exceeds_deadline_nothing_sent(self):
        sock = support.ScriptedSocket([ok()])
        clock = self.clock

        class SlowConnection(support.ScriptedConnection):
            def connect(self):
                clock.advance(11)               # DNS + TCP + handshake together overran deadline_s=10
                super().connect()

        def factory(host, *, context, timeout):
            return SlowConnection(host, sock, context=context, timeout=timeout)

        http_transport = transport.HttpTransport(allowed_hosts=HOSTS, connection_factory=factory, clock=clock, env={})
        self.addCleanup(http_transport.close)
        error = self.assert_refused("deadline_exceeded", lambda: self.order(http_transport), phase="connect")
        self.assertFalse(error.may_have_arrived)
        self.assertEqual(bytes(sock.sent), b"")
        self.assertTrue(sock.closed)

    def slow_transport(self, sock, connect_s):
        clock = self.clock

        class SlowConnection(support.ScriptedConnection):
            def connect(self):
                clock.advance(connect_s)        # DNS + TCP + handshake, inside the 10 s HTTP budget
                super().connect()

        def factory(host, *, context, timeout):
            return SlowConnection(host, sock, context=context, timeout=timeout)

        http_transport = transport.HttpTransport(allowed_hosts=HOSTS, connection_factory=factory, clock=clock, env={})
        self.addCleanup(http_transport.close)
        return http_transport

    def test_send_by_expiry_after_connect_sends_nothing(self):
        """Amendment A3, C2 (Astra case b): a send-by that passes during a slow connect stops the request
        right before its first byte: phase send, definitely unsent, connection dropped."""
        sock = support.ScriptedSocket([ok()])
        http_transport = self.slow_transport(sock, 6)
        send_by = self.clock.monotonic_ns() + 5 * 10**9
        error = self.assert_refused("send_window_expired", lambda: self.order(
            http_transport, send_by_monotonic_ns=send_by, send_gate=lambda: None), phase="send")
        self.assertFalse(error.may_have_arrived)
        self.assertEqual(bytes(sock.sent), b"")
        self.assertEqual(http_transport.last_bytes_written, 0)
        self.assertTrue(sock.closed)
        # Within its bound the same request is written once and answered.
        sock = support.ScriptedSocket([ok()])
        http_transport = self.slow_transport(sock, 2)
        response = self.order(http_transport, send_by_monotonic_ns=self.clock.monotonic_ns() + 5 * 10**9,
                              send_gate=lambda: None)
        self.assertEqual(response["status"], 200)
        self.assertEqual(bytes(sock.sent).count(b"POST "), 1)

    def test_capability_expiry_stops_the_first_byte_through_send_order(self):
        """Astra C2 case (b) with Astra's numbers, through the production path: a place capability minted at
        monotonic 5 s expires at 10 s; JsonRpcClient.send_order consumes it before the connection exists, then
        the real transport spends 6 s on DNS + TCP + TLS inside the 10 s HTTP budget. Before the fix the request
        bytes went out at 11 s and HTTP 200 came back. Now send_order passes min(capability expiry, the caller's
        bound) down and the transport checks it right before the first byte: send_window_expired, phase send,
        zero bytes, may_have_arrived False, so the gateway journals FAILED_NOT_SENT. (send_order is the package's
        only order_digest= call site, test_betfair_boundaries, so no order body reaches the transport without
        passing through this rule.)"""
        config = support.make_test_config(SimpleNamespace(profile="default"))
        self.assertEqual(config["http"]["order_deadline_s"], 10)

        def client(sock, connect_s):
            http_transport = self.slow_transport(sock, connect_s)
            session = SimpleNamespace(app_key_slot="live", transport=http_transport,
                                      app_key=lambda: Secret("synthetic-live-key-0001"),
                                      current_token=lambda: Secret("synthetic-session-token-0001"))
            return api.JsonRpcClient(config, session, clock=self.clock), http_transport

        def minted(prepared):
            return capability._mint("decision-astra-c2b", prepared["method"], prepared["body_sha256"], "live",
                                    self.clock.monotonic_ns())

        gate_calls = []

        def gate():
            gate_calls.append(self.clock.monotonic_ns())

        sock = support.ScriptedSocket([ok()])
        rpc, http_transport = client(sock, 6)
        prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        self.assertEqual(self.clock.monotonic_ns(), 5 * 10**9)
        cap = minted(prepared)
        self.assertEqual(cap.expires_monotonic_ns, 10 * 10**9)
        later = self.clock.monotonic_ns() + 60 * 10**9         # the caller's (book) bound is later: expiry binds
        with self.assertRaises(TransportError) as caught:
            rpc.send_order(cap, prepared, send_by_monotonic_ns=later, send_gate=gate)
        error = caught.exception
        self.assertEqual((error.code, error.phase, error.may_have_arrived), ("send_window_expired", "send", False))
        self.assertEqual(outcome_from_exception(error), "FAILED_NOT_SENT")
        self.assertEqual(self.clock.monotonic_ns(), 11 * 10**9)
        self.assertTrue(cap.consumed)
        self.assertEqual((bytes(sock.sent), http_transport.last_bytes_written, gate_calls), (b"", 0, []))
        self.assertTrue(sock.closed)
        # The same order with a 4 s connect reaches its first byte before the expiry: written once, answered.
        report = {"status": "SUCCESS", "marketId": "1.1", "instructionReports": []}
        prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        sock = support.ScriptedSocket([ok({"jsonrpc": "2.0", "result": report, "id": prepared["rpc_id"]})])
        rpc, http_transport = client(sock, 4)
        cap = minted(prepared)
        self.assertEqual(rpc.send_order(cap, prepared, send_by_monotonic_ns=self.clock.monotonic_ns() + 60 * 10**9,
                                        send_gate=gate), report)
        self.assertEqual(bytes(sock.sent).count(b"POST "), 1)
        self.assertEqual(gate_calls, [cap.expires_monotonic_ns - 10**9])

    def test_send_gate_refusal_is_definitely_unsent(self):
        """The gate runs after the connect, immediately before the first byte; its code (or send_gate_failed
        when it raises) is a send-phase refusal with zero bytes written."""
        for name, gate, code in (("kill", lambda: "kill_engaged_before_send", "kill_engaged_before_send"),
                                 ("stale", lambda: "book_stale_before_send", "book_stale_before_send"),
                                 ("raises", lambda: 1 / 0, "send_gate_failed")):
            with self.subTest(case=name):
                sock = support.ScriptedSocket([ok()])
                calls = []

                def recording(gate=gate):
                    calls.append(self.clock.monotonic_ns())
                    return gate()

                http_transport = self.slow_transport(sock, 1)
                started = self.clock.monotonic_ns()
                error = self.assert_refused(code, lambda: self.order(
                    http_transport, send_by_monotonic_ns=started + 10 * 10**9, send_gate=recording), phase="send")
                self.assertFalse(error.may_have_arrived)
                self.assertEqual(calls, [started + 10**9])          # evaluated after the 1 s connect
                self.assertEqual(bytes(sock.sent), b"")
                self.assertTrue(sock.closed)

    def test_send_by_and_gate_refused_on_reads(self):
        http_transport, script = self.make(support.ScriptedSocket([ok()]))
        for bounds in ({"send_by_monotonic_ns": 10**12}, {"send_gate": lambda: None}):
            with self.subTest(bounds=sorted(bounds)):
                self.assert_refused("invalid_request", lambda: self.read(http_transport, **bounds))
        for bounds in ({"send_by_monotonic_ns": 1.5}, {"send_gate": "not callable"}):
            with self.subTest(bounds=sorted(bounds)):
                self.assert_refused("invalid_request", lambda: self.order(http_transport, **bounds))
        self.assertEqual(script.calls, [])

    def test_default_connection_shares_one_budget(self):
        events = []

        class FakeSock:
            def setsockopt(self, *args):
                pass

            def settimeout(self, value):
                events.append(("settimeout", value))

            def close(self):
                events.append(("close",))

        class FakeContext:
            # The attributes HTTPSConnection.__init__ reads on Python 3.10.
            verify_mode = ssl.CERT_REQUIRED
            check_hostname = True
            post_handshake_auth = None

            def wrap_socket(self, sock, server_hostname):
                events.append(("handshake", server_hostname))
                return "tls-socket"

        for ticks, expected in (([100.0, 103.0], [("settimeout", 7.0), ("handshake", "api.betfair.com")]),
                                ([100.0, 111.0], [("close",)])):
            events.clear()
            clock_values = list(ticks)
            with patch.object(transport.socket, "create_connection", return_value=FakeSock()) as dial, \
                    patch.object(transport.time, "monotonic", side_effect=lambda: clock_values.pop(0)):
                conn = transport._default_connection("api.betfair.com", context=FakeContext(), timeout=10)
                if expected[-1][0] == "close":
                    with self.assertRaises(socket.timeout):
                        conn.connect()
                else:
                    conn.connect()
                    self.assertEqual(conn.sock, "tls-socket")
            dial.assert_called_once_with(("api.betfair.com", 443), timeout=10)
            self.assertEqual(events, expected)

    def test_offline_guard_not_swallowed(self):
        # Regression: the guard used to surface as an ordinary connect_failed TransportError.
        http_transport = transport.HttpTransport(allowed_hosts=HOSTS, clock=self.clock, env={})
        self.addCleanup(http_transport.close)
        with self.assertRaises(support.OfflineViolation):
            self.read(http_transport)
        with self.assertRaises(support.OfflineViolation):
            transport.open_stream_socket("stream-api.betfair.com", 443, 15, env={})

    def test_fake_transport_mirrors_real_refusals(self):
        fake = support.FakeHttpTransport([support.http_response({})])
        self.assertEqual(fake.allowed_hosts, HOSTS)
        refusals = [
            ("url_not_allowed", dict(url="https://identitysso.betfair.it/api/keepAlive")),
            ("invalid_request", dict(url=CERTLOGIN, body=b"username=u&password=p")),
        ] + [("invalid_deadline", dict(deadline_s=bad)) for bad in (0, -1, True, 601, float("nan"), "10")]
        for code, changes in refusals:
            call = dict(dict(url=KEEPALIVE, body=None, deadline_s=10), **changes)
            with self.subTest(code=code, changes=str(changes)):
                with self.assertRaises(TransportError) as caught:
                    fake.request("POST", call["url"], {}, call["body"], deadline_s=call["deadline_s"],
                                 idempotent=True)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(fake.requests, [])
        italy = support.FakeHttpTransport([support.http_response({})], allowed_hosts={"identitysso.betfair.it"})
        self.assertEqual(italy.request("POST", "https://identitysso.betfair.it/api/keepAlive", {}, None,
                                       deadline_s=10, idempotent=True)["status"], 200)

    def test_fake_transport_failure_defaults_are_conservative(self):
        # Regression: a scripted receive/decode failure defaulted to "not sent" (may_have_arrived False).
        for phase, arrived in (("send", True), ("receive", True), ("decode", True), ("connect", False),
                               ("preflight", False)):
            with self.subTest(phase=phase):
                fake = support.FakeHttpTransport([support.transport_failure(phase, "connection_lost")])
                with self.assertRaises(TransportError) as caught:
                    fake.request("POST", API, {}, rpc_body(), deadline_s=10, idempotent=True)
                self.assertEqual((caught.exception.phase, caught.exception.may_have_arrived), (phase, arrived))
        fake = support.FakeHttpTransport([support.transport_failure("send", bytes_written=0)])
        with self.assertRaises(TransportError) as caught:
            fake.request("POST", API, {}, rpc_body(), deadline_s=10, idempotent=True)
        self.assertFalse(caught.exception.may_have_arrived)
        for phase, written in (("receive", 0), ("decode", 0), ("connect", 5), ("preflight", 1), ("bogus", None)):
            with self.subTest(refused=phase):
                with self.assertRaises(ValueError):
                    support.transport_failure(phase, bytes_written=written)

    def test_read_method_with_digest_refused(self):
        http_transport, script = self.make(support.ScriptedSocket([ok()]))
        body = rpc_body()
        self.assert_refused("unbound_rpc_refused", lambda: self.read(http_transport, body,
                                                                     order_digest=hashlib.sha256(body).hexdigest()))
        account_on_betting = rpc_body("AccountAPING/v1.0/getAccountFunds", {})
        self.assert_refused("unbound_rpc_refused", lambda: self.read(http_transport, account_on_betting))
        self.assertEqual(script.calls, [])
        self.read(http_transport, account_on_betting, url=ACCOUNT)

    def test_batch_body_refused(self):
        http_transport, script = self.make(support.ScriptedSocket([ok()]))
        batch = json.dumps([json.loads(rpc_body()), json.loads(rpc_body(rpc_id=2))]).encode()
        self.assert_refused("unbound_rpc_refused", lambda: self.read(http_transport, batch))
        self.assert_refused("unbound_rpc_refused", lambda: self.read(http_transport, b"[]"))
        self.assertEqual(script.calls, [])

    def test_headers_never_in_exception_text(self):
        failures = [
            self.make(connect_error=OSError("connect " + support.PLANTED_SECRET))[0],
            self.make(support.ScriptedSocket([ok()], send_error=OSError("send " + support.PLANTED_SECRET)))[0],
            self.make(support.ScriptedSocket([ok()], recv_error=ValueError("recv " + support.PLANTED_SECRET)))[0],
            self.make(support.ScriptedSocket([b"garbage " + support.PLANTED_SECRET.encode() + b"\r\n\r\n"]))[0],
        ]
        for http_transport in failures:
            with self.subTest(transport=http_transport):
                with self.assertRaises(TransportError) as caught:
                    self.order(http_transport)
                exc = caught.exception
                for text in (str(exc), repr(exc), repr(exc.args), exc.code):
                    self.assertNotIn(support.PLANTED_SECRET, text)
                    self.assertNotIn("X-Authentication", text)
                self.assertIsNone(exc.__context__)
                self.assertIsNone(exc.__cause__)

    def test_fake_transport_shares_preflight(self):
        fake = support.FakeHttpTransport([support.rpc_response([])])
        with self.assertRaises(TransportError):
            fake.request("POST", API, {}, rpc_body(ORDER_METHOD, {}), deadline_s=10, idempotent=False)
        self.assertEqual(fake.refused[0]["code"], "unbound_rpc_refused")
        result = fake.request("POST", API, {"X-Authentication": Secret(support.PLANTED_SECRET)}, rpc_body(),
                              deadline_s=10, idempotent=True)
        self.assertEqual(json.loads(result["body"])["result"], [])
        self.assertEqual(fake.json_requests()[0]["method"], "SportsAPING/v1.0/listMarketBook")
        self.assertEqual(fake.header_text(fake.requests[0], "X-Authentication"), support.PLANTED_SECRET)
        fake.queue(support.transport_failure("send", bytes_written=5))
        with self.assertRaises(TransportError) as caught:
            fake.request("POST", API, {}, rpc_body(), deadline_s=10, idempotent=True)
        self.assertTrue(caught.exception.may_have_arrived)


class CapabilityTests(unittest.TestCase):
    DIGEST = "ab" * 32

    def mint(self, **changes):
        args = {"decision_id": "decision-1", "method": ORDER_METHOD, "body_sha256": self.DIGEST, "mode": "live",
                "now_monotonic_ns": 1_000}
        args.update(changes)
        return capability._mint(**args)

    def test_mint_consume_once(self):
        cap = self.mint()
        self.assertEqual(cap.expires_monotonic_ns, 1_000 + capability.CAPABILITY_TTL_NS)
        self.assertEqual(cap.consume(method=ORDER_METHOD, body_sha256=self.DIGEST, now_monotonic_ns=2_000), self.DIGEST)
        self.assertTrue(cap.consumed)
        with self.assertRaisesRegex(OrderRefused, "capability_reused"):
            cap.consume(method=ORDER_METHOD, body_sha256=self.DIGEST, now_monotonic_ns=2_000)

    def test_mismatch_expiry_and_mode(self):
        with self.assertRaisesRegex(OrderRefused, "capability_mismatch"):
            self.mint().consume(method=ORDER_METHOD, body_sha256="cd" * 32, now_monotonic_ns=2_000)
        burned = self.mint()
        with self.assertRaises(OrderRefused):
            burned.consume(method="SportsAPING/v1.0/other", body_sha256=self.DIGEST, now_monotonic_ns=2_000)
        with self.assertRaisesRegex(OrderRefused, "capability_reused"):
            burned.consume(method=ORDER_METHOD, body_sha256=self.DIGEST, now_monotonic_ns=2_000)
        with self.assertRaisesRegex(OrderRefused, "capability_expired"):
            self.mint().consume(method=ORDER_METHOD, body_sha256=self.DIGEST,
                                now_monotonic_ns=1_000 + capability.CAPABILITY_TTL_NS)
        with self.assertRaisesRegex(OrderRefused, "capability_mode"):
            self.mint(mode="paper")
        for bad in ({"method": "SportsAPING/v1.0/listMarketBook"}, {"body_sha256": "xyz"}, {"decision_id": ""},
                    {"ttl_ns": capability.CAPABILITY_TTL_NS + 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(OrderRefused):
                    self.mint(**bad)

    def test_malformed_digest_is_refusal_not_type_error(self):
        # Regression: compare_digest raised TypeError on non-ASCII text instead of OrderRefused.
        for digest in ("\u00e9" * 64, "AB" * 32, b"ab" * 32, None, 7, "ab" * 31):
            with self.subTest(digest=digest):
                cap = self.mint()
                with self.assertRaisesRegex(OrderRefused, "capability_mismatch"):
                    cap.consume(method=ORDER_METHOD, body_sha256=digest, now_monotonic_ns=2_000)
                self.assertTrue(cap.consumed)

    def test_not_constructible_copyable_or_picklable(self):
        with self.assertRaises(TypeError):
            capability.OrderCapability("d", ORDER_METHOD, self.DIGEST, "live", 10)
        with self.assertRaises(TypeError):
            capability.OrderCapability("d", ORDER_METHOD, self.DIGEST, "live", 10, _token=object())
        cap = self.mint()
        for attempt in (lambda: copy.copy(cap), lambda: copy.deepcopy(cap), lambda: pickle.dumps(cap)):
            with self.assertRaises(TypeError):
                attempt()
        with self.assertRaises(AttributeError):
            cap.expires_monotonic_ns = 10 ** 18
        self.assertNotIn(self.DIGEST, repr(cap))


if __name__ == "__main__":
    unittest.main()
