"""Registered measurement movement-v1 (betangel_research.betfair.backtest.movement and movement_study).

Every archive here is synthetic and goes through the real ingest, index and replay, with the fixture helpers of
tests/test_betfair_backtest_replay.py; selection ids, market ids and the event id are distinctive, so the output scan
finds any that leak. These tests show the study's rules on these synthetic shapes only; they say nothing about real
Betfair files or results.
"""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from fractions import Fraction
import hashlib
import io
import itertools
import json
from pathlib import Path
import random
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest

from betangel_research.betfair import constants, pilot, ticks
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, safe_target
from betangel_research.betfair.backtest.contract import canonical_json
from betangel_research.betfair.backtest.movement import (CLASSIC_BANDS_H, CONTRASTS, CONTROLS, LADDER_H, MARKET_CODES,
                                                         LadderFold, Moment, RegistrationError, cr1_contrast, cr1_mean,
                                                         exact, field_share, holm, kill_and_flags, late_move_cell,
                                                         nclv, nearest_rank, parse_registration, permuted_contrast,
                                                         sample_sd, select, student_t_quantile, tick_index,
                                                         tick_position, window_problem)
from betangel_research.betfair.backtest.movement_study import Tally, decide_record, main, run_study
from betangel_research.betfair.book import MarketBookCache

import test_betfair_backtest_replay as fx
from test_betfair_backtest_replay import OFF, definition, iso, mcm, md, ms, runner

REPO = Path(__file__).resolve().parent.parent
REGISTRATION = REPO / "research" / "studies" / "movement-v1.json"
P1_CONTRACT = REPO / "research" / "pilots" / "p1-fav-t5.json"
REG = parse_registration(REGISTRATION.read_bytes())
COMMIT = "0123456789abcdef0123456789abcdef01234567"
FAV, R2, R3, R4, R5, R6 = SIDS = (41000001, 41000002, 41000003, 41000004, 41000005, 41000006)
FIELD = {R2: (4.0, 4.1), R3: (6.0, 6.2), R4: (8.0, 8.4), R5: (10.0, 10.5), R6: (15.0, 16.0)}
BSPS = {FAV: 2.5, R2: 4.2, R3: 6.4, R4: 8.6, R5: 11.0, R6: 17.0}
MINUTES = (58, 32, 12, 7, 3)            # book updates before the replayed times off -55, -30, -10, -5 and -2 min
M = "1.170000001"
OFFSETS = (-1800, -600, -300, -120)


def quotes(fav=(2.5, 2.52), field=None, runners=SIDS):
    """{selection id: (back, lay[, back size])}: the favourite's quote and the standard field's."""
    book = {FAV: fav, **(FIELD if field is None else field)}
    return {sid: quote for sid, quote in book.items() if sid in runners}


class Ladders:
    """One market's ladder deltas: PRO clears a changed price with size 0, ADVANCED replaces level 0."""

    def __init__(self, plan):
        self.plan, self.last = plan, {}

    def rc(self, book):
        items = []
        for sid, (back, lay, *size) in sorted(book.items()):
            back_size = size[0] if size else 50.0
            if self.plan == "PRO":
                old_back, old_lay = self.last.get(sid, (None, None))
                atb = ([[old_back, 0]] if old_back is not None and old_back != back else [])
                atl = ([[old_lay, 0]] if old_lay is not None and old_lay != lay else [])
                atb += [[back, back_size]] if back is not None else []
                atl += [[lay, 75.0]] if lay is not None else []
                items.append({"id": sid, "atb": atb, "atl": atl})
            else:
                items.append({"id": sid, "batb": [[0, back, back_size] if back is not None else [0, 0, 0]],
                              "batl": [[0, lay, 75.0] if lay is not None else [0, 0, 0]]})
            self.last[sid] = (back, lay)
        return items


def market(market_id=M, off=OFF, plan="PRO", books=None, runners=SIDS, bsps=None, statuses=None, extra=(),
           opening_minutes=120, reconciled=True, bad_tail=False, tail=(), **fields):
    """A market's messages: its definition at off - opening_minutes, ladder deltas at off - MINUTES (books maps
    minutes to quotes; default the standard book at each), extra messages, then BSP reconciled and CLOSED after
    the off with statuses (default FAV wins) and bsps, then tail; bad_tail appends an unreadable line after that."""
    ladders, opening = Ladders(plan), off - timedelta(minutes=opening_minutes)
    timed = [(ms(opening), md(market_id, opening, definition(off, [runner(sid) for sid in runners], **fields)))]
    books = {minutes: quotes(runners=runners) for minutes in MINUTES} if books is None else books
    for minutes, book in books.items():
        when = off - timedelta(minutes=minutes)
        timed.append((ms(when), mcm(when, [{"id": market_id, "rc": ladders.rc(book)}])))
    timed += [(message["pt"], message) for message in extra]
    statuses = statuses or {sid: "WINNER" if sid == FAV else "LOSER" for sid in runners}
    bsps = BSPS if bsps is None else bsps
    final = [runner(sid, statuses[sid], bsps.get(sid) if statuses[sid] in ("WINNER", "LOSER") else None)
             for sid in runners]
    settled = off + timedelta(minutes=8)
    after = [md(market_id, off + timedelta(minutes=2), definition(off, final, in_play=True,
                                                                  bspReconciled=reconciled, **fields)),
             md(market_id, settled, definition(off, final, status="CLOSED", in_play=True, bspReconciled=reconciled,
                                               settledTime=iso(settled), **fields))]
    messages = [message for _, message in sorted(timed, key=lambda item: item[0])] + after + list(tail)
    return messages + ([b"not json\n"] if bad_tail else [])


def path(market_id, when=OFF, plan="PRO"):
    """The member path of a market file; PRO unless asked (the replay helpers default to BASIC)."""
    return fx.path(market_id, when=when, plan=plan)


def at(off, minutes):
    return off - timedelta(minutes=minutes)


def moment(book, malformed=False, active=None):
    """A Moment holding quotes {sid: (back, lay[, back size])} and, when active is given, a view with those runners
    ACTIVE and listed."""
    rows = []
    for sid, (back, lay, *size) in sorted(book.items()):
        rows.append((sid, None if back is None else (exact(back), exact(size[0] if size else 50.0)),
                     None if lay is None else (exact(lay), exact(75.0))))
    view = None if active is None else SimpleNamespace(runners=tuple(
        SimpleNamespace(selection_id=sid, status="ACTIVE", listed=True) for sid in active))
    return Moment(offset_s=-300, t=0, view=view, reason=None, quotes=tuple(rows), ladder_malformed=malformed)


def outcome_of(tally):
    """{offset: the one market code, issued class or None} of a tally holding one record."""
    found = {}
    for offset in OFFSETS:
        codes = [code for code, count in tally.codes[offset].items() if count]
        classes = [name for name, count in tally.classes[offset].items() if count]
        found[offset] = classes[0] if codes == ["issued"] else codes[0]
    return found


def walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item)


class StudyCase(fx.Workspace):
    def decide(self, members, registration=REG):
        root, manifest, records = self.build(members)
        tally = Tally(registration)
        for market_id in sorted(records):
            decide_record(registration, records[market_id], root, manifest, tally)
        return tally

    def one(self, messages, plan="PRO", off=OFF, market_id=M):
        return self.decide([(path(market_id, when=off, plan=plan), messages)])


# ---------------------------------------------------------------------------------------------------------------

class LadderAndNumberTests(unittest.TestCase):
    def test_the_classic_ladder_is_the_clients(self):
        self.assertEqual(CLASSIC_BANDS_H, constants.CLASSIC_BANDS)
        self.assertEqual(LADDER_H, ticks.LADDER_CLASSIC)

    def test_tick_index_across_band_boundaries(self):
        for price, index in ((1.01, 0), (1.99, 98), (2.0, 99), (2.02, 100), (2.98, 148), (3.0, 149), (3.05, 150),
                             (3.95, 168), (4.0, 169), (4.1, 170), (1000, 349)):
            with self.subTest(price=price):
                self.assertEqual(tick_index(exact(price)), index)
                self.assertEqual(tick_index(exact(price)), ticks.tick_index(ticks.to_hundredths(price)))
        for price in (2.01, 2.99, 3.02, 4.05, 1.005, 1001):
            self.assertIsNone(tick_index(exact(price)), price)
        # Two ticks from 1.99 to 2.02 across the 2.00 boundary; one from 2.98 to 3.00.
        self.assertEqual(tick_index(exact(2.02)) - tick_index(exact(1.99)), 2)
        self.assertEqual(tick_index(exact(3.0)) - tick_index(exact(2.98)), 1)

    def test_tick_position_is_linear_off_the_ladder(self):
        self.assertEqual(tick_position(exact(2.01)), Fraction(199, 2))
        self.assertEqual(tick_position(exact(3.47)), 158 + Fraction(2, 5))
        self.assertEqual(tick_position(exact(2.5)), 124)
        self.assertIsNone(tick_position(exact(1.005)))
        self.assertIsNone(tick_position(exact(1001)))

    def test_exact_reads_the_files_numbers(self):
        self.assertEqual(exact(2.04), Fraction(51, 25))
        self.assertEqual(exact(3), Fraction(3))
        for value in (True, None, "2.0", float("nan"), float("inf")):
            self.assertIsNone(exact(value), value)

    def test_nclv_hand_worked_and_pilots(self):
        c = Fraction(2, 100)
        for price, bsp, expected in ((2.0, 2.0, Fraction(-1, 100)), (2.04, 2.0, Fraction(96, 10000)),
                                     (3.05, 3.0, Fraction(3, 1000)), (1.52, 1.5, Fraction(64, 10000))):
            with self.subTest(price=price, bsp=bsp):
                value = nclv(exact(price), exact(bsp), c)
                self.assertEqual(value, expected)
                self.assertEqual(value, pilot.nclv(exact(price), exact(bsp), c=c))
        # At P = BSP the cost is -c(BSP - 1) / BSP.
        self.assertEqual(nclv(exact(4.0), exact(4.0), c), -c * 3 / 4)


