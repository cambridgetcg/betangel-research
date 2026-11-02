"""Registered measurement lp-v1 (betangel_research.betfair.backtest.lp and lp_study).

Every archive here is synthetic and goes through the real ingest, index and replay, with the fixture helpers of
tests/test_betfair_backtest_replay.py and tests/test_backtest_movement.py's PRO ladder deltas; selection ids, market
ids and the event id are distinctive, so the output scan finds any that leak. These tests show the study's rules on
these synthetic shapes only; they say nothing about real Betfair files or results.
"""

import bz2
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import random
import stat
import subprocess
import sys
import unittest

from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, ingest_archive
from betangel_research.betfair.backtest.contract import canonical_json
from betangel_research.betfair.backtest.lp import (BACK, BSP_HORIZON, CLASSES, CODES, CROSS, FILL_KINDS, JOIN, LAY,
                                                   MAIN, MID_SERIES, SERIES, SIDES, THROUGH, TRD_COUNTS, VOLUME, Leg,
                                                   RegistrationError, TradedFold, adjacent, after_the_hour,
                                                   after_the_hour_ms, against, bsp_book, cr1_test, decide, edge,
                                                   markouts, null_markouts, null_time, parse_registration, penny,
                                                   tick_step, valued, valued_normalised, wide)
from betangel_research.betfair.backtest.lp_study import (Decision, LegRow, Tally, _filled_legs, _fills, _markouts,
                                                         _net, _realised, _stats, _strata, _trd_expected, _walk,
                                                         decide_record, main, markdown, run_study, summary)
from betangel_research.betfair.backtest.market_index import index_archives
from betangel_research.betfair.backtest.movement import MARKET_CODES, exact, nclv, tick_position, valid_bsp
from betangel_research.betfair.book import MarketBookCache

import test_betfair_backtest_replay as fx
from test_backtest_movement import Ladders, walk_keys
from test_betfair_backtest_replay import OFF, definition, iso, mcm, md, ms, runner

REPO = Path(__file__).resolve().parent.parent
REGISTRATION = REPO / "research" / "studies" / "lp-v1.json"
REG = parse_registration(REGISTRATION.read_bytes())
# The registration's sha256 as reviewed: any change to the file must change this line too (a new registration).
REGISTRATION_SHA256 = "76edf3da303c7a86400b494502c8459670ba0d2aa7c45914250ec37f04f80d3c"
DOC = REPO / "docs" / "lp-study.md"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
FAV, R2, R3, R4, R5, R6 = SIDS = (51000001, 51000002, 51000003, 51000004, 51000005, 51000006)
# R3 (6.0 / 6.6, three ticks) and R4 (8.0 / 9.0, five) are eligible; R3 has the lower best back and is selected: main
# LAY 6.2 and BACK 6.4, join LAY 6.0 (50 shown) and BACK 6.6 (75 shown). R6 is above the band.
BOOK = {FAV: (2.5, 2.52), R2: (4.0, 4.1), R3: (6.0, 6.6), R4: (8.0, 9.0), R5: (10.0, 10.5), R6: (15.0, 16.0)}
BSPS = {FAV: 2.6, R2: 4.2, R3: 6.4, R4: 8.6, R5: 11.0, R6: 17.0}
M = "1.180000001"
OFFSETS = (-600, -300, -120)
C = ms(OFF) - 10_000                     # the cancel time, off - 10 s


def before(seconds, off=OFF):
    """The ms timestamp `seconds` before the off."""
    return ms(off) - round(seconds * 1000)


def book(**changes):
    """The standard book with some runners' quotes changed, by name (R3=(6.2, 6.8))."""
    named = dict(zip(("FAV", "R2", "R3", "R4", "R5", "R6"), SIDS))
    return {**BOOK, **{named[name]: quote for name, quote in changes.items()}}


def touched_levels(books, seconds, sid, points):
    """The ladder entries a trade at each of points' prices leaves in its batch without moving the book: the shown
    size again at the runner's best back or best lay then, else a deletion of the empty level (a level created and
    matched in the batch). A real trade changes the level it traded at (lp-v1 data.traded_volume.rule)."""
    shown = [key for key in books if key >= seconds and sid in books[key]]
    back, lay, *size = books[min(shown)][sid] if shown else (None, None)
    atb, atl = [], []
    for price, _ in points:
        if back is not None and exact(price) == exact(back):
            atb.append([price, size[0] if size else 50.0])
        elif lay is not None and exact(price) == exact(lay):
            atl.append([price, 75.0])
        else:
            atb.append([price, 0])
    return {key: entries for key, entries in (("atb", atb), ("atl", atl)) if entries}   # [] would empty a ladder


def lp_market(market_id=M, off=OFF, books=None, trades=(), restatements=(), extra=(), runners=SIDS, bsps=None,
              statuses=None, reconciled=True, opening_minutes=120, tail=(), bad_tail=False, **fields):
    """A PRO market's messages: its definition at off - opening_minutes; ladder deltas from books {seconds before the
    off: {sid: (back, lay[, back size])}} (default the standard book an hour before the off); trades [(seconds before
    the off, sid, [[price, cumulative volume], ...])] as rc trd deltas with the ladder entries touched_levels gives;
    restatements in the same form as trd deltas alone, no level changing (a currency restatement); extra messages;
    then BSP reconciled and CLOSED after the off (FAV wins by default), then tail; bad_tail appends an unreadable
    line."""
    ladders, opening = Ladders("PRO"), off - timedelta(minutes=opening_minutes)
    timed = [(ms(opening), md(market_id, opening, definition(off, [runner(sid) for sid in runners], **fields)))]
    books = books or {3600: BOOK}
    for seconds, quotes in sorted(books.items(), reverse=True):
        when = before(seconds, off)
        rc = ladders.rc({sid: quote for sid, quote in quotes.items() if sid in runners})
        timed.append((when, mcm(when, [{"id": market_id, "rc": rc}])))
    for seconds, sid, points in trades:
        when = before(seconds, off)
        levels = touched_levels(books, seconds, sid, points)
        timed.append((when, mcm(when, [{"id": market_id, "rc": [{"id": sid, **levels, "trd": points}]}])))
    for seconds, sid, points in restatements:
        when = before(seconds, off)
        timed.append((when, mcm(when, [{"id": market_id, "rc": [{"id": sid, "trd": points}]}])))
    timed += [(message["pt"], message) for message in extra]
    statuses = statuses or {sid: "WINNER" if sid == FAV else "LOSER" for sid in runners}
    bsps = BSPS if bsps is None else bsps
    final = [runner(sid, statuses[sid], bsps.get(sid) if statuses[sid] in ("WINNER", "LOSER") else None)
             for sid in runners]
    settled = off + timedelta(minutes=8)
    after = [md(market_id, off + timedelta(minutes=2), definition(off, final, in_play=True, bspReconciled=reconciled,
                                                                  **fields)),
             md(market_id, settled, definition(off, final, status="CLOSED", in_play=True, bspReconciled=reconciled,
                                               settledTime=iso(settled), **fields))]
    messages = [message for _, message in sorted(timed, key=lambda item: item[0])] + after + list(tail)
    return messages + ([b"not json\n"] if bad_tail else [])


def status_at(seconds, status="OPEN", in_play=False, off=OFF, market_id=M, runners=SIDS, **fields):
    """A definition published `seconds` before the off (negative: after it)."""
    return md(market_id, before(seconds, off), definition(off, [runner(sid) for sid in runners], status=status,
                                                          in_play=in_play, **fields))


def path(market_id, when=OFF):
    return fx.path(market_id, when=when, plan="PRO")


def outcome_of(tally):
    """{offset: the one market code, the issued class} of a tally holding one record."""
    found = {}
    for offset in OFFSETS:
        codes = [code for code, count in tally.codes[offset].items() if count]
        classes = [name for name, count in tally.classes[offset].items() if count]
        found[offset] = classes[0] if codes == ["issued"] else codes[0]
    return found


def P(value):
    return exact(value)


class LpCase(fx.Workspace):
    def walk(self, messages, registration=REG, offsets=OFFSETS):
        root, manifest, records = self.build([(path(M), messages)])
        return _walk(root, manifest, records[M], registration, list(offsets), DEFAULT_LIMITS)

    def legs(self, messages, **kwargs):
        """{(offset, role, side): Leg} of one market's walk."""
        return {(leg.offset_s, leg.role, leg.side): leg for leg in self.walk(messages, **kwargs).legs}

    def filled(self, messages):
        """{(offset, role, side): fill kind} of every leg that filled."""
        return {key: leg.fill_kind for key, leg in self.legs(messages).items() if leg.fill_pt is not None}

    def decide(self, members, registration=REG):
        root, manifest, records = self.build(members)
        tally = Tally(registration)
        for market_id in sorted(records):
            decide_record(registration, records[market_id], root, manifest, tally)
        return tally

    def one(self, messages):
        return self.decide([(path(M), messages)])


# ---------------------------------------------------------------------------------------------------------------

