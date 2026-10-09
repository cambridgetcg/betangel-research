"""Bounded-memory LTP replay of a single ordered Betfair historical JSONL stream.

This intentionally does NOT implement an order book, matching engine or settlement.
pt is publisher time, not proven customer receipt time. Labels remain separate.
"""

import bz2
import copy
import json
import math
from pathlib import Path
from .temporal import timestamp_ms, iso_time


def messages(path):
    path = Path(path)
    opener = bz2.open if path.suffix == ".bz2" else open
    previous = -1
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("op") != "mcm":
                continue
            pt = msg.get("pt")
            if isinstance(pt, bool) or not isinstance(pt, int) or pt < previous:
                raise ValueError(f"invalid or decreasing pt at line {line_number}")
            if msg.get("segmentType") is not None:
                raise ValueError("segmented stream messages are not supported")
            previous = pt
            yield msg


def validate_config(config):
    for field in ("decision_before_start_s", "lookback_s", "horizon_s", "max_snapshot_age_s"):
        x = config[field]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
            raise ValueError(f"{field} must be finite and positive")
    if config["horizon_s"] >= config["decision_before_start_s"]:
        raise ValueError("label must be strictly before scheduled start")
    delays = config["delays_s"]
    if not delays or len(set(delays)) != len(delays):
        raise ValueError("delays must be nonempty and unique")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in delays):
        raise ValueError("delays must be finite and nonnegative")
    if not config.get("countries") or config.get("market_type") != "WIN":
        raise ValueError("declare countries and WIN market_type")


def replay_snapshots(path, config):
    """Capture the last observable state at each cutoff, including exact-time updates.

    Initial scheduled start is fixed when first published. A schedule revision before
    a snapshot marks that snapshot unusable, even if the schedule later changes back.
    """
    validate_config(config)
    states = {}
    count = 0
    final_pt = -1

    def capture_due(state, before):
        for cutoff in state["cutoffs"]:
            if cutoff >= before or cutoff in state["snapshots"]:
                continue
            if state["pt"] <= cutoff:
                state["snapshots"][cutoff] = copy.deepcopy({k: state[k] for k in
                    ("pt", "definition", "runners", "ltp", "ltp_time", "schedule_changed")})

    for msg in messages(path):
        count += 1
        final_pt = msg["pt"]
        for change in msg.get("mc", []):
            market_id = str(change["id"])
            md = change.get("marketDefinition", {})
            if market_id not in states:
                if "marketTime" not in md:
                    raise ValueError(f"market {market_id} starts without marketTime")
                start = timestamp_ms(md["marketTime"])
                decision = start - round(config["decision_before_start_s"] * 1000)
                cutoffs = {decision, decision + round(config["horizon_s"] * 1000)}
                for delay in config["delays_s"]:
                    cutoffs.add(decision - round(delay * 1000))
                    cutoffs.add(decision - round((delay + config["lookback_s"]) * 1000))
                states[market_id] = dict(start=start, decision=decision, cutoffs=sorted(cutoffs),
                    snapshots={}, pt=final_pt, definition={}, runners={}, ltp={}, ltp_time={},
                    schedule_changed=False)
            state = states[market_id]
            capture_due(state, final_pt)
            if change.get("img"):
                state["ltp"].clear()
                state["ltp_time"].clear()
                if md:
                    state["definition"].clear()
                    state["runners"].clear()
            if "marketTime" in md and timestamp_ms(md["marketTime"]) != state["start"]:
                state["schedule_changed"] = True
            if md.get("status") in ("SUSPENDED", "CLOSED"):
                state["ltp"].clear()
                state["ltp_time"].clear()
            state["definition"].update({k: v for k, v in md.items() if k != "runners"})
            for runner in md.get("runners", []):
                rid = str(runner["id"])
                state["runners"].setdefault(rid, {}).update(runner)
            for runner in change.get("rc", []):
                if "ltp" in runner:
                    rid = str(runner["id"])
                    value = float(runner["ltp"])
                    state["ltp"][rid] = value if math.isfinite(value) and value > 1 else None
                    state["ltp_time"][rid] = final_pt
            # Even an update accompanying suspension is not carried into a later
            # reopened snapshot; new observations after reopening are required.
            if state["definition"].get("status") != "OPEN":
                state["ltp"].clear()
                state["ltp_time"].clear()
            state["pt"] = final_pt
    for state in states.values():
        capture_due(state, final_pt + 1)
    return states, {"messages": count, "markets": len(states), "last_pt": final_pt}


