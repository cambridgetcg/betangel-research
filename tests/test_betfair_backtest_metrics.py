"""Per-variant metrics, drawdown and the block bootstrap (betangel_research.betfair.backtest.metrics), synthetic only.

These show N_R, S_R, S_U and both ROIs as exact fractions (None for a zero denominator, never 0), that variants are
never pooled, that drawdown follows global chronology, and that the sha256 stream and the bootstrap are
deterministic: pinned numbers for one seed, reproduced by an independent implementation of the documented
algorithm written here. They do not show anything about real markets or that an interval is well calibrated.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import itertools
import json
import math
from pathlib import Path
import unittest

from betangel_research.betfair.backtest.accounting import (AccountingRules, IssuedBet, MarketOutcome, RunnerOutcome,
                                                           Settlement, settle)
from betangel_research.betfair.backtest.metrics import (BootstrapPlan, HashStream, Interval, as_record, block_bootstrap,
                                                        block_key, max_drawdown, metrics_by_variant,
                                                        percentile_interval, variant_metrics)

REPO = Path(__file__).resolve().parent.parent
DRAFT = REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json"
TWO_PERCENT = AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny")
ZERO = AccountingRules(commission_rate=Decimal("0"), rounding="per_market_ceil_penny")
COMPLETE = dict(read_complete=True, closed=True, market_void=False, result_revised_after_close=False,
                bsp_reconciled=True, dead_heat=False, declared_winners=1, removed_after_decision=frozenset())
DOMAIN = b"betangel-research/backtest/hash-stream/v1"


def at(day, hour=14, minute=0):
    return datetime(2015, 6, day, hour, minute, tzinfo=timezone.utc)


def outcome(market_id, off, own="WINNER", bsp="2", **changes):
    """Runner 1 is ours (own status at bsp); runner 2 takes the other result at 3.0."""
    other = "LOSER" if own == "WINNER" else "WINNER"
    runners = {1: RunnerOutcome(own, Decimal(bsp)), 2: RunnerOutcome(other, Decimal("3"))}
    return MarketOutcome(market_id=market_id, first_published_off=off, runners=runners, **dict(COMPLETE, **changes))


def bet(market_id, stake=100, variant="v", selection=1):
    return IssuedBet(decision_id=f"{variant}-{market_id}-{selection}", variant=variant, market_id=market_id,
                     selection_id=selection, stake_pence=stake, side="BACK", basis="BSP")


def run(spec, rules=TWO_PERCENT, variant="v"):
    """spec: (market id, off, own status, bsp, stake, outcome changes)."""
    return settle([bet(m, stake, variant) for m, _, _, _, stake, _ in spec],
                  [outcome(m, off, own, bsp, **changes) for m, off, own, bsp, _, changes in spec], rules)


# Five race days at 2%: day totals (net, S_R, S_U) = (47, 200, 0), (-100, 100, 0), (294, 100, 100), (-200, 200, 0),
# (49, 100, 0).
FIVE_DAYS = (("1.1", at(1), "WINNER", "2.5", 100, {}), ("1.2", at(1, 15), "LOSER", "2", 100, {}),
             ("1.3", at(2), "LOSER", "2", 100, {}), ("1.4", at(3), "WINNER", "4.0", 100, {}),
             ("1.5", at(3, 15), "WINNER", "2", 100, {"market_void": True}), ("1.6", at(4), "LOSER", "2", 100, {}),
             ("1.7", at(4, 15), "LOSER", "2", 100, {}), ("1.8", at(5), "WINNER", "1.5", 100, {}))


def reference_bootstrap(day_totals, seed, label, resamples, level):
    """The module docstring's algorithm, written again from the text: sha256 words, rejection, nearest rank."""
    prefix = DOMAIN + b"\x00" + json.dumps([seed, label], separators=(",", ":")).encode("utf-8") + b"\x00"
    words = (int.from_bytes(hashlib.sha256(prefix + counter.to_bytes(8, "big")).digest()[i:i + 8], "big")
             for counter in itertools.count() for i in (0, 8, 16, 24))
    n = len(day_totals)
    limit = 2 ** 64 - 2 ** 64 % n
    accepted = (word % n for word in words if word < limit)
    resolved, pessimistic = [], []
    for _ in range(resamples):
        drawn = [day_totals[next(accepted)] for _ in range(n)]
        net, s_r, s_u = (sum(t[i] for t in drawn) for i in range(3))
        resolved.append(Fraction(net, s_r) if s_r else None)
        pessimistic.append(Fraction(net - s_u, s_r + s_u))
    tail = (1 - level) / 2

    def interval(values):
        if None in values:
            return None
        values = sorted(values)
        return Interval(*(values[max(math.ceil(q * len(values)), 1) - 1] for q in (tail, 1 - tail)))

    return interval(resolved), interval(pessimistic)