class RegistrationTests(unittest.TestCase):
    def mutated(self, change):
        value = json.loads(REGISTRATION.read_bytes())
        change(value)
        return json.dumps(value).encode()

    def test_the_repository_registration_parses_and_is_named_by_its_bytes(self):
        raw = REGISTRATION.read_bytes()
        self.assertEqual(REG.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(REG.decision_offsets_s, OFFSETS)
        self.assertEqual(REG.replay_offsets_s, (-3300, -1800, -600, -300, -120))
        self.assertEqual(REG.commission, Fraction(2, 100))
        self.assertEqual(REG.move_threshold, Fraction(5, 100))
        self.assertEqual(REG.random_runner_seed, 2)
        self.assertEqual((REG.kill_fraction, REG.kill_alpha, REG.min_cell_mean_nclv), (Fraction(1, 2), Fraction(1, 20),
                                                                                       Fraction(5, 1000)))
        self.assertEqual((REG.min_bets_for_statistics, REG.min_stake_p), (100, 200))
        self.assertEqual(REG.tiers, ("ADVANCED", "PRO"))
        self.assertEqual(REG.months, tuple(f"2015-{m:02d}" for m in range(5, 13)) + ("2016-01", "2016-02", "2016-03",
                                                                                      "2016-04"))

    def test_no_released_quantile_is_a_groups_minimum_or_maximum(self):
        n = REG.min_bets_for_statistics
        self.assertGreater(nearest_rank(range(n), REG.quantiles)[REG.quantiles[0]], 0)
        self.assertLess(nearest_rank(range(n), REG.quantiles)[REG.quantiles[-1]], n - 1)

    def test_the_text_says_what_the_code_does(self):
        value = json.loads(REGISTRATION.read_bytes())
        for text in (value["summary"], value["decided_by"]):
            self.assertNotIn("nothing in backtest-p0-bsp-v1 or the certified engine", text)
            self.assertNotIn("not a change to the certified engine", text)
        self.assertIn("certificate key", value["summary"])
        self.assertIn("positive", value["registered_pass"]["discovery"]["rule"])
        self.assertIn("market_in_two_tiers", value["data"]["duplicates"])
        self.assertIn("empty list empties that ladder", value["population"]["quotes"])
        self.assertNotIn("shuffle", value["seeds"])

    def test_the_population_is_pilot_ones(self):
        p1 = json.loads(P1_CONTRACT.read_bytes())
        rule = p1["rule"]
        self.assertEqual(list(REG.countries), rule["market"]["country_codes"])
        self.assertEqual((REG.event_type_id, REG.market_type, REG.betting_type, REG.number_of_winners,
                          REG.min_active_runners),
                         (rule["market"]["event_type_id"], rule["market"]["market_type"],
                          rule["market"]["betting_type"], rule["market"]["number_of_winners"],
                          rule["market"]["min_active_runners_in_decision_book"]))
        selection = rule["selection"]
        self.assertEqual((REG.best_back_min_h, REG.best_back_max_h, REG.min_available_p, REG.max_lay_ticks),
                         (selection["best_back_min_h"], selection["best_back_max_h"],
                          selection["min_available_at_best_back_p"], selection["max_best_lay_ticks_above_best_back"]))
        self.assertEqual(REG.commission, Fraction(p1["commission_rate"]))
        self.assertIn(rule["window"]["decision_book_observed_from_offset_s"], REG.decision_offsets_s)
        self.assertEqual(REG.late_offset_s, rule["window"]["decision_book_observed_from_offset_s"])

    def test_pilots_codes_keep_pilots_order(self):
        shared = [code for code in pilot.SKIP_CODES if code in MARKET_CODES]
        self.assertEqual(shared, [code for code in MARKET_CODES if code in pilot.SKIP_CODES])
        self.assertEqual(len(shared), 15)

    def test_a_changed_rule_is_refused(self):
        def reorder(value):
            codes = value["population"]["codes"]
            codes[-1], codes[-2] = codes[-2], codes[-1]

        cases = {
            "population.codes:not_the_implemented_list": reorder,
            "top:keys_not_the_registered_set": lambda value: value.update(extra="x"),
            "strata.price_bands_h:bands_not_contiguous":
                lambda value: value["strata"]["price_bands_h"][1].__setitem__(1, 202),
            "strata.field_size_bands:bands_not_contiguous":
                lambda value: value["strata"]["field_size_bands"][1].__setitem__(1, 9),
            "late_move.threshold:not_a_decimal_rate": lambda value: value["late_move"].update(threshold="five"),
            "status:not_supported": lambda value: value.update(status="draft"),
            "timing.decision_offsets_s:not_sorted_and_unique":
                lambda value: value["timing"].update(decision_offsets_s=[-300, -1800]),
            "data.tiers:not_supported": lambda value: value["data"].update(tiers=["BASIC"]),
            "registered_pass.controls.earlier_window:not_before_the_late_window":
                lambda value: value["registered_pass"]["controls"]["earlier_window"].update(to_offset_s=-600),
            "uncertainty.method:not_supported": lambda value: value["uncertainty"].update(method="bootstrap"),
            "registered_pass.contrasts:not_the_implemented_list":
                lambda value: value["registered_pass"].update(contrasts=["STEAM-FLAT"]),
        }
        for code, change in cases.items():
            with self.subTest(code=code), self.assertRaises(RegistrationError) as caught:
                parse_registration(self.mutated(change))
            self.assertEqual(str(caught.exception), f"registration:{code}")
        with self.assertRaises(RegistrationError):
            parse_registration(b"{not json")


class LadderFoldTests(unittest.TestCase):
    def test_pro_price_keyed_deltas(self):
        fold = LadderFold(("atb", "atl"))
        fold.change(1, {"rc": [{"id": 7, "atb": [[2.5, 10], [2.48, 5]], "atl": [[2.52, 7], [2.54, 9]]}]})
        self.assertEqual(fold.quotes(), ((7, (exact(2.5), 10), (exact(2.52), 7)),))
        fold.change(2, {"rc": [{"id": 7, "atb": [[2.5, 0]], "atl": [[2.52, 0]]}]})
        self.assertEqual(fold.quotes(), ((7, (exact(2.48), 5), (exact(2.54), 9)),))
        fold.change(3, {"rc": [{"id": 7, "ltp": 2.5, "batb": [[0, 9.0, 1]]}]})          # another tier's key: unread
        self.assertEqual(fold.quotes(), ((7, (exact(2.48), 5), (exact(2.54), 9)),))
        self.assertEqual(fold.malformed, [])

    def test_advanced_level_keyed_deltas(self):
        fold = LadderFold(("batb", "batl"))
        fold.change(1, {"rc": [{"id": 7, "batb": [[0, 2.5, 10], [1, 2.48, 5]], "batl": [[0, 2.52, 7]]}]})
        self.assertEqual(fold.quotes(), ((7, (exact(2.5), 10), (exact(2.52), 7)),))
        fold.change(2, {"rc": [{"id": 7, "batb": [[0, 2.48, 5], [1, 2.46, 3]], "batl": [[0, 0, 0]]}]})
        self.assertEqual(fold.quotes(), ((7, (exact(2.48), 5), None),))
        fold.change(3, {"rc": [{"id": 7, "atb": [[9.0, 1]]}]})                              # PRO's key: unread
        self.assertEqual(fold.quotes(), ((7, (exact(2.48), 5), None),))

    def test_an_image_replaces_every_runners_ladders(self):
        fold = LadderFold(("atb", "atl"))
        fold.change(1, {"rc": [{"id": 7, "atb": [[2.5, 10]], "atl": [[2.52, 7]]},
                               {"id": 8, "atb": [[4.0, 10]], "atl": [[4.1, 7]]}]})
        fold.change(2, {"img": True, "rc": [{"id": 7, "atb": [[2.6, 3]]}]})
        self.assertEqual(fold.quotes(), ((7, (exact(2.6), 3), None), (8, None, None)))

    def test_malformed_entries_are_recorded_never_guessed(self):
        for item in ({"id": "x7", "atb": [[2.5, 10]]}, {"id": 7, "atb": "2.5"}, {"id": 7, "atb": [[2.5]]},
                     {"id": 7, "atb": [[2.5, -1]]}, {"id": 7, "atb": [[1.0, 5]]}, {"id": 7, "atb": [[True, 5]]},
                     {"id": 7, "atb": [[None, 0]]}):                   # a PRO size 0 must name its price
            with self.subTest(item=item):
                fold = LadderFold(("atb", "atl"))
                fold.change(5, {"rc": [item]})
                self.assertEqual(fold.malformed, [5])
        fold = LadderFold(("batb", "batl"))
        fold.change(6, {"rc": [{"id": 7, "batb": [[-1, 2.5, 5]]}]})
        self.assertEqual(fold.malformed, [6])
        fold = LadderFold(("batb", "batl"))
        fold.change(7, {"rc": [{"id": 7, "batb": [[0, None, 0]]}]})       # a level needs no price to be emptied
        self.assertEqual(fold.malformed, [])

    # The review's cases A, B and C (2026-10-31), where the first fold disagreed with book.py's F54 rules.
    def test_a_advanced_size_zero_at_a_level_empties_it_and_every_deeper_level(self):
        fold = LadderFold(("batb", "batl"))
        fold.change(1, {"rc": [{"id": 7, "batb": [[0, 2.5, 100], [1, 2.48, 50], [2, 2.46, 30]],
                                "batl": [[0, 2.52, 80], [1, 2.54, 10]]}]})
        fold.change(2, {"rc": [{"id": 7, "batb": [[0, 0, 0]], "batl": [[1, 0, 0]]}]})
        self.assertEqual(fold.quotes(), ((7, None, (exact(2.52), 80)),))
        self.assertEqual(fold.ladders(7), ([], [(exact(2.52), 80)]))

    def test_b_advanced_best_is_level_zero_of_the_ordered_prefix_not_a_stale_deeper_level(self):
        fold = LadderFold(("batb", "batl"))
        fold.change(1, {"rc": [{"id": 7, "batb": [[0, 2.5, 100], [1, 2.48, 50]], "batl": [[0, 2.52, 80]]}]})
        fold.change(2, {"rc": [{"id": 7, "batb": [[0, 2.4, 20]]}]})
        self.assertEqual(fold.quotes(), ((7, (exact(2.4), 20), (exact(2.52), 80)),))
        self.assertEqual(fold.ladders(7)[0], [(exact(2.4), 20)])          # level 1 (2.48) is out of order: dropped
        fold.change(3, {"rc": [{"id": 7, "batb": [[1, 2.38, 9]]}]})
        self.assertEqual(fold.ladders(7)[0], [(exact(2.4), 20), (exact(2.38), 9)])

    def test_c_pro_an_empty_list_empties_that_ladder(self):
        fold = LadderFold(("atb", "atl"))
        fold.change(1, {"rc": [{"id": 7, "atb": [[2.5, 100], [2.48, 50]], "atl": [[2.52, 80]]}]})
        fold.change(2, {"rc": [{"id": 7, "atb": []}]})
        self.assertEqual(fold.quotes(), ((7, None, (exact(2.52), 80)),))

    def test_an_advanced_empty_list_and_a_null_change_nothing(self):
        fold = LadderFold(("batb", "batl"))
        fold.change(1, {"rc": [{"id": 7, "batb": [[0, 2.5, 100]], "batl": [[0, 2.52, 80]]}]})
        fold.change(2, {"rc": [{"id": 7, "batb": [], "batl": None}]})
        self.assertEqual(fold.quotes(), ((7, (exact(2.5), 100), (exact(2.52), 80)),))
        self.assertEqual((fold.malformed, fold.nulls), ([], [2]))
        fold = LadderFold(("atb", "atl"))
        fold.change(1, {"rc": [{"id": 7, "atb": [[2.5, 100]], "atl": [[2.52, 80]]}]})
        fold.change(3, {"rc": [{"id": 7, "atb": None}]})
        self.assertEqual(fold.quotes(), ((7, (exact(2.5), 100), (exact(2.52), 80)),))
        self.assertEqual((fold.malformed, fold.nulls), ([], [3]))


class StreamCacheAgreementTests(unittest.TestCase):
    """LadderFold folds ladder deltas as betangel_research/betfair/book.py's MarketBookCache does (F54): its
    _apply_points for PRO's atb, its _apply_levels and _level_prefix for ADVANCED's batb."""

    def cache_ladder(self, field, steps):
        cache = MarketBookCache(key_type="historical", app_key_slot=None, source="recording")
        runner = {field: {}, "poisoned": False}
        for updates in steps:
            if field == "atb":
                cache._apply_points(runner, field, updates)
            else:
                cache._apply_levels(runner, field, updates)
        if field == "atb":
            return [[price, size] for price, size in sorted(runner[field].items(), reverse=True)]
        return cache._level_prefix(runner[field], descending=True)

    def fold_ladder(self, field, steps):
        fold = LadderFold(("atb", "atl") if field == "atb" else ("batb", "batl"))
        for pt, updates in enumerate(steps):
            fold.change(pt, {"rc": [{"id": 7, field: updates}]})
        self.assertEqual(fold.malformed, [])
        return [[float(price), float(size)] for price, size in fold.ladders(7)[0]]

    def assert_agree(self, field, steps):
        self.assertEqual(self.fold_ladder(field, steps), self.cache_ladder(field, steps), steps)

    def test_the_reviews_cases(self):
        self.assert_agree("batb", [[[0, 2.5, 100], [1, 2.48, 50], [2, 2.46, 30]], [[0, 0, 0]]])
        self.assert_agree("batb", [[[0, 2.5, 100], [1, 2.48, 50]], [[0, 2.4, 20]]])
        self.assert_agree("atb", [[[2.5, 100], [2.48, 50]], []])

    def test_seeded_random_sequences(self):
        rng = random.Random(20261031)
        prices = [price / 100 for price in LADDER_H[140:160]]
        for case in range(300):
            field = "atb" if case % 2 else "batb"
            steps = []
            for _ in range(rng.randint(1, 8)):
                updates = []
                for _ in range(rng.choice((0, 1, 1, 2, 3))):
                    size = rng.choice((0, 0, 2.0, 10.5, 50))
                    price = rng.choice(prices)
                    updates.append([price, size] if field == "atb" else [rng.randint(0, 3), price, size])
                steps.append(updates)
            with self.subTest(case=case):
                self.assert_agree(field, steps)


class SelectionTests(unittest.TestCase):
    ACTIVE = SIDS

    def code(self, book, active=None, malformed=False):
        code, selection = select(REG, self.ACTIVE if active is None else active, moment(book, malformed))
        return code or ("issued", selection.selection_id, float(selection.back[0]))

    def test_pilot_ones_selection_edge_cases(self):
        cases = {
            "unique favourite": (quotes(), ("issued", FAV, 2.5)),
            "tie": (quotes(field={**FIELD, R2: (2.5, 2.52)}), "favourite_tie"),
            "back 1.49 below the range": (quotes((1.49, 1.5)), "best_back_out_of_range"),
            "back 1.50 at the bottom": (quotes((1.5, 1.51)), ("issued", FAV, 1.5)),
            "back 4.00 at the top": (quotes((4.0, 4.1), field={**FIELD, R2: (4.5, 4.6)}), ("issued", FAV, 4.0)),
            "back 4.10 above the range": (quotes((4.1, 4.2), field={**FIELD, R2: (4.5, 4.6)}),
                                          "best_back_out_of_range"),
            "back off the ladder": (quotes((2.51, 2.54)), "best_back_off_ladder"),
            "size 9.99": (quotes((2.5, 2.52, 9.99)), "depth_below_min"),
            "size 10.00": (quotes((2.5, 2.52, 10.0)), ("issued", FAV, 2.5)),
            "lay 2 ticks above": (quotes((2.5, 2.54)), ("issued", FAV, 2.5)),
            "lay 3 ticks above": (quotes((2.5, 2.56)), "spread_too_wide"),
            "lay missing": (quotes((2.5, None)), "best_lay_missing"),
            "book crossed": (quotes((2.5, 2.5)), "book_crossed"),
            "lay off the ladder": (quotes((2.5, 2.53)), "best_lay_off_ladder"),
            "nothing priced": ({sid: (None, 3.0) for sid in SIDS}, "no_priced_active_runner"),
        }
        for name, (book, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.code(book), expected)

    def test_only_active_runners_compete_and_a_malformed_ladder_refuses(self):
        book = quotes(field={**FIELD, R2: (2.0, 2.02)})
        self.assertEqual(self.code(book), ("issued", R2, 2.0))
        self.assertEqual(self.code(book, active=(FAV, R3, R4, R5, R6)), ("issued", FAV, 2.5))
        self.assertEqual(self.code(quotes(), malformed=True), "ladder_malformed")


class CellAndShareTests(unittest.TestCase):
    def test_the_cells_at_the_boundaries(self):
        for move, cell in ((0.05, "STEAM"), (0.0500001, "STEAM"), (0.0499999, "FLAT"), (0.0, "FLAT"),
                           (-0.0499999, "FLAT"), (-0.05, "DRIFT"), (-0.2, "DRIFT")):
            with self.subTest(move=move):
                self.assertEqual(late_move_cell(REG, move), cell)

    def test_the_field_normalised_midpoint_share(self):
        book = quotes()
        q, reason = field_share(REG, moment(book, active=SIDS), FAV)
        inverse = {sid: 2 / (exact(back) + exact(lay)) for sid, (back, lay, *_) in book.items()}
        self.assertIsNone(reason)
        self.assertEqual(q, inverse[FAV] / sum(inverse.values()))
        for name, (changed, sid, active, expected) in {
            "other runner one-sided": ({**book, R6: (15.0, None)}, FAV, SIDS, "other_runner_not_two_sided"),
            "runner one-sided": ({**book, FAV: (None, 2.52)}, FAV, SIDS, "runner_not_two_sided"),
            "runner crossed": ({**book, FAV: (2.52, 2.52)}, FAV, SIDS, "runner_not_two_sided"),
            "spread 3 ticks": ({**book, FAV: (2.5, 2.56)}, FAV, SIDS, None),
            "spread 4 ticks": ({**book, FAV: (2.5, 2.58)}, FAV, SIDS, "runner_spread_too_wide"),
            "off the ladder": ({**book, FAV: (2.51, 2.56)}, FAV, SIDS, "runner_off_ladder"),
            "not active": (book, FAV, SIDS[1:], "runner_not_active"),
        }.items():
            with self.subTest(case=name):
                self.assertEqual(field_share(REG, moment(changed, active=active), sid)[1], expected)
        self.assertEqual(field_share(REG, moment(book), FAV), (None, "no_view_at_time"))
        self.assertEqual(field_share(REG, moment(book, malformed=True, active=SIDS), FAV), (None, "ladder_malformed"))

    def test_the_window_rule(self):
        def replay(statuses, removals=(), unreadable_from=None):
            fold = SimpleNamespace(removals=lambda: list(removals), fields={"status": statuses})
            return SimpleNamespace(unreadable_from=unreadable_from, market=fold)

        statuses = [[0, "OPEN"], [70, "SUSPENDED"], [71, "OPEN"]]
        removed = [{"id": 1, "pt": 50, "status": "REMOVED"}]
        self.assertEqual(window_problem(replay(statuses, removed), 10, 100), "removal_in_window")
        self.assertEqual(window_problem(replay(statuses, removed), 50, 100), "suspension_in_window")  # (50, 100]
        self.assertIsNone(window_problem(replay(statuses, removed), 71, 100))
        self.assertEqual(window_problem(replay(statuses, unreadable_from=100), 71, 100), "window_unknown")
        self.assertIsNone(window_problem(replay(statuses, unreadable_from=101), 71, 100))
        self.assertIsNone(window_problem(replay([[20, "OPEN"]]), 10, 100))              # not yet published at start
        self.assertEqual(window_problem(replay([[0, "SUSPENDED"], [20, "OPEN"]]), 10, 100), "suspension_in_window")
        self.assertEqual(window_problem(replay([[0, "OPEN"], [60, None]]), 10, 100), "suspension_in_window")
        # PR #50 Codex r4432420328: a status reset to null before the window, and OPEN again inside it, is not an
        # unpublished market; the null at the window's start is a status other than OPEN.
        reset = [[0, "OPEN"], [40, None], [80, "OPEN"]]
        self.assertEqual(window_problem(replay(reset), 50, 100), "suspension_in_window")
        self.assertIsNone(window_problem(replay([[60, "OPEN"]]), 50, 100))  # first published inside: not excluded here


class StatisticsTests(unittest.TestCase):
    def test_sd_and_nearest_rank(self):
        self.assertAlmostEqual(sample_sd([1.0, 2.0, 3.0, 4.0]), (5 / 3) ** 0.5)
        self.assertIsNone(sample_sd([1.0]))
        quantiles = nearest_rank([5, 1, 3, 2, 4], [Fraction(1, 5), Fraction(1, 2), Fraction(1)])
        self.assertEqual(quantiles, {Fraction(1, 5): 1, Fraction(1, 2): 3, Fraction(1): 5})

    def test_cr1_is_pilot_ones(self):
        rows = [("d1", 0.01), ("d1", -0.02), ("d2", 0.03), ("d3", 0.0), ("d3", 0.05), ("d4", -0.01)]
        ours = cr1_mean(rows, Fraction(95, 100), 2)
        theirs = pilot.cr1([(day, Fraction(str(x))) for day, x in rows], min_clusters=2, alpha=Fraction(1, 40))
        self.assertAlmostEqual(ours["mean"], float(theirs["m"]))
        self.assertAlmostEqual(ours["se"] ** 2, float(theirs["se2"]))
        self.assertEqual(ours["t_critical"], float(theirs["t"]))
        self.assertEqual((ours["bets"], ours["race_dates"], ours["df"]), (6, 4, 3))
        self.assertEqual(cr1_mean(rows, Fraction(95, 100), 15)["reason"], "too_few_clusters")
        self.assertEqual(student_t_quantile(0.975, 5), pilot.student_t_quantile(0.975, 5))

    def test_the_cr1_contrast_by_hand(self):
        first, second = [("d1", 1.0), ("d2", 3.0)], [("d1", 0.0), ("d2", 1.0)]
        result = cr1_contrast(first, second, 2, 2)
        self.assertEqual((result["estimate"], result["se"], result["t"], result["df"]), (1.5, 0.5, 3.0, 1))
        self.assertAlmostEqual(result["p_two_sided"], 0.204833, places=6)   # Cauchy: 1 - 2 atan(3) / pi
        self.assertEqual(cr1_contrast(first, second[:1], 2, 2)["reason"], "underpowered")
        self.assertEqual(cr1_contrast(first, second, 2, 3)["reason"], "underpowered")

    def test_holm(self):
        self.assertEqual(holm({"a": 0.01, "b": 0.04}, Fraction(5, 100), 2), {"a": (0.02, True), "b": (0.04, True)})
        self.assertEqual(holm({"a": 0.03, "b": 0.04}, Fraction(5, 100), 2), {"a": (0.06, False), "b": (0.06, False)})
        self.assertEqual(holm({"a": 0.02, "b": None}, Fraction(5, 100), 2), {"a": (0.04, True), "b": (None, False)})
        self.assertEqual(holm({"a": 0.03, "b": None}, Fraction(5, 100), 2), {"a": (0.06, False), "b": (None, False)})

    def test_the_shuffled_m_control_is_the_exact_within_date_permutation_expectation(self):
        rows = [("d1", "STEAM", 0.10), ("d1", "FLAT", 0.00), ("d1", "DRIFT", -0.05),
                ("d2", "STEAM", 0.20), ("d2", "STEAM", 0.05), ("d2", "FLAT", 0.00),
                ("d3", "FLAT", 0.03), ("d3", "DRIFT", -0.02), ("d3", "STEAM", 0.07), ("d3", "FLAT", -0.01)]
        days = {}
        for day, cell, x in rows:
            days.setdefault(day, []).append((cell, x))
        # Every within-date permutation of the cells, each equally likely, by brute force (3! * 3! * 4! = 864).
        estimates = []
        for orders in itertools.product(*(itertools.permutations(range(len(items))) for items in days.values())):
            permuted = [(day, items[order[k]][0], items[k][1]) for (day, items), order in zip(days.items(), orders)
                        for k in range(len(items))]
            steam = [x for _, cell, x in permuted if cell == "STEAM"]
            flat = [x for _, cell, x in permuted if cell == "FLAT"]
            estimates.append(sum(steam) / len(steam) - sum(flat) / len(flat))
        result = permuted_contrast(rows, "STEAM", "FLAT", 1, 1)
        self.assertEqual(len(estimates), 864)
        self.assertAlmostEqual(result["estimate"], sum(estimates) / len(estimates), places=12)
        # By hand: sum over dates of (n_d,S / 4 - n_d,F / 4) * mean_d.
        means = {day: sum(x for _, x in items) / len(items) for day, items in days.items()}
        self.assertAlmostEqual(result["estimate"], (1 / 4 - 1 / 4) * means["d1"] + (2 / 4 - 1 / 4) * means["d2"]
                               + (1 / 4 - 2 / 4) * means["d3"], places=12)
        self.assertEqual((result["bets"], result["race_dates"], result["df"]), ([4, 4], [3, 3], 2))
        self.assertEqual(permuted_contrast(rows, "STEAM", "FLAT", 5, 1)["reason"], "underpowered")
        self.assertEqual(permuted_contrast(rows, "STEAM", "FLAT", 1, 4)["reason"], "underpowered")

    def test_with_one_cell_per_date_nothing_moves_and_it_is_the_cr1_contrast(self):
        rows = [("d1", "STEAM", 0.1), ("d1", "STEAM", 0.3), ("d2", "FLAT", 0.0), ("d3", "STEAM", 0.2),
                ("d4", "FLAT", -0.1), ("d4", "FLAT", 0.05), ("d5", "DRIFT", 0.4)]
        ours = permuted_contrast(rows, "STEAM", "FLAT", 1, 1)
        theirs = cr1_contrast([(d, x) for d, c, x in rows if c == "STEAM"], [(d, x) for d, c, x in rows if c == "FLAT"],
                              1, 1)
        for key in ("estimate", "se", "df", "t", "p_two_sided", "bets", "race_dates"):
            self.assertAlmostEqual(ours[key], theirs[key], places=12) if isinstance(ours[key], float) \
                else self.assertEqual(ours[key], theirs[key], key)
        # A perfectly balanced input has an exact expectation of 0 and no SE.
        balanced = [(day, cell, x) for day in ("d1", "d2") for cell, x in (("STEAM", 0.1), ("FLAT", 0.2))]
        self.assertEqual(permuted_contrast(balanced, "STEAM", "FLAT", 1, 1)["estimate"], 0.0)
        self.assertEqual(permuted_contrast(balanced, "STEAM", "FLAT", 1, 1)["reason"], "se_undefined")


def tested(estimate, p=0.001):
    """A contrast result that was tested (cr1_contrast's keys)."""
    return {"estimate": estimate, "se": 0.002, "df": 40, "t": 5.0, "p_two_sided": p, "bets": [300, 300],
            "race_dates": [40, 40], "reason": None}


UNDERPOWERED = {"estimate": None, "se": None, "df": None, "t": None, "p_two_sided": None, "bets": [5, 300],
                "race_dates": [3, 40], "reason": "underpowered"}


class KillAndFlagTests(unittest.TestCase):
    """movement.kill_and_flags: the registered kill and discovery rules on hand-built contrasts."""

    def decide(self, main=None, decisions=None, controls=None, levels=None):
        main = main or {"STEAM-FLAT": tested(0.02), "DRIFT-FLAT": tested(0.012)}
        decisions = decisions or {"STEAM-FLAT": (0.002, True), "DRIFT-FLAT": (0.002, True)}
        base = {control: ({name: tested(0.001, p=0.6) for name, _, _ in CONTRASTS}, dict(main)) for control in CONTROLS}
        for control, (own, same) in (controls or {}).items():
            base[control] = ({**base[control][0], **own}, {**base[control][1], **same})
        levels = {"STEAM": 0.006, "FLAT": -0.014, "DRIFT": 0.006, **(levels or {})}
        return kill_and_flags(REG, main, decisions, base, levels)

    def flag(self, **changes):
        return self.decide(**changes)[1]["STEAM"]

    def test_a_flag_when_every_check_holds(self):
        kill, flags = self.decide()
        self.assertEqual(flags["STEAM"], {"cell_nclv_at_least_min": True, "contrast_positive": True,
                                          "contrast_rejected_under_holm": True, "no_control_blocks": True,
                                          "tradable_flag": True})
        self.assertTrue(flags["DRIFT"]["tradable_flag"])
        self.assertEqual(kill["STEAM-FLAT"]["random_runner"], {"control_estimate": 0.001, "control_p_two_sided": 0.6,
                                                               "main_estimate_same_bets": 0.02, "blocks": False,
                                                               "why": None})

    def test_each_check_failing_alone_clears_the_flag(self):
        main = {"STEAM-FLAT": tested(0.02), "DRIFT-FLAT": tested(0.012)}
        cases = {
            "mean below 0.005": ({"levels": {"STEAM": 0.0049}}, "cell_nclv_at_least_min"),
            "mean not reported": ({"levels": {"STEAM": None}}, "cell_nclv_at_least_min"),
            "not rejected under Holm": ({"decisions": {"STEAM-FLAT": (0.08, False), "DRIFT-FLAT": (0.002, True)}},
                                        "contrast_rejected_under_holm"),
            "contrast negative": ({"main": {**main, "STEAM-FLAT": tested(-0.02)},
                                   "controls": {c: ({}, {"STEAM-FLAT": tested(-0.02)}) for c in CONTROLS}},
                                  "contrast_positive"),
            "a control at half the main effect and significant": (
                {"controls": {"random_runner": ({"STEAM-FLAT": tested(0.01, p=0.01)}, {})}}, "no_control_blocks"),
            "a control of the opposite sign as large": (
                {"controls": {"earlier_window": ({"STEAM-FLAT": tested(-0.015, p=0.001)}, {})}}, "no_control_blocks"),
            "a control underpowered": ({"controls": {"earlier_window": ({"STEAM-FLAT": UNDERPOWERED}, {})}},
                                       "no_control_blocks"),
            "the main contrast over a control's bets underpowered": (
                {"controls": {"random_runner": ({}, {"STEAM-FLAT": UNDERPOWERED})}}, "no_control_blocks"),
            "a large control whose noise cannot be judged": (
                {"controls": {"shuffled_m": ({"STEAM-FLAT": dict(tested(0.015), p_two_sided=None, se=0.0, df=None,
                                                                   t=None, reason="se_undefined")}, {})}},
                "no_control_blocks"),
        }
        for name, (changes, failing) in cases.items():
            with self.subTest(case=name):
                flag = self.flag(**changes)
                self.assertFalse(flag["tradable_flag"])
                self.assertEqual([key for key, value in flag.items() if not value], [failing, "tradable_flag"])

    def test_why_a_control_blocks(self):
        for own, same, why in ((UNDERPOWERED, {}, "control_not_computed"),
                               (tested(0.001), {"STEAM-FLAT": UNDERPOWERED}, "main_not_computed_on_same_bets"),
                               (tested(0.01, p=0.01), {}, "control_not_below")):
            with self.subTest(why=why):
                kill, _ = self.decide(controls={"earlier_window": ({"STEAM-FLAT": own}, same)})
                self.assertEqual((kill["STEAM-FLAT"]["earlier_window"]["blocks"],
                                  kill["STEAM-FLAT"]["earlier_window"]["why"]), (True, why))

    def test_a_control_that_does_not_block(self):
        cases = {
            "as large but within its noise": tested(0.015, p=0.2),
            "significant but under half": tested(0.0099, p=0.0001),
            "p exactly alpha": tested(0.012, p=0.05),
            "small with no SE": dict(tested(0.0), p_two_sided=None, se=0.0, reason="se_undefined"),
        }
        for name, own in cases.items():
            with self.subTest(case=name):
                kill, flags = self.decide(controls={"random_runner": ({"STEAM-FLAT": own}, {})})
                self.assertFalse(kill["STEAM-FLAT"]["random_runner"]["blocks"])
                self.assertTrue(flags["STEAM"]["tradable_flag"])

    def test_a_control_is_judged_against_the_main_contrast_over_its_own_bets(self):
        # 0.015 is at least half the overall 0.02 but under half the 0.04 measured over the control's own bets.
        controls = {"earlier_window": ({"STEAM-FLAT": tested(0.015, p=0.001)}, {"STEAM-FLAT": tested(0.04)})}
        kill, flags = self.decide(controls=controls)
        self.assertEqual(kill["STEAM-FLAT"]["earlier_window"]["main_estimate_same_bets"], 0.04)
        self.assertFalse(kill["STEAM-FLAT"]["earlier_window"]["blocks"])
        self.assertTrue(flags["STEAM"]["tradable_flag"])
        controls = {"earlier_window": ({"STEAM-FLAT": tested(0.015, p=0.001)}, {"STEAM-FLAT": tested(0.02)})}
        self.assertFalse(self.flag(controls=controls)["tradable_flag"])

    def test_cells_are_flagged_independently(self):
        _, flags = self.decide(levels={"DRIFT": 0.001})
        self.assertEqual((flags["STEAM"]["tradable_flag"], flags["DRIFT"]["tradable_flag"]), (True, False))


# ---------------------------------------------------------------------------------------------------------------

class MarketCodeTests(StudyCase):
    def test_a_standard_market_is_issued_and_eligible_at_every_time(self):
        tally = self.one(market())
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "eligible"))
        self.assertEqual(tally.replayed, 1)
        value = tally.bets[-300][0]
        self.assertEqual((value.race_date, value.price_band, value.field_band), ("2015-06-01", "B3", "F1"))
        self.assertAlmostEqual(value.nclv, float(nclv(exact(2.5), exact(2.5), Fraction(2, 100))))
        self.assertEqual(value.ticks, 0.0)

    def test_history_gates_before_pilot_ones_predicates(self):
        later = OFF + timedelta(days=335)                                         # 2016-05: not a registered month
        cases = {
            "plan": ([(path(M, plan="BASIC"), market(plan="BASIC"))], dict.fromkeys(OFFSETS, "plan_not_registered")),
            "month": ([(path(M, when=later), market(off=later))], dict.fromkeys(OFFSETS, "month_not_registered")),
            "no view": ([(path(M), market(opening_minutes=-1))], dict.fromkeys(OFFSETS, "no_view")),
            "anchor after t": ([(path(M), market(opening_minutes=20))],
                               {-1800: "anchor_not_known_by_decision", -600: "eligible", -300: "eligible",
                                -120: "eligible"}),
        }
        for name, (members, expected) in cases.items():
            with self.subTest(case=name):
                tally = self.decide(members)
                self.assertEqual(outcome_of(tally), expected)
                self.assertEqual(tally.replayed, 0 if name in ("plan", "month", "no view") else 1)

    def test_a_market_closed_before_t_has_no_view_after_it(self):
        closed = md(M, at(OFF, 8), definition(OFF, [runner(sid, "REMOVED") for sid in SIDS], status="CLOSED"))
        messages = [message for message in market() if isinstance(message, bytes) or message["pt"] < ms(at(OFF, 8))]
        tally = self.one(messages + [closed])
        self.assertEqual(outcome_of(tally), {-1800: "void_own_runner_removed", -600: "void_own_runner_removed",
                                             -300: "view_ended_before_decision", -120: "view_ended_before_decision"})

    def test_a_revised_market_time_is_skipped_from_the_revision_on(self):
        revised = md(M, at(OFF, 40), definition(OFF + timedelta(minutes=10), [runner(sid) for sid in SIDS]))
        tally = self.one(market(extra=[revised]))
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "schedule_revised_before_decision"))
        revised_late = md(M, at(OFF, 4), definition(OFF + timedelta(minutes=10), [runner(sid) for sid in SIDS]))
        tally = self.one(market(extra=[revised_late]))
        self.assertEqual(outcome_of(tally)[-300], "eligible")
        self.assertEqual(outcome_of(tally)[-120], "schedule_revised_before_decision")

    def test_pilot_ones_market_predicates(self):
        cases = {
            "IE": ({"countryCode": "IE"}, "eligible"),
            "FR": ({"countryCode": "FR"}, "country_not_allowed"),
            "event type": ({"eventTypeId": "4339"}, "event_type_not_allowed"),
            "market type": ({"marketType": "PLACE"}, "market_type_not_allowed"),
            "betting type": ({"bettingType": "ASIAN_HANDICAP_DOUBLE_LINE"}, "betting_type_not_odds"),
            "winners": ({"numberOfWinners": 2}, "number_of_winners_not_1"),
        }
        for name, (fields, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(outcome_of(self.one(market(**fields))), dict.fromkeys(OFFSETS, expected))
        four = SIDS[:4]
        tally = self.one(market(runners=four))
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "too_few_active_runners"))
        self.assertEqual(tally.replayed, 0)

    def test_a_malformed_ladder_and_a_tie_from_the_stream(self):
        bad = mcm(at(OFF, 20), [{"id": M, "rc": [{"id": FAV, "atb": [[2.5]]}]}])
        self.assertEqual(outcome_of(self.one(market(extra=[bad]))),
                         {-1800: "eligible", -600: "ladder_malformed", -300: "ladder_malformed",
                          -120: "ladder_malformed"})
        tie = {minutes: quotes(field={**FIELD, R2: (2.5, 2.54)}) for minutes in MINUTES}
        self.assertEqual(outcome_of(self.one(market(books=tie))), dict.fromkeys(OFFSETS, "favourite_tie"))


