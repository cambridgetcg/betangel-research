"""Own P&L and commission tests on hand-computed synthetic bets (pence, hundredths)."""

import unittest
from unittest.mock import patch

from betangel_research import evaluation
from betangel_research.betfair import pnl

import test_betfair_support as support
import test_betfair_support_orders as orders


def matched(side, price_h, size_p, selection_id=12345, ref="bro200101120000-0123456789abcdef"):
    return orders.order_record(side=side, price_h=price_h, size_p=size_p, state="EXECUTION_COMPLETE",
                               size_matched_p=size_p, average_price_matched_h=price_h, size_remaining_p=0,
                               selection_id=selection_id, customer_order_ref=ref)


class PnlTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_back_lay_win_lose_hand_computed(self):
        # 2.00 @ 3.50: a winning back makes 2.00 x 2.50 = 5.00; a losing back loses the 2.00 stake.
        self.assertEqual(pnl.order_pnl_pence("BACK", 350, 200, "WINNER"), 500)
        self.assertEqual(pnl.order_pnl_pence("BACK", 350, 200, "LOSER"), -200)
        self.assertEqual(pnl.order_pnl_pence("LAY", 350, 200, "WINNER"), -500)
        self.assertEqual(pnl.order_pnl_pence("LAY", 350, 200, "LOSER"), 200)
        # 1.01 @ 1.05: 5.05p is floored for a profit (5) and rounded up for a loss (-6).
        self.assertEqual(pnl.order_pnl_pence("BACK", 105, 101, "WINNER"), 5)
        self.assertEqual(pnl.order_pnl_pence("LAY", 105, 101, "WINNER"), -6)
        self.assertEqual(pnl.order_pnl_pence("BACK", 1000, 0, "WINNER"), 0)
        for bad in (("back", 350, 200, "WINNER"), ("BACK", 350, 200, "REMOVED"), ("BACK", 100, 200, "WINNER"),
                    ("BACK", 3.5, 200, "WINNER"), ("BACK", 350, 2.0, "WINNER"), ("BACK", 350, -1, "LOSER"),
                    ("BACK", 350, True, "LOSER")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pnl.order_pnl_pence(*bad)

    def test_compare_profit_min_and_mismatch(self):
        self.assertEqual(pnl.compare_profit(500, 499), {"used": 499, "mismatch": False})
        self.assertEqual(pnl.compare_profit(499, 500), {"used": 499, "mismatch": False})
        self.assertEqual(pnl.compare_profit(500, 498), {"used": 498, "mismatch": True})
        self.assertEqual(pnl.compare_profit(-200, -300), {"used": -300, "mismatch": True})
        self.assertEqual(pnl.compare_profit(500, None), {"used": 500, "mismatch": False})
        for bad in ((None, 1), (1.0, 1), (1, 1.0), (True, 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pnl.compare_profit(*bad)

    def test_commission_via_settled_market_costs(self):
        bets = [matched("BACK", 350, 200), matched("LAY", 300, 100, selection_id=999,
                                                   ref="bro200101120000-00000000000000ee")]
        results = {12345: "WINNER", 999: "LOSER"}
        with patch.object(evaluation, "settled_market_costs", wraps=evaluation.settled_market_costs) as costs:
            realized = pnl.market_realized(bets, results, commission_rate=0.05)
        costs.assert_called_once_with({support.MARKET_ID: 600}, 0.05)
        # gross 500 + 100 = 600; commission 5% = 30; net 570.
        self.assertEqual(realized, {"gross_pence": 600, "commission_pence": 30, "net_pence": 570,
                                    "excluded_reason": None})
        losing = pnl.market_realized(bets, {12345: "LOSER", 999: "WINNER"}, commission_rate=0.05)
        self.assertEqual(losing, {"gross_pence": -400, "commission_pence": 0, "net_pence": -400,
                                  "excluded_reason": None})
        # Float noise (300 x 0.07 = 21.000000000000004) never adds a penny; a real fraction rounds up.
        self.assertEqual(300 * 0.07, 21.000000000000004)
        self.assertEqual(pnl.market_realized([matched("LAY", 400, 300)], {12345: "LOSER", 1: "WINNER"},
                                             commission_rate=0.07)["commission_pence"], 21)
        self.assertEqual(pnl.market_realized([matched("BACK", 350, 101)], {12345: "WINNER"},
                                             commission_rate=0.05)["commission_pence"], 13)
        self.assertEqual(pnl.market_realized([], {}, commission_rate=0.05)["net_pence"], 0)

    def test_excluded_markets_carry_no_figures(self):
        bet = [matched("BACK", 350, 200)]
        cases = {"rule4_adjustment_not_modelled": {12345: "WINNER", 2: "REMOVED"},
                 "dead_heat_not_modelled": {12345: "WINNER", 2: "WINNER"},
                 "runner_result_missing": {2: "WINNER"}}
        for reason, results in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(pnl.market_realized(bet, results, commission_rate=0.05),
                                 {"gross_pence": None, "commission_pence": None, "net_pence": None,
                                  "excluded_reason": reason})
        no_price = [dict(bet[0], average_price_matched_h=None)]
        self.assertEqual(pnl.market_realized(no_price, {12345: "WINNER"}, commission_rate=0.05)["excluded_reason"],
                         "matched_price_missing")
        with self.assertRaises(ValueError):
            pnl.market_realized(bet + [dict(bet[0], market_id="1.2")], {12345: "WINNER"}, commission_rate=0.05)


if __name__ == "__main__":
    unittest.main()