class Accounting(unittest.TestCase):
    def test_n_r_s_r_s_u_and_both_rois(self):
        spec = (("1.1", at(1), "WINNER", "3.0", 100, {}), ("1.2", at(1, 15), "LOSER", "2", 100, {}),
                ("1.3", at(2), "WINNER", "2", 100, {"market_void": True}),
                ("1.4", at(2, 15), "WINNER", "2", 100, {"removed_after_decision": {1}}))
        m = variant_metrics(run(spec), "v")
        self.assertEqual((m.net_resolved_pence, m.resolved_stake_pence, m.unresolved_stake_pence), (96, 200, 200))
        self.assertEqual((m.gross_resolved_pence, m.commission_pence), (100, 4))
        self.assertEqual((m.issued_bets, m.issued_stake_pence, m.resolved_bets, m.unresolved_bets), (4, 400, 2, 2))
        self.assertEqual((m.resolved_markets, m.unresolved_markets), (2, 2))
        self.assertEqual(m.roi_resolved, Fraction(96, 200))
        self.assertEqual(m.roi_all_issued_pessimistic, Fraction(96 - 200, 400))
        self.assertEqual((m.settlement_coverage_by_count, m.settlement_coverage_by_stake),
                         (Fraction(1, 2), Fraction(1, 2)))
        self.assertEqual(m.bets_by_status, (("lost", 1), ("unresolved", 2), ("won", 1)))
        self.assertEqual(m.bets_by_reason, (("market_void", 1), ("own_runner_removed_after_decision", 1)))

    def test_zero_denominators_are_undefined_never_zero(self):
        spec = (("1.1", at(1), "WINNER", "2", 100, {"closed": None}),)
        m = variant_metrics(run(spec), "v")
        self.assertEqual((m.resolved_stake_pence, m.unresolved_stake_pence, m.net_resolved_pence), (0, 100, 0))
        self.assertIsNone(m.roi_resolved)
        self.assertEqual(m.roi_all_issued_pessimistic, Fraction(-1))
        self.assertEqual((m.settlement_coverage_by_count, m.settlement_coverage_by_stake), (Fraction(0), Fraction(0)))
        empty = variant_metrics(run(spec), "nobody")
        self.assertEqual((empty.issued_bets, empty.roi_resolved, empty.roi_all_issued_pessimistic,
                          empty.settlement_coverage_by_count, empty.settlement_coverage_by_stake),
                         (0, None, None, None, None))
        self.assertEqual(empty.drawdown.max_drawdown_pence, 0)

    def test_settlement_coverage_by_stake_weighs_stakes(self):
        spec = (("1.1", at(1), "WINNER", "2", 300, {}), ("1.2", at(2), "WINNER", "2", 100, {"closed": False}))
        m = variant_metrics(run(spec), "v")
        self.assertEqual((m.settlement_coverage_by_count, m.settlement_coverage_by_stake),
                         (Fraction(1, 2), Fraction(3, 4)))
        self.assertEqual(m.roi_all_issued_pessimistic, Fraction(294 - 100, 400))

    def test_reason_mentions_count_every_reason(self):
        spec = (("1.1", at(1), "REMOVED", "2", 100, {"closed": False, "removed_after_decision": {1}}),)
        m = variant_metrics(run(spec), "v")
        self.assertEqual(m.bets_by_reason, (("market_not_closed", 1),))
        self.assertEqual(m.reason_mentions, (("market_not_closed", 1), ("own_runner_removed_after_decision", 1)))

    def test_variants_are_reported_separately_never_pooled(self):
        result = settle([bet("1.1", 1000, "candidate", 1), bet("1.1", 1000, "baseline", 2)],
                        [outcome("1.1", at(1))], TWO_PERCENT)
        candidate, baseline = (variant_metrics(result, v) for v in ("candidate", "baseline"))
        self.assertEqual((candidate.net_resolved_pence, candidate.commission_pence), (980, 20))
        self.assertEqual((baseline.net_resolved_pence, baseline.commission_pence), (-1000, 0))
        self.assertEqual((candidate.roi_resolved, baseline.roi_resolved), (Fraction(49, 50), Fraction(-1)))
        rules = AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny",
                                variants=["baseline", "candidate", "idle"])
        listed = metrics_by_variant(settle([bet("1.1", 1000, "candidate", 1)], [outcome("1.1", at(1))], rules))
        self.assertEqual([m.variant for m in listed], ["baseline", "candidate", "idle"])
        self.assertEqual([m.issued_bets for m in listed], [0, 1, 0])

    def test_a_settlement_that_does_not_reconcile_is_refused(self):
        good = run(FIVE_DAYS)
        broken = Settlement(rules=good.rules, bets=good.bets[1:], markets=good.markets)
        with self.assertRaisesRegex(ValueError, "^reconciliation_failed$"):
            variant_metrics(broken, "v")


