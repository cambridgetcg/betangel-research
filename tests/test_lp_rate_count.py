"""scripts/lp_rate_count.py, the Stage 0 rate count for Pilot 2 (docs/lp-rate-count.md), on synthetic recordings only.

Sessions are laid out as the #41 recorder writes them (rec-*/seg-NNN.jsonl, seg-NNN.recv.jsonl, receipt.json or
receipt.partial.json). Every id, name, venue and time below is a sentinel that must never reach the output. The
script's copies of the reader, the fold and the CLASSIC ladder are checked here against stream.messages,
betfair.book.MarketBookCache and betfair.ticks.
"""

import ast
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from betangel_research import stream
from betangel_research.betfair import book, constants, ticks
from betangel_research.betfair.recorder import StreamRecorder
from betangel_research.temporal import timestamp_ms

import test_betfair_support as support
import test_betfair_support_stream as fx

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "scripts" / "lp_rate_count.py"
SPEC = importlib.util.spec_from_file_location("reviewed_lp_rate_count", TOOL)
lp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lp)

DAY = "2026-11-10"
OFF_TEXT = f"{DAY}T14:35:00.000Z"
OFF = timestamp_ms(OFF_TEXT)
MARKET = "1.999123401"
SELECTIONS = (91234561, 91234562, 91234563, 91234564, 91234565, 91234566)
FIELDS = ["EX_BEST_OFFERS", "EX_TRADED", "EX_TRADED_VOL", "EX_LTP", "EX_MARKET_DEF"]
SENTINELS = ("1.999123401", "1.999123402", "999123401", "91234561", "91234566", "39876543", "SENTINEL-VENUE",
             "SENTINEL-EVENT", "SENTINEL-RUNNER", "Europe/London", "14:35", "14:30", "2.98", "7.4", "4.6", "21.5",
             "17.25")


def at(seconds, off=OFF):
    """Publish time `seconds` from the off (negative before it)."""
    return off + int(round(seconds * 1000))


def definition(*, off_text=OFF_TEXT, status="OPEN", in_play=False, country="GB", market_type="WIN", active=6,
               event_type="7", version=1, **changes):
    runners = [{"id": selection, "hc": 0, "sortPriority": n + 1, "status": "ACTIVE" if n < active else "REMOVED",
                "name": "SENTINEL-RUNNER"} for n, selection in enumerate(SELECTIONS)]
    value = {"bspMarket": False, "turnInPlayEnabled": True, "persistenceEnabled": True, "marketBaseRate": 5.0,
             "eventId": "39876543", "eventTypeId": event_type, "numberOfWinners": 1, "bettingType": "ODDS",
             "marketType": market_type, "marketTime": off_text, "suspendTime": off_text, "bspReconciled": False,
             "complete": True, "inPlay": in_play, "crossMatching": True, "runnersVoidable": False,
             "numberOfActiveRunners": active, "betDelay": 0, "status": status, "countryCode": country,
             "venue": "SENTINEL-VENUE", "eventName": "SENTINEL-EVENT", "timezone": "Europe/London",
             "priceLadderDefinition": {"type": "CLASSIC"}, "version": version, "runners": runners}
    value.update(changes)
    return value


def quotes(back, lay, *, others=True):
    """Runner changes: the first selection at (back, lay); the others far outside every band (never qualifying)."""
    first = {"id": SELECTIONS[0], "batb": [[0, back, 21.5]] if back else [[0, 0, 0]],
             "batl": [[0, lay, 17.25]] if lay else [[0, 0, 0]]}
    rest = [{"id": s, "batb": [[0, 12.0, 5.5]], "batl": [[0, 20.0, 6.5]]} for s in SELECTIONS[1:]] if others else []
    return [first] + rest


def image(pt, *markets):
    """SUB_IMAGE: markets are (market id, definition, runner changes)."""
    return {"op": "mcm", "id": 1, "pt": pt, "clk": "c", "ct": "SUB_IMAGE", "initialClk": "i", "heartbeatMs": 5000,
            "conflateMs": 180000,
            "mc": [{"id": m, "img": True, "marketDefinition": d, "rc": rc} for m, d, rc in markets]}


def delta(pt, market_id=MARKET, rc=None, md=None, status=None):
    change = {"id": market_id}
    if rc is not None:
        change["rc"] = rc
    if md is not None:
        change["marketDefinition"] = md
    msg = {"op": "mcm", "id": 1, "pt": pt, "clk": "c", "mc": [change]}
    if status is not None:
        msg["status"] = status
    return msg


def heartbeat(pt, status=None):
    msg = {"op": "mcm", "id": 1, "pt": pt, "clk": "c", "ct": "HEARTBEAT"}
    if status is not None:
        msg["status"] = status
    return msg


def beats(start_s, end_s, off=OFF, step=30):
    """Heartbeats every `step` seconds in (start_s, end_s)."""
    out, t = [], start_s + step
    while t < end_s:
        out.append(heartbeat(at(t, off)))
        t += step
    return out


def seg(*parts):
    """One segment's lines in publish-time order (a stable sort, so lines with one pt keep their order)."""
    lines = [m for part in parts for m in (part if isinstance(part, list) else [part])]
    return sorted(lines, key=lambda m: m["pt"])


def multi_lines(schedules, *, off=OFF, step=180, start=-900, end=60):
    """Markets recorded from `start` to `end` seconds in one segment: one image, then a delta per market every
    `step` s (as 180 s conflation does), quotes from prices(t) -> (back, lay), heartbeats every 30 s between."""
    lines = [image(at(start, off), *[(m, definition(off_text=_iso(off)), quotes(*prices(start)))
                                     for m, prices in schedules.items()])]
    t = start + step
    while t <= end:
        lines += [delta(at(t, off), m, quotes(*prices(t), others=False)) for m, prices in schedules.items()]
        t += step
    return seg(lines, beats(start, end, off))