class LadderTierTests(StudyCase):
    def test_pro_and_advanced_give_the_same_decisions(self):
        books = {58: quotes((2.8, 2.82)), 32: quotes((2.8, 2.82)), 12: quotes((2.6, 2.62)), 7: quotes((2.5, 2.52)),
                 3: quotes((2.44, 2.46))}
        found = {}
        for plan in ("PRO", "ADVANCED"):
            tally = self.one(market(plan=plan, books=books), plan=plan)
            found[plan] = (outcome_of(tally), {offset: [bet.nclv for bet in tally.bets[offset]] for offset in OFFSETS},
                           [(bet.cell, bet.move) for bet in tally.late])
        self.assertEqual(found["PRO"], found["ADVANCED"])
        self.assertEqual(found["PRO"][2][0][0], "STEAM")

    def test_an_emptied_favourite_ladder_is_not_carried_forward_through_the_replay(self):
        # The favourite's back ladder is emptied at off -8 min and nothing is sent for it after: from then on R2
        # (4.00 / 4.10) is the favourite. The first fold kept FAV's stale 2.50 (PRO) or 2.48 (ADVANCED).
        books = {58: quotes(), 32: quotes(), 12: quotes()}
        deep = mcm(at(OFF, 9), [{"id": M, "rc": [{"id": FAV, "batb": [[0, 2.5, 50.0], [1, 2.48, 40.0],
                                                                      [2, 2.46, 30.0]]}]}])
        emptied = {"PRO": [mcm(at(OFF, 8), [{"id": M, "rc": [{"id": FAV, "atb": []}]}])],
                   "ADVANCED": [deep, mcm(at(OFF, 8), [{"id": M, "rc": [{"id": FAV, "batb": [[0, 0, 0]]}]}])]}
        for plan, extra in emptied.items():
            with self.subTest(plan=plan):
                tally = self.one(market(plan=plan, books=books, extra=extra), plan=plan)
                self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "eligible"))
                self.assertEqual([bet.price_band for bet in tally.bets[-600]], ["B3"])           # FAV at 2.50
                for offset in (-300, -120):
                    self.assertEqual([(bet.price_band, bet.nclv) for bet in tally.bets[offset]],
                                     [("B4", float(nclv(exact(4.0), exact(BSPS[R2]), Fraction(2, 100))))])

    def test_an_image_with_a_definition_resets_ladders_it_does_not_carry(self):
        image = mcm(at(OFF, 6), [{"id": M, "img": True,
                                  "marketDefinition": definition(OFF, [runner(sid) for sid in SIDS]),
                                  "rc": [{"id": sid, "atb": [[back, 50.0]], "atl": [[lay, 75.0]]}
                                         for sid, (back, lay) in quotes().items() if sid != R6]}])
        books = {minutes: quotes() for minutes in MINUTES if minutes != 3}
        tally = self.one(market(books=books, extra=[image]))
        self.assertEqual(outcome_of(tally)[-300], "eligible")
        self.assertEqual(tally.late_undefined["other_runner_not_two_sided"], 1)       # R6 lost its book at T-5