class RegistrationTests(unittest.TestCase):
    def mutated(self, change):
        value = json.loads(REGISTRATION.read_bytes())
        change(value)
        return json.dumps(value).encode()

    def test_the_repository_registration_parses_and_is_named_by_its_bytes(self):
        raw = REGISTRATION.read_bytes()
        self.assertEqual(REG.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(REG.sha256, REGISTRATION_SHA256)
        self.assertEqual((REG.decision_offsets_s, REG.cancel_offset_s, REG.horizons_s), (OFFSETS, -10, (10, 30, 60)))
        self.assertEqual((REG.tiers, REG.divisor, REG.stake, REG.improve_ticks, REG.min_spread_ticks,
                          REG.max_spread_ticks), (("PRO",), 2, Fraction(2), 1, 3, 20))
        self.assertEqual(REG.spread_buckets, (("S1", 3, 4), ("S2", 5, 7), ("S3", 8, 20)))
        self.assertEqual((REG.after_the_hour_s, REG.latency_s), (60, (1, 2)))
        self.assertEqual(FILL_KINDS, (THROUGH, CROSS, VOLUME))
        self.assertEqual((REG.event_type_id, REG.countries, REG.market_type, REG.min_active_runners),
                         ("7", ("GB", "IE"), "WIN", 5))
        self.assertEqual((REG.best_back_min_h, REG.best_back_max_h, REG.commission), (150, 1000, Fraction(2, 100)))
        self.assertEqual((REG.secondary_horizon_s, REG.null_seed, REG.min_tested_bets, REG.min_tested_race_dates),
                         (60, 3, 200, 15))
        self.assertEqual((REG.holm_alpha, REG.kill_alpha, REG.kill_fraction, REG.power),
                         (Fraction(1, 20), Fraction(1, 20), Fraction(1, 2), Fraction(4, 5)))
        self.assertEqual(REG.months[0], "2015-05")
        self.assertEqual(len(REG.months), 12)
        self.assertEqual(REG.horizons, (10, 30, 60, BSP_HORIZON))

    def test_movement_v1s_gates_and_predicates_keep_their_order(self):
        self.assertEqual(CODES[:14], MARKET_CODES[:14])
        self.assertEqual(CODES[13], "ladder_malformed")
        self.assertEqual(CLASSES[:4], ("outcome_unknown", "void_own_runner_removed", "post_decision_removal",
                                       "result_revised_after_close"))

    def test_the_text_says_what_the_code_does(self):
        value = json.loads(REGISTRATION.read_bytes())
        self.assertIn("certificate key", value["summary"])
        self.assertIn("4339", value["scope"])
        traded = value["data"]["traded_volume"]["rule"]
        self.assertIn("x 2", traded)
        for cited in ("SA/360006560978", "SA/360002401937", "2.38 + 41.12", "absolute", "foreign currency"):
            self.assertIn(cited, traded)
        self.assertIn("in precedence order", value["orders"]["fill_rule"])
        self.assertIn("partly mechanical", value["controls"]["join"]["failure"])
        self.assertIn("rarely the market favourite", value["population"]["selection_rule"])
        self.assertIn("3.335", value["measurement"]["valuation"])
        self.assertIn("(1 - c) - (L - c) / BSP", value["measurement"]["valuation"])
        self.assertIn("sum of 1 / BSP over the market's runners with a valid BSP", value["measurement"]["valuation"])
        registered = value["registered_pass"]
        self.assertIn("never a decision", registered["discovery"]["rule"])
        self.assertIn("realised spread is not significantly negative", registered["discovery"]["rule"])
        self.assertIn("picked off but paid for it", registered["discovery"]["rule"])
        self.assertIn("judged unadjusted", registered["realised_spread"]["rule"])
        self.assertIn("never supports story (a) on its own", registered["secondary"]["rule"])
        self.assertIn("off - 70 s", registered["secondary"]["rule"])
        self.assertIn("counts only (no mean, SE or interval)", registered["tests"]["rule"])
        self.assertIn("Inference, not checked on real files", traded)
        self.assertIn("rises_not_counted_at_touch_outside_the_hour", traded)
        self.assertIn("spread_above_max at the horizon", value["measurement"]["markouts"]["rule"])
        self.assertIn("q(1 - a) - q(a)", value["statistics"]["rule"])
        self.assertIn("2 significant figures", value["strata"]["volume_rule"])
        self.assertIn("counts only", value["blind_pass"]["releases"][6])
        self.assertIn("touched before their fill", value["blind_pass"]["releases"][7])
        self.assertIn("edge at fill over the main legs only", value["blind_pass"]["releases"][7])
        self.assertIn("by-side groups (LAY, BACK) in ticks only", value["blind_pass"]["releases"][7])
        self.assertIn("Inference, from a synthetic probe only", value["blind_pass"]["rule"])
        self.assertIn("plausibly below that bound (95%, unadjusted)", registered["secondary"]["rule"])
        self.assertIn("reason, underpowered", value["statistics"]["rule"])
        self.assertIn("over exactly its decisions or legs", registered["releases"])
        self.assertIn("both sides pooled", value["power_note"])
        self.assertNotIn("minus the group's nearest-rank median", json.dumps(value["blind_pass"]))
        self.assertTrue(all(ord(ch) < 128 for ch in REGISTRATION.read_text(encoding="utf-8")))

    def test_the_doc_says_what_the_registration_does(self):
        doc = DOC.read_text(encoding="utf-8")
        minus = "\u2212"                                                                  # the doc's minus sign
        for text in ("picked off but paid for it", "A single registered pass", "into the body of the next PR",
                     "blind_receipt:another_code", "at its own commit, 147f807",
                     "*Inference, not checked on real files:*", "rises_not_counted_at_touch_outside_the_hour",
                     "`spread_above_max`", f"q95 {minus} q05", "but only if BSP is calibrated",
                     "displayed impact of the filling order", f"a fill after about off {minus} 70 s",
                     "pass the kill rule", "never supports story (a) on its own",
                     "plausibly below that bound (95%, unadjusted)", "never the join or null legs",
                     "give them in ticks only", "*inference*, from a synthetic probe only",
                     "every descriptive group over exactly its decisions or legs"):
            with self.subTest(text=text):
                self.assertIn(text, doc)
        self.assertNotIn("each minus their group's median", doc)

    def test_a_changed_rule_is_refused(self):
        def reorder(value):
            codes = value["population"]["codes"]
            codes[-1], codes[-2] = codes[-2], codes[-1]

        cases = {
            "population.codes:not_the_implemented_list": reorder,
            "top:keys_not_the_registered_set": lambda value: value.update(extra="x"),
            "data.tiers:not_supported": lambda value: value["data"].update(tiers=["ADVANCED", "PRO"]),
            "data.traded_volume.divisor:not_the_basis": lambda value: value["data"]["traded_volume"].update(divisor=1),
            "data.traded_volume.basis:not_supported":
                lambda value: value["data"]["traded_volume"].update(basis="backers_stake"),
            "timing.trials:not_supported": lambda value: value["timing"].update(trials="pooled"),
            "timing.cancel_offset_s:not_after_every_decision":
                lambda value: value["timing"].update(cancel_offset_s=-120),
            "population.runners_per_market:not_supported":
                lambda value: value["population"].update(runners_per_market="every_eligible"),
            "population.min_spread_ticks:lay_not_below_back":
                lambda value: value["population"].update(min_spread_ticks=2),
            "orders.legs:not_the_implemented_list": lambda value: value["orders"].update(legs=["BACK", "LAY"]),
            "measurement.commission_rounding:not_supported":
                lambda value: value["measurement"].update(commission_rounding="per_market_ceil_penny"),
            "measurement.markouts.series:not_the_implemented_list":
                lambda value: value["measurement"]["markouts"].update(series=["mid_ticks"]),
            "registered_pass.secondary.horizon_s:not_a_markout_horizon":
                lambda value: value["registered_pass"]["secondary"].update(horizon_s=45),
            "strata.price_bands_h:bands_not_contiguous":
                lambda value: value["strata"]["price_bands_h"][1].__setitem__(1, 305),
            "strata.spread_buckets_ticks:bands_not_contiguous":
                lambda value: value["strata"]["spread_buckets_ticks"][1].__setitem__(1, 6),
            "strata.spread_buckets_ticks:last_band_not_max_spread":
                lambda value: value["strata"]["spread_buckets_ticks"][2].__setitem__(2, None),
            "population.max_spread_ticks:not_an_int_in_range":
                lambda value: value["population"].update(max_spread_ticks=2),
            "orders.fill_kinds:not_the_implemented_list":
                lambda value: value["orders"].update(fill_kinds=["volume_at_price", "quote_cross", "trade_through"]),
            "diagnostics.latency_s:not_sorted_and_unique": lambda value: value["diagnostics"].update(latency_s=[2, 1]),
            "diagnostics:keys_not_the_registered_set": lambda value: value["diagnostics"].pop("after_the_hour_s"),
            "controls.null.label:not_supported": lambda value: value["controls"]["null"].update(label="lp/{side}"),
            "uncertainty.method:not_supported": lambda value: value["uncertainty"].update(method="bootstrap"),
            "status:not_supported": lambda value: value.update(status="draft"),
            "statistics.quantiles:not_symmetric": lambda value: value["statistics"].update(quantiles=["0.05", "0.5"]),
            "measurement.markouts.undefined_reasons:not_the_implemented_list":
                lambda value: value["measurement"]["markouts"]["undefined_reasons"].pop(),
            "registered_pass.realised_spread.measure:not_supported":
                lambda value: value["registered_pass"]["realised_spread"].update(measure="mid_points"),
            "registered_pass:keys_not_the_registered_set":
                lambda value: value["registered_pass"].pop("realised_spread"),
        }
        for code, change in cases.items():
            with self.subTest(code=code), self.assertRaises(RegistrationError) as caught:
                parse_registration(self.mutated(change))
            self.assertEqual(str(caught.exception), f"registration:{code}")
        with self.assertRaises(RegistrationError):
            parse_registration(b"{not json")

    def test_a_greyhound_registration_is_data_not_code(self):
        def greyhounds(value):
            value["population"].update(event_type_id="4339", min_active_runners=5)

        reg = parse_registration(self.mutated(greyhounds))
        self.assertEqual(reg.event_type_id, "4339")
        self.assertNotEqual(reg.sha256, REG.sha256)


class TradedFoldTests(unittest.TestCase):
    def test_rises_from_the_last_published_value(self):
        fold = TradedFold()
        self.assertEqual(fold.change(1, {"rc": [{"id": 7, "trd": [[6.2, 4.0], [6.4, 2.0]]}]}),
                         [(7, [(P(6.2), 4), (P(6.4), 2)], 0)])
        self.assertEqual(fold.change(2, {"rc": [{"id": 7, "trd": [[6.2, 6.0]]}]}), [(7, [(P(6.2), 2)], 0)])
        # A currency decrease is counted and becomes the last value, so a later trade rises from it, not from 6.
        self.assertEqual(fold.change(3, {"rc": [{"id": 7, "trd": [[6.2, 5.9]]}]}), [(7, [], 1)])
        self.assertEqual(fold.change(4, {"rc": [{"id": 7, "trd": [[6.2, 5.98]]}]}), [(7, [(P(6.2), P(0.08))], 0)])
        self.assertEqual((fold.points[7][P(6.2)], fold.last[7][P(6.2)], fold.decreases), (P(5.98), P(5.98), [3]))
        self.assertEqual(fold.change(5, {"rc": [{"id": 7, "trd": [[6.4, 0]]}]}), [(7, [], 0)])  # 0 deletes the point
        self.assertEqual(set(fold.points[7]), {P(6.2)})
        self.assertEqual(fold.last[7][P(6.4)], 2)                                            # but not its last value
        self.assertEqual(fold.change(6, {"rc": [{"id": 7, "trd": [[6.4, 2.0]]}]}), [(7, [], 0)])
        self.assertEqual(fold.change(7, {"rc": [{"id": 7, "trd": None, "atb": [[6.0, 3]]}]}), [])
        self.assertEqual(fold.matched(), P(7.98))
        # Only walk_market decides which rises are trades; count() accumulates those.
        self.assertEqual(fold.counted, {})
        fold.count(7, P(6.2), 4)
        fold.count(7, P(6.2), P(0.08))
        self.assertEqual(fold.counted, {7: {P(6.2): P(4.08)}})

    def test_settlement_zeroing_and_images(self):
        fold = TradedFold()
        fold.change(1, {"rc": [{"id": 7, "trd": [[6.2, 4.0], [6.4, 2.0]]}]})
        fold.change(2, {"rc": [{"id": 7, "trd": []}]})                                       # F54: ignored
        fold.change(3, {"rc": [{"id": 7, "trd": [[6.2, 0], [6.4, 0]]}]})                     # ignored too
        self.assertEqual((fold.zeroing, fold.points[7]), ([2, 3], {P(6.2): 4, P(6.4): 2}))
        # An image replaces the points with those it carries; the last values stay, so a re-sent value is no rise.
        self.assertEqual(fold.change(4, {"img": True, "rc": [{"id": 7, "trd": [[6.2, 4.0]]}]}), [(7, [], 0)])
        self.assertEqual(fold.points[7], {P(6.2): 4})
        self.assertEqual(fold.change(5, {"rc": [{"id": 7, "trd": [[6.4, 2.5]]}]}), [(7, [(P(6.4), P(0.5))], 0)])

    def test_malformed_entries_are_recorded_never_guessed(self):
        for entries in ("6.2", [[6.2]], [[6.2, -1]], [[1.0, 5]], [[True, 5]], [[None, 0]], [[6.2, "4"]]):
            with self.subTest(entries=entries):
                fold = TradedFold()
                self.assertEqual(fold.change(9, {"rc": [{"id": 7, "trd": entries}]}), [])
                self.assertEqual(fold.malformed, [9])

    def test_the_points_agree_with_book_pys_traded_ladder(self):
        """TradedFold's points follow book.py's _apply_traded (F54) on seeded random sequences."""
        rng = random.Random(20261101)
        prices = [price / 100 for price in (600, 620, 640, 660, 680)]
        for case in range(300):
            cache = MarketBookCache(key_type="historical", app_key_slot=None, source="recording")
            state, fold = {"trd": None, "poisoned": False}, TradedFold()
            for pt in range(rng.randint(1, 8)):
                updates = [[rng.choice(prices), rng.choice((0, 0, 2.0, 4.0, 10.5))]
                           for _ in range(rng.choice((0, 1, 1, 2, 3)))]
                cache._apply_traded(state, [list(update) for update in updates])
                fold.change(pt, {"rc": [{"id": 7, "trd": updates}]})
            with self.subTest(case=case):
                ours = {float(price): float(volume) for price, volume in fold.points.get(7, {}).items()}
                self.assertEqual(ours, state["trd"] or {})


class ClockAndLadderHelperTests(unittest.TestCase):
    def test_after_the_hour_on_weekdays(self):
        monday = ms(OFF)                                    # 2015-06-01 14:00 UTC, a Monday
        self.assertTrue(after_the_hour(monday, 60))
        self.assertTrue(after_the_hour(monday + 59_999, 60))
        self.assertFalse(after_the_hour(monday + 60_000, 60))
        self.assertFalse(after_the_hour(monday - 1, 60))
        self.assertTrue(after_the_hour(monday + 4 * 86_400_000, 60))           # Friday
        self.assertFalse(after_the_hour(monday + 5 * 86_400_000, 60))          # Saturday
        self.assertFalse(after_the_hour(monday - 86_400_000, 60))              # Sunday

    def test_after_the_hour_share_of_a_window(self):
        off = ms(OFF) + 300_000                                                 # 14:05
        self.assertEqual(after_the_hour_ms(off - 600_000, off - 10_000, 60), 60_000)
        self.assertEqual(after_the_hour_ms(off - 300_000, off - 10_000, 60), 59_999)  # (t, C]: 14:00:00.000 is t
        self.assertEqual(after_the_hour_ms(off - 120_000, off - 10_000, 60), 0)
        self.assertEqual(after_the_hour_ms(ms(OFF) - 600_000, ms(OFF) - 10_000, 60), 0)
        saturday = ms(OFF) + 5 * 86_400_000
        self.assertEqual(after_the_hour_ms(saturday - 1, saturday + 90_000, 60), 0)
        # A window over two hour starts counts both.
        self.assertEqual(after_the_hour_ms(ms(OFF) - 3_630_000, ms(OFF) + 30_000, 60), 90_001)

    def test_adjacent_prices(self):
        self.assertTrue(adjacent([P(6.0), P(6.2), P(6.4)]))
        self.assertTrue(adjacent([P(1.99), P(2.0), P(2.02)]))
        self.assertFalse(adjacent([P(4.0), P(6.2), P(9.0)]))
        self.assertFalse(adjacent([P(6.0), P(6.1), P(6.2)]))                     # 6.1 is off the ladder


class LadderArithmeticTests(unittest.TestCase):
    def test_tick_steps_and_the_main_prices(self):
        self.assertEqual(tick_step(P(6.0), 1), P(6.2))
        self.assertEqual(tick_step(P(6.6), -1), P(6.4))
        self.assertEqual(tick_step(P(1.99), 1), P(2.0))
        self.assertEqual(tick_step(P(2.0), 1), P(2.02))
        self.assertIsNone(tick_step(P(6.1), 1))
        self.assertIsNone(tick_step(P(1000), 1))

    def test_the_move_against_a_leg(self):
        # A BACK is hurt when the price lengthens, a LAY when it shortens; 6.3 lies half way between 6.2 and 6.4.
        self.assertEqual(against(BACK, P(6.3), P(6.5), "ticks"), 1)
        self.assertEqual(against(LAY, P(6.3), P(6.5), "ticks"), -1)
        self.assertEqual(against(BACK, P(6.3), P(6.5), "points"), Fraction(100) / P(6.3) - Fraction(100) / P(6.5))
        self.assertEqual(against(LAY, P(5.0), P(4.0), "points"), 5)                       # 25 points - 20 points


class ValuationTests(unittest.TestCase):
    S, BSP = Fraction(2), P(6.4)

    def test_penny_rounding_at_the_boundary(self):
        self.assertEqual(penny(Fraction(5, 1000)), Fraction(1, 100))                      # a half penny rounds up
        self.assertEqual(penny(Fraction(4999, 1000000)), 0)
        self.assertEqual(penny(Fraction(3335, 1000)), Fraction(334, 100))                 # the General Rules' example
        self.assertEqual(penny(Fraction(3333, 1000)), Fraction(333, 100))
        self.assertEqual(penny(Fraction(0)), 0)

    def test_commission_under_25p_of_net_pays_nothing(self):
        # A round trip pays stake x (B - L) if the runner wins: GBP 0.25 at 1.25 x 0.2 pays 1p, GBP 0.249 nothing.
        at = replace(REG, stake=Fraction(125, 100))
        net, unrounded, effect = valued(at, P(5.0), P(5.2), P(5.0))
        self.assertEqual((effect, net), ("rounded_up", (Fraction(1, 4) - Fraction(1, 100)) / 5))
        self.assertEqual(unrounded, Fraction(1, 4) * Fraction(98, 100) / 5)
        under = replace(REG, stake=Fraction(249, 100))
        net, unrounded, effect = valued(under, P(5.0), P(5.1), P(5.0))
        self.assertEqual((effect, net), ("rounded_to_zero", Fraction(249, 1000) / 5))

    def test_the_back_is_contract_p1s_nclv_and_the_lay_its_analogue(self):
        c = REG.commission
        for price, bsp in ((6.4, 6.4), (6.4, 5.8), (2.02, 2.1), (9.8, 8.0)):
            with self.subTest(price=price, bsp=bsp):
                _, back, _ = valued(REG, None, P(price), P(bsp))
                self.assertEqual(back, self.S * nclv(P(price), P(bsp), c))
                _, lay, _ = valued(REG, P(price), None, P(bsp))
                self.assertEqual(lay, self.S * ((1 - c) - (P(price) - c) / P(bsp)))
        # At L = BSP the lay's cost is the back's: -c(BSP - 1) / BSP per pound.
        _, lay, _ = valued(REG, self.BSP, None, self.BSP)
        self.assertEqual(lay, self.S * -c * (self.BSP - 1) / self.BSP)

    def test_hand_worked_decisions(self):
        bsp = self.BSP
        # A lone LAY at 6.2: wins +2 (commission 4p, exact) when the runner loses, pays 10.40 when it wins.
        self.assertEqual(valued(REG, P(6.2), None, bsp),
                         (Fraction(2875, 100000), Fraction(2875, 100000), "exact"))
        # A lone BACK at 6.4: wins 10.80 (commission 21.6p, settled 22p) or loses 2.
        net, unrounded, effect = valued(REG, None, P(6.4), bsp)
        self.assertEqual((net, unrounded, effect), (Fraction(-34375, 1000000), Fraction(-3375, 100000), "rounded_up"))
        # A round trip, BACK 6.4 above LAY 6.2: 40p if the runner wins (commission 0.8p, settled 1p), else nothing.
        self.assertEqual(valued(REG, P(6.2), P(6.4), bsp),
                         (Fraction(39, 100) / bsp, Fraction(40, 100) * Fraction(98, 100) / bsp, "rounded_up"))
        self.assertEqual(valued(REG, None, None, bsp), (0, 0, "no_positive_outcome"))


class MarkoutUnitTests(unittest.TestCase):
    def walk(self, status=(), in_play=(), unreadable_from=None):
        market = type("Fold", (), {"fields": {"status": [[0, "OPEN"], *status], "inPlay": [[0, False], *in_play]}})
        return type("Walk", (), {"market": market, "unreadable_from": unreadable_from})

    QUOTE = ((P(6.0), 50), (P(6.6), 75))

    def test_each_undefined_reason(self):
        captures = {10: ((P(6.2), 5), (P(6.8), 5)), 30: ((P(6.2), 5), None), 60: None}
        values = markouts(REG, self.walk(status=[[45_000, "SUSPENDED"]]), LAY, 1_000, self.QUOTE, captures, P(6.4))
        self.assertEqual(values["mid_ticks", 10], -1.0)
        self.assertEqual(values["mid_ticks", 30], "not_two_sided")
        self.assertEqual(values["opposite_ticks", 30], "not_two_sided")
        self.assertEqual(values["mid_ticks", 60], "member_unreadable")
        self.assertEqual(values["mid_ticks", BSP_HORIZON], -0.5)
        self.assertNotIn(("opposite_ticks", BSP_HORIZON), values)
        captures = {10: self.QUOTE, 30: self.QUOTE, 60: self.QUOTE}
        values = markouts(REG, self.walk(status=[[20_000, "SUSPENDED"]], in_play=[[40_000, True]]), BACK, 0,
                          self.QUOTE, captures, P(6.4))
        self.assertEqual([values["mid_points", h] for h in (10, 30, 60)], [0.0, "market_not_open", "market_not_open"])
        values = markouts(REG, self.walk(in_play=[[40_000, True]]), BACK, 0, self.QUOTE, captures, P(6.4))
        self.assertEqual([values["mid_points", h] for h in (10, 30, 60)], [0.0, 0.0, "in_play"])
        values = markouts(REG, self.walk(), BACK, 0, (None, (P(6.6), 75)), captures, P(6.4))
        self.assertEqual(set(values.values()), {"start_not_two_sided"})                  # a BACK's opposite: best back

    def test_a_quote_too_wide_to_be_a_market(self):
        # 6.0 / 10.0 is 20 ticks, the cap itself; 6.0 / 10.5 is 21, and the lay side emptied to a stray 1000 far more.
        self.assertFalse(wide(REG, ((P(6.0), 5), (P(10.0), 5))))
        self.assertTrue(wide(REG, ((P(6.0), 5), (P(10.5), 5))))
        self.assertTrue(wide(REG, ((P(1.01), 2), (P(6.6), 75))))
        self.assertFalse(wide(REG, ((P(6.0), 5), None)))                                 # one side: no spread
        gutted, stray = ((P(6.0), 50), (P(1000), 2)), ((P(1.01), 2), (P(6.6), 75))
        # At a horizon: every series, the BSP horizon still defined from the start.
        captures = {10: gutted, 30: stray, 60: self.QUOTE}
        for side in (LAY, BACK):
            values = markouts(REG, self.walk(), side, 0, self.QUOTE, captures, P(6.4))
            self.assertEqual({values[series, 10] for series in ("mid_ticks", "mid_points", "opposite_ticks",
                                                                 "opposite_points")}, {"spread_above_max"})
            self.assertEqual(values["mid_points", 30], "spread_above_max")
            self.assertEqual(values["mid_ticks", 60], 0.0)
            self.assertIsInstance(values["mid_ticks", BSP_HORIZON], float)
        # At the start: every horizon of every series, BSP included.
        values = markouts(REG, self.walk(), LAY, 0, gutted, {10: self.QUOTE, 30: self.QUOTE, 60: self.QUOTE}, P(6.4))
        self.assertEqual(set(values.values()), {"spread_above_max"})
        self.assertIn(("mid_ticks", BSP_HORIZON), values)
        # The order of the reasons: a horizon not OPEN is market_not_open first; one-sided is not_two_sided.
        values = markouts(REG, self.walk(status=[[5_000, "SUSPENDED"]]), LAY, 0, self.QUOTE, captures, P(6.4))
        self.assertEqual(values["mid_ticks", 10], "market_not_open")
        values = markouts(REG, self.walk(), LAY, 0, self.QUOTE, {10: (None, (P(1000), 2))}, P(6.4))
        self.assertEqual(values["mid_ticks", 10], "not_two_sided")
        # The edge at fill reads the same start: no edge from a start too wide.
        leg = Leg(-300, MAIN, LAY, R3, P(6.2), Fraction(0), 0, 290_000, {})
        leg.start = gutted
        self.assertEqual(edge(REG, leg), "spread_above_max")
        leg.start = (None, (P(6.6), 75))
        self.assertEqual(edge(REG, leg), "start_not_two_sided")
        leg.start = self.QUOTE
        self.assertEqual(edge(REG, leg)["ticks"], 0.5)


# ---------------------------------------------------------------------------------------------------------------

class SelectionAndCodeTests(LpCase):
    def test_the_standard_market_selects_the_shortest_eligible_runner(self):
        walk = self.walk(lp_market())
        for offset in OFFSETS:
            code, selection = walk.selections[offset]
            self.assertIsNone(code)
            self.assertEqual((selection.selection_id, selection.spread), (R3, 3))
        legs = {(leg.offset_s, leg.role, leg.side): leg for leg in walk.legs}
        self.assertEqual(len(legs), 12)
        for offset in OFFSETS:
            found = {(role, side): (legs[offset, role, side].price, legs[offset, role, side].queue)
                     for role in (MAIN, JOIN) for side in (LAY, BACK)}
            self.assertEqual(found, {(MAIN, LAY): (P(6.2), 0), (MAIN, BACK): (P(6.4), 0), (JOIN, LAY): (P(6.0), 50),
                                     (JOIN, BACK): (P(6.6), 75)})
            self.assertTrue(all(leg.fill_pt is None for leg in walk.legs))
        tally = self.one(lp_market())
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "eligible"))
        decision = tally.decisions[-300][0]
        self.assertEqual((decision.state, decision.net, decision.effect, decision.spread_bucket, decision.price_band),
                         ("no_fill", 0.0, "no_positive_outcome", "S1", "B3"))

    def test_ties_and_wider_spreads(self):
        # R4 at the same best back as R3 but a wider spread: the lower best lay (R3) wins; then the lower id.
        walk = self.walk(lp_market(books={3600: book(R4=(6.0, 7.0))}))
        self.assertEqual(walk.selections[-300][1].selection_id, R3)
        walk = self.walk(lp_market(books={3600: book(R4=(6.0, 6.6))}))
        self.assertEqual(walk.selections[-300][1].selection_id, R3)
        walk = self.walk(lp_market(books={3600: book(R3=(6.0, 6.2), R4=(8.0, 10.0))}))
        self.assertEqual(walk.selections[-300][1].spread, 10)                              # 8.0 to 10.0, 10 ticks
        # A placeholder lay far above the back is no market: R3 at 6.0 / 1000 is passed over for R4 (max 20 ticks).
        walk = self.walk(lp_market(books={3600: book(R3=(6.0, 1000.0))}))
        self.assertEqual((walk.selections[-300][1].selection_id, walk.selections[-300][1].spread), (R4, 5))
        walk = self.walk(lp_market(books={3600: book(R3=(6.0, 10.0))}))                 # 20 ticks: the cap itself
        self.assertEqual((walk.selections[-300][1].selection_id, walk.selections[-300][1].spread), (R3, 20))

    def test_the_selection_codes(self):
        narrow = book(R3=(6.0, 6.2), R4=(8.0, 8.2))
        outside = {sid: (back * 5, back * 5 + 5) for sid, (back, _) in BOOK.items()}   # every best back above 10
        one_sided = {sid: (back, None) for sid, (back, _) in BOOK.items()}
        for name, books, code in (("narrow", {3600: narrow}, "spread_below_min"),
                                  ("outside the band", {3600: outside}, "no_runner_in_band"),
                                  ("one-sided", {3600: one_sided}, "no_runner_in_band")):
            with self.subTest(case=name):
                self.assertEqual(outcome_of(self.one(lp_market(books=books))), dict.fromkeys(OFFSETS, code))
        placeholders = book(R3=(6.0, 1000.0), R4=(8.0, 1000.0))                    # wide, but no market
        self.assertEqual(outcome_of(self.one(lp_market(books={3600: placeholders}))),
                         dict.fromkeys(OFFSETS, "spread_above_max"))
        bad = mcm(before(700), [{"id": M, "rc": [{"id": R3, "trd": [[6.2]]}]}])
        self.assertEqual(outcome_of(self.one(lp_market(extra=[bad]))), dict.fromkeys(OFFSETS, "ladder_malformed"))
        self.assertEqual(outcome_of(self.one(lp_market(runners=SIDS[:4]))),
                         dict.fromkeys(OFFSETS, "too_few_active_runners"))
        self.assertEqual(outcome_of(self.one(lp_market(eventTypeId="4339"))),
                         dict.fromkeys(OFFSETS, "event_type_not_allowed"))

    def test_a_record_with_no_anchor_is_counted_not_fatal(self):
        # No market definition, so no first-published marketTime: no_view at every time, never a crash (Codex, #53).
        messages = [message for message in lp_market()
                    if not (isinstance(message, dict) and any("marketDefinition" in change for change in message["mc"]))]
        tally = self.one(messages)
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "no_view"))
        self.assertEqual((tally.replayed, tally.months["no_anchor"]["records"]), (0, 1))