class Drawdowns(unittest.TestCase):
    def test_global_chronology_not_input_order(self):
        spec = (("1.1", at(1, 13), "WINNER", "2", 100, {}), ("1.2", at(1, 12), "LOSER", "2", 300, {}),
                ("1.3", at(1, 14), "LOSER", "2", 50, {}),
                ("1.4", at(1, 12, 30), "WINNER", "2", 1000, {"market_void": True}))
        result = run(spec, ZERO)
        d = variant_metrics(result, "v").drawdown
        self.assertEqual((d.max_drawdown_pence, d.peak_pence, d.trough_pence), (300, 0, -300))
        self.assertEqual((d.peak_off, d.trough_off, d.final_net_pence), (None, at(1, 12), -250))
        self.assertEqual((d.resolved_markets, d.unresolved_markets, d.unresolved_stake_pence), (3, 1, 1000))
        self.assertEqual(max_drawdown(reversed(result.markets)), d)

    def test_simultaneous_offs_are_one_point_so_market_ids_cannot_move_the_drawdown(self):
        found = []
        for loser, winner in (("1.9", "1.10"), ("1.10", "1.9")):
            spec = (("1.1", at(1, 13), "LOSER", "2", 100, {}), (loser, at(1, 14), "LOSER", "2", 500, {}),
                    (winner, at(1, 14), "WINNER", "6", 100, {}))
            found.append(variant_metrics(run(spec, ZERO), "v").drawdown)
        self.assertEqual(found[0], found[1])                    # one by one in id order: 600 one way, 500 the other
        self.assertEqual((found[0].max_drawdown_pence, found[0].peak_off, found[0].trough_off), (100, None, at(1, 13)))
        spec = (("1.1", at(1, 13), "WINNER", "4", 100, {}), ("1.2", at(1, 14), "LOSER", "2", 100, {}),
                ("1.3", at(1, 14), "LOSER", "2", 150, {}), ("1.4", at(1, 14), "WINNER", "2", 900, {"closed": None}))
        d = variant_metrics(run(spec, ZERO), "v").drawdown
        self.assertEqual((d.max_drawdown_pence, d.peak_pence, d.trough_pence, d.peak_off, d.trough_off),
                         (250, 300, 50, at(1, 13), at(1, 14)))
        self.assertEqual((d.resolved_markets, d.unresolved_markets, d.unresolved_stake_pence), (3, 1, 900))

    def test_one_variant_at_a_time(self):
        result = settle([bet("1.1", variant="a"), bet("1.1", variant="b")], [outcome("1.1", at(1))], ZERO)
        with self.assertRaisesRegex(ValueError, "^drawdown_takes_one_variant$"):
            max_drawdown(result.markets)


