"""Our own realised P&L in integer pence, commission, and the comparison with Betfair's profit.

The sign and basis of Betfair's ClearedOrderSummary.profit are unverified (F37), so settlement
uses the smaller of our figure and Betfair's when both exist, and a difference above 1p is a
mismatch the gateway turns into a HALT. Rounding always goes against us: a profit is floored to
the penny and a loss is rounded up. Commission is charged by evaluation.settled_market_costs on
positive market totals only, then rounded up to the penny.
"""

import math

from .. import evaluation

RUNNER_RESULTS = ("WINNER", "LOSER")
EXCLUDING_RESULTS = {"REMOVED": "rule4_adjustment_not_modelled", "REMOVED_VACANT": "rule4_adjustment_not_modelled"}


def _pence_int(value, name, *, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be an integer" + (f" >= {minimum}" if minimum is not None else ""))
    return value


def order_pnl_pence(side, avg_price_h, size_settled_p, runner_result):
    """P&L of a settled bet: BACK wins size*(price-1) or loses size; LAY the reverse."""
    if side not in ("BACK", "LAY"):
        raise ValueError("side must be BACK or LAY")
    _pence_int(size_settled_p, "size_settled_p", minimum=0)
    _pence_int(avg_price_h, "avg_price_h", minimum=101)
    if runner_result not in RUNNER_RESULTS:
        raise ValueError("runner_result must be WINNER or LOSER")
    odds_part = size_settled_p * (avg_price_h - 100)          # hundredths of a penny
    if side == "BACK":
        return odds_part // 100 if runner_result == "WINNER" else -size_settled_p
    return (-odds_part) // 100 if runner_result == "WINNER" else size_settled_p


def market_realized(orders, runner_results, *, commission_rate):
    """Gross, commission and net pence for one settled market, or an excluded_reason.

    orders: order records of one market (matched size and average matched price are used).
    runner_results: {selection_id: "WINNER"|"LOSER"|"REMOVED"|...}. A removed runner (rule 4
    deductions are not modelled), a dead heat, or a missing result excludes the market; then
    every figure is None and the caller must fall back to the worst case.
    """
    results = {}
    for selection, result in dict(runner_results).items():
        try:
            results[int(selection)] = result
        except (TypeError, ValueError):
            raise ValueError("runner_results keys must be selection ids") from None
    excluded = None
    if any(result in EXCLUDING_RESULTS for result in results.values()):
        excluded = "rule4_adjustment_not_modelled"
    elif sum(1 for result in results.values() if result == "WINNER") > 1:
        excluded = "dead_heat_not_modelled"
    markets = {o["market_id"] for o in orders}
    if len(markets) > 1:
        raise ValueError("market_realized takes the orders of one market")
    gross = 0
    for order in orders:
        matched = order["size_matched_p"]
        if not matched:
            continue
        if excluded is not None:
            continue
        result = results.get(int(order["selection_id"]))
        if result not in RUNNER_RESULTS:
            excluded = "runner_result_missing"
            continue
        price = order["average_price_matched_h"]
        if price is None:
            excluded = "matched_price_missing"
            continue
        gross += order_pnl_pence(order["side"], price, matched, result)
    if excluded is not None:
        return {"gross_pence": None, "commission_pence": None, "net_pence": None, "excluded_reason": excluded}
    market_id = next(iter(markets)) if markets else "market"
    costs = evaluation.settled_market_costs({market_id: gross}, commission_rate)
    commission = costs["markets"][market_id]["commission"]
    # Rounded to 6 dp first so float noise (21.000000000000004) cannot add a penny; then up.
    commission_pence = math.ceil(round(commission, 6))
    return {"gross_pence": gross, "commission_pence": commission_pence, "net_pence": gross - commission_pence,
            "excluded_reason": None}


def compare_profit(own_pence, betfair_profit_pence):
    """{"used": min(own, betfair) or own, "mismatch": |own - betfair| > 1}."""
    _pence_int(own_pence, "own_pence")
    if betfair_profit_pence is None:
        return {"used": own_pence, "mismatch": False}
    _pence_int(betfair_profit_pence, "betfair_profit_pence")
    return {"used": min(own_pence, betfair_profit_pence), "mismatch": abs(own_pence - betfair_profit_pence) > 1}
