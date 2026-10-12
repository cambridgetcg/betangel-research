"""JsonRpcClient, ReadClient, WeightPlanner and response-parsing tests over FakeHttpTransport; synthetic data only."""

import hashlib
import json
import os
import ssl
import traceback
import unittest
from unittest.mock import patch

from betangel_research.betfair import api, auth, capability, constants, errors
from betangel_research.betfair import config as cfg
from betangel_research.betfair.errors import ApiError, OrderRefused, SecretLeak, TransportError
from betangel_research.betfair.lifecycle import outcome_from_exception
from betangel_research.betfair.schemas import validate_book_snapshot
from betangel_research.betfair.secret import Secret

import test_betfair_support as support

BETTING = "https://api.betfair.com/exchange/betting/json-rpc/v1"
ACCOUNT = "https://api.betfair.com/exchange/account/json-rpc/v1"
CERTLOGIN = "https://identitysso-cert.betfair.com/api/certlogin"
# Built by concatenation so no order-method literal appears even in test source.
ORDER_METHOD = "SportsAPING/v1.0/" + "place" + "Orders"
OTHER_ORDER_METHOD = "SportsAPING/v1.0/" + "cancel" + "Orders"
REQUEST_UUID = "prdang001-11060807-000a67cb6c"


def echo(result):
    """A scripted response whose JSON-RPC id echoes the request's id."""
    def respond(record):
        return support.rpc_response(result, json.loads(record["body"])["id"])
    return respond


def aping_error(code, *, exception="APINGException", details=support.PLANTED_SECRET, status=200, rpc_id=1):
    data = {"exceptionname": exception, exception: {"errorDetails": details, "errorCode": code,
                                                    "requestUUID": REQUEST_UUID}}
    return support.rpc_error_response(-32099, "ANGX-0003", data, rpc_id=rpc_id, status=status)


def params(record):
    return json.loads(record["body"])["params"]


def book_row(market_id="1.1", status="OPEN", **extra):
    row = {"marketId": market_id, "isMarketDataDelayed": True, "status": status, "betDelay": 0, "inplay": False,
           "numberOfWinners": 1, "version": 4249587130, "runners": [{
               "selectionId": 12345, "handicap": 0.0, "status": "ACTIVE", "lastPriceTraded": 3.5,
               "ex": {"availableToBack": [{"price": 3.45, "size": 40.0}, {"price": 3.5, "size": 120.0}],
                      "availableToLay": [{"price": 3.55, "size": 80.0}],
                      "tradedVolume": [{"price": 3.5, "size": 1520.4}]}}]}
    row.update(extra)
    return row


def catalogue_row(market_id="1.1"):
    return {"marketId": market_id, "marketName": "Synthetic 1m Hcap", "totalMatched": 0.0,
            "marketStartTime": "2020-01-01T12:30:00.000Z",
            "description": {"bettingType": "ODDS", "marketType": "WIN", "marketTime": "2020-01-01T12:30:00.000Z",
                            "persistenceEnabled": True, "priceLadderDescription": {"type": "CLASSIC"}},
            "eventType": {"id": "7", "name": "Horse Racing"},
            "runners": [{"selectionId": 12345, "runnerName": "Synthetic Runner", "handicap": 0.0, "sortPriority": 1}]}


class FakeSession:
    """The parts of SessionManager a JsonRpcClient uses; relogins hand out a new synthetic token."""

    def __init__(self, transport, slot="delayed", logged_in=True):
        self.transport = transport
        self._slot = slot
        self._key = Secret("synthetic-live-key-0001" if slot == "live" else "synthetic-delayed-key-0001")
        self._token = Secret("synthetic-session-token-api-0") if logged_in else None
        self.relogins = 0

    @property
    def app_key_slot(self):
        return self._slot

    def app_key(self):
        return self._key

    def token(self):
        if self._token is None:
            raise AssertionError("lazy login not expected in this test")
        return self._token

    def current_token(self):
        return self._token

    def invalidate_and_relogin(self):
        self.relogins += 1
        self._token = Secret(f"synthetic-session-token-api-{self.relogins}")
        return {"status": "SUCCESS", "method": "cert", "can_bet": True, "logged_in_at": support.BASE_UTC}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.clock = support.FakeClock()
        self.paths = support.temp_state_paths(self)
        self.config = cfg.load_config(support.write_test_config(self.paths), paths=self.paths)

    def client(self, responses=(), *, slot="delayed", logged_in=True):
        transport = support.FakeHttpTransport(responses, clock=self.clock)
        session = FakeSession(transport, slot, logged_in)
        rpc = api.JsonRpcClient(self.config, session, clock=self.clock)
        read = api.ReadClient(rpc, paths=self.paths, clock=self.clock,
                              catalogue_cache_max_age_s=self.config["read"]["catalogue_cache_max_age_s"])
        return rpc, read, transport, session

    def mint(self, prepared, *, method=None, digest=None, mode="live"):
        return capability._mint("decision-0001", method or prepared["method"], digest or prepared["body_sha256"], mode,
                                self.clock.monotonic_ns())

    def send(self, rpc, cap, prepared):
        """send_order as the gateway calls it: a place body carries a send-by and a send gate (amendment A3, C2)."""
        bounds = {}
        if isinstance(prepared, dict) and prepared.get("method") == ORDER_METHOD:
            bounds = {"send_by_monotonic_ns": self.clock.monotonic_ns() + 60 * 10**9, "send_gate": lambda: None}
        return rpc.send_order(cap, prepared, **bounds)


