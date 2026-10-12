"""Stream recorder tests against a scripted fake stream. Offline and synthetic: these show that recorded
segments fit the existing replay and motif readers, not anything about live Betfair data."""

import json
import os
from pathlib import Path
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch

from betangel_research import microstructure, stream
from betangel_research.betfair import recorder as recorder_module
from betangel_research.betfair.errors import ApiError, AuthError, ConfigError, LockBusy, SecretLeak
from betangel_research.betfair.esa import StreamClient
from betangel_research.betfair.paths import file_lock
from betangel_research.betfair.receipts import (canonical_sha256, file_sha256, parse_utc_ns, strict_json_loads,
                                                utc_compact_from_ns, utc_ms_from_ns)
from betangel_research.betfair.recorder import (RECEIPT_STATUS, REJECT_REASONS, StreamRecorder, line_acceptance,
                                                verify_recording)

import test_betfair_support as support
import test_betfair_support_stream as fx

SEGMENT_KEYS = {"index", "path", "sha256", "bytes", "lines", "recv_path", "recv_sha256", "connection_id",
                "subscription_mode", "requested_count", "requested_market_ids_sha256", "first_pt", "last_pt",
                "first_received_at", "last_received_at", "market_ids", "heartbeat_lines", "con_true_lines",
                "status_503_lines", "img_market_changes", "superseded_dropped", "max_inter_line_gap_ms",
                "close_reason", "markets_without_initial_definition", "replay_check"}


def sub(builder):
    """A change for the latest subscription."""
    return fx.change(builder)


def image_at(offset_s, **kwargs):
    return sub(lambda sid: fx.image(sid, fx.pt_at(offset_s), **kwargs))


def delta_at(offset_s, rc, **kwargs):
    return sub(lambda sid: fx.delta(sid, fx.pt_at(offset_s), rc, **kwargs))


def read_jsonl(path):
    return [json.loads(text) for text in Path(path).read_text().splitlines()]


class RecorderTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock()
        self.session = fx.FakeSession()
        self.events = []

    def recorder(self, connections, *, overrides=None, ids=(support.MARKET_ID,), session=None, seed=7,
                 market_lister=None, emit=None, stream_factory=None):
        if connections and not all(isinstance(c, list) for c in connections):
            connections = [connections]                 # one script given: one connection
        self.server = fx.FakeStreamServer(connections, self.clock)
        session = session or self.session
        self.lister = market_lister or fx.lister(ids)
        config = fx.recorder_config(self.paths, overrides)
        factory = stream_factory or (lambda: fx.make_client(self.server, session, self.clock))
        return StreamRecorder(config, paths=self.paths, session=session, stream_factory=factory,
                              market_lister=self.lister, clock=self.clock, rng=random.Random(seed),
                              emit=emit or self.events.append)

    def run_recorder(self, connections, *, max_seconds=None, stop=None, **kwargs):
        recorder = self.recorder(connections, **kwargs)
        receipt = recorder.run(stop_requested=stop or self.server.exhausted, max_seconds=max_seconds)
        self.dir = self.paths.recordings_dir / receipt["session_id"]
        return receipt

    def simple(self, *items, offset=-300, **image_kwargs):
        return fx.handshake() + [image_at(offset, **image_kwargs)] + list(items)

    def event_rows(self, name=None):
        rows = read_jsonl(self.dir / "events.jsonl")
        return [r for r in rows if name is None or r["event"] == name]

    # --- compatibility with the existing readers (synthetic only) -------------------------------

    def test_segment_replays_through_stream_messages(self):
        """R1."""
        msgs = []

        def keep(builder):
            def build(sid):
                msgs.append(builder(sid))
                return msgs[-1]
            return sub(build)
        receipt = self.run_recorder(fx.handshake() + [
            keep(lambda sid: fx.image(sid, fx.pt_at(-300))), fx.wait(1),
            keep(lambda sid: fx.delta(sid, fx.pt_at(-299), [{"id": 11, "ltp": 2.1}])),
            keep(lambda sid: fx.heartbeat(sid, fx.pt_at(-298))),
            keep(lambda sid: fx.delta(sid, fx.pt_at(-298), [{"id": 12, "tv": 120}], con=True))])
        segment = self.dir / "seg-000.jsonl"
        expected = b"".join(json.dumps(m, separators=(",", ":")).encode() + b"\n" for m in msgs)
        self.assertEqual(segment.read_bytes(), expected)            # byte for byte, terminator normalised
        replayed = list(stream.messages(segment))
        self.assertEqual(replayed, msgs)
        info = receipt["segments"][0]
        self.assertEqual(set(info), SEGMENT_KEYS)
        self.assertEqual((info["lines"], info["first_pt"], info["last_pt"]), (4, fx.pt_at(-300), fx.pt_at(-298)))
        self.assertEqual((info["heartbeat_lines"], info["con_true_lines"], info["img_market_changes"]), (1, 1, 1))
        self.assertEqual(info["market_ids"], ["1.1"])
        self.assertEqual(info["replay_check"], {"reader": "betangel_research.stream.messages", "status": "passed",
                                                "error": None})
        self.assertEqual(info["max_inter_line_gap_ms"], 1000)
        self.assertEqual(verify_recording(self.dir)["status"], "verified")

    def test_segment_feeds_replay_snapshots_and_build_records(self):
        """R2: the forecast replay reads a recorded segment (synthetic data only)."""
        config = dict(countries=["IE"], market_type="WIN", decision_before_start_s=120, lookback_s=60, horizon_s=30,
                      delays_s=[0], max_snapshot_age_s=90)
        self.run_recorder(fx.handshake() + [
            image_at(-240), delta_at(-180, [{"id": 11, "ltp": 2.5}]), delta_at(-120, [{"id": 11, "ltp": 3}]),
            delta_at(-90, [{"id": 11, "ltp": 4}]), delta_at(-89, [{"id": 11, "ltp": 100}])])
        states, summary = stream.replay_snapshots(self.dir / "seg-000.jsonl", config)
        self.assertEqual((summary["messages"], summary["markets"]), (5, 1))
        rows, exclusions = stream.build_records(states, config)
        self.assertEqual(len(rows["0"]), 2, exclusions)
        self.assertEqual(exclusions["0"], {})

    def test_segment_classified_adv_by_microstructure_synthetic_only(self):
        """R3: the motif reader sees an ADV-shaped recording (synthetic data only)."""
        self.run_recorder(fx.handshake() + [
            image_at(-160),
            delta_at(-155, [{"id": 11, "batb": [[0, 2.0, 40]], "tv": 110}]),
            delta_at(-125, [{"id": 11, "tv": 120}, {"id": 12, "tv": 105}]),
            delta_at(-119, [{"id": 11, "tv": 130}])])
        result = microstructure.sample_features(self.dir / "seg-000.jsonl")
        self.assertEqual(len(result["records"]), 2)
        for record in result["records"]:
            self.assertEqual(record["formats_observed"], ["ADV"])
            self.assertTrue(record["eligible"], record["invalid_reasons"])
        self.assertEqual(result["audit"]["eligible_records"], 2)

    # --- line acceptance and segment boundaries -------------------------------------------------

    def test_line_acceptance_each_reason(self):
        good = {"op": "mcm", "id": 2, "pt": 10}
        cases = [
            ("not_mcm", b"{}", {"op": "status", "id": 2}, None, 2),
            ("not_mcm", b"{}", {"op": "connection"}, None, 2),
            ("superseded_id", b"{}", dict(good, id=1), None, 2),
            ("superseded_id", b"{}", dict(good, id=True), None, 1),
            ("superseded_id", b"{}", good, None, None),
            ("pt_not_int", b"{}", dict(good, pt=10.0), None, 2),
            ("pt_not_int", b"{}", dict(good, pt="10"), None, 2),
            ("pt_not_int", b"{}", dict(good, pt=True), None, 2),
            ("pt_not_int", b"{}", {"op": "mcm", "id": 2}, None, 2),
            ("pt_regression", b"{}", good, 11, 2),
            ("pt_regression", b"{}", dict(good, pt=-2), None, 2),
            ("segmented", b"{}", dict(good, segmentType="SEG"), None, 2),
            ("carriage_return", b'{"op":"mcm",\r"id":2,"pt":10}', good, None, 2),
            (None, b"{}", good, 10, 2),
            (None, b"{}", dict(good, segmentType=None, pt=0), None, 2),
        ]
        self.assertEqual(set(REJECT_REASONS), {c[0] for c in cases} - {None})
        for expected, raw, msg, prev_pt, active in cases:
            with self.subTest(expected=expected, msg=msg, prev_pt=prev_pt):
                self.assertEqual(line_acceptance(raw, msg, prev_pt, active), expected)

    def test_only_mcm_written_status_to_events(self):
        self.run_recorder(self.simple(
            sub(lambda sid: {"op": "status", "id": sid, "statusCode": "SUCCESS", "connectionClosed": False,
                             "errorMessage": "free text is never stored"}),
            fx.line({"op": "surprise", "id": 9}),
            delta_at(-299, [{"id": 11, "ltp": 2.2}])))
        lines = (self.dir / "seg-000.jsonl").read_bytes().splitlines()
        self.assertEqual([json.loads(raw)["op"] for raw in lines], ["mcm", "mcm"])
        names = [r["event"] for r in self.event_rows()]
        for name in ("connected", "auth_status", "subscribed", "echo", "status_message", "ignored_message",
                     "segment_closed", "stopped"):
            self.assertIn(name, names)
        statuses = self.event_rows("status_message")
        self.assertEqual(len(statuses), 2)            # the subscription reply and the mid-stream status
        self.assertEqual(set(statuses[0]), {"event", "at", "monotonic_ns", "id", "status_code", "error_code",
                                            "connection_closed"})
        self.assertNotIn("free text", (self.dir / "events.jsonl").read_text())
        subscribed = self.event_rows("subscribed")[0]
        self.assertEqual((subscribed["segment"], subscribed["segmentation_enabled"], subscribed["requested_count"]),
                         (0, False, 1))
        # Taken from the subscription message actually sent, not from config.
        sent = [m for m in self.server.sockets[0].sent_messages() if m["op"] == "marketSubscription"][0]
        self.assertEqual((subscribed["market_filter"], subscribed["market_data_filter"], subscribed["heartbeat_ms"],
                          subscribed["conflate_ms"], subscribed["request_id"]),
                         (sent["marketFilter"], sent["marketDataFilter"], 5000, None, sent["id"]))
        self.assertTrue(all("at" in r and isinstance(r["monotonic_ns"], int) for r in self.event_rows()))

    def test_recv_sidecar_aligns_lines_offsets_hashes(self):
        self.run_recorder(self.simple(fx.wait(2), delta_at(-299, [{"id": 11, "ltp": 2.2}]), fx.wait(0.5),
                                      sub(lambda sid: fx.heartbeat(sid, fx.pt_at(-298)))))
        data = (self.dir / "seg-000.jsonl").read_bytes()
        rows = read_jsonl(self.dir / "seg-000.recv.jsonl")
        self.assertEqual([r["line"] for r in rows], [1, 2, 3])
        previous_ns = None
        for row, raw in zip(rows, data.splitlines()):
            self.assertEqual(set(row), {"line", "byte_offset", "bytes", "sha256", "received_at", "monotonic_ns",
                                        "pt", "clk"})
            self.assertEqual(data[row["byte_offset"]:row["byte_offset"] + row["bytes"]], raw)
            self.assertEqual(data[row["byte_offset"] + row["bytes"]:row["byte_offset"] + row["bytes"] + 1], b"\n")
            self.assertEqual(row["sha256"], __import__("hashlib").sha256(raw).hexdigest())
            msg = json.loads(raw)
            self.assertEqual((row["pt"], row["clk"]), (msg["pt"], msg["clk"]))
            parse_utc_ns(row["received_at"])
            if previous_ns is not None:
                self.assertGreater(row["monotonic_ns"], previous_ns)
            previous_ns = row["monotonic_ns"]
        self.assertEqual(rows[1]["monotonic_ns"] - rows[0]["monotonic_ns"], 2_000_000_000)

    def test_reconnect_new_segment_fresh_image_gap(self):
        receipt = self.run_recorder([
            self.simple(delta_at(-299, [{"id": 11, "ltp": 2.2}]), fx.EOF),
            fx.handshake(connection_id="002-second") + [image_at(-200), delta_at(-199, [{"id": 11, "ltp": 2.4}])]])
        self.assertEqual(len(self.server.calls), 2)
        first, second = receipt["segments"]
        self.assertEqual((first["close_reason"], first["lines"]), ("connection_lost", 2))
        self.assertEqual((second["connection_id"], second["img_market_changes"]), ("002-second", 1))
        first_line = json.loads((self.dir / "seg-001.jsonl").read_bytes().splitlines()[0])
        self.assertEqual(first_line["ct"], "SUB_IMAGE")
        self.assertEqual(receipt["gaps"], [{"after_segment": 0, "from_received_at": first["last_received_at"],
                                            "to_received_at": second["first_received_at"],
                                            "reason": "connection_lost"}])
        self.assertGreater(parse_utc_ns(second["first_received_at"]), parse_utc_ns(first["last_received_at"]))
        # The recorder never resumes with clocks: the second subscription asks for a fresh image.
        resubscribe = [m for m in self.server.sockets[1].sent_messages() if m["op"] == "marketSubscription"][0]
        self.assertNotIn("clk", resubscribe)
        self.assertNotIn("initialClk", resubscribe)
        self.assertEqual(self.session.calls.count("token"), 2)
        self.assertIn("disconnected", [r["event"] for r in self.event_rows()])
        self.assertTrue(all(s["replay_check"]["status"] == "passed" for s in receipt["segments"]))
        self.assertEqual(verify_recording(self.dir)["status"], "verified")

    def test_refresh_new_subscription_new_segment(self):
        beats = []
        for i in range(14):
            beats += [fx.wait(5), fx.change(lambda sid, i=i: fx.heartbeat(sid, fx.pt_at(-299 + 5 * i)), nth=0)]
        script = (fx.handshake() + [fx.change(lambda sid: fx.image(sid, fx.pt_at(-300)), nth=0)] + beats
                  + [fx.reply("marketSubscription", nth=1),
                     fx.change(lambda sid: fx.delta(sid, fx.pt_at(-200), [{"id": 11, "ltp": 9.0}]), nth=0),
                     fx.change(lambda sid: fx.image(sid, fx.pt_at(-199)), nth=1),
                     fx.change(lambda sid: fx.delta(sid, fx.pt_at(-198), [{"id": 11, "ltp": 2.6}]), nth=1)])
        receipt = self.run_recorder([script], overrides={"recorder": {"refresh_s": 60}})
        self.assertEqual(len(self.server.calls), 1)                  # same connection
        self.assertEqual(len(self.lister.calls), 2)
        first, second = receipt["segments"]
        self.assertEqual(first["close_reason"], "scheduled_refresh")
        self.assertGreaterEqual(second["superseded_dropped"], 1)
        self.assertEqual(second["first_pt"], fx.pt_at(-199))
        self.assertEqual(receipt["gaps"][0]["reason"], "scheduled_refresh")
        subscriptions = [m for m in self.server.sockets[0].sent_messages() if m["op"] == "marketSubscription"]
        self.assertEqual(len(subscriptions), 2)
        for path in ("seg-000.jsonl", "seg-001.jsonl"):
            ids = {m["id"] for m in stream.messages(self.dir / path)}
            self.assertEqual(len(ids), 1)
        self.assertEqual(verify_recording(self.dir)["status"], "verified")

    def test_precount_coarse_vs_explicit(self):
        receipt = self.run_recorder([self.simple()], ids=["1.1", "1.2", "1.3", "1.2"])
        # The pre-count runs before connecting, at the session's start time (the fake clock has not moved).
        start_from = utc_ms_from_ns(parse_utc_ns(support.BASE_UTC) - 1800 * 1_000_000_000)
        self.assertEqual(start_from, "2020-01-01T11:30:00.123Z")
        self.assertEqual(self.lister.calls, [{"eventTypeIds": ["7"], "marketCountries": ["GB", "IE"],
                                              "marketTypeCodes": ["WIN"], "marketStartTime": {"from": start_from}}])
        sent = [m for m in self.server.sockets[0].sent_messages() if m["op"] == "marketSubscription"][0]
        self.assertEqual(sent["marketFilter"], {"eventTypeIds": ["7"], "countryCodes": ["GB", "IE"],
                                                "marketTypes": ["WIN"]})
        info = receipt["segments"][0]
        self.assertEqual((info["subscription_mode"], info["requested_count"], info["requested_market_ids_sha256"]),
                         ("coarse", 3, None))
        many = [f"1.{1000 + n}" for n in range(200)]
        self.clock.advance(5)
        receipt = self.run_recorder([self.simple()], ids=many, seed=8)
        sent = [m for m in self.server.sockets[0].sent_messages() if m["op"] == "marketSubscription"][0]
        self.assertEqual(sent["marketFilter"]["marketIds"], many[:150])
        self.assertEqual(sent["marketFilter"]["eventTypeIds"], ["7"])
        info = receipt["segments"][0]
        self.assertEqual((info["subscription_mode"], info["requested_count"]), ("explicit", 150))
        self.assertEqual(info["requested_market_ids_sha256"], canonical_sha256(many[:150]))
        # A lister that returns malformed ids stops the session before any connection.
        receipt = self.run_recorder([self.simple()], ids=["2.x"], seed=9)
        self.assertEqual(receipt["stop_reason"], "market_lister_failed:ValueError")
        self.assertEqual(self.server.calls, [])

    def test_subscription_limit_stops_session(self):
        limit = [fx.connection_line(), fx.auth_ok(),
                 fx.reply("marketSubscription", status_code="FAILURE", errorCode="SUBSCRIPTION_LIMIT_EXCEEDED",
                          connectionClosed=False)]
        receipt = self.run_recorder([limit, self.simple()])
        self.assertEqual(receipt["stop_reason"], "subscription_limit_exceeded")
        self.assertEqual(len(self.server.calls), 1)                   # never retried
        self.assertEqual((receipt["segments"][0]["lines"], receipt["segments"][0]["close_reason"]),
                         (0, "SUBSCRIPTION_LIMIT_EXCEEDED"))
        self.assertTrue(receipt["complete"])
        self.assertEqual(self.session.calls[-1], "logout")
        self.assertEqual(self.event_rows("status_message")[-1]["error_code"], "SUBSCRIPTION_LIMIT_EXCEEDED")
        self.assertTrue((self.dir / "receipt.json").is_file())

    def test_pt_regression_closes_segment(self):
        receipt = self.run_recorder([
            self.simple(delta_at(-310, [{"id": 11, "ltp": 2.2}])),
            fx.handshake() + [image_at(-200)]])
        first, second = receipt["segments"]
        self.assertEqual((first["close_reason"], first["lines"], first["last_pt"]), ("pt_regression", 1, fx.pt_at(-300)))
        rejected = self.event_rows("rejected_line")
        self.assertEqual(len(rejected), 1)
        self.assertEqual((rejected[0]["reason"], rejected[0]["pt"]), ("pt_regression", fx.pt_at(-310)))
        self.assertRegex(rejected[0]["rejected_line_sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(receipt["gaps"][0]["reason"], "pt_regression")
        self.assertEqual(len(self.server.calls), 2)                   # a new connection, a fresh image
        self.assertEqual(second["first_pt"], fx.pt_at(-200))
        self.assertEqual(verify_recording(self.dir)["status"], "verified")

    def test_unexpected_segmentation_closes_segment(self):
        receipt = self.run_recorder([
            self.simple(sub(lambda sid: dict(fx.delta(sid, fx.pt_at(-299), [{"id": 11, "ltp": 2.2}]),
                                             segmentType="SEG_START"))),
            fx.handshake() + [image_at(-200)]])
        self.assertEqual(receipt["segments"][0]["close_reason"], "segmented")
        self.assertEqual(receipt["segments"][0]["lines"], 1)
        self.assertNotIn(b"SEG_START", (self.dir / "seg-000.jsonl").read_bytes())
        self.assertEqual(self.event_rows("rejected_line")[0]["reason"], "segmented")

    def test_other_rejections_close_segment(self):
        def raw_change(transform):
            def build(sock):
                ids = [m["id"] for m in sock.sent_messages() if m["op"] == "marketSubscription"]
                return transform(ids[-1]) if ids else None
            return build
        cases = {
            # A bare \r is JSON whitespace, but stream.messages() reads with universal newlines and would split there.
            "carriage_return": raw_change(lambda sid: fx.line(fx.delta(sid, fx.pt_at(-299), [{"id": 11, "ltp": 2.2}]))
                                          .replace(b'"pt"', b'\r"pt"', 1)),
            "pt_not_int": sub(lambda sid: dict(fx.delta(sid, 0, [{"id": 11, "ltp": 2.2}]), pt=float(fx.pt_at(-299)))),
        }
        for seed, (reason, item) in enumerate(sorted(cases.items()), 20):
            with self.subTest(reason=reason):
                self.clock.advance(2)
                receipt = self.run_recorder([self.simple(item), fx.handshake() + [image_at(-200)]], seed=seed)
                self.assertEqual(receipt["segments"][0]["close_reason"], reason)
                self.assertEqual(self.event_rows("rejected_line")[0]["reason"], reason)
                self.assertNotIn(b"\r", (self.dir / "seg-000.jsonl").read_bytes())

    def test_replay_check_fails_without_market_time(self):
        no_definition = sub(lambda sid: fx.mcm(sid, fx.pt_at(-300), [
            {"id": "1.1", "img": True, "rc": fx.runners_adv()},
            {"id": "1.2", "img": True, "marketDefinition": {"status": "OPEN"}, "rc": []}], ct="SUB_IMAGE"))
        receipt = self.run_recorder(fx.handshake() + [no_definition])
        info = receipt["segments"][0]
        self.assertEqual(info["replay_check"]["status"], "failed")
        self.assertEqual(info["replay_check"]["error"], "markets_without_initial_definition")
        self.assertEqual(info["markets_without_initial_definition"], ["1.1", "1.2"])
        with self.assertRaisesRegex(ValueError, "starts without marketTime"):
            stream.replay_snapshots(self.dir / "seg-000.jsonl", dict(
                countries=["IE"], market_type="WIN", decision_before_start_s=120, lookback_s=60, horizon_s=30,
                delays_s=[0], max_snapshot_age_s=90))
        # The data itself is intact, so the recording still verifies.
        self.assertEqual(verify_recording(self.dir)["status"], "verified")

    def test_replay_check_requires_market_time_python_3_10_parses(self):
        """3.10's fromisoformat (temporal.timestamp_ms) takes 0, 3 or 6 fraction digits, whatever this
        interpreter accepts, so replay_check must not pass a marketTime like 12:00:00.5Z."""
        odd = fx.market_definition(marketTime="2020-01-01T12:00:00.5Z")
        receipt = self.run_recorder(fx.handshake() + [sub(lambda sid: fx.image(sid, fx.pt_at(-300), definition=odd))])
        info = receipt["segments"][0]
        self.assertEqual((info["replay_check"]["status"], info["replay_check"]["error"]),
                         ("failed", "markets_without_initial_definition"))
        self.assertEqual(info["markets_without_initial_definition"], ["1.1"])
        # A later definition is parsed by the replay readers too (schedule changes).
        self.clock.advance(2)
        receipt = self.run_recorder(self.simple(delta_at(-299, [], marketDefinition=odd)), seed=12)
        info = receipt["segments"][0]
        self.assertEqual((info["replay_check"]["status"], info["replay_check"]["error"]),
                         ("failed", "market_time_not_replayable"))
        self.assertEqual(info["markets_without_initial_definition"], [])

    def test_max_bytes_stops_without_delete(self):
        big = [delta_at(-299 + n, [{"id": 11, "ltp": 2.2}], pad="x" * 450_000) for n in range(3)]
        receipt = self.run_recorder(self.simple(*big), overrides={"recorder": {"max_bytes": 1_000_000}})
        self.assertEqual(receipt["stop_reason"], "max_bytes_reached")
        info = receipt["segments"][0]
        self.assertEqual((info["lines"], info["close_reason"]), (3, "max_bytes_reached"))
        written = (self.dir / "seg-000.jsonl").stat().st_size + (self.dir / "seg-000.recv.jsonl").stat().st_size
        self.assertLessEqual(written, 1_000_000)
        self.assertEqual(len(list(stream.messages(self.dir / "seg-000.jsonl"))), 3)
        self.assertEqual(verify_recording(self.dir)["status"], "verified")

    def test_delayed_key_receipt_labels_conflation(self):
        receipt = self.run_recorder(self.simple(conflate_ms=180000))
        self.assertEqual(receipt["key_type"], "delayed")
        freshness = receipt["data_freshness"]
        self.assertEqual({k: freshness[k] for k in ("conflate_ms_requested", "conflate_ms_echoed",
                                                    "heartbeat_ms_echoed", "label")},
                         {"conflate_ms_requested": None, "conflate_ms_echoed": 180000, "heartbeat_ms_echoed": 5000,
                          "label": "delayed_conflated"})
        self.assertIn("delayed key", freshness["note"])
        self.assertIn("delayed-key data unsuitable for sub-180s features", receipt["limitations"])
        live = {"stream": {"app_key_slot": "live"}, "recorder": {"allow_live_key": True}}
        for seed, (echo, label) in enumerate([(0, "live_unconflated"), (500, "live_conflated")], 30):
            with self.subTest(echo=echo):
                self.clock.advance(2)
                receipt = self.run_recorder(self.simple(conflate_ms=echo), overrides=live,
                                            session=fx.FakeSession(slot="live"), seed=seed)
                self.assertEqual((receipt["key_type"], receipt["data_freshness"]["label"]), ("live", label))
        self.clock.advance(2)
        receipt = self.run_recorder([fx.handshake()], overrides=live, session=fx.FakeSession(slot="live"), seed=40)
        self.assertEqual(receipt["data_freshness"]["label"], "live_conflated")    # nothing echoed: claim less

    def test_live_key_refused_without_allow(self):
        with self.assertRaisesRegex(ConfigError, "stream_live_key_needs_recorder_allow_live_key"):
            fx.recorder_config(self.paths, {"stream": {"app_key_slot": "live"}})
        with self.assertRaisesRegex(ConfigError, "recorder_live_key_not_allowed"):
            self.recorder([self.simple()], session=fx.FakeSession(slot="live"))
        with self.assertRaisesRegex(ConfigError, "does_not_match"):
            self.recorder([self.simple()], overrides={"stream": {"app_key_slot": "live"},
                                                      "recorder": {"allow_live_key": True}})
        self.assertEqual(self.server.calls, [])
        self.assertFalse(any(self.paths.recordings_dir.iterdir()))

    def test_no_secret_in_any_output_file(self):
        self.run_recorder([self.simple(fx.EOF), fx.handshake() + [image_at(-200)]])
        wire = self.server.all_sent()
        secrets = [support.PLANTED_SECRET.encode(), fx.APP_KEY.encode()]
        for secret in secrets:
            self.assertEqual(wire.count(secret), 2)                  # one authentication line per connection
        files = [p for p in self.paths.root.rglob("*") if p.is_file()]
        self.assertGreaterEqual(len(files), 8)
        for path in files:
            content = path.read_bytes()
            for secret in secrets:
                self.assertNotIn(secret, content, path.name)
        emitted = json.dumps(self.events, sort_keys=True).encode()
        for secret in secrets:
            self.assertNotIn(secret, emitted)

    def test_secret_in_stream_data_never_written(self):
        secret = support.PLANTED_SECRET
        status = sub(lambda sid: {"op": "status", "id": sid, "statusCode": "SUCCESS", "connectionClosed": False,
                                  "errorCode": secret})
        leak = delta_at(-299, [{"id": 11, "ltp": 2.2}], pad=secret)
        recorder = self.recorder([self.simple(status, leak, delta_at(-298, [{"id": 11, "ltp": 2.4}]))])
        with self.assertRaises(SecretLeak) as caught:
            recorder.run(stop_requested=self.server.exhausted)
        self.assertNotIn(secret, str(caught.exception))
        (directory,) = [p for p in self.paths.recordings_dir.iterdir() if p.is_dir()]
        files = [p for p in self.paths.root.rglob("*") if p.is_file()]
        self.assertTrue(files)
        for path in files:
            self.assertNotIn(secret.encode(), path.read_bytes(), path.name)
        self.assertFalse((directory / "receipt.json").exists())
        partial = strict_json_loads((directory / "receipt.partial.json").read_text())
        self.assertIs(partial["complete"], False)
        lines = (directory / "seg-000.jsonl").read_bytes().splitlines()
        self.assertEqual(len(lines), 1)                               # the image only; the leak line never
        self.assertIn("logout", self.session.calls)
        statuses = [json.loads(t) for t in (directory / "events.jsonl").read_text().splitlines()
                    if json.loads(t)["event"] == "status_message"]
        self.assertEqual(statuses[-1]["error_code"], "malformed_code")

    def test_stream_factory_must_use_gated_session_and_config(self):
        """The live-key gate and the receipt's key and endpoint describe self.session and the config, so a
        client built on anything else is refused before it connects."""
        live = fx.FakeSession(slot="live", app_key="LIVE-synthetic-app-key-0002")
        cases = {
            "session": lambda: fx.make_client(self.server, live, self.clock),
            "host": lambda: fx.make_client(self.server, self.session, self.clock,
                                           host="stream-api-integration.betfair.com"),
            "heartbeat_ms": lambda: fx.make_client(self.server, self.session, self.clock, heartbeat_ms=1000),
            "conflate_ms": lambda: fx.make_client(self.server, self.session, self.clock, conflate_ms=0),
            "client_type": lambda: object(),
        }
        for seed, (field, factory) in enumerate(sorted(cases.items()), 80):
            with self.subTest(field=field):
                self.clock.advance(2)
                self.session.calls.clear()
                receipt = self.run_recorder([self.simple()], stream_factory=factory, seed=seed)
                self.assertEqual(receipt["stop_reason"], f"stream_factory_mismatch:{field}")
                self.assertTrue(receipt["complete"])
                self.assertEqual((self.server.calls, receipt["segments"]), ([], []))
                self.assertEqual(self.event_rows("stream_factory_refused")[0]["field"], field)
                self.assertEqual(self.session.calls[-1], "logout")
        self.assertNotIn("token", live.calls)
        self.assertNotIn("app_key", live.calls)

    def test_existing_session_dir_refused(self):
        suffix = format(random.Random(7).getrandbits(32), "08x")
        taken = self.paths.recordings_dir / f"rec-{utc_compact_from_ns(self.clock.time_ns())}-{suffix}"
        taken.mkdir(parents=True)
        (taken / "keep.txt").write_text("existing")
        recorder = self.recorder([self.simple()])
        with self.assertRaisesRegex(ConfigError, "recording_session_dir_exists"):
            recorder.run(stop_requested=self.server.exhausted)
        self.assertEqual(sorted(p.name for p in taken.iterdir()), ["keep.txt"])
        self.assertEqual((self.lister.calls, self.server.calls), ([], []))

    def test_crash_leaves_partial_receipt_incomplete(self):
        def emit(event):
            self.events.append(event)
            if event["event"] == "segment_opened" and event["segment"] == 1:
                raise RuntimeError("simulated crash")
        recorder = self.recorder([self.simple(delta_at(-299, [{"id": 11, "ltp": 2.2}]), fx.EOF),
                                  fx.handshake() + [image_at(-200)]], emit=emit)
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            recorder.run(stop_requested=self.server.exhausted)
        (directory,) = [p for p in self.paths.recordings_dir.iterdir() if p.is_dir()]
        self.assertFalse((directory / "receipt.json").exists())
        partial = strict_json_loads((directory / "receipt.partial.json").read_text())
        self.assertIs(partial["complete"], False)
        self.assertEqual((len(partial["segments"]), partial["segments"][0]["lines"]), (1, 2))
        self.assertFalse((self.paths.recordings_dir / "run-ledger.jsonl").exists())
        self.assertIn("logout", self.session.calls)
        self.assertTrue(self.server.sockets[-1].closed)
        with file_lock(self.paths.recorder_lock):                    # the lock was released
            pass
        result = verify_recording(directory)
        self.assertEqual(result["status"], "failed")
        self.assertIn("partial", result["checks"][0]["detail"])

    def test_verify_recording_detects_tamper(self):
        self.run_recorder(self.simple(delta_at(-299, [{"id": 11, "ltp": 2.2}])))
        self.assertEqual(verify_recording(self.dir)["status"], "verified")
        segment, recv, receipt_path = (self.dir / "seg-000.jsonl", self.dir / "seg-000.recv.jsonl",
                                       self.dir / "receipt.json")
        originals = {p: p.read_bytes() for p in (segment, recv, receipt_path)}

        def failed(path, data):
            path.write_bytes(data)
            result = verify_recording(self.dir)
            path.write_bytes(originals[path])
            self.assertEqual(result["status"], "failed")
            return {c["check"] for c in result["checks"] if c["status"] == "failed"}
        self.assertIn("segment_sha256", failed(segment, originals[segment].replace(b"2.2", b"2.3")))
        self.assertIn("recv_sha256", failed(recv, originals[recv].replace(b'"line":2', b'"line":3')))
        self.assertIn("recv_rows_contiguous", failed(recv, originals[recv].replace(b'"line":2', b'"line":3')))
        receipt = json.loads(originals[receipt_path])
        receipt["segments"][0]["last_pt"] += 1
        self.assertEqual(failed(receipt_path, json.dumps(receipt).encode()), {"pt_bounds"})
        self.assertIn("receipt_parses", failed(receipt_path, originals[receipt_path].replace(b"{", b"{NaN", 1)))
        receipt = json.loads(originals[receipt_path])
        receipt["segments"][0]["path"] = "../seg-000.jsonl"
        self.assertIn("segment_paths", failed(receipt_path, json.dumps(receipt).encode()))
        self.assertEqual(verify_recording(self.dir)["status"], "verified")

    def test_ledger_appended(self):
        first = self.run_recorder([self.simple()])
        first_dir = self.dir
        self.clock.advance(2)
        self.run_recorder([self.simple(), ], seed=11)
        rows = read_jsonl(self.paths.recordings_dir / "run-ledger.jsonl")
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row), {"created_at", "session_id", "receipt_path", "receipt_sha256", "segments",
                                        "status"})
            self.assertEqual(row["receipt_sha256"], file_sha256(row["receipt_path"]))
            self.assertEqual((row["segments"], row["status"]), (1, RECEIPT_STATUS))
        self.assertEqual(rows[0]["session_id"], first["session_id"])
        self.assertEqual(Path(rows[0]["receipt_path"]), first_dir / "receipt.json")
        self.assertEqual(os.stat(self.paths.recordings_dir / "run-ledger.jsonl").st_mode & 0o777, 0o600)

    def test_ledger_stays_in_profile_when_root_overridden(self):
        """§8.4: recordings/run-ledger.jsonl under the profile, wherever recorder.root puts the sessions."""
        other = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        receipt = self.recorder([self.simple()], overrides={"recorder": {"root": str(other)}}).run(
            stop_requested=self.server.exhausted)
        session_dir = other / receipt["session_id"]
        self.assertTrue((session_dir / "receipt.json").is_file())
        self.assertFalse((other / "run-ledger.jsonl").exists())
        rows = read_jsonl(self.paths.recordings_dir / "run-ledger.jsonl")
        self.assertEqual([r["session_id"] for r in rows], [receipt["session_id"]])
        self.assertEqual(Path(rows[0]["receipt_path"]), session_dir / "receipt.json")
        self.assertEqual(rows[0]["receipt_sha256"], file_sha256(session_dir / "receipt.json"))

    def test_receipt_write_failure_still_logs_out(self):
        real = recorder_module.atomic_write_json

        def disk_full(path, *args, **kwargs):
            if Path(path).name == "receipt.json":
                raise OSError(28, "No space left on device")
            return real(path, *args, **kwargs)
        recorder = self.recorder([self.simple()])
        with patch.object(recorder_module, "atomic_write_json", disk_full):
            with self.assertRaises(OSError):
                recorder.run(stop_requested=self.server.exhausted)
        self.assertEqual(self.session.calls[-1], "logout")
        self.assertEqual(self.session.calls.count("logout"), 1)
        self.assertFalse((self.paths.recordings_dir / "run-ledger.jsonl").exists())
        with file_lock(self.paths.recorder_lock):                    # the lock was released
            pass

    def test_progress_events_emitted(self):
        self.run_recorder([self.simple(fx.wait(65), delta_at(-200, [{"id": 11, "ltp": 2.2}]), fx.EOF,
                                       conflate_ms=180000),
                           fx.handshake() + [image_at(-100)]])
        names = [e["event"] for e in self.events]
        for name in ("segment_opened", "segment_closed", "heartbeat_at", "gap", "stopped"):
            self.assertIn(name, names)
        self.assertEqual(names[-1], "stopped")
        self.assertEqual(self.events[-1]["reason"], "stop_requested")
        for event in self.events:
            json.dumps(event, sort_keys=True, allow_nan=False)
            parse_utc_ns(event["at"])

    def test_progress_and_keepalive_continue_during_backoff(self):
        """A stream_backoff wait can last up to an hour: heartbeat_at keeps coming every 60 s and the
        session keepAlive keeps running (a 20-minute it/es session must not lapse)."""
        def too_many():
            return [fx.connection_line(), fx.reply("authentication", status_code="FAILURE",
                                                   errorCode="MAX_CONNECTION_LIMIT_EXCEEDED", connectionClosed=True)]
        session = fx.FakeSession(clock=self.clock)
        started = self.clock.mono_ns
        receipt = self.run_recorder([too_many() for _ in range(4)] + [self.simple()], session=session)
        elapsed_s = (self.clock.mono_ns - started) / 1e9
        self.assertGreaterEqual(elapsed_s, 60 + 120 + 240 + 480)
        self.assertEqual(len(receipt["segments"]), 1)
        beats = [e for e in self.events if e["event"] == "heartbeat_at"]
        self.assertGreaterEqual(len(beats), int(elapsed_s // 60) - 1)
        self.assertTrue(all(b["segment"] is None for b in beats[:-1]))
        gaps = [(b - a) / 1e9 for a, b in zip(session.keep_alive_at_ns, session.keep_alive_at_ns[1:])]
        self.assertLessEqual(max(gaps), 2.0)

    def test_keepalive_fail_result_throttled(self):
        """Regression: SessionManager.keep_alive() returns {"status": "FAIL", ...} without raising and stays
        due, so the recorder must treat that result as a failure: one event, then at most one try a minute."""
        beats = [item for i in range(150)
                 for item in (fx.wait(1), sub(lambda sid, i=i: fx.heartbeat(sid, fx.pt_at(-299 + i))))]
        cases = [("returned", {"keep_alive_result": {"status": "FAIL", "error": "INTERNAL_ERROR"}}, "INTERNAL_ERROR"),
                 ("returned_no_error", {"keep_alive_result": {"status": "FAIL", "error": None}}, "FAIL"),
                 ("raised", {"keep_alive_error": AuthError("INPUT_VALIDATION_ERROR")}, "INPUT_VALIDATION_ERROR")]
        for seed, (name, options, code) in enumerate(cases, 90):
            with self.subTest(case=name):
                self.clock.advance(2)
                session = fx.FakeSession(clock=self.clock, **options)
                receipt = self.run_recorder(self.simple(*beats), session=session, seed=seed)
                self.assertEqual(receipt["segments"][0]["lines"], 151)
                times = session.keep_alive_at_ns
                self.assertIn(len(times), (3, 4))                       # ~150 s: at 0, 60 and 120 s
                for earlier, later in zip(times, times[1:]):
                    self.assertGreaterEqual(later - earlier, 60 * 1_000_000_000)
                failed = self.event_rows("keepalive_failed")
                self.assertEqual(len(failed), len(times))
                self.assertEqual({r["code"] for r in failed}, {code})
        # A SUCCESS result is not a failure: no event, and the call is not throttled.
        self.clock.advance(2)
        session = fx.FakeSession(clock=self.clock, keep_alive_result={"status": "SUCCESS", "error": None})
        self.run_recorder(self.simple(*beats[:20]), session=session, seed=99)
        self.assertEqual(self.event_rows("keepalive_failed"), [])
        self.assertGreater(len(session.keep_alive_at_ns), 10)

    def test_echo_out_of_range_recorded_not_adopted(self):
        receipt = self.run_recorder(self.simple(heartbeat_ms=600000, conflate_ms=3600000))
        rows = self.event_rows("echo_out_of_range")
        self.assertEqual([(r["heartbeat_ms"], r["conflate_ms"]) for r in rows], [(600000, 3600000)])
        self.assertEqual(self.event_rows("echo"), [])
        self.assertEqual((receipt["data_freshness"]["heartbeat_ms_echoed"],
                          receipt["data_freshness"]["conflate_ms_echoed"]), (None, None))

    # --- errors, budgets and locks --------------------------------------------------------------

    def test_stream_errors_follow_the_table(self):
        def failing(code, closed=True):
            return [fx.connection_line(), fx.reply("authentication", status_code="FAILURE", errorCode=code,
                                                   connectionClosed=closed)]
        cases = [("INVALID_APP_KEY", "app_key:INVALID_APP_KEY"), ("NOT_AUTHORIZED", "stream_fatal:NOT_AUTHORIZED"),
                 ("SOMETHING_NEW", "unrecognized:SOMETHING_NEW")]
        for seed, (code, reason) in enumerate(cases, 50):
            with self.subTest(code=code):
                self.clock.advance(2)
                receipt = self.run_recorder([failing(code), self.simple()], seed=seed)
                self.assertEqual(receipt["stop_reason"], reason)
                self.assertEqual(len(self.server.calls), 1)
        # A session error re-logs in once and reconnects.
        self.clock.advance(2)
        session = fx.FakeSession()
        receipt = self.run_recorder([failing("NO_SESSION"), self.simple()], session=session, seed=60)
        self.assertEqual(session.calls.count("invalidate_and_relogin"), 1)
        self.assertEqual((len(self.server.calls), len(receipt["segments"])), (2, 1))
        # Too many connections: wait at least 60 s before trying again.
        self.clock.advance(2)
        started = self.clock.mono_ns
        receipt = self.run_recorder([failing("MAX_CONNECTION_LIMIT_EXCEEDED"), self.simple()], seed=61)
        self.assertEqual(len(receipt["segments"]), 1)
        self.assertGreaterEqual((self.clock.mono_ns - started) / 1e9, 60)

    def test_reconnect_budget_and_transport_refusals(self):
        overrides = {"stream": {"max_reconnects_per_hour": 2}}
        receipt = self.run_recorder([[fx.EOF]] * 5, overrides=overrides)
        self.assertEqual(receipt["stop_reason"], "reconnect_budget_exhausted")
        self.assertEqual(len(self.server.calls), 3)
        # The real socket factory refuses under the forbid-network switch: the session stops at once.
        self.clock.advance(2)
        receipt = self.run_recorder([], seed=70, stop=lambda: False, stream_factory=lambda: StreamClient(
            host=fx.STREAM_HOST, port=443, session=self.session, clock=self.clock))
        self.assertEqual((receipt["stop_reason"], receipt["segments"]), ("transport:network_forbidden", []))

    def test_lister_failure_stops_before_connecting(self):
        def broken(rest_filter):
            raise ApiError("TOO_MANY_REQUESTS", surface="aping")
        receipt = self.run_recorder([self.simple()], market_lister=broken)
        self.assertEqual(receipt["stop_reason"], "market_lister_failed:TOO_MANY_REQUESTS")
        self.assertEqual(self.server.calls, [])
        self.assertTrue(receipt["complete"])

    def test_second_recorder_refused_by_lock(self):
        recorder = self.recorder([self.simple()])
        with file_lock(self.paths.recorder_lock):
            with self.assertRaises(LockBusy):
                recorder.run(stop_requested=self.server.exhausted)
        self.assertEqual(self.server.calls, [])

    def test_max_seconds_and_receipt_shape(self):
        receipt = self.run_recorder(self.simple(fx.wait(30), delta_at(-200, [{"id": 11, "ltp": 2.2}]),
                                                conflate_ms=180000), max_seconds=10)
        self.assertEqual(receipt["stop_reason"], "max_seconds_reached")
        self.assertEqual(receipt["segments"][0]["close_reason"], "max_seconds_reached")
        for key, value in {"schema_version": 1, "status": RECEIPT_STATUS, "complete": True,
                           "data_kind": "betfair_exchange_stream", "endpoint": "stream-api.betfair.com:443",
                           "coverage": "continuous_within_segment_only_gaps_listed", "economic_result": None}.items():
            self.assertEqual(receipt[key], value)
        self.assertEqual(receipt["subscription"]["segmentation_enabled"], False)
        self.assertEqual(set(receipt["timestamp_provenance"]), {"received_at", "monotonic_ns", "pt"})
        self.assertIn("betangel_research/betfair/recorder.py", receipt["code_sha256"])
        on_disk = strict_json_loads((self.dir / "receipt.json").read_text())
        self.assertEqual(on_disk, receipt)
        self.assertEqual(oct(self.dir.stat().st_mode & 0o777), oct(0o700))
        self.assertEqual(oct((self.dir / "seg-000.jsonl").stat().st_mode & 0o777), oct(0o600))


if __name__ == "__main__":
    unittest.main()