def probabilities(snapshot):
    ids = sorted(r for r, item in snapshot["runners"].items() if item.get("status") == "ACTIVE")
    if len(ids) < 2 or any(snapshot["ltp"].get(r) is None for r in ids):
        raise ValueError("missing_or_invalid_active_runner_prices")
    if snapshot["definition"].get("numberOfActiveRunners") != len(ids):
        raise ValueError("incomplete_runner_definition")
    inverse = {r: 1 / snapshot["ltp"][r] for r in ids}
    total = sum(inverse.values())
    return {r: value / total for r, value in inverse.items()}


def fixed_forecasts(past, observed, lookback_s, time_to_target_s):
    """Four declared candidates; no parameter fitting or label access."""
    if set(past) != set(observed):
        raise ValueError("runner_set_changed")
    trend = {r: (observed[r] - past[r]) * time_to_target_s / lookback_s for r in observed}

    def normalize(values):
        bounded = {r: max(1e-12, min(1, value)) for r, value in values.items()}
        total = sum(bounded.values())
        return {r: value / total for r, value in bounded.items()}

    linked = {}
    for runner in observed:
        # A deliberately simple, fixed rival linkage. Ties broken by stable ID.
        rival = max((r for r in observed if r != runner), key=lambda r: (observed[r], r))
        linked[runner] = observed[runner] + trend[runner] - 0.5 * trend[rival]
    return {
        "no_change": dict(observed),
        "momentum": normalize({r: observed[r] + trend[r] for r in observed}),
        "mean_reversion": normalize({r: observed[r] - trend[r] for r in observed}),
        "rival_linkage": normalize(linked),
    }


def build_records(states, config):
    """Evaluate all delay variants on their common eligible market cohort.

    Decision-time q is a LABEL REFERENCE only: it is subtracted from both predicted
    target level and actual target level. It is not supplied to fixed_forecasts.
    Thus MAE equals target-level MAE even when a delayed decision snapshot differs.
    """
    by_delay = {str(d): [] for d in config["delays_s"]}
    exclusions = {str(d): {} for d in config["delays_s"]}
    cohorts = {}
    for delay in config["delays_s"]:
        key = str(delay)
        for mid, state in states.items():
            decision = state["decision"]
            cuts = [decision - round((delay + config["lookback_s"]) * 1000),
                    decision - round(delay * 1000), decision,
                    decision + round(config["horizon_s"] * 1000)]
            try:
                snaps = [state["snapshots"].get(c) for c in cuts]
                if any(s is None for s in snaps):
                    raise ValueError("missing_snapshot")
                for cutoff, snap in zip(cuts, snaps):
                    md = snap["definition"]
                    if md.get("marketType") != config["market_type"] or md.get("countryCode") not in config["countries"]:
                        raise ValueError("outside_declared_population")
                    if snap["schedule_changed"]:
                        raise ValueError("scheduled_start_revised")
                    if md.get("status") != "OPEN" or md.get("inPlay") is not False:
                        raise ValueError("not_open_preplay")
                    if cutoff - snap["pt"] > config["max_snapshot_age_s"] * 1000:
                        raise ValueError("stale_market_snapshot")
                past, observed, reference, target = map(probabilities, snaps)
                if any(set(p) != set(reference) for p in [past, observed, target]):
                    raise ValueError("runner_set_changed")
                forecasts = fixed_forecasts(past, observed, config["lookback_s"], config["horizon_s"] + delay)
                for runner in reference:
                    by_delay[key].append({
                        "market_id": mid, "selection_id": runner,
                        "event_date": iso_time(state["start"])[:10],
                        "decision_time": iso_time(decision), "data_cutoff": iso_time(cuts[1]),
                        "latest_feature_pt": iso_time(snaps[1]["pt"]),
                        "ltp_age_s": (cuts[1] - snaps[1]["ltp_time"][runner]) / 1000,
                        "actual_change": target[runner] - reference[runner],
                        "predictions": {name: pred[runner] - reference[runner] for name, pred in forecasts.items()},
                    })
            except ValueError as exc:
                exclusions[key][mid] = str(exc)
        cohorts[key] = {r["market_id"] for r in by_delay[key]}
    common = set.intersection(*cohorts.values())
    for key in by_delay:
        for mid in cohorts[key] - common:
            exclusions[key][mid] = "not_in_common_delay_cohort"
        by_delay[key] = [r for r in by_delay[key] if r["market_id"] in common]
    return by_delay, exclusions