class Stream(unittest.TestCase):
    def test_words_are_the_documented_sha256_words(self):
        prefix = DOMAIN + b"\x00" + b'[2,"v|race_day"]' + b"\x00"
        expected = [int.from_bytes(hashlib.sha256(prefix + c.to_bytes(8, "big")).digest()[i:i + 8], "big")
                    for c in range(3) for i in (0, 8, 16, 24)]
        stream = HashStream(2, "v|race_day")
        self.assertEqual([stream.word() for _ in range(12)], expected)
        self.assertEqual(expected[:3], [15504695377800842130, 15335013941657233144, 9933751304907564346])

    def test_rejection_sampling_skips_words_above_the_largest_multiple(self):
        n = 2 ** 63 + 1
        limit = 2 ** 64 - 2 ** 64 % n
        self.assertEqual(limit, 2 ** 63 + 1)
        words = HashStream(5, "x")
        raw = [words.word() for _ in range(200)]
        expected = [w % n for w in raw if w < limit][:40]
        self.assertGreater(sum(1 for w in raw[:80] if w >= limit), 10)     # the rejection path runs
        self.assertEqual(HashStream(5, "x").draws(n, 40), expected)

    def test_uniform_and_deterministic(self):
        values = HashStream(1, "u").draws(3, 30000)
        counts = [values.count(k) for k in range(3)]
        self.assertTrue(all(abs(c - 10000) < 600 for c in counts), counts)
        self.assertEqual(HashStream(1, "u").draws(7, 50), HashStream(1, "u").draws(7, 50))
        self.assertNotEqual(HashStream(1, "u").draws(7, 50), HashStream(1, "w").draws(7, 50))
        self.assertNotEqual(HashStream(1, "u").draws(7, 50), HashStream(2, "u").draws(7, 50))
        self.assertEqual(HashStream(3, "z").draws(1, 5), [0] * 5)

    def test_bad_bounds_and_seeds_are_refused(self):
        for n in (0, -1, 2 ** 64 + 1, True, 2.0):
            with self.subTest(n=n), self.assertRaisesRegex(ValueError, "^bound_out_of_range$"):
                HashStream(1, "x").below(n)
        for seed in (-1, 2 ** 63, True, "1"):
            with self.subTest(seed=seed), self.assertRaisesRegex(ValueError, "^seed_out_of_range$"):
                HashStream(seed, "x")