class MeasurementTests(StudyCase):
    def test_a_market_without_bsp_is_issued_and_missing_bsp(self):
        tally = self.one(market(bspMarket=False, bsps={}, reconciled=False))
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "missing_bsp"))
        tally = self.one(market(reconciled=False))
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "missing_bsp"))

    def test_removals_after_the_decision(self):
        def removed(sid):
            listed = [runner(other, "REMOVED" if other == sid else "ACTIVE") for other in SIDS]
            return md(M, at(OFF, 1), definition(OFF, listed))

        statuses = {sid: "WINNER" if sid == R2 else "LOSER" for sid in SIDS}
        own = self.one(market(extra=[removed(FAV)], statuses={**statuses, FAV: "REMOVED"}))
        self.assertEqual(outcome_of(own), dict.fromkeys(OFFSETS, "void_own_runner_removed"))
        other = self.one(market(extra=[removed(R6)], statuses={**statuses, FAV: "WINNER", R2: "LOSER",
                                                                R6: "REMOVED"}))
        self.assertEqual(outcome_of(other), dict.fromkeys(OFFSETS, "post_decision_removal"))

    def test_a_member_unreadable_after_the_off_leaves_the_outcome_unknown(self):
        tally = self.one(market(bad_tail=True))
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "outcome_unknown"))

    def test_a_result_revised_after_close_is_counted_apart(self):
        # Pilot 1 freezes BSP at the first read showing CLOSED; a later correction makes these final values not it.
        final = [runner(sid, "WINNER" if sid == FAV else "LOSER", 2.44 if sid == FAV else BSPS[sid]) for sid in SIDS]
        settled = OFF + timedelta(minutes=8)
        revised = md(M, OFF + timedelta(minutes=30), definition(OFF, final, status="CLOSED", in_play=True,
                                                                bspReconciled=True, settledTime=iso(settled)))
        tally = self.one(market(tail=[revised]))
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "result_revised_after_close"))
        self.assertEqual(tally.bets[-300], [])


