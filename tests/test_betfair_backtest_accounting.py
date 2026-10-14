"""P0 settlement and commission (betangel_research.betfair.backtest.accounting) on synthetic records only.

These show the plan section 9 known answers to the penny, that every exceptional case of the section 7 table (and
every unknown indicator) is unresolved with its stake kept and an explicit reason, the BSP precision and rounding
rules, the mixed-market rule, input refusals and the Europe/London race day. They read no index, archive or raw
data, and they do not show that c2 fills the records correctly from the task (b) index.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import random
import unittest

from betangel_research.betfair.backtest import accounting
from betangel_research.betfair.backtest.accounting import (LOST, SETTLEMENT_CASE, UNRESOLVED, UNRESOLVED_REASONS, WON,
                                                           AccountingRules, IssuedBet, MarketOutcome, RunnerOutcome,
                                                           commission_pence, decimal_from_json_number, race_day_of,
                                                           settle, utc_from_text, win_gross_pence)
from betangel_research.betfair.backtest.contract import SETTLEMENT_CASES

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:                                            # pragma: no cover - zoneinfo is 3.9+
    ZoneInfo = None

REPO = Path(__file__).resolve().parent.parent
DRAFT = REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json"
OFF = datetime(2015, 6, 1, 13, 30, tzinfo=timezone.utc)
TWO_PERCENT = AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny")
COMPLETE = dict(read_complete=True, closed=True, market_void=False, result_revised_after_close=False,
                bsp_reconciled=True, dead_heat=False, declared_winners=1, removed_after_decision=frozenset())


def draft():
    return json.loads(DRAFT.read_text(encoding="utf-8"))


def bet(decision_id, market_id="1.1", selection_id=1, stake_pence=100, variant="v"):
    return IssuedBet(decision_id=decision_id, variant=variant, market_id=market_id, selection_id=selection_id,
                     stake_pence=stake_pence, side="BACK", basis="BSP")


def runner(status, bsp):
    return RunnerOutcome(status=status, bsp=None if bsp is None else Decimal(bsp))


def outcome(market_id="1.1", off=OFF, runners=None, **changes):
    """A complete, clean result unless changed: runner 1 WINNER at 2.0, runner 2 LOSER at 3.0."""
    fields = dict(COMPLETE, **changes)
    if runners is None:
        runners = {1: runner("WINNER", "2.0"), 2: runner("LOSER", "3.0")}
    return MarketOutcome(market_id=market_id, first_published_off=off, runners=runners, **fields)


def by_id(settlement):
    return {result.decision_id: result for result in settlement.bets}


def total_net(settlement):
    return sum(market.net_pence for market in settlement.markets if market.resolved)


class KnownAnswers(unittest.TestCase):
    """docs/backtest-plan.md section 9 at 2% commission, to the penny."""

    def test_separate_one_pound_win_and_loss_net_minus_two_pence(self):
        result = settle([bet("win", "1.1"), bet("loss", "1.2")],
                        [outcome("1.1"), outcome("1.2", runners={1: runner("LOSER", "2.0"),
                                                                 2: runner("WINNER", "3.0")})], TWO_PERCENT)
        bets = by_id(result)
        self.assertEqual((bets["win"].status, bets["win"].gross_pence), (WON, 100))
        self.assertEqual((bets["loss"].status, bets["loss"].gross_pence), (LOST, -100))
        markets = {m.market_id: m for m in result.markets}
        self.assertEqual((markets["1.1"].commission_pence, markets["1.1"].net_pence), (2, 98))
        self.assertEqual((markets["1.2"].commission_pence, markets["1.2"].net_pence), (0, -100))
        self.assertEqual(total_net(result), -2)

    def test_plus_and_minus_ten_pounds_in_one_market_pay_no_commission(self):
        result = settle([bet("a", "1.1", 1, 1000), bet("b", "1.1", 2, 1000)],
                        [outcome("1.1", runners={1: runner("WINNER", "2.0"), 2: runner("LOSER", "2.0")})],
                        TWO_PERCENT)
        (market,) = result.markets
        self.assertEqual((market.gross_pence, market.commission_pence, market.net_pence), (0, 0, 0))
        self.assertEqual(sorted(b.gross_pence for b in result.bets), [-1000, 1000])

    def test_plus_and_minus_ten_pounds_in_separate_markets_net_minus_twenty_pence(self):
        result = settle([bet("a", "1.1", 1, 1000), bet("b", "1.2", 1, 1000)],
                        [outcome("1.1"), outcome("1.2", runners={1: runner("LOSER", "2.0"),
                                                                 2: runner("WINNER", "2.0")})], TWO_PERCENT)
        self.assertEqual(sum(m.commission_pence for m in result.markets), 20)
        self.assertEqual(total_net(result), -20)

    def test_void_and_dead_heat_fixtures_are_unresolved_with_exposure(self):
        result = settle([bet("void", "1.1"), bet("heat", "1.2")],
                        [outcome("1.1", market_void=True),
                         outcome("1.2", dead_heat=True, runners={1: runner("WINNER", "2.0"),
                                                                 2: runner("WINNER", "3.0")})], TWO_PERCENT)
        bets = by_id(result)
        self.assertEqual((bets["void"].status, bets["void"].reason, bets["void"].gross_pence),
                         (UNRESOLVED, "market_void", None))
        self.assertEqual((bets["heat"].status, bets["heat"].reason, bets["heat"].gross_pence),
                         (UNRESOLVED, "dead_heat", None))
        self.assertEqual([m.unresolved_stake_pence for m in result.markets], [100, 100])
        self.assertTrue(all(m.net_pence is None and m.commission_pence is None for m in result.markets))

    def test_contract_rules_give_the_same_answers_at_two_percent_and_five_at_the_draft_rate(self):
        contract = draft()
        contract["costs"]["commission_rate"] = 0.02
        rules = AccountingRules.from_contract(contract)
        self.assertEqual((rules.commission_rate, rules.rounding, rules.stake_pence, rules.variants),
                         (Decimal("0.02"), "per_market_ceil_penny", 100, ("ltp_favourite", "seeded_random_runner")))
        bets = [bet("w", "1.1", variant="ltp_favourite"), bet("l", "1.2", variant="ltp_favourite")]
        outcomes = [outcome("1.1"), outcome("1.2", runners={1: runner("LOSER", "2.0"), 2: runner("WINNER", "3")})]
        self.assertEqual(total_net(settle(bets, outcomes, rules)), -2)
        five = AccountingRules.from_contract(draft())
        self.assertEqual(five.commission_rate, Decimal("0.05"))
        self.assertEqual(total_net(settle(bets, outcomes, five)), -5)


class UnresolvedCases(unittest.TestCase):
    """Every reason, alone, on a bet of 100p on runner 1; nothing is settled as zero or dropped."""

    CASES = (
        ("outcome_incomplete", dict(read_complete=False), None),
        ("outcome_completeness_unknown", dict(read_complete=None), None),
        ("market_not_closed", dict(closed=False), None),
        ("closed_unknown", dict(closed=None), None),
        ("market_void", dict(market_void=True), None),
        ("void_unknown", dict(market_void=None), None),
        ("result_revised_after_close", dict(result_revised_after_close=True), None),
        ("revision_unknown", dict(result_revised_after_close=None), None),
        ("bsp_not_reconciled", dict(bsp_reconciled=False), None),
        ("bsp_reconciled_unknown", dict(bsp_reconciled=None), None),
        ("not_single_winner_market", dict(declared_winners=2), None),
        ("dead_heat", dict(dead_heat=True), None),
        ("dead_heat_unknown", dict(dead_heat=None), None),
        ("removals_unknown", dict(removed_after_decision=None), None),
        ("other_runner_removed_after_decision", dict(removed_after_decision={3}),
         {1: ("WINNER", "2.0"), 2: ("LOSER", "3.0"), 3: ("REMOVED", None)}),
        ("other_runner_status_unexpected", {}, {1: ("WINNER", "2.0"), 2: ("ACTIVE", "3.0")}),
        ("own_runner_removed_after_decision", dict(removed_after_decision={1}),
         {1: ("REMOVED", None), 2: ("WINNER", "3.0")}),
        ("own_runner_not_listed", {}, {2: ("WINNER", "3.0")}),
        ("own_status_unknown", {}, {1: (None, "2.0"), 2: ("WINNER", "3.0")}),
        ("own_status_unexpected", {}, {1: ("PLACED", "2.0"), 2: ("WINNER", "3.0")}),
        ("bsp_missing", {}, {1: ("WINNER", None), 2: ("LOSER", "3.0")}),
        ("bsp_out_of_range", {}, {1: ("WINNER", "1001"), 2: ("LOSER", "3.0")}),
    )

    def settle_one(self, changes, runners):
        runners = None if runners is None else {k: runner(*v) for k, v in runners.items()}
        return settle([bet("d")], [outcome(runners=runners, **changes)], TWO_PERCENT)

    def test_each_reason_alone(self):
        covered = set()
        for reason, changes, runners in self.CASES:
            with self.subTest(reason=reason):
                result = self.settle_one(changes, runners)
                (only,), (market,) = result.bets, result.markets
                self.assertEqual((only.status, only.reason, only.reasons), (UNRESOLVED, reason, (reason,)))
                self.assertEqual((only.gross_pence, only.bsp), (None, None))
                self.assertEqual(only.settlement_case, SETTLEMENT_CASE[reason])
                self.assertEqual((market.resolved, market.gross_pence, market.commission_pence, market.net_pence),
                                 (False, None, None, None))
                self.assertEqual((market.unresolved_stake_pence, market.resolved_stake_pence), (100, 0))
                self.assertEqual(market.reasons, (reason,))
                covered.add(reason)
        self.assertEqual(covered | {"no_winner_in_result", "market_partly_unresolved"}, set(UNRESOLVED_REASONS))

    def test_a_loser_needs_its_bsp_too(self):
        for bsp, reason in ((None, "bsp_missing"), ("1001", "bsp_out_of_range"), ("0", "bsp_out_of_range")):
            with self.subTest(bsp=bsp):
                result = self.settle_one({}, {1: ("LOSER", bsp), 2: ("WINNER", "3.0")})
                self.assertEqual((result.bets[0].status, result.bets[0].reason), (UNRESOLVED, reason))

    def test_more_winners_than_declared_is_a_dead_heat_even_when_the_flag_says_no(self):
        result = self.settle_one(dict(dead_heat=False), {1: ("WINNER", "2.0"), 2: ("WINNER", "3.0")})
        self.assertEqual(result.bets[0].reasons, ("dead_heat",))
        result = self.settle_one(dict(dead_heat=None), {1: ("WINNER", "2.0"), 2: ("WINNER", "3.0")})
        self.assertEqual(result.bets[0].reasons, ("dead_heat",))

    def test_an_unknown_or_non_single_winner_count_is_never_settled(self):
        two = {1: ("WINNER", "2.0"), 2: ("WINNER", "3.0"), 3: ("LOSER", "4.0")}
        one = {1: ("WINNER", "3.0"), 2: ("LOSER", "4.0")}
        for declared, runners, reasons in ((None, two, ("dead_heat_unknown",)), (None, one, ("dead_heat_unknown",)),
                                           (0, two, ("not_single_winner_market",)),
                                           (0, one, ("not_single_winner_market",)),
                                           (2, two, ("not_single_winner_market",)),
                                           (2, {**two, 3: ("WINNER", "4.0")},
                                            ("not_single_winner_market", "dead_heat")),
                                           (3, {1: ("WINNER", "5"), 2: ("PLACED", "3"), 3: ("PLACED", "4")},
                                            ("not_single_winner_market", "other_runner_status_unexpected"))):
            with self.subTest(declared=declared, runners=runners):
                result = self.settle_one(dict(dead_heat=False, declared_winners=declared), runners)
                (only,) = result.bets
                self.assertEqual((only.status, only.reasons, only.gross_pence), (UNRESOLVED, reasons, None))
                self.assertEqual(result.markets[0].unresolved_stake_pence, 100)
        self.assertEqual(self.settle_one({}, one).bets[0].status, WON)            # the same market, declared 1

    def test_another_listed_runner_without_a_final_status_blocks_the_winner_count(self):
        for status in (None, "ACTIVE", "PLACED", "WINNER?", ""):
            with self.subTest(status=status):
                result = self.settle_one({}, {1: ("WINNER", "5"), 2: ("LOSER", "3.0"), 3: (status, None)})
                self.assertEqual(result.bets[0].reasons, ("other_runner_status_unexpected",))
        for status in ("LOSER", "REMOVED", "REMOVED_VACANT", "HIDDEN"):
            with self.subTest(status=status):
                result = self.settle_one({}, {1: ("WINNER", "5"), 2: ("LOSER", "3.0"), 3: (status, None)})
                self.assertEqual((result.bets[0].status, result.bets[0].gross_pence), (WON, 400))
        result = self.settle_one(dict(closed=False), {1: ("WINNER", "5"), 2: ("ACTIVE", None)})
        self.assertEqual(result.bets[0].reasons, ("market_not_closed",))     # an open market's ACTIVE is no news

    def test_a_runner_without_a_final_status_blocks_every_other_variant_in_its_market(self):
        runners = {1: runner("WINNER", "2.0"), 2: runner(None, "3.0"), 3: runner("LOSER", "4.0")}
        result = settle([bet("a", selection_id=1, variant="a"), bet("b", selection_id=2, variant="b")],
                        [outcome(runners=runners)], TWO_PERCENT)
        bets = by_id(result)
        self.assertEqual(bets["a"].reasons, ("other_runner_status_unexpected",))
        self.assertEqual(bets["b"].reasons, ("own_status_unknown",))

    def test_a_closed_market_without_a_winner_is_unresolved_not_void(self):
        result = self.settle_one({}, {1: ("LOSER", "2.0"), 2: ("LOSER", "3.0")})
        self.assertEqual((result.bets[0].reason, result.bets[0].settlement_case),
                         ("no_winner_in_result", "missing_bsp_or_result"))

    def test_an_empty_outcome_is_unresolved_never_void_never_zero(self):
        result = settle([bet("d")], [MarketOutcome.unknown("1.1", OFF)], TWO_PERCENT)
        (only,) = result.bets
        self.assertEqual(only.reasons, ("outcome_completeness_unknown", "closed_unknown", "void_unknown",
                                        "revision_unknown", "bsp_reconciled_unknown", "dead_heat_unknown",
                                        "removals_unknown", "own_runner_not_listed"))
        self.assertNotIn("market_void", only.reasons)
        self.assertEqual({SETTLEMENT_CASE[r] for r in only.reasons}, {"missing_bsp_or_result"})
        self.assertEqual((only.gross_pence, result.markets[0].unresolved_stake_pence), (None, 100))

    def test_every_applicable_reason_is_listed_in_precedence_order(self):
        result = self.settle_one(dict(closed=False, bsp_reconciled=False, removed_after_decision={1}),
                                 {1: ("REMOVED", None), 2: ("WINNER", "3.0")})
        self.assertEqual(result.bets[0].reasons, ("market_not_closed", "bsp_not_reconciled",
                                                  "own_runner_removed_after_decision"))
        self.assertEqual(result.bets[0].reason, "market_not_closed")

    def test_the_removal_set_counts_even_when_the_status_still_shows_a_result(self):
        result = self.settle_one(dict(removed_after_decision={1}), {1: ("WINNER", "2.0"), 2: ("LOSER", "3.0")})
        self.assertEqual(result.bets[0].reasons, ("own_runner_removed_after_decision",))

    def test_a_removed_status_counts_even_when_the_removal_set_misses_it(self):
        result = self.settle_one({}, {1: ("REMOVED_VACANT", None), 2: ("WINNER", "3.0")})
        self.assertEqual(result.bets[0].reasons, ("own_runner_removed_after_decision",))

    def test_every_reason_maps_to_a_contract_case_and_every_case_is_reachable(self):
        cases = {case for case in SETTLEMENT_CASE.values() if case is not None}
        self.assertEqual(cases, set(SETTLEMENT_CASES))
        self.assertEqual([r for r, case in SETTLEMENT_CASE.items() if case is None], ["market_partly_unresolved"])
        unknown = [r for r in UNRESOLVED_REASONS if r.endswith("_unknown")]
        self.assertEqual({SETTLEMENT_CASE[r] for r in unknown}, {"missing_bsp_or_result"})


class Precision(unittest.TestCase):
    def settle_bsp(self, bsp, stake=100, status="WINNER"):
        other = "LOSER" if status == "WINNER" else "WINNER"
        runners = {1: RunnerOutcome(status, bsp), 2: runner(other, "3.0")}
        return settle([bet("d", stake_pence=stake)], [outcome(runners=runners)], TWO_PERCENT).bets[0]

    def test_supplied_precision_decides_the_penny(self):
        self.assertEqual(self.settle_bsp(Decimal("3.17")).gross_pence, 217)
        self.assertEqual(self.settle_bsp(Decimal("3.17000")).gross_pence, 217)
        self.assertEqual(self.settle_bsp(Decimal("3.1699")).gross_pence, 216)
        self.assertEqual(self.settle_bsp(Decimal("3.17999999")).gross_pence, 217)
        self.assertEqual(self.settle_bsp(Decimal("3.16994321")).bsp, Decimal("3.16994321"))

    def test_sentinel_and_ladder_bounds(self):
        self.assertEqual(self.settle_bsp(Decimal("1001")).reason, "bsp_out_of_range")
        self.assertEqual(self.settle_bsp(Decimal("1000.01")).reason, "bsp_out_of_range")
        self.assertEqual(self.settle_bsp(Decimal("1000")).gross_pence, 99900)
        self.assertEqual(self.settle_bsp(Decimal("1.01")).gross_pence, 1)
        self.assertEqual(self.settle_bsp(Decimal("1.009")).reason, "bsp_out_of_range")
        self.assertEqual(self.settle_bsp(Decimal("0")).reason, "bsp_out_of_range")
        self.assertEqual(self.settle_bsp(Decimal("-2")).reason, "bsp_out_of_range")

    def test_floats_and_binary_expansions_are_refused(self):
        for bad, code in ((3.17, "bsp_not_decimal"), (Decimal(3.17), "bsp_too_precise"), ("3.17", "bsp_not_decimal"),
                          (Decimal("NaN"), "bsp_not_finite"), (Decimal("Infinity"), "bsp_not_finite"),
                          (317, "bsp_not_decimal"), (True, "bsp_not_decimal")):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, f"^{code}$"):
                RunnerOutcome("WINNER", bad)
        self.assertEqual(win_gross_pence(100, Decimal(3.17)), 216)       # what a leaked float would have cost

    def test_any_float_literal_the_index_can_supply_is_kept_not_refused(self):
        long = decimal_from_json_number(3.3333333333333335)              # 16 places, the most repr gives here
        self.assertEqual(str(long), "3.3333333333333335")
        self.assertEqual(self.settle_bsp(long).gross_pence, 233)
        self.assertEqual(self.settle_bsp(decimal_from_json_number(1.0100000000000002)).gross_pence, 1)
        self.assertEqual(self.settle_bsp(Decimal("2." + "1" * 20)).gross_pence, 111)      # MAX_BSP_PLACES, kept
        with self.assertRaisesRegex(ValueError, "^bsp_too_precise$"):
            RunnerOutcome("WINNER", Decimal("2." + "1" * 21))

    def test_json_numbers_keep_the_literal(self):
        self.assertEqual(str(decimal_from_json_number(3.17)), "3.17")
        self.assertEqual(str(decimal_from_json_number(json.loads("3.16994321"))), "3.16994321")
        self.assertEqual(str(decimal_from_json_number(1001)), "1001")
        self.assertEqual(str(decimal_from_json_number("3.1700")), "3.1700")
        self.assertIsNone(decimal_from_json_number(None))
        for bad in (float("nan"), float("inf"), True, "3,17", "", [3.17]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                decimal_from_json_number(bad)


class Rounding(unittest.TestCase):
    def test_a_win_rounds_down_to_the_penny(self):
        self.assertEqual(win_gross_pence(1, Decimal("1.01")), 0)          # 0.01p
        self.assertEqual(win_gross_pence(3, Decimal("1.5")), 1)           # 1.5p
        self.assertEqual(win_gross_pence(7, Decimal("2.43")), 10)         # 10.01p
        self.assertEqual(win_gross_pence(100, Decimal("1.999999")), 99)   # 99.9999p
        self.assertEqual(win_gross_pence(100000, Decimal("999.9999999999")), 99899999)

    def test_a_tiny_win_is_won_with_zero_gross_not_unresolved(self):
        runners = {1: runner("WINNER", "1.01"), 2: runner("LOSER", "3.0")}
        result = settle([bet("d", stake_pence=1)], [outcome(runners=runners)], TWO_PERCENT)
        self.assertEqual((result.bets[0].status, result.bets[0].gross_pence), (WON, 0))
        self.assertEqual((result.markets[0].commission_pence, result.markets[0].net_pence), (0, 0))

    def test_commission_rounding_per_contract_token(self):
        ceil, half = "per_market_ceil_penny", "per_market_half_up_penny"
        two = Decimal("0.02")
        for net, up, half_up in ((101, 3, 2), (25, 1, 1), (75, 2, 2), (50, 1, 1), (1, 1, 0), (0, 0, 0),
                                 (-500, 0, 0)):
            with self.subTest(net=net):
                self.assertEqual(commission_pence(net, two, ceil), up)
                self.assertEqual(commission_pence(net, two, half), half_up)

    def test_commission_is_exact_not_float(self):
        rate = Decimal(repr(0.07))
        self.assertEqual(commission_pence(100, rate, "per_market_ceil_penny"), 7)     # float: ceil(7.000000000000001)
        self.assertEqual(commission_pence(100, Decimal("0"), "per_market_ceil_penny"), 0)
        with self.assertRaises(ValueError):
            commission_pence(100.0, rate, "per_market_ceil_penny")

    def test_commission_is_charged_on_the_market_net_not_per_bet(self):
        runners = {1: runner("WINNER", "2.01"), 2: runner("LOSER", "2.01")}
        result = settle([bet("a", selection_id=1), bet("b", selection_id=1)], [outcome(runners=runners)], TWO_PERCENT)
        (market,) = result.markets
        self.assertEqual((market.gross_pence, market.commission_pence, market.net_pence), (202, 5, 197))  # not 3+3
        result = settle([bet("a", selection_id=1), bet("b", selection_id=2, stake_pence=50)],
                        [outcome(runners=runners)], TWO_PERCENT)
        (market,) = result.markets
        self.assertEqual((market.gross_pence, market.commission_pence, market.net_pence), (51, 2, 49))     # not 3+0


class MixedMarkets(unittest.TestCase):
    def test_a_market_with_one_unresolved_bet_is_unresolved_whole(self):
        runners = {1: runner("WINNER", "2.0"), 2: runner("LOSER", None)}
        result = settle([bet("good", selection_id=1, stake_pence=300), bet("bad", selection_id=2, stake_pence=200)],
                        [outcome(runners=runners)], TWO_PERCENT)
        bets = by_id(result)
        self.assertEqual((bets["bad"].status, bets["bad"].reasons), (UNRESOLVED, ("bsp_missing",)))
        self.assertEqual((bets["good"].status, bets["good"].reasons, bets["good"].settlement_case),
                         (UNRESOLVED, ("market_partly_unresolved",), None))
        self.assertEqual((bets["good"].gross_pence, bets["good"].bsp), (None, None))
        (market,) = result.markets
        self.assertEqual((market.resolved, market.commission_pence, market.net_pence, market.unresolved_stake_pence),
                         (False, None, None, 500))
        self.assertEqual(market.reasons, ("bsp_missing", "market_partly_unresolved"))

    def test_another_variant_in_the_same_market_is_not_affected(self):
        runners = {1: runner("WINNER", "2.0"), 2: runner("LOSER", None)}
        result = settle([bet("a1", selection_id=1, variant="a"), bet("a2", selection_id=2, variant="a"),
                         bet("b1", selection_id=1, variant="b")], [outcome(runners=runners)], TWO_PERCENT)
        markets = {m.variant: m for m in result.markets}
        self.assertFalse(markets["a"].resolved)
        self.assertEqual((markets["b"].resolved, markets["b"].net_pence), (True, 98))

    def test_candidate_and_baseline_are_never_netted_for_commission(self):
        result = settle([bet("cand", selection_id=1, stake_pence=1000, variant="candidate"),
                         bet("base", selection_id=2, stake_pence=1000, variant="baseline")],
                        [outcome(runners={1: runner("WINNER", "2.0"), 2: runner("LOSER", "2.0")})], TWO_PERCENT)
        markets = {m.variant: m for m in result.markets}
        self.assertEqual((markets["candidate"].commission_pence, markets["candidate"].net_pence), (20, 980))
        self.assertEqual((markets["baseline"].commission_pence, markets["baseline"].net_pence), (0, -1000))


class Inputs(unittest.TestCase):
    def test_issued_bet_refusals(self):
        good = dict(decision_id="d", variant="v", market_id="1.1", selection_id=1, stake_pence=100, side="BACK",
                    basis="BSP")
        for change, code in ((dict(stake_pence=0), "stake_pence_not_a_positive_integer"),
                             (dict(stake_pence=-100), "stake_pence_not_a_positive_integer"),
                             (dict(stake_pence=True), "stake_pence_not_a_positive_integer"),
                             (dict(stake_pence=1.0), "stake_pence_not_a_positive_integer"),
                             (dict(stake_pence=100001), "stake_pence_not_a_positive_integer"),
                             (dict(selection_id=0), "selection_id_not_a_positive_integer"),
                             (dict(side="LAY"), "side_not_supported"), (dict(basis="LIMIT"), "basis_not_supported"),
                             (dict(basis="LIMIT_ON_CLOSE"), "basis_not_supported"),
                             (dict(market_id="1.x"), "market_id_malformed"),
                             (dict(market_id=1.1), "market_id_malformed"), (dict(variant="Bad"), "variant_malformed"),
                             (dict(decision_id="a b"), "decision_id_malformed"),
                             (dict(decision_id=""), "decision_id_malformed")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, f"^{code}$"):
                IssuedBet(**dict(good, **change))

    def test_market_outcome_refusals(self):
        for change, code in ((dict(closed=1), "closed_not_true_false_or_none"),
                             (dict(market_void="no"), "market_void_not_true_false_or_none"),
                             (dict(declared_winners=True), "declared_winners_not_a_count"),
                             (dict(declared_winners=-1), "declared_winners_not_a_count"),
                             (dict(removed_after_decision={0}), "removed_selection_id_not_a_positive_integer"),
                             (dict(removed_after_decision="1"), "removed_after_decision_not_a_set"),
                             (dict(off=datetime(2015, 6, 1, 13, 30)), "first_published_off_not_aware"),
                             (dict(off=datetime(1995, 6, 1, tzinfo=timezone.utc)), "before_london_rule"),
                             (dict(market_id="1"), "market_id_malformed"),
                             (dict(runners=[(1, runner("WINNER", "2"))]), "runners_not_a_mapping"),
                             (dict(runners={True: runner("WINNER", "2")}),
                              "runner_selection_id_not_a_positive_integer"),
                             (dict(runners={1: ("WINNER", "2")}), "runner_not_a_runner_outcome")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, f"^{code}$"):
                outcome(**change)
        for bad in (5, b"WINNER", True):
            with self.subTest(status=bad), self.assertRaisesRegex(ValueError, "^status_malformed$"):
                RunnerOutcome(bad, None)

    def test_any_status_text_is_accepted_and_unexpected_text_is_unresolved_not_refused(self):
        for status in ("WIN NER", "REMOVED VACANT", "", "X" * 33, "WINNER\n", "勝"):
            with self.subTest(status=status):
                runners = {1: RunnerOutcome(status, Decimal("2.0")), 2: runner("WINNER", "3.0")}
                result = settle([bet("d")], [outcome(runners=runners)], TWO_PERCENT)
                self.assertEqual((result.bets[0].status, result.bets[0].reasons),
                                 (UNRESOLVED, ("own_status_unexpected",)))

    def test_variants_share_a_decision_id_but_one_variant_may_not_repeat_it(self):
        rules = AccountingRules.from_contract(draft())
        both = settle([bet("m1|r1|t", variant="ltp_favourite"), bet("m1|r1|t", variant="seeded_random_runner")],
                      [outcome("1.1")], rules)
        self.assertEqual([(b.variant, b.decision_id, b.status) for b in both.bets],
                         [("ltp_favourite", "m1|r1|t", WON), ("seeded_random_runner", "m1|r1|t", WON)])
        self.assertEqual([m.variant for m in both.markets], ["ltp_favourite", "seeded_random_runner"])
        with self.assertRaisesRegex(ValueError, "^duplicate_decision_id:seeded_random_runner:m1\\|r1\\|t$"):
            settle([bet("m1|r1|t", variant="seeded_random_runner"),
                    bet("m1|r1|t", "1.2", variant="seeded_random_runner")], [outcome("1.1"), outcome("1.2")], rules)

    def test_settle_refuses_inconsistent_input_instead_of_dropping(self):
        rules = AccountingRules.from_contract(draft())
        cases = (([bet("d", variant="ltp_favourite"), bet("d", "1.2", variant="ltp_favourite")],
                  [outcome("1.1"), outcome("1.2")], "duplicate_decision_id:ltp_favourite:d"),
                 ([bet("d", "1.9", variant="ltp_favourite")], [outcome("1.1")], "missing_market_outcome:1.9"),
                 ([bet("d", variant="ltp_favourite")], [outcome("1.1"), outcome("1.1")],
                  "duplicate_market_outcome:1.1"),
                 ([bet("d", variant="other")], [outcome("1.1")], "undeclared_variant:other"),
                 ([bet("d", stake_pence=200, variant="ltp_favourite")], [outcome("1.1")],
                  "stake_not_the_fixed_stake:ltp_favourite:d"))
        for bets, outcomes, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, f"^{code}$"):
                settle(bets, outcomes, rules)

    def test_rules_refuse_what_the_code_cannot_settle(self):
        for change, code in ((dict(commission_rate=0.05), "commission_rate_not_a_decimal_fraction"),
                             (dict(commission_rate=Decimal("1")), "commission_rate_not_a_decimal_fraction"),
                             (dict(commission_rate=Decimal("-0.01")), "commission_rate_not_a_decimal_fraction"),
                             (dict(commission_rate=Decimal("NaN")), "commission_rate_not_a_decimal_fraction"),
                             (dict(commission_rate=Decimal(0.05)), "commission_rate_too_precise"),
                             (dict(rounding="per_market_floor_penny"), "rounding_not_supported"),
                             (dict(settlement=dict.fromkeys(SETTLEMENT_CASES, "void_refund")),
                              "settlement_rule_not_supported"),
                             (dict(settlement={"dead_heat": "unresolved"}), "settlement_cases_not_the_contract_cases"),
                             (dict(stake_pence=0), "stake_pence_not_a_positive_integer"),
                             (dict(variants=[]), "variants_malformed"), (dict(variants="abc"), "variants_malformed"),
                             (dict(variants=b"abc"), "variants_malformed"),
                             (dict(variants=["ok", "Bad"]), "variants_malformed")):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, f"^{code}$"):
                AccountingRules(**dict(dict(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny"),
                                       **change))
        bad = draft()
        bad["costs"]["rate_source"] = "market_base_rate"
        with self.assertRaisesRegex(ValueError, "^contract_invalid$"):
            AccountingRules.from_contract(bad)

    def test_output_is_chronological_and_independent_of_input_order(self):
        outcomes = [outcome("1.10", OFF), outcome("1.9", OFF), outcome("1.2", OFF - timedelta(hours=1)),
                    outcome("1.3", OFF + timedelta(hours=1))]
        bets = [bet(f"d{m.market_id}{v}", m.market_id, variant=v) for m in outcomes for v in ("b", "a")]
        first = settle(bets, outcomes, TWO_PERCENT)
        self.assertEqual(sorted(b.decision_id for b in first.bets), sorted(b.decision_id for b in bets))
        self.assertEqual([(m.market_id, m.variant) for m in first.markets],
                         [("1.2", "a"), ("1.2", "b"), ("1.9", "a"), ("1.9", "b"), ("1.10", "a"), ("1.10", "b"),
                          ("1.3", "a"), ("1.3", "b")])
        shuffled_bets, shuffled_outcomes = list(bets), list(outcomes)
        random.Random(7).shuffle(shuffled_bets)
        random.Random(8).shuffle(shuffled_outcomes)
        self.assertEqual(settle(shuffled_bets, shuffled_outcomes, TWO_PERCENT), first)

    def test_ids_equal_as_numbers_still_have_one_order(self):
        outcomes, bets = [outcome("1.01"), outcome("1.1")], [bet("a", "1.01"), bet("b", "1.1")]
        forward = settle(bets, outcomes, TWO_PERCENT)
        self.assertEqual([m.market_id for m in forward.markets], ["1.01", "1.1"])
        self.assertEqual(settle(bets[::-1], outcomes[::-1], TWO_PERCENT), forward)


class RaceDay(unittest.TestCase):
    def test_london_dates_around_midnight_and_the_changes(self):
        utc = timezone.utc
        cases = ((datetime(2015, 6, 1, 23, 30, tzinfo=utc), date(2015, 6, 2)),       # 00:30 BST
                 (datetime(2015, 12, 1, 23, 30, tzinfo=utc), date(2015, 12, 1)),     # GMT
                 (datetime(2016, 3, 26, 23, 59, tzinfo=utc), date(2016, 3, 26)),     # BST starts 2016-03-27 01:00Z
                 (datetime(2016, 3, 27, 0, 59, tzinfo=utc), date(2016, 3, 27)),
                 (datetime(2016, 3, 27, 23, 0, tzinfo=utc), date(2016, 3, 28)),
                 (datetime(2015, 10, 24, 23, 30, tzinfo=utc), date(2015, 10, 25)),   # BST until 2015-10-25 01:00Z
                 (datetime(2015, 10, 25, 23, 30, tzinfo=utc), date(2015, 10, 25)),
                 (datetime(2015, 6, 1, 13, 30, tzinfo=timezone(timedelta(hours=-5))), date(2015, 6, 1)))
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(race_day_of(moment), expected)
        self.assertEqual(outcome(off=datetime(2015, 6, 1, 23, 30, tzinfo=utc)).race_day, date(2015, 6, 2))

    @unittest.skipIf(ZoneInfo is None, "zoneinfo unavailable")
    def test_rule_matches_the_tz_database_from_1996(self):
        try:
            london = ZoneInfo("Europe/London")
        except ZoneInfoNotFoundError:                                          # pragma: no cover
            self.skipTest("no Europe/London tz data")
        moments = []
        for year in range(1996, 2038):
            day = datetime(year, 1, 1, 23, 30, tzinfo=timezone.utc)
            while day.year == year:
                moments.append(day)
                day += timedelta(days=1)
            for month in (3, 10):
                start = datetime(year, month, 24, tzinfo=timezone.utc)
                moments += [start + timedelta(minutes=15 * k) for k in range(8 * 24 * 4)]
        mismatches = [m for m in moments if race_day_of(m) != m.astimezone(london).date()]
        self.assertEqual(mismatches, [])

    def test_before_the_rule_and_timestamp_text(self):
        with self.assertRaisesRegex(ValueError, "^before_london_rule$"):
            race_day_of(datetime(1995, 12, 31, tzinfo=timezone.utc))
        for naive in (datetime(2015, 6, 1, 23, 30), date(2015, 6, 1), "2015-06-01T23:30:00Z"):
            with self.subTest(naive=naive), self.assertRaisesRegex(ValueError, "^moment_not_aware$"):
                race_day_of(naive)                                     # never the host's local zone
        self.assertEqual(utc_from_text("2015-05-01T13:30:00.000Z"), datetime(2015, 5, 1, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(utc_from_text("2015-05-01T13:30:00.5Z").microsecond, 500000)
        self.assertEqual(utc_from_text("2015-05-01T13:30:00Z").tzinfo, timezone.utc)
        for bad in ("2015-05-01T13:30:00", "2015-05-01 13:30:00Z", "2015-02-30T13:30:00Z", 20150501, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                utc_from_text(bad)


class Boundary(unittest.TestCase):
    def test_module_has_no_clock_or_randomness(self):
        source = Path(accounting.__file__).read_text(encoding="utf-8")
        for word in ("import random", "import time", "import os", "now(", "today("):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