class JsonRpcClientTests(ApiTestCase):
    def test_jsonrpc_envelope_headers_and_prefix(self):
        rpc, _, transport, _ = self.client([echo([]), echo({"availableToBetBalance": 1.0})])
        self.assertEqual(rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}}), [])
        rpc.call("AccountAPING/v1.0/getAccountFunds", {})
        first, second = transport.requests
        self.assertEqual((first["method"], first["url"], first["idempotent"], first["deadline_s"]),
                         ("POST", BETTING, True, 10))
        self.assertIsNone(first["order_digest"])
        self.assertEqual(first["body"], b'{"id":1,"jsonrpc":"2.0","method":"SportsAPING/v1.0/listEventTypes",'
                                        b'"params":{"filter":{}}}')
        self.assertEqual(transport.header_text(first, "X-Application"), "synthetic-delayed-key-0001")
        self.assertEqual(transport.header_text(first, "X-Authentication"), "synthetic-session-token-api-0")
        self.assertEqual((first["headers"]["Accept"], first["headers"]["Content-Type"]),
                         ("application/json", "application/json"))
        self.assertEqual(second["url"], ACCOUNT)
        self.assertEqual(json.loads(second["body"]), {"jsonrpc": "2.0", "method": "AccountAPING/v1.0/getAccountFunds",
                                                      "params": {}, "id": 2})
        self.assertEqual(rpc.app_key_slot, "delayed")
        with self.assertRaises(AttributeError):
            rpc.app_key_slot = "live"

    def test_call_refuses_non_read_method(self):
        rpc, _, transport, _ = self.client(slot="live")
        for method in (ORDER_METHOD, OTHER_ORDER_METHOD, "SportsAPING/v1.0/listMarketProfitAndLoss",
                       "AccountAPING/v1.0/getAccountStatement", "HeartbeatAPING/v1.0/" + "keepAlive", None):
            with self.subTest(method=method):
                with self.assertRaises(ValueError):
                    rpc.call(method, {})
        with self.assertRaises(ValueError):
            rpc.call("SportsAPING/v1.0/listEventTypes", [])
        self.assertEqual(transport.requests, [])

    def test_prepare_order_requires_live_slot(self):
        delayed, _, delayed_transport, _ = self.client()
        with self.assertRaises(OrderRefused) as caught:
            delayed.prepare_order(ORDER_METHOD, {"marketId": "1.1"})
        self.assertEqual(caught.exception.rule, "order_requires_live_app_key")
        rpc, _, transport, _ = self.client(slot="live")
        prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        self.assertEqual(set(prepared), {"method", "body", "body_sha256", "rpc_id"})
        self.assertEqual(prepared["body"], json.dumps({"id": prepared["rpc_id"], "jsonrpc": "2.0", "method": ORDER_METHOD,
                                                       "params": {"instructions": [], "marketId": "1.1"}},
                                                      sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(prepared["body_sha256"], hashlib.sha256(prepared["body"]).hexdigest())
        refusals = [("SportsAPING/v1.0/listMarketBook", {}, "order_method_invalid"),
                    ("AccountAPING/v1.0/transferFunds", {}, "order_method_invalid"),
                    ("SportsAPING/v1.0/../x", {}, "order_method_invalid"),
                    (ORDER_METHOD, [], "order_params_invalid"),
                    (ORDER_METHOD, {"size": float("nan")}, "order_params_invalid"),
                    (ORDER_METHOD, {"marketId": object()}, "order_params_invalid")]
        for method, order_params, rule in refusals:
            with self.subTest(rule=rule, method=method):
                with self.assertRaises(OrderRefused) as caught:
                    rpc.prepare_order(method, order_params)
                self.assertEqual(caught.exception.rule, rule)
        Secret(support.PLANTED_SECRET)          # registered, as a live session token would be
        for leaky in ({"sessionToken": "x"}, {"customerRef": support.PLANTED_SECRET},
                      {"marketId": Secret("synthetic-secret-object")}, {"nested": [{"X-Authentication": "x"}]}):
            with self.subTest(leaky=sorted(leaky)):
                with self.assertRaises(SecretLeak):
                    rpc.prepare_order(ORDER_METHOD, leaky)
        self.assertEqual(transport.requests + delayed_transport.requests, [])

    def test_send_order_consumes_capability_and_passes_digest(self):
        rpc, _, transport, session = self.client(slot="live")
        prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        cap = self.mint(prepared)
        report = {"status": "SUCCESS", "marketId": "1.1", "instructionReports": []}
        transport.queue(support.rpc_response(report, prepared["rpc_id"]))
        self.assertEqual(self.send(rpc, cap, prepared), report)
        self.assertTrue(cap.consumed)
        request = transport.requests[0]
        self.assertEqual((request["url"], request["idempotent"], request["deadline_s"]), (BETTING, False, 10))
        self.assertEqual(request["body"], prepared["body"])
        self.assertEqual(request["order_digest"], hashlib.sha256(request["body"]).hexdigest())
        self.assertEqual(transport.header_text(request, "X-Application"), "synthetic-live-key-0001")
        self.assertEqual(transport.refused, [])
        with self.assertRaises(OrderRefused) as caught:
            self.send(rpc, cap, prepared)
        self.assertEqual(caught.exception.rule, "capability_reused")
        # A session error on an order is final: no relogin, no resend.
        second = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        transport.queue(aping_error("INVALID_SESSION_INFORMATION", rpc_id=second["rpc_id"]))
        with self.assertRaises(ApiError) as caught:
            self.send(rpc, self.mint(second), second)
        self.assertEqual(caught.exception.category, "session")
        self.assertEqual(session.relogins, 0)
        # A transport failure is final too, and says whether the order may have arrived.
        third = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        transport.queue(support.transport_failure("receive"), echo({}))
        with self.assertRaises(TransportError) as caught:
            self.send(rpc, self.mint(third), third)
        self.assertTrue(caught.exception.may_have_arrived)
        self.assertEqual(len(transport.requests), 3)
        # An echoed id other than ours is never read as a success.
        fourth = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        transport.responses.clear()
        transport.queue(support.rpc_response(report, fourth["rpc_id"] + 100))
        with self.assertRaises(ApiError) as caught:
            self.send(rpc, self.mint(fourth), fourth)
        self.assertEqual((caught.exception.code, caught.exception.category), ("rpc_id_mismatch", "unrecognized"))

    def test_order_response_must_be_attributable(self):
        """An order response that cannot be tied to our request id, or carries both result and error,
        is an unknown outcome (unrecognized), never a success and never a definite rejection."""
        rpc, _, transport, _ = self.client(slot="live")
        matched = {"status": "SUCCESS", "instructionReports": [{"betId": "123", "sizeMatched": 2.0}]}
        session_error = {"code": -32099, "message": "ANGX-0003", "data": {
            "exceptionname": "APINGException", "APINGException": {"errorCode": "INVALID_SESSION_INFORMATION"}}}
        cases = [("missing id", lambda rid: {"jsonrpc": "2.0", "result": matched}, "rpc_id_mismatch"),
                 ("null id", lambda rid: {"jsonrpc": "2.0", "result": matched, "id": None}, "rpc_id_mismatch"),
                 ("string id", lambda rid: {"jsonrpc": "2.0", "result": matched, "id": str(rid)}, "rpc_id_mismatch"),
                 ("bool id", lambda rid: {"jsonrpc": "2.0", "result": matched, "id": True}, "rpc_id_mismatch"),
                 ("foreign id on an error", lambda rid: {"jsonrpc": "2.0", "error": session_error, "id": rid + 999},
                  "rpc_id_mismatch"),
                 ("null id on an error", lambda rid: {"jsonrpc": "2.0", "error": {"code": -32700, "message": "x"},
                                                      "id": None}, "rpc_id_mismatch"),
                 ("result and error", lambda rid: {"jsonrpc": "2.0", "result": matched, "id": rid,
                                                   "error": {"code": -32602, "message": "x"}},
                  "ambiguous_rpc_envelope")]
        for name, body, code in cases:
            with self.subTest(case=name):
                prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
                transport.queue(support.http_response(body(prepared["rpc_id"])))
                with self.assertRaises(ApiError) as caught:
                    self.send(rpc, self.mint(prepared), prepared)
                self.assertEqual((caught.exception.surface, caught.exception.code, caught.exception.category),
                                 ("local", code, "unrecognized"))
        # With our id, an error is classified as usual and a result is returned.
        prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        transport.queue(support.http_response({"jsonrpc": "2.0", "error": session_error, "id": prepared["rpc_id"]}))
        with self.assertRaises(ApiError) as caught:
            self.send(rpc, self.mint(prepared), prepared)
        self.assertEqual((caught.exception.code, caught.exception.category), ("INVALID_SESSION_INFORMATION", "session"))
        prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        transport.queue(support.http_response(body=b"<fault>DSC-0018</fault>", status=400))
        with self.assertRaises(ApiError) as caught:
            self.send(rpc, self.mint(prepared), prepared)
        self.assertEqual((caught.exception.surface, caught.exception.code), ("dsc", "DSC-0018"),
                         "a body that is not a JSON-RPC envelope keeps its spec section 7 classification")
        prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        transport.queue(support.rpc_response(matched, prepared["rpc_id"]))
        self.assertEqual(self.send(rpc, self.mint(prepared), prepared), matched)
        # Reads keep spec section 7's precedence: no id check, and an error wins over a result.
        rpc, _, _, _ = self.client([support.http_response({"jsonrpc": "2.0", "result": ["ok"]})])
        self.assertEqual(rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}}), ["ok"])

    def test_capability_mismatch_reuse_expiry_mode(self):
        rpc, _, transport, session = self.client(slot="live")

        def prepared():
            return rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})

        order = prepared()
        cases = [("capability_mismatch", lambda p: self.mint(p, digest="0" * 64), prepared()),
                 ("capability_mismatch", lambda p: self.mint(p, method=OTHER_ORDER_METHOD), prepared())]
        for rule, make, prep in cases:
            with self.subTest(rule=rule):
                cap = make(prep)
                with self.assertRaises(OrderRefused) as caught:
                    self.send(rpc, cap, prep)
                self.assertEqual(caught.exception.rule, rule)
                self.assertTrue(cap.consumed)
        expiring = prepared()
        cap = self.mint(expiring)
        self.clock.advance(5)
        with self.assertRaises(OrderRefused) as caught:
            self.send(rpc, cap, expiring)
        self.assertEqual(caught.exception.rule, "capability_expired")
        paper = prepared()
        paper_cap = capability.OrderCapability("decision-0002", ORDER_METHOD, paper["body_sha256"], "paper",
                                               self.clock.monotonic_ns() + 10 ** 9, _token=capability._MINT_TOKEN)
        with self.assertRaises(OrderRefused) as caught:
            self.send(rpc, paper_cap, paper)
        self.assertEqual(caught.exception.rule, "capability_mode")

        class Forged:
            consumed = False

            def consume(self, **kw):
                return kw["body_sha256"]

        with self.assertRaises(OrderRefused) as caught:
            self.send(rpc, Forged(), prepared())
        self.assertEqual(caught.exception.rule, "capability_invalid")
        tampered = prepared()
        cap = self.mint(tampered)
        tampered["body"] = tampered["body"].replace(b'"instructions":[]', b'"instructions":[{}]')
        with self.assertRaises(OrderRefused) as caught:
            self.send(rpc, cap, tampered)
        self.assertEqual(caught.exception.rule, "prepared_order_inconsistent")
        self.assertTrue(cap.consumed, "a refused send burns the capability")
        no_session = prepared()
        cap = self.mint(no_session)
        session._token = None
        with self.assertRaises(OrderRefused) as caught:
            self.send(rpc, cap, no_session)
        self.assertEqual(caught.exception.rule, "order_requires_logged_in_session")
        self.assertTrue(cap.consumed)
        delayed, _, delayed_transport, _ = self.client()
        cap = self.mint(order)
        with self.assertRaises(OrderRefused) as caught:
            self.send(delayed, cap, order)
        self.assertEqual(caught.exception.rule, "order_requires_live_app_key")
        self.assertTrue(cap.consumed)
        self.assertEqual(transport.requests + delayed_transport.requests, [])

    def test_send_order_passes_capability_expiry_as_send_by(self):
        """Amendment A3, C2: a place body goes to the transport with send-by = min(capability expiry, the
        caller's bound) and the caller's gate, both checked right before the first request byte."""
        rpc, _, transport, _ = self.client(slot="live")
        report = {"status": "SUCCESS", "marketId": "1.1", "instructionReports": []}
        gates = []

        def gate():
            gates.append(self.clock.monotonic_ns())
            return None

        first = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        cap = self.mint(first)
        transport.queue(support.rpc_response(report, first["rpc_id"]))
        rpc.send_order(cap, first, send_by_monotonic_ns=self.clock.monotonic_ns() + 60 * 10**9, send_gate=gate)
        self.assertEqual(transport.requests[0]["send_by_monotonic_ns"], cap.expires_monotonic_ns)
        self.assertEqual(len(gates), 1)
        second = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        near = self.clock.monotonic_ns() + 10**9            # the caller's (book) bound is earlier: it wins
        transport.queue(support.rpc_response(report, second["rpc_id"]))
        rpc.send_order(self.mint(second), second, send_by_monotonic_ns=near, send_gate=gate)
        self.assertEqual(transport.requests[1]["send_by_monotonic_ns"], near)
        # A gate refusal is definitely unsent: no request recorded, zero bytes, FAILED_NOT_SENT for the gateway.
        third = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        cap = self.mint(third)
        with self.assertRaises(TransportError) as caught:
            rpc.send_order(cap, third, send_by_monotonic_ns=near, send_gate=lambda: "kill_engaged_before_send")
        self.assertEqual((caught.exception.code, caught.exception.phase, caught.exception.may_have_arrived),
                         ("kill_engaged_before_send", "send", False))
        self.assertTrue(cap.consumed)
        self.assertEqual(outcome_from_exception(caught.exception), "FAILED_NOT_SENT")
        # The send-by has passed by the first byte (a slow connect): send_window_expired, nothing sent.
        fourth = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        transport.connect_delay_s = 2
        with self.assertRaises(TransportError) as caught:
            rpc.send_order(self.mint(fourth), fourth, send_by_monotonic_ns=self.clock.monotonic_ns() + 10**9,
                           send_gate=gate)
        self.assertEqual((caught.exception.code, caught.exception.may_have_arrived), ("send_window_expired", False))
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(transport.bytes_written[-2:], [0, 0])

    def test_place_body_without_send_bound_is_refused(self):
        """A place body without both bounds is refused before the capability is consumed, burning it."""
        rpc, _, transport, _ = self.client(slot="live")
        later = self.clock.monotonic_ns() + 60 * 10**9
        for name, bounds in (("none", {}), ("send-by only", {"send_by_monotonic_ns": later}),
                             ("gate only", {"send_gate": lambda: None}),
                             ("float send-by", {"send_by_monotonic_ns": float(later), "send_gate": lambda: None}),
                             ("gate not callable", {"send_by_monotonic_ns": later, "send_gate": "no"})):
            with self.subTest(case=name):
                prepared = rpc.prepare_order(ORDER_METHOD, {"marketId": "1.1", "instructions": []})
                cap = self.mint(prepared)
                with self.assertRaises(OrderRefused) as caught:
                    rpc.send_order(cap, prepared, **bounds)
                self.assertEqual(caught.exception.rule, "capability_send_bound_required")
                self.assertTrue(cap.consumed)
                self.assertEqual(outcome_from_exception(caught.exception), "FAILED_NOT_SENT")
        self.assertEqual(transport.requests, [])

    def test_cancel_body_with_send_bound_is_refused(self):
        """Cancels are never bounded or gated (a late cancel of our own bet adds no risk): either bound on a
        cancel body is refused before anything is sent; without them the cancel goes out."""
        rpc, _, transport, _ = self.client(slot="live")
        for name, bounds in (("send-by", {"send_by_monotonic_ns": self.clock.monotonic_ns() + 10**9}),
                             ("gate", {"send_gate": lambda: None})):
            with self.subTest(case=name):
                prepared = rpc.prepare_order(OTHER_ORDER_METHOD, {"marketId": "1.1", "instructions": []})
                cap = self.mint(prepared)
                with self.assertRaises(OrderRefused) as caught:
                    rpc.send_order(cap, prepared, **bounds)
                self.assertEqual(caught.exception.rule, "send_bound_not_for_this_method")
                self.assertTrue(cap.consumed)
        self.assertEqual(transport.requests, [])
        prepared = rpc.prepare_order(OTHER_ORDER_METHOD, {"marketId": "1.1", "instructions": []})
        report = {"status": "SUCCESS", "marketId": "1.1", "instructionReports": []}
        transport.queue(support.rpc_response(report, prepared["rpc_id"]))
        transport.connect_delay_s = 8                     # a slow reconnect never stops a cancel
        self.assertEqual(rpc.send_order(self.mint(prepared), prepared), report)
        self.assertEqual((transport.requests[0]["send_by_monotonic_ns"], transport.requests[0]["send_gate"]),
                         (None, False))

    def test_read_retry_and_no_retry_categories(self):
        backoff = [1, 4]
        rpc, _, transport, session = self.client([aping_error("TOO_MANY_REQUESTS"), aping_error("SERVICE_BUSY"),
                                                  echo(["ok"])])
        self.assertEqual(rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}}), ["ok"])
        self.assertEqual((len(transport.requests), self.clock.sleeps), (3, backoff))
        rpc, _, transport, _ = self.client([aping_error("TOO_MANY_REQUESTS")] * 3 + [echo(["late"])])
        self.clock.sleeps.clear()
        with self.assertRaises(ApiError) as caught:
            rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}})
        self.assertEqual((caught.exception.category, len(transport.requests), self.clock.sleeps),
                         ("rate_limited", 3, backoff))
        rpc, _, transport, _ = self.client([support.transport_failure("receive"),
                                            support.http_response(body=b"<html>busy</html>", status=503),
                                            echo(["ok"])])
        self.clock.sleeps.clear()
        self.assertEqual(rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}}), ["ok"])
        self.assertEqual(self.clock.sleeps, backoff)
        rpc, _, transport, session = self.client([aping_error("INVALID_SESSION_INFORMATION"), echo(["ok"])])
        self.clock.sleeps.clear()
        self.assertEqual(rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}}), ["ok"])
        self.assertEqual((session.relogins, self.clock.sleeps), (1, []))
        self.assertEqual(transport.header_text(transport.requests[1], "X-Authentication"),
                         "synthetic-session-token-api-1")
        rpc, _, transport, session = self.client([aping_error("NO_SESSION", exception="AccountAPINGException")] * 3)
        with self.assertRaises(ApiError) as caught:
            rpc.call("AccountAPING/v1.0/getAccountFunds", {})
        self.assertEqual((caught.exception.category, session.relogins, len(transport.requests)), ("session", 1, 2))
        no_retry = [(aping_error("TOO_MUCH_DATA"), "planner_bug"), (aping_error("ACCESS_DENIED"), "terminal_account"),
                    (aping_error("INVALID_APP_KEY"), "app_key"), (aping_error("INVALID_INPUT_DATA"), "planner_bug"),
                    (support.rpc_error_response(-32602, "bad params"), "planner_bug"),
                    (support.rpc_error_response(-32099, "ANGX-0099"), "unrecognized"),
                    (support.http_response(body=b"<fault>DSC-0034</fault>", status=400), "terminal_account"),
                    (support.http_response({"jsonrpc": "2.0", "id": 1}), "unrecognized")]
        for response, category in no_retry:
            with self.subTest(category=category):
                rpc, _, transport, _ = self.client([response, echo(["never"])])
                with self.assertRaises(ApiError) as caught:
                    rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}})
                self.assertEqual((caught.exception.category, len(transport.requests)), (category, 1))
        rpc, _, transport, _ = self.client([support.transport_failure("preflight"), echo(["never"])])
        with self.assertRaises(TransportError):
            rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}})
        self.assertEqual(len(transport.requests), 1)
        # Transport failures a retry cannot change are not retried.
        for code in ("redirect_refused", "response_too_large", "unsupported_content_encoding",
                     "tls_verification_failed", "tls_context_unsafe", "invalid_deadline"):
            with self.subTest(code=code):
                rpc, _, transport, _ = self.client([support.transport_failure("receive", code=code), echo(["never"])])
                self.clock.sleeps.clear()
                with self.assertRaises(TransportError) as caught:
                    rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}})
                self.assertEqual((caught.exception.code, len(transport.requests), self.clock.sleeps), (code, 1, []))

    def test_accounts_errors_classify_like_betting_errors(self):
        """Regression (audit): AccountAPINGException's transient and input codes were "unrecognized", so one
        SERVICE_BUSY on getAccountFunds was never retried and refused the live order."""
        for code, category in (("TOO_MANY_REQUESTS", "rate_limited"), ("SERVICE_BUSY", "unknown_outcome"),
                               ("TIMEOUT_ERROR", "unknown_outcome"), ("UNEXPECTED_ERROR", "unknown_outcome"),
                               ("INVALID_INPUT_DATA", "planner_bug")):
            with self.subTest(code=code):
                self.assertEqual(errors.classify(code, surface="account_aping"), category)
                self.assertEqual(errors.classify(code, surface="aping"), category)
        funds = {"availableToBetBalance": 10.0, "exposure": 0.0, "retainedCommission": 0.0, "exposureLimit": -100.0,
                 "discountRate": 0.0, "pointsBalance": 0, "wallet": "UK"}
        _, read, transport, _ = self.client([aping_error("SERVICE_BUSY", exception="AccountAPINGException"),
                                             aping_error("TOO_MANY_REQUESTS", exception="AccountAPINGException"),
                                             echo(funds)])
        self.clock.sleeps.clear()
        self.assertEqual(read.get_account_funds()["availableToBetBalance"], 10.0)
        self.assertEqual((len(transport.requests), self.clock.sleeps), (3, [1, 4]))
        _, read, transport, _ = self.client([aping_error("INVALID_INPUT_DATA", exception="AccountAPINGException"),
                                             echo(funds)])
        with self.assertRaises(ApiError) as caught:
            read.get_account_funds()
        self.assertEqual((caught.exception.category, len(transport.requests)), ("planner_bug", 1))

    def test_book_throttle_four_per_second_per_market(self):
        rpc, read, transport, _ = self.client([echo([book_row()])] * 4 + [echo([book_row("1.2")])]
                                              + [echo([book_row()])] * 2)
        for _ in range(4):
            read.list_market_book(["1.1"])
        self.assertEqual(self.clock.sleeps, [])
        read.list_market_book(["1.2"])
        self.assertEqual(self.clock.sleeps, [], "another market has its own window")
        read.list_market_book(["1.1"])
        self.assertEqual(self.clock.sleeps, [1.0])
        self.clock.advance(0.5)
        read.list_market_book(["1.1"])
        self.assertEqual(self.clock.sleeps, [1.0], "the window slides: earlier calls aged out")
        self.assertEqual(len(transport.requests), 7)

    def test_order_projection_inflight_two(self):
        rpc, read, transport, _ = self.client()
        observed = []
        no_orders = {"currentOrders": [], "moreAvailable": False}

        def inner(record):
            observed.append(dict(rpc._inflight))
            with self.assertRaises(ApiError) as caught:
                read.list_market_book(["1.1"], order_projection="EXECUTABLE")   # would be the third in flight
            observed.append(caught.exception.code)
            transport.queue(echo([book_row()]), echo({"clearedOrders": [], "moreAvailable": False}))
            read.list_market_book(["1.1"])          # no order projection: not limited
            read.list_cleared_orders("SETTLED", settled_from_utc=support.BASE_UTC)    # limited separately
            return support.rpc_response(no_orders, json.loads(record["body"])["id"])

        def outer(record):
            observed.append(dict(rpc._inflight))
            transport.queue(inner)
            read.list_current_orders()
            return support.rpc_response(no_orders, json.loads(record["body"])["id"])

        transport.queue(outer)
        read.list_current_orders()
        self.assertEqual(observed, [{"order_projection": 1, "cleared": 0}, {"order_projection": 2, "cleared": 0},
                                    "inflight_limit_reached"])
        self.assertEqual(len(transport.requests), 4, "the refused third call never reached the transport")
        self.assertEqual(rpc._inflight, {"order_projection": 0, "cleared": 0})

    def test_match_projection_book_counts_as_order_projection(self):
        rpc, read, transport, _ = self.client()
        observed = []

        def inner(record):
            observed.append(dict(rpc._inflight))
            with self.assertRaises(ApiError) as caught:
                read.list_market_book(["1.1"], match_projection="ROLLED_UP_BY_PRICE")
            observed.append(caught.exception.code)
            return support.rpc_response([book_row()], json.loads(record["body"])["id"])

        def outer(record):
            observed.append(dict(rpc._inflight))
            transport.queue(inner)
            read.list_market_book(["1.1"], match_projection="NO_ROLLUP")
            return support.rpc_response([book_row()], json.loads(record["body"])["id"])

        transport.queue(outer)
        read.list_market_book(["1.1"], match_projection="ROLLED_UP_BY_AVG_PRICE")
        self.assertEqual(observed, [{"order_projection": 1, "cleared": 0}, {"order_projection": 2, "cleared": 0},
                                    "inflight_limit_reached"])
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(params(transport.requests[0])["matchProjection"], "ROLLED_UP_BY_AVG_PRICE")
        self.assertEqual(rpc._inflight, {"order_projection": 0, "cleared": 0})

    def test_errordetails_never_kept(self):
        rpc, _, transport, _ = self.client([aping_error("INVALID_INPUT_DATA")])
        with self.assertRaises(ApiError) as caught:
            rpc.call("SportsAPING/v1.0/listMarketBook", {"marketIds": ["1.1"]})
        exc = caught.exception
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) + repr(exc) + repr(vars(exc))
        self.assertNotIn(support.PLANTED_SECRET, text)
        self.assertNotIn("errorDetails", text)
        self.assertEqual(exc.request_uuid, REQUEST_UUID)
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)