class DiagnosticTests(StudyCase):
    def diagnostics(self, tally):
        return {offset: {key: count for key, count in tally.diagnostics[offset].items() if count}
                for offset in OFFSETS}

    def test_suspended_and_in_play_books_are_issued_and_counted(self):
        suspended = md(M, at(OFF, 6), definition(OFF, [runner(sid) for sid in SIDS], status="SUSPENDED"))
        tally = self.one(market(extra=[suspended]))
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "eligible"))
        self.assertEqual(self.diagnostics(tally), {-1800: {}, -600: {}, -300: {"issued_status_not_open": 1},
                                                   -120: {"issued_status_not_open": 1}})
        in_play = md(M, at(OFF, 2.5), definition(OFF, [runner(sid) for sid in SIDS], in_play=True))
        tally = self.one(market(extra=[in_play]))
        self.assertEqual(self.diagnostics(tally), {-1800: {}, -600: {}, -300: {}, -120: {"issued_in_play": 1}})

    def test_a_favourite_below_the_minimum_stake_is_counted(self):
        books = {minutes: quotes((2.5, 2.52, 1.5) if minutes == 7 else (2.5, 2.52)) for minutes in MINUTES}
        tally = self.one(market(books=books))
        self.assertEqual(outcome_of(tally)[-300], "depth_below_min")
        self.assertEqual(self.diagnostics(tally)[-300], {"favourite_best_back_below_min_stake": 1})
        self.assertEqual(self.diagnostics(tally)[-120], {})

    def test_a_null_ladder_value_changes_nothing_and_is_counted(self):
        null = mcm(at(OFF, 6), [{"id": M, "rc": [{"id": FAV, "atb": None}]}])
        tally = self.one(market(extra=[null]))
        self.assertEqual(outcome_of(tally), dict.fromkeys(OFFSETS, "eligible"))
        self.assertEqual(tally.null_ladder_markets, 1)