class FillTests(LpCase):
    def test_volume_at_our_price_after_placement_fills(self):
        found = self.filled(lp_market(trades=[(200, R3, [[6.2, 4.0]])]))
        self.assertEqual(found, {(-600, MAIN, LAY): VOLUME, (-300, MAIN, LAY): VOLUME})   # -120 was placed after it
        self.assertEqual(self.filled(lp_market(trades=[(200, R3, [[6.2, 3.98]])])), {})  # GBP 1.99 of backer's stake
        self.assertEqual(self.filled(lp_market(trades=[(200, R3, [[6.4, 4.0]])])),
                         {(-600, MAIN, BACK): VOLUME, (-300, MAIN, BACK): VOLUME})
        legs = self.legs(lp_market(trades=[(200, R3, [[6.2, 2.0]]), (150, R3, [[6.2, 4.0]])]))
        self.assertEqual(legs[-300, MAIN, LAY].fill_pt, before(150))                        # the cumulative rise counts

    def test_any_trade_through_our_price_fills_whatever_its_size(self):
        self.assertEqual(self.filled(lp_market(trades=[(200, R3, [[6.0, 0.02]])])),
                         {(-600, MAIN, LAY): THROUGH, (-300, MAIN, LAY): THROUGH})
        self.assertEqual(self.filled(lp_market(trades=[(200, R3, [[6.6, 0.02]])])),
                         {(-600, MAIN, BACK): THROUGH, (-300, MAIN, BACK): THROUGH})
        # A message that meets both rules is a trade-through.
        self.assertEqual(self.filled(lp_market(trades=[(200, R3, [[6.2, 4.0], [6.0, 0.02]])]))[-300, MAIN, LAY],
                         THROUGH)
        # Trades beyond the LAY are on the BACK's good side, and the other way round: they fill nothing there.
        self.assertEqual(self.filled(lp_market(trades=[(200, R3, [[6.4, 3.98]])])), {})

    def test_nothing_published_at_or_before_placement_fills(self):
        self.assertEqual(self.filled(lp_market(trades=[(900, R3, [[6.2, 10.0]]), (200, R3, [[6.2, 13.98]])])), {})
        self.assertEqual(self.filled(lp_market(trades=[(900, R3, [[6.2, 10.0]]), (200, R3, [[6.2, 14.0]])])),
                         {(-600, MAIN, LAY): VOLUME, (-300, MAIN, LAY): VOLUME})
        # In the batch at t the trade is in the view, so in the legs' marks: the -600 legs never see it as a rise.
        self.assertEqual(self.filled(lp_market(trades=[(600, R3, [[6.2, 4.0], [6.0, 1.0]])])), {})
        self.assertEqual(self.filled(lp_market(trades=[(599.999, R3, [[6.2, 4.0]])])), {(-600, MAIN, LAY): VOLUME})
        # A restatement down and back up again (no level changing) is no trade.
        walk = self.walk(lp_market(trades=[(900, R3, [[6.2, 10.0]])],
                                   restatements=[(400, R3, [[6.2, 6.0]]), (200, R3, [[6.2, 10.0]])]))
        self.assertEqual([leg for leg in walk.legs if leg.fill_pt is not None], [])
        self.assertEqual(walk.trd_decreases, 1)

    def test_a_re_sent_or_zeroed_traded_ladder_never_fills(self):
        # The settlement artefact (an empty list) is ignored, and re-sending a volume already seen is no rise; so is an
        # image carrying the same cumulative volume.
        zeroed = [(900, R3, [[6.2, 4.0]]), (200, R3, []), (150, R3, [[6.2, 4.0]])]
        walk = self.walk(lp_market(trades=zeroed))
        self.assertEqual(([leg for leg in walk.legs if leg.fill_pt is not None], walk.trd_zeroing), ([], 1))
        image = mcm(before(200), [{"id": M, "img": True, "marketDefinition": definition(OFF, [runner(s) for s in SIDS]),
                                   "rc": [{"id": sid, "atb": [[back, 50.0]], "atl": [[lay, 75.0]],
                                           **({"trd": [[6.2, 4.0]]} if sid == R3 else {})}
                                          for sid, (back, lay) in BOOK.items()]}])
        self.assertEqual(self.filled(lp_market(trades=[(900, R3, [[6.2, 4.0]])], extra=[image])), {})

    def test_nothing_after_the_cancel_fills(self):
        self.assertEqual(self.filled(lp_market(trades=[(9.999, R3, [[6.2, 4.0]])])), {})          # C + 1 ms
        self.assertEqual(self.filled(lp_market(trades=[(10, R3, [[6.2, 4.0]])])),
                         {(offset, MAIN, LAY): VOLUME for offset in OFFSETS})                       # at C

    def test_a_level_we_did_not_create_fills_only_behind_its_queue(self):
        # The join LAY at 6.0 has GBP 50 shown ahead of it: it needs 2 x (50 + 2) on the files' basis.
        found = self.filled(lp_market(trades=[(200, R3, [[6.0, 103.98]])]))
        self.assertEqual(found, {(-600, MAIN, LAY): THROUGH, (-300, MAIN, LAY): THROUGH})
        found = self.filled(lp_market(trades=[(200, R3, [[6.0, 104.0]])]))
        self.assertEqual(found[-300, JOIN, LAY], VOLUME)
        found = self.filled(lp_market(trades=[(200, R3, [[6.6, 154.0]]), (200, R3, [[5.8, 0.02]])]))
        self.assertEqual((found[-300, JOIN, BACK], found[-300, JOIN, LAY], found[-300, MAIN, LAY]),
                         (VOLUME, THROUGH, THROUGH))
        # Volume on another runner fills nothing.
        self.assertEqual(self.filled(lp_market(trades=[(200, R4, [[6.2, 40.0], [6.0, 40.0]])])), {})



