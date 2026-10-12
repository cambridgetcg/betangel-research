"""Exchange Stream API client tests over a scripted fake socket. Offline and synthetic; no live stream."""

import random
import unittest

from betangel_research.betfair.errors import ConfigError, StreamError
from betangel_research.betfair.esa import ReconnectPolicy, StreamClient

import test_betfair_support as support
import test_betfair_support_stream as fx


class EsaTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.clock = support.FakeClock()
        self.session = fx.FakeSession()

    def client(self, *connections, **kwargs):
        self.server = fx.FakeStreamServer(connections, self.clock)
        return fx.make_client(self.server, self.session, self.clock, **kwargs)

    def streaming(self, script, **kwargs):
        client = self.client(fx.handshake() + list(script), **kwargs)
        client.connect()
        client.authenticate()
        sub_id = client.subscribe_markets({"marketIds": [support.MARKET_ID]},
                                          {"fields": ["EX_BEST_OFFERS", "EX_MARKET_DEF"], "ladderLevels": 3})
        status = client.next_message()
        self.assertEqual((status["op"], status["msg"]["statusCode"]), ("status", "SUCCESS"))
        return client, sub_id

    def test_handshake_connection_auth_subscribe(self):
        client = self.client(fx.handshake(connection_id="002-x-1", connections_available=2)
                             + [fx.change(lambda sid: fx.image(sid, fx.pt_at(-300)))])
        self.assertEqual(client.state, "DISCONNECTED")
        self.assertEqual(client.connect(), {"connection_id": "002-x-1"})
        self.assertEqual(self.server.calls, [(fx.STREAM_HOST, 443, 15)])
        self.assertEqual(client.state, "AUTHENTICATING")
        self.assertEqual(client.authenticate(), {"status_code": "SUCCESS", "connections_available": 2})
        self.assertEqual(client.state, "AUTHENTICATED")
        data_filter = {"fields": ["EX_BEST_OFFERS", "EX_TRADED_VOL", "EX_MARKET_DEF"], "ladderLevels": 3}
        sub_id = client.subscribe_markets({"eventTypeIds": ["7"], "countryCodes": ["IE"]}, data_filter)
        self.assertEqual(client.state, "SUBSCRIBING")
        self.assertEqual(client.active_subscription_id, sub_id)
        sent = self.server.sockets[0].sent_messages()
        self.assertEqual([m["op"] for m in sent], ["authentication", "marketSubscription"])
        self.assertEqual([m["id"] for m in sent], [1, 2])
        self.assertEqual(sent[1], {"op": "marketSubscription", "id": sub_id, "segmentationEnabled": False,
                                   "heartbeatMs": 5000, "marketFilter": {"eventTypeIds": ["7"], "countryCodes": ["IE"]},
                                   "marketDataFilter": data_filter})
        self.assertEqual(client.subscription_message(), sent[1])
        self.assertTrue(self.server.sockets[0].sent_bytes().endswith(b"\r\n"))
        status = client.next_message()
        self.assertEqual((status["op"], status["id"]), ("status", sub_id))
        change = client.next_message()
        self.assertEqual((change["op"], change["id"], change["msg"]["ct"]), ("mcm", sub_id, "SUB_IMAGE"))
        self.assertEqual(change["received_at"], self.clock.utc_now())
        self.assertIsInstance(change["monotonic_ns"], int)
        self.assertEqual(client.state, "STREAMING")
        self.assertEqual(client.echoed(), {"heartbeat_ms": 5000, "conflate_ms": 0})

    def test_auth_line_never_returned_or_logged(self):
        secrets = (support.PLANTED_SECRET, fx.APP_KEY)
        client = self.client(fx.handshake() + [fx.change(lambda sid: fx.image(sid, fx.pt_at(-300)))])
        results = [client.connect(), client.authenticate()]
        sub_id = client.subscribe_markets({"marketIds": ["1.1"]}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1})
        results += [sub_id, client.next_message(), client.next_message(), client.subscription_message(),
                    client.clocks(), client.echoed(), client.last_status, client.counters]
        wire = self.server.sockets[0].sent_bytes()
        for secret in secrets:
            self.assertEqual(wire.count(secret.encode()), 1)        # only in the authentication line
            self.assertNotIn(secret, repr(results))
            self.assertNotIn(secret, repr(vars(client)))
        # A refused authentication: the exception text carries the code only.
        failing = self.client([fx.connection_line(), fx.reply("authentication", status_code="FAILURE",
                                                              errorCode="INVALID_APP_KEY", connectionClosed=True,
                                                              errorMessage=support.PLANTED_SECRET)])
        failing.connect()
        with self.assertRaises(StreamError) as caught:
            failing.authenticate()
        for secret in secrets:
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(secret, repr(caught.exception.args))
            self.assertNotIn(secret, repr(failing.last_status))
        self.assertIsNone(caught.exception.__context__)

    def test_session_must_supply_secrets(self):
        class PlainSession(fx.FakeSession):
            def token(self):
                return support.PLANTED_SECRET
        self.session = PlainSession()
        client = self.client([fx.connection_line()])
        client.connect()
        with self.assertRaises(TypeError):
            client.authenticate()
        self.assertEqual(self.server.sockets[0].sent, [])
        self.assertTrue(self.server.sockets[0].closed)

    def test_first_message_timeout_15s(self):
        client = self.client([])
        started = self.clock.mono_ns
        with self.assertRaises(StreamError) as caught:
            client.connect()
        self.assertEqual(caught.exception.code, "connection_message_timeout")
        waited = (self.clock.mono_ns - started) / 1e9
        self.assertGreaterEqual(waited, 15)
        self.assertLess(waited, 15.1)
        self.assertTrue(self.server.sockets[0].closed)
        self.assertEqual(client.state, "CLOSED")
        # A connection message just inside the window is accepted.
        client = self.client([fx.wait(14.5), fx.connection_line("late")])
        self.assertEqual(client.connect(), {"connection_id": "late"})
        # Anything other than op=connection first is refused.
        client = self.client([fx.line({"op": "mcm", "id": 1, "pt": 1})])
        with self.assertRaises(StreamError) as caught:
            client.connect()
        self.assertEqual(caught.exception.code, "unexpected_first_message")

    def test_crlf_and_partial_reads(self):
        first = fx.line({"op": "mcm", "id": 2, "pt": 10, "clk": "a"})
        second = fx.line({"op": "mcm", "id": 2, "pt": 11, "clk": "b"}, terminator=b"\n")
        third = fx.line({"op": "mcm", "id": 2, "pt": 12, "clk": "c"})
        client, sub_id = self.streaming([first[:7], first[7:-1], first[-1:] + second + third[:5], third[5:]])
        self.assertEqual(sub_id, 2)
        raws = [client.next_message()["raw"] for _ in range(3)]
        self.assertEqual(raws, [first[:-2], second[:-1], third[:-2]])
        self.assertTrue(all(not r.endswith((b"\r", b"\n")) for r in raws))
        # Only one trailing \r is stripped: a doubled \r survives for the recorder to reject.
        client, _ = self.streaming([fx.line({"op": "mcm", "id": 2, "pt": 13}, terminator=b"\r\r\n")])
        self.assertTrue(client.next_message()["raw"].endswith(b"\r"))

    def test_line_too_long(self):
        long_line = fx.line({"op": "mcm", "id": 2, "pt": 1, "pad": "x" * 3000})
        client, _ = self.streaming([long_line], max_line_bytes=1024)
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "line_too_long")
        self.assertTrue(caught.exception.connection_closed)
        self.assertTrue(self.server.sockets[0].closed)
        # Also when no newline ever arrives (the buffer itself overflows).
        client, _ = self.streaming([b"{" + b"x" * 2000], max_line_bytes=1024)
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "line_too_long")

    def test_line_of_exactly_max_bytes_with_split_crlf(self):
        def sized(n):
            msg = {"op": "mcm", "id": 2, "pt": 1, "pad": ""}
            msg["pad"] = "x" * (n - len(fx.line(msg, b"")))
            raw = fx.line(msg, b"")
            self.assertEqual(len(raw), n)
            return raw
        # The \r arrives in one recv and the \n in the next: a 1024-byte line is still within 1024.
        exact = sized(1024)
        client, _ = self.streaming([exact + b"\r", b"\n"], max_line_bytes=1024)
        self.assertEqual(client.next_message()["raw"], exact)
        # One byte more is refused, however the terminator is split.
        for chunks in ([sized(1025) + b"\r", b"\n"], [sized(1025), b"\r\n"], [sized(1024) + b"x", b"\r\n"]):
            with self.subTest(chunks=[len(c) for c in chunks]):
                client, _ = self.streaming(chunks, max_line_bytes=1024)
                with self.assertRaises(StreamError) as caught:
                    client.next_message()
                self.assertEqual(caught.exception.code, "line_too_long")

    def test_failure_status_raises_with_connection_closed(self):
        client = self.client([fx.connection_line(), fx.reply("authentication", status_code="FAILURE",
                                                             errorCode="NO_APP_KEY", connectionClosed=True)])
        client.connect()
        with self.assertRaises(StreamError) as caught:
            client.authenticate()
        self.assertEqual((caught.exception.code, caught.exception.connection_closed), ("NO_APP_KEY", True))
        self.assertTrue(self.server.sockets[0].closed)
        self.assertEqual(client.state, "CLOSED")
        # connectionClosed missing on a FAILURE counts as closed.
        client, _ = self.streaming([fx.line({"op": "status", "id": 2, "statusCode": "FAILURE",
                                             "errorCode": "UNEXPECTED_ERROR"})])
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual((caught.exception.code, caught.exception.connection_closed), ("UNEXPECTED_ERROR", True))
        self.assertEqual(client.state, "CLOSED")

    def test_subscription_limit_raises_nonclosing(self):
        client = self.client([fx.connection_line(), fx.auth_ok(),
                              fx.reply("marketSubscription", status_code="FAILURE",
                                       errorCode="SUBSCRIPTION_LIMIT_EXCEEDED", connectionClosed=False),
                              fx.reply("marketSubscription", nth=1)])
        client.connect()
        client.authenticate()
        client.subscribe_markets({"eventTypeIds": ["7"]}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1})
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual((caught.exception.code, caught.exception.connection_closed),
                         ("SUBSCRIPTION_LIMIT_EXCEEDED", False))
        self.assertFalse(self.server.sockets[0].closed)
        self.assertEqual(client.last_status["error_code"], "SUBSCRIPTION_LIMIT_EXCEEDED")
        second = client.subscribe_markets({"marketIds": ["1.1"]}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1})
        self.assertEqual(client.next_message()["id"], second)

    def test_liveness_2x_echoed_heartbeat(self):
        client, _ = self.streaming([], heartbeat_ms=1000)
        self.assertEqual(client.liveness_timeout_s(), 2.0)       # requested value before any change message
        started = self.clock.mono_ns
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "liveness_timeout")
        self.assertAlmostEqual((self.clock.mono_ns - started) / 1e9, 2.0, delta=0.05)
        client, _ = self.streaming([fx.change(lambda sid: fx.image(sid, 1, heartbeat_ms=3000, conflate_ms=0)),
                                    fx.wait(5.5), fx.change(lambda sid: fx.heartbeat(sid, 2))], heartbeat_ms=1000)
        client.next_message()
        self.assertEqual(client.echoed(), {"heartbeat_ms": 3000, "conflate_ms": 0})
        self.assertEqual(client.liveness_timeout_s(), 6.0)
        self.assertEqual(client.next_message()["msg"]["ct"], "HEARTBEAT")     # 5.5 s of silence is fine
        started = self.clock.mono_ns
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "liveness_timeout")
        self.assertAlmostEqual((self.clock.mono_ns - started) / 1e9, 6.0, delta=0.05)

    def test_liveness_with_conflation(self):
        client, _ = self.streaming([fx.change(lambda sid: fx.image(sid, 1, heartbeat_ms=5000, conflate_ms=180000)),
                                    fx.wait(185), fx.change(lambda sid: fx.delta(sid, 2, [{"id": 11, "ltp": 2.5}]))])
        client.next_message()
        self.assertEqual(client.liveness_timeout_s(), 190.0)
        self.assertEqual(client.next_message()["msg"]["pt"], 2)
        # With max_wait_s the caller gets None back while the connection is still live.
        self.assertIsNone(client.next_message(max_wait_s=1.0))
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "liveness_timeout")

    def test_echo_outside_documented_bounds_ignored(self):
        """F50/F51: heartbeat echo 500-30000, conflation at most 180000 (delayed key)."""
        cases = [
            ((600000, 3600000), {"heartbeat_ms": None, "conflate_ms": None}, 10.0, 1),
            ((400, 0), {"heartbeat_ms": None, "conflate_ms": None}, 10.0, 1),
            ((5000, 3600000), {"heartbeat_ms": 5000, "conflate_ms": None}, 10.0, 1),
            ((5000, 180001), {"heartbeat_ms": 5000, "conflate_ms": None}, 10.0, 1),
            ((30000, 180000), {"heartbeat_ms": 30000, "conflate_ms": 180000}, 240.0, 0),
            ((500, None), {"heartbeat_ms": 500, "conflate_ms": None}, 1.0, 0),
        ]
        for (heartbeat, conflate), echoed, timeout, rejected in cases:
            with self.subTest(heartbeat=heartbeat, conflate=conflate):
                client, _ = self.streaming([fx.change(lambda sid: fx.image(sid, 1, heartbeat_ms=heartbeat,
                                                                           conflate_ms=conflate))])
                client.next_message()
                self.assertEqual(client.echoed(), echoed)
                self.assertEqual(client.liveness_timeout_s(), timeout)
                self.assertEqual(client.counters["echo_out_of_range"], rejected)
                if rejected:
                    self.assertEqual(client.rejected_echo, {"heartbeat_ms": heartbeat, "conflate_ms": conflate})
        # A dead connection behind an absurd echo still times out on the requested heartbeat.
        client, _ = self.streaming([fx.change(lambda sid: fx.image(sid, 1, heartbeat_ms=600000, conflate_ms=3600000))])
        client.next_message()
        started = self.clock.mono_ns
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "liveness_timeout")
        self.assertAlmostEqual((self.clock.mono_ns - started) / 1e9, 10.0, delta=0.05)

    def test_uses_session_is_identity(self):
        client = self.client([])
        self.assertTrue(client.uses_session(self.session))
        self.assertFalse(client.uses_session(fx.FakeSession()))

    def test_clocks_track_non_null_only(self):
        client, _ = self.streaming([
            fx.change(lambda sid: fx.image(sid, 1)),
            fx.change(lambda sid: fx.mcm(sid, 2, clk="c2")),
            fx.change(lambda sid: dict(fx.mcm(sid, 3), clk=None, initialClk=None)),
            fx.change(lambda sid: fx.heartbeat(sid, 4))])
        self.assertEqual(client.clocks(), {"initial_clk": None, "clk": None})
        client.next_message()
        self.assertEqual(client.clocks(), {"initial_clk": "init-1", "clk": "clk-1"})
        client.next_message()
        self.assertEqual(client.clocks(), {"initial_clk": "init-1", "clk": "c2"})
        client.next_message()
        self.assertEqual(client.clocks(), {"initial_clk": "init-1", "clk": "c2"})
        client.next_message()
        self.assertEqual(client.clocks(), {"initial_clk": "init-1", "clk": "clk-4"})
        self.assertEqual(client.clocks("order"), {"initial_clk": None, "clk": None})
        client.drop_clocks()
        self.assertEqual(client.clocks(), {"initial_clk": None, "clk": None})

    def test_invalid_clock_drops_clocks(self):
        client, _ = self.streaming([fx.change(lambda sid: fx.image(sid, 1)),
                                    fx.line({"op": "status", "id": 2, "statusCode": "FAILURE",
                                             "errorCode": "INVALID_CLOCK", "connectionClosed": True})])
        client.next_message()
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "INVALID_CLOCK")
        self.assertEqual(client.clocks(), {"initial_clk": None, "clk": None})

    def test_superseded_id_dropped(self):
        client, first = self.streaming([
            fx.change(lambda sid: fx.image(sid, 1), nth=0),
            fx.reply("marketSubscription", nth=1),
            fx.change(lambda sid: fx.delta(sid, 2, [{"id": 11, "ltp": 3.0}]), nth=0),
            fx.change(lambda sid: fx.image(sid, 3), nth=1)])
        client.next_message()
        second = client.subscribe_markets({"marketIds": ["1.1"]}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1})
        self.assertNotEqual(first, second)
        self.assertEqual(client.next_message()["op"], "status")
        message = client.next_message()
        self.assertEqual((message["id"], message["msg"]["pt"]), (second, 3))
        self.assertEqual(client.counters["superseded_dropped"], 1)
        self.assertEqual(client.clocks()["clk"], "clk-3")      # the dropped message never touched the clocks

    def test_status_503_flag_no_disconnect(self):
        client, _ = self.streaming([fx.change(lambda sid: fx.image(sid, 1)),
                                    fx.change(lambda sid: dict(fx.heartbeat(sid, 2), status=503)),
                                    fx.change(lambda sid: fx.heartbeat(sid, 3))])
        client.next_message()
        flagged = client.next_message()
        self.assertEqual(flagged["msg"]["status"], 503)
        self.assertEqual(client.counters["status_503"], 1)
        self.assertEqual(client.state, "STREAMING")
        self.assertEqual(client.next_message()["msg"]["pt"], 3)

    def test_connection_lost_and_invalid_json(self):
        client, _ = self.streaming([fx.EOF])
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "connection_lost")
        client, _ = self.streaming([b'{"op": "mcm", ' + support.PLANTED_SECRET.encode() + b"\r\n"])
        with self.assertRaises(StreamError) as caught:
            client.next_message()
        self.assertEqual(caught.exception.code, "invalid_json")
        self.assertNotIn(support.PLANTED_SECRET, repr(caught.exception.__context__))
        self.assertIsNone(caught.exception.__context__)

    def test_backoff_with_jitter_capped_per_hour(self):
        clock = support.FakeClock()
        policy = ReconnectPolicy([5, 30, 120, 300], 4, rng=random.Random(3), clock=clock)
        delays = [policy.next_delay() for _ in range(4)]
        for delay, base in zip(delays, [5, 30, 120, 300]):
            self.assertGreaterEqual(delay, base * 0.8)
            self.assertLessEqual(delay, base * 1.2)
        self.assertNotEqual(delays, [5, 30, 120, 300])           # jitter applied
        with self.assertRaises(StreamError) as caught:
            policy.next_delay()
        self.assertEqual(caught.exception.code, "reconnect_budget_exhausted")
        clock.advance(3601)
        self.assertEqual(policy.attempts_in_last_hour(), 0)
        self.assertLessEqual(policy.next_delay(), 300 * 1.2)      # last step repeats; never beyond the list
        policy.reset()
        self.assertLessEqual(policy.next_delay(), 5 * 1.2)
        # stream_backoff codes wait at least 60 s, doubling while they repeat.
        backoff = ReconnectPolicy([5], 10, rng=random.Random(1), clock=clock)
        self.assertEqual([round(backoff.next_delay(category="stream_backoff")) for _ in range(3)], [60, 120, 240])
        self.assertLess(backoff.next_delay(), 7)
        # Same seed, same delays.
        again = [ReconnectPolicy([5, 30], 9, rng=random.Random(8), clock=clock).next_delay() for _ in range(2)]
        self.assertEqual(again[0], again[1])

    def test_segmentation_disabled(self):
        client = self.client(fx.handshake())
        client.connect()
        client.authenticate()
        client.subscribe_markets({"marketIds": ["1.1"]}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1},
                                 initial_clk="i", clk="c")
        client.subscribe_orders({"customerStrategyRefs": ["bar-manual"]})
        sent = self.server.sockets[0].sent_messages()[1:]
        self.assertEqual([m["segmentationEnabled"] for m in sent], [False, False])
        self.assertEqual((sent[0]["initialClk"], sent[0]["clk"]), ("i", "c"))
        self.assertEqual(sent[1], {"op": "orderSubscription", "id": 3, "segmentationEnabled": False,
                                   "heartbeatMs": 5000, "orderFilter": {"customerStrategyRefs": ["bar-manual"]}})
        conflated = self.client(fx.handshake(), conflate_ms=0)
        conflated.connect()
        conflated.authenticate()
        conflated.subscribe_markets({"marketIds": ["1.1"]}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1})
        self.assertEqual(self.server.sockets[0].sent_messages()[1]["conflateMs"], 0)

    def test_subscription_inputs_refused(self):
        client = self.client(fx.handshake())
        client.connect()
        client.authenticate()
        bad_calls = [
            lambda: client.subscribe_markets({}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1}),
            lambda: client.subscribe_markets({"venues": ["X"]}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1}),
            lambda: client.subscribe_markets({"marketIds": ["1.1"]},
                                             {"fields": ["EX_BEST_OFFERS_DISP", "EX_MARKET_DEF"], "ladderLevels": 1}),
            lambda: client.subscribe_markets({"marketIds": ["1.1"]}, {"fields": ["EX_LTP"], "ladderLevels": 1}),
            lambda: client.subscribe_orders({"accountIds": [1]}),
            lambda: client.subscribe_markets({"marketIds": ["1.1"]}, {"fields": ["EX_MARKET_DEF"], "ladderLevels": 1},
                                             clk=5),
        ]
        for call in bad_calls:
            with self.subTest(call=call):
                with self.assertRaises(ConfigError):
                    call()
        self.assertEqual(len(self.server.sockets[0].sent_messages()), 1)    # only the authentication line

    def test_constructor_allowlists(self):
        for kwargs in ({"host": "example.invalid"}, {"port": 444}, {"heartbeat_ms": 100}, {"heartbeat_ms": True},
                       {"conflate_ms": 200000}, {"connect_timeout_s": 0}, {"max_line_bytes": 10}):
            with self.subTest(kwargs=kwargs):
                options = dict(host=fx.STREAM_HOST, port=443, session=self.session, clock=self.clock)
                options.update(kwargs)
                with self.assertRaises(ConfigError):
                    StreamClient(**options)

    def test_default_socket_factory_respects_forbid_network(self):
        client = StreamClient(host=fx.STREAM_HOST, port=443, session=self.session, clock=self.clock)
        from betangel_research.betfair.errors import TransportError
        with self.assertRaises(TransportError) as caught:
            client.connect()
        self.assertEqual(caught.exception.code, "network_forbidden")
        self.assertEqual(client.state, "CLOSED")


if __name__ == "__main__":
    unittest.main()