def market_lines(prices, *, market_id=MARKET, **options):
    return multi_lines({market_id: prices}, **options)


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Recordings:
    """Writes sessions laid out as the recorder writes them."""

    def __init__(self, root):
        self.root = Path(root)
        self.count = 0

    def session(self, segments, *, partial=False, open_segment=None, key_type="delayed", label="delayed_conflated",
                fields=FIELDS, levels=3, replay_status="passed", subscription=True, conflate_ms=180000,
                stop_reason="end_at_reached", complete=None):
        self.count += 1
        first = next((seg[0]["pt"] for seg in segments if seg), OFF)
        name = "rec-" + datetime.fromtimestamp(first / 1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = self.root / f"{name}-{self.count:08x}"
        directory.mkdir(mode=0o700)
        entries = [self.segment(directory, index, lines, replay_status) for index, lines in enumerate(segments)]
        if open_segment is not None:
            self.segment(directory, len(segments), open_segment, replay_status)
        receipt = {"schema_version": 1, "status": "betfair_stream_recording_not_profitability_evidence",
                   "complete": (not partial) if complete is None else complete, "session_id": directory.name,
                   "stop_reason": None if partial else stop_reason, "key_type": key_type,
                   "data_freshness": {"conflate_ms_requested": None, "conflate_ms_echoed": conflate_ms,
                                      "heartbeat_ms_echoed": 5000, "label": label, "note": "synthetic"},
                   "segments": entries, "gaps": [], "economic_result": None}
        if subscription:
            receipt["subscription"] = {
                "market_filter": {"event_type_ids": ["7"], "country_codes": ["GB", "IE"], "market_types": ["WIN"]},
                "market_data_filter": {"fields": list(fields), "ladderLevels": levels}, "segmentation_enabled": False}
        name = "receipt.partial.json" if partial else "receipt.json"
        (directory / name).write_text(json.dumps(receipt), encoding="utf-8")
        return directory

    @staticmethod
    def segment(directory, index, lines, replay_status):
        data, rows = b"", b""
        for number, msg in enumerate(lines, 1):
            raw = json.dumps(msg, separators=(",", ":")).encode()
            rows += (json.dumps({"line": number, "byte_offset": len(data), "bytes": len(raw),
                                 "sha256": hashlib.sha256(raw).hexdigest(), "received_at": _iso(msg["pt"]),
                                 "monotonic_ns": msg["pt"] * 1000, "pt": msg["pt"], "clk": msg.get("clk")},
                                sort_keys=True, separators=(",", ":")) + "\n").encode()
            data += raw + b"\n"
        (directory / f"seg-{index:03d}.jsonl").write_bytes(data)
        (directory / f"seg-{index:03d}.recv.jsonl").write_bytes(rows)
        return {"index": index, "path": f"seg-{index:03d}.jsonl", "recv_path": f"seg-{index:03d}.recv.jsonl",
                "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "lines": len(lines),
                "first_pt": lines[0]["pt"] if lines else None, "last_pt": lines[-1]["pt"] if lines else None,
                "subscription_mode": "coarse", "close_reason": "scheduled_refresh",
                "replay_check": {"reader": "betangel_research.stream.messages", "status": replay_status,
                                 "error": None}}


def steady(back, lay):
    return lambda t: (back, lay)


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "recordings"
        self.root.mkdir()
        self.rec = Recordings(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def reset(self):
        self.tearDown()
        self.setUp()

    def run_tool(self, *extra, root=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lp.main(["--root", str(root or self.root), *extra])
        return code, out.getvalue(), err.getvalue()

    def summary(self):
        code, out, err = self.run_tool("--json")
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["inputs"]["segments"]["unreadable"], 0)     # no fixture fails to read
        return result

    def counted(self):
        """Every column, as tallied before the printed result withholds them below 10 racing days."""
        count = lp.Count().read_root(lp.check_root(str(self.root)))
        self.assertEqual(count.segments["unreadable"], 0)
        counted = lp.tally(count)                                           # reasons and revisions over all dates
        counted["not_usable"] = {code: sum(day[code] for day in counted["not_usable"].values())
                                     for code in lp.NOT_USABLE}
        counted["schedule_revised"] = sum(counted["schedule_revised"].values())
        return counted

    def pooled(self):
        return self.counted()["pooled"]


class LadderTests(Case):
    def test_ladder_matches_the_package_ladder(self):
        self.assertEqual(lp.CLASSIC_BANDS, constants.CLASSIC_BANDS)
        self.assertEqual(lp.LADDER, ticks.LADDER_CLASSIC)

    def test_band_edges_are_one_tick_apart(self):
        for low, high in ((2.98, 3.00), (3.95, 4.00), (4.9, 5.0), (5.9, 6.0), (6.0, 6.2)):
            with self.subTest(low=low, high=high):
                b, l = lp.hundredths(low), lp.hundredths(high)
                self.assertEqual((b, l), (ticks.to_hundredths(low), ticks.to_hundredths(high)))
                self.assertEqual(lp.spread_ticks(b, l), 1)
                self.assertEqual(ticks.tick_index(l) - ticks.tick_index(b), 1)

    def test_prices_off_the_ladder_or_not_numbers_never_qualify(self):
        for value in (3.01, 2.99, 1.005, float("inf"), float("nan"), True, "3.0", None, 0.5):
            with self.subTest(value=value):
                self.assertIsNone(lp.hundredths(value))
        self.assertEqual(lp.hundredths(3), 300)
        self.assertEqual(lp.runner_flags(3.01, 3.2), (False, False, False))

    def test_spread_widths_and_bands(self):
        cases = {
            (3.0, 3.1): (False, False, False),       # 2 ticks
            (3.0, 3.15): (True, False, False),       # 3 ticks
            (3.0, 3.2): (True, False, False),        # 4 ticks
            (3.0, 3.25): (False, False, True),       # 5 ticks: width lever
            (3.0, 3.35): (False, False, True),       # 7 ticks
            (3.0, 3.4): (False, False, False),       # 8 ticks
            (1.49, 1.52): (False, False, False),     # best back below 1.50
            (1.5, 1.53): (True, False, False),       # 1.50 inclusive
            (5.9, 6.4): (True, False, False),        # 5.90 inclusive (5.9, 6.0, 6.2, 6.4)
            (6.0, 6.6): (False, True, False),        # price lever from 6.0
            (10.0, 11.5): (False, True, False),      # 10.0 inclusive
            (10.5, 12.0): (False, False, False),
            (3.2, 3.0): (False, False, False),       # crossed
            (3.0, 3.0): (False, False, False),
            (None, 3.15): (False, False, False),
        }
        for (back, lay), expected in cases.items():
            with self.subTest(back=back, lay=lay):
                self.assertEqual(lp.runner_flags(back, lay), expected)
                if back and lay and lp.hundredths(back) and lp.hundredths(lay) and back < lay:
                    self.assertEqual(lp.spread_ticks(lp.hundredths(back), lp.hundredths(lay)),
                                     ticks.tick_index(ticks.to_hundredths(lay))
                                     - ticks.tick_index(ticks.to_hundredths(back)))

    def test_two_to_five_tick_spreads_end_to_end(self):
        lays = {"1.999123411": 3.1, "1.999123412": 3.15, "1.999123413": 3.2, "1.999123414": 3.25}
        mds = [(m, definition(), quotes(3.0, lay)) for m, lay in lays.items()]
        lines = [image(at(-690), *mds)] + beats(-690, 0)
        self.rec.session([lines])
        pooled = self.pooled()
        self.assertEqual((pooled["usable"], pooled["primary"], pooled["width_lever"], pooled["price_lever"]),
                         (4, 2, 1, 0))


class ReaderAndFoldTests(Case):
    def test_reader_matches_stream_messages(self):
        lines = market_lines(steady(3.0, 3.15))
        directory = self.rec.session([lines])
        path = directory / "seg-000.jsonl"
        text = path.read_text() + "\n" + json.dumps({"op": "status", "id": 1}) + "\n"
        path.write_text(text)
        self.assertEqual(list(lp.messages(text)), list(stream.messages(path)))
        bad = json.dumps(heartbeat(5)) + "\n" + json.dumps(heartbeat(4)) + "\n"
        path.write_text(bad)
        with self.assertRaises(ValueError):
            list(stream.messages(path))
        with self.assertRaises(ValueError):
            list(lp.messages(bad))

    def test_best_offers_match_the_book_cache(self):
        """Seeded random sequences of images, level deltas (size 0 truncates), suspensions, re-images and 503s."""
        prices = [p / 100 for p in ticks.LADDER_CLASSIC[:120]]
        for seed in range(12):
            rng = random.Random(seed)
            mine = lp.BestOffers()
            cache = book.MarketBookCache(key_type="delayed", app_key_slot="delayed", source="recording")
            version, pt = 1, 1_000
            msgs = [image(pt, (MARKET, definition(), quotes(3.0, 3.15)))]
            for _ in range(250):
                pt += rng.choice((0, 1, 500))
                roll = rng.random()
                if roll < 0.06:
                    version += 1
                    msgs.append(delta(pt, md=definition(status=rng.choice(("OPEN", "SUSPENDED")), version=version)))
                elif roll < 0.09:
                    msgs.append({**image(pt, (MARKET, definition(version=version), quotes(2.0, 2.04))), "ct": None})
                elif roll < 0.12:
                    msgs.append(heartbeat(pt, status=503))
                elif roll < 0.15:
                    msgs.append(heartbeat(pt))
                else:
                    rc = []
                    for selection in rng.sample(SELECTIONS, rng.randint(1, 3)):
                        change = {"id": selection}
                        for field in rng.sample(("batb", "batl"), rng.randint(1, 2)):
                            change[field] = [[rng.randint(0, 2), rng.choice(prices),
                                              0 if rng.random() < 0.25 else rng.randint(1, 50)]
                                             for _ in range(rng.randint(1, 3))]
                        rc.append(change)
                    msgs.append(delta(pt, rc=rc, status=503 if rng.random() < 0.03 else None))
            for msg in msgs:
                mine.apply(msg)
                cache.apply(msg)
                snap = cache.snapshot(MARKET, unreliable_ok=True)
                with self.subTest(seed=seed, pt=msg["pt"]):
                    self.assertEqual(mine.unreliable, snap["invalid_reason"] == "stream_unreliable")
                    self.assertEqual(mine.markets[MARKET]["definition"]["status"], snap["status"])
                    for selection in SELECTIONS:
                        runner = snap["runners"][str(selection)]
                        expected = (runner["back"][0][0] if runner["back"] else None,
                                    runner["lay"][0][0] if runner["lay"] else None)
                        self.assertEqual(mine.best(MARKET, str(selection)), expected)

    def test_non_finite_numbers_make_the_market_invalid_until_its_next_image(self):
        lines = [image(at(-990), (MARKET, definition(), quotes(3.0, 3.15))),
                 delta(at(-790), rc=[{"id": SELECTIONS[1], "batb": [[0, 12.0, "Infinity"]]}]),
                 delta(at(-610), rc=quotes(3.0, 3.15, others=False))]
        self.rec.session([seg(lines, beats(-990, 0))])
        summary = self.summary()
        self.assertEqual(summary["inputs"]["segments"]["read"], 1)
        self.assertEqual(summary["not_usable"]["invalid"], 1)
        self.assertEqual(summary["pooled"]["usable"], 0)


class SnapshotTests(Case):
    def test_last_state_at_or_before_each_offset(self):
        """A change published exactly at off - 590 s is part of the -590 s state; one a millisecond later is not."""
        self.rec.session([seg(image(at(-690), (MARKET, definition(), quotes(3.0, 3.1)),
                                    ("1.999123402", definition(), quotes(3.0, 3.15))),
                              delta(at(-590), rc=quotes(3.0, 3.15, others=False)),
                              delta(at(-589.999), rc=quotes(3.0, 3.1, others=False)),
                              delta(at(-589.999), "1.999123402", rc=quotes(3.0, 3.1, others=False)),
                              beats(-690, 0))])
        self.assertEqual((self.pooled()["usable"], self.pooled()["primary"]), (2, 2))

    def test_snapshot_older_than_180_s_is_no_snapshot(self):
        """Heartbeats do not refresh the data: age is measured from the last non-heartbeat line."""
        for last_data_s, usable in ((-770, 1), (-770.001, 0), (-990, 0)):
            with self.subTest(last_data_s=last_data_s):
                self.reset()
                lines = [image(at(-1190), (MARKET, definition(), quotes(3.0, 3.15))),
                         delta(at(last_data_s), rc=quotes(3.0, 3.15, others=False)),
                         delta(at(-390), rc=quotes(3.0, 3.15, others=False))]
                lines += [heartbeat(at(t)) for t in range(-1185, 0, 5) if at(t) > at(last_data_s)]
                self.rec.session([sorted(lines, key=lambda m: m["pt"])])
                summary = self.summary()
                self.assertEqual(summary["pooled"]["usable"], usable)
                self.assertEqual(summary["not_usable"]["no_snapshot"], 1 - usable)

    def test_the_latest_state_across_segments_is_used(self):
        early = market_lines(steady(3.0, 3.15), start=-1190, end=-650)  # last data at -650 s, qualifying
        # The next segment's later image (before -590 s) wins, although it does not qualify.
        self.rec.session([early, [image(at(-620), (MARKET, definition(), quotes(3.0, 3.1)))] + beats(-620, 0)])
        self.assertEqual((self.pooled()["usable"], self.pooled()["primary"]), (1, 0))

    def test_a_state_is_never_carried_past_the_end_of_its_segment(self):
        """The -590 s state needs a line of that segment (a heartbeat will do) at or after -590 s, and fresh data."""
        for last_line_s, last_data_s, primary in ((-690, -710, 0), (-590.001, -710, 0), (-590, -710, 1),
                                                  (-580, -830, 0), (-580, -740, 1)):
            with self.subTest(last_line_s=last_line_s, last_data_s=last_data_s):
                self.reset()
                early = [image(at(-1190), (MARKET, definition(), quotes(3.0, 3.15))),
                         delta(at(last_data_s), rc=quotes(3.0, 3.15, others=False))]
                early += [heartbeat(at(t)) for t in range(-1185, int(last_line_s), 5) if at(t) > at(last_data_s)]
                early.append(heartbeat(at(last_line_s)))
                late = [image(at(-540), (MARKET, definition(), quotes(3.0, 3.15)))] + beats(-540, 0)
                self.rec.session([early, late])
                counted = self.counted()
                self.assertEqual((counted["pooled"]["primary"], counted["not_usable"]["no_snapshot"]),
                                 (primary, 1 - primary))

    def test_a_503_at_the_snapshot_makes_it_invalid(self):
        base = [image(at(-1190), (MARKET, definition(), quotes(3.0, 3.15))),
                delta(at(-650), rc=quotes(3.0, 3.15, others=False))]
        self.rec.session([base + [heartbeat(at(-610), status=503), delta(at(-390), rc=quotes(3.0, 3.15))]])
        summary = self.summary()
        self.assertEqual((summary["pooled"]["usable"], summary["not_usable"]["invalid"]), (0, 1))
        self.reset()
        # A healthy line after the 503 and before -590 s restores the same (fresh) data.
        self.rec.session([base + [heartbeat(at(-610), status=503), heartbeat(at(-600)),
                                  delta(at(-390), rc=quotes(3.0, 3.15))]])
        self.assertEqual(self.pooled()["primary"], 1)

    def test_off_is_the_first_published_market_time_and_revisions_are_counted(self):
        revised = definition(off_text=_iso(at(300)), version=2)          # 5 minutes later
        lines = [image(at(-1190), (MARKET, definition(), quotes(3.0, 3.1))),
                 delta(at(-890), md=revised),
                 delta(at(-650), rc=quotes(3.0, 3.15, others=False)),     # qualifies at the first off - 590 s
                 delta(at(-350), rc=quotes(3.0, 3.1, others=False))]
        self.rec.session([seg(lines, beats(-1190, 300))])
        counted = self.counted()
        self.assertEqual((counted["pooled"]["primary"], counted["schedule_revised"]), (1, 1))

    def test_racing_day_is_the_utc_date_of_the_first_off(self):
        off = timestamp_ms("2026-11-10T23:58:00.000Z")
        md = definition(off_text=_iso(off))
        lines = [image(at(-1190, off), (MARKET, md, quotes(3.0, 3.15))),
                 delta(at(-690, off), md=definition(off_text=_iso(at(600, off)), version=2))]
        self.rec.session([seg(lines, beats(-1190, 0, off))])
        self.assertEqual([row["date"] for row in self.summary()["days"]], ["2026-11-10"])


class ExclusionTests(Case):
    def test_markets_outside_the_population_or_not_open_are_excluded(self):
        cases = {"1.999123421": (definition(status="SUSPENDED"), "not_open_or_in_play"),
                 "1.999123422": (definition(in_play=True), "not_open_or_in_play"),
                 "1.999123423": (definition(active=4), "under_5_active"),
                 "1.999123424": (definition(country="FR"), "out_of_population"),
                 "1.999123425": (definition(market_type="PLACE"), "out_of_population"),
                 "1.999123426": (definition(event_type="4339"), "out_of_population"),
                 "1.999123427": (definition(numberOfWinners=2), "out_of_population"),
                 "1.999123428": (definition(bettingType="ASIAN_HANDICAP_DOUBLE_LINE"), "out_of_population"),
                 "1.999123430": (definition(priceLadderDefinition={"type": "FINEST"}), "out_of_population"),
                 "1.999123435": (definition(priceLadderDefinition=None), "out_of_population"),
                 "1.999123429": (definition(active=5, country="IE", eventTypeId=7), None),
                 "1.999123436": (definition(priceLadderDefinition="CLASSIC"), None)}    # the bare-string form
        lines = [image(at(-690), *[(m, md, quotes(3.0, 3.15)) for m, (md, _) in cases.items()])] + beats(-690, 0)
        self.rec.session([lines])
        counted = self.counted()
        self.assertEqual((counted["pooled"]["usable"], counted["pooled"]["primary"]), (2, 2))
        self.assertEqual(counted["pooled"]["recorded"], len(cases))
        expected = {code: 0 for code in lp.NOT_USABLE}
        for _, code in cases.values():
            if code:
                expected[code] += 1
        self.assertEqual(counted["not_usable"], expected)

    def test_suspension_clears_the_quotes_and_a_reopen_without_new_quotes_shows_none(self):
        lines = [image(at(-1190), (MARKET, definition(), quotes(3.0, 3.15))),
                 delta(at(-790), md=definition(status="SUSPENDED", version=2)),
                 delta(at(-690), md=definition(status="OPEN", version=3))]
        self.rec.session([seg(lines, beats(-1190, 0))])
        self.assertEqual((self.pooled()["usable"], self.pooled()["primary"]), (1, 0))


class ColumnTests(Case):
    def test_persistence_needs_both_snapshots(self):
        def schedule(qualify_780, qualify_590):
            # data at -900 (the -780 s state), -720 (the -590 s state), -540, -360, -180, 0
            return lambda t: (3.0, 3.15) if (t == -900 and qualify_780) or (t == -720 and qualify_590) else (3.0, 3.1)
        markets = {"1.999123431": (True, True), "1.999123432": (False, True), "1.999123433": (True, False)}
        self.rec.session([multi_lines({m: schedule(*flags) for m, flags in markets.items()})])
        pooled = self.pooled()
        self.assertEqual((pooled["primary"], pooled["flagged"], pooled["persistence"],
                          pooled["persistence_two_batches"]), (2, 2, 1, 1))

    def test_one_batch_is_never_both_snapshots(self):
        """Amendment 3: the -780 s and -590 s states are 190 s apart, more than the 180 s age limit, so data seen at
        -780 s is stale at -590 s, and every persistent market comes from two batches."""
        lines = [image(at(-900), (MARKET, definition(), quotes(3.0, 3.1))),
                 delta(at(-780), rc=quotes(3.0, 3.15, others=False)),
                 delta(at(-400), rc=quotes(3.0, 3.1, others=False))]
        self.rec.session([seg(lines, beats(-900, 0, step=5))])
        counted = self.counted()
        self.assertEqual((counted["pooled"]["usable"], counted["not_usable"]["no_snapshot"]), (0, 1))

    def test_window_counts_any_usable_qualifying_state_from_600_to_300_s(self):
        cases = {"1.999123441": [(-650, (3.0, 3.15)), (-610, (3.0, 3.1))],    # qualifies only before the window
                 "1.999123442": [(-650, (3.0, 3.15)), (-590, (3.0, 3.1))],    # the -600 s state qualifies
                 "1.999123443": [(-650, (3.0, 3.1)), (-450, (3.0, 3.2)), (-400, (3.0, 3.1))],  # inside only
                 "1.999123444": [(-650, (3.0, 3.1)), (-299, (3.0, 3.15))],    # just after the window
                 "1.999123445": [(-650, (3.0, 3.1)), (-300, (3.0, 3.15))],    # at -300 s exactly
                 "1.999123446": [(-650, (3.0, 3.1)), (-450, (3.0, 3.15))],    # suspended at the -590 s state, below
                 "1.999123447": [(-650, (3.0, 3.1)), (-590, (3.0, 3.15))]}    # at -590 s exactly: primary too
        lines = [image(at(-650), *[(m, definition(), quotes(*changes[0][1])) for m, changes in cases.items()])]
        lines += [delta(at(t), m, quotes(*p, others=False)) for m, changes in cases.items() for t, p in changes[1:]]
        # Suspended from -620 to -500 s: not usable at -590 s, so outside every rate's denominator, the window's too.
        lines += [delta(at(-620), "1.999123446", md=definition(status="SUSPENDED", version=2)),
                  delta(at(-500), "1.999123446", md=definition(version=3))]
        self.rec.session([seg(lines, beats(-650, 0, step=5))])
        counted = self.counted()
        self.assertEqual((counted["pooled"]["usable"], counted["pooled"]["window"], counted["pooled"]["primary"]),
                         (6, 4, 1))
        self.assertEqual(counted["not_usable"]["not_open_or_in_play"], 1)

    def test_window_ignores_a_stale_state(self):
        lines = [image(at(-900), (MARKET, definition(), quotes(3.0, 3.15)))] + [
            heartbeat(at(t)) for t in range(-895, -200, 5)] + [delta(at(-200), rc=quotes(3.0, 3.1, others=False))]
        self.rec.session([lines])
        counted = self.counted()
        self.assertEqual((counted["pooled"]["window"], counted["not_usable"]["no_snapshot"]), (0, 1))

    def test_price_and_width_levers(self):
        mds = [("1.999123451", definition(), quotes(6.0, 6.6)),     # price lever (3 ticks from 6.0)
               ("1.999123452", definition(), quotes(9.4, 10.5)),    # price lever: 9.4 9.6 9.8 10 10.5 is 4 ticks
               ("1.999123453", definition(), quotes(3.0, 3.25)),    # width lever, 5 ticks
               ("1.999123454", definition(), quotes(5.9, 7.0)),     # width lever, 6 ticks (6.0 6.2 ... 7.0)
               ("1.999123455", definition(), quotes(5.9, 7.2)),     # width lever, 7 ticks
               ("1.999123456", definition(), quotes(5.9, 7.4))]     # 8 ticks: nothing
        self.rec.session([[image(at(-690), *mds)] + beats(-690, 0)])
        pooled = self.pooled()
        self.assertEqual((pooled["usable"], pooled["primary"], pooled["price_lever"], pooled["width_lever"]),
                         (6, 0, 2, 3))


class GateTests(Case):
    def test_gate_a_threshold_days_markets_and_open_recording(self):
        self.assertEqual(lp.gate_a(10, 14, 1000), "PASS")
        self.assertEqual(lp.gate_a(10, 13, 1000), "FAIL")
        self.assertEqual(lp.gate_a(12, 7, 500), "PASS")
        self.assertEqual(lp.gate_a(10, 42, 3000), "PASS")                   # exactly 1.4%
        self.assertEqual(lp.gate_a(10, 41, 3000), "FAIL")
        self.assertEqual(lp.gate_a(9, 1000, 1000), "NOT_ENOUGH_DAYS")
        self.assertEqual(lp.gate_a(10, 299, 299), "NOT_ENOUGH_MARKETS")
        self.assertEqual(lp.gate_a(10, 0, 300), "FAIL")
        self.assertEqual(lp.gate_a(10, 300, 300, recording_open=True), "RECORDING_OPEN")
        self.assertEqual(lp.gate_a(9, 300, 300, recording_open=True), "NOT_ENOUGH_DAYS")

    def days(self, count, qualifying, per_day=30, first=1, closed=True, **session):
        """`count` days of `per_day` markets each, with `qualifying` qualifying markets spread over them and, unless
        `closed` is False, a session started on the next UTC date, so the last day's recording has ended."""
        for n in range(count):
            off = timestamp_ms(f"2026-12-{first + n:02d}T14:35:00.000Z")
            markets = []
            for k in range(per_day):
                index = n * per_day + k
                prices = (3.0, 3.15) if index < qualifying else (3.0, 3.1)
                markets.append((f"1.99{first + n:02d}{k:05d}", definition(off_text=_iso(off)), quotes(*prices)))
            self.rec.session([[image(at(-690, off), *markets)] + beats(-690, 0, off)],
                             **(session if n == count - 1 else {}))
        if closed:
            self.later_session(first + count)
        return self.summary()["gate_a"]

    def later_session(self, date):
        """The directory of a session started on 2026-12-`date`, with no receipt yet. Its name is all Gate A uses."""
        (self.root / f"rec-202612{date:02d}T070000Z-ffffff{date:02d}").mkdir()

    def day(self, date, count, qualifying, hour=14, **session):
        """One session on 2026-12-`date`: `count` markets off at `hour`:35 UTC, the first `qualifying` qualifying."""
        off = timestamp_ms(f"2026-12-{date:02d}T{hour:02d}:35:00.000Z")
        markets = [(f"1.{hour:02d}{date:02d}{k:05d}", definition(off_text=_iso(off)),
                    quotes(3.0, 3.15 if k < qualifying else 3.1)) for k in range(count)]
        self.rec.session([[image(at(-690, off), *markets)] + beats(-690, 0, off)], **session)

    def test_gate_a_pass(self):
        gate = self.days(10, 5)                                            # 5 of 300: 1.67%
        self.assertEqual((gate["result"], gate["primary"], gate["usable"]), ("PASS", 5, 300))

    def test_gate_a_fail(self):
        gate = self.days(10, 4)                                            # 4 of 300: 1.33%
        self.assertEqual((gate["result"], gate["racing_days"], gate["usable"]), ("FAIL", 10, 300))

    def test_gate_a_not_enough_days(self):
        self.assertEqual(self.days(9, 300)["result"], "NOT_ENOUGH_DAYS")

    def assert_withheld(self):
        summary = self.summary()
        self.assertTrue(summary["counts_withheld"])
        self.assertNotIn("primary", summary["gate_a"])
        self.assertEqual(set(summary["pooled"]), {"recorded", "usable", "racing_days"})
        for row in summary["days"]:
            self.assertEqual(set(row), {"date", "racing_day", "in_gate_a", "recorded", "usable"})
        code, out, err = self.run_tool()
        self.assertEqual((code, err), (0, ""))
        for word in ("primary", "persistence", "window", "lever", "flagged", "%"):
            self.assertNotIn(word, out)
        self.assertIn("qualifying counts withheld until Gate A can be read: 10 racing days and 300 usable markets, "
                      "and a session started on a later UTC date than the last of those dates", out)
        return summary["gate_a"]

    def test_gate_a_not_enough_markets(self):
        self.days(10, 300, per_day=29)
        gate = self.assert_withheld()
        self.assertEqual((gate["result"], gate["usable"], gate["through"]), ("NOT_ENOUGH_MARKETS", 290, None))
        self.assertEqual(lp.gate_a(10, 299, 299, recording_open=True), "NOT_ENOUGH_MARKETS")

    def test_gate_a_waits_until_the_recording_of_its_last_date_has_ended(self):
        """Amendment 4: however the last date's latest session stopped, the date is open until a session started on
        a later UTC date exists. A hand-started recording reaches its cap or its end instant at any time of day."""
        for session in ({"partial": True},                                     # running or crashed
                        {"stop_reason": "stop_requested"},                     # drained for live work
                        {"stop_reason": "reconnect_budget_exhausted"},
                        {"complete": False},
                        {"stop_reason": "max_duration_reached"},
                        {"stop_reason": "max_seconds_reached"},
                        {}):                                                   # end_at_reached
            with self.subTest(session=session):
                self.reset()
                self.days(10, 300, closed=False, **session)
                gate = self.assert_withheld()
                self.assertEqual((gate["result"], gate["recording_open"], gate["through"]),
                                 ("RECORDING_OPEN", True, "2026-12-10"))
                self.assertIn("pooled through 2026-12-10, whose recording is open until a session starts on a later "
                              "UTC date)", self.run_tool()[1])
                self.later_session(11)
                gate = self.summary()["gate_a"]
                self.assertEqual((gate["result"], gate["recording_open"], gate["primary"]), ("PASS", False, 300))
                self.assertIn("pooled through 2026-12-10, whose recording has ended;", self.run_tool()[1])
        self.reset()
        self.days(10, 300, closed=False)
        (self.root / "rec-20261210T200000Z-fffffff0").mkdir()                  # resumed that day, no receipt yet
        self.assertEqual(self.assert_withheld()["result"], "RECORDING_OPEN")
        (self.root / "rec-20261211T060000Z-fffffff1").write_text("x")          # not a session: ignored
        self.assertEqual(self.summary()["gate_a"]["result"], "RECORDING_OPEN")
        (self.root / "rec-20261211T070000Z-fffffff2").mkdir()                  # a session started on a later date
        gate = self.summary()["gate_a"]
        self.assertEqual((gate["result"], gate["recording_open"], gate["primary"]), ("PASS", False, 300))

    def test_a_part_recorded_last_date_is_never_decisive(self):
        """Amendment 4, the review's two cases. A hand-started session on the last date stops at its cap or its end
        instant, and recording goes on that afternoon. Under amendment 3 the first case printed FAIL (4 of 307) and
        then PASS (10 of 313), and the second PASS (5 of 300) and then FAIL (5 of 358). Now nothing is decided and no
        qualifying count is printed until a later date's session exists, and later data does not change the result."""
        for per_day, first_day, morning, afternoon, result in (
                (34, 4, (1, 0, "max_seconds_reached"), (6, 6), ("PASS", 10, 313)),
                (33, 5, (3, 0, "end_at_reached"), (58, 0), ("FAIL", 5, 358))):
            with self.subTest(stop_reason=morning[2]):
                self.reset()
                for date in range(1, 10):
                    self.day(date, per_day, first_day if date == 1 else 0)
                self.assertEqual(self.assert_withheld()["result"], "NOT_ENOUGH_DAYS")
                self.day(10, morning[0], morning[1], hour=11, stop_reason=morning[2])
                gate = self.assert_withheld()
                self.assertEqual((gate["result"], gate["through"]), ("RECORDING_OPEN", "2026-12-10"))
                self.day(10, *afternoon, hour=16)                              # end_at_reached, as the daily wrapper
                gate = self.assert_withheld()
                self.assertEqual((gate["result"], gate["through"]), ("RECORDING_OPEN", "2026-12-10"))
                self.later_session(11)
                decided = self.summary()["gate_a"]
                self.assertEqual((decided["result"], decided["primary"], decided["usable"]), result)
                self.day(11, 30, 30)                                           # a later date, every market qualifying
                self.assertEqual(self.summary()["gate_a"], decided)

    def test_a_later_date_is_later_across_a_month_and_a_year_end(self):
        """Amendment 4: what closes D is a later UTC date, not a later day of the month. Ten days from 2026-12-27 to
        2027-01-05: December's sessions, with their higher day numbers, do not close it; 2027-01-06 does."""
        dates = [f"2026-12-{n}" for n in range(27, 32)] + [f"2027-01-{n:02d}" for n in range(1, 6)]
        for index, date in enumerate(dates):
            off = timestamp_ms(f"{date}T14:35:00.000Z")
            markets = [(f"1.97{index:02d}{k:05d}", definition(off_text=_iso(off)), quotes(3.0, 3.15))
                       for k in range(30)]
            self.rec.session([[image(at(-690, off), *markets)] + beats(-690, 0, off)])
        gate = self.assert_withheld()
        self.assertEqual((gate["result"], gate["through"]), ("RECORDING_OPEN", "2027-01-05"))
        (self.root / "rec-20270106T070000Z-fffffff3").mkdir()
        gate = self.summary()["gate_a"]
        self.assertEqual((gate["result"], gate["through"], gate["primary"]), ("PASS", "2027-01-05", 300))

    def test_gate_a_is_fixed_by_its_dates_whenever_the_script_runs(self):
        self.days(10, 4)                                                       # 4 of 300: FAIL
        first = self.summary()
        self.days(5, 150, first=11)                                            # every later market qualifies
        later = self.summary()
        self.assertEqual(later["gate_a"], first["gate_a"])
        self.assertEqual((later["gate_a"]["result"], later["gate_a"]["through"], later["pooled"]["primary"]),
                         ("FAIL", "2026-12-10", 4))
        self.assertEqual([row["in_gate_a"] for row in later["days"]], [True] * 10 + [False] * 5)
        self.assertEqual(sum(row["primary"] for row in later["days"]), 154)    # later dates shown, not pooled
        self.assertIn("pooled and Gate A cover the dates through 2026-12-10; the 5 later dates are descriptive only",
                      self.run_tool()[1])

    def test_sessions_started_before_the_first_merge_are_not_read(self):
        """Amendment 1: a session that started before 2026-11-03 23:05:00 UTC adds no market, no date and no ended
        day, and its receipt is not read; read, these four would give 3 racing days of qualifying markets and a PASS."""
        for index, date in enumerate(("2026-10-13", "2026-10-19", "2026-10-19", "2026-10-22")):
            off = timestamp_ms(f"{date}T14:35:00.000Z")
            markets = [(f"1.98{index}{k:05d}", definition(off_text=_iso(off)), quotes(3.0, 3.15)) for k in range(30)]
            self.rec.session([[image(at(-690, off), *markets)] + beats(-690, 0, off)],
                             stop_reason="max_seconds_reached")
        gate = self.days(10, 4)                                                # December: 4 of 300
        self.assertEqual((gate["result"], gate["through"], gate["primary"], gate["usable"]),
                         ("FAIL", "2026-12-10", 4, 300))
        summary = self.summary()
        self.assertEqual([row["date"] for row in summary["days"]], [f"2026-12-{n:02d}" for n in range(1, 11)])
        self.assertEqual((summary["inputs"]["sessions"]["recorded_before_registration"],
                          summary["inputs"]["sessions"]["read"]), (4, 10))
        (self.root / "rec-20261103T230459Z-0000000a").mkdir()                  # one second early: not read
        (self.root / "rec-20261103T230500Z-0000000b").mkdir()                  # on time: read, and has no receipt
        with mock.patch.object(lp, "load_receipt", wraps=lp.load_receipt) as reader:
            summary = self.summary()
        opened = [call.args[0].name for call in reader.call_args_list]
        self.assertEqual(min(opened), "rec-20261103T230500Z-0000000b")          # no earlier receipt is even opened
        self.assertEqual(len(opened), 12)                                       # 10 days, this one, the later session
        self.assertEqual(summary["gate_a"], gate)
        self.assertEqual((summary["inputs"]["sessions"]["recorded_before_registration"],
                          summary["inputs"]["sessions"]["no_receipt"]), (5, 2))
        self.assertIn("recorded_before_registration 5", self.run_tool()[1])

    def test_this_registration_names_the_sha256_that_decides(self):
        """Amendment 4 names the deciding script's sha256, so the script cannot change without a new amendment.
        Amendment 3's line keeps the sha256 it named, which no longer decides."""
        page = (ROOT / "docs" / "lp-rate-count.md").read_text(encoding="utf-8")

        def decides(number):
            amendment = re.split(rf"\n### Amendment {number} \([^)\n]*\)\n", page, maxsplit=1)[1]
            amendment = re.split(r"\n##+ ", amendment, maxsplit=1)[0]             # up to the next heading
            line, = [line for line in amendment.splitlines() if line.startswith("- **Which run decides.**")]
            return re.findall(r"`([0-9a-f]{64})`", line)

        script = hashlib.sha256((ROOT / "scripts" / "lp_rate_count.py").read_bytes()).hexdigest()
        self.assertEqual(decides(4), [script])
        self.assertEqual(decides(3), ["fa6b4fbaae27d9cef6313aecad09c0fbccf0372b771adb807a335349e52f147a"])
        self.assertNotEqual(decides(3), decides(4))

    def test_side_counts_cover_the_same_dates_as_the_pool(self):
        """Revised and not-usable markets after Gate A's last date are not set against its usable markets."""
        self.days(10, 4)
        off = timestamp_ms("2026-12-11T14:35:00.000Z")
        revised = definition(off_text=_iso(at(300, off)), version=2)
        self.rec.session([seg(image(at(-900, off), (MARKET, definition(off_text=_iso(off)), quotes(3.0, 3.1)),
                                    ("1.999123402", definition(off_text=_iso(off), country="FR"), quotes(3.0, 3.1))),
                              delta(at(-600, off), md=revised), delta(at(-360, off), rc=quotes(3.0, 3.1)),
                              beats(-900, 300, off))])
        everything = self.counted()
        self.assertEqual((everything["schedule_revised"], everything["not_usable"]["out_of_population"]),
                         (1, 1))
        summary = self.summary()
        self.assertEqual((summary["gate_a"]["through"], summary["schedule_revised"]), ("2026-12-10", 0))
        self.assertEqual(summary["not_usable"], {code: 0 for code in lp.NOT_USABLE})
        self.assertIn("marketTime revised at or before off -590 s (counted, not excluded): 0 of 300 usable",
                      self.run_tool()[1])

    def test_until_gate_a_can_be_read_nothing_about_qualifying_markets_is_printed(self):
        self.days(9, 270)
        self.assertEqual(self.assert_withheld()["result"], "NOT_ENOUGH_DAYS")
        self.days(1, 0, first=20)                                           # the tenth racing day shows them all
        tenth = self.summary()
        self.assertFalse(tenth["counts_withheld"])
        self.assertEqual((tenth["gate_a"]["result"], tenth["pooled"]["primary"]), ("PASS", 270))

    def test_rates_round_down_so_a_fail_never_prints_at_the_threshold(self):
        self.assertEqual((lp.rate_bp(20, 1430), lp.rate_bp(279, 20000), lp.rate_bp(14, 1000)), (139, 139, 140))
        self.assertEqual(lp.gate_a(10, 20, 1430), "FAIL")
        self.assertEqual(lp._percent(lp.rate_bp(20, 1430)), "1.39%")
        self.assertIsNone(lp.rate_bp(0, 0))

    def test_a_date_with_no_usable_market_is_not_a_racing_day(self):
        off = timestamp_ms("2026-12-20T14:35:00.000Z")
        self.rec.session([[image(at(-900, off), (MARKET, definition(off_text=_iso(off)), quotes(3.0, 3.15)))]])
        summary = self.summary()
        self.assertEqual((summary["pooled"]["recorded"], summary["pooled"]["racing_days"]), (1, 0))
        self.assertFalse(summary["days"][0]["racing_day"])


class SessionTests(Case):
    def test_only_listed_closed_segments_of_delayed_sessions_are_read(self):
        good = market_lines(steady(3.0, 3.15))
        self.rec.session([good])
        self.rec.session([market_lines(steady(3.0, 3.15), market_id="1.999123471")], key_type="live")
        self.rec.session([market_lines(steady(3.0, 3.15), market_id="1.999123472")],
                         fields=FIELDS + ["EX_BEST_OFFERS_DISP"])
        self.rec.session([market_lines(steady(3.0, 3.15), market_id="1.999123473")], subscription=False)
        self.rec.session([market_lines(steady(3.0, 3.15), market_id="1.999123474")], replay_status="failed")
        crashed = self.rec.session([market_lines(steady(3.0, 3.1), market_id="1.999123475", start=-1190, end=-890)],
                                   partial=True,
                                   open_segment=market_lines(steady(3.0, 3.15), market_id="1.999123475", start=-790))
        tampered = self.rec.session([market_lines(steady(3.0, 3.15), market_id="1.999123476")])
        (tampered / "seg-000.jsonl").write_bytes((tampered / "seg-000.jsonl").read_bytes() + b"\n")
        self.rec.session([market_lines(steady(3.0, 3.15), market_id="1.999123477")], conflate_ms=None)
        self.rec.session([market_lines(steady(3.0, 3.15), market_id="1.999123478")], conflate_ms=60000)
        (self.root / "rec-20261110T000000Z-ffffffff").mkdir()               # no receipt
        (self.root / "rec-20261110T000001Z-fffffffe").write_text("x")       # a file, not a session directory
        (self.root / "rec-20261110T000002Z-fffffffd").symlink_to(tampered)  # a symlink is never followed
        (self.root / "index").mkdir()                                       # not a session
        summary = self.summary()
        self.assertEqual(summary["inputs"]["sessions"],
                         {"found": 12, "read": 4, "read_partial": 1, "recorded_before_registration": 0,
                          "not_a_directory": 2, "no_receipt": 1,
                          "receipt_unreadable": 0, "not_delayed_key": 1, "conflation_not_registered": 2,
                          "subscription_not_registered": 2})
        self.assertEqual(summary["inputs"]["segments"],
                         {"listed": 4, "read": 2, "unlisted": 1, "listing_failed": 0, "missing": 0, "unreadable": 0,
                          "hash_mismatch": 1, "replay_check_not_passed": 1})
        self.assertTrue(crashed.joinpath("seg-001.jsonl").is_file())
        # The good market qualifies; the crashed session's closed segment ends before -590 s, its open one is unread.
        counted = self.counted()
        self.assertEqual((counted["pooled"]["recorded"], counted["pooled"]["primary"]), (2, 1))
        self.assertEqual(counted["not_usable"]["no_snapshot"], 1)

    def test_output_is_deterministic(self):
        self.rec.session([market_lines(steady(3.0, 3.15))])
        self.rec.session([market_lines(steady(3.0, 3.2), market_id="1.999123402", off=at(3600))])
        first, second = self.run_tool(), self.run_tool()
        self.assertEqual(first, second)
        self.assertEqual(self.run_tool("--json"), self.run_tool("--json"))

    def test_refusals_print_a_fixed_code_only(self):
        repo = Path(self._tmp.name) / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "recordings").mkdir()
        a_file = Path(self._tmp.name) / "file"
        a_file.write_text("x")
        for argv, code in (([str(Path(self._tmp.name) / "missing")], "root_missing"),
                           ([str(repo / "recordings")], "root_inside_git_worktree"),
                           ([str(a_file)], "root_not_a_directory")):
            with self.subTest(code=code):
                rc, out, err = self.run_tool(root=argv[0])
                self.assertEqual((rc, out, err), (2, "", f"lp_rate_count: refused: {code}\n"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(lp.main(["--root"]), 2)
            self.assertEqual(lp.main(["--root", "x", "--SENTINEL-EVENT"]), 2)
        self.assertEqual(err.getvalue(), "lp_rate_count: refused: arguments\n" * 2)
        self.rec.session([market_lines(steady(3.0, 3.15))])
        with mock.patch.object(lp, "summarise", side_effect=RuntimeError("SENTINEL-EVENT 1.999123401")):
            rc, out, err = self.run_tool()
        self.assertEqual((rc, out, err), (2, "", "lp_rate_count: failed: unexpected_error (no counts printed)\n"))


def normalise(text):
    text = re.sub(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "DATE", text)
    text = re.sub(r"[0-9]+\.[0-9]{2}%", "PCT", text)
    return re.sub(r"[0-9]+", "N", re.sub(r" +", " ", text))


JSON_KEYS = {"schema", "registration", "script_sha256", "inputs", "sessions", "segments", "markets_without_off",
             "counts_withheld", "days", "date", "racing_day", "in_gate_a", "rates_bp", "pooled", "racing_days",
             "not_usable", "schedule_revised", "gate_a", "result", "through", "min_racing_days",
             "min_usable", "recording_open", "threshold_per_mille", "limits", "found", "read", "read_partial",
             "listed", "unlisted", "listing_failed", *lp.COLUMNS, *lp.NOT_USABLE, *lp.SESSION_SKIPS, *lp.SEGMENT_SKIPS}


class ProjectionTests(Case):
    def fixture(self, root, back, lay, market_id):
        """Ten racing days of 30 usable markets and a session started on a later date, so every column is printed."""
        rec = Recordings(root)
        (Path(root) / "rec-20261120T070000Z-ffffffff").mkdir()
        rc = quotes(back, lay) + [{"id": SELECTIONS[2], "batb": [[0, 7.4, 3.5]], "batl": [[0, 8.0, 2.5]]},
                                  {"id": SELECTIONS[3], "batb": [[0, 4.6, 3.5]], "batl": [[0, 5.1, 2.5]]}]
        for day in range(10):
            off = at(86_400 * day)
            day_market = market_id if day == 0 else f"{market_id[:-2]}{50 + day}"
            fillers = [(f"1.98{day:02d}{k:05d}", definition(off_text=_iso(off)), quotes(3.0, 3.1)) for k in range(29)]
            rec.session([seg(image(at(-1190, off), (day_market, definition(off_text=_iso(off)), rc), *fillers),
                             beats(-1190, 0, off), delta(at(-650, off), day_market, quotes(back, lay, others=False)),
                             [delta(at(-650, off), m, quotes(3.0, 3.1, others=False)) for m, _, _ in fillers])])
        rec.session([[image(at(-900), ("1.999123402", definition(country="FR"), rc))]])

    def test_no_id_name_price_or_time_reaches_the_output(self):
        self.fixture(self.root, 2.98, 3.1, MARKET)
        for flag in ((), ("--json",)):
            rc, out, err = self.run_tool(*flag)
            self.assertEqual((rc, err), (0, ""))
            for sentinel in SENTINELS:
                with self.subTest(flag=flag, sentinel=sentinel):
                    self.assertNotIn(sentinel, out)
            self.assertIsNone(re.search(r"[0-9]{1,2}:[0-9]{2}", out))      # no time of day
            self.assertIsNone(re.search(r"1\.[0-9]{9}", out))              # no market id shape

    def test_table_lines_follow_a_fixed_grammar(self):
        """With other ids, prices and dates the table differs only in numbers, percentages and dates."""
        self.fixture(self.root, 2.98, 3.1, MARKET)
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        self.fixture(other, 3.5, 3.65, "1.999123499")
        tomorrow = at(86_400)
        Recordings(other).session([[image(at(-690, tomorrow), ("1.999123498", definition(
            off_text=_iso(tomorrow)), quotes(5.9, 6.4)))] + beats(-690, 0, tomorrow)])
        mine, theirs = self.run_tool()[1], self.run_tool(root=other)[1]
        self.assertNotEqual(mine, theirs)
        self.assertEqual(set(normalise(mine).splitlines()), set(normalise(theirs).splitlines()))

    def test_json_holds_only_allowlisted_keys_and_plain_values(self):
        self.fixture(self.root, 2.98, 3.1, MARKET)
        summary = self.summary()
        allowed_text = {lp.SCHEMA, lp.REGISTRATION, lp.LIMITS, *lp.GATE_RESULTS}

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertIn(key, JSON_KEYS)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str):
                self.assertTrue(value in allowed_text or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value), value)
            else:
                self.assertTrue(value is None or type(value) in (int, bool), value)

        self.assertEqual(summary.pop("script_sha256"), hashlib.sha256(TOOL.read_bytes()).hexdigest())
        walk(summary)
        self.assertEqual((summary["gate_a"]["result"], summary["counts_withheld"]), ("PASS", False))

    def test_limits_are_worded_as_the_page_words_them(self):
        """Amendment 4: the count is mostly an upper bound, not a strict one, and the output says no more than that."""
        page = (ROOT / "docs" / "lp-rate-count.md").read_text(encoding="utf-8")
        self.assertTrue("It is mostly an upper bound, not a strict one" in page)
        self.assertIn("mostly an upper bound on the bot's rate, not live recall", lp.LIMITS)
        self.assertNotIn(": an upper bound", lp.LIMITS)
        self.rec.session([market_lines(steady(3.0, 3.15))])
        self.assertIn(f"Limits: {lp.LIMITS}. It decides nothing beyond Gate A.", self.run_tool()[1])

    def test_the_sha256_is_printed_where_the_page_says_to_check_it(self):
        """The page has the reader check the sha256 before reading on: line 2 of the table, under the title, and line
        4 of --json, under the schema and the registration. No count comes before it."""
        self.fixture(self.root, 2.98, 3.1, MARKET)
        sha256 = hashlib.sha256(TOOL.read_bytes()).hexdigest()
        table, printed = self.run_tool()[1].splitlines(), self.run_tool("--json")[1].splitlines()
        self.assertTrue(table[0].startswith(f"LP rate count ({lp.SCHEMA}; definitions in {lp.REGISTRATION})."))
        self.assertEqual(table[1], f"script sha256 {sha256}")
        self.assertEqual(printed[:4], ["{", f' "schema": "{lp.SCHEMA}",', f' "registration": "{lp.REGISTRATION}",',
                                       f' "script_sha256": "{sha256}",'])
        page = (ROOT / "docs" / "lp-rate-count.md").read_text(encoding="utf-8")
        self.assertIn("It is the second line of the table and the fourth line of `--json`", page)


class RecorderOutputTests(unittest.TestCase):
    def test_reads_the_recorders_own_session(self):
        """The real StreamRecorder, driven by the scripted stream, writes a session this count reads whole."""
        support.block_network(self)
        paths = support.temp_state_paths(self)
        start = "2026-11-10T14:23:30.000"                               # off - 690 s
        clock = support.FakeClock(start=start + "000Z")
        session = fx.FakeSession()
        script = fx.handshake() + [fx.change(lambda sid: fx.image(
            sid, timestamp_ms(start + "Z"), market_id=MARKET, definition=definition(), rc=quotes(3.0, 3.15),
            conflate_ms=180000))]
        # A heartbeat after -590 s keeps the session's span past the snapshot (a state is never carried further).
        script.append(fx.change(lambda sid: fx.heartbeat(sid, at(-585))))
        server = fx.FakeStreamServer([script], clock)
        StreamRecorder(fx.recorder_config(paths), paths=paths, session=session,
                       stream_factory=lambda: fx.make_client(server, session, clock),
                       market_lister=fx.lister([MARKET]), clock=clock, rng=random.Random(5),
                       emit=lambda e: None).run(stop_requested=server.exhausted)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lp.main(["--root", str(paths.recordings_dir), "--json"]), 0)
        summary = json.loads(out.getvalue())
        self.assertEqual((summary["inputs"]["sessions"]["read"], summary["inputs"]["segments"]["read"]), (1, 1))
        self.assertEqual((summary["pooled"]["usable"], summary["gate_a"]["result"]), (1, "NOT_ENOUGH_DAYS"))
        pooled = lp.tally(lp.Count().read_root(paths.recordings_dir))["pooled"]
        self.assertEqual((pooled["usable"], pooled["primary"]), (1, 1))
        self.assertEqual(pooled["flagged"], 0)                               # -780 s is before the session began