def ladder_move(seconds, atb=None, atl=None, trd=None, sid=R3):
    """One rc change of R3 (or sid) at `seconds` before the off: atb, atl and trd deltas as given."""
    item = {"id": sid, **{key: value for key, value in (("atb", atb), ("atl", atl), ("trd", trd)) if value}}
    return mcm(before(seconds), [{"id": M, "rc": [item]}])


class CurrencyRestatementTests(LpCase):
    """SA/360006560978: trd updates sent for currency-rate changes, a nominal rise or a decrease, at prices nobody is
    trading; a rise counts as a trade only with a ladder entry at its price in its batch."""

    def test_a_nominal_rise_at_a_stale_price_fills_nothing(self):
        messages = lp_market(trades=[(1800, R3, [[4.0, 500.0]])], restatements=[(200, R3, [[4.0, 500.01]])])
        self.assertEqual(self.filled(messages), {})
        walk = self.walk(messages)
        self.assertEqual((walk.trd[-300]["rises_not_counted_not_displayed"], walk.trd[-300]["rises_counted"]), (1, 0))
        # The same rise with the level changing in its batch is a trade through both LAYs.
        found = self.filled(lp_market(trades=[(1800, R3, [[4.0, 500.0]]), (200, R3, [[4.0, 500.01]])]))
        self.assertEqual(found, {(-600, MAIN, LAY): THROUGH, (-300, MAIN, LAY): THROUGH, (-600, JOIN, LAY): THROUGH,
                                 (-300, JOIN, LAY): THROUGH})

    def test_a_restatement_at_two_stale_prices_makes_no_round_trip(self):
        tally = self.one(lp_market(trades=[(1800, R3, [[4.0, 500.0], [9.0, 300.0]])],
                                   restatements=[(200, R3, [[4.0, 500.01], [9.0, 300.01]])]))
        self.assertEqual({tally.decisions[offset][0].state for offset in OFFSETS}, {"no_fill"})

    def test_a_restatement_at_the_touch_fills_nothing(self):
        messages = lp_market(trades=[(1800, R3, [[6.0, 100.0], [6.6, 50.0]])],
                             restatements=[(200, R3, [[6.0, 100.01], [6.6, 50.01]])])
        self.assertEqual(self.filled(messages), {})
        self.assertEqual(self.walk(messages).trd[-300]["rises_not_counted_at_touch"], 2)

    def test_a_small_real_trade_through_after_a_decrease_still_fills(self):
        messages = lp_market(trades=[(1800, R3, [[6.0, 100.0]]), (200, R3, [[6.0, 99.98]])],
                             restatements=[(400, R3, [[6.0, 99.9]])])
        self.assertEqual(self.filled(messages), {(-600, MAIN, LAY): THROUGH, (-300, MAIN, LAY): THROUGH})
        walk = self.walk(messages)
        self.assertEqual((walk.trd[-600]["values_below_last_published"], walk.trd[-300]["values_below_last_published"]),
                         (1, 0))

    def test_the_hourly_diagnostics(self):
        # Off 14:05 on a Monday: the -600 and -300 windows hold 14:00:00 to 14:01:00, -120's does not. A restatement at
        # 14:00:10 raises three scattered prices; a real trade at 14:00:20 fills both LAYs, at 14:03:20 nothing.
        off = OFF + timedelta(minutes=5)
        messages = lp_market(off=off, trades=[(280, R3, [[6.2, 4.0]]), (100, R3, [[6.2, 8.0]])],
                             restatements=[(290, R3, [[4.0, 1.0], [9.0, 2.0], [12.0, 3.0]])])
        walk = self.walk(messages)
        # The trades delete the empty level at 6.2 in their batch (touched_levels): the level is left as shown.
        self.assertEqual({name: walk.trd[-600][name] for name in TRD_COUNTS if walk.trd[-600][name]},
                         {"rises_counted": 2, "rises_counted_level_unchanged": 2, "rises_counted_after_the_hour": 1,
                          "rises_not_counted_not_displayed": 3, "rises_not_counted_after_the_hour": 3,
                          "rc_raising_3_non_adjacent_prices": 1})
        self.assertEqual(walk.trd[-120]["rises_counted"], 1)
        root, manifest, records = self.build([(path(M, off), messages)])
        tally = Tally(REG)
        decide_record(REG, records[M], root, manifest, tally)
        fills = {offset: _fills(REG, tally.decisions[offset], MAIN) for offset in OFFSETS}
        self.assertEqual(fills[-600]["after_the_hour"][VOLUME], {"fills": 1, "expected_if_uniform": round(60 / 590, 6)})
        self.assertEqual(fills[-300]["after_the_hour"][VOLUME]["fills"], 1)
        self.assertEqual(fills[-120]["after_the_hour"][VOLUME], {"fills": 0, "expected_if_uniform": 0.0})
        self.assertEqual(fills[-120]["legs"][LAY]["by_kind"][VOLUME], 1)
        self.assertEqual(fills[-300]["within_s_of_t"], {"1": dict.fromkeys(FILL_KINDS, 0),
                                                        "2": dict.fromkeys(FILL_KINDS, 0)})
        self.assertEqual(tally.decisions[-600][0].trd, walk.trd[-600])
        # Beside the after-the-hour rises, the counts expected if each rise's time were uniform over its window.
        self.assertEqual(_trd_expected(tally.decisions[-600]), {"rises_counted": round(2 * 60 / 590, 6),
                                                                "rises_not_counted": round(3 * 60 / 590, 6)})
        self.assertEqual(_trd_expected(tally.decisions[-120]), {"rises_counted": 0.0, "rises_not_counted": 0.0})

    def test_counted_rises_by_what_their_batch_did_to_the_level(self):
        # R3's book is 6.0 (50 shown) / 6.6 (75 shown). A trade at the best back that takes 10 of the 50 lowers the
        # level; one that deletes it lowers it too; a rise beside an entry that adds size there only raises it.
        cases = {"rises_counted_level_lowered": [ladder_move(200, atb=[[6.0, 40.0]], trd=[[6.0, 20.0]]),
                                                 ladder_move(150, atb=[[6.0, 0]], trd=[[6.0, 60.0]])],
                 "rises_counted_level_raised": [ladder_move(200, atb=[[6.0, 60.0]], trd=[[6.0, 20.0]]),
                                                ladder_move(150, atl=[[6.2, 5.0]], trd=[[6.2, 2.0]])],
                 "rises_counted_level_unchanged": [ladder_move(200, atb=[[6.0, 50.0]], trd=[[6.0, 20.0]]),
                                                   ladder_move(150, atb=[[6.4, 0]], trd=[[6.4, 2.0]])]}
        for name, extra in cases.items():
            with self.subTest(name=name):
                trd = self.walk(lp_market(extra=extra)).trd[-300]
                self.assertEqual((trd["rises_counted"], trd[name]), (2, 2))
        # Two messages in one batch: an entry that adds size, then one that takes it: lowered (each entry against the
        # level just before its message).
        both = [ladder_move(200, atb=[[6.0, 70.0]]), ladder_move(200, atb=[[6.0, 55.0]], trd=[[6.0, 20.0]])]
        self.assertEqual(self.walk(lp_market(extra=both)).trd[-300]["rises_counted_level_lowered"], 1)

    def test_rises_not_counted_at_the_touch_outside_the_hour(self):
        # Off 14:05 on a Monday: restatements at the touch at 14:00:10 (after the hour) and 14:02:00 (outside it).
        off = OFF + timedelta(minutes=5)
        messages = lp_market(off=off, trades=[(1800, R3, [[6.0, 100.0], [6.6, 50.0]])],
                             restatements=[(290, R3, [[6.0, 100.01]]), (180, R3, [[6.6, 50.01]])])
        trd = self.walk(messages).trd[-300]
        self.assertEqual((trd["rises_not_counted_at_touch"], trd["rises_not_counted_after_the_hour"],
                          trd["rises_not_counted_at_touch_outside_the_hour"]), (2, 1, 1))


class QuoteCrossTests(LpCase):
    def test_a_quote_at_our_price_fills_at_the_touch(self):
        # At -200 s a backer offers GBP 75 at 6.2, our LAY's price, and would have matched us there; a trade at 6.0
        # follows at -150 s. The fill is at the touch, marked from the quote before it (mid 6.3): the mid then falls to
        # 6.1, one tick against the LAY.
        tally = self.one(lp_market(books={3600: BOOK, 200: book(R3=(6.0, 6.2))}, trades=[(150, R3, [[6.0, 1.0]])]))
        lay = tally.decisions[-300][0].legs[MAIN, LAY]
        self.assertEqual((lay.kind, lay.seconds, lay.touched_before_fill), (CROSS, 100.0, False))
        self.assertEqual((lay.edge["ticks"], lay.marks["mid_ticks", 10]), (0.5, 1.0))
        self.assertFalse(tally.decisions[-300][0].legs[MAIN, BACK].filled)

    def test_a_quote_below_our_stake_is_a_touch_not_a_fill(self):
        small = ladder_move(200, atl=[[6.6, 0], [6.2, 1.5]])
        legs = self.legs(lp_market(extra=[small]))
        self.assertEqual((legs[-300, MAIN, LAY].fill_pt, legs[-300, MAIN, LAY].touched_pt), (None, before(200)))
        self.assertIsNone(legs[-300, MAIN, BACK].touched_pt)
        row = self.one(lp_market(extra=[small])).decisions[-300][0].legs[MAIN, LAY]
        self.assertEqual((row.filled, row.touched_never_filled), (False, True))
        # A trade-through later fills it, marked from the quote before the trade (mid 6.1): touched_before_fill.
        row = self.one(lp_market(extra=[small], trades=[(150, R3, [[6.0, 1.0]])])).decisions[-300][0].legs[MAIN, LAY]
        self.assertEqual((row.kind, row.touched_before_fill, row.touched_never_filled), (THROUGH, True, False))
        self.assertEqual(row.edge["ticks"], -0.5)

    def test_sizes_at_and_through_our_price_add_up(self):
        def moved(size):
            return ladder_move(200, atb=[[6.0, 0], [5.8, 50.0]], atl=[[6.6, 0], [6.2, 1.0], [6.0, size]])

        legs = self.legs(lp_market(extra=[moved(1.0)]))
        self.assertEqual((legs[-300, MAIN, LAY].fill_kind, legs[-300, MAIN, LAY].fill_pt), (CROSS, before(200)))
        # The join LAY at 6.0 sees 1.0 there, short of its queue (50) and stake: touched, not filled.
        self.assertEqual((legs[-300, JOIN, LAY].fill_pt, legs[-300, JOIN, LAY].touched_pt), (None, before(200)))
        self.assertIsNone(self.legs(lp_market(extra=[moved(0.99)]))[-300, MAIN, LAY].fill_pt)

    def test_a_level_we_joined_needs_its_queue_too(self):
        def moved(size):
            return ladder_move(200, atb=[[6.0, 0], [5.8, 50.0]], atl=[[6.6, 0], [6.0, size]])

        legs = self.legs(lp_market(extra=[moved(52.0)]))
        self.assertEqual((legs[-300, JOIN, LAY].fill_kind, legs[-300, MAIN, LAY].fill_kind), (CROSS, CROSS))
        legs = self.legs(lp_market(extra=[moved(51.99)]))
        self.assertEqual((legs[-300, JOIN, LAY].fill_pt, legs[-300, MAIN, LAY].fill_kind), (None, CROSS))

    def test_precedence_within_one_batch(self):
        # A trade through the LAY and a quote crossing it in one batch: a trade-through.
        both = ladder_move(200, atb=[[6.0, 49.0]], atl=[[6.6, 0], [6.2, 5.0]], trd=[[6.0, 2.0]])
        self.assertEqual(self.legs(lp_market(extra=[both]))[-300, MAIN, LAY].fill_kind, THROUGH)
        # A quote crossing it and volume at its price in one batch: a quote cross.
        both = ladder_move(200, atl=[[6.6, 0], [6.2, 5.0]], trd=[[6.2, 4.0]])
        self.assertEqual(self.legs(lp_market(extra=[both]))[-300, MAIN, LAY].fill_kind, CROSS)
        # Two messages with one pt are one batch: the ladder entry of the second lets the first's rise count.
        rise, entry = ladder_move(200, trd=[[6.2, 4.0]]), ladder_move(200, atb=[[6.2, 0]])
        self.assertEqual(self.legs(lp_market(extra=[rise, entry]))[-300, MAIN, LAY].fill_kind, VOLUME)
        self.assertIsNone(self.legs(lp_market(extra=[rise]))[-300, MAIN, LAY].fill_pt)


class InventoryAndValueTests(LpCase):
    def test_round_trips_and_inventory(self):
        cases = {
            "round trip": ([(200, R3, [[6.2, 4.0]]), (150, R3, [[6.4, 4.0]])], "round_trip",
                           Fraction(39, 100) / P(6.4)),
            "lay only": ([(200, R3, [[6.2, 4.0]])], "lay_only", Fraction(2875, 100000)),
            "back only": ([(200, R3, [[6.4, 4.0]])], "back_only", Fraction(-34375, 1000000)),
        }
        for name, (trades, state, net) in cases.items():
            with self.subTest(case=name):
                tally = self.one(lp_market(trades=trades))
                for offset in (-600, -300):
                    decision = tally.decisions[offset][0]
                    self.assertEqual((decision.state, decision.net), (state, float(net)))
                self.assertEqual(tally.decisions[-120][0].state, "no_fill")
        tally = self.one(lp_market(trades=[(200, R3, [[6.4, 4.0]])]))
        self.assertEqual(tally.decisions[-300][0].net_unrounded, float(2 * nclv(P(6.4), P(6.4), REG.commission)))
        self.assertEqual(tally.decisions[-300][0].effect, "rounded_up")

    def test_the_net_with_p_normalised_by_the_book(self):
        book_sum = sum(1 / P(bsp) for bsp in BSPS.values())
        root, manifest, records = self.build([(path(M), lp_market())])
        self.assertEqual(bsp_book(records[M]), book_sum)
        self.assertEqual(valid_bsp(records[M], R3), P(6.4))
        # A runner without a BSP is left out of the book.
        root, manifest, records = self.build([(path("1.180000002"), lp_market("1.180000002",
                                                                               bsps={**BSPS, R6: None}))])
        self.assertEqual(bsp_book(records["1.180000002"]), book_sum - 1 / P(17.0))
        # A lone BACK at 6.4, BSP 6.4: p = (1 / 6.4) / book; the commission is still the rounded 22p.
        p = 1 / P(6.4) / book_sum
        self.assertEqual(valued_normalised(REG, None, P(6.4), P(6.4), book_sum),
                         p * (Fraction(1080, 100) - Fraction(22, 100)) - (1 - p) * 2)
        self.assertEqual(valued_normalised(REG, P(6.2), P(6.4), P(6.4), 1), valued(REG, P(6.2), P(6.4), P(6.4))[0])
        tally = self.one(lp_market(trades=[(200, R3, [[6.4, 4.0]])]))
        self.assertEqual(tally.decisions[-300][0].net_normalised,
                         float(valued_normalised(REG, None, P(6.4), P(6.4), book_sum)))