class LateMoveTests(StudyCase):
    def late(self, **changes):
        tally = self.one(market(**changes))
        return tally, {key: count for key, count in tally.late_exclusions.items() if count}

    def test_the_cells_from_the_stream(self):
        for early, cell in (((2.8, 2.82), "STEAM"), ((2.5, 2.52), "FLAT"), ((2.2, 2.22), "DRIFT")):
            with self.subTest(cell=cell):
                books = {58: quotes(early), 32: quotes(early), 12: quotes(), 7: quotes(), 3: quotes()}
                tally, excluded = self.late(books=books)
                self.assertEqual(excluded, {})
                self.assertEqual([bet.cell for bet in tally.late], [cell])
                self.assertEqual(tally.late[0].placebo, "FLAT")             # no move from off -55 to off -30 min
                self.assertIsNotNone(tally.late[0].random)

    def test_each_exclusion(self):
        removal = md(M, at(OFF, 20), definition(OFF, [runner(sid, "REMOVED" if sid == R6 else "ACTIVE")
                                                      for sid in SIDS]))
        statuses = {sid: "REMOVED" if sid == R6 else "WINNER" if sid == FAV else "LOSER" for sid in SIDS}
        suspended = [md(M, at(OFF, 15), definition(OFF, [runner(sid) for sid in SIDS], status="SUSPENDED")),
                     md(M, at(OFF, 14), definition(OFF, [runner(sid) for sid in SIDS]))]
        wide = {minutes: quotes((2.5, 2.58) if minutes == 32 else (2.5, 2.52)) for minutes in MINUTES}
        one_sided = {minutes: quotes(field={**FIELD, R6: (15.0, None)} if minutes == 32 else FIELD)
                     for minutes in MINUTES}
        for name, changes, exclusion, reason in (
                ("removal", {"extra": [removal], "statuses": statuses}, "removal_in_window", None),
                ("suspension", {"extra": suspended}, "suspension_in_window", None),
                ("wide at off -30", {"books": wide}, "cell_undefined", "runner_spread_too_wide"),
                ("one-sided at off -30", {"books": one_sided}, "cell_undefined", "other_runner_not_two_sided")):
            with self.subTest(case=name):
                tally, excluded = self.late(**changes)
                self.assertEqual(outcome_of(tally)[-300], "eligible")
                self.assertEqual(excluded, {exclusion: 1})
                self.assertEqual(tally.late, [])
                if reason:
                    self.assertEqual({key: count for key, count in tally.late_undefined.items() if count}, {reason: 1})

    def test_the_window_opens_after_off_minus_30_and_a_later_removal_is_a_post_decision_one(self):
        def removal(when):
            return md(M, when, definition(OFF, [runner(sid, "REMOVED" if sid == R6 else "ACTIVE") for sid in SIDS]))

        statuses = {sid: "REMOVED" if sid == R6 else "WINNER" if sid == FAV else "LOSER" for sid in SIDS}
        tally, excluded = self.late(extra=[removal(at(OFF, 5))], statuses=statuses)        # at t: in the view
        self.assertEqual((outcome_of(tally)[-300], excluded), ("eligible", {"removal_in_window": 1}))
        tally, excluded = self.late(extra=[removal(at(OFF, 30))], statuses=statuses)       # at the window's start
        self.assertEqual((outcome_of(tally)[-300], excluded, len(tally.late)), ("eligible", {}, 1))
        tally = self.one(market(extra=[removal(at(OFF, 4))], statuses=statuses))
        self.assertEqual(outcome_of(tally)[-300], "post_decision_removal")      # after t: never measured
        self.assertEqual(tally.late, [])

    def test_a_member_unreadable_in_the_window_never_reaches_the_late_move(self):
        tally = self.decide([(path(M), [message for message in market() if isinstance(message, bytes)
                                        or message["pt"] <= ms(at(OFF, 3))] + [b"not json\n"])])
        self.assertEqual(outcome_of(tally)[-300], "outcome_unknown")             # the outcome needs the whole file
        self.assertEqual(outcome_of(tally)[-120], "member_unreadable_before_decision")
        self.assertEqual(tally.late, [])


