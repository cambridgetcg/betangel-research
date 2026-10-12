"""Market book cache and snapshot tests on synthetic stream messages; not a check of live Betfair data."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from betangel_research import stream
from betangel_research.betfair.book import (MarketBookCache, ladder_type, replay_market_time,
                                            snapshots_from_recording)
from betangel_research.betfair.paper import PaperExchange
from betangel_research.betfair.receipts import utc_from_ns
from betangel_research.betfair.schemas import BOOK_SNAPSHOT_KEYS, validate_book_snapshot

import test_betfair_support as support
import test_betfair_support_orders as orders
import test_betfair_support_stream as fx


def runner(snapshot, rid=11):
    return snapshot["runners"][str(rid)]


class BookTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")

    def apply(self, msg, **kwargs):
        return self.cache.apply(msg, **kwargs)

    def start(self, rc=None, **image_kwargs):
        self.apply(fx.image(1, fx.pt_at(-300), rc=rc, **image_kwargs), received_at=support.BASE_UTC,
                   monotonic_ns=7)

    def test_img_replaces_market(self):
        self.start()
        self.apply(fx.delta(1, fx.pt_at(-299), [{"id": 11, "atb": [[3.0, 5]], "trd": [[2.5, 1]]}]))
        replacement = {"id": "1.1", "img": True, "marketDefinition": fx.market_definition(version=2),
                       "rc": [{"id": 12, "batb": [[0, 4.0, 7]]}]}
        changed = self.apply(fx.mcm(1, fx.pt_at(-298), [replacement]))
        self.assertEqual(changed, ["1.1"])
        snap = self.cache.snapshot("1.1")
        self.assertEqual(runner(snap, 11)["back"], [])           # nothing survives from before the image
        self.assertIsNone(runner(snap, 11)["traded"])
        self.assertEqual(runner(snap, 12)["back"], [[4.0, 7.0]])
        self.assertEqual(snap["version"], 2)
        self.assertEqual(snap["pt"], fx.pt_at(-298))

    def test_sub_image_drops_absent_markets(self):
        self.apply(fx.image(1, fx.pt_at(-300), extra_markets=[("1.2", fx.market_definition(), [])]))
        self.assertEqual(self.cache.markets(), ["1.1", "1.2"])
        self.assertEqual(self.apply(fx.image(1, fx.pt_at(-200))), ["1.1"])
        self.assertEqual(self.cache.markets(), ["1.1"])
        with self.assertRaisesRegex(ValueError, "unknown_market"):
            self.cache.snapshot("1.2")
        # An image may restart pt.
        self.apply(fx.image(1, fx.pt_at(-400)))
        self.assertEqual(self.cache.markets(), ["1.1"])
        # A non-image delta touching one market never drops the others.
        self.apply(fx.image(1, fx.pt_at(-100), extra_markets=[("1.2", fx.market_definition(), [])]))
        self.assertEqual(self.apply(fx.delta(1, fx.pt_at(-99), [{"id": 11, "ltp": 2.2}])), ["1.1"])
        self.assertEqual(self.cache.markets(), ["1.1", "1.2"])
        self.assertEqual(self.cache.snapshot("1.2")["market_id"], "1.2")

    def test_level_ladder_size_zero_deletes(self):
        self.start(rc=[{"id": 11, "batb": [[0, 2.0, 30], [1, 1.98, 99], [2, 1.96, 5]], "batl": [[0, 2.02, 10]]}])
        self.apply(fx.delta(1, fx.pt_at(-299), [{"id": 11, "batb": [[2, 1.96, 0]]}]))
        self.assertEqual(runner(self.cache.snapshot("1.1"))["back"], [[2.0, 30.0], [1.98, 99.0]])
        # An empty level empties every deeper level too (best-N ladders are contiguous).
        self.apply(fx.delta(1, fx.pt_at(-298), [{"id": 11, "batb": [[2, 1.95, 7], [1, 0, 0]]}]))
        self.assertEqual(runner(self.cache.snapshot("1.1"))["back"], [[2.0, 30.0]])
        self.assertEqual(self.cache._markets["1.1"]["runners"][("11", 0.0)]["batb"], {0: [2.0, 30.0]})
        # [0,0,0]: the whole ladder is empty.
        self.apply(fx.delta(1, fx.pt_at(-297), [{"id": 11, "batb": [[0, 0, 0]]}]))
        self.assertEqual(runner(self.cache.snapshot("1.1"))["back"], [])
        self.assertEqual(self.cache._markets["1.1"]["runners"][("11", 0.0)]["batb"], {})
        self.apply(fx.delta(1, fx.pt_at(-296), [{"id": 11, "batb": [[0, 1.9, 4]]}]))
        snap = self.cache.snapshot("1.1")
        self.assertEqual(runner(snap)["back"], [[1.9, 4.0]])
        self.assertEqual(self.cache.counters["ladder_prefix_truncations"], 0)
        self.assertEqual(runner(snap)["lay"], [[2.02, 10.0]])
        # A level ladder that breaks price order is cut at the break, never reordered.
        cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
        cache.apply(fx.image(1, fx.pt_at(-300), rc=[{"id": 11, "batb": [[0, 2.0, 30], [1, 2.02, 5]]}]))
        self.assertEqual(runner(cache.snapshot("1.1"))["back"], [[2.0, 30.0]])
        self.assertEqual(cache.counters["ladder_prefix_truncations"], 1)

    def test_emptied_level_ladder_never_resurfaces(self):
        """Regression: depth withdrawn by [0,0,0] must not come back when level 0 is refilled at a
        better price (paper fills walk these levels)."""
        for field, levels, better, expected in (
                ("batb", [[0, 2.0, 30], [1, 1.98, 99], [2, 1.96, 5]], [0, 2.1, 4], "back"),
                ("batl", [[0, 2.02, 10], [1, 2.04, 20], [2, 2.06, 8]], [0, 1.99, 3], "lay")):
            with self.subTest(field=field):
                cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
                cache.apply(fx.image(1, fx.pt_at(-300), rc=[{"id": 11, field: levels}]))
                cache.apply(fx.delta(1, fx.pt_at(-299), [{"id": 11, field: [[0, 0, 0]]}]))
                self.assertEqual(runner(cache.snapshot("1.1"))[expected], [])
                cache.apply(fx.delta(1, fx.pt_at(-298), [{"id": 11, field: [better]}]))
                snap = cache.snapshot("1.1")
                self.assertEqual(runner(snap)[expected], [[better[1], float(better[2])]])
                self.assertIsNone(snap["invalid_reason"])

    def test_price_ladder_deletes(self):
        self.start(rc=[{"id": 11, "atb": [[2.0, 30], [1.99, 5]], "atl": [[2.02, 10], [2.04, 3]]}])
        self.apply(fx.delta(1, fx.pt_at(-299), [{"id": 11, "atb": [[1.99, 0], [1.98, 7]], "atl": [[2.04, 0]]}]))
        snap = self.cache.snapshot("1.1")
        self.assertEqual(runner(snap)["back"], [[2.0, 30.0], [1.98, 7.0]])
        self.assertEqual(runner(snap)["lay"], [[2.02, 10.0]])
        # Full depth wins for the whole market once atb/atl is seen; runners without it show empty ladders.
        self.assertEqual(runner(snap, 12)["back"], [])

    def test_empty_list_semantics_price_point_only(self):
        self.start(rc=[{"id": 11, "atb": [[2.0, 30]], "atl": [[2.02, 10]]}])
        self.apply(fx.delta(1, fx.pt_at(-299), [{"id": 11, "atb": []}]))
        snap = self.cache.snapshot("1.1")
        self.assertEqual(runner(snap)["back"], [])                # price-point ladder: [] empties it
        self.assertEqual(runner(snap)["lay"], [[2.02, 10.0]])
        cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
        cache.apply(fx.image(1, fx.pt_at(-300), rc=[{"id": 11, "batb": [[0, 2.0, 30]], "batl": [[0, 2.02, 10]]}]))
        cache.apply(fx.delta(1, fx.pt_at(-299), [{"id": 11, "batb": [], "batl": []}]))
        snap = cache.snapshot("1.1")
        self.assertEqual(runner(snap)["back"], [[2.0, 30.0]])     # level ladder: [] changes nothing
        self.assertEqual(runner(snap)["lay"], [[2.02, 10.0]])

    def test_bdat_empty_list_is_filtered_not_empty(self):
        self.start(rc=[{"id": 11, "bdatb": [[0, 2.0, 50]], "bdatl": [[0, 2.04, 20]]}])
        self.apply(fx.delta(1, fx.pt_at(-299), [{"id": 11, "bdatb": [], "bdatl": []}]))
        state = self.cache._markets["1.1"]["runners"][("11", 0.0)]
        self.assertEqual(state["bdatb"], {0: [2.0, 50.0]})        # [] = filtered by ladderLevels, not empty
        self.assertEqual(state["bdatl"], {0: [2.04, 20.0]})
        # Virtual ladders are never reported as availability.
        self.assertEqual(runner(self.cache.snapshot("1.1"))["back"], [])

    def test_infinity_token_and_string_invalidates_snapshot(self):
        for size in (float("inf"), "Infinity"):
            with self.subTest(size=size):
                cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
                raw = fx.line(fx.image(1, fx.pt_at(-300), rc=[{"id": 11, "batb": [[0, 2.0, size]],
                                                               "batl": [[0, 2.02, 10]]}]))
                if size == "Infinity":
                    self.assertIn(b'"Infinity"', raw)
                else:
                    self.assertIn(b",Infinity]", raw)
                cache.apply(json.loads(raw))
                snap = cache.snapshot("1.1")
                self.assertEqual(runner(snap)["back"], [[2.0, None]])
                self.assertEqual(snap["invalid_reason"], "non_finite_size")
                self.assertGreaterEqual(cache.counters["non_finite_seen"], 1)
                json.dumps(snap, allow_nan=False)
                # A finite replacement clears it.
                cache.apply(fx.delta(1, fx.pt_at(-299), [{"id": 11, "batb": [[0, 2.0, 12]]}]))
                self.assertIsNone(cache.snapshot("1.1")["invalid_reason"])
        # A non-finite virtual size also invalidates (conservative), though virtual ladders are never reported.
        for size in (float("inf"), "Infinity"):
            with self.subTest(virtual_size=size):
                cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
                cache.apply(fx.image(1, fx.pt_at(-300), rc=[{"id": 11, "bdatb": [[0, 2.0, size]]}]))
                snap = cache.snapshot("1.1")
                self.assertEqual(snap["invalid_reason"], "non_finite_size")
                self.assertEqual(runner(snap)["back"], [])
                self.assertEqual(cache.counters["non_finite_seen"], 1)
        # A non-finite price poisons the runner until the next image.
        cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
        cache.apply(fx.image(1, fx.pt_at(-300), rc=[{"id": 11, "atb": [["Infinity", 4]]}]))
        snap = cache.snapshot("1.1")
        self.assertEqual(snap["invalid_reason"], "non_finite_size")
        self.assertEqual(runner(snap)["back"], [])

    def test_settlement_zeroing_ignored(self):
        self.start(rc=[{"id": 11, "trd": [[2.0, 100], [2.02, 40]], "batb": [[0, 2.0, 3]], "batl": [[0, 2.02, 4]]}])
        self.apply(fx.delta(1, fx.pt_at(-10), [{"id": 11, "trd": [[2.0, 0], [2.02, 0]]}]))
        self.assertEqual(runner(self.cache.snapshot("1.1"))["traded"], {"2.0": 100.0, "2.02": 40.0})
        self.assertEqual(self.cache.counters["settlement_zeroing_seen"], 1)
        self.apply(fx.delta(1, fx.pt_at(-9), [{"id": 11, "trd": []}]))
        self.assertEqual(self.cache.counters["settlement_zeroing_seen"], 2)
        # Removing one point among several is an ordinary delete.
        self.apply(fx.delta(1, fx.pt_at(-8), [{"id": 11, "trd": [[2.02, 0], [2.04, 5]]}]))
        self.assertEqual(runner(self.cache.snapshot("1.1"))["traded"], {"2.0": 100.0, "2.04": 5.0})
        self.assertEqual(self.cache.counters["settlement_zeroing_seen"], 2)

    def test_suspension_clears_ladders_no_forward_fill(self):
        self.start(rc=[{"id": 11, "batb": [[0, 2.0, 30]], "batl": [[0, 2.02, 10]], "ltp": 2.0, "tv": 50,
                        "trd": [[2.0, 50]]}])
        suspended = fx.market_definition(status="SUSPENDED", version=2)
        self.apply(fx.mcm(1, fx.pt_at(-200), [{"id": "1.1", "marketDefinition": suspended,
                                                "rc": [{"id": 11, "batb": [[0, 2.1, 9]], "ltp": 2.1}]}]))
        snap = self.cache.snapshot("1.1")
        self.assertEqual(snap["status"], "SUSPENDED")
        self.assertEqual((runner(snap)["back"], runner(snap)["lay"], runner(snap)["ltp"]), ([], [], None))
        self.assertEqual(runner(snap)["traded"], {"2.0": 50.0})      # cumulative volume is history, not a quote
        self.assertEqual(self.cache.counters["deltas_dropped_while_not_open"], 1)
        reopened = fx.market_definition(status="OPEN", version=3)
        self.apply(fx.mcm(1, fx.pt_at(-190), [{"id": "1.1", "marketDefinition": reopened,
                                                "rc": [{"id": 11, "batl": [[0, 2.06, 4]]}]}]))
        snap = self.cache.snapshot("1.1")
        self.assertEqual(runner(snap)["back"], [])                   # nothing carried through the suspension
        self.assertEqual(runner(snap)["lay"], [[2.06, 4.0]])

    def test_full_depth_ladder_partial_after_reopen_until_image(self):
        """Regression (audit): after SUSPENDED -> OPEN a full-depth (atb/atl) ladder holds only the points
        re-sent since reopening, so an interior price not re-sent read as empty and paper set its queue
        to 0. Such snapshots now say so (invalid_reason) until the market's next image; level ladders
        are unaffected because their contiguous prefix stops at the first level not re-sent."""
        self.start(rc=[{"id": 11, "atb": [[1.98, 40]], "atl": [[2.0, 50], [2.02, 100], [2.04, 30]],
                        "trd": [[2.02, 500]]}])
        self.assertIsNone(self.cache.snapshot("1.1")["invalid_reason"])
        self.apply(fx.mcm(1, fx.pt_at(-290), [{"id": "1.1",
                                                "marketDefinition": fx.market_definition(status="SUSPENDED")}]))
        self.assertIsNone(self.cache.snapshot("1.1")["invalid_reason"])           # empty while suspended
        self.apply(fx.mcm(1, fx.pt_at(-280), [{"id": "1.1", "marketDefinition": fx.market_definition(status="OPEN"),
                                                "rc": [{"id": 11, "atl": [[2.0, 60], [2.04, 30]]}]}]))
        snap = self.cache.snapshot("1.1")
        self.assertEqual(runner(snap)["lay"], [[2.0, 60.0], [2.04, 30.0]])
        self.assertEqual(snap["invalid_reason"], "ladder_partial_after_suspension")
        exchange = PaperExchange(orders.order_config()["orders"]["paper"])
        order = {"customer_order_ref": "bro200101120000-00000000000000b1", "market_id": "1.1", "selection_id": 11,
                 "side": "BACK", "price_h": 202, "size_p": 1000}
        report = exchange.place([order], book=snap, now_ms=snap["pt"])
        self.assertEqual(report["instructions"][0]["error_code"], "paper_book_invalid")      # no queue of 0
        # A new image of the market makes the ladder whole again.
        self.apply(fx.image(1, fx.pt_at(-270), rc=[{"id": 11, "atl": [[2.0, 60], [2.02, 90], [2.04, 30]]}]))
        self.assertIsNone(self.cache.snapshot("1.1")["invalid_reason"])
        # Level ladders after a reopen stay valid.
        levels = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
        levels.apply(fx.image(1, fx.pt_at(-300), rc=[{"id": 11, "batl": [[0, 2.0, 50], [1, 2.02, 100]]}]))
        levels.apply(fx.mcm(1, fx.pt_at(-290), [{"id": "1.1",
                                                 "marketDefinition": fx.market_definition(status="SUSPENDED")}]))
        levels.apply(fx.mcm(1, fx.pt_at(-280), [{"id": "1.1", "marketDefinition": fx.market_definition(status="OPEN"),
                                                 "rc": [{"id": 11, "batl": [[1, 2.04, 30]]}]}]))
        level_snap = levels.snapshot("1.1")
        self.assertIsNone(level_snap["invalid_reason"])
        self.assertEqual(runner(level_snap)["lay"], [])                 # level 0 not re-sent: nothing shown

    def test_status_503_flags_cache_until_healthy_message(self):
        self.start()
        self.assertFalse(self.cache.stream_unreliable)
        self.apply(dict(fx.delta(1, fx.pt_at(-299), [{"id": 11, "batb": [[0, 2.1, 9]]}]), status=503),
                   received_at="2020-01-01T11:55:01.000001Z")
        self.assertTrue(self.cache.stream_unreliable)
        self.assertEqual(self.cache.counters["stream_unreliable_seen"], 1)
        with self.assertRaisesRegex(ValueError, "stream_unreliable"):
            self.cache.snapshot("1.1")                            # possibly stale: no snapshot at all
        # A 503 heartbeat keeps the flag; the next message without a status clears it.
        self.apply(dict(fx.heartbeat(1, fx.pt_at(-298)), status=503))
        self.assertTrue(self.cache.stream_unreliable)
        self.apply(fx.heartbeat(1, fx.pt_at(-297)))
        self.assertFalse(self.cache.stream_unreliable)
        self.assertEqual(runner(self.cache.snapshot("1.1"))["back"], [[2.1, 9.0], [1.98, 99.0]])
        self.assertEqual(self.cache.counters["stream_unreliable_seen"], 2)
        # Recordings: a market changed under a 503 is yielded unusable (stream_unreliable), then usable again.
        msgs = [fx.image(1, fx.pt_at(-300)), dict(fx.delta(1, fx.pt_at(-299), [{"id": 11, "ltp": 2.2}]), status=503),
                fx.delta(1, fx.pt_at(-298), [{"id": 11, "ltp": 2.4}])]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seg-000.jsonl"
            path.write_bytes(b"".join(fx.line(m, b"\n") for m in msgs))
            results = list(snapshots_from_recording(path, key_type="historical"))
        self.assertEqual([(pt, s["invalid_reason"]) for pt, s in results],
                         [(fx.pt_at(-300), None), (fx.pt_at(-299), "stream_unreliable"), (fx.pt_at(-298), None)])
        self.assertEqual(runner(results[1][1])["ltp"], 2.2)
        self.assertEqual(runner(results[-1][1])["ltp"], 2.4)

    def test_change_under_a_503_is_not_lost_when_the_market_goes_quiet(self):
        """Regression (audit): a market changed by a status-503 message (in-play here) was never yielded
        unless it changed again later, so paper replay kept trading on the superseded pre-play book."""
        msgs = [fx.image(1, fx.pt_at(-300)),
                fx.delta(1, fx.pt_at(-101), [{"id": 11, "ltp": 2.0}]),
                fx.mcm(1, fx.pt_at(-100), [{"id": "1.1", "marketDefinition": fx.market_definition(inPlay=True,
                                                                                                  version=2)}],
                       status=503),
                dict(fx.heartbeat(1, fx.pt_at(-99.5)), status=503),
                fx.heartbeat(1, fx.pt_at(-99)),
                fx.delta(1, fx.pt_at(-50), [{"id": 12, "tv": 500}])]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seg-000.jsonl"
            path.write_bytes(b"".join(fx.line(m, b"\n") for m in msgs))
            results = list(snapshots_from_recording(path, key_type="synthetic"))
        self.assertEqual([(pt - support.BASE_PT, s["inplay"], s["invalid_reason"]) for pt, s in results],
                         [(-300000, False, None), (-101000, False, None), (-100000, True, "stream_unreliable"),
                          (-99000, True, None), (-50000, True, None)])

    def recorded(self, msgs, *, sidecar=False):
        """snapshots_from_recording over msgs as [(pt, market_id, invalid_reason, snapshot pt)] (plus the
        snapshots), with a receipt sidecar whose received_at is pt + 0.5 s and monotonic_ns the line number."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seg-000.jsonl"
            path.write_bytes(b"".join(fx.line(m, b"\n") for m in msgs))
            if sidecar:
                rows = [{"line": n, "pt": m["pt"], "received_at": utc_from_ns(m["pt"] * 1_000_000 + 500_000_000),
                         "monotonic_ns": n, "byte_offset": 0, "bytes": 0, "sha256": "0" * 64, "clk": None}
                        for n, m in enumerate(msgs, 1)]
                path.with_name("seg-000.recv.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            results = list(snapshots_from_recording(path, key_type="synthetic"))
        return [(pt, s["market_id"], s["invalid_reason"], s["pt"]) for pt, s in results], [s for _, s in results]

    def two_markets(self, pt):
        return fx.image(1, pt, extra_markets=[("1.2", fx.market_definition(), fx.runners_adv())])

    def test_global_503_without_mc_invalidates_every_cached_market(self):
        """Regression C3 (Astra): a 503 heartbeat with no mc yielded no snapshot at all, so the next healthy
        delta looked like contiguous, tradable history. Entering the 503 window now yields every cached
        market unusable at the 503's pt, and a second 503 in the same window repeats only what it changes."""
        t0 = fx.pt_at(-100)
        rows, _ = self.recorded([self.two_markets(t0), dict(fx.heartbeat(1, t0 + 1000), status=503),
                                 dict(fx.heartbeat(1, t0 + 1500), status=503),
                                 fx.delta(1, t0 + 2000, [{"id": 11, "trd": [[2.02, 104.0]]}])])
        self.assertEqual(rows, [
            (t0, "1.1", None, t0), (t0, "1.2", None, t0),
            (t0 + 1000, "1.1", "stream_unreliable", t0 + 1000), (t0 + 1000, "1.2", "stream_unreliable", t0 + 1000),
            (t0 + 2000, "1.2", None, t0 + 2000), (t0 + 2000, "1.1", None, t0 + 2000)])

    def test_503_on_another_market_invalidates_this_one_too(self):
        """Regression C3 (Astra): a 503 carrying a change of 1.2 only left 1.1 looking healthy throughout."""
        t0 = fx.pt_at(-100)
        rows, _ = self.recorded([self.two_markets(t0),
                                 dict(fx.delta(1, t0 + 1000, [{"id": 12, "ltp": 2.1}], market_id="1.2"), status=503),
                                 dict(fx.delta(1, t0 + 1500, [{"id": 11, "ltp": 2.2}]), status=503),
                                 fx.delta(1, t0 + 2000, [{"id": 11, "trd": [[2.02, 104.0]]}])])
        self.assertEqual(rows, [
            (t0, "1.1", None, t0), (t0, "1.2", None, t0),
            (t0 + 1000, "1.2", "stream_unreliable", t0 + 1000), (t0 + 1000, "1.1", "stream_unreliable", t0 + 1000),
            (t0 + 1500, "1.1", "stream_unreliable", t0 + 1500),
            (t0 + 2000, "1.2", None, t0 + 2000), (t0 + 2000, "1.1", None, t0 + 2000)])

    def test_recovery_reyields_every_market_at_the_healthy_pt(self):
        """C3: a market yielded at a message that did not change it carries that message's pt, observed_at and
        monotonic_ns (its data is as of then), at the 503 and at the healthy message that ends the window."""
        t0 = fx.pt_at(-100)
        msgs = [self.two_markets(t0), dict(fx.heartbeat(1, t0 + 1000), status=503),
                fx.delta(1, t0 + 2000, [{"id": 11, "ltp": 2.2}])]
        rows, snaps = self.recorded(msgs, sidecar=True)
        quiet = [s for s in snaps if s["market_id"] == "1.2"]
        self.assertEqual([(s["pt"], s["observed_at"], s["monotonic_ns"], s["invalid_reason"]) for s in quiet], [
            (t0, utc_from_ns(t0 * 1_000_000 + 500_000_000), 1, None),
            (t0 + 1000, utc_from_ns((t0 + 1000) * 1_000_000 + 500_000_000), 2, "stream_unreliable"),
            (t0 + 2000, utc_from_ns((t0 + 2000) * 1_000_000 + 500_000_000), 3, None)])
        self.assertEqual(runner(quiet[-1])["back"], runner(quiet[0])["back"])          # same data, newer stamp
        # A paper order resting on 1.1 sees an unusable interval on each side of the 503: nothing fills.
        self.assertEqual([r[2] for r in rows if r[1] == "1.1"], [None, "stream_unreliable", None])

    def test_image_taken_while_suspended_marks_full_depth_partial(self):
        """Regression (audit): ladder_partial_after_suspension needed full_depth set before the ladders were
        cleared, so an image that arrived while the market was not OPEN (every segment's first image,
        after a reconnect or refresh) dropped its atb/atl unmarked, and paper read the missing interior
        price as a queue of 0 after the reopen."""
        self.start(definition=fx.market_definition(status="SUSPENDED"),
                   rc=[{"id": 11, "atl": [[2.04, 20], [2.06, 10]], "trd": [[2.04, 100]]}])
        self.apply(fx.mcm(1, fx.pt_at(-290), [{"id": "1.1", "marketDefinition": fx.market_definition(status="OPEN")}]))
        self.apply(fx.delta(1, fx.pt_at(-280), [{"id": 11, "atl": [[2.06, 11]]}]))
        snap = self.cache.snapshot("1.1")
        self.assertEqual(runner(snap)["lay"], [[2.06, 11.0]])
        self.assertEqual(snap["invalid_reason"], "ladder_partial_after_suspension")
        exchange = PaperExchange(orders.order_config()["orders"]["paper"])
        order = {"customer_order_ref": "bro200101120000-00000000000000b2", "market_id": "1.1", "selection_id": 11,
                 "side": "BACK", "price_h": 204, "size_p": 200}
        report = exchange.place([order], book=snap, now_ms=snap["pt"])
        self.assertEqual(report["instructions"][0]["error_code"], "paper_book_invalid")
        # An image while OPEN makes it whole; a suspended image with no atb/atl drops nothing and marks nothing.
        self.apply(fx.image(1, fx.pt_at(-270), rc=[{"id": 11, "atl": [[2.04, 20], [2.06, 11]]}]))
        self.assertIsNone(self.cache.snapshot("1.1")["invalid_reason"])
        quiet = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
        quiet.apply(fx.image(1, fx.pt_at(-300), definition=fx.market_definition(status="SUSPENDED"),
                             rc=[{"id": 11, "trd": [[2.04, 100]]}]))
        quiet.apply(fx.mcm(1, fx.pt_at(-290), [{"id": "1.1", "marketDefinition": fx.market_definition(status="OPEN"),
                                                 "rc": [{"id": 11, "atl": [[2.04, 20]]}]}]))
        self.assertIsNone(quiet.snapshot("1.1")["invalid_reason"])

    def test_market_time_must_parse_on_python_3_10(self):
        for value, expected in (("2020-01-01T12:00:00Z", True), ("2020-01-01T12:00:00.000Z", True),
                                ("2020-01-01T12:00:00.000000+00:00", True), ("2020-01-01T12:00:00.5Z", False),
                                ("2020-01-01T12:00:00.50Z", False), ("2020-01-01T12:00:00.0001Z", False),
                                ("2020-01-01T12:00:00.000001Z", False), ("2020-01-01T12:00:00", False), (None, False)):
            with self.subTest(value=value):
                self.assertEqual(replay_market_time(value) is not None, expected)
        self.start(definition=fx.market_definition(marketTime="2020-01-01T12:00:00.5Z"))
        self.assertIsNone(self.cache.snapshot("1.1")["market_time"])

    def test_duplicate_market_higher_version_kept(self):
        low = {"id": "1.1", "img": True, "marketDefinition": fx.market_definition(version=7, eventId="old"),
               "rc": [{"id": 11, "batb": [[0, 9.0, 1]]}]}
        high = {"id": "1.1", "img": True, "marketDefinition": fx.market_definition(version=9, eventId="new"),
                "rc": [{"id": 11, "batb": [[0, 3.0, 2]]}]}
        for order in ([low, high], [high, low]):
            with self.subTest(first=order[0]["marketDefinition"]["version"]):
                cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
                cache.apply(fx.mcm(1, fx.pt_at(-300), order, ct="SUB_IMAGE"))
                snap = cache.snapshot("1.1")
                self.assertEqual(snap["version"], 9)
                self.assertEqual(runner(snap)["back"], [[3.0, 2.0]])
                self.assertEqual(cache.counters["duplicate_definition_dropped"], 1)
        # Outside an image a lower version is applied: version is not assumed monotonic.
        cache = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
        cache.apply(fx.mcm(1, fx.pt_at(-300), [high], ct="SUB_IMAGE"))
        cache.apply(fx.mcm(1, fx.pt_at(-299), [{"id": "1.1", "marketDefinition": fx.market_definition(version=8)}]))
        self.assertEqual(cache.snapshot("1.1")["version"], 8)

    def test_ladder_type_object_and_string(self):
        self.assertEqual(ladder_type({"priceLadderDefinition": {"type": "CLASSIC"}}), "CLASSIC")
        self.assertEqual(ladder_type({"priceLadderDefinition": "FINEST"}), "FINEST")
        for value in (None, {}, {"type": 3}, 5, ""):
            self.assertIsNone(ladder_type({"priceLadderDefinition": value}))
        self.assertIsNone(ladder_type({}))
        self.assertIsNone(ladder_type(None))
        self.start(definition=fx.market_definition(priceLadderDefinition="CLASSIC"))
        self.assertEqual(self.cache.snapshot("1.1")["price_ladder_type"], "CLASSIC")

    def test_snapshot_schema_valid(self):
        self.start()
        snap = self.cache.snapshot("1.1")
        self.assertEqual(set(snap), BOOK_SNAPSHOT_KEYS)
        validate_book_snapshot(snap)
        expected = {"market_id": "1.1", "source": "stream_cache", "fetched_by": "stream_cache", "key_type": "synthetic",
                    "app_key_slot": None, "observed_at": support.BASE_UTC, "monotonic_ns": 7, "pt": fx.pt_at(-300),
                    "status": "OPEN", "inplay": False, "version": 1, "bet_delay": 0, "is_market_data_delayed": None,
                    "catalogue_present": True, "market_time": fx.MARKET_TIME, "number_of_winners": 1,
                    "betting_type": "ODDS", "market_type": "WIN", "event_type_id": "7",
                    "price_ladder_type": "CLASSIC", "invalid_reason": None}
        self.assertEqual({k: snap[k] for k in expected}, expected)
        self.assertEqual(runner(snap), {"status": "ACTIVE", "handicap": 0.0, "ltp": 2.0,
                                        "back": [[2.0, 30.0], [1.98, 99.0]], "lay": [[2.02, 10.0]],
                                        "traded": {"2.0": 100.0}, "total_matched": 100.0})
        json.dumps(snap, allow_nan=False)
        # Without a receipt time, observed_at falls back to publish time.
        cache = MarketBookCache(key_type="delayed", app_key_slot="delayed", source="recording")
        cache.apply(fx.image(1, fx.pt_at(-300)))
        snap = cache.snapshot("1.1")
        self.assertEqual((snap["observed_at"], snap["fetched_by"]), ("2020-01-01T11:55:00.000000Z", "recording"))
        # Handicap runners are left out (instruction v1 is handicap 0 only); a market without a definition
        # cannot be snapshotted.
        definition = fx.market_definition(runners=[{"id": 11, "hc": 0, "status": "ACTIVE"},
                                                   {"id": 11, "hc": 1.5, "status": "ACTIVE"}])
        cache.apply(fx.image(1, fx.pt_at(-299), definition=definition))
        self.assertEqual(list(cache.snapshot("1.1")["runners"]), ["11"])
        self.assertEqual(cache.counters["handicap_runners_omitted"], 1)
        bare = MarketBookCache(key_type="synthetic", app_key_slot=None, source="stream_cache")
        bare.apply(fx.mcm(1, 5, [{"id": "1.1", "rc": [{"id": 11, "ltp": 2.0}]}]))
        with self.assertRaisesRegex(ValueError, "market_definition_missing"):
            bare.snapshot("1.1")

    def test_malformed_input_refused(self):
        self.start()
        bad = [fx.mcm(1, True), fx.mcm(1, 1.5), dict(fx.mcm(1, fx.pt_at(-200)), segmentType="SEG_START"),
               fx.delta(1, fx.pt_at(-200), [{"id": 11, "batb": [[0, 2.0]]}]),
               fx.delta(1, fx.pt_at(-200), [{"id": 11, "batb": [[11, 2.0, 1]]}]),
               fx.delta(1, fx.pt_at(-200), [{"id": 11, "batb": [[0, 2.0, -1]]}]),
               fx.delta(1, fx.pt_at(-200), [{"id": 11, "atb": [[0.5, 3]]}]),
               fx.delta(1, fx.pt_at(-200), [{"id": True, "ltp": 2.0}]),
               fx.mcm(1, fx.pt_at(-400), [])]
        for msg in bad:
            with self.subTest(msg=msg):
                with self.assertRaises(ValueError):
                    self.apply(msg)
        self.assertEqual(self.apply({"op": "status", "id": 1}), [])

    def test_snapshots_from_recording_uses_stream_messages(self):
        msgs = [fx.image(1, fx.pt_at(-300)), fx.heartbeat(1, fx.pt_at(-299)),
                fx.delta(1, fx.pt_at(-298), [{"id": 11, "batb": [[0, 2.04, 6]], "batl": [[0, 2.06, 1]]}])]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seg-000.jsonl"
            path.write_bytes(b"".join(fx.line(m, b"\n") for m in msgs))
            with patch.object(stream, "messages", wraps=stream.messages) as reader:
                results = list(snapshots_from_recording(path, key_type="delayed"))
            reader.assert_called_once_with(path)
            self.assertEqual([pt for pt, _ in results], [fx.pt_at(-300), fx.pt_at(-298)])
            last = results[-1][1]
            self.assertEqual((last["source"], last["key_type"], last["app_key_slot"]), ("recording", "delayed", "delayed"))
            self.assertEqual(runner(last)["back"], [[2.04, 6.0], [1.98, 99.0]])
            self.assertEqual(last["observed_at"], "2020-01-01T11:55:02.000000Z")    # no sidecar: publish time
            # With the recorder's receipt sidecar, observed_at is the local receipt time of that line.
            rows = [{"line": n, "pt": m["pt"], "received_at": f"2020-01-01T12:0{n}:00.000001Z", "monotonic_ns": n,
                     "byte_offset": 0, "bytes": 0, "sha256": "0" * 64, "clk": None} for n, m in enumerate(msgs, 1)]
            path.with_name("seg-000.recv.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            results = list(snapshots_from_recording(path, key_type="historical"))
            self.assertEqual(results[-1][1]["observed_at"], "2020-01-01T12:03:00.000001Z")
            self.assertEqual(results[-1][1]["monotonic_ns"], 3)
            self.assertIsNone(results[-1][1]["app_key_slot"])
            path.with_name("seg-000.recv.jsonl").write_text(json.dumps(rows[0]) + "\n")
            with self.assertRaisesRegex(ValueError, "shorter"):
                list(snapshots_from_recording(path, key_type="historical"))
            # stream.messages() rules apply: a decreasing pt is refused.
            path.with_name("seg-000.recv.jsonl").unlink()
            path.write_bytes(b"".join(fx.line(m, b"\n") for m in (msgs[2], msgs[0])))
            with self.assertRaisesRegex(ValueError, "decreasing pt"):
                list(snapshots_from_recording(path, key_type="historical"))


if __name__ == "__main__":
    unittest.main()