class ImportTests(unittest.TestCase):
    def test_imports_only_the_standard_library(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"), feature_version=(3, 10))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                imports.append(node.module)
        self.assertTrue({name.split(".")[0] for name in imports} <= set(sys.stdlib_module_names), imports)
        for name in imports:
            self.assertNotIn(name.split(".")[0], {"socket", "ssl", "http", "urllib", "subprocess", "betangel_research"})
            self.assertNotIn("betfair", name)
        self.assertNotIn(TOOL.stem.casefold(), {name.casefold() for name in sys.stdlib_module_names})

    def test_runs_isolated_as_documented_loads_nothing_networked_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "recordings"
            root.mkdir()
            Recordings(root).session([market_lines(steady(3.0, 3.15))])
            (Path(tmp) / "scripts").mkdir()
            tool = Path(tmp) / "scripts" / TOOL.name
            tool.write_bytes(TOOL.read_bytes())
            before = sorted(str(p) for p in Path(tmp).rglob("*"))
            probe = ("import importlib.util, sys\n"
                     "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
                     "m = importlib.util.module_from_spec(spec)\n"
                     "spec.loader.exec_module(m)\n"
                     "code = m.main(['--root', sys.argv[2]])\n"
                     # pathlib imports urllib.parse (string parsing only) on Python 3.12 and earlier
                     "bad = sorted(n for n in sys.modules if n.split('.')[0] in ('socket', '_socket', 'ssl', "
                     "'http', 'betangel_research', 'subprocess') or n.startswith('urllib.') and n != 'urllib.parse')\n"
                     "print('LOADED', code, bad, file=sys.stderr)\n")
            result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(tool), str(root)],
                                    capture_output=True, text=True, timeout=60, cwd=tmp, check=False)
            self.assertEqual(result.stderr.strip(), "LOADED 0 []")
            documented = subprocess.run([sys.executable, "-I", "-B", str(tool), "--root", str(root), "--json"],
                                        capture_output=True, text=True, timeout=60, cwd=tmp, check=False)
            self.assertEqual((documented.returncode, documented.stderr), (0, ""))
            printed = json.loads(documented.stdout)
            self.assertEqual((printed["pooled"]["usable"], printed["counts_withheld"]), (1, True))
            self.assertEqual(printed["script_sha256"], hashlib.sha256(TOOL.read_bytes()).hexdigest())
            self.assertEqual(sorted(str(p) for p in Path(tmp).rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