# ---------------------------------------------------------------------------------------------------------------

def study_members(plan="PRO"):
    """Three race dates, each with a STEAM, a FLAT and a DRIFT market and one four-runner market."""
    members, early = [], {"STEAM": (2.8, 2.82), "FLAT": (2.5, 2.52), "DRIFT": (2.2, 2.22)}
    bsps = {"STEAM": (2.4, 2.36, 2.44), "FLAT": (2.5, 2.52, 2.46), "DRIFT": (2.6, 2.62, 2.58)}
    for day in range(3):
        off = OFF + timedelta(days=day)
        for index, cell in enumerate(early):
            market_id = f"1.1700{day}{index}001"
            books = {58: quotes(early[cell]), 32: quotes(early[cell]), 12: quotes(), 7: quotes(), 3: quotes()}
            members.append((path(market_id, when=off, plan=plan),
                            market(market_id, off, plan, books=books, bsps={**BSPS, FAV: bsps[cell][day]})))
        market_id = f"1.1700{day}9001"
        members.append((path(market_id, when=off, plan=plan), market(market_id, off, plan, runners=SIDS[:4])))
    return members


def small_registration(target):
    """The registration with thresholds small enough for synthetic data; (path, sha256)."""
    value = json.loads(REGISTRATION.read_bytes())
    value["registered_pass"].update(min_cell_bets=2, min_cell_race_dates=2)
    value["uncertainty"]["min_clusters"] = 2
    value["statistics"]["min_bets_for_statistics"] = 2
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    target.write_bytes(data)
    return target, hashlib.sha256(data).hexdigest()