class Bootstrap(unittest.TestCase):
    def test_pinned_numbers_for_seed_two_and_an_independent_reimplementation(self):
        result = run(FIVE_DAYS)
        days = [(47, 200, 0), (-100, 100, 0), (294, 100, 100), (-200, 200, 0), (49, 100, 0)]
        point = variant_metrics(result, "v")
        self.assertEqual((point.roi_resolved, point.roi_all_issued_pessimistic), (Fraction(9, 70), Fraction(-1, 80)))
        for resamples, resolved, pessimistic in (
                (1000, Interval(Fraction(-551, 700), Fraction(244, 175)), Interval(Fraction(-551, 700),
                                                                                    Fraction(533, 800))),
                (10000, Interval(Fraction(-551, 700), Fraction(147, 100)), Interval(Fraction(-551, 700),
                                                                                     Fraction(533, 800)))):
            with self.subTest(resamples=resamples):
                found = block_bootstrap(result, "v", BootstrapPlan(resamples, Fraction(19, 20), 2))
                self.assertEqual((found.roi_resolved, found.roi_all_issued_pessimistic), (resolved, pessimistic))
                self.assertEqual((found.blocks, found.roi_resolved_undefined,
                                  found.roi_all_issued_pessimistic_undefined), (5, 0, 0))
                self.assertEqual(reference_bootstrap(days, 2, "v|race_day", resamples, Fraction(19, 20)),
                                 (resolved, pessimistic))

    def test_nearest_rank_percentiles(self):
        values = [Fraction(v) for v in range(40, 0, -1)]
        self.assertEqual(percentile_interval(values, Fraction(19, 20)), Interval(Fraction(1), Fraction(39)))
        self.assertEqual(percentile_interval(values, Fraction(9, 10)), Interval(Fraction(2), Fraction(38)))
        self.assertEqual(percentile_interval(values[:10], Fraction(1, 2)), Interval(Fraction(33), Fraction(38)))
        self.assertEqual(percentile_interval([Fraction(5)], Fraction(19, 20)), Interval(Fraction(5), Fraction(5)))
        self.assertIsNone(percentile_interval(values + [None], Fraction(19, 20)))
        many = list(range(1, 10001))
        self.assertEqual(percentile_interval(many, Fraction(19, 20)), Interval(250, 9750))
        for level in (0.95, 0.7, Fraction(0), Fraction(1), 1, None):             # 0.95 would give low 251
            with self.subTest(level=level), self.assertRaisesRegex(ValueError, "^level_not_a_fraction_in_0_1$"):
                percentile_interval(many, level)
        with self.assertRaisesRegex(ValueError, "^no_values$"):
            percentile_interval([], Fraction(19, 20))

    def test_one_block_gives_no_interval_not_a_zero_width_one(self):
        spec = (("1.1", at(1), "WINNER", "3", 100, {}), ("1.2", at(1, 15), "LOSER", "2", 100, {}))
        result = run(spec)
        self.assertEqual(variant_metrics(result, "v").roi_resolved, Fraction(12, 25))
        found = block_bootstrap(result, "v", BootstrapPlan(10000, Fraction(19, 20), 2))
        self.assertEqual((found.blocks, found.roi_resolved, found.roi_all_issued_pessimistic,
                          found.intervals_undefined_reason), (1, None, None, "too_few_blocks"))
        self.assertEqual((found.roi_resolved_undefined, found.roi_all_issued_pessimistic_undefined), (0, 0))
        unresolved = run((("1.1", at(1), "WINNER", "3", 100, {"closed": None}),))
        found = block_bootstrap(unresolved, "v", BootstrapPlan(100, Fraction(19, 20), 2))
        self.assertEqual((found.blocks, found.roi_resolved_undefined, found.roi_all_issued_pessimistic_undefined,
                          found.intervals_undefined_reason), (1, 100, 0, "too_few_blocks"))
        two = block_bootstrap(run(spec + (("1.3", at(2), "LOSER", "2", 100, {}),)), "v",
                              BootstrapPlan(100, Fraction(19, 20), 2))
        self.assertEqual((two.blocks, two.intervals_undefined_reason), (2, None))
        self.assertIsNotNone(two.roi_resolved)

    def test_blocks_are_london_race_days_not_utc_dates(self):
        for first, second, blocks in ((at(1, 23, 30), at(2, 13), 1),            # 00:30 BST on 06-02, then 14:00 BST
                                      (datetime(2015, 12, 1, 23, 30, tzinfo=timezone.utc),
                                       datetime(2015, 12, 2, 13, tzinfo=timezone.utc), 2)):   # GMT: London is UTC
            with self.subTest(first=first):
                spec = (("1.1", first, "WINNER", "3", 100, {}), ("1.2", second, "LOSER", "2", 100, {}))
                found = block_bootstrap(run(spec), "v", BootstrapPlan(100, Fraction(19, 20), 2))
                self.assertEqual(found.blocks, blocks)

    def test_an_undeclared_variant_is_refused_not_reported_empty(self):
        rules = AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny",
                                variants=["base", "cand"])
        result = settle([bet("1.1", 100, "cand")], [outcome("1.1", at(1))], rules)
        plan = BootstrapPlan(100, Fraction(19, 20), 2)
        for call in (lambda: variant_metrics(result, "cnad"), lambda: block_bootstrap(result, "cnad", plan),
                     lambda: metrics_by_variant(result, ["cand", "cnad"])):
            with self.assertRaisesRegex(ValueError, "^undeclared_variant:cnad$"):
                call()
        self.assertEqual(variant_metrics(result, "base").issued_bets, 0)          # declared and idle: fine
        self.assertEqual(block_bootstrap(result, "base", plan).blocks, 0)

    def test_many_distinct_days_match_the_independent_reimplementation(self):
        spec, days = [], []
        for day in range(1, 31):
            stake, bsp = 100 + 37 * day, Decimal(1) + Decimal(day * 7 % 23) / 4
            own = "WINNER" if day % 3 == 0 else "LOSER"
            void = {"market_void": True} if day % 7 == 0 else {}
            spec.append((f"1.{day}", datetime(2015, 7, day, 14, tzinfo=timezone.utc), own, str(bsp), stake, void))
        result = run(spec)
        for market in result.markets:
            days.append((market.net_pence or 0, market.resolved_stake_pence, market.unresolved_stake_pence))
        self.assertEqual(len(set(days)), 30)                     # no two days alike, so ranks rarely tie
        plan = BootstrapPlan(2000, Fraction(19, 20), 7)
        found = block_bootstrap(result, "v", plan)
        self.assertEqual((found.roi_resolved, found.roi_all_issued_pessimistic),
                         reference_bootstrap(days, 7, "v|race_day", 2000, Fraction(19, 20)))

    def test_the_contract_plan(self):
        contract = json.loads(DRAFT.read_text(encoding="utf-8"))
        plan = BootstrapPlan.from_contract(contract)
        self.assertEqual(plan, BootstrapPlan(10000, Fraction(19, 20), 2, "race_day"))
        found = block_bootstrap(run(FIVE_DAYS), "v", plan)
        self.assertEqual((found.resamples, found.method, found.confidence_level), (10000, "percentile_nearest_rank",
                                                                                   Fraction(19, 20)))
        contract["uncertainty"]["confidence_level"] = 0.9
        self.assertEqual(BootstrapPlan.from_contract(contract).confidence_level, Fraction(9, 10))
        contract["uncertainty"]["block"] = "market"
        with self.assertRaisesRegex(ValueError, "^contract_invalid$"):
            BootstrapPlan.from_contract(contract)

    def test_constant_roi_gives_a_degenerate_interval(self):
        spec = []
        for day in range(1, 7):
            spec += [(f"1.{day}1", at(day), "WINNER", "3.0", 100, {}),
                     (f"1.{day}2", at(day, 15), "LOSER", "2", 100, {}),
                     (f"1.{day}3", at(day, 16), "LOSER", "2", 100, {})]
        result = run(spec)
        roi = variant_metrics(result, "v").roi_resolved
        self.assertEqual(roi, Fraction(-4, 300))
        found = block_bootstrap(result, "v", BootstrapPlan(500, Fraction(19, 20), 2))
        self.assertEqual(found.roi_resolved, Interval(roi, roi))
        self.assertEqual(found.roi_all_issued_pessimistic, Interval(roi, roi))

    def test_an_undefined_resample_leaves_the_interval_undefined(self):
        spec = (("1.1", at(1), "WINNER", "2", 100, {}), ("1.2", at(2), "WINNER", "2", 100, {"closed": None}))
        found = block_bootstrap(run(spec), "v", BootstrapPlan(400, Fraction(19, 20), 2))
        self.assertIsNone(found.roi_resolved)
        self.assertGreater(found.roi_resolved_undefined, 0)
        self.assertIsNotNone(found.roi_all_issued_pessimistic)
        self.assertEqual(found.roi_all_issued_pessimistic_undefined, 0)
        nobody = block_bootstrap(run(spec), "nobody", BootstrapPlan(400, Fraction(19, 20), 2))
        self.assertEqual((nobody.blocks, nobody.roi_resolved, nobody.roi_resolved_undefined,
                          nobody.roi_all_issued_pessimistic), (0, None, 400, None))

    def test_a_variant_is_not_moved_by_another_variant(self):
        alone = run(FIVE_DAYS)
        both = settle([bet(m, 100, "v") for m, *_ in FIVE_DAYS] + [bet(m, 100, "w", 2) for m, *_ in FIVE_DAYS],
                      [outcome(m, off, own, bsp, **ch) for m, off, own, bsp, _, ch in FIVE_DAYS], TWO_PERCENT)
        plan = BootstrapPlan(300, Fraction(19, 20), 2)
        self.assertEqual(block_bootstrap(both, "v", plan), block_bootstrap(alone, "v", plan))
        self.assertEqual(variant_metrics(both, "v"), variant_metrics(alone, "v"))

    def test_longer_blocks_group_race_days(self):
        self.assertEqual(block_key(date(2015, 6, 1), "iso_week"), (2015, 23))
        self.assertEqual(block_key(date(2015, 6, 7), "iso_week"), (2015, 23))
        self.assertEqual(block_key(date(2015, 6, 8), "calendar_month"), (2015, 6))
        spec = tuple(("1.%d" % day, at(day), "WINNER", "2", 100, {}) for day in (1, 2, 7, 8))
        result = run(spec)
        for block, blocks in (("race_day", 4), ("iso_week", 2), ("calendar_month", 1)):
            with self.subTest(block=block):
                self.assertEqual(block_bootstrap(result, "v", BootstrapPlan(100, Fraction(1, 2), 2, block)).blocks,
                                 blocks)

    def test_plan_refusals(self):
        for change, code in ((dict(resamples=99), "resamples_out_of_range"),
                             (dict(confidence_level=0.95), "confidence_level_not_a_fraction_in_0_1"),
                             (dict(confidence_level=Fraction(1)), "confidence_level_not_a_fraction_in_0_1"),
                             (dict(seed=-1), "seed_out_of_range"), (dict(block="market"), "block_not_supported")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, f"^{code}$"):
                BootstrapPlan(**dict(dict(resamples=100, confidence_level=Fraction(19, 20), seed=2), **change))


