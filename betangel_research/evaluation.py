"""Offline forecast scores and costs for already-settled or simulated markets.

Forecasts and actual changes must use the same declared units. These functions
do not infer execution, stakes, P&L, or ROI from market prices. Invalid input,
including an empty forecast dataset, raises ValueError.
"""

import math
from collections import defaultdict
from datetime import date
from numbers import Real

ZERO_TOLERANCE = 1e-12


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{label} must be a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _date(value):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("event dates must be YYYY-MM-DD") from None
    return value


def _identifier(value):
    return isinstance(value, str) and bool(value.strip())


def _direction(value):
    return 0 if abs(value) <= ZERO_TOLERANCE else (1 if value > 0 else -1)


def _mean(values):
    # Divide first so averaging finite, large errors does not overflow a sum.
    return math.fsum(value / len(values) for value in values)


def _scores(rows, model_names):
    result = {}
    for model in model_names:
        errors, directions = defaultdict(list), defaultdict(list)
        for row in rows:
            actual, prediction = row["actual_change"], row["predictions"][model]
            error = abs(prediction - actual)
            if not math.isfinite(error):
                raise ValueError("forecast error exceeds finite numeric range")
            errors[row["market_id"]].append(error)
            directions[row["market_id"]].append(int(_direction(actual) == _direction(prediction)))
        result[model] = {
            "mae": _mean([x for group in errors.values() for x in group]),
            "race_balanced_mae": _mean([_mean(group) for group in errors.values()]),
            "directional_accuracy": _mean([x for group in directions.values() for x in group]),
            "race_balanced_directional_accuracy": _mean([_mean(group) for group in directions.values()]),
        }
    return result


def _counts(rows):
    return {"records": len(rows), "markets": len({r["market_id"] for r in rows}),
            "days": len({r["event_date"] for r in rows})}


def evaluate_records(records: list[dict]) -> dict:
    """Score runner forecasts and equally weighted markets; flat includes ±1e-12.

    Each market must belong to exactly one date. A market is treated as one
    race unit: callers must not submit multiple market types for the same race.
    Missing models, duplicate market/selection pairs, and empty input are errors.
    Directional accuracy is a fraction in [0, 1]; MAE retains the input units.
    """
    if not isinstance(records, list) or not records:
        raise ValueError("records must be a nonempty list")
    rows, seen, market_dates, by_day = [], set(), {}, defaultdict(list)
    models = None
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("each record must be a dictionary")
        market, selection = record.get("market_id"), record.get("selection_id")
        if not _identifier(market) or not _identifier(selection):
            raise ValueError("market_id and selection_id must be nonempty strings")
        key = (market, selection)
        if key in seen:
            raise ValueError("duplicate market/selection record")
        seen.add(key)
        event_date = _date(record.get("event_date"))
        if market_dates.setdefault(market, event_date) != event_date:
            raise ValueError("one market cannot have multiple event dates")
        predictions = record.get("predictions")
        if not isinstance(predictions, dict) or not predictions or not all(map(_identifier, predictions)):
            raise ValueError("predictions must have nonempty string model names")
        if models is None:
            models = set(predictions)
        if set(predictions) != models:
            raise ValueError("every record must contain the same prediction models")
        row = {"market_id": market, "event_date": event_date,
               "actual_change": _number(record.get("actual_change"), "actual_change"),
               "predictions": {name: _number(value, name) for name, value in predictions.items()}}
        rows.append(row)
        by_day[event_date].append(row)
    names = sorted(models)
    return {"status": "ok", "counts": _counts(rows),
            "direction_zero_tolerance": ZERO_TOLERANCE, "models": _scores(rows, names),
            "per_day": {day: {"counts": _counts(by_day[day]), "models": _scores(by_day[day], names)}
                        for day in sorted(by_day)}}


def chronological_folds(event_dates: list[str], min_train_days: int = 2,
                        test_days: int = 1) -> list[dict]:
    """Expanding training days with disjoint later test blocks (last may be short).

    Dates are deduplicated and sorted; day counts mean observed event dates,
    not contiguous calendar dates. Insufficient dates return an empty list.
    """
    if not isinstance(event_dates, list):
        raise ValueError("event_dates must be a list")
    for value in (min_train_days, test_days):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("day counts must be positive integers")
    days = sorted({_date(day) for day in event_dates})
    return [{"train_dates": days[:start], "test_dates": days[start:start + test_days]}
            for start in range(min_train_days, len(days), test_days)]


def settled_market_costs(gross_by_market: dict[str, float], commission_rate: float,
                         fixed_cost: float = 0, additional_fees: float = 0) -> dict:
    """Apply commission only to positive GROSS net market totals, then costs.

    Inputs must already represent settled or simulated execution in one currency;
    already-net exports must not be passed as gross. No ROI denominator is assumed.
    Empty market totals are allowed: fixed and additional costs still apply.
    """
    if not isinstance(gross_by_market, dict) or not all(map(_identifier, gross_by_market)):
        raise ValueError("gross_by_market must map nonempty market IDs to gross totals")
    rate = _number(commission_rate, "commission_rate")
    fixed, fees = _number(fixed_cost, "fixed_cost"), _number(additional_fees, "additional_fees")
    if not 0 <= rate <= 1 or fixed < 0 or fees < 0:
        raise ValueError("rate must be between 0 and 1; costs must be nonnegative")
    markets = {}
    for market, value in gross_by_market.items():
        gross = _number(value, "gross total")
        commission = max(gross, 0) * rate
        markets[market] = {"gross": gross, "commission": commission,
                           "net_after_commission": gross - commission}
    try:
        gross_total = math.fsum(row["gross"] for row in markets.values())
        commission_total = math.fsum(row["commission"] for row in markets.values())
        net = _number(gross_total - commission_total, "net total")
        after_costs = _number(net - fixed - fees, "net after all costs")
    except OverflowError:
        raise ValueError("market totals exceed finite numeric range") from None
    return {"gross_total": gross_total, "commission_total": commission_total,
            "net_after_commission": net, "fixed_cost": fixed, "additional_fees": fees,
            "net_after_all_costs": after_costs, "markets": markets}