class MarkoutTests(LpCase):
    PATH = {3600: BOOK, 195: book(R3=(6.2, 6.8)), 175: book(R3=(6.4, 7.0)), 100: book(R3=(5.8, 6.2))}

    def test_mark_outs_from_the_stream(self):
        tally = self.one(lp_market(books=self.PATH, trades=[(200, R3, [[6.2, 4.0], [6.4, 4.0]])]))
        decision = tally.decisions[-300][0]
        self.assertEqual(decision.state, "round_trip")
        lay, back = decision.legs[MAIN, LAY], decision.legs[MAIN, BACK]
        # The start mid is 6.3 (6.0 / 6.6); +10 s reads 6.5 (6.2 / 6.8), +30 and +60 s read 6.7 (6.4 / 7.0).
        self.assertEqual([lay.marks["mid_ticks", h] for h in (10, 30, 60, BSP_HORIZON)], [-1.0, -2.0, -2.0, -0.5])
        self.assertEqual([back.marks["mid_ticks", h] for h in (10, 30, 60, BSP_HORIZON)], [1.0, 2.0, 2.0, 0.5])
        self.assertAlmostEqual(lay.marks["mid_points", 10], -(100 / 6.3 - 100 / 6.5))
        self.assertAlmostEqual(back.marks["mid_points", BSP_HORIZON], 100 / 6.3 - 100 / 6.4)
        # The opposite quote: the best lay (6.6, 6.8, 7.0) for the LAY, the best back (6.0, 6.2, 6.4) for the BACK.
        self.assertEqual([lay.marks["opposite_ticks", h] for h in (10, 30)], [-1.0, -2.0])
        self.assertEqual([back.marks["opposite_ticks", h] for h in (10, 30)], [1.0, 2.0])
        self.assertEqual(lay.edge["ticks"], 0.5)
        self.assertAlmostEqual(lay.edge["points"], 100 / 6.2 - 100 / 6.3)
        self.assertEqual(back.edge["ticks"], 0.5)
        self.assertEqual(tick_position(P(6.3)), (tick_position(P(6.2)) + tick_position(P(6.4))) / 2)
        self.assertEqual(lay.seconds, 100.0)

    def test_the_start_is_the_quote_before_the_filling_batch(self):
        # One message both fills the LAY (4 traded at 6.2) and moves R3's book (someone joins at 6.2, behind us): the
        # start mid is still 6.3, from the quote before that batch.
        joined = mcm(before(200), [{"id": M, "rc": [{"id": R3, "atb": [[6.2, 50.0]], "trd": [[6.2, 4.0]]}]}])
        tally = self.one(lp_market(extra=[joined]))
        lay = tally.decisions[-300][0].legs[MAIN, LAY]
        self.assertEqual((lay.kind, lay.edge["ticks"], lay.marks["mid_ticks", 10]), (VOLUME, 0.5, -0.5))

    def test_the_join_legs_are_marked_out_too(self):
        tally = self.one(lp_market(books={3600: BOOK, 190: book(R3=(5.8, 6.4))}, trades=[(200, R3, [[6.0, 104.0]])]))
        join = tally.decisions[-300][0].legs[JOIN, LAY]
        self.assertEqual((join.filled, join.kind), (True, VOLUME))
        # The mid falls from 6.3 to 6.1 ((5.8 + 6.4) / 2, half way from 6.0 to 6.2): a shortening of one tick, which
        # hurts a LAY; joined at 6.0, the LAY's edge was 1.5 ticks.
        self.assertEqual(join.marks["mid_ticks", 10], 1.0)
        self.assertEqual(join.edge["ticks"], 1.5)
        self.assertIsNone(join.null)

    def test_horizons_after_the_off(self):
        suspended, in_play = status_at(0, status="SUSPENDED"), status_at(-1, in_play=True)
        tally = self.one(lp_market(trades=[(20, R3, [[6.2, 4.0]])], extra=[suspended, in_play]))
        lay = tally.decisions[-120][0].legs[MAIN, LAY]
        self.assertEqual([lay.marks["mid_ticks", h] for h in (10, 30, 60, BSP_HORIZON)],
                         [0.0, "in_play", "in_play", -0.5])
        tally = self.one(lp_market(trades=[(20, R3, [[6.2, 4.0]])], extra=[suspended]))
        lay = tally.decisions[-120][0].legs[MAIN, LAY]
        self.assertEqual([lay.marks["mid_ticks", h] for h in (10, 30, 60)], [0.0, "market_not_open", "market_not_open"])
        emptied = mcm(before(185), [{"id": M, "rc": [{"id": R3, "atl": []}]}])
        tally = self.one(lp_market(trades=[(200, R3, [[6.2, 4.0]])], extra=[emptied]))
        lay = tally.decisions[-300][0].legs[MAIN, LAY]
        self.assertEqual([lay.marks["mid_ticks", h] for h in (10, 30)], [0.0, "not_two_sided"])
        self.assertEqual(lay.marks["opposite_ticks", 30], "not_two_sided")

    def test_a_quote_too_wide_after_t_is_no_market(self):
        # At the start: R3's lay side empties to a stray 1000 at -250 s, after t (-300 s, 6.0 / 6.6), and a trade at 6.0
        # goes through the LAY at -200 s. Its start, the quote before that batch, is 6.0 / 1000: no mark-out, no edge.
        tally = self.one(lp_market(books={3600: BOOK, 250: book(R3=(6.0, 1000.0))}, trades=[(200, R3, [[6.0, 1.0]])]))
        lay = tally.decisions[-300][0].legs[MAIN, LAY]
        self.assertEqual((lay.kind, lay.edge), (THROUGH, "spread_above_max"))
        self.assertEqual(set(lay.marks.values()), {"spread_above_max"})
        # At a horizon: the lay side empties to 1000 at -195 s and recovers at -185 s; a LAY filled at -200 s from a
        # normal quote has no +10 s value, and its +30 and +60 s values are back.
        tally = self.one(lp_market(books={3600: BOOK, 195: book(R3=(6.0, 1000.0)), 185: BOOK},
                                   trades=[(200, R3, [[6.2, 4.0]])]))
        lay = tally.decisions[-300][0].legs[MAIN, LAY]
        self.assertEqual([lay.marks["mid_ticks", h] for h in (10, 30, 60)], ["spread_above_max", 0.0, 0.0])
        self.assertEqual(lay.marks["opposite_points", 10], "spread_above_max")
        self.assertEqual(lay.edge["ticks"], 0.5)
        tables = _markouts(REG, tally.decisions[-300], False)
        self.assertEqual(tables["main"]["all"]["mid_ticks"]["10"]["undefined"]["spread_above_max"], 1)

    def test_legs_touched_before_their_fill_are_their_own_group(self):
        small = ladder_move(200, atl=[[6.6, 0], [6.2, 1.5]])
        tally = self.one(lp_market(extra=[small], trades=[(150, R3, [[6.0, 1.0]])]))
        tables = _markouts(REG, tally.decisions[-300], False)["main"]
        self.assertEqual((tables["touched_before_fill"]["mid_ticks"]["10"]["values"],
                          tables["all"]["mid_ticks"]["10"]["values"]), (1, 1))
        tally = self.one(lp_market(trades=[(150, R3, [[6.0, 1.0]])]))                     # no touch first
        tables = _markouts(REG, tally.decisions[-300], False)["main"]
        self.assertEqual((tables["touched_before_fill"]["mid_ticks"]["10"]["values"],
                          tables["all"]["mid_ticks"]["10"]["values"]), (0, 1))

    def test_the_realised_spread_is_the_edge_less_the_move(self):
        tally = self.one(lp_market(books=self.PATH, trades=[(200, R3, [[6.2, 4.0], [6.4, 4.0]])]))
        legs = _filled_legs(tally.decisions[-300], MAIN)
        lay, back = (row for _, row in legs)
        # The LAY at 6.2 from mid 6.3; at +60 s the mid is 6.7: the LAY gained, 100 / 6.2 - 100 / 6.7 points in all.
        found = [value for _, value in _realised(REG, legs)]
        self.assertAlmostEqual(found[0], lay.edge["points"] - lay.marks["mid_points", 60])
        self.assertAlmostEqual(found[0], 100 / 6.2 - 100 / 6.7)
        self.assertAlmostEqual(found[1], 100 / 6.7 - 100 / 6.4)                            # the BACK lost

    def test_the_null_control(self):
        t, cancel = before(300), C
        times = {null_time(REG, M, -300, side, t, cancel) for side in (LAY, BACK)}
        self.assertEqual(len(times), 2)
        self.assertTrue(all(t < u <= cancel for u in times))
        self.assertEqual(null_time(REG, M, -300, LAY, t, cancel), null_time(REG, M, -300, LAY, t, cancel))
        for k in range(200):
            u = null_time(REG, f"1.18{k:07d}", -120, BACK, before(120), cancel)
            self.assertTrue(before(120) < u <= cancel)
        walk = self.walk(lp_market(trades=[(200, R3, [[6.2, 4.0]])]))
        lay = [leg for leg in walk.legs if (leg.offset_s, leg.role, leg.side) == (-300, MAIN, LAY)][0]
        self.assertEqual(lay.null_time, null_time(REG, M, -300, LAY, t, cancel))
        tally = self.one(lp_market(trades=[(200, R3, [[6.2, 4.0]])]))
        row = tally.decisions[-300][0].legs[MAIN, LAY]
        self.assertEqual({row.null[key] for key in row.null if key[1] != BSP_HORIZON}, {0.0})  # nothing moved
        self.assertEqual(row.null["mid_ticks", BSP_HORIZON], -0.5)
        self.assertNotIn(("opposite_ticks", 10), row.null)
        self.assertIsNone(tally.decisions[-300][0].legs[JOIN, LAY].null)


class NullStartTests(unittest.TestCase):
    QUOTE = ((P(6.0), 50), (P(6.6), 75))

    def walk(self, status=(), in_play=(), unreadable_from=None):
        market = type("Fold", (), {"fields": {"status": [[0, "OPEN"], *status], "inPlay": [[0, False], *in_play]}})
        return type("Walk", (), {"market": market, "unreadable_from": unreadable_from})

    def leg(self, start):
        leg = Leg(-300, MAIN, LAY, R3, P(6.2), Fraction(0), 0, 290_000, {})
        leg.null_time, leg.null = 50_000, {0: start, 10: self.QUOTE, 30: self.QUOTE, 60: self.QUOTE}
        return leg

    def test_a_null_start_gives_its_own_reason(self):
        cases = (({"status": [[40_000, "SUSPENDED"]]}, self.QUOTE, "market_not_open"),
                 ({"in_play": [[45_000, True]]}, self.QUOTE, "in_play"),
                 ({"unreadable_from": 50_000}, self.QUOTE, "member_unreadable"),
                 ({}, None, "member_unreadable"),
                 ({}, (None, (P(6.6), 75)), "start_not_two_sided"))
        for walk, start, reason in cases:
            with self.subTest(reason=reason, walk=walk):
                values = null_markouts(REG, self.walk(**walk), self.leg(start), P(6.4))
                self.assertEqual(set(values.values()), {reason})
                self.assertEqual({series for series, _ in values}, {"mid_ticks", "mid_points"})
        values = null_markouts(REG, self.walk(), self.leg(self.QUOTE), P(6.4))
        self.assertEqual(values["mid_ticks", 10], 0.0)

    def test_a_null_quote_too_wide_to_be_a_market(self):
        gutted = ((P(6.0), 50), (P(1000), 2))
        values = null_markouts(REG, self.walk(), self.leg(gutted), P(6.4))                # at u
        self.assertEqual(set(values.values()), {"spread_above_max"})
        self.assertIn(("mid_points", BSP_HORIZON), values)
        # A start at u not OPEN gives that reason first.
        values = null_markouts(REG, self.walk(status=[[40_000, "SUSPENDED"]]), self.leg(gutted), P(6.4))
        self.assertEqual(set(values.values()), {"market_not_open"})
        leg = self.leg(self.QUOTE)                                                         # at u + 30 s
        leg.null[30] = gutted
        values = null_markouts(REG, self.walk(), leg, P(6.4))
        self.assertEqual([values["mid_points", h] for h in (10, 30, 60)], [0.0, "spread_above_max", 0.0])
        self.assertIsInstance(values["mid_points", BSP_HORIZON], float)


class ExclusionTests(LpCase):
    def test_the_window_classes(self):
        def removal(seconds, sid=R6):
            return md(M, before(seconds), definition(OFF, [runner(s, "REMOVED" if s == sid else "ACTIVE")
                                                           for s in SIDS]))

        removed_r6 = {s: "REMOVED" if s == R6 else "WINNER" if s == FAV else "LOSER" for s in SIDS}
        removed_r3 = {s: "REMOVED" if s == R3 else "WINNER" if s == FAV else "LOSER" for s in SIDS}
        revised = md(M, before(200), definition(OFF + timedelta(minutes=5), [runner(s) for s in SIDS]))
        malformed = mcm(before(200), [{"id": M, "rc": [{"id": R3, "trd": [[6.2]]}]}])
        cases = {
            "suspension": ({"extra": [status_at(250, "SUSPENDED"), status_at(240)]},
                           {-600: "suspension_in_window", -300: "suspension_in_window", -120: "eligible"}),
            "in play": ({"extra": [status_at(50, in_play=True)]}, dict.fromkeys(OFFSETS, "in_play_in_window")),
            "revised": ({"extra": [revised]}, {-600: "schedule_revised_in_window", -300: "schedule_revised_in_window",
                                               -120: "schedule_revised_before_decision"}),
            "malformed": ({"extra": [malformed]}, {-600: "stream_malformed_in_window",
                                                   -300: "stream_malformed_in_window", -120: "ladder_malformed"}),
            "another runner removed": ({"extra": [removal(200)], "statuses": removed_r6},
                                       {-600: "post_decision_removal", -300: "post_decision_removal",
                                        -120: "eligible"}),
            "our runner removed": ({"extra": [removal(200, R3)], "statuses": removed_r3},
                                   {-600: "void_own_runner_removed", -300: "void_own_runner_removed",
                                    -120: "eligible"}),                              # R4 at -120
            "no BSP": ({"bspMarket": False, "bsps": {}, "reconciled": False}, dict.fromkeys(OFFSETS, "missing_bsp")),
            "unreadable after the off": ({"bad_tail": True}, dict.fromkeys(OFFSETS, "outcome_unknown")),
        }
        for name, (changes, expected) in cases.items():
            with self.subTest(case=name):
                tally = self.one(lp_market(**changes))
                self.assertEqual(outcome_of(tally), expected)
        cut = [message for message in lp_market(trades=[(250, R4, [[8.0, 2.0]])])
               if isinstance(message, bytes) or message["pt"] <= before(200)]          # readable to off -250 s
        self.assertEqual(outcome_of(self.one(cut + [b"not json\n"])),
                         {-600: "outcome_unknown", -300: "outcome_unknown", -120: "member_unreadable_before_decision"})
        # The excluded decisions' fills are counted by class and kind: the exclusions remove them from every statistic.
        suspended = [status_at(250, "SUSPENDED"), status_at(240)]
        tally = self.one(lp_market(extra=suspended, trades=[(200, R3, [[6.0, 1.0]])]))
        self.assertEqual(tally.excluded[-300]["suspension_in_window"],
                         {MAIN: {THROUGH: 1, CROSS: 0, VOLUME: 0}, JOIN: dict.fromkeys(FILL_KINDS, 0)})
        self.assertEqual(sum(n for roles in tally.excluded[-120].values() for kinds in roles.values()
                             for n in kinds.values()), 0)
        # A suspension after C is outside the window.
        self.assertEqual(outcome_of(self.one(lp_market(extra=[status_at(5, "SUSPENDED")]))),
                         dict.fromkeys(OFFSETS, "eligible"))