class Records(unittest.TestCase):
    def test_records_are_json_ready_and_exact(self):
        result = run(FIVE_DAYS)
        record = as_record(variant_metrics(result, "v"))
        self.assertEqual(record["roi_resolved"], {"exact": "9/70", "decimal": "0.12857143"})
        self.assertEqual(record["roi_all_issued_pessimistic"], {"exact": "-1/80", "decimal": "-0.01250000"})
        self.assertIsNone(as_record(variant_metrics(result, "nobody"))["roi_resolved"])
        markets = as_record(result)["markets"]
        self.assertEqual((markets[0]["first_published_off"], markets[0]["race_day"]), ("2015-06-01T14:00:00Z",
                                                                                         "2015-06-01"))
        text = json.dumps(as_record(result), sort_keys=True)
        self.assertEqual(text, json.dumps(as_record(run(tuple(reversed(FIVE_DAYS)))), sort_keys=True))
        self.assertIn('"bsp": "2.5"', text)

    def test_display_rounds_half_even_exactly_in_fixed_point(self):
        for value, text in ((Fraction(1, 200000000), "0.00000000"), (Fraction(3, 200000000), "0.00000002"),
                            (Fraction(-1, 200000000), "0.00000000"), (Fraction(-3, 200000000), "-0.00000002"),
                            (Fraction(1, 200000000) + Fraction(1, 10 ** 70), "0.00000001"),
                            (Fraction(0), "0.00000000"), (Fraction(7), "7.00000000"), (Fraction(-1, 3), "-0.33333333")):
            with self.subTest(value=value):
                self.assertEqual(as_record(value), {"exact": f"{value.numerator}/{value.denominator}",
                                                    "decimal": text})

    def test_accounting_inputs_are_recordable(self):
        known = outcome("1.2", at(1), removed_after_decision={3, 2})
        record = as_record(known)
        self.assertEqual(record["removed_after_decision"], [2, 3])
        self.assertEqual((record["first_published_off"], record["race_day"]), ("2015-06-01T14:00:00Z", "2015-06-01"))
        self.assertEqual(record["runners"][0], [1, {"status": "WINNER", "bsp": "2"}])
        self.assertNotIn("_by_id", record)
        self.assertIsNone(as_record(MarketOutcome.unknown("1.2", at(1)))["removed_after_decision"])
        json.dumps(record)
        with self.assertRaisesRegex(ValueError, "^not_recordable$"):
            as_record({1, "a"})


if __name__ == "__main__":
    unittest.main()
