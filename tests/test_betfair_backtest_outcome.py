"""The evaluator's accounting inputs (betangel_research.betfair.backtest.outcome) from synthetic archives.

Every market goes through the real ingest, index, replay and cohort (helpers from tests/test_betfair_backtest_replay.py
and tests/test_betfair_backtest_cohort.py). These show the field mapping of market_outcome, the removal union relative
to the actual decision time, the decimal BSP conversion, unknowns staying unknown, and that a P0 bet on a normally
settled BASIC-shaped market is unresolved because market_void is never inferred (D11, Q5). They say nothing about real
files or results.
"""

import dataclasses
from datetime import timedelta
from decimal import Decimal
import json
import unittest

from betangel_research.betfair.backtest.accounting import AccountingRules, IssuedBet, settle
from betangel_research.betfair.backtest.outcome import cohort_ids, issued_bets, market_outcome

import test_betfair_backtest_replay as fx
from test_betfair_backtest_cohort import CohortCase
from test_betfair_backtest_replay import OFF, T, definition, md, ms, path, prices, runner

M = "1.100000001"


def settled(statuses, bsps, off=OFF, **closed_extra):
    final = [runner(rid, statuses[rid], bsps.get(rid)) for rid in sorted(statuses)]
    return [md(M, off + timedelta(minutes=2), definition(off, final, in_play=True, bspReconciled=True)),
            md(M, off + timedelta(minutes=8), definition(off, final, status="CLOSED", in_play=True, bspReconciled=True,
                                                         settledTime=fx.iso(off + timedelta(minutes=8)),
                                                         **closed_extra))]


class OutcomeCase(CohortCase):
    def decided(self, messages):
        """(record, MarketDecision) of one synthetic market."""
        root, manifest, records = self.build([(path(M), messages)])
        decision = self.decide([(path(M), messages)])[M]
        return records[M], decision

    def pre(self, runners=(1, 2, 3), ltps=None):
        return [md(M, OFF - timedelta(hours=2), definition(OFF, [runner(rid) for rid in runners])),
                prices(M, OFF - timedelta(minutes=10), ltps or {1: 3.0, 2: 2.5, 3: 6.0})]