# ---------------------------------------------------------------------------------------------------------------

def tested(mean, p=0.001, ci_low=None, reason=None, bets=300):
    return {"bets": bets, "race_dates": 40, "mean": mean, "se": 0.01, "df": 39, "t_critical": 2.0,
            "ci_low": mean - 0.02 if ci_low is None else ci_low, "ci_high": mean + 0.02, "reason": reason, "t": 3.0,
            "p_two_sided": p}


UNDERPOWERED = dict(tested(0.01, p=None), reason="underpowered", bets=5, mean=None, se=None, df=None, t_critical=None,
                    ci_low=None, ci_high=None, t=None)


class DecideTests(unittest.TestCase):
    """lp.decide: Holm per family, the realised spread's and the controls' checks and the flags, on hand-built test
    results."""

    def run_decide(self, primary=None, secondary=None, realised=None, join=None, null=None, null_main=None):
        every = dict.fromkeys(OFFSETS)
        primary = {**{o: tested(0.03, ci_low=0.01) for o in OFFSETS}, **(primary or {})}
        secondary = {**{o: tested(0.01, p=0.4) for o in OFFSETS}, **(secondary or {})}
        realised = {**{o: tested(0.02, p=0.2) for o in OFFSETS}, **(realised or {})}
        join = {**{o: tested(0.5, p=0.001) for o in OFFSETS}, **(join or {})}
        null = {**{o: tested(0.001, p=0.7) for o in every}, **(null or {})}
        null_main = {**{o: tested(0.01, p=0.4) for o in every}, **(null_main or {})}
        return decide(REG, primary, secondary, realised, join, null, null_main)

    def test_a_flag_when_every_check_holds(self):
        tests, controls, flags = self.run_decide()
        self.assertEqual(flags[-300], {"primary_rejected_under_holm": True, "primary_positive": True,
                                       "primary_ci_low_above_0": True,
                                       "realised_spread_not_significantly_negative": True,
                                       "join_detects_adverse_selection": True, "null_does_not_block": True,
                                       "discovery_flag": True})
        self.assertEqual(tests["primary"][-300]["holm_p_adjusted"], 0.003)
        self.assertFalse(tests["secondary"][-300]["adverse_selection_detected"])
        self.assertTrue(tests["realised_spread"][-300]["not_significantly_negative"])
        self.assertNotIn("holm_p_adjusted", tests["realised_spread"][-300])
        self.assertTrue(controls["join"][-300]["detects_adverse_selection"])

    def test_each_check_failing_alone_clears_the_flag(self):
        cases = {
            "primary not rejected": ({"primary": {-300: tested(0.03, p=0.06, ci_low=0.001)}},
                                     ["primary_rejected_under_holm"]),
            "primary negative": ({"primary": {-300: tested(-0.03, ci_low=-0.05)}},
                                 ["primary_positive", "primary_ci_low_above_0"]),
            "realised spread negative": ({"realised": {-300: tested(-0.2, p=0.03)}},
                                         ["realised_spread_not_significantly_negative"]),
            "realised spread untestable": ({"realised": {-300: UNDERPOWERED}},
                                           ["realised_spread_not_significantly_negative"]),
            "realised spread negative, its noise unknown": ({"realised": {-300: dict(tested(-0.2, p=None), se=0.0)}},
                                                            ["realised_spread_not_significantly_negative"]),
            "join blind": ({"join": {-300: tested(0.1, p=0.2)}}, ["join_detects_adverse_selection"]),
            "join the wrong way": ({"join": {-300: tested(-0.4, p=0.001)}}, ["join_detects_adverse_selection"]),
            "join untestable": ({"join": {-300: UNDERPOWERED}}, ["join_detects_adverse_selection"]),
            "null drifts": ({"null": {-300: tested(0.006, p=0.01)}}, ["null_does_not_block"]),
            "null untestable": ({"null": {-300: UNDERPOWERED}}, ["null_does_not_block"]),
            "main over the null's legs untestable": ({"null_main": {-300: UNDERPOWERED}}, ["null_does_not_block"]),
        }
        for name, (changes, failing) in cases.items():
            with self.subTest(case=name):
                _, _, flags = self.run_decide(**changes)
                self.assertEqual([key for key, value in flags[-300].items() if not value], failing + ["discovery_flag"])
                self.assertTrue(flags[-600]["discovery_flag"])

    def test_the_flag_asks_whether_the_spread_pays_not_whether_we_are_picked_off(self):
        # Adverse selection detected at every time, the spread still paying: a flag, the fourth reading (picked off
        # but paid for it). The secondary is reported under Holm and decides no flag.
        tests, _, flags = self.run_decide(secondary={o: tested(0.2, p=0.001) for o in OFFSETS})
        self.assertTrue(all(tests["secondary"][o]["adverse_selection_detected"] for o in OFFSETS))
        self.assertTrue(all(flags[o]["discovery_flag"] for o in OFFSETS))
        self.assertNotIn("markout_not_significantly_adverse", flags[-300])
        # The secondary untestable changes no flag either.
        _, _, flags = self.run_decide(secondary={-300: UNDERPOWERED})
        self.assertTrue(flags[-300]["discovery_flag"])

    def test_the_realised_spread_is_judged_unadjusted(self):
        # p 0.03 below 0 is significant alone, though no Holm family would reject it over three: the flag fails.
        _, _, flags = self.run_decide(realised={-300: tested(-0.2, p=0.03)})
        self.assertFalse(flags[-300]["realised_spread_not_significantly_negative"])
        for result in (tested(-0.2, p=0.06), tested(0.2, p=0.001), tested(0.0, p=None)):
            with self.subTest(result=result):
                _, _, flags = self.run_decide(realised={-300: result})
                self.assertTrue(flags[-300]["realised_spread_not_significantly_negative"])

    def test_the_secondary_is_reported_under_holm(self):
        # p 0.03 is significant alone but not under Holm over three: not "detected".
        tests, _, _ = self.run_decide(secondary={-300: tested(0.2, p=0.03)})
        self.assertFalse(tests["secondary"][-300]["holm_rejected"])
        self.assertFalse(tests["secondary"][-300]["adverse_selection_detected"])
        tests, _, _ = self.run_decide(secondary={-300: tested(-0.2, p=0.001)})            # in our favour
        self.assertTrue(tests["secondary"][-300]["holm_rejected"])
        self.assertFalse(tests["secondary"][-300]["adverse_selection_detected"])
        self.assertAlmostEqual(tests["secondary"][-300]["ci_high"], -0.18)                 # the reported upper bound

    def test_the_null_kill_rule(self):
        for name, null, blocks in (("small and significant", tested(0.004, p=0.001), False),
                                   ("large within its noise", tested(0.02, p=0.3), False),
                                   ("half the main and significant", tested(-0.005, p=0.01), True),
                                   ("large and its noise unknown", dict(tested(0.02, p=None), se=0.0), True)):
            with self.subTest(case=name):
                _, controls, _ = self.run_decide(null={-300: null})
                self.assertEqual(controls["null"][-300]["blocks"], blocks)
                if blocks:
                    self.assertEqual(controls["null"][-300]["why"], "control_not_below")

    def test_cr1_test_gates(self):
        rows = [(f"d{k % 20}", 0.01 * (k % 7) - 0.02) for k in range(250)]
        result = cr1_test(REG, rows)
        self.assertIsNotNone(result["p_two_sided"])
        self.assertAlmostEqual(result["t"], result["mean"] / result["se"])
        self.assertEqual(cr1_test(REG, [(f"d{k % 20}", 0.01) for k in range(300)])["reason"], "se_undefined")
        # Below either gate: not tested, and counts only (as movement.cr1_contrast), never a mean or an interval.
        for rows in (rows[:199], [(f"d{k % 14}", 0.01) for k in range(300)], []):
            with self.subTest(values=len(rows)):
                result = cr1_test(REG, rows)
                self.assertEqual(result["reason"], "underpowered")
                self.assertEqual({key for key, value in result.items() if value is not None},
                                 {"bets", "race_dates", "reason"})


# An eligible decision with no fill, for the table functions.
TEMPLATE = Decision(race_date="d0", spread_bucket="S1", price_band="B3", matched=0.0, net=0.0, net_unrounded=0.0,
                    net_normalised=0.0, effect="no_positive_outcome", state="no_fill", legs={}, trd={}, hour_share=0.0)
UNFILLED = LegRow(filled=False, kind=None, seconds=None, touched_before_fill=False, touched_never_filled=False,
                  after_the_hour=False, marks=None, edge=None, null=None)


def filled_row(rng, kind=VOLUME, touched=False, undefined=()):
    """A filled leg's row with random mark-outs, null moves and edge, each defined but at the (series, horizon) keys
    in undefined (in_play)."""
    marks = {(name, h): rng.gauss(0.2, 1.0) for name in SERIES for h in REG.horizons_s}
    marks.update({(name, BSP_HORIZON): rng.gauss(0.2, 2.0) for name in MID_SERIES})
    null = {key: rng.gauss(0.0, 1.0) for key in marks if key[0] in MID_SERIES}
    marks.update(dict.fromkeys(undefined, "in_play"))
    return LegRow(filled=True, kind=kind, seconds=30.0, touched_before_fill=touched, touched_never_filled=False,
                  after_the_hour=False, marks=marks, edge={"ticks": 0.5, "points": rng.uniform(0.1, 0.6)}, null=null)


def table_decision(day, rows, net):
    """An eligible decision on race date day with legs {(role, side): LegRow}, unfilled where rows has none."""
    legs = {(role, side): rows.get((role, side), UNFILLED) for role in (MAIN, JOIN) for side in SIDES}
    lay, back = legs[MAIN, LAY].filled, legs[MAIN, BACK].filled
    state = "round_trip" if lay and back else "lay_only" if lay else "back_only" if back else "no_fill"
    return TEMPLATE._replace(race_date=day, net=net, net_unrounded=net, net_normalised=net, state=state, legs=legs,
                             trd=dict.fromkeys(TRD_COUNTS, 0))


def summary_of(decisions, pass_name):
    """summary() of a tally holding these decisions at off -300 s and none at the other decision times."""
    tally = Tally(REG)
    tally.decisions[-300] = decisions
    return summary(REG, tally, pass_name, "0" * 64)


def markdown_row(text, label, offset=-300):
    """The cells of the first Markdown table row whose first cell is label, in text's section of one decision time."""
    section = text.split(f"## Decision at off {offset} s\n")[1]
    line = next(line for line in section.splitlines() if line.startswith(f"| {label} |"))
    return [cell.strip() for cell in line.strip("|").split("|")]