class RunTests(fx.Workspace):
    def setUp(self):
        super().setUp()
        self.root, self.manifest, self.records = self.build(study_members())
        self.registration, self.sha = small_registration(self.tmp / "registration.json")

    def run_pass(self, name, out, **kwargs):
        arguments = dict(registration_path=self.registration, clock=lambda: "2026-10-31T00:00:00.000000Z")
        arguments.update(kwargs)
        return run_study(name, arguments.pop("sha", self.sha), arguments.pop("root", self.root), out, COMMIT,
                         **arguments)

    def outputs(self, out):
        return {name: (out / name).read_bytes() for name in ("summary.json", "summary.md", "receipt.json")}

    def test_blind_then_registered(self):
        blind = self.run_pass("blind", self.tmp / "blind")
        self.assertEqual(blind["status"], "completed", blind)
        self.assertEqual((blind["records"], blind["replayed_markets"], blind["late_move_population"]), (12, 9, 9))
        self.assertEqual(blind["issued"], dict.fromkeys(map(str, OFFSETS), 9))
        files = self.outputs(self.tmp / "blind")
        value = json.loads(files["summary.json"])
        for offset, tables in value["decision_times"].items():
            self.assertEqual(sum(tables["codes"].values()), 12, offset)
            self.assertEqual(tables["codes"]["too_few_active_runners"], 3)
            self.assertEqual(sum(tables["classes"].values()), tables["codes"]["issued"])
        self.assertEqual({cell: item["bets"] for cell, item in value["late_move"]["cells"].items()},
                         {"STEAM": 3, "FLAT": 3, "DRIFT": 3})
        self.assertEqual(value["late_move"]["cells_by_price_band"],
                         {cell: {"B1": 0, "B2": 0, "B3": 3, "B4": 0} for cell in ("STEAM", "FLAT", "DRIFT")})
        self.assertEqual(value["coverage_by_month"]["2015-06"],
                         {"records": 12, "issued": dict.fromkeys(map(str, OFFSETS), 9),
                          "eligible": dict.fromkeys(map(str, OFFSETS), 9)})
        self.assertEqual({month: row["records"] for month, row in value["coverage_by_month"].items() if row["records"]},
                         {"2015-06": 12})
        self.assertEqual(len(value["coverage_by_month"]), 14)                 # 12 months, outside, no anchor
        self.assertEqual([key for key in walk_keys(value) if "mean" in key.lower()], [])
        self.assertNotIn("mean", files["summary.md"].decode().lower())
        self.assertEqual({"contrasts", "kill_rule", "flags", "nclv_mean", "quantiles"} & set(walk_keys(value)), set())
        # Tick quantiles carry no location in the blind pass: each is less its group's median.
        for tables in value["decision_times"].values():
            for group in [tables["ticks_p_minus_bsp"]["all"], *tables["ticks_p_minus_bsp"]["price_band"].values()]:
                if group["quantiles_minus_median"] is not None:
                    self.assertEqual(group["quantiles_minus_median"]["0.5"], 0.0)
        ticks_300 = value["decision_times"]["-300"]["ticks_p_minus_bsp"]["all"]["quantiles_minus_median"]
        self.assertGreater(ticks_300["0.95"], 0)                       # the shape is kept
        self.assertLess(ticks_300["0.05"], 0)
        receipt = json.loads(files["receipt.json"])
        self.assertEqual((receipt["pass"], receipt["registration_sha256"], receipt["code_commit"],
                          receipt["blind_receipt_sha256"]), ("blind", self.sha, COMMIT, None))
        self.assertEqual(receipt["outputs"]["summary.json"]["sha256"],
                         hashlib.sha256(files["summary.json"]).hexdigest())

        refused = self.run_pass("registered", self.tmp / "registered")
        self.assertEqual(refused, {"status": "refused", "problems": ["blind_receipt:missing"]})
        registered = self.run_pass("registered", self.tmp / "registered", blind_receipt=self.tmp / "blind/receipt.json")
        self.assertEqual(registered["status"], "completed", registered)
        files = self.outputs(self.tmp / "registered")
        value = json.loads(files["summary.json"])
        receipt = json.loads(files["receipt.json"])
        self.assertEqual(receipt["blind_receipt_sha256"],
                         hashlib.sha256((self.tmp / "blind/receipt.json").read_bytes()).hexdigest())
        late = value["late_move"]
        nclvs = {cell: [float(nclv(exact(2.5), exact(bsp), Fraction(2, 100))) for bsp in bsps]
                 for cell, bsps in {"STEAM": (2.4, 2.36, 2.44), "FLAT": (2.5, 2.52, 2.46),
                                    "DRIFT": (2.6, 2.62, 2.58)}.items()}
        for cell, values in nclvs.items():
            self.assertAlmostEqual(late["cells"][cell]["nclv_mean"]["mean"], sum(values) / 3, places=5)
        self.assertAlmostEqual(late["contrasts"]["STEAM-FLAT"]["estimate"],
                               sum(nclvs["STEAM"]) / 3 - sum(nclvs["FLAT"]) / 3, places=5)
        self.assertEqual(set(value["flags"]), {"STEAM", "DRIFT"})
        # The earlier window never moved, so its STEAM and DRIFT cells are empty: that control cannot be computed and
        # blocks both flags.
        self.assertEqual(value["controls"]["earlier_window"]["cells"]["FLAT"]["bets"], 9)
        self.assertEqual(value["controls"]["earlier_window"]["contrasts"]["STEAM-FLAT"]["reason"], "underpowered")
        self.assertEqual({key: value["kill_rule"]["STEAM-FLAT"]["earlier_window"][key] for key in ("blocks", "why")},
                         {"blocks": True, "why": "control_not_computed"})
        # Each race date holds one bet of each cell, so every within-date permutation keeps the contrast's expected
        # value at exactly 0: the shuffled control explains nothing and does not block.
        shuffled = value["controls"]["shuffled_m"]["contrasts"]["STEAM-FLAT"]
        self.assertEqual((shuffled["estimate"], shuffled["reason"]), (0.0, "se_undefined"))
        self.assertFalse(value["kill_rule"]["STEAM-FLAT"]["shuffled_m"]["blocks"])
        self.assertEqual(value["controls"]["shuffled_m"]["main_contrasts_same_bets"]["STEAM-FLAT"]["estimate"],
                         late["contrasts"]["STEAM-FLAT"]["estimate"])
        self.assertEqual((value["flags"]["STEAM"]["tradable_flag"], value["flags"]["DRIFT"]["tradable_flag"]),
                         (False, False))
        self.assertTrue(value["flags"]["STEAM"]["contrast_positive"])          # STEAM's BSPs are below FLAT's
        self.assertFalse(value["flags"]["DRIFT"]["contrast_positive"])
        ticks_300 = value["decision_times"]["-300"]["ticks_p_minus_bsp"]["all"]
        self.assertNotIn("quantiles_minus_median", ticks_300)
        self.assertEqual(set(ticks_300["quantiles"]), {"0.05", "0.1", "0.25", "0.5", "0.75", "0.9", "0.95"})
        self.assertIn("mean NCLV", files["summary.md"].decode())

    def cli(self, *arguments):
        """(exit code, stdout) of main() with this registration and commit."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--registration-sha256", self.sha, "--archive-root", str(self.root), "--code-commit", COMMIT,
                         "--registration", str(self.registration), *arguments])
        return code, out.getvalue()

    def test_no_output_holds_a_market_or_selection_id(self):
        ids = sorted(self.records) + [str(sid) for sid in SIDS] + [fx.EVENT]
        self.assertEqual(len(self.records), 12)
        blind = self.cli("--pass", "blind", "--out", str(self.tmp / "blind"))
        registered = self.cli("--pass", "registered", "--out", str(self.tmp / "registered"), "--blind-receipt",
                              str(self.tmp / "blind/receipt.json"))
        self.assertEqual((blind[0], registered[0]), (0, 0))
        for out in ("blind", "registered"):
            for name, data in self.outputs(self.tmp / out).items():
                text = data.decode()
                with self.subTest(out=out, file=name):
                    self.assertEqual([item for item in ids if item in text], [])
        record = self.records["1.170000001"]
        raw = safe_target(self.root, self.manifest["archive"]["sha256"], record["provenance"]["member_path"])
        raw.chmod(0o644)
        raw.write_bytes(raw.read_bytes() + b"\0")
        failed = self.cli("--pass", "blind", "--out", str(self.tmp / "failed"))
        self.assertEqual(failed[0], 2)
        self.assertEqual(json.loads(failed[1]), {"status": "failed", "failure": "replay:member_hash_mismatch"})
        for name, (_, printed) in {"blind": blind, "registered": registered, "failed replay": failed}.items():
            with self.subTest(stdout=name):
                self.assertEqual([item for item in ids if item in printed], [])

    def test_a_market_in_two_registered_tiers_refuses(self):
        same = [(path(M, plan="PRO"), market(plan="PRO")), (path(M, plan="ADVANCED"), market(plan="ADVANCED"))]
        root, manifest, _ = self.build(same)
        sha = manifest["archive"]["sha256"]
        self.assertEqual(self.run_pass("blind", self.tmp / "two", root=root),
                         {"status": "failed", "failure": f"input:{sha}:market_in_two_tiers"})
        self.assertFalse((self.tmp / "two").exists())
        # A copy under a plan the registration does not name is coded plan_not_registered, not refused.
        basic = [(path(M, plan="PRO"), market(plan="PRO")), (path(M, plan="BASIC"), market(plan="BASIC"))]
        root, _, _ = self.build(basic)
        result = self.run_pass("blind", self.tmp / "basic", root=root)
        self.assertEqual((result["status"], result["records"], result["issued"]["-300"]), ("completed", 2, 1))
        value = json.loads((self.tmp / "basic" / "summary.json").read_bytes())
        self.assertEqual(value["decision_times"]["-300"]["codes"]["plan_not_registered"], 1)

    def test_the_registered_pass_needs_the_blind_pass_code(self):
        self.run_pass("blind", self.tmp / "blind")
        receipt = json.loads((self.tmp / "blind/receipt.json").read_bytes())
        forged = self.tmp / "forged.json"
        for change in ({"code_commit": "f" * 40}, {"source_descriptor_sha256": "sha256:" + "3" * 64}):
            with self.subTest(change=change):
                forged.write_bytes(canonical_json({**receipt, **change}) + b"\n")
                self.assertEqual(self.run_pass("registered", self.tmp / "registered", blind_receipt=forged),
                                 {"status": "refused", "problems": ["blind_receipt:another_code"]})
        self.assertFalse((self.tmp / "registered").exists())

    def test_outputs_are_private_and_appear_whole(self):
        out = self.tmp / "blind"
        self.assertEqual(self.run_pass("blind", out)["status"], "completed")
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertEqual({name: stat.S_IMODE((out / name).stat().st_mode) for name in self.outputs(out)},
                         dict.fromkeys(("summary.json", "summary.md", "receipt.json"), 0o600))
        self.assertFalse((self.tmp / "blind.partial").exists())
        (self.tmp / "again.partial").mkdir()
        self.assertEqual(self.run_pass("blind", self.tmp / "again"),
                         {"status": "refused", "problems": ["out:partial_present"]})
        self.assertFalse((self.tmp / "again").exists())

    def test_the_same_input_gives_the_same_summary(self):
        self.run_pass("blind", self.tmp / "one")
        self.run_pass("blind", self.tmp / "two")
        self.assertEqual(self.outputs(self.tmp / "one")["summary.json"], self.outputs(self.tmp / "two")["summary.json"])

    def test_the_registration_hash_is_required(self):
        out = self.tmp / "out"
        for sha, problem in (("0" * 64, "registration:sha256_mismatch"), ("abc", "registration_sha256:malformed")):
            with self.subTest(problem=problem):
                self.assertEqual(self.run_pass("registered", out, sha=sha),
                                 {"status": "refused", "problems": [problem]})
                self.assertEqual(self.run_pass("blind", out, sha=sha), {"status": "refused", "problems": [problem]})
        reordered = json.loads(self.registration.read_bytes())
        reordered["late_move"]["exclusions"].reverse()
        changed = self.tmp / "changed.json"
        changed.write_bytes(json.dumps(reordered).encode())
        self.assertEqual(self.run_pass("blind", out, registration_path=changed,
                                       sha=hashlib.sha256(changed.read_bytes()).hexdigest()),
                         {"status": "refused",
                          "problems": ["registration:late_move.exclusions:not_the_implemented_list"]})
        self.assertFalse(out.exists())

    def test_the_out_directory_is_refused_in_a_worktree_a_data_tree_or_the_root(self):
        worktree = self.tmp / "clone"
        (worktree / ".git").mkdir(parents=True)
        data_tree = self.tmp / "Betangel-Data"
        data_tree.mkdir()
        existing = self.tmp / "existing"
        existing.mkdir()
        for out, problem in ((worktree / "out", "out:inside_git_worktree"), (REPO / "out", "out:inside_repository"),
                             (data_tree / "out", "out:under_a_data_tree"),
                             (self.tmp / "soccer-data" / "out", "out:under_a_data_tree"),
                             (existing, "out:already_exists"), (self.root / "out", "roots:not_distinct"),
                             (self.tmp / "missing" / "out", "out:unresolvable")):
            with self.subTest(out=str(out)):
                self.assertEqual(self.run_pass("blind", out), {"status": "refused", "problems": [problem]})
                self.assertFalse(out.exists() and out != existing)

    def test_the_blind_receipt_must_be_this_registrations_and_inputs(self):
        self.run_pass("blind", self.tmp / "blind")
        receipt = json.loads((self.tmp / "blind/receipt.json").read_bytes())
        self.assertEqual(self.run_pass("blind", self.tmp / "again", blind_receipt=self.tmp / "blind/receipt.json"),
                         {"status": "refused", "problems": ["blind_receipt:not_for_the_blind_pass"]})
        forged = self.tmp / "forged.json"
        for change, problem in (({"registration_sha256": "1" * 64}, "blind_receipt:another_registration"),
                                ({"input_sha256": "2" * 64}, "blind_receipt:another_input"),
                                ({"pass": "registered"}, "blind_receipt:not_a_completed_blind_receipt")):
            with self.subTest(problem=problem):
                forged.write_bytes(canonical_json({**receipt, **change}) + b"\n")
                self.assertEqual(self.run_pass("registered", self.tmp / "registered", blind_receipt=forged),
                                 {"status": "refused", "problems": [problem]})
        forged.write_bytes(json.dumps(receipt).encode())
        self.assertEqual(self.run_pass("registered", self.tmp / "registered", blind_receipt=forged),
                         {"status": "refused", "problems": ["blind_receipt:not_canonical"]})
        self.assertFalse((self.tmp / "registered").exists())

    def test_archives_ingested_under_other_limits_are_refused(self):
        limits = replace(DEFAULT_LIMITS, max_line_bytes=DEFAULT_LIMITS.max_line_bytes - 1)
        sha = self.manifest["archive"]["sha256"]
        self.assertEqual(self.run_pass("blind", self.tmp / "out", limits=limits),
                         {"status": "refused", "problems": [f"input:{sha}:limits_not_the_runs"]})

    def test_a_tampered_member_fails_and_writes_nothing(self):
        record = self.records["1.170000001"]
        raw = safe_target(self.root, self.manifest["archive"]["sha256"], record["provenance"]["member_path"])
        raw.chmod(0o644)
        raw.write_bytes(raw.read_bytes() + b"\0")
        self.assertEqual(self.run_pass("blind", self.tmp / "out"),
                         {"status": "failed", "failure": "replay:member_hash_mismatch"})
        self.assertFalse((self.tmp / "out").exists())

    def test_the_cli_prints_counts_codes_and_hashes(self):
        code, printed = self.cli("--pass", "blind", "--out", str(self.tmp / "cli"))
        printed = json.loads(printed)
        self.assertEqual((code, printed["status"]), (0, "completed"))
        self.assertEqual(set(printed), {"status", "pass", "registration_sha256", "input_sha256", "summary_sha256",
                                        "receipt_sha256", "records", "replayed_markets", "issued", "eligible",
                                        "late_move_population"})
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--pass", "blind", "--registration-sha256", self.sha, "--archive-root", str(self.root),
                         "--out", str(self.tmp / "cli"), "--code-commit", "HEAD", "--registration",
                         str(self.registration)])
        self.assertEqual((code, json.loads(out.getvalue())), (2, {"status": "refused",
                                                                  "problems": ["code_commit:malformed"]}))


class ImportBoundaryTests(unittest.TestCase):
    def test_the_study_stays_inside_the_backtest_closure(self):
        from test_betfair_backtest_boundaries import (BACKTEST, BETFAIR, FORBIDDEN_BETFAIR, REAL, TOP,
                                                      backtest_violations)
        closure = REAL.closure([f"{BACKTEST}.movement", f"{BACKTEST}.movement_study"])
        outside = {module for module in closure if not module.startswith(BACKTEST)}
        self.assertEqual(outside, {TOP, BETFAIR, f"{TOP}.temporal"})
        self.assertEqual(backtest_violations(REAL, betfair=BETFAIR, forbidden=FORBIDDEN_BETFAIR), [])

    def test_importing_the_study_loads_no_other_betfair_module(self):
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "import betangel_research.betfair.backtest.movement_study; "
                 "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('betangel_research'))))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(REPO)], capture_output=True, text=True,
                                check=True)
        loaded = set(result.stdout.split())
        self.assertEqual({m for m in loaded if not m.startswith("betangel_research.betfair.backtest")},
                         {"betangel_research", "betangel_research.betfair", "betangel_research.temporal"})


if __name__ == "__main__":
    unittest.main()
