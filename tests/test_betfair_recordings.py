"""recordings index and verify-day (recording design v3 section 6, v4 section F, v5): offline, read only, on
synthetic sessions only. A small writer below lays out sessions exactly as the recorder does (segments, receipt
sidecars, receipts, run-ledger rows) so coverage cases can be placed to the second; one test also indexes the
recorder's own output. Every runner, price and BSP value is a sentinel that must never reach any output."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import random
import tempfile
import unittest

from betangel_research.betfair import recordings
from betangel_research.betfair.cli import main
from betangel_research.betfair.paths import StatePaths
from betangel_research.betfair.receipts import append_jsonl, atomic_write_json, parse_utc_ns, utc_from_ns
from betangel_research.betfair.recorder import PRECOUNT_NAME, RECEIPT_STATUS, StreamRecorder
from betangel_research.betfair.recordings import CLASSES, build_index, expected_gaps, verify_day

import test_betfair_support as support
import test_betfair_support_stream as fx

DAY = "2026-10-21"
SENTINELS = ("SENTINEL-RUNNER-NAME", "777.777001", "9.87654321", "123456.789", "4321.0987", "987654321",
             "SENTINEL-VENUE")
RC_SENTINEL = [{"id": 987654321, "batb": [[0, 9.87654321, 4321.0987]], "batl": [[0, 9.87654321, 4321.0987]],
                "ltp": 9.87654321, "tv": 123456.789, "trd": [[9.87654321, 123456.789]], "spn": 777.777001}]
# Every key the index file may hold, at any depth.
INDEX_KEYS = {"schema_version", "status", "day", "built_at", "time_note", "scan", "from", "to", "classes_by_precedence",
              "sessions", "session_id", "key_type", "label", "segments", "verified_segments", "open_segments",
              "precount", "markets", "market_id", "event_id", "class", "flags", "anchor", "anchor_source",
              "catalogue_start", "window", "first_image", "first_inplay_publish", "closed_publish", "segment", "line",
              "received_at", "publish_pt", "late_start_s", "gap_s", "coverage", "first_line", "last_line", "image",
              "verified", "stalls", "gaps", "seconds", "cause", "tolerated"}


def t(clock_time, day=DAY):
    return parse_utc_ns(f"{day}T{clock_time}Z")


def definition(market_time="14:00:00", day=DAY, **changes):
    value = {"marketTime": f"{day}T{market_time}.000Z" if market_time else None, "status": "OPEN", "inPlay": False,
             "eventId": "30001", "bspReconciled": True, "venue": "SENTINEL-VENUE",
             "runners": [{"id": 987654321, "name": "SENTINEL-RUNNER-NAME", "status": "WINNER", "bsp": 777.777001}]}
    if value["marketTime"] is None:
        del value["marketTime"]
    value.update(changes)
    return value


def image(at, *markets):
    """A SUB_IMAGE line: markets are (market_id, definition) pairs."""
    return at, {"op": "mcm", "id": 1, "pt": at // 1_000_000 - 1000, "clk": "c", "ct": "SUB_IMAGE",
                "mc": [{"id": mid, "img": True, "marketDefinition": d, "rc": RC_SENTINEL} for mid, d in markets]}


def change(at, market_id, **definition_changes):
    entry = {"id": market_id, "rc": RC_SENTINEL}
    if definition_changes:
        entry["marketDefinition"] = definition(**definition_changes)
    return at, {"op": "mcm", "id": 1, "pt": at // 1_000_000 - 1000, "clk": "c", "mc": [entry]}


def beats(start, end, step=120):
    """Heartbeat lines every step seconds in (start, end]."""
    out, at = [], start + step * 10**9
    while at <= end:
        out.append((at, {"op": "mcm", "id": 1, "pt": at // 1_000_000 - 1000, "clk": "c", "ct": "HEARTBEAT"}))
        at += step * 10**9
    return out


def seg(lines, mode="coarse", close="scheduled_refresh"):
    return {"lines": sorted(lines, key=lambda item: item[0]), "mode": mode, "close": close}


class SessionWriter:
    """Writes sessions laid out as the recorder writes them; hashes and gaps follow its rules exactly."""

    def __init__(self, paths, root=None):
        self.paths = paths
        self.root = Path(root or paths.recordings_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def segment(self, directory, index, spec):
        name, recv_name = f"seg-{index:03d}.jsonl", f"seg-{index:03d}.recv.jsonl"
        data, rows, offset, ids = b"", b"", 0, set()
        for number, (at, msg) in enumerate(spec["lines"], 1):
            raw = json.dumps(msg, separators=(",", ":")).encode()
            rows += (json.dumps({"line": number, "byte_offset": offset, "bytes": len(raw),
                                 "sha256": hashlib.sha256(raw).hexdigest(), "received_at": utc_from_ns(at),
                                 "monotonic_ns": at, "pt": msg["pt"], "clk": msg.get("clk")}, sort_keys=True,
                                separators=(",", ":")) + "\n").encode()
            data += raw + b"\n"
            offset += len(raw) + 1
            ids.update(change_["id"] for change_ in msg.get("mc", []))
        (directory / name).write_bytes(data)
        (directory / recv_name).write_bytes(rows)
        lines = spec["lines"]
        return {"index": index, "path": name, "recv_path": recv_name, "sha256": hashlib.sha256(data).hexdigest(),
                "recv_sha256": hashlib.sha256(rows).hexdigest(), "bytes": len(data), "lines": len(lines),
                "first_pt": lines[0][1]["pt"] if lines else None, "last_pt": lines[-1][1]["pt"] if lines else None,
                "first_received_at": utc_from_ns(lines[0][0]) if lines else None,
                "last_received_at": utc_from_ns(lines[-1][0]) if lines else None, "market_ids": sorted(ids),
                "connection_id": "c-1", "subscription_mode": spec["mode"], "requested_count": len(ids),
                "requested_market_ids_sha256": None, "close_reason": spec["close"], "superseded_dropped": 0,
                "max_inter_line_gap_ms": 0, "heartbeat_lines": 0, "con_true_lines": 0, "status_503_lines": 0,
                "img_market_changes": 0, "markets_without_initial_definition": [],
                "replay_check": {"reader": "betangel_research.stream.messages", "status": "passed", "error": None}}

    def session(self, start, segments, *, crash_open=None, ledger=True, precount=None, conflate_ms=180000,
                events=True):
        """Write one session starting at start (ns). crash_open: a segment spec left open by a crash (only
        receipt.partial.json is written). precount: [(segment, [(market_id, start text or None)])]."""
        session_id = f"rec-{utc_from_ns(start)[:19].replace('-', '').replace(':', '')}Z-{random.getrandbits(32):08x}"
        directory = self.root / session_id
        directory.mkdir(mode=0o700)
        infos = [self.segment(directory, index, spec) for index, spec in enumerate(segments)]
        receipt = {"schema_version": 1, "status": RECEIPT_STATUS, "complete": crash_open is None,
                   "session_id": session_id, "created_at": utc_from_ns(start), "key_type": "delayed",
                   "closed_at": None if crash_open is not None else max(
                       [i["last_received_at"] for i in infos if i["last_received_at"]] + [utc_from_ns(start)]),
                   "stop_reason": None if crash_open is not None else "max_seconds_reached",
                   "data_freshness": {"conflate_ms_requested": None, "conflate_ms_echoed": conflate_ms,
                                      "heartbeat_ms_echoed": 5000, "label": "delayed_conflated", "note": "synthetic"},
                   "segments": infos, "gaps": expected_gaps(infos), "economic_result": None}
        if precount:
            for index, markets in precount:
                append_jsonl(directory / PRECOUNT_NAME, {
                    "segment": index, "taken_at": utc_from_ns(start), "lookback_s": 1800, "count": len(markets),
                    "possibly_more": False, "subscription_mode": "coarse", "requested_count": len(markets),
                    "markets": [{"market_id": m, "market_start_time": s} for m, s in markets]})
            receipt["precount"] = {"path": PRECOUNT_NAME, "rows": len(precount),
                                   "sha256": hashlib.sha256((directory / PRECOUNT_NAME).read_bytes()).hexdigest()}
        if events:
            append_jsonl(directory / "events.jsonl", {"event": "stopped", "at": utc_from_ns(start)})
        if crash_open is not None:
            self.segment(directory, len(segments), crash_open)
            atomic_write_json(directory / "receipt.partial.json", receipt)
            return directory
        digest = atomic_write_json(directory / "receipt.json", receipt)
        if ledger:
            append_jsonl(self.paths.recordings_dir / "run-ledger.jsonl", {
                "created_at": utc_from_ns(start), "session_id": session_id,
                "receipt_path": str(directory / "receipt.json"), "receipt_sha256": digest, "segments": len(infos),
                "status": RECEIPT_STATUS})
        return directory


class IndexTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        random.seed(1)
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock(start=f"{DAY}T23:00:00.000000Z")
        self.writer = SessionWriter(self.paths)

    def index(self, day=DAY):
        summary = build_index(self.paths, root=self.paths.recordings_dir, day=day, clock=self.clock)
        document = json.loads((self.paths.recordings_index_dir / f"{day}.json").read_text())
        self.assertEqual(hashlib.sha256((self.paths.recordings_index_dir / f"{day}.json").read_bytes()).hexdigest(),
                         summary["index_sha256"])
        return summary, {row["market_id"]: row for row in document["markets"]}, document

    def assert_no_sentinel(self, text):
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)

    def test_complete_no_close_and_day_assignment(self):
        lines = ([image(t("12:30:00"), ("1.101", definition("14:00:00")), ("1.102", definition("14:00:00", day="2026-10-22")),
                        ("1.103", definition("15:00:00")))] + beats(t("12:30:00"), t("14:20:00"))
                 + [change(t("14:02:30"), "1.101", inPlay=True), change(t("14:06:30"), "1.101", status="CLOSED", inPlay=True)])
        directory = self.writer.session(t("12:30:00"), [seg(lines, close="max_seconds_reached")])
        summary, rows, document = self.index()
        self.assertEqual(set(rows), {"1.101", "1.103"})                       # 1.102 races on the next day
        complete = rows["1.101"]
        self.assertEqual((complete["class"], complete["flags"]), ("COMPLETE", []))
        self.assertEqual(complete["window"], {"from": "2026-10-21T13:00:00.000000Z", "to": "2026-10-21T14:06:30.000000Z"})
        self.assertEqual(complete["first_image"]["line"], 1)
        self.assertEqual(complete["first_image"]["session_id"], directory.name)
        self.assertEqual(complete["closed_publish"]["received_at"], "2026-10-21T14:06:30.000000Z")
        self.assertEqual(complete["first_inplay_publish"]["publish_pt"], t("14:02:30") // 1_000_000 - 1000)
        self.assertEqual((complete["coverage"][0]["first_line"], complete["coverage"][0]["verified"]), (1, True))
        self.assertEqual((complete["event_id"], complete["anchor_source"]), ("30001", "market_definition"))
        no_close = rows["1.103"]
        self.assertEqual((no_close["class"], no_close["flags"]), ("NO_CLOSE", ["no_close"]))
        self.assertEqual(no_close["window"]["to"], "2026-10-21T14:20:00.000000Z")      # the session's end
        self.assertEqual(summary["classes"]["COMPLETE"], 1)
        self.assertEqual(summary["classes"]["NO_CLOSE"], 1)
        self.assertEqual((summary["sessions"], summary["segments"], summary["segments_verified"]), (1, 1, 1))
        self.assertEqual(document["classes_by_precedence"], list(CLASSES))
        self.assertEqual(oct((self.paths.recordings_index_dir / f"{DAY}.json").stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(self.paths.recordings_index_dir.stat().st_mode & 0o777), oct(0o700))

    def test_late_start(self):
        lines = ([image(t("13:30:00"), ("1.101", definition("14:00:00")))] + beats(t("13:30:00"), t("14:10:00"))
                 + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED")])
        self.writer.session(t("13:30:00"), [seg(lines)])
        row = self.index()[1]["1.101"]
        self.assertEqual((row["class"], row["flags"], row["late_start_s"]), ("LATE_START", ["late_start"], 1800.0))
        # A market whose whole window came after the recording ended is all late start.
        self.writer = SessionWriter(self.paths)
        lines = [image(t("08:00:00"), ("1.150", definition("20:00:00")))] + beats(t("08:00:00"), t("09:00:00"))
        self.writer.session(t("08:00:00"), [seg(lines)])
        row = self.index()[1]["1.150"]
        self.assertEqual((row["class"], set(row["flags"])), ("LATE_START", {"late_start", "no_close", "never_in_window"}))
        self.assertEqual((row["window"], row["late_start_s"]),
                         ({"from": "2026-10-21T19:00:00.000000Z", "to": "2026-10-21T20:00:00.000000Z"}, 3600.0))

    def test_market_whose_sessions_ended_before_its_window_is_never_hidden(self):
        """Workflow #6: seen only 09:00-10:00, off 16:00; a later explicit session 14:00-18:00 left it out. The
        window is the hour before the off, all of it late start, flagged never_in_window, and the explicit
        segment that left it out ranks it DROPPED_EXPLICIT (it used to be a zero-length 15:00-15:00 window)."""
        morning = [image(t("09:00:00"), ("1.160", definition("16:00:00")))] + beats(t("09:00:00"), t("10:00:00"))
        self.writer.session(t("09:00:00"), [seg(morning)])
        afternoon = [image(t("14:00:00"), ("1.161", definition("17:00:00")))] + beats(t("14:00:00"), t("18:00:00"))
        self.writer.session(t("14:00:00"), [seg(afternoon, mode="explicit")])
        row = self.index()[1]["1.160"]
        self.assertEqual(row["window"], {"from": "2026-10-21T15:00:00.000000Z", "to": "2026-10-21T16:00:00.000000Z"})
        self.assertEqual((row["class"], row["late_start_s"]), ("DROPPED_EXPLICIT", 3600.0))
        self.assertEqual(set(row["flags"]), {"dropped_explicit", "late_start", "never_in_window", "no_close"})
        # A market CLOSED before its window began gets no coverage figures, and is not called late.
        self.writer = SessionWriter(self.paths)
        early = ([image(t("06:00:00"), ("1.162", definition("16:00:00")))] + beats(t("06:00:00"), t("07:00:00"))
                 + [change(t("06:30:00"), "1.162", status="CLOSED", market_time="16:00:00")])
        self.writer.session(t("06:00:00"), [seg(early)])
        row = self.index()[1]["1.162"]
        self.assertEqual((row["class"], set(row["flags"])),
                         ("CLOSED_WITHOUT_OBSERVED_INPLAY", {"closed_before_window", "closed_without_observed_inplay"}))

    def test_reconnect_gap_tolerated_refresh_and_a_late_market_in_a_segment(self):
        seg0 = [image(t("12:30:00"), ("1.101", definition("14:00:00")), ("1.107", definition("14:00:00")))] + \
            beats(t("12:30:00"), t("13:20:00"))
        seg1 = ([image(t("13:20:02"), ("1.101", definition("14:00:00")))] + beats(t("13:20:02"), t("13:40:02"))
                + [change(t("13:30:00"), "1.107", status="OPEN")])                 # 1.107 back mid-segment, no image
        seg2 = ([image(t("13:45:00"), ("1.101", definition("14:00:00")), ("1.107", definition("14:00:00")))]
                + beats(t("13:45:00"), t("14:20:00"))
                + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED"),
                   change(t("14:02:00"), "1.107", inPlay=True), change(t("14:06:00"), "1.107", status="CLOSED")])
        self.writer.session(t("12:30:00"), [seg(seg0), seg(seg1, close="connection_lost"),
                                            seg(seg2, close="max_seconds_reached")])
        summary, rows, _ = self.index()
        row = rows["1.101"]
        self.assertEqual((row["class"], row["flags"]), ("GAP", ["gap"]))
        tolerated, gap = row["gaps"]
        self.assertEqual((tolerated["cause"], tolerated["tolerated"], tolerated["seconds"]), ("scheduled_refresh", True, 2.0))
        self.assertEqual((gap["cause"], gap["tolerated"], gap["seconds"]), ("connection_lost", False, 298.0))
        self.assertEqual(row["gap_s"], 298.0)
        late = rows["1.107"]                       # never inherits seg1's start: covered only from 13:30 there
        self.assertEqual(late["class"], "GAP")
        # Not in the next segment's image, so the refresh handover is not tolerated.
        self.assertIn({"from": "2026-10-21T13:20:00.000000Z", "to": "2026-10-21T13:30:00.000000Z", "seconds": 600.0,
                       "cause": "scheduled_refresh", "tolerated": False}, late["gaps"])
        self.assertEqual(late["coverage"][1]["from"], "2026-10-21T13:30:00.000000Z")
        self.assertEqual((summary["gaps"], summary["tolerated_handovers"]), (3, 1))

    def test_slow_refresh_handover_is_a_gap(self):
        seg0 = [image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("13:20:00"))
        seg1 = ([image(t("13:24:00"), ("1.101", definition("14:00:00")))] + beats(t("13:24:00"), t("14:10:00"))
                + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED")])
        self.writer.session(t("12:30:00"), [seg(seg0), seg(seg1)])
        row = self.index()[1]["1.101"]
        self.assertEqual((row["class"], row["gaps"][0]["cause"], row["gaps"][0]["tolerated"]),
                         ("GAP", "scheduled_refresh", False))

    def test_dropped_explicit(self):
        seg0 = [image(t("12:30:00"), ("1.101", definition("14:00:00")), ("1.108", definition("16:00:00")))] + \
            beats(t("12:30:00"), t("13:30:00"))
        seg1 = [image(t("13:30:01"), ("1.108", definition("16:00:00")))] + beats(t("13:30:01"), t("14:20:00"))
        self.writer.session(t("12:30:00"), [seg(seg0, mode="explicit"), seg(seg1, mode="explicit")])
        row = self.index()[1]["1.101"]
        self.assertEqual(row["class"], "DROPPED_EXPLICIT")
        self.assertEqual(set(row["flags"]), {"dropped_explicit", "gap", "no_close"})
        self.assertEqual(row["gaps"][0]["cause"], "dropped_explicit")

    def test_stall_inside_a_segment(self):
        lines = ([image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("13:10:00"))
                 + beats(t("13:48:00"), t("14:10:00"))
                 + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED")])
        self.writer.session(t("12:30:00"), [seg(lines)])
        row = self.index()[1]["1.101"]
        self.assertEqual((row["class"], row["gaps"][0]["cause"]), ("GAP", "stall_in_segment"))
        self.assertEqual(row["gaps"][0]["seconds"], 2400.0)
        self.assertEqual(row["coverage"][0]["stalls"], 1)

    def test_between_sessions_and_an_unverified_crash(self):
        first = [image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("13:20:00"))
        self.writer.session(t("12:30:00"), [seg(first)])
        crashed = ([image(t("13:30:00"), ("1.101", definition("14:00:00")))] + beats(t("13:30:00"), t("14:10:00"))
                   + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED")])
        self.writer.session(t("13:30:00"), [], crash_open=seg(crashed))
        summary, rows, document = self.index()
        row = rows["1.101"]
        self.assertEqual(row["class"], "UNVERIFIED")
        self.assertEqual(set(row["flags"]), {"unverified", "gap"})
        self.assertEqual(row["gaps"][0]["cause"], "between_sessions")
        self.assertEqual([c["verified"] for c in row["coverage"]], [True, False])
        self.assertEqual(summary["sessions_by_status"]["crashed"], 1)
        self.assertEqual([s["open_segments"] for s in document["sessions"]], [0, 1])

    def test_anchor_inplay_and_schedule_classes(self):
        lines = ([image(t("12:30:00"), ("1.104", definition(None)), ("1.105", definition("14:00:00")),
                        ("1.106", definition("14:30:00")))] + beats(t("12:30:00"), t("15:20:00"))
                 + [change(t("14:05:00"), "1.105", status="CLOSED"),
                    change(t("13:00:00"), "1.106", market_time="14:45:00"),
                    change(t("14:47:00"), "1.106", market_time="14:45:00", inPlay=True),
                    change(t("14:52:00"), "1.106", market_time="14:45:00", status="CLOSED", inPlay=True)])
        self.writer.session(t("12:30:00"), [seg(lines)],
                            precount=[(0, [("1.104", f"{DAY}T15:00:00.000Z"), ("1.105", None), ("1.106", None)])])
        rows = self.index()[1]
        self.assertEqual((rows["1.104"]["class"], rows["1.104"]["anchor"], rows["1.104"]["catalogue_start"]),
                         ("NO_ANCHOR", None, "2026-10-21T15:00:00.000000Z"))
        self.assertIsNone(rows["1.104"]["window"])
        self.assertEqual((rows["1.105"]["class"], rows["1.105"]["flags"]),
                         ("CLOSED_WITHOUT_OBSERVED_INPLAY", ["closed_without_observed_inplay"]))
        self.assertEqual((rows["1.106"]["class"], rows["1.106"]["anchor"]),
                         ("SCHEDULE_CHANGED", "2026-10-21T14:30:00.000000Z"))
        self.assertEqual(rows["1.106"]["window"]["from"], "2026-10-21T13:30:00.000000Z")     # the initial marketTime

    def test_not_observed_from_the_precount_ranks_above_no_anchor(self):
        self.assertLess(CLASSES.index("NOT_OBSERVED"), CLASSES.index("NO_ANCHOR"))
        self.assertEqual(CLASSES[0], "UNVERIFIED")
        self.assertEqual(CLASSES[-1], "COMPLETE")
        lines = [image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("13:00:00"))
        self.writer.session(t("12:30:00"), [seg(lines)],
                            precount=[(0, [("1.101", f"{DAY}T14:00:00.000Z"), ("1.199", f"{DAY}T16:00:00.000Z"),
                                           ("1.198", "2026-10-22T16:00:00.000Z")])])
        rows = self.index()[1]
        self.assertEqual(set(rows), {"1.101", "1.199"})
        missing = rows["1.199"]
        self.assertEqual((missing["class"], missing["flags"], missing["anchor_source"]),
                         ("NOT_OBSERVED", ["not_observed"], "catalogue"))
        self.assertEqual(missing["window"], {"from": "2026-10-21T15:00:00.000000Z", "to": None})
        self.assertEqual(rows["1.101"]["catalogue_start"], "2026-10-21T14:00:00.000000Z")

    def test_flags_are_a_set_beside_one_class(self):
        seg0 = ([image(t("13:20:00"), ("1.109", definition("14:00:00")))] + beats(t("13:20:00"), t("13:40:00"))
                + [change(t("13:35:00"), "1.109", market_time="14:10:00")])
        seg1 = [image(t("13:50:00"), ("1.109", definition("14:10:00")))] + beats(t("13:50:00"), t("14:05:00"))
        self.writer.session(t("13:20:00"), [seg(seg0, close="connection_lost"), seg(seg1)])
        row = self.index()[1]["1.109"]
        self.assertEqual(row["class"], "LATE_START")
        self.assertEqual(set(row["flags"]), {"late_start", "gap", "no_close", "schedule_changed"})

    def test_tampered_segment_is_unverified(self):
        lines = ([image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("14:10:00"))
                 + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED")])
        directory = self.writer.session(t("12:30:00"), [seg(lines)])
        path = directory / "seg-000.jsonl"
        path.write_bytes(path.read_bytes().replace(b"9.87654321", b"9.87654322", 1))
        summary, rows, _ = self.index()
        self.assertEqual((rows["1.101"]["class"], summary["segments_verified"]), ("UNVERIFIED", 0))

    def test_index_never_outputs_runner_price_or_bsp_fields(self):
        """Sentinel test: runner names, selection ids, prices, volumes, BSP and bspReconciled planted in every
        line (synthetic sessions and the recorder's own output) never reach the index file, its summary or the
        CLI's stdout; the index file holds only whitelisted keys."""
        lines = ([image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("14:10:00"))
                 + [change(t("14:02:00"), "1.101", inPlay=True), change(t("14:06:00"), "1.101", status="CLOSED")])
        self.writer.session(t("12:30:00"), [seg(lines)], precount=[(0, [("1.101", None)])])
        # The recorder's own files, from the scripted stream, with the same sentinels.
        clock = support.FakeClock(start=f"{DAY}T14:30:00.000000Z")
        session = fx.FakeSession()
        start_ms = t("14:30:00") // 1_000_000
        script = fx.handshake() + [fx.change(lambda sid: fx.image(
            sid, start_ms, market_id="1.120", definition=fx.market_definition(
                marketTime=f"{DAY}T15:00:00.000Z", venue="SENTINEL-VENUE", bspReconciled=True,
                runners=[{"id": 987654321, "name": "SENTINEL-RUNNER-NAME", "bsp": 777.777001, "status": "ACTIVE"}]),
            rc=RC_SENTINEL))]
        server = fx.FakeStreamServer([script], clock)
        StreamRecorder(fx.recorder_config(self.paths), paths=self.paths, session=session,
                       stream_factory=lambda: fx.make_client(server, session, clock), market_lister=fx.lister(["1.120"]),
                       clock=clock, rng=random.Random(5), emit=lambda e: None).run(stop_requested=server.exhausted)
        summary, rows, document = self.index()
        self.assertIn("1.120", rows)                                       # the recorder's output was indexed
        text = (self.paths.recordings_index_dir / f"{DAY}.json").read_text()
        self.assert_no_sentinel(text)
        self.assert_no_sentinel(json.dumps(summary))

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)
        self.assertLessEqual(set(keys(document)), INDEX_KEYS)
        self.assertNotIn("market", json.dumps(summary).replace('"markets"', ""))    # counts only: no market fields
        # Through the CLI: stdout carries counts only.
        support.write_test_config(self.paths)
        code, out, err = run_cli(["--state-root", str(self.paths.root), "--config", str(self.paths.config_path),
                                  "recordings", "index", "--day", DAY])
        self.assertEqual(code, 0, err)
        self.assert_no_sentinel(out + err)
        for market_id in rows:
            self.assertNotIn(market_id, out)
        printed = json.loads(out)
        self.assertEqual(set(printed), {"status", "day", "sessions", "sessions_by_status", "inventory_damaged_sessions",
                                        "segments",
                                        "segments_verified", "markets", "classes", "flags", "gaps",
                                        "tolerated_handovers", "index_sha256"})
        self.assertTrue(all(isinstance(v, int) for v in printed["classes"].values()))
        code, out, err = run_cli(["--state-root", str(self.paths.root), "--config", str(self.paths.config_path),
                                  "verify-day", "--day", DAY])
        self.assertEqual(code, 0, out + err)
        self.assert_no_sentinel(out + err)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, deps={"clock": support.FakeClock(start=f"{DAY}T23:00:00.000000Z")})
    return code, out.getvalue(), err.getvalue()


class VerifyDayTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        random.seed(2)
        self.paths = support.temp_state_paths(self)
        self.writer = SessionWriter(self.paths)
        lines = ([image(t("12:30:00"), ("1.101", definition("14:00:00")))] + beats(t("12:30:00"), t("13:20:00")))
        more = [image(t("13:20:02"), ("1.101", definition("14:00:00")))] + beats(t("13:20:02"), t("14:10:00"))
        self.closed = self.writer.session(t("12:30:00"), [seg(lines), seg(more, close="max_seconds_reached")])

    def verify(self):
        return verify_day(self.paths, root=self.paths.recordings_dir, day=DAY)

    def failed(self):
        return {(f["session_id"], f["check"]) for f in self.verify()["failures"]}

    def test_clean_day_verifies(self):
        result = self.verify()
        self.assertEqual((result["status"], result["sessions"], result["checks_failed"]), ("verified", 1, 0))
        self.assertEqual(verify_day(self.paths, root=self.paths.recordings_dir, day="2026-10-22")["sessions"], 0)

    def test_receipt_sha_must_match_the_run_ledger_row(self):
        ledger = self.paths.recordings_dir / "run-ledger.jsonl"
        row = json.loads(ledger.read_text())
        ledger.write_text(json.dumps(dict(row, receipt_sha256="0" * 64)) + "\n")
        self.assertEqual(self.failed(), {(self.closed.name, "ledger_receipt_sha256_mismatch")})
        ledger.write_text(json.dumps(dict(row, receipt_path=str(self.closed / "elsewhere.json"))) + "\n")
        self.assertEqual(self.failed(), {(self.closed.name, "ledger_receipt_path_mismatch")})
        ledger.write_text("")
        self.assertEqual(self.failed(), {(self.closed.name, "receipt_without_ledger_row")})
        ledger.write_text(json.dumps(row) + "\n" + '{"torn')
        self.assertIn((None, "run_ledger_unreadable"), self.failed())

    def test_events_present_gap_list_and_stray_files(self):
        (self.closed / "events.jsonl").unlink()
        self.assertEqual(self.failed(), {(self.closed.name, "events_missing")})
        append_jsonl(self.closed / "events.jsonl", {"event": "stopped", "at": utc_from_ns(t("14:10:00"))})
        receipt = json.loads((self.closed / "receipt.json").read_text())
        receipt["gaps"] = []
        digest = atomic_write_json(self.closed / "receipt.json", receipt)
        ledger = self.paths.recordings_dir / "run-ledger.jsonl"
        ledger.write_text(json.dumps(dict(json.loads(ledger.read_text()), receipt_sha256=digest)) + "\n")
        self.assertEqual(self.failed(), {(self.closed.name, "gaps_do_not_match_segments")})
        receipt["gaps"] = expected_gaps(receipt["segments"])
        digest = atomic_write_json(self.closed / "receipt.json", receipt)
        ledger.write_text(json.dumps(dict(json.loads(ledger.read_text()), receipt_sha256=digest)) + "\n")
        (self.closed / ".receipt.json.tmp-123-0badf00d").write_text("{")
        self.assertEqual(self.verify()["crash_leftovers"], 1)
        self.assertEqual(self.verify()["status"], "verified")
        (self.closed / "notes.txt").write_text("stray")
        self.assertEqual(self.failed(), {(self.closed.name, "unexpected_file")})

    def test_crashed_session_and_open_segment_are_classified_not_failed(self):
        crashed = [image(t("14:30:00"), ("1.101", definition("15:00:00")))] + beats(t("14:30:00"), t("15:00:00"))
        closed_part = [image(t("14:20:00"), ("1.101", definition("15:00:00")))] + beats(t("14:20:00"), t("14:29:00"))
        self.writer.session(t("14:20:00"), [seg(closed_part)], crash_open=seg(crashed))
        result = self.verify()
        self.assertEqual(result["status"], "verified", result["failures"])
        self.assertEqual((result["sessions_by_status"]["crashed"], result["open_segments"]), (1, 1))
        # Its listed segments are still hash-checked.
        crashed_dir = sorted(self.paths.recordings_dir.glob("rec-*"))[-1]
        path = crashed_dir / "seg-000.jsonl"
        path.write_bytes(path.read_bytes().replace(b"HEARTBEAT", b"HEARTBEAt", 1))
        self.assertIn((crashed_dir.name, "segment_sha256"), self.failed())

    def test_declared_precount_must_match(self):
        """REC-3: a declared pre-count snapshot that is deleted, truncated or altered fails verify-day (and the
        index stops using it, counting the session as damaged) instead of passing as a session from before C4."""
        start = t("15:00:00")
        lines = ([image(start, ("1.101", definition("16:00:00")))] + beats(start, t("16:20:00"))
                 + [change(t("16:02:00"), "1.101", inPlay=True), change(t("16:06:00"), "1.101", status="CLOSED")])
        directory = self.writer.session(start, [seg(lines, close="max_seconds_reached")],
                                        precount=[(0, [("1.101", f"{DAY}T16:00:00.000Z"),
                                                       ("1.199", f"{DAY}T16:00:00.000Z")])])
        inventory = directory / PRECOUNT_NAME
        original = inventory.read_bytes()
        self.assertEqual(self.verify()["status"], "verified")
        clock = support.FakeClock(start=f"{DAY}T23:00:00.000000Z")
        summary = build_index(self.paths, root=self.paths.recordings_dir, day=DAY, clock=clock)
        self.assertEqual((summary["classes"]["NOT_OBSERVED"], summary["inventory_damaged_sessions"]), (1, 0))
        damage = [("deleted", lambda: inventory.unlink(), "precount_missing"),
                  ("emptied", lambda: inventory.write_bytes(b""), "precount_sha256_mismatch"),
                  ("truncated", lambda: inventory.write_bytes(original[:len(original) // 2]), "precount_sha256_mismatch"),
                  ("altered", lambda: inventory.write_bytes(original.replace(b"1.199", b"1.198")),
                   "precount_sha256_mismatch"),
                  ("extra_row", lambda: inventory.write_bytes(original + original), "precount_rows_mismatch"),
                  ("symlink", lambda: (inventory.unlink(), inventory.symlink_to(directory / "events.jsonl")),
                   "precount_not_regular")]
        for name, make, check in damage:
            with self.subTest(damage=name):
                make()
                self.assertEqual(self.failed(), {(directory.name, check)})
                summary = build_index(self.paths, root=self.paths.recordings_dir, day=DAY, clock=clock)
                self.assertEqual((summary["classes"]["NOT_OBSERVED"], summary["inventory_damaged_sessions"]), (0, 1))
                if inventory.is_symlink() or inventory.exists():
                    inventory.unlink()
                inventory.write_bytes(original)
        self.assertEqual(self.verify()["status"], "verified")
        # A receipt from before C4 (no precount entry) passes without a file, but a file it never declared fails.
        self.assertEqual(recordings.precount_problems(self.closed, json.loads((self.closed / "receipt.json").read_text()),
                                                      final=True), [])
        (self.closed / PRECOUNT_NAME).write_bytes(original)
        self.assertEqual(self.failed(), {(self.closed.name, "precount_undeclared")})

    def test_ledger_row_without_its_session(self):
        import shutil
        shutil.rmtree(self.closed)
        self.assertEqual(self.failed(), {(self.closed.name, "ledger_row_without_session")})

    def test_cli_exit_codes_and_day_validation(self):
        support.write_test_config(self.paths)
        base = ["--state-root", str(self.paths.root), "--config", str(self.paths.config_path)]
        code, out, err = run_cli(base + ["verify-day", "--day", DAY])
        self.assertEqual((code, json.loads(out)["status"]), (0, "verified"))
        (self.closed / "events.jsonl").unlink()
        code, out, err = run_cli(base + ["verify-day", "--day", DAY])
        self.assertEqual((code, err.strip()), (2, "Betfair input error: day_failed_verification"))
        self.assertEqual(json.loads(out)["failures"], [{"session_id": self.closed.name, "check": "events_missing"}])
        for command in (["verify-day"], ["recordings", "index"]):
            with self.subTest(command=command):
                code, out, err = run_cli(base + command + ["--day", "21-10-2026"])
                self.assertEqual((code, err.strip()), (2, "Betfair input error: day_invalid; give a UTC date YYYY-MM-DD"))


if __name__ == "__main__":
    unittest.main()