class BlindReleaseTests(unittest.TestCase):
    """What the blind pass releases of a group: its SD and symmetric ranges, which neither a shift nor a change of sign
    moves (lp_study._stats), never a quantile less its median."""

    @staticmethod
    def moves(shift, n=1601):
        """+60 s mid moves in points with an atom at exactly 0 (no quote change, 45%), else around shift; n odd and
        not a multiple of 5, so that nearest-rank ranges are exactly symmetric under a change of sign."""
        rng = random.Random(11)
        return [(f"d{k % 300}", 0.0 if rng.random() < 0.45 else rng.gauss(shift, 1.0)) for k in range(n)]

    def test_a_change_of_sign_moves_nothing_released(self):
        rows = self.moves(0.25)
        flipped = [(day, -x) for day, x in rows]
        blind, mirrored = _stats(REG, rows, False), _stats(REG, flipped, False)
        self.assertEqual(blind, mirrored)
        self.assertEqual(set(blind), {"values", "race_dates", "sd", "ranges"})
        self.assertEqual(list(blind["ranges"]), ["0.95-0.05", "0.9-0.1", "0.75-0.25"])
        # The atom sits at the median, so quantiles less the median would have been the raw quantiles, which differ.
        registered, opposite = _stats(REG, rows, True), _stats(REG, flipped, True)
        self.assertEqual(registered["quantiles"]["0.5"], 0.0)
        self.assertNotEqual(registered["quantiles"], opposite["quantiles"])
        self.assertGreater(registered["mean"]["mean"], 0)
        self.assertLess(opposite["mean"]["mean"], 0)
        # A shift moves nothing released either (up to float rounding at 4 and 6 places).
        shifted = _stats(REG, [(day, x + 0.5) for day, x in rows], False)
        self.assertEqual(shifted["ranges"], blind["ranges"])
        self.assertAlmostEqual(shifted["sd"], blind["sd"], places=5)

    def test_rounding_and_the_gate(self):
        rows = [(f"d{k % 30}", k / 7) for k in range(150)]      # nearest rank: q 0.25 is value 38, 37 / 7
        registered, blind = _stats(REG, rows, True), _stats(REG, rows, False)
        self.assertEqual((registered["quantiles"]["0.05"], registered["quantiles"]["0.25"]), (1.0, 5.2857))
        self.assertEqual(blind["ranges"], {"0.95-0.05": 19.2857, "0.9-0.1": 17.1429,
                                           "0.75-0.25": 10.7143})
        self.assertEqual(blind["sd"], registered["sd"])
        # Below min_bets_for_statistics, counts only, in both passes; the registered pass says why its mean is missing.
        for registered in (False, True):
            small = _stats(REG, rows[:99], registered)
            self.assertEqual({key for key, value in small.items() if value is not None},
                             {"values", "race_dates", *(["mean"] if registered else [])})
        self.assertEqual({key: value for key, value in small["mean"].items() if value is not None},
                         {"bets": 99, "race_dates": 30, "reason": "counts_only"})

    def test_net_by_inventory_state_is_counts_only_blind(self):
        states = ("lay_only", "back_only", "round_trip", "no_fill")
        decisions = [TEMPLATE._replace(race_date=f"d{k % 40}", state=states[k % 4], net=0.0 if k % 4 == 3 else
                                       (k % 13) / 100 - 0.05) for k in range(800)]
        blind = _net(REG, decisions, False)
        self.assertEqual(set(blind), {"filled", "lay_only", "back_only", "round_trip"})
        self.assertIsNotNone(blind["filled"]["sd"])
        self.assertEqual(blind["round_trip"], {"values": 200, "race_dates": 10})
        registered = _net(REG, decisions, True)
        self.assertIsNotNone(registered["round_trip"]["sd"])
        self.assertIsNotNone(registered["all_eligible_bsp_normalised"]["mean"])

    def test_matched_volume_cut_points_need_100_decisions_and_keep_2_significant_figures(self):
        decisions = [TEMPLATE._replace(matched=12345.678 + 1000 * k) for k in range(150)]
        strata = _strata(REG, decisions)
        cuts = sorted(d.matched for d in decisions)
        self.assertEqual(strata["matched_volume_cut_points"], [float(f"{cuts[49]:.2g}"), float(f"{cuts[99]:.2g}")])
        self.assertEqual(strata["matched_volume_cut_points"], [61000.0, 110000.0])
        self.assertEqual(strata["cells"]["S1"]["B3"], {"V1": 50, "V2": 50, "V3": 50})          # cut unrounded
        small = _strata(REG, decisions[:99])
        self.assertIsNone(small["matched_volume_cut_points"])
        self.assertEqual(sum(small["cells"]["S1"]["B3"].values()), 99)

    def test_groups_by_side_are_ticks_only_blind(self):
        """Points per tick fall as the price lengthens, and a shortening hurts a LAY while a lengthening hurts a BACK,
        so the LAY group's points SD over its ticks SD, beside the BACK group's, would track the sign of the mark-out:
        by side the blind pass gives ticks only, every group pooling both sides gives both units, and the registered
        pass gives both units everywhere."""
        rng = random.Random(5)
        decisions = [table_decision(f"d{k % 30}", {(MAIN, LAY): filled_row(rng, FILL_KINDS[k % 3]),
                                                   (MAIN, BACK): filled_row(rng, FILL_KINDS[(k + 1) % 3], k % 2 == 0),
                                                   (JOIN, LAY): filled_row(rng), (JOIN, BACK): filled_row(rng)},
                                    rng.gauss(0.0, 0.3)) for k in range(300)]

        def groups(tables):
            for role, by_group in tables["markouts"].items():
                for group, series in by_group.items():
                    for name, horizons in series.items():
                        for horizon, stats in horizons.items():
                            yield (role, group, name, horizon), stats
            for group, units in tables["edge_at_fill"].items():
                for unit, stats in units.items():
                    yield (MAIN, group, f"edge_{unit}", None), stats
            yield (MAIN, "all", "realised_points", None), tables["realised_spread"]["all"]

        blind = summary_of(decisions, "blind")
        found = list(groups(blind["decision_times"]["-300"]))
        self.assertEqual(len([key for key, _ in found if key[1] in SIDES and key[2].endswith("points")]),
                         2 * (len(REG.horizons) + len(REG.horizons_s) + 1))         # mid, opposite, edge; per side
        for (role, group, name, horizon), stats in found:
            with self.subTest(role=role, group=group, series=name, horizon=horizon):
                if role == MAIN and group in SIDES and name.endswith("points"):
                    self.assertEqual(set(stats), {"values", "race_dates", "undefined"})
                    self.assertEqual(stats["values"], 300)
                else:
                    self.assertIsNotNone(stats["sd"])
                    self.assertIsNotNone(stats["ranges"])
        for key, stats in groups(summary_of(decisions, "registered")["decision_times"]["-300"]):
            with self.subTest(registered=key):
                self.assertIsNotNone(stats["sd"])
                self.assertIsNotNone(stats["mean"]["mean"])
        # The power note's secondary reads the pooled +60 s mid points, which stay released.
        self.assertIsNotNone(blind["decision_times"]["-300"]["power"]["secondary"]["minimum_detectable_points"])
        text = markdown(REG, blind)
        self.assertEqual(markdown_row(text, "LAY mid_points +60 s")[3], "-")
        self.assertNotEqual(markdown_row(text, "LAY mid_ticks +60 s")[3], "-")
        self.assertNotEqual(markdown_row(text, "all mid_points +60 s")[3], "-")
        self.assertEqual(markdown_row(text, "BACK points")[3], "-")
        self.assertNotIn("mean", text.lower())


class DescriptiveCopyTests(unittest.TestCase):
    """A descriptive group over exactly the decisions or legs of a test needs the tests' gates (200 values on 15 race
    dates) as well, in both passes: below them it reports counts only, reason underpowered as the test's. Any other
    group keeps the gate of 100 values."""

    @staticmethod
    def build(days, lays=100, backs=50, unfilled=30):
        """Decisions on `days` race dates: `lays` fill their main LAY alone, `backs` their main BACK alone and
        `unfilled` neither, each both join legs; one main leg in ten has no +30 s mid value, every other is defined."""
        rng, decisions = random.Random(3), []
        for k in range(lays + backs + unfilled):
            rows = {(JOIN, LAY): filled_row(rng), (JOIN, BACK): filled_row(rng)}
            undefined = [("mid_ticks", 30), ("mid_points", 30)] if k % 10 == 0 else []
            if k < lays + backs:
                rows[MAIN, LAY if k < lays else BACK] = filled_row(rng, undefined=undefined)
            decisions.append(table_decision(f"d{k % days}", rows, rng.gauss(0.05, 0.3) if k < lays + backs else 0.0))
        return decisions

    def assertCountsOnly(self, stats, values, registered=True):
        self.assertEqual((stats["values"], stats["sd"], stats["quantiles" if registered else "ranges"]),
                         (values, None, None))
        if registered:
            self.assertEqual({key: value for key, value in stats["mean"].items() if value is not None},
                             {"bets": values, "race_dates": stats["race_dates"], "reason": "underpowered"})

    def assertDescribed(self, stats, values, registered=True):
        self.assertEqual(stats["values"], values)
        self.assertIsNotNone(stats["sd"])
        if registered:
            self.assertIsNotNone(stats["mean"]["mean"])

    def test_copies_of_an_underpowered_test_report_counts_only(self):
        # 180 decisions and 150 filled main legs on 20 race dates: from 100 values, but below the tests' 200.
        value = summary_of(self.build(20), "registered")
        tables = value["decision_times"]["-300"]
        for family in ("primary", "secondary", "realised_spread"):
            self.assertEqual(value["tests"][family]["-300"]["reason"], "underpowered")
        join = value["controls"]["join"]["-300"]
        self.assertEqual(join["bets"], 360)
        self.assertIsNotNone(join["mean"])
        # The copies: the tests' own decisions or legs, whatever the value (net, a series, a horizon, an edge).
        main = tables["markouts"]["main"]["all"]
        copies = {"net " + name: (tables["net"][name], 180) for name in
                  ("all_eligible", "all_eligible_unrounded_commission", "all_eligible_bsp_normalised")}
        copies.update({f"main {name} 60": (main[name]["60"], 150) for name in SERIES})
        copies.update({f"main {name} {horizon}": (main[name][horizon], 150) for name in MID_SERIES
                       for horizon in ("10", BSP_HORIZON)})
        copies.update({f"null {name} 60": (tables["markouts"]["null"]["all"][name]["60"], 150) for name in MID_SERIES})
        copies.update({"realised spread": (tables["realised_spread"]["all"], 150),
                       "edge points": (tables["edge_at_fill"]["all"]["points"], 150)})
        for name, (stats, values) in copies.items():
            with self.subTest(copy=name):
                self.assertCountsOnly(stats, values)
        self.assertEqual(value["descriptive"]["markout_at_bsp"]["-300"]["reason"], "underpowered")
        self.assertEqual(value["controls"]["null"]["-300"]["at_bsp"]["null"]["reason"], "underpowered")
        # Every other group keeps the gate of 100 values: a part of a test's legs, other legs, or a powered test's.
        for name, stats, values in (("net filled", tables["net"]["filled"], 150),
                                    ("main LAY", tables["markouts"]["main"]["LAY"]["mid_points"]["60"], 100),
                                    ("main all +30 s", main["mid_points"]["30"], 135),
                                    ("join all", tables["markouts"]["join"]["all"]["mid_points"]["60"], 360),
                                    ("edge LAY", tables["edge_at_fill"]["LAY"]["points"], 100)):
            with self.subTest(other=name):
                self.assertDescribed(stats, values)
        text = markdown(REG, value)
        self.assertEqual(markdown_row(text, "all_eligible")[-1], "underpowered")
        self.assertNotEqual(markdown_row(text, "filled")[3], "-")
        # The blind pass: no SD or range for a copy, so no minimum detectable effect from the secondary's SD either.
        blind = summary_of(self.build(20), "blind")["decision_times"]["-300"]
        self.assertCountsOnly(blind["markouts"]["main"]["all"]["mid_points"]["60"], 150, registered=False)
        self.assertCountsOnly(blind["realised_spread"]["all"], 150, registered=False)
        self.assertDescribed(blind["markouts"]["main"]["all"]["mid_points"]["30"], 135, registered=False)
        self.assertDescribed(blind["net"]["filled"], 150, registered=False)
        self.assertIsNone(blind["power"]["secondary"]["minimum_detectable_points"])
        self.assertIsNotNone(blind["power"]["primary"]["minimum_detectable_gbp_per_decision"])

    def test_the_race_date_gate_applies_to_the_copies_too(self):
        # 330 decisions and 300 filled main legs, but on 14 race dates: every test is underpowered, the join's too.
        value = summary_of(self.build(14, lays=200, backs=100), "registered")
        tables = value["decision_times"]["-300"]
        self.assertEqual({family: results["-300"]["reason"] for family, results in value["tests"].items()},
                         dict.fromkeys(("primary", "secondary", "realised_spread"), "underpowered"))
        self.assertEqual(value["controls"]["join"]["-300"]["reason"], "underpowered")
        self.assertCountsOnly(tables["net"]["all_eligible"], 330)
        self.assertCountsOnly(tables["markouts"]["main"]["all"]["mid_points"]["60"], 300)
        self.assertCountsOnly(tables["markouts"]["join"]["all"]["mid_points"]["60"], 660)
        self.assertCountsOnly(tables["realised_spread"]["all"], 300)
        self.assertDescribed(tables["net"]["filled"], 300)
        self.assertDescribed(tables["markouts"]["main"]["LAY"]["mid_points"]["60"], 200)
        self.assertEqual(tables["net"]["filled"]["mean"]["reason"], "too_few_clusters")


# ---------------------------------------------------------------------------------------------------------------

def study_members():
    """Four race dates, each with a round trip, a lone LAY with a moving mid, an unfilled market and a four-runner
    market; the -120 s decision fills on later trades in the round-trip market."""
    members = []
    for day in range(4):
        off = OFF + timedelta(days=day)
        bsps = {**BSPS, R3: (6.4, 6.2, 6.6, 6.0)[day]}
        trip = f"1.1800{day}1001"
        members.append((path(trip, off), lp_market(trip, off, bsps=bsps, trades=[
            (200, R3, [[6.2, 4.0]]), (150, R3, [[6.4, 4.0]]), (60, R3, [[6.2, 8.0]])])))
        lone = f"1.1800{day}2001"
        moves = {3600: BOOK, 190: book(R3=(6.2, 6.8)), 140: book(R3=((6.0, 6.2, 5.8, 5.6)[day], 7.0))}
        members.append((path(lone, off), lp_market(lone, off, bsps=bsps, books=moves,
                                                   trades=[(200, R3, [[6.0, 0.5 + day]])])))
        idle = f"1.1800{day}3001"
        members.append((path(idle, off), lp_market(idle, off, bsps=bsps)))
        small = f"1.1800{day}9001"
        members.append((path(small, off), lp_market(small, off, runners=SIDS[:4])))
    return members


def small_registration(target):
    """The registration with thresholds small enough for synthetic data; (path, sha256)."""
    value = json.loads(REGISTRATION.read_bytes())
    value["registered_pass"]["tests"].update(min_bets=2, min_race_dates=2)
    value["uncertainty"]["min_clusters"] = 2
    value["statistics"]["min_bets_for_statistics"] = 2
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    target.write_bytes(data)
    return target, hashlib.sha256(data).hexdigest()


