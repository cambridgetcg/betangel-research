"""Reconciliation against the exchange (sections 9.7 and 9.9), through a ReadClient only. Live only:
paper never reconciles.

An order whose outcome is unknown is never treated as zero and never resent. reconcile_unknown
polls for it by ref (or betId) for a window anchored at the time the outcome was observed; a read
error leaves it UNKNOWN and never counts as "not found". Only when enough successful polls span the
window, every poll in the last five seconds succeeded, and a ref-independent search and the cleared
orders both came back empty does it become ABSENT_UNCONFIRMED, which still blocks placement at full
adverse exposure. ABSENT_ACCEPTED needs the operator (reconcile --accept). Nothing here writes the
journal: each function returns the events and moves for the gateway to journal.

The live evidence rule (amendment A3, D3, binding): exchange evidence (current-order rows, cleared
rows) may establish that a bet exists (its betId) and raise its known matched size; it never lowers
an order's counted worst case and never moves an order to a terminal or zero-exposure state. Only a
report of an order request this process sent, reconcile --accept and settle-market lower live
exposure. So found_move withholds any cancelled, lapsed or voided size the journal did not already
know (the order stays EXECUTABLE at its worst case, exchange_reduction_withheld_until_settle), and a
cleared row of any status only records the betId (EXECUTABLE, cleared_row_bet_exists_worst_case):
nothing here concludes LAPSED, VOIDED or SETTLED. Betfair's sizes are raw doubles
(3.4400000000000004 for 3.44), so a response amount within a millionth of a penny is that penny
(pence); what we send stays exact.

startup_reconcile reads today's orders by our strategy refs and by the live app key (sourceIdKey,
compared in memory and never kept). An exchange bet is ours only by its betId, or by its
customerOrderRef when that journal order is still unresolved and has no betId yet; anything else
(including a bet that reuses the ref of an order the journal already closed, accepted as absent
or tied to another betId) is foreign and halts the gateway (foreign_strategy_order). A bet the
exchange shows EXECUTABLE whose journal order is neither EXECUTABLE, CANCEL_PENDING nor unresolved
is a contradiction (exchange_journal_contradiction); so is a bet whose exchange matched size
(sizeMatched while current, sizeSettled once settled) is more than a closed journal order says
matched (for an order settled by hand, matched plus the recorded remainder whose worst case was
booked), or any matched size at all on a journal order closed with nothing at risk (LAPSED, FAILED,
ABSENT_ACCEPTED, VOIDED and the like). Either fails A17.

Clocks: reconcile_unknown paces and bounds its polling on the monotonic clock and uses the wall
clock only for Betfair's date ranges; if the two disagree while it polls (a wall-clock step) it
concludes nothing (clock_step_during_reconcile).
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
import math
import re

from .constants import MAX_IDS_PER_REQUEST
from .errors import BetfairError, safe_code
from .filters import MARKET_ID_RE
from .killswitch import placed_with_key
from .receipts import parse_utc_ns, utc_from_ns, utc_ms_from_ns
from .schemas import UNRESOLVED_STATES
from .secret import Secret
from .ticks import is_on_ladder

MAX_BLOCK_S = 40
FINAL_POLLS_S = 5
CLOCK_STEP_TOLERANCE_S = 2          # wall vs monotonic elapsed time while polling
SEARCH_BACK_S = 5
CLEARED_BACK_S = 60
CLEARED_STATUSES = ("LAPSED", "CANCELLED", "SETTLED", "VOIDED")
_BET_ID_RE = re.compile(r"[0-9]{1,20}")
_NS_PER_S = 1_000_000_000
# Journal states consistent with a bet the exchange shows EXECUTABLE (unresolved ones fail A17 anyway).
_OPEN_JOURNAL_STATES = frozenset({"EXECUTABLE", "CANCEL_PENDING"}) | UNRESOLVED_STATES
# Closed journal states that claim nothing of the bet is matched and at risk (zero matched exposure).
_ZERO_MATCHED_STATES = frozenset({"REFUSED", "FAILED", "ABSENT_ACCEPTED", "LAPSED", "VOIDED", "RECORDED"})
_RECONCILE_KEYS = ("poll_interval_s", "window_s", "grace_s", "min_successful_polls", "cleared_lookback_s")
# Betfair serialises sizes and amounts as raw doubles (its own order-placement example shows
# "sizeMatched":3.4400000000000004), so a response amount within this of a whole penny is that penny.
# What we send stays exact (ticks.to_pence and to_hundredths).
RESPONSE_AMOUNT_TOLERANCE = Decimal("0.000001")
_CENT = Decimal("0.01")


# --- value helpers -----------------------------------------------------------------------------


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, OverflowError, ValueError):
        return None


def _snapped(amount):
    """The whole-penny Decimal a Betfair response amount stands for, or None when it is not one.

    Betfair's doubles carry binary artefacts (3.4400000000000004 for 3.44): a value within
    RESPONSE_AMOUNT_TOLERANCE of a penny is that penny; anything further away (1.005, say) is not a
    penny amount at all."""
    nearest = amount.quantize(_CENT, rounding=ROUND_HALF_EVEN)
    return nearest if abs(amount - nearest) <= RESPONSE_AMOUNT_TOLERANCE else None


def pence(value):
    """Integer pence from a Betfair GBP response amount (>= 0, a whole penny up to a double's
    rounding artefact), else None. Only for values Betfair returns, never for what we send."""
    amount = _decimal(value)
    if amount is None or amount < 0:
        return None
    snapped = _snapped(amount)
    return None if snapped is None else int(snapped * 100)


def profit_pence(value):
    """Betfair profit (may be negative) as pence: the penny it stands for, else rounded against us (down)."""
    amount = _decimal(value)
    if amount is None:
        return None
    snapped = _snapped(amount)
    if snapped is not None:
        return int(snapped * 100)
    return int((amount * 100).to_integral_value(rounding=ROUND_FLOOR))


def _ceil_pence(value):
    amount = _decimal(value)
    if amount is None or amount < 0:
        return None
    snapped = _snapped(amount)
    if snapped is not None:
        return int(snapped * 100)
    return int((amount * 100).to_integral_value(rounding=ROUND_CEILING))


def average_price_h(value, side):
    """Average matched price in hundredths, rounded against us: down for a BACK, up for a LAY."""
    amount = _decimal(value)
    if amount is None or amount <= 1:
        return None
    rounding = ROUND_FLOOR if side == "BACK" else ROUND_CEILING
    return int((amount * 100).to_integral_value(rounding=rounding))


def _exact_price_h(value):
    """A Betfair response price as ladder hundredths (up to a double's rounding artefact), else None."""
    amount = _decimal(value)
    if amount is None:
        return None
    snapped = _snapped(amount)
    if snapped is None:
        return None
    price_h = int(snapped * 100)
    return price_h if is_on_ladder(price_h) else None


def _reconcile_cfg(cfg):
    section = cfg["orders"]["reconcile"] if isinstance(cfg, dict) and "orders" in cfg else cfg
    if not isinstance(section, dict) or any(key not in section for key in _RECONCILE_KEYS):
        raise ValueError("reconcile needs the orders.reconcile settings")
    return section


def _terms_match(item, order):
    """The exchange order has this order's selection, side, limit price and size."""
    price_size = item.get("priceSize") if isinstance(item.get("priceSize"), dict) else {}
    return (item.get("selectionId") == order["selection_id"] and item.get("side") == order["side"]
            and _exact_price_h(price_size.get("price")) == order["price_h"]
            and pence(price_size.get("size")) == order["size_p"])


def found_move(item, order):
    """(state, fields) for our order as one CurrentOrderSummary shows it, or None if it cannot be trusted.

    The exchange order must carry our selection, side, price and size, and its sizes must add up
    to the order size exactly; otherwise nothing is taken from it (the order stays unresolved).
    A voided fragment leaves sizeMatched for sizeVoided (Betfair's VAR example: sm 2, sv 0 becomes
    sm 0, sv 2), so the identity is matched + remaining + lapsed + cancelled + voided == size.

    The live evidence rule (module docstring): when the exchange reports cancelled, lapsed or voided
    size the journal did not know, that reduction is withheld until settle-market: EXECUTABLE with
    the journal's cancelled/lapsed/voided sizes, the larger of the two matched sizes and the rest
    remaining (>= 0; 0 is a valid matched_exact record when the journal's matched size exceeds the
    exchange's, e.g. part of a matched bet was voided), error_code
    exchange_reduction_withheld_until_settle. Less reduced, or less matched, than the journal already
    knows is contradictory evidence: None. Otherwise the exchange state is taken (EXECUTABLE, or
    EXECUTION_COMPLETE when matched plus the known reductions make up the whole size); neither
    lowers the order's counted worst case. That resolves the order, so the code of the outcome that
    left it unresolved (e.g. cancel_size_short_refresh_needed) is cleared: error_code None.
    """
    if not isinstance(item, dict) or item.get("marketId") != order["market_id"]:
        return None
    bet_id = item.get("betId")
    if not isinstance(bet_id, str) or not _BET_ID_RE.fullmatch(bet_id):
        return None
    if not _terms_match(item, order):
        return None
    sizes = {key: pence(item.get(name)) for key, name in (("matched", "sizeMatched"), ("remaining", "sizeRemaining"),
                                                          ("lapsed", "sizeLapsed"), ("cancelled", "sizeCancelled"))}
    if any(value is None for value in sizes.values()):
        return None
    voided = pence(item.get("sizeVoided")) if item.get("sizeVoided") is not None else 0
    if voided is None or sum(sizes.values()) + voided != order["size_p"]:
        return None
    average = None
    if sizes["matched"] > 0:
        average = average_price_h(item.get("averagePriceMatched"), order["side"])
        if average is None:
            return None
    status = item.get("status")
    if not ((status == "EXECUTABLE" and sizes["remaining"] > 0)
            or (status == "EXECUTION_COMPLETE" and sizes["remaining"] == 0)):
        return None
    known = order["size_cancelled_p"] + order["size_lapsed_p"] + (order.get("size_voided_p") or 0)
    seen = sizes["cancelled"] + sizes["lapsed"] + voided
    journal_matched = order["size_matched_p"]
    if seen < known or (seen == known and sizes["matched"] < journal_matched):
        return None                    # the exchange shows less than our own reports established
    if seen > known:
        matched = max(journal_matched, sizes["matched"])
        fields = {"bet_id": bet_id, "size_matched_p": matched,
                  "average_price_matched_h": average if sizes["matched"] > 0 else (
                      order["average_price_matched_h"] if matched > 0 else None),
                  "size_remaining_p": order["size_p"] - matched - known,
                  "size_cancelled_p": order["size_cancelled_p"], "size_lapsed_p": order["size_lapsed_p"],
                  "size_voided_p": order.get("size_voided_p") or 0,
                  "error_code": "exchange_reduction_withheld_until_settle"}
        return "EXECUTABLE", fields
    fields = {"bet_id": bet_id, "size_matched_p": sizes["matched"], "average_price_matched_h": average,
              "size_remaining_p": sizes["remaining"], "size_cancelled_p": sizes["cancelled"],
              "size_lapsed_p": sizes["lapsed"], "size_voided_p": voided, "error_code": None}
    return ("EXECUTABLE" if sizes["remaining"] > 0 else "EXECUTION_COMPLETE"), fields


def _read(fetch):
    """(items, None) or (None, code). Only Betfair and value errors are read failures."""
    try:
        return list(fetch()), None
    except (BetfairError, ValueError) as exc:
        return None, safe_code(getattr(exc, "code", None) or type(exc).__name__)


def _ts(order, key, default):
    value = order.get(key)
    if value is None:
        return default
    try:
        return parse_utc_ns(value)
    except ValueError:
        return default


def _absent_ready(polls, observed_ns, cfg):
    ok = [t for t, success in polls if success and t >= observed_ns]
    if len(ok) < cfg["min_successful_polls"] or not polls:
        return False
    if max(ok) - min(ok) < int(cfg["window_s"] * _NS_PER_S):
        return False
    last = polls[-1][0]
    return all(success for t, success in polls if t >= last - FINAL_POLLS_S * _NS_PER_S)


# --- cleared orders ----------------------------------------------------------------------------


def _is_this_order(item, order):
    if not isinstance(item, dict):
        return False
    if order.get("bet_id") is not None and item.get("betId") == order["bet_id"]:
        return True
    return item.get("customerOrderRef") == order["customer_order_ref"]


def cleared_hits(read, order, *, since_utc):
    """({status: [rows of this order]}, None) over CLEARED_STATUSES, or (None, code) on any read failure."""
    query = ({"bet_ids": [order["bet_id"]]} if order.get("bet_id") is not None
             else {"customer_order_refs": [order["customer_order_ref"]]})
    hits = {}
    for status in CLEARED_STATUSES:
        items, error = _read(lambda: read.iter_cleared_orders(status, settled_from_utc=since_utc, **query))
        if error is not None:
            return None, error
        hits[status] = [item for item in items if _is_this_order(item, order)]
    return hits, None


def _cleared_bet_id(order, hits):
    """(bet_id, None) when this order's cleared rows (any status) name exactly one valid betId, equal to
    the order's own when it has one; (None, "cleared_rows_inconsistent") otherwise; (None, None) when
    there are no rows. Rows only establish that the bet exists: nothing is sized or closed from them
    (the live evidence rule), so the order keeps every unaccounted size as remaining, at worst case."""
    rows = [item for status in CLEARED_STATUSES for item in hits.get(status, ())]
    if not rows:
        return None, None
    bet_ids = {item.get("betId") if isinstance(item, dict) else None for item in rows}
    hit_bet = next(iter(bet_ids))
    if (len(bet_ids) != 1 or not isinstance(hit_bet, str) or not _BET_ID_RE.fullmatch(hit_bet)
            or order.get("bet_id") not in (None, hit_bet)):
        return None, "cleared_rows_inconsistent"
    return hit_bet, None


# --- UNKNOWN outcomes --------------------------------------------------------------------------


def _result(order, *, final_state, move, reason, polls, flags, halt=None, clean=False):
    ref, market_id = order["customer_order_ref"], order["market_id"]
    successful = sum(1 for _, ok in polls if ok)
    payload = {"reason": reason, "successful_polls": successful, "polls": len(polls), "flags": list(flags)}
    if move is not None:
        state, fields = move
        payload["transitions"] = [{"customer_order_ref": ref, "state": state, "reason": reason,
                                   "fields": dict(fields)}]
    events = [{"kind": "reconcile", "payload": payload, "market_id": market_id, "customer_order_ref": None}]
    if halt is not None:
        scope = halt[1]
        events.append({"kind": "halt", "payload": {"reason": halt[0], "scope": scope},
                       "market_id": market_id if scope == "market" else None, "customer_order_ref": None})
    return {"customer_order_ref": ref, "final_state": final_state, "transition": list(move) if move else None,
            "reason": reason, "successful_polls": successful, "polls": len(polls), "flags": list(flags),
            "halt": None if halt is None else {"reason": halt[0], "scope": halt[1]}, "clean": clean, "events": events}


def _still_unknown(order, reason, polls, flags, halt=None):
    """The order stays as it is: UNKNOWN (attempt counted) or ABSENT_UNCONFIRMED (no move)."""
    successful = sum(1 for _, ok in polls if ok)
    move = None
    final = order["state"]
    if order["state"] in ("UNKNOWN", "SUBMITTING", "CANCEL_PENDING", "REPLACE_PENDING"):
        move = ("UNKNOWN", {"successful_polls": successful})
        final = "UNKNOWN"
    return _result(order, final_state=final, move=move, reason=reason, polls=polls, flags=flags, halt=halt)


def _found(order, item, polls, flags, reason):
    move = found_move(item, order)
    if move is None:
        return _still_unknown(order, "found_order_terms_or_sizes_inconsistent", polls,
                              flags + ["found_but_unusable"], halt=("found_order_inconsistent", "market"))
    state, fields = move
    fields = dict(fields, successful_polls=sum(1 for _, ok in polls if ok))
    return _result(order, final_state=state, move=(state, fields), reason=reason, polls=polls, flags=flags,
                   clean=False)


def _matches_order(item, order):
    if not isinstance(item, dict) or item.get("marketId") != order["market_id"]:
        return False
    if order.get("bet_id") is not None and item.get("betId") == order["bet_id"]:
        return True
    return item.get("customerOrderRef") == order["customer_order_ref"]


def reconcile_unknown(read, order, *, cfg, clock, strategy_refs, known_bet_ids=(), live_app_key=None):
    """Resolve one unresolved order (spec section 9.7). -> {"final_state", "transition", "events", "halt", "clean", ...}

    cfg is the config (or its orders.reconcile section). known_bet_ids (an addition) are every betId
    the journal knows, for the ref-independent search. live_app_key (an addition, a Secret) adds the
    sourceIdKey leg to the ref-independent search, so a bet placed with our key but without our
    strategy ref is still a candidate (it only halts). Found and cleared evidence follows the live
    evidence rule (module docstring): it never lowers the order's counted worst case.
    "clean" is True only when the order is still absent after a complete pass (what --accept needs).
    A kill never waits for this call (it takes no gateway lock, amendment A3 D4), so nothing here
    watches for one.

    Polling is paced and bounded on the monotonic clock: the window's remaining length is read off
    the wall clock once, at the start, and every poll time, the window span, the final-5-s rule and
    the hard stop are monotonic. The wall clock only dates Betfair's searches.
    """
    rec = _reconcile_cfg(cfg)
    ref, market_id, bet_id = order["customer_order_ref"], order["market_id"], order.get("bet_id")
    now = clock.time_ns()
    mono_start = clock.monotonic_ns()
    observed = _ts(order, "outcome_observed_at", now)
    submitted = _ts(order, "submitted_at", None)
    t0 = submitted if submitted is not None else observed
    window_ns = int(rec["window_s"] * _NS_PER_S)
    deadline = max(t0 + window_ns + int(rec["grace_s"] * _NS_PER_S), observed + window_ns)
    block_ns = int(max(MAX_BLOCK_S, rec["window_s"] + rec["grace_s"] + 10) * _NS_PER_S)
    # Monotonic equivalents: the window still to run (never beyond the hard stop), and the
    # observation time (never later than now: it was journaled before this call).
    deadline_mono = mono_start + min(max(0, deadline - now), block_ns)
    hard_stop_mono = mono_start + block_ns
    observed_mono = mono_start - max(0, now - observed)
    tolerance_ns = CLOCK_STEP_TOLERANCE_S * _NS_PER_S
    known = set(known_bet_ids or ())
    flags, polls = [], []
    query = ({"bet_ids": [bet_id]} if bet_id is not None
             else {"customer_order_refs": [ref], "market_ids": [market_id]})
    while True:
        t = clock.monotonic_ns()
        items, error = _read(lambda: read.iter_current_orders(order_projection="ALL", **query))
        polls.append((t, error is None))
        if error is None:
            matches = [item for item in items if _matches_order(item, order)]
            if len(matches) > 1:
                return _still_unknown(order, "several_exchange_orders_match", polls, flags,
                                      halt=("unattributed_candidate_order", "global"))
            if matches:
                return _found(order, matches[0], polls, flags, "found_by_bet_id" if bet_id else "found_by_ref")
        if abs((clock.time_ns() - now) - (clock.monotonic_ns() - mono_start)) > tolerance_ns:
            return _still_unknown(order, "clock_step_during_reconcile", polls, flags + ["wall_clock_stepped"])
        if t >= deadline_mono and _absent_ready(polls, observed_mono, rec):
            break
        if clock.monotonic_ns() >= hard_stop_mono:
            return _still_unknown(order, "reconcile_window_incomplete", polls, flags)
        clock.sleep(rec["poll_interval_s"])
    refs = sorted({order["customer_strategy_ref"]} | {r for r in strategy_refs or () if isinstance(r, str) and r})
    since_search = utc_ms_from_ns(t0 - SEARCH_BACK_S * _NS_PER_S)
    search, error = _read(lambda: read.iter_current_orders(
        market_ids=[market_id], customer_strategy_refs=refs[:MAX_IDS_PER_REQUEST],
        date_range={"from": since_search}, order_by="BY_PLACE_TIME", order_projection="ALL"))
    if error is not None:
        return _still_unknown(order, "search_read_failed", polls, flags + ["search_failed:" + error])
    if isinstance(live_app_key, Secret):
        keyed, error = _read(lambda: read.iter_current_orders(
            market_ids=[market_id], date_range={"from": since_search}, order_by="BY_PLACE_TIME",
            order_projection="ALL", include_source_id=True))
        if error is not None:
            return _still_unknown(order, "search_read_failed", polls, flags + ["search_failed:" + error])
        listed = {item.get("betId") for item in search if isinstance(item, dict)}
        search = search + [item for item in keyed if placed_with_key(item, live_app_key)
                           and item.get("betId") not in listed]
    else:
        flags = flags + ["source_id_key_leg_skipped_no_live_key"]
    own = [item for item in search if _matches_order(item, order)]
    if own:
        return _found(order, own[0], polls, flags, "found_by_search")
    candidates = [item for item in search if isinstance(item, dict) and item.get("marketId") == market_id
                  and _terms_match(item, order) and item.get("betId") not in known]
    if candidates:
        return _still_unknown(order, "unattributed_candidate_order", polls, flags,
                              halt=("unattributed_candidate_order", "global"))
    cleared_from = utc_from_ns(t0 - CLEARED_BACK_S * _NS_PER_S)
    hits, error = cleared_hits(read, order, since_utc=cleared_from)
    if error is not None:
        return _still_unknown(order, "cleared_read_failed", polls, flags + ["cleared_failed:" + error])
    hit_bet, reason = _cleared_bet_id(order, hits)
    if reason is not None:
        return _still_unknown(order, reason, polls, flags)
    if hit_bet is not None:
        # The bet exists: record its betId and keep every size not accounted for as remaining (worst
        # case) until settle-market. Never LAPSED, VOIDED or SETTLED from cleared rows.
        fields = {"bet_id": hit_bet, "error_code": "cleared_row_bet_exists_worst_case",
                  "size_remaining_p": order["size_p"] - order["size_matched_p"] - order["size_cancelled_p"]
                  - order["size_lapsed_p"] - (order.get("size_voided_p") or 0),
                  "successful_polls": sum(1 for _, ok in polls if ok)}
        return _result(order, final_state="EXECUTABLE", move=("EXECUTABLE", fields),
                       reason="cleared_row_bet_exists_worst_case", polls=polls, flags=flags)
    if order["size_matched_p"] > 0:
        # A known matched size is the exchange's own evidence that the bet exists: never "absent".
        return _still_unknown(order, "matched_size_known_never_absent", polls, flags + ["matched_size_known"],
                              halt=("matched_size_known_never_absent", "market"))
    if order["state"] == "ABSENT_UNCONFIRMED":
        return _result(order, final_state="ABSENT_UNCONFIRMED", move=None, reason="still_absent", polls=polls,
                       flags=flags, clean=True)
    fields = {"successful_polls": sum(1 for _, ok in polls if ok)}
    return _result(order, final_state="ABSENT_UNCONFIRMED", move=("ABSENT_UNCONFIRMED", fields),
                   reason="absent_after_window", polls=polls, flags=flags, clean=True)


# --- startup -----------------------------------------------------------------------------------


def _strategy_batches(strategy_refs):
    refs = sorted({r for r in strategy_refs or () if isinstance(r, str) and r})
    return [refs[i:i + MAX_IDS_PER_REQUEST] for i in range(0, len(refs), MAX_IDS_PER_REQUEST)]


def _placed_today(item, since_ns):
    try:
        return parse_utc_ns(item.get("placedDate")) >= since_ns
    except (ValueError, TypeError):
        return True          # an unreadable date counts (the conservative reading for R6)


def startup_reconcile(read, journal_state, *, strategy_refs, live_app_key, since_utc, clock):
    """Today's exchange orders against the journal (spec section 9.9).

    -> {"foreign": [{"bet_id", "market_id", "leg"}] (None if any read failed), "exchange_open": [bet_id],
    "contradictions": [{"bet_id", "market_id", "journal_state"[, "reason"]}] (an addition), "exchange_orders_today": n,
    "exchange_realized_today_pence": int|None, "read_failures", "notes"}. See the module docstring for
    when an exchange bet counts as ours.
    Realised P&L is per market from bet-level profit; a winning market whose commission is unknown
    counts as 0 (commission never exceeds the winnings), so the figure never overstates a profit.
    """
    since_ns = parse_utc_ns(since_utc)
    orders = list((journal_state or {}).get("orders", {}).values())
    by_ref = {o["customer_order_ref"]: o for o in orders}
    by_bet = {o["bet_id"]: o for o in orders if o.get("bet_id") is not None}
    failures, notes = [], []
    seen = {}          # bet_id -> {"market_id", "legs", "placed_today", "open", "ref", "matched"}

    def take(items, leg, *, current, status=None):
        for item in items:
            if not isinstance(item, dict):
                failures.append("unexpected_item_shape")
                continue
            bet_id, market_id = item.get("betId"), item.get("marketId")
            if not isinstance(bet_id, str) or not _BET_ID_RE.fullmatch(bet_id) or not isinstance(market_id, str) \
                    or not MARKET_ID_RE.fullmatch(market_id):
                failures.append("unexpected_item_shape")
                continue
            entry = seen.setdefault(bet_id, {"market_id": market_id, "legs": set(), "placed_today": False,
                                             "open": False, "ref": None, "matched": 0})
            entry["legs"].add(leg)
            entry["placed_today"] |= _placed_today(item, since_ns)
            entry["open"] |= current and item.get("status") == "EXECUTABLE"
            if isinstance(item.get("customerOrderRef"), str):
                entry["ref"] = item["customerOrderRef"]
            # The exchange's matched size: sizeMatched while current, sizeSettled on a SETTLED row
            # (LAPSED and CANCELLED rows carry no matched part). An unreadable size is not compared.
            size = pence(item.get("sizeMatched")) if current else (
                pence(item.get("sizeSettled")) if status == "SETTLED" else None)
            if size is not None:
                entry["matched"] = max(entry["matched"], size)

    since_wire = utc_ms_from_ns(since_ns)
    for batch in _strategy_batches(strategy_refs):
        items, error = _read(lambda: read.iter_current_orders(customer_strategy_refs=batch, order_projection="ALL",
                                                              date_range={"from": since_wire},
                                                              include_source_id=True))
        if error:
            failures.append("current_by_strategy:" + error)
        else:
            take(items, "strategy_ref", current=True)
    settled_items = []
    for status in ("SETTLED", "LAPSED", "CANCELLED"):
        for batch in _strategy_batches(strategy_refs):
            items, error = _read(lambda: read.iter_cleared_orders(status, settled_from_utc=since_utc,
                                                                  customer_strategy_refs=batch,
                                                                  include_source_id=True))
            if error:
                failures.append(f"cleared_{status.lower()}:" + error)
                continue
            take(items, "strategy_ref", current=False, status=status)
            if status == "SETTLED":
                settled_items.extend(items)
    if isinstance(live_app_key, Secret):
        items, error = _read(lambda: read.iter_current_orders(order_projection="ALL", date_range={"from": since_wire},
                                                              include_source_id=True))
        if error:
            failures.append("current_by_source_id_key:" + error)
        else:
            absent = sum(1 for item in items if isinstance(item, dict) and "sourceIdKey" not in item)
            if absent:
                notes.append("source_id_key_absent_on_some_orders")
            take([item for item in items if placed_with_key(item, live_app_key)], "source_id_key", current=True)
    else:
        notes.append("source_id_key_leg_skipped_no_live_key")
    foreign, contradictions = [], []
    for bet_id in sorted(seen):
        entry = seen[bet_id]
        order = by_bet.get(bet_id)
        if order is None and entry["ref"] is not None:
            # A ref proves nothing once its journal order has a betId or is closed: only an unresolved
            # order still waiting to learn its betId can own a bet by ref.
            candidate = by_ref.get(entry["ref"])
            if candidate is not None and candidate.get("bet_id") is None and candidate["state"] in UNRESOLVED_STATES:
                order = candidate
        if order is None:
            foreign.append({"bet_id": bet_id, "market_id": entry["market_id"],
                            "leg": sorted(entry["legs"])[0]})
            continue
        if order["state"] in _OPEN_JOURNAL_STATES:
            continue                   # the journal lags an open bet's fills (live never refreshes them)
        if entry["open"]:
            contradictions.append({"bet_id": bet_id, "market_id": entry["market_id"],
                                   "journal_state": order["state"]})
            continue
        # The size a closed journal order accounts for: nothing for a zero-matched state; its matched
        # size, plus, for an order settled by hand (settle-market books the worst case of matched +
        # remaining), its recorded remainder, so a stale matched size is no false contradiction.
        if order["state"] in _ZERO_MATCHED_STATES:
            bound = 0
        else:
            bound = order["size_matched_p"] + (order["size_remaining_p"] if order["state"] == "SETTLED" else 0)
        if entry["matched"] > bound:
            contradictions.append({"bet_id": bet_id, "market_id": entry["market_id"],
                                   "journal_state": order["state"], "reason": "exchange_matched_size_exceeds_journal"})
    realized = None if failures else _realized_today(read, settled_items, strategy_refs, since_utc, failures)
    return {"foreign": None if failures else foreign, "contradictions": contradictions,
            "exchange_open": sorted(b for b, e in seen.items() if e["open"]),
            "exchange_orders_today": sum(1 for e in seen.values() if e["placed_today"]),
            "exchange_realized_today_pence": realized, "read_failures": failures, "notes": notes}


def _realized_today(read, settled_items, strategy_refs, since_utc, failures):
    gross = {}
    for item in settled_items:
        profit = profit_pence(item.get("profit")) if isinstance(item, dict) else None
        if profit is None:
            return None
        market_id = item.get("marketId")
        gross[market_id] = gross.get(market_id, 0) + profit
    commission = {}
    if any(value > 0 for value in gross.values()):
        for batch in _strategy_batches(strategy_refs):
            items, error = _read(lambda: read.iter_cleared_orders("SETTLED", settled_from_utc=since_utc,
                                                                  customer_strategy_refs=batch, group_by="MARKET"))
            if error:
                break
            for item in items:
                if isinstance(item, dict) and item.get("marketId") in gross:
                    value = _ceil_pence(item.get("commission"))
                    if value is not None:
                        commission[item["marketId"]] = commission.get(item["marketId"], 0) + value
    total = 0
    for market_id, value in gross.items():
        if value <= 0:
            total += value
        elif market_id in commission:
            total += max(0, value - commission[market_id])
    return total