class MappingTests(OutcomeCase):
    def test_a_normally_settled_market(self):
        record, decision = self.decided(self.pre() + settled({1: "LOSER", 2: "WINNER", 3: "LOSER"},
                                                             {1: 3.1, 2: 3.14159265, 3: 7}))
        outcome = market_outcome(record, decision)
        self.assertEqual((outcome.read_complete, outcome.closed, outcome.market_void,
                          outcome.result_revised_after_close, outcome.bsp_reconciled, outcome.dead_heat,
                          outcome.declared_winners),
                         (True, True, None, False, True, False, 1))
        self.assertEqual(outcome.removed_after_decision, frozenset())
        self.assertEqual([(rid, r.status, r.bsp) for rid, r in outcome.runners],
                         [(1, "LOSER", Decimal("3.1")), (2, "WINNER", Decimal("3.14159265")), (3, "LOSER", Decimal(7))])
        self.assertEqual(outcome.first_published_off, OFF)
        self.assertEqual(cohort_ids(decision), (1, 2, 3))

    def test_removals_after_t_in_the_window_and_after_the_cutoff_are_both_counted(self):
        pre = [md(M, OFF - timedelta(hours=2), definition(OFF, [runner(rid) for rid in (1, 2, 3, 4)])),
               md(M, OFF - timedelta(minutes=20),
                  definition(OFF, [runner(1), runner(2), runner(3), runner(4, "REMOVED")])),
               prices(M, OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0})]
        window = md(M, OFF - timedelta(minutes=2), definition(OFF, [runner(1), runner(2), runner(3, "REMOVED"),
                                                                    runner(4, "REMOVED")]))
        after_off = md(M, OFF + timedelta(minutes=1), definition(OFF, [runner(1), runner(2, "REMOVED"),
                                                                       runner(3, "REMOVED"), runner(4, "REMOVED")]))
        record, decision = self.decided(pre + [window, after_off] + settled(
            {1: "WINNER", 2: "REMOVED", 3: "REMOVED", 4: "REMOVED"}, {1: 2.0}))
        self.assertEqual(cohort_ids(decision), (1, 2, 3))              # runner 4 was removed before t
        self.assertIn(3, [item["id"] for item in record["decision_time"]["removals"]])
        self.assertIn(2, [item["id"] for item in record["evaluator_only_outcome"]["removals_after_cutoff"]])
        outcome = market_outcome(record, decision)
        self.assertEqual(outcome.removed_after_decision, frozenset({2, 3}))
        only_one = dataclasses.replace(decision, rows=tuple(r for r in decision.rows if r["selection_id"] == 1))
        self.assertEqual(market_outcome(record, only_one).removed_after_decision, frozenset())

    def test_an_incomplete_read_leaves_removals_unknown(self):
        record, decision = self.decided(self.pre() + [b"{not json\n"] + settled({1: "WINNER", 2: "LOSER", 3: "LOSER"},
                                                                               {1: 2.0, 2: 3.0, 3: 4.0}))
        outcome = market_outcome(record, decision)
        self.assertEqual((outcome.read_complete, outcome.removed_after_decision), (False, None))

    def test_a_market_never_closed_and_unknown_values(self):
        record, decision = self.decided(self.pre())
        outcome = market_outcome(record, decision)
        self.assertEqual((outcome.closed, outcome.bsp_reconciled, outcome.dead_heat,
                          outcome.result_revised_after_close),
                         (False, False, None, None))
        odd = json.loads(json.dumps(record))
        body = odd["evaluator_only_outcome"]
        body.update(final_status=5, declared_number_of_winners=True, dead_heat="no", read_complete="yes")
        body["outcome_definition"]["bspReconciled"] = [[1, "true"]]
        body["final_runners"][0].update(status=7, bsp="not a number")
        outcome = market_outcome(odd, decision)
        self.assertEqual((outcome.closed, outcome.declared_winners, outcome.dead_heat, outcome.read_complete,
                          outcome.bsp_reconciled), (None, None, None, None, None))
        self.assertEqual((outcome.runner(1).status, outcome.runner(1).bsp), (None, None))

    def test_a_listed_runner_id_accounting_cannot_hold_makes_the_outcome_incomplete(self):
        record, decision = self.decided(self.pre() + settled({1: "WINNER", 2: "LOSER", 3: "LOSER"},
                                                             {1: 2.0, 2: 3.0, 3: 4.0}))
        odd = json.loads(json.dumps(record))
        odd["evaluator_only_outcome"]["final_runners"].append({"id": 0, "status": "LOSER", "bsp": 5.0, "listed": True})
        outcome = market_outcome(odd, decision)
        self.assertEqual((outcome.read_complete, outcome.runners), (None, ()))
        odd["evaluator_only_outcome"]["final_runners"][-1]["listed"] = False       # an unlisted one is no result
        self.assertTrue(market_outcome(odd, decision).read_complete)


    def test_a_cohort_id_accounting_cannot_hold_never_aborts_the_run(self):
        pre = [md(M, OFF - timedelta(hours=2), definition(OFF, [runner(0), runner(1), runner(2)])),
               prices(M, OFF - timedelta(minutes=10), {0: 3.0, 1: 2.5, 2: 6.0}),
               md(M, OFF - timedelta(minutes=2), definition(OFF, [runner(0, "REMOVED"), runner(1), runner(2)]))]
        record, decision = self.decided(pre + settled({0: "REMOVED", 1: "WINNER", 2: "LOSER"}, {1: 2.0, 2: 3.0}))
        self.assertEqual(cohort_ids(decision), (0, 1, 2))
        outcome = market_outcome(record, decision)
        self.assertEqual((outcome.read_complete, outcome.removed_after_decision, outcome.runners), (None, None, ()))
        gone = pre[:2] + settled({1: "WINNER", 2: "LOSER"}, {1: 2.0, 2: 3.0})       # runner 0 not listed at the end
        record, decision = self.decided(gone)
        self.assertEqual(cohort_ids(decision), (0, 1, 2))
        outcome = market_outcome(record, decision)
        self.assertEqual((outcome.read_complete, outcome.removed_after_decision), (None, None))

    def test_a_cohort_runner_missing_at_the_end_leaves_removals_unknown(self):
        record, decision = self.decided(self.pre() + settled({1: "WINNER", 2: "LOSER"}, {1: 2.0, 2: 3.0}))
        self.assertEqual(cohort_ids(decision), (1, 2, 3))
        listed = [r["id"] for r in record["evaluator_only_outcome"]["final_runners"] if r["listed"]]
        self.assertNotIn(3, listed)
        self.assertIsNone(market_outcome(record, decision).removed_after_decision)

    def test_an_unknown_interval_after_t_leaves_removals_unknown_even_when_later_restored(self):
        """Astra's c2b1 review R1: an observed unknown or unlisted interval of a cohort runner after t, in the decision
        window or after the cutoff, stays unknown although the final result lists the runner again."""
        final = settled({1: "LOSER", 2: "WINNER", 3: "LOSER"}, {1: 3.1, 2: 2.4, 3: 7.0})
        three = [runner(1), runner(2), runner(3)]
        gaps = {
            "null status in the window": md(M, OFF - timedelta(minutes=2),
                                            definition(OFF, three[:2] + [runner(3, None)])),
            "null status after the cutoff": md(M, OFF + timedelta(minutes=1),
                                               definition(OFF, three[:2] + [runner(3, None)], in_play=True)),
            "unrecognised text in the window": md(M, OFF - timedelta(minutes=2),
                                                  definition(OFF, three[:2] + [runner(3, "REMOVED?")])),
            "unrecognised text after the cutoff": md(M, OFF + timedelta(minutes=1),
                                                     definition(OFF, three[:2] + [runner(3, "REMOVED?")],
                                                                in_play=True)),
            "an image that unlists it in the window": md(M, OFF - timedelta(minutes=2), definition(OFF, three[:2]),
                                                         img=True),
            "a definition that unlists it after the cutoff": md(M, OFF + timedelta(minutes=1),
                                                                definition(OFF, three[:2], in_play=True)),
        }
        for name, gap in gaps.items():
            with self.subTest(gap=name):
                record, decision = self.decided(self.pre() + [gap] + final)
                self.assertEqual(cohort_ids(decision), (1, 2, 3))
                outcome = market_outcome(record, decision)
                self.assertTrue(outcome.read_complete)
                self.assertIsNone(outcome.removed_after_decision)
                settlement = settle(issued_bets(decision), [outcome], AccountingRules.from_contract(self.contract))
                self.assertEqual(len(settlement.bets), 2)
                for bet in settlement.bets:
                    self.assertEqual(bet.status, "unresolved")
                    self.assertIn("removals_unknown", bet.reasons)
                self.assertEqual(sum(m.unresolved_stake_pence for m in settlement.markets), 200)

    def test_known_states_non_cohort_runners_and_gaps_before_t_are_not_unknowns(self):
        final = settled({1: "LOSER", 2: "WINNER", 3: "LOSER"}, {1: 3.1, 2: 2.4, 3: 7.0})
        three = [runner(1), runner(2), runner(3)]
        carried = self.pre() + [md(M, OFF - timedelta(minutes=2), definition(OFF, three, version=2))] + final
        before_t = [md(M, OFF - timedelta(hours=2), definition(OFF, three)),
                    md(M, OFF - timedelta(minutes=20), definition(OFF, three[:2] + [runner(3, None)])),
                    md(M, OFF - timedelta(minutes=15), definition(OFF, three)),
                    prices(M, OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0})] + final
        removed_first = self.pre() + [
            md(M, OFF - timedelta(minutes=2), definition(OFF, three[:2] + [runner(3, "REMOVED")])),
            md(M, OFF - timedelta(minutes=1), definition(OFF, three[:2] + [runner(3, None)]))]
        removed_first += settled({1: "LOSER", 2: "WINNER", 3: "REMOVED"}, {1: 3.1, 2: 2.4})
        four = [runner(1), runner(2), runner(3), runner(4, "REMOVED")]
        non_cohort = [md(M, OFF - timedelta(hours=2), definition(OFF, four)),
                      prices(M, OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0}),
                      md(M, OFF - timedelta(minutes=2), definition(OFF, three + [runner(4, None)]))]
        non_cohort += settled({1: "LOSER", 2: "WINNER", 3: "LOSER", 4: "REMOVED"}, {1: 3.1, 2: 2.4, 3: 7.0})
        for name, messages, expected in (("carried forward", carried, frozenset()),
                                         ("a gap before t", before_t, frozenset()),
                                         ("removed before the unknown", removed_first, frozenset({3})),
                                         ("a non-cohort runner's gap", non_cohort, frozenset())):
            with self.subTest(case=name):
                record, decision = self.decided(messages)
                self.assertEqual(cohort_ids(decision), (1, 2, 3))
                self.assertEqual(market_outcome(record, decision).removed_after_decision, expected)

    def test_a_record_that_is_not_the_decisions_refuses(self):
        record, decision = self.decided(self.pre() + settled({1: "WINNER", 2: "LOSER", 3: "LOSER"},
                                                             {1: 2.0, 2: 3.0, 3: 4.0}))
        for change in ("market_id", "plan", "anchor_pt", "anchor_ms"):
            with self.subTest(change=change):
                other = dataclasses.replace(decision, **{change: {"market_id": "1.100000009", "plan": "PRO",
                                                                  "anchor_pt": 1, "anchor_ms": 1}[change]})
                with self.assertRaises(ValueError):
                    market_outcome(record, other)