def walk_items(value):
    """(key, item) of every key at every depth of a JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk_items(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_items(item)


def markdown_columns(text):
    """{header: [cells]} over every Markdown table of text, the cells of columns sharing a header together."""
    out, lines = {}, text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if not (line.startswith("| ") and lines[index + 1].startswith("|---")):
            continue
        header = [cell.strip() for cell in line.strip("|").split("|")]
        for row in lines[index + 2:]:
            if not row.startswith("| "):
                break
            for name, cell in zip(header, (cell.strip() for cell in row.strip("|").split("|"))):
                out.setdefault(name, []).append(cell)
    return out


class RunTests(fx.Workspace):
    def setUp(self):
        super().setUp()
        self.root, self.manifest, self.records = self.build(study_members())
        self.registration, self.sha = small_registration(self.tmp / "registration.json")

    def run_pass(self, name, out, **kwargs):
        arguments = dict(registration_path=self.registration, clock=lambda: "2026-11-01T00:00:00.000000Z")
        arguments.update(kwargs)
        return run_study(name, arguments.pop("sha", self.sha), arguments.pop("root", self.root), out, COMMIT,
                         **arguments)

    def outputs(self, out):
        return {name: (out / name).read_bytes() for name in ("summary.json", "summary.md", "receipt.json")}

    def test_blind_then_registered(self):
        blind = self.run_pass("blind", self.tmp / "blind")
        self.assertEqual(blind["status"], "completed", blind)
        self.assertEqual((blind["records"], blind["replayed_markets"]), (16, 12))
        self.assertEqual(blind["eligible"], dict.fromkeys(map(str, OFFSETS), 12))
        self.assertEqual(blind["filled_main_legs"], {"-600": 12, "-300": 12, "-120": 4})
        files = self.outputs(self.tmp / "blind")
        value = json.loads(files["summary.json"])
        for offset, tables in value["decision_times"].items():
            self.assertEqual(sum(tables["codes"].values()), 16, offset)
            self.assertEqual(sum(tables["classes"].values()), tables["codes"]["issued"])
        fills = value["decision_times"]["-300"]["fills"]["main"]
        self.assertEqual(fills["inventory"], {"no_fill": 4, "lay_only": 4, "back_only": 0, "round_trip": 4})
        self.assertEqual((fills["legs"]["LAY"]["filled"], fills["legs"]["LAY"]["by_kind"]),
                         (8, {"trade_through": 4, "quote_cross": 0, "volume_at_price": 4}))
        self.assertEqual(fills["both_fill_rate"], round(4 / 12, 6))
        self.assertEqual(set(fills), {"eligible", "legs", "both_filled", "both_fill_rate", "inventory",
                                      "within_s_of_t", "after_the_hour"})
        self.assertEqual(set(fills["legs"]["LAY"]), {"filled", "fill_rate", "by_kind", "touched_before_fill",
                                                     "touched_never_filled", "seconds_to_fill"})
        tables = value["decision_times"]["-300"]
        self.assertEqual(set(tables["trd_changes"]), set(TRD_COUNTS))
        self.assertEqual(tables["trd_changes"]["rises_counted"], 16)                  # 4 trades a race date
        self.assertEqual(set(tables["excluded_fills"]), set(CLASSES) - {"eligible"})
        # No location anywhere in the blind pass: no mean, no quantile of an outcome (only the symmetric ranges), no
        # statistic of net over every eligible decision or by inventory state but counts.
        self.assertEqual([key for key in walk_keys(value) if "mean" in key.lower()], [])
        markdown = files["summary.md"].decode()
        self.assertNotIn("mean", markdown.lower())
        self.assertNotIn("median", markdown.lower())
        self.assertEqual({"quantiles", "quantiles_minus_median", "tests", "descriptive", "controls", "flags",
                          "all_eligible", "all_eligible_unrounded_commission", "all_eligible_bsp_normalised"}
                         & set(walk_keys(value)), set())
        self.assertEqual({key for key in walk_keys(value) if "quantile" in key}, {"seconds_quantiles"})
        ranges = [item for key, item in walk_items(value) if key == "ranges"]
        self.assertTrue(ranges)
        self.assertEqual({frozenset(item) for item in ranges if item is not None},
                         {frozenset(("0.95-0.05", "0.9-0.1", "0.75-0.25"))})
        net = value["decision_times"]["-300"]["net"]
        self.assertEqual(set(net), {"filled", "lay_only", "back_only", "round_trip"})
        self.assertEqual(set(net["filled"]), {"values", "race_dates", "sd", "ranges"})
        for state in ("lay_only", "back_only", "round_trip"):                     # by state: counts only
            self.assertEqual(set(net[state]), {"values", "race_dates"})
        self.assertEqual(net["round_trip"], {"values": 4, "race_dates": 4})
        mark = value["decision_times"]["-300"]["markouts"]["main"]["LAY"]["mid_ticks"]["10"]
        self.assertEqual((mark["values"], set(mark)), (8, {"values", "race_dates", "undefined", "sd", "ranges"}))
        self.assertIsNotNone(mark["sd"])
        groups = value["decision_times"]["-300"]["markouts"]["main"]
        self.assertEqual(set(groups), {"all", "LAY", "BACK", *FILL_KINDS, "touched_before_fill"})
        self.assertEqual(set(value["decision_times"]["-300"]["edge_at_fill"]), set(groups))
        realised = value["decision_times"]["-300"]["realised_spread"]["all"]
        self.assertEqual((realised["values"], set(realised)), (12, {"values", "race_dates", "undefined", "sd",
                                                                     "ranges"}))
        self.assertEqual(value["decision_times"]["-300"]["commission_rounding"]["no_positive_outcome"], 4)
        self.assertIsNotNone(value["decision_times"]["-300"]["power"]["primary"]["minimum_detectable_gbp_per_decision"])
        self.assertEqual(set(value["decision_times"]["-300"]["trd_after_the_hour_expected_if_uniform"]),
                         {"rises_counted", "rises_not_counted"})
        receipt = json.loads(files["receipt.json"])
        self.assertEqual((receipt["study"], receipt["pass"], receipt["registration_sha256"], receipt["code_commit"]),
                         ("lp-v1", "blind", self.sha, COMMIT))

        refused = self.run_pass("registered", self.tmp / "registered")
        self.assertEqual(refused, {"status": "refused", "problems": ["blind_receipt:missing"]})
        registered = self.run_pass("registered", self.tmp / "registered", blind_receipt=self.tmp / "blind/receipt.json")
        self.assertEqual(registered["status"], "completed", registered)
        files = self.outputs(self.tmp / "registered")
        value = json.loads(files["summary.json"])
        tables = value["decision_times"]["-300"]
        nets = tables["net"]
        self.assertEqual((nets["all_eligible"]["values"], nets["all_eligible_bsp_normalised"]["values"]), (12, 12))
        self.assertIn("quantiles", nets["round_trip"])
        self.assertIsNotNone(nets["round_trip"]["mean"]["mean"])
        self.assertEqual(set(value["tests"]), {"primary", "secondary", "realised_spread"})
        self.assertEqual(set(value["flags"]), {"-600", "-300", "-120"})
        self.assertAlmostEqual(value["tests"]["primary"]["-300"]["mean"], nets["all_eligible"]["mean"]["mean"],
                               places=6)
        # The realised spread's test and the descriptive rows read the same legs as the decision tables.
        self.assertAlmostEqual(value["tests"]["realised_spread"]["-300"]["mean"],
                               tables["realised_spread"]["all"]["mean"]["mean"], places=6)
        self.assertAlmostEqual(value["descriptive"]["markout_at_bsp"]["-300"]["mean"],
                               tables["markouts"]["main"]["all"]["mid_points"]["bsp"]["mean"]["mean"], places=6)
        at_bsp = value["controls"]["null"]["-300"]["at_bsp"]
        self.assertEqual(set(at_bsp), {"null", "main_same_legs"})
        self.assertEqual(at_bsp["null"]["bets"], at_bsp["main_same_legs"]["bets"])
        self.assertIsNotNone(at_bsp["null"]["mean"])
        self.assertIn("quantiles", nets["filled"])
        markdown = files["summary.md"].decode()
        for text in ("mean", "Realised spread at the secondary's horizon", "Mark-out at BSP beside the secondary",
                     "Null control at BSP", "picked off but paid for it", "CI high (points)"):
            self.assertIn(text, markdown)
        self.assertEqual(json.loads(files["receipt.json"])["blind_receipt_sha256"],
                         hashlib.sha256((self.tmp / "blind/receipt.json").read_bytes()).hexdigest())

    def test_groups_below_the_gates_report_counts_only(self):
        """The registration as it is: 12 eligible decisions a decision time, far below min_bets_for_statistics
        (100) and the tests' gates (200 values on 15 race dates). Neither pass prints a mean, an SE, an interval, a
        p, an SD, a quantile, a range or a cut point anywhere, in summary.json or summary.md."""
        sha = REG.sha256
        self.assertEqual(self.run_pass("blind", self.tmp / "blind", registration_path=REGISTRATION, sha=sha)["status"],
                         "completed")
        registered = self.run_pass("registered", self.tmp / "registered", registration_path=REGISTRATION, sha=sha,
                                   blind_receipt=self.tmp / "blind/receipt.json")
        self.assertEqual(registered["status"], "completed", registered)
        statistics = {"mean", "se", "df", "t_critical", "ci_low", "ci_high", "t", "p_two_sided", "holm_p_adjusted",
                      "sd", "quantiles", "ranges", "seconds_quantiles", "matched_volume_cut_points",
                      "minimum_detectable_gbp_per_decision", "minimum_detectable_points"}
        columns = {"SD", "mean", "CI low", "CI high", "p", "Holm p", "null mean", "null p", "main over the same legs",
                   "CI high (points)"}
        for out in ("blind", "registered"):
            files = self.outputs(self.tmp / out)
            value = json.loads(files["summary.json"])
            # A group's mean entry is a container (its counts and why it has no mean): walk_items checks its keys.
            found = [(key, item) for key, item in walk_items(value) if key in statistics and item is not None
                     and not (key == "mean" and isinstance(item, dict))]
            with self.subTest(out=out, file="summary.json"):
                self.assertEqual(found, [])
            tables = markdown_columns(files["summary.md"].decode())
            with self.subTest(out=out, file="summary.md"):
                self.assertEqual({column: cells for column, cells in tables.items()
                                  if column in columns and set(cells) != {"-"}}, {})
                self.assertIn("2 significant figures): None.", files["summary.md"].decode())
        value = json.loads(self.outputs(self.tmp / "registered")["summary.json"])
        self.assertEqual({result["reason"] for family in value["tests"].values() for result in family.values()},
                         {"underpowered"})
        # At -300 s every filled main leg has its +60 s value, so the legs at BSP are the secondary's: its reason.
        self.assertEqual(value["tests"]["secondary"]["-300"]["bets"], 12)
        self.assertEqual(value["descriptive"]["markout_at_bsp"]["-300"]["reason"], "underpowered")
        self.assertEqual(value["descriptive"]["markout_at_bsp"]["-300"]["bets"], 12)
        tables = value["decision_times"]["-300"]
        self.assertEqual((tables["net"]["all_eligible"]["mean"]["reason"], tables["net"]["filled"]["mean"]["reason"]),
                         ("underpowered", "counts_only"))
        self.assertFalse(any(flag["discovery_flag"] for flag in value["flags"].values()))

    def cli(self, *arguments):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--registration-sha256", self.sha, "--archive-root", str(self.root), "--code-commit", COMMIT,
                         "--registration", str(self.registration), *arguments])
        return code, out.getvalue()

    def test_no_output_holds_a_market_or_selection_id(self):
        ids = sorted(self.records) + [str(sid) for sid in SIDS] + [fx.EVENT]
        blind = self.cli("--pass", "blind", "--out", str(self.tmp / "blind"))
        registered = self.cli("--pass", "registered", "--out", str(self.tmp / "registered"), "--blind-receipt",
                              str(self.tmp / "blind/receipt.json"))
        self.assertEqual((blind[0], registered[0]), (0, 0))
        for out in ("blind", "registered"):
            for name, data in self.outputs(self.tmp / out).items():
                with self.subTest(out=out, file=name):
                    self.assertEqual([item for item in ids if item in data.decode()], [])
        for name, (_, printed) in {"blind": blind, "registered": registered}.items():
            with self.subTest(stdout=name):
                self.assertEqual([item for item in ids if item in printed], [])

    def test_the_registration_hash_is_required(self):
        out = self.tmp / "out"
        for sha, problem in (("0" * 64, "registration:sha256_mismatch"), ("abc", "registration_sha256:malformed")):
            for pass_name in ("blind", "registered"):
                with self.subTest(problem=problem, pass_name=pass_name):
                    self.assertEqual(self.run_pass(pass_name, out, sha=sha),
                                     {"status": "refused", "problems": [problem]})
        changed = self.tmp / "changed.json"
        value = json.loads(self.registration.read_bytes())
        value["orders"]["fill_kinds"].reverse()
        changed.write_bytes(json.dumps(value).encode())
        self.assertEqual(self.run_pass("blind", out, registration_path=changed,
                                       sha=hashlib.sha256(changed.read_bytes()).hexdigest()),
                         {"status": "refused", "problems": ["registration:orders.fill_kinds:not_the_implemented_list"]})
        # The movement-v1 registration, hash and all, is not this study's.
        movement = REPO / "research" / "studies" / "movement-v1.json"
        self.assertEqual(self.run_pass("blind", out, registration_path=movement,
                                       sha=hashlib.sha256(movement.read_bytes()).hexdigest())["status"], "refused")
        self.assertFalse(out.exists())

    def test_the_out_directory_is_refused_in_a_worktree_a_data_tree_or_the_root(self):
        worktree = self.tmp / "clone"
        (worktree / ".git").mkdir(parents=True)
        data_tree = self.tmp / "Betangel-Data"
        data_tree.mkdir()
        existing = self.tmp / "existing"
        existing.mkdir()
        (self.tmp / "again.partial").mkdir()
        for out, problem in ((worktree / "out", "out:inside_git_worktree"), (REPO / "out", "out:inside_repository"),
                             (data_tree / "out", "out:under_a_data_tree"),
                             (self.tmp / "soccer-data" / "out", "out:under_a_data_tree"),
                             (existing, "out:already_exists"), (self.root / "out", "roots:not_distinct"),
                             (self.tmp / "missing" / "out", "out:unresolvable"),
                             (self.tmp / "again", "out:partial_present")):
            with self.subTest(out=str(out)):
                self.assertEqual(self.run_pass("blind", out), {"status": "refused", "problems": [problem]})
                self.assertFalse(out.exists() and out != existing)

    def test_the_blind_receipt_must_be_this_registrations_code_and_input(self):
        self.run_pass("blind", self.tmp / "blind")
        receipt = json.loads((self.tmp / "blind/receipt.json").read_bytes())
        forged = self.tmp / "forged.json"
        for change, problem in (({"registration_sha256": "1" * 64}, "blind_receipt:another_registration"),
                                ({"input_sha256": "2" * 64}, "blind_receipt:another_input"),
                                ({"code_commit": "f" * 40}, "blind_receipt:another_code"),
                                ({"study": "movement-v1"}, "blind_receipt:not_a_completed_blind_receipt")):
            with self.subTest(problem=problem):
                forged.write_bytes(canonical_json({**receipt, **change}) + b"\n")
                self.assertEqual(self.run_pass("registered", self.tmp / "registered", blind_receipt=forged),
                                 {"status": "refused", "problems": [problem]})
        self.assertFalse((self.tmp / "registered").exists())

    def test_outputs_are_private_whole_and_repeatable(self):
        out = self.tmp / "one"
        self.assertEqual(self.run_pass("blind", out)["status"], "completed")
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertEqual({name: stat.S_IMODE((out / name).stat().st_mode) for name in self.outputs(out)},
                         dict.fromkeys(("summary.json", "summary.md", "receipt.json"), 0o600))
        self.run_pass("blind", self.tmp / "two")
        self.assertEqual(self.outputs(out)["summary.json"], self.outputs(self.tmp / "two")["summary.json"])

    def test_a_market_twice_in_the_registered_tier_refuses(self):
        root = self.tmp / "twice"
        root.mkdir()
        for index, event in enumerate((fx.EVENT, "27000002")):
            member = fx.path(M, when=OFF, plan="PRO", event=event)
            tar = fx.make_tar(self.tmp / f"twice{index}.tar", [(member, bz2.compress(fx.lines(lp_market())))])
            self.assertEqual(ingest_archive(tar, root)["status"], "ingested")
        self.assertEqual(index_archives(root)["status"], "indexed")
        result = self.run_pass("blind", self.tmp / "twice-out", root=root)
        self.assertEqual(result["status"], "failed", result)
        self.assertRegex(result["failure"], r"^input:[0-9a-f]{64}:duplicate_market$")
        self.assertFalse((self.tmp / "twice-out").exists())


class ImportBoundaryTests(unittest.TestCase):
    def test_the_study_stays_inside_the_backtest_closure(self):
        from test_betfair_backtest_boundaries import (BACKTEST, BETFAIR, FORBIDDEN_BETFAIR, REAL, TOP,
                                                      backtest_violations)
        closure = REAL.closure([f"{BACKTEST}.lp", f"{BACKTEST}.lp_study"])
        self.assertEqual({module for module in closure if not module.startswith(BACKTEST)},
                         {TOP, BETFAIR, f"{TOP}.temporal"})
        self.assertEqual(backtest_violations(REAL, betfair=BETFAIR, forbidden=FORBIDDEN_BETFAIR), [])

    def test_importing_the_study_loads_no_other_betfair_module(self):
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "import betangel_research.betfair.backtest.lp_study; "
                 "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('betangel_research'))))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(REPO)], capture_output=True, text=True,
                                check=True)
        loaded = set(result.stdout.split())
        self.assertEqual({m for m in loaded if not m.startswith("betangel_research.betfair.backtest")},
                         {"betangel_research", "betangel_research.betfair", "betangel_research.temporal"})


if __name__ == "__main__":
    unittest.main()