class ParseResponseTests(unittest.TestCase):
    def raised(self, status, body, surface="aping"):
        with self.assertRaises(ApiError) as caught:
            api.parse_rpc_response(status, body, surface=surface)
        return caught.exception

    def test_apingexception_from_exceptionname(self):
        exc = self.raised(400, aping_error("INVALID_APP_KEY")["body"])
        self.assertEqual((exc.surface, exc.code, exc.category, exc.request_uuid),
                         ("aping", "INVALID_APP_KEY", "app_key", REQUEST_UUID))
        exc = self.raised(200, aping_error("NO_SESSION", exception="AccountAPINGException")["body"])
        self.assertEqual((exc.surface, exc.category), ("account_aping", "session"))
        nested = support.json_body({"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32099, "message": "ANGX-0001", "data": {"wrapper": [{"inner": {"errorCode": "TOO_MUCH_DATA"}}]}}})
        exc = self.raised(200, nested)
        self.assertEqual((exc.surface, exc.code, exc.category), ("aping", "TOO_MUCH_DATA", "planner_bug"))
        both = support.json_body({"jsonrpc": "2.0", "id": 1, "result": [1], "error": {"code": -32603, "message": "x"}})
        self.assertEqual(self.raised(200, both).category, "unknown_outcome", "error wins over result")
        self.assertEqual(api.parse_rpc_response(500, support.json_body({"jsonrpc": "2.0", "result": [], "id": 1}),
                                                surface="aping"), [])

    def test_angx_minus_32099_unrecognized(self):
        exc = self.raised(200, support.rpc_error_response(-32099, "ANGX-0006")["body"])
        self.assertEqual((exc.surface, exc.code, exc.category), ("angx", "ANGX-0006", "unrecognized"))
        exc = self.raised(200, support.rpc_error_response(-32099, "free text " + support.PLANTED_SECRET)["body"])
        self.assertEqual((exc.surface, exc.code), ("angx", "malformed_code"))
        self.assertNotIn(support.PLANTED_SECRET, str(exc))
        for code, category in ((-32700, "planner_bug"), (-32601, "planner_bug"), (-32602, "planner_bug"),
                               (-32603, "unknown_outcome"), (-1, "unrecognized")):
            with self.subTest(code=code):
                exc = self.raised(200, support.rpc_error_response(code, "m")["body"])
                self.assertEqual((exc.surface, exc.code, exc.category), ("jsonrpc", str(code), category))

    def test_non_json_dsc_fallback(self):
        cases = [(400, b"<faultstring>DSC-0018</faultstring>", ("dsc", "DSC-0018", "planner_bug")),
                 (504, b"DSC-0019 Timeout", ("dsc", "DSC-0019", "unknown_outcome")),
                 (502, b"<html>Bad gateway</html>", ("http", "HTTP_502", "unknown_outcome")),
                 (200, support.json_body({"jsonrpc": "2.0", "id": 1}), ("http", "HTTP_200", "unrecognized")),
                 (200, b'{"jsonrpc": "2.0", "result": [NaN], "id": 1}', ("http", "HTTP_200", "unrecognized")),
                 (200, b'{"result": [], "result": [1]}', ("http", "HTTP_200", "unrecognized")),
                 (403, support.json_body({"faultcode": "Client", "faultstring": "DSC-0037"}),
                  ("dsc", "DSC-0037", "terminal_account"))]
        for status, body, expected in cases:
            with self.subTest(body=body):
                exc = self.raised(status, body)
                self.assertEqual((exc.surface, exc.code, exc.category), expected)
                self.assertIsNone(exc.__context__)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = api.WeightPlanner()
        self.ids = [f"1.{n}" for n in range(1, 301)]

    def size(self, price_data, depth=None):
        return len(self.planner.chunk(self.ids, self.planner.book_weight(price_data, depth))[0])

    def test_planner_chunks_40_10_6_12_100(self):
        self.assertEqual(self.size({"EX_BEST_OFFERS"}), 40)
        self.assertEqual(self.size({"EX_BEST_OFFERS", "EX_TRADED"}), 10)
        self.assertEqual(self.size({"EX_ALL_OFFERS", "EX_TRADED"}), 6)
        self.assertEqual(self.size({"EX_BEST_OFFERS"}, depth=10), 12)
        self.assertEqual(self.size(set()), 100)
        self.assertEqual(self.size({"SP_AVAILABLE", "SP_TRADED", "EX_BEST_OFFERS", "EX_TRADED"}), 6)
        chunks = self.planner.chunk(self.ids[:100], self.planner.book_weight({"EX_BEST_OFFERS"}, None))
        self.assertEqual([len(c) for c in chunks], [40, 40, 20])
        self.assertEqual(sum(chunks, []), self.ids[:100])

    def test_planner_depth_below_3_conservative(self):
        for depth in (1, 2, 3):
            self.assertEqual(self.planner.book_weight({"EX_BEST_OFFERS"}, depth), 5.0)
        self.assertEqual(self.planner.book_weight({"EX_BEST_OFFERS"}, 6), 10.0)
        self.assertEqual(self.planner.book_weight({"EX_BEST_OFFERS", "EX_TRADED"}, 6), 40.0, "the total is scaled")
        for depth in (0, 11, True, 3.0):
            with self.subTest(depth=depth):
                with self.assertRaises(ValueError):
                    self.planner.book_weight({"EX_BEST_OFFERS"}, depth)

    def test_planner_all_offers_overrides_best(self):
        self.assertEqual(self.planner.book_weight({"EX_BEST_OFFERS", "EX_ALL_OFFERS"}, None), 17.0)
        self.assertEqual(self.planner.book_weight({"EX_BEST_OFFERS", "EX_ALL_OFFERS", "EX_TRADED"}, None), 32.0)
        with self.assertRaises(ValueError):
            self.planner.book_weight({"EX_BEST_OFFERS_DISP"}, None)

    def test_planner_zero_weight_no_div(self):
        self.assertEqual(self.planner.catalogue_weight({"EVENT_TYPE", "MARKET_START_TIME", "RUNNER_DESCRIPTION"}), 0)
        self.assertEqual(self.planner.catalogue_weight({"MARKET_DESCRIPTION", "RUNNER_METADATA", "EVENT"}), 2)
        self.assertEqual([len(c) for c in self.planner.chunk(self.ids, 0)], [250, 50])
        self.assertEqual([len(c) for c in self.planner.chunk(self.ids, 0, max_ids=100)], [100, 100, 100])
        self.assertEqual(self.planner.chunk([], 5.0), [])
        for weight in (201, -1, float("inf"), True):
            with self.subTest(weight=weight):
                with self.assertRaises(ValueError):
                    self.planner.chunk(self.ids, weight)
        with self.assertRaises(ValueError):
            self.planner.chunk(self.ids, 0, max_ids=251)


class ReadClientTests(ApiTestCase):
    def test_catalogue_max_results_capped(self):
        _, read, transport, _ = self.client([echo([])] * 3)
        read.list_market_catalogue({"eventTypeIds": ["7"]}, max_results=200)
        with self.assertRaises(ValueError):
            read.list_market_catalogue({"eventTypeIds": ["7"]}, max_results=201)
        read.list_market_catalogue({"eventTypeIds": ["7"]}, projections=("MARKET_DESCRIPTION", "RUNNER_METADATA"),
                                   max_results=100)
        with self.assertRaises(ValueError):
            read.list_market_catalogue({"eventTypeIds": ["7"]}, projections=("MARKET_DESCRIPTION", "RUNNER_METADATA"),
                                       max_results=101)
        read.list_market_catalogue({"eventTypeIds": ["7"]}, projections=("EVENT",), max_results=1000)
        for bad in (1001, 0, True, 5.0):
            with self.assertRaises(ValueError):
                read.list_market_catalogue({"eventTypeIds": ["7"]}, projections=("EVENT",), max_results=bad)
        self.assertEqual([params(r)["maxResults"] for r in transport.requests], [200, 100, 1000])

    def test_sort_rank_never_sent(self):
        _, read, transport, _ = self.client([echo([]), echo([]), echo([{"marketId": "1.2"}, {"marketId": "1.1"}])])
        with self.assertRaises(ValueError):
            read.list_market_catalogue({"eventTypeIds": ["7"]}, sort="RANK")
        self.assertEqual(transport.requests, [])
        read.list_market_catalogue({"eventTypeIds": ["7"]})
        read.list_market_catalogue({"eventTypeIds": ["7"]}, sort="FIRST_TO_START")
        self.assertEqual(read.count_markets({"eventTypeIds": ["7"], "marketCountries": ["GB"]}), ["1.2", "1.1"])
        sent = [params(r) for r in transport.requests]
        self.assertNotIn("sort", sent[0])
        self.assertEqual(sent[1]["sort"], "FIRST_TO_START")
        self.assertEqual(sent[2], {"filter": {"eventTypeIds": ["7"], "marketCountries": ["GB"]},
                                   "marketProjection": ["MARKET_START_TIME"], "maxResults": 1000,
                                   "sort": "FIRST_TO_START"})
        self.assertNotIn(b"RANK", b"".join(r["body"] for r in transport.requests))

    def test_list_event_types_sends_empty_filter(self):
        _, read, transport, _ = self.client([echo([{"eventType": {"id": "7"}, "marketCount": 3}]), echo([])])
        self.assertEqual(read.list_event_types(), [{"eventType": {"id": "7"}, "marketCount": 3}])
        read.list_event_types({"marketCountries": ["GB"]})
        self.assertEqual([params(r) for r in transport.requests], [{"filter": {}}, {"filter": {"marketCountries": ["GB"]}}])
        with self.assertRaises(ValueError):
            read.list_event_types({"countryCodes": ["GB"]})     # a stream key never goes to REST

    def test_catalogue_none_filter_rejected(self):
        _, read, transport, _ = self.client()
        with self.assertRaises(ValueError):
            read.list_market_catalogue(None)
        with self.assertRaises(ValueError):
            read.list_market_catalogue({"marketTypes": ["WIN"]})
        self.assertEqual(transport.requests, [])

    def test_missing_ids_rerequested_singly(self):
        _, read, transport, _ = self.client([echo([book_row("1.1")]), echo([book_row("1.2", status="CLOSED")]),
                                             echo([])])
        rows = read.list_market_book(["1.1", "1.2", "1.3"], price_data=("EX_BEST_OFFERS", "EX_TRADED"), depth=3)
        self.assertEqual([(r["marketId"], r["status"]) for r in rows], [("1.1", "OPEN"), ("1.2", "CLOSED")])
        self.assertEqual([params(r)["marketIds"] for r in transport.requests], [["1.1", "1.2", "1.3"], ["1.2"], ["1.3"]])
        self.assertEqual(params(transport.requests[0])["priceProjection"],
                         {"priceData": ["EX_BEST_OFFERS", "EX_TRADED"], "exBestOffersOverrides": {"bestPricesDepth": 3}})
        _, read, _, _ = self.client([echo([book_row("1.9")])])
        with self.assertRaises(ApiError):
            read.list_market_book(["1.1"])
        _, read, transport, _ = self.client([echo([])])
        read.list_market_book(["1.1"], price_data=("EX_ALL_OFFERS", "EX_BEST_OFFERS"))
        self.assertEqual(params(transport.requests[0])["priceProjection"], {"priceData": ["EX_ALL_OFFERS"]})
        with self.assertRaises(ValueError):
            read.list_market_book(["1.1"], price_data=("EX_ALL_OFFERS",), depth=5)
        with self.assertRaises(ValueError):
            read.list_market_book(["1.1", "1.1"])
        # A response that repeats a market id is refused, not silently merged.
        _, read, transport, _ = self.client([echo([book_row("1.1"), book_row("1.1")])])
        with self.assertRaises(ApiError) as caught:
            read.list_market_book(["1.1", "1.2"])
        self.assertEqual(caught.exception.code, "unrequested_or_duplicate_market_in_response")
        self.assertEqual(len(transport.requests), 1)

    def test_market_snapshot_joins_catalogue_and_caches(self):
        _, read, transport, _ = self.client([echo([catalogue_row()]), echo([book_row()]), echo([book_row()]),
                                             echo([catalogue_row()]), echo([book_row()])])
        snapshot = read.market_snapshot("1.1", fetched_by="read_command")
        validate_book_snapshot(snapshot)
        self.assertEqual({k: snapshot[k] for k in ("market_id", "source", "fetched_by", "key_type", "app_key_slot",
                                                   "observed_at", "monotonic_ns", "pt", "catalogue_present",
                                                   "market_time", "number_of_winners", "betting_type", "market_type",
                                                   "event_type_id", "price_ladder_type", "invalid_reason",
                                                   "is_market_data_delayed", "version")},
                         {"market_id": "1.1", "source": "rest_listMarketBook", "fetched_by": "read_command",
                          "key_type": "delayed", "app_key_slot": "delayed", "observed_at": support.BASE_UTC,
                          "monotonic_ns": 5_000_000_000, "pt": None, "catalogue_present": True,
                          "market_time": "2020-01-01T12:30:00.000Z", "number_of_winners": 1, "betting_type": "ODDS",
                          "market_type": "WIN", "event_type_id": "7", "price_ladder_type": "CLASSIC",
                          "invalid_reason": None, "is_market_data_delayed": True, "version": 4249587130})
        self.assertEqual(snapshot["runners"], {"12345": {"status": "ACTIVE", "handicap": 0.0, "ltp": 3.5,
                                                         "back": [[3.5, 120.0], [3.45, 40.0]], "lay": [[3.55, 80.0]],
                                                         "traded": {"3.5": 1520.4}, "total_matched": None}})
        catalogue_request = params(transport.requests[0])
        self.assertEqual(catalogue_request, {"filter": {"marketIds": ["1.1"]}, "maxResults": 1, "marketProjection":
                                             ["EVENT_TYPE", "MARKET_DESCRIPTION", "MARKET_START_TIME",
                                              "RUNNER_DESCRIPTION"]})
        self.assertEqual(params(transport.requests[1])["priceProjection"], {"priceData": ["EX_BEST_OFFERS", "EX_TRADED"]})
        cache = self.paths.catalogue_cache_dir / "1.1.json"
        self.assertEqual(oct(os.stat(cache).st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(cache.read_text())["row"]["marketId"], "1.1")
        read.market_snapshot("1.1", fetched_by="read_command")
        self.assertEqual(len(transport.requests), 3, "a fresh cached row is not fetched again")
        self.clock.advance(21601)
        read.market_snapshot("1.1", fetched_by="read_command")
        self.assertEqual([json.loads(r["body"])["method"].rsplit("/", 1)[1] for r in transport.requests],
                         ["listMarketCatalogue", "listMarketBook", "listMarketBook", "listMarketCatalogue",
                          "listMarketBook"])
        live, live_read, _, _ = self.client([echo([book_row(isMarketDataDelayed=False)])], slot="live")
        live_snapshot = live_read.market_snapshot("1.1", fetched_by="gateway_self_fetch")
        self.assertEqual((live_snapshot["key_type"], live_snapshot["app_key_slot"], live_snapshot["fetched_by"],
                          live_snapshot["is_market_data_delayed"]), ("live", "live", "gateway_self_fetch", False))
        with self.assertRaises(ValueError):
            live_read.market_snapshot("1.1", fetched_by="fixture")

    def test_market_snapshot_age_taken_before_book_request(self):
        def slow_book(record):
            self.clock.advance(2)       # the book request takes 2 s
            return support.rpc_response([book_row()], json.loads(record["body"])["id"])

        _, read, transport, _ = self.client([echo([catalogue_row()]), slow_book])
        before_ns = self.clock.monotonic_ns()
        before_utc = self.clock.utc_now()
        snapshot = read.market_snapshot("1.1", fetched_by="read_command")
        self.assertEqual((snapshot["monotonic_ns"], snapshot["observed_at"]), (before_ns, before_utc),
                         "the snapshot's age is never understated")
        self.assertEqual(self.clock.monotonic_ns(), before_ns + 2_000_000_000)

    def test_catalogue_cache_dated_in_future_is_refetched(self):
        _, read, transport, _ = self.client([echo([catalogue_row()])])
        self.paths.ensure()
        cache = self.paths.catalogue_cache_dir / "1.1.json"
        stale = dict(catalogue_row(), marketName="Stale row")
        cache.write_text(json.dumps({"schema_version": 1, "market_id": "1.1", "cached_at": "2020-01-01T13:00:00Z",
                                     "row": stale}))
        row = read.catalogue_row("1.1")
        self.assertEqual(row["marketName"], "Synthetic 1m Hcap")
        self.assertEqual(len(transport.requests), 1, "a cached_at in the future is not fresh")
        self.assertEqual(json.loads(cache.read_text())["cached_at"], support.BASE_UTC)

    def test_catalogue_missing_sets_invalid_reason(self):
        _, read, transport, _ = self.client([echo([]), echo([book_row()])])
        snapshot = read.market_snapshot("1.1", fetched_by="read_command")
        self.assertEqual((snapshot["catalogue_present"], snapshot["invalid_reason"]), (False, "catalogue_missing"))
        for key in ("market_time", "betting_type", "market_type", "event_type_id", "price_ladder_type"):
            self.assertIsNone(snapshot[key])
        self.assertFalse((self.paths.catalogue_cache_dir / "1.1.json").exists())
        # A CLOSED market leaves the catalogue: a stale cached row is kept for settlement.
        _, read, _, _ = self.client([echo([catalogue_row("1.2")]), echo([book_row("1.2")]), echo([]),
                                     echo([book_row("1.2", status="CLOSED")])])
        read.market_snapshot("1.2", fetched_by="read_command")
        self.clock.advance(30000)
        closed = read.market_snapshot("1.2", fetched_by="read_command")
        self.assertEqual((closed["status"], closed["catalogue_present"], closed["market_type"]), ("CLOSED", True, "WIN"))
        # Non-finite sizes become null and mark the snapshot invalid.
        row = book_row()
        row["runners"][0]["ex"]["availableToLay"] = [{"price": 3.55, "size": float("inf")}]
        snapshot = api.normalise_market_book(row, catalogue_item=catalogue_row(), key_type="delayed",
                                             app_key_slot="delayed", observed_at=support.BASE_UTC, monotonic_ns=1,
                                             fetched_by="read_command")
        self.assertEqual((snapshot["invalid_reason"], snapshot["runners"]["12345"]["lay"]),
                         ("non_finite_size", [[3.55, None]]))
        with self.assertRaises(ValueError):
            api.normalise_market_book(book_row("1.1"), catalogue_item=catalogue_row("1.2"), key_type="delayed",
                                      app_key_slot="delayed", observed_at=support.BASE_UTC, monotonic_ns=1,
                                      fetched_by="read_command")

    def test_iter_current_orders_follows_more_available(self):
        orders = [{"betId": str(310000000000 + n), "marketId": "1.1", "customerOrderRef": f"bro-{n}"} for n in range(3)]
        page_one = {"currentOrders": orders[:2], "moreAvailable": True}
        page_two = {"currentOrders": orders[2:] + [{"betId": "1", "sourceIdKey": "synthetic-source"}],
                    "moreAvailable": False}
        _, read, transport, _ = self.client([echo(page_one), echo(page_two)])
        found = list(read.iter_current_orders(market_ids=["1.1"], order_projection="EXECUTABLE"))
        self.assertEqual([o["betId"] for o in found], [o["betId"] for o in orders] + ["1"])
        self.assertNotIn("sourceIdKey", found[-1], "stripped unless include_source_id")
        self.assertEqual([(params(r)["fromRecord"], params(r)["recordCount"]) for r in transport.requests],
                         [(0, 1000), (2, 1000)])
        self.assertEqual(params(transport.requests[0])["orderProjection"], "EXECUTABLE")
        _, read, _, _ = self.client([echo(page_two)])
        kept = read.list_current_orders(include_source_id=True)
        self.assertEqual(kept["currentOrders"][-1]["sourceIdKey"], "synthetic-source")
        _, read, _, _ = self.client([echo({"currentOrders": [], "moreAvailable": True})])
        with self.assertRaises(ApiError):
            list(read.iter_current_orders())
        # moreAvailable is required: a page without it is never taken as the last one.
        for bad in ({"currentOrders": [{"betId": "1"}]}, {"currentOrders": [{"betId": "1"}], "moreAvailable": None},
                    {"currentOrders": [{"betId": "1"}], "moreAvailable": "false"}):
            with self.subTest(page=sorted(bad)):
                _, read, transport, _ = self.client([echo(bad), echo({"currentOrders": [{"betId": "2"}],
                                                                      "moreAvailable": False})])
                with self.assertRaises(ApiError) as caught:
                    list(read.iter_current_orders(market_ids=["1.1"]))
                self.assertEqual(caught.exception.code, "unexpected_result_shape")
                self.assertEqual(len(transport.requests), 1)
        _, read, _, _ = self.client([echo({"clearedOrders": []})])
        with self.assertRaises(ApiError):
            list(read.iter_cleared_orders("SETTLED", settled_from_utc=support.BASE_UTC))

    def test_cleared_requires_from_group_by_only_settled(self):
        empty = {"clearedOrders": [], "moreAvailable": False}
        _, read, transport, _ = self.client([echo(empty)] * 4)
        with self.assertRaises(ValueError):
            read.list_cleared_orders("SETTLED", settled_from_utc=None)
        with self.assertRaises(TypeError):
            read.list_cleared_orders("SETTLED")
        with self.assertRaises(ValueError):
            read.list_cleared_orders("LAPSED", settled_from_utc=support.BASE_UTC, group_by="MARKET")
        with self.assertRaises(ValueError):
            read.list_cleared_orders("SETTLED", settled_from_utc="2020-01-01T12:00:00")     # naive
        read.list_cleared_orders("SETTLED", settled_from_utc=support.BASE_UTC)
        read.list_cleared_orders("SETTLED", settled_from_utc=support.BASE_UTC, group_by="MARKET",
                                 customer_strategy_refs=["bar-manual"])
        read.list_cleared_orders("LAPSED", settled_from_utc=support.BASE_UTC, customer_order_refs=["bro-1"])
        list(read.iter_cleared_orders("CANCELLED", settled_from_utc=support.BASE_UTC,
                                      settled_to_utc="2020-01-02T00:00:00Z"))
        sent = [params(r) for r in transport.requests]
        # sourceIdKey is stripped unless include_source_id.
        page = {"clearedOrders": [{"betId": "1", "sourceIdKey": "synthetic-source"}], "moreAvailable": False}
        _, cleared_read, _, _ = self.client([echo(page), echo(page)])
        self.assertEqual(cleared_read.list_cleared_orders("SETTLED", settled_from_utc=support.BASE_UTC),
                         {"clearedOrders": [{"betId": "1"}], "moreAvailable": False})
        kept = cleared_read.list_cleared_orders("SETTLED", settled_from_utc=support.BASE_UTC, include_source_id=True)
        self.assertEqual(kept["clearedOrders"][0]["sourceIdKey"], "synthetic-source")
        self.assertEqual(sent[0], {"betStatus": "SETTLED", "settledDateRange": {"from": "2020-01-01T12:00:00.123Z"},
                                   "groupBy": "BET", "fromRecord": 0, "recordCount": 1000})
        self.assertEqual((sent[1]["groupBy"], sent[1]["customerStrategyRefs"]), ("MARKET", ["bar-manual"]))
        self.assertNotIn("groupBy", sent[2])
        self.assertNotIn("groupBy", sent[3])
        self.assertEqual(sent[3]["settledDateRange"], {"from": "2020-01-01T12:00:00.123Z",
                                                       "to": "2020-01-02T00:00:00.000Z"})
        # Regression (audit): EXCHANGE is named only in the commission field's description, not in the
        # GroupBy enum, so it is refused before anything is sent.
        _, refusing, transport, _ = self.client([echo(empty)])
        with self.assertRaises(ValueError):
            refusing.list_cleared_orders("SETTLED", settled_from_utc=support.BASE_UTC, group_by="EXCHANGE")
        self.assertEqual(transport.requests, [])
        self.assertEqual(constants.GROUP_BY_VALUES, ("EVENT_TYPE", "EVENT", "MARKET", "SIDE", "BET"))

    def test_date_range_not_placed_date_range(self):
        _, read, transport, _ = self.client([echo({"currentOrders": [], "moreAvailable": False})])
        read.list_current_orders(customer_strategy_refs=["bar-manual"], date_range={"from": support.BASE_UTC},
                                 include_source_id=True)
        sent = params(transport.requests[0])
        self.assertEqual(sent, {"customerStrategyRefs": ["bar-manual"], "orderProjection": "ALL",
                                "dateRange": {"from": "2020-01-01T12:00:00.123Z"}, "orderBy": "BY_PLACE_TIME",
                                "sortDir": "EARLIEST_TO_LATEST", "includeSourceId": True, "fromRecord": 0,
                                "recordCount": 1000})
        self.assertNotIn(b"placedDateRange", transport.requests[0]["body"])
        bad = [{"order_by": "BY_BET"}, {"date_range": {"from": "2020-01-02T00:00:00Z", "to": support.BASE_UTC}},
               {"date_range": {"since": support.BASE_UTC}}, {"bet_ids": ["1"] * 2},
               {"bet_ids": [str(n) for n in range(200)], "market_ids": [f"1.{n}" for n in range(51)]},
               {"customer_order_refs": ["bad ref"]}, {"record_count": 1001}, {"from_record": -1},
               {"order_projection": "OPEN"}]
        for kwargs in bad:
            with self.subTest(kwargs=sorted(kwargs)):
                with self.assertRaises(ValueError):
                    read.list_current_orders(**kwargs)
        self.assertEqual(len(transport.requests), 1)

    def test_account_details_whitelist(self):
        details = {"currencyCode": "GBP", "firstName": "Synthetic", "lastName": "Person", "localeCode": "en",
                   "region": "GBR", "timezone": "GMT", "discountRate": 0.0, "pointsBalance": 10, "countryCode": "GB"}
        _, read, _, _ = self.client([echo(details), echo({}), echo({"currencyCode": "gbp"})])
        result = read.get_account_details()
        self.assertEqual(result, {"currencyCode": "GBP", "countryCode": "GB", "discountRate": 0.0, "pointsBalance": 10})
        self.assertNotIn("Synthetic", json.dumps(result))
        self.assertEqual(read.get_account_details(), {"currencyCode": None, "countryCode": None, "discountRate": None,
                                                      "pointsBalance": None})
        with self.assertRaises(ApiError):
            read.get_account_details()

    def test_funds_fields(self):
        funds = {"availableToBetBalance": 100.5, "exposure": -2.0, "retainedCommission": 0.0, "exposureLimit": -10000.0,
                 "discountRate": 0.0, "pointsBalance": 3, "wallet": "UK"}
        _, read, transport, _ = self.client([echo(funds), echo({"availableToBetBalance": 1.0}),
                                             echo({"exposure": True})])
        self.assertEqual(read.get_account_funds(), {k: v for k, v in funds.items() if k != "wallet"})
        self.assertEqual(params(transport.requests[0]), {})
        partial = read.get_account_funds()
        self.assertEqual(partial["availableToBetBalance"], 1.0)
        self.assertIsNone(partial["exposure"])
        with self.assertRaises(ApiError):
            read.get_account_funds()


class SessionIntegrationTests(unittest.TestCase):
    """The real SessionManager under the JsonRpcClient: lazy login, one relogin, no login retry."""

    def setUp(self):
        support.block_network(self)
        self.clock = support.FakeClock()
        self.paths = support.temp_state_paths(self)
        self.config = cfg.load_config(support.write_test_config(self.paths), paths=self.paths)
        tls = patch.object(auth, "make_tls_context", side_effect=lambda *a: ssl.create_default_context())
        tls.start()
        self.addCleanup(tls.stop)

    def build(self, responses):
        transport = support.FakeHttpTransport(responses)
        credentials = {"username": Secret("synthetic-user"), "password": Secret("synthetic-password-0001"),
                       "app_key": Secret("synthetic-delayed-key-0001"), "app_key_slot": "delayed",
                       "cert_path": self.paths.home / "c.crt", "key_path": self.paths.home / "c.key",
                       "key_passphrase": None, "source": "keychain"}
        session = auth.SessionManager(self.config, credentials, paths=self.paths, transport=transport, clock=self.clock)
        return api.JsonRpcClient(self.config, session, clock=self.clock), transport

    def test_lazy_login_then_single_relogin(self):
        login = support.http_response({"sessionToken": support.PLANTED_SECRET, "loginStatus": "SUCCESS"})
        relogin = support.http_response({"sessionToken": "synthetic-session-token-0002", "loginStatus": "SUCCESS"})
        rpc, transport = self.build([login, aping_error("INVALID_SESSION_INFORMATION"), relogin, echo(["ok"])])
        self.assertEqual(rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}}), ["ok"])
        self.assertEqual([r["url"] for r in transport.requests], [CERTLOGIN, BETTING, CERTLOGIN, BETTING])
        self.assertEqual(transport.header_text(transport.requests[3], "X-Authentication"),
                         "synthetic-session-token-0002")

    def test_failed_lazy_login_is_not_retried_as_a_read(self):
        rpc, transport = self.build([support.transport_failure("receive"),
                                     support.http_response({"sessionToken": "x-0003", "loginStatus": "SUCCESS"})])
        with self.assertRaises(TransportError):
            rpc.call("SportsAPING/v1.0/listEventTypes", {"filter": {}})
        self.assertEqual([r["url"] for r in transport.requests], [CERTLOGIN])
        self.assertEqual(self.clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
