"""Offline ADV top-book features; no orders, labels, fills or profit estimates.

`pt` is publisher time, not customer receipt time. batb/batl are level-keyed
replacement deltas, not price-keyed ladders. Runner tv is unsigned cumulative
matched volume; it does not identify the initiating side of a trade.

Semantics checked 2026-10-09 against:
https://historicdata.betfair.com/files/Betfair-Historical-Data-Feed-Specification.pdf
https://support.developer.betfair.com/hc/en-us/articles/360018468438-Advanced-Historical-Data-How-do-I-interpret-updates
"""

import copy
import math
from collections import Counter

from .stream import messages
from .temporal import timestamp_ms


_PRO_FIELDS = {"atb", "atl", "spb", "spl", "spn", "spf"}
_VIRTUAL_FIELDS = {"bdatb", "bdatl"}


def _milliseconds(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be finite and positive")
    result = value * 1000
    if not math.isfinite(result) or result != int(result):
        raise ValueError(f"{name} must be representable in whole milliseconds")
    return int(result)


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _active(snapshot):
    return {rid for rid, runner in snapshot["runners"].items()
            if runner.get("status") == "ACTIVE"}


def _new_state(start, decision, lookback):
    return dict(start=start, decision=decision, lookback=lookback, pt=None,
                definition={}, runners={}, books={}, volumes={}, volume_times={},
                volume_faults={}, formats=set(), schedule_changed=False,
                image_count=0, status_break_count=0, field_change_count=0,
                snapshots={})


def _capture(state, before):
    for cutoff in (state["lookback"], state["decision"]):
        if (cutoff < before and cutoff not in state["snapshots"]
                and state["pt"] is not None and state["pt"] <= cutoff):
            state["snapshots"][cutoff] = copy.deepcopy({
                key: value for key, value in state.items()
                if key not in {"snapshots", "start", "decision", "lookback"}})


def _update_levels(book, field, updates, pt):
    if updates is None:
        return  # Optional/null delta means no observation, never a fresh quote.
    if not isinstance(updates, list):
        raise ValueError(f"{field} must contain [level, price, size] updates")
    seen = {}
    for update in updates:
        if not isinstance(update, list) or len(update) != 3:
            raise ValueError(f"{field} requires ADV [level, price, size] triples")
        level, price, size = update
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 2:
            raise ValueError(f"{field} ADV level must be 0, 1 or 2")
        if not _number(size) or size < 0 or not _number(price):
            raise ValueError(f"{field} price/size must be finite; size nonnegative")
        if size > 0 and price <= 1:
            raise ValueError(f"{field} nonempty decimal odds must exceed 1")
        if level in seen and seen[level] != (price, size):
            raise ValueError(f"conflicting {field} updates at level {level}")
        seen[level] = (price, size)
        if size == 0:
            book[field].pop(level, None)
        else:
            # In particular, overwriting level zero discards its OLD price.
            # Deleting zero does not promote level one without an explicit delta.
            book[field][level] = (price, size, pt)


def _apply(state, change, pt):
    md = change.get("marketDefinition") or {}
    old_active = _active(state)
    if change.get("img") is True:
        state["image_count"] += 1
        for field in ("definition", "runners", "books", "volumes", "volume_times"):
            state[field].clear()
    if "marketTime" in md and timestamp_ms(md["marketTime"]) != state["start"]:
        state["schedule_changed"] = True
    state["definition"].update({key: value for key, value in md.items() if key != "runners"})
    for runner in md.get("runners", []):
        rid = str(runner["id"])
        state["runners"].setdefault(rid, {}).update(runner)
        if runner.get("hc", 0) != 0:
            state["formats"].add("unsupported_handicap")
    if state["pt"] is not None and old_active != _active(state):
        state["field_change_count"] += 1
    opened = (state["definition"].get("status") == "OPEN"
              and state["definition"].get("inPlay") is False)
    if not opened:
        state["status_break_count"] += 1
    for runner in change.get("rc", []):
        rid = str(runner["id"])
        if runner.get("hc", 0) != 0:
            state["formats"].add("unsupported_handicap")
        if any(key in runner for key in ("batb", "batl")):
            state["formats"].add("ADV")
        if _PRO_FIELDS.intersection(runner):
            state["formats"].add("PRO")
        if _VIRTUAL_FIELDS.intersection(runner):
            state["formats"].add("virtual")
        book = state["books"].setdefault(rid, {"batb": {}, "batl": {}})
        for field in ("batb", "batl"):
            if field in runner:
                _update_levels(book, field, runner[field], pt)
        if "tv" in runner and runner["tv"] is not None:
            value = runner["tv"]
            previous = state["volumes"].get(rid)
            if not _number(value) or value < 0:
                state["volumes"][rid] = None
                state["volume_faults"][rid] = state["volume_faults"].get(rid, 0) + 1
            else:
                if previous is not None and value < previous:
                    state["volume_faults"][rid] = state["volume_faults"].get(rid, 0) + 1
                state["volumes"][rid] = value
            state["volume_times"][rid] = pt
    # Never carry pre-suspension, removed-runner, or in-play quotes on reopening.
    for rid in list(state["books"]):
        if not opened or rid not in _active(state):
            del state["books"][rid]
    state["pt"] = pt


def _top(snapshot, rid, side):
    if snapshot is None:
        return None, None, None
    return snapshot["books"].get(rid, {}).get(side, {}).get(0, (None, None, None))


def _format_reasons(snapshot):
    formats = snapshot["formats"]
    reasons = []
    if "PRO" in formats:
        reasons.append("unsupported_pro_or_mixed_fields")
    if "virtual" in formats:
        reasons.append("unsupported_virtual_fields")
    if "unsupported_handicap" in formats:
        reasons.append("unsupported_handicap")
    if "ADV" not in formats:
        reasons.append("unsupported_basic_or_unknown_fields")
    return reasons


def _market_reasons(snapshot, prefix=""):
    reasons = []
    md = snapshot["definition"]
    if md.get("status") != "OPEN" or md.get("inPlay") is not False:
        reasons.append(prefix + "not_open_preplay")
    if snapshot["schedule_changed"]:
        reasons.append("scheduled_start_revised")
    if md.get("numberOfActiveRunners") != len(_active(snapshot)):
        reasons.append(prefix + "incomplete_runner_definition")
    return reasons


def _record(mid, state, rid, current, previous, max_age):
    reasons = _format_reasons(current) + _market_reasons(current)
    if state["decision"] - current["pt"] > max_age:
        reasons.append("stale_decision_snapshot")
    back, back_size, back_time = _top(current, rid, "batb")
    lay, lay_size, lay_time = _top(current, rid, "batl")
    old_back, _, old_back_time = _top(previous, rid, "batb")
    old_lay, _, old_lay_time = _top(previous, rid, "batl")
    valid_book = back is not None and lay is not None and back < lay
    if back is None or lay is None:
        reasons.append("missing_top_book")
    elif back >= lay:
        reasons.append("locked_or_crossed_top_book")
    delta = None
    if previous is None:
        reasons.append("missing_lookback_snapshot")
    else:
        reasons.extend(_market_reasons(previous, "lookback_"))
        if state["lookback"] - previous["pt"] > max_age:
            reasons.append("stale_lookback_snapshot")
        if "ADV" not in previous["formats"]:
            reasons.append("lookback_missing_adv_fields")
        if old_back is None or old_lay is None:
            reasons.append("lookback_missing_top_book")
        elif old_back >= old_lay:
            reasons.append("lookback_locked_or_crossed_top_book")
        continuity = True
        for counter, reason in (("image_count", "image_reset_in_window"),
                                ("status_break_count", "status_break_in_window"),
                                ("field_change_count", "runner_set_changed_in_window")):
            if current[counter] != previous[counter]:
                reasons.append(reason)
                continuity = False
        if _active(current) != _active(previous):
            reasons.append("runner_set_changed_in_window")
            continuity = False
        volume = current["volumes"].get(rid)
        old_volume = previous["volumes"].get(rid)
        if volume is None or old_volume is None:
            reasons.append("missing_or_invalid_runner_volume")
        elif (current["volume_faults"].get(rid, 0) != previous["volume_faults"].get(rid, 0)
              or volume < old_volume):
            reasons.append("runner_volume_decreased_or_invalid_in_window")
        elif continuity:
            delta = volume - old_volume
    # Missing delta is deliberately an exclusion, never silently zero-filled.
    reasons = sorted(set(reasons))
    md = current["definition"]
    return dict(market_id=mid, selection_id=rid, decision_ms=state["decision"],
                observed_ms=current["pt"], start_ms=state["start"],
                lookback_ms=state["lookback"],
                previous_observed_ms=previous["pt"] if previous else None,
                back=back, lay=lay, back_size=back_size, lay_size=lay_size,
                previous_back=old_back, previous_lay=old_lay,
                back_observed_ms=back_time, lay_observed_ms=lay_time,
                previous_back_observed_ms=old_back_time,
                previous_lay_observed_ms=old_lay_time,
                runner_volume=current["volumes"].get(rid),
                previous_runner_volume=previous["volumes"].get(rid) if previous else None,
                volume_observed_ms=current["volume_times"].get(rid),
                volume_delta=delta,
                book_imbalance=(back_size / 2 - lay_size / 2) / (back_size / 2 + lay_size / 2) if valid_book else None,
                relative_spread=(lay - back) / (back / 2 + lay / 2) if valid_book else None,
                eligible=not reasons, invalid_reasons=reasons,
                market_type=md.get("marketType"), country_code=md.get("countryCode"),
                venue=md.get("venue"), formats_observed=sorted(current["formats"]))


def sample_features(path, decision_before_start_s=120, lookback_s=30,
                    max_snapshot_age_s=10):
    """Sample ADV features as-of initial schedule minus the requested intervals.

    The last state with pt <= cutoff is used (all equal-pt messages included).
    The stream must reach the decision cutoff. Known active runners with unusable
    features remain in `records`; markets without an observable decision/active
    field remain in audit.market_exclusions. Eligibility never uses future labels.

    Scope is WIN markets, using marketType known by decision; country is recorded
    but unfiltered. Other market types remain in the market exclusion audit.
    ADV compatibility is inferred from pre-decision fields, not a trusted package
    manifest. BASIC/unknown, PRO, mixed, virtual and handicap fields are reported
    unsupported. Malformed level updates and unordered streams raise ValueError.
    Both endpoint MARKET messages must be no older than max_snapshot_age_s
    (10 seconds by default). This does not prove feed continuity or quote age.
    An unchanged delta field is carried forward; quote timestamps are returned.
    No customer-receipt latency or missing-packet detection is modelled.
    """
    lead = _milliseconds(decision_before_start_s, "decision_before_start_s")
    lag = _milliseconds(lookback_s, "lookback_s")
    max_age = _milliseconds(max_snapshot_age_s, "max_snapshot_age_s")
    states = {}
    count = 0
    final_pt = None
    for msg in messages(path):
        count += 1
        final_pt = msg["pt"]
        for change in msg.get("mc", []):
            mid = str(change["id"])
            if mid not in states:
                md = change.get("marketDefinition") or {}
                if "marketTime" not in md:
                    raise ValueError(f"market {mid} starts without marketTime")
                start = timestamp_ms(md["marketTime"])
                states[mid] = _new_state(start, start - lead, start - lead - lag)
            state = states[mid]
            _capture(state, final_pt)
            # Stop interpreting this market once both pre-decision states are
            # immutable. Later removals, corrections and outcomes cannot veto it.
            if state["decision"] in state["snapshots"]:
                continue
            _apply(state, change, final_pt)
    if final_pt is not None:
        for state in states.values():
            _capture(state, final_pt + 1)
    records = []
    exclusions = {}
    for mid, state in sorted(states.items()):
        current = state["snapshots"].get(state["decision"])
        previous = state["snapshots"].get(state["lookback"])
        if current is None:
            exclusions[mid] = ["missing_observable_decision_snapshot"]
            continue
        if current["definition"].get("marketType") != "WIN":
            exclusions[mid] = ["outside_win_market_scope"]
            continue
        active = sorted(_active(current))
        if not active:
            exclusions[mid] = sorted(set(["no_known_active_runners"]
                                        + _format_reasons(current) + _market_reasons(current)))
        records.extend(_record(mid, state, rid, current, previous, max_age) for rid in active)
    reason_counts = Counter(reason for record in records for reason in record["invalid_reasons"])
    return {"records": records, "audit": {
        "messages": count, "markets": len(states), "records": len(records),
        "eligible_records": sum(record["eligible"] for record in records),
        "invalid_reason_counts": dict(sorted(reason_counts.items())),
        "market_exclusions": exclusions, "last_pt": final_pt,
        "supported_package": "ADV_ONLY", "format_detection": "pre-decision field evidence only",
        "market_type": "WIN", "countries": "unfiltered",
        "time_basis": "publisher_pt_not_customer_receipt",
        "schedule_anchor": "first_published_marketTime",
        "volume_basis": "unsigned_runner_cumulative_tv_not_price_keyed_trd",
        "decision_before_start_s": decision_before_start_s, "lookback_s": lookback_s,
        "max_snapshot_age_s": max_snapshot_age_s,
        "future_labels_used": False, "execution_model": None}}