class SettlementTests(OutcomeCase):
    def test_issued_bets_follow_the_ledger(self):
        record, decision = self.decided(self.pre() + settled({1: "LOSER", 2: "WINNER", 3: "LOSER"},
                                                             {1: 3.1, 2: 2.4, 3: 7.0}))
        bets = issued_bets(decision)
        self.assertEqual({bet.variant for bet in bets}, {"ltp_favourite", "seeded_random_runner"})
        favourite = [bet for bet in bets if bet.variant == "ltp_favourite"][0]
        row = [row for row in decision.rows if row["selection_id"] == 2][0]
        self.assertEqual(favourite, IssuedBet(decision_id=row["decision_id"], variant="ltp_favourite", market_id=M,
                                              selection_id=2, stake_pence=100, side="BACK", basis="BSP"))

    def test_a_p0_bet_on_a_settled_basic_market_is_unresolved_because_void_is_unknown(self):
        record, decision = self.decided(self.pre() + settled({1: "LOSER", 2: "WINNER", 3: "LOSER"},
                                                             {1: 3.1, 2: 2.4, 3: 7.0}))
        outcome = market_outcome(record, decision)
        settlement = settle(issued_bets(decision), [outcome], AccountingRules.from_contract(self.contract))
        self.assertEqual({(bet.status, bet.reason) for bet in settlement.bets}, {("unresolved", "void_unknown")})
        self.assertEqual(sum(market.unresolved_stake_pence for market in settlement.markets), 200)


if __name__ == "__main__":
    unittest.main()
