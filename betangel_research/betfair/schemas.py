"""Exact-key validators for the records shared between units: book snapshots, v1 instructions,
normalised place/cancel/replace reports and folded order records.

Paper and live produce the same report shapes, so the lifecycle code cannot tell them apart.
Prices inside order records are integer hundredths and sizes integer pence; book ladders keep
Betfair's decimal values. A snapshot that lacks catalogue data or carries a non-finite size must
say so in invalid_reason; it is never silently usable.
"""

import math
import re

from .constants import (CLASSIC_BANDS, CLASSIC_MAX_PRICE_H, CUSTOMER_ORDER_REF_PATTERN, CUSTOMER_REF_PATTERN,
                        CUSTOMER_STRATEGY_REF_PATTERN, MARKET_STATUSES, SIDES)
from .filters import MARKET_ID_RE
from .receipts import parse_utc_ns

BOOK_SNAPSHOT_KEYS = frozenset({
    "market_id", "source", "fetched_by", "key_type", "app_key_slot", "observed_at", "monotonic_ns", "pt",
    "status", "inplay", "version", "bet_delay", "is_market_data_delayed", "catalogue_present", "market_time",
    "number_of_winners", "betting_type", "market_type", "event_type_id", "price_ladder_type", "invalid_reason",
    "runners"})
RUNNER_KEYS = frozenset({"status", "handicap", "ltp", "back", "lay", "traded", "total_matched"})
SNAPSHOT_SOURCES = frozenset({"rest_listMarketBook", "stream_cache", "recording", "fixture"})
SNAPSHOT_FETCHED_BY = frozenset({"gateway_self_fetch", "read_command", "stream_cache", "recording", "fixture"})
SNAPSHOT_KEY_TYPES = frozenset({"delayed", "live", "historical", "synthetic"})
# ladder_partial_after_suspension (an addition): a full-depth (atb/atl) stream ladder after the market
# reopened without a new image holds only the price points re-sent since then (book.py).
SNAPSHOT_INVALID_REASONS = frozenset({"non_finite_size", "catalogue_missing", "ladder_partial_after_suspension",
                                      "stream_unreliable"})

INSTRUCTION_V1_KEYS = frozenset({"selection_id", "handicap", "side", "price_h", "size_p", "persistence_type"})

REPORT_EXECUTION_STATUSES = frozenset({"SUCCESS", "FAILURE", "PROCESSED_WITH_ERRORS", "TIMEOUT"})
REPORT_INSTRUCTION_STATUSES = frozenset({"SUCCESS", "FAILURE", "TIMEOUT"})
REPORT_SOURCES = frozenset({"betfair", "paper-v1"})
PLACE_REPORT_KEYS = frozenset({"status", "error_code", "market_id", "instructions", "source"})
PLACE_REPORT_INSTRUCTION_KEYS = frozenset({"customer_order_ref", "status", "error_code", "order_status", "bet_id",
                                           "placed_date", "average_price_matched_h", "size_matched_p"})
CANCEL_REPORT_KEYS = PLACE_REPORT_KEYS
CANCEL_REPORT_INSTRUCTION_KEYS = frozenset({"bet_id", "size_reduction_p", "status", "error_code",
                                            "size_cancelled_p", "cancelled_date"})
REPLACE_REPORT_KEYS = PLACE_REPORT_KEYS
REPLACE_REPORT_INSTRUCTION_KEYS = frozenset({"bet_id", "new_price_h", "status", "error_code", "cancel", "place"})

ORDER_STATES = frozenset({"RECORDED", "REFUSED", "SUBMITTING", "EXECUTABLE", "EXECUTION_COMPLETE", "FAILED",
                          "UNKNOWN", "ABSENT_UNCONFIRMED", "ABSENT_ACCEPTED", "LAPSED", "CANCEL_PENDING",
                          "REPLACE_PENDING", "REPLACED", "SETTLED", "VOIDED"})
EXPOSURE_BASES = frozenset({"none", "matched_exact", "adverse_unmatched", "adverse_unknown"})
# Spec section 9.4: these block placement and count at full adverse exposure (so does a provisional child).
UNRESOLVED_STATES = frozenset({"SUBMITTING", "UNKNOWN", "ABSENT_UNCONFIRMED", "CANCEL_PENDING", "REPLACE_PENDING"})
TERMINAL_STATES = frozenset({"REFUSED", "FAILED", "ABSENT_ACCEPTED", "LAPSED", "REPLACED", "SETTLED", "VOIDED"})
ORDER_RECORD_KEYS = frozenset({
    "intent_id", "customer_order_ref", "customer_ref", "customer_strategy_ref", "mode", "market_id", "selection_id",
    "handicap", "side", "price_h", "size_p", "persistence_type", "state", "bet_id", "size_matched_p",
    "average_price_matched_h", "size_remaining_p", "size_cancelled_p", "size_lapsed_p", "size_voided_p",
    "submitted_at", "submitted_monotonic_ns", "body_sha256", "outcome_observed_at", "successful_polls",
    "last_report_at", "parent_ref", "child_ref", "provisional_child", "is_probe", "error_code",
    "reconcile_attempts", "settled_profit_own_p", "settled_profit_betfair_p", "exposure_basis"})

_BET_ID_RE = re.compile(r"[0-9]{1,20}")      # ASCII digits only, never \d
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CODE_RE = re.compile(r"[A-Za-z0-9_\-.:]{1,80}")


def _classic_ladder():
    prices = set()
    for low, high, step in CLASSIC_BANDS:
        prices.update(range(low, high, step))
    prices.add(CLASSIC_MAX_PRICE_H)
    return frozenset(prices)


_CLASSIC_PRICES = _classic_ladder()


def _finite(value):
    """math.isfinite without the OverflowError a huge int raises."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _exact(value, keys, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    if set(value) != keys:
        missing = sorted(keys - set(value))
        extra = sorted(str(k) for k in set(value) - keys)
        raise ValueError(f"{name} keys must be exact (missing={missing}, unexpected={extra})")


def _int(value, name, *, minimum=None, optional=False):
    if value is None and optional:
        return
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be an integer" + (f" >= {minimum}" if minimum is not None else ""))


def _number(value, name, *, minimum=None, optional=False, above=None):
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if above is not None and not value > above:
        raise ValueError(f"{name} must be > {above}")


def _text(value, name, *, optional=False, pattern=None):
    if value is None and optional:
        return
    if not isinstance(value, str) or not value or (pattern is not None and not re.fullmatch(pattern, value)):
        raise ValueError(f"{name} must be a valid string")


def _timestamp(value, name, *, optional=False):
    if value is None and optional:
        return
    try:
        parse_utc_ns(value)
    except ValueError:
        raise ValueError(f"{name} must be a UTC ISO timestamp with Z or an offset") from None


def _bool(value, name, *, optional=False):
    if value is None and optional:
        return
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")


def _choice(value, choices, name):
    try:
        allowed = value in choices
    except TypeError:
        allowed = False
    if not allowed or isinstance(value, bool):
        raise ValueError(f"{name} is not an allowed value")


def _ladder(levels, name, *, descending):
    if not isinstance(levels, list):
        raise ValueError(f"{name} must be a list of [price, size] pairs")
    previous = None
    has_null = False
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise ValueError(f"{name} levels must be [price, size] pairs")
        price, size = level
        _number(price, f"{name} price", above=1)
        if size is None:
            has_null = True
        else:
            _number(size, f"{name} size", minimum=0)
        if previous is not None and (price >= previous if descending else price <= previous):
            raise ValueError(f"{name} must be strictly {'descending' if descending else 'ascending'} by price")
        previous = price
    return has_null


def validate_book_snapshot(s):
    """Book snapshot (I7). back is best (highest) first; lay is best (lowest) first."""
    _exact(s, BOOK_SNAPSHOT_KEYS, "book snapshot")
    if not isinstance(s["market_id"], str) or not MARKET_ID_RE.fullmatch(s["market_id"]):
        raise ValueError("market_id must look like 1.<digits>")
    _choice(s["source"], SNAPSHOT_SOURCES, "source")
    _choice(s["fetched_by"], SNAPSHOT_FETCHED_BY, "fetched_by")
    _choice(s["key_type"], SNAPSHOT_KEY_TYPES, "key_type")
    _choice(s["app_key_slot"], {"delayed", "live", None}, "app_key_slot")
    _timestamp(s["observed_at"], "observed_at")
    _int(s["monotonic_ns"], "monotonic_ns", minimum=0, optional=True)
    _int(s["pt"], "pt", minimum=0, optional=True)
    _choice(s["status"], set(MARKET_STATUSES), "status")
    _bool(s["inplay"], "inplay")
    _int(s["version"], "version", minimum=0, optional=True)
    _int(s["bet_delay"], "bet_delay", minimum=0)
    _bool(s["is_market_data_delayed"], "is_market_data_delayed", optional=True)
    _bool(s["catalogue_present"], "catalogue_present")
    _timestamp(s["market_time"], "market_time", optional=True)
    _int(s["number_of_winners"], "number_of_winners", minimum=0, optional=True)
    for key in ("betting_type", "market_type", "event_type_id", "price_ladder_type"):
        _text(s[key], key, optional=True)
    _choice(s["invalid_reason"], SNAPSHOT_INVALID_REASONS | {None}, "invalid_reason")
    if s["catalogue_present"] is False and s["invalid_reason"] is None:
        raise ValueError("a snapshot without catalogue data must carry invalid_reason")
    runners = s["runners"]
    if not isinstance(runners, dict):
        raise ValueError("runners must be an object keyed by selection id")
    has_null_size = False
    for selection, runner in runners.items():
        if not isinstance(selection, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", selection):
            raise ValueError("runner keys must be positive integer selection ids as strings")
        _exact(runner, RUNNER_KEYS, "runner")
        _text(runner["status"], "runner status")
        _number(runner["handicap"], "handicap")
        _number(runner["ltp"], "ltp", optional=True, above=1)
        has_null_size |= _ladder(runner["back"], "back", descending=True)
        has_null_size |= _ladder(runner["lay"], "lay", descending=False)
        traded = runner["traded"]
        if traded is not None:
            if not isinstance(traded, dict):
                raise ValueError("traded must be an object of price -> volume, or null")
            for price, volume in traded.items():
                try:
                    if not isinstance(price, str) or not float(price) > 1 or not math.isfinite(float(price)):
                        raise ValueError
                except ValueError:
                    raise ValueError("traded keys must be decimal price strings above 1") from None
                if volume is None:
                    has_null_size = True
                else:
                    _number(volume, "traded volume", minimum=0)
        _number(runner["total_matched"], "total_matched", minimum=0, optional=True)
    if has_null_size and s["invalid_reason"] is None:
        raise ValueError("a snapshot with a null (non-finite) size must carry invalid_reason")
    return s


def validate_instruction_v1(i):
    """Plain LIMIT instruction: integer hundredths on the CLASSIC ladder, integer pence, LAPSE only."""
    _exact(i, INSTRUCTION_V1_KEYS, "instruction")
    _int(i["selection_id"], "selection_id", minimum=1)
    if isinstance(i["handicap"], bool) or i["handicap"] not in (0, 0.0):
        raise ValueError("handicap must be 0")
    _choice(i["side"], set(SIDES), "side")
    _int(i["price_h"], "price_h")
    if i["price_h"] not in _CLASSIC_PRICES:
        raise ValueError("price_h must be exactly on the CLASSIC ladder")
    _int(i["size_p"], "size_p", minimum=1)
    if i["persistence_type"] != "LAPSE":
        raise ValueError("persistence_type must be LAPSE")
    return i


def _report_envelope(r, keys, name):
    _exact(r, keys, name)
    _choice(r["status"], REPORT_EXECUTION_STATUSES, f"{name} status")
    _text(r["error_code"], f"{name} error_code", optional=True, pattern=_CODE_RE.pattern)
    if not isinstance(r["market_id"], str) or not MARKET_ID_RE.fullmatch(r["market_id"]):
        raise ValueError(f"{name} market_id must look like 1.<digits>")
    _choice(r["source"], REPORT_SOURCES, f"{name} source")
    if not isinstance(r["instructions"], list):
        raise ValueError(f"{name} instructions must be a list")


def _place_instruction_report(item, name):
    _exact(item, PLACE_REPORT_INSTRUCTION_KEYS, name)
    _text(item["customer_order_ref"], "customer_order_ref", optional=True, pattern=CUSTOMER_ORDER_REF_PATTERN)
    _choice(item["status"], REPORT_INSTRUCTION_STATUSES, f"{name} status")
    _text(item["error_code"], "error_code", optional=True, pattern=_CODE_RE.pattern)
    # Any string is kept: an unexpected orderStatus must reach the lifecycle rules (-> UNKNOWN).
    _text(item["order_status"], "order_status", optional=True)
    _text(item["bet_id"], "bet_id", optional=True, pattern=_BET_ID_RE.pattern)
    _timestamp(item["placed_date"], "placed_date", optional=True)
    _int(item["average_price_matched_h"], "average_price_matched_h", minimum=0, optional=True)
    _int(item["size_matched_p"], "size_matched_p", minimum=0, optional=True)


def _cancel_instruction_report(item, name):
    _exact(item, CANCEL_REPORT_INSTRUCTION_KEYS, name)
    _text(item["bet_id"], "bet_id", optional=True, pattern=_BET_ID_RE.pattern)
    _int(item["size_reduction_p"], "size_reduction_p", minimum=1, optional=True)
    _choice(item["status"], REPORT_INSTRUCTION_STATUSES, f"{name} status")
    _text(item["error_code"], "error_code", optional=True, pattern=_CODE_RE.pattern)
    _int(item["size_cancelled_p"], "size_cancelled_p", minimum=0, optional=True)
    _timestamp(item["cancelled_date"], "cancelled_date", optional=True)


def validate_place_report(r):
    """Normalised place report; identical shape for source "betfair" and "paper-v1"."""
    _report_envelope(r, PLACE_REPORT_KEYS, "place report")
    for item in r["instructions"]:
        _place_instruction_report(item, "place instruction report")
    return r


def validate_cancel_report(r):
    _report_envelope(r, CANCEL_REPORT_KEYS, "cancel report")
    for item in r["instructions"]:
        _cancel_instruction_report(item, "cancel instruction report")
    return r


def validate_replace_report(r):
    """Each replace instruction carries its cancel half and place half (either may be null)."""
    _report_envelope(r, REPLACE_REPORT_KEYS, "replace report")
    for item in r["instructions"]:
        _exact(item, REPLACE_REPORT_INSTRUCTION_KEYS, "replace instruction report")
        _text(item["bet_id"], "bet_id", optional=True, pattern=_BET_ID_RE.pattern)
        _int(item["new_price_h"], "new_price_h", optional=True)
        if item["new_price_h"] is not None and item["new_price_h"] not in _CLASSIC_PRICES:
            raise ValueError("new_price_h must be exactly on the CLASSIC ladder")
        _choice(item["status"], REPORT_INSTRUCTION_STATUSES, "replace instruction report status")
        _text(item["error_code"], "error_code", optional=True, pattern=_CODE_RE.pattern)
        if item["cancel"] is not None:
            _cancel_instruction_report(item["cancel"], "replace cancel half")
        if item["place"] is not None:
            _place_instruction_report(item["place"], "replace place half")
    return r


def validate_order_record(o):
    """Order record (I8) as built by journal.fold()."""
    _exact(o, ORDER_RECORD_KEYS, "order record")
    _text(o["intent_id"], "intent_id")
    _text(o["customer_order_ref"], "customer_order_ref", pattern=CUSTOMER_ORDER_REF_PATTERN)
    _text(o["customer_ref"], "customer_ref", pattern=CUSTOMER_REF_PATTERN)     # spec section 3: not nullable
    _text(o["customer_strategy_ref"], "customer_strategy_ref", pattern=CUSTOMER_STRATEGY_REF_PATTERN)
    _choice(o["mode"], {"paper", "live"}, "mode")
    if not isinstance(o["market_id"], str) or not MARKET_ID_RE.fullmatch(o["market_id"]):
        raise ValueError("market_id must look like 1.<digits>")
    _int(o["selection_id"], "selection_id", minimum=1)
    if isinstance(o["handicap"], bool) or o["handicap"] not in (0, 0.0):
        raise ValueError("handicap must be 0")
    _choice(o["side"], set(SIDES), "side")
    _int(o["price_h"], "price_h")
    if o["price_h"] not in _CLASSIC_PRICES:
        raise ValueError("price_h must be exactly on the CLASSIC ladder")
    _int(o["size_p"], "size_p", minimum=1)
    if o["persistence_type"] != "LAPSE":
        raise ValueError("persistence_type must be LAPSE")
    _choice(o["state"], ORDER_STATES, "state")
    _text(o["bet_id"], "bet_id", optional=True, pattern=_BET_ID_RE.pattern)
    for key in ("size_matched_p", "size_remaining_p", "size_cancelled_p", "size_lapsed_p", "size_voided_p",
                "successful_polls", "reconcile_attempts"):
        _int(o[key], key, minimum=0)
    _int(o["average_price_matched_h"], "average_price_matched_h", minimum=0, optional=True)
    for key in ("submitted_at", "outcome_observed_at", "last_report_at"):
        _timestamp(o[key], key, optional=True)
    _int(o["submitted_monotonic_ns"], "submitted_monotonic_ns", minimum=0, optional=True)
    _text(o["body_sha256"], "body_sha256", optional=True, pattern=_SHA256_RE.pattern)
    for key in ("parent_ref", "child_ref"):
        _text(o[key], key, optional=True, pattern=CUSTOMER_ORDER_REF_PATTERN)
    child = o["provisional_child"]
    if child is not None and type(child) is not bool:
        _text(child, "provisional_child", pattern=CUSTOMER_ORDER_REF_PATTERN)
    _bool(o["is_probe"], "is_probe")
    _text(o["error_code"], "error_code", optional=True)
    _int(o["settled_profit_own_p"], "settled_profit_own_p", optional=True)
    _int(o["settled_profit_betfair_p"], "settled_profit_betfair_p", optional=True)
    _choice(o["exposure_basis"], EXPOSURE_BASES, "exposure_basis")
    _order_record_consistency(o)
    return o


def _order_record_consistency(o):
    """Refuse records that could count an unresolved or open order as less than its exposure.

    An unknown outcome is never zero (spec section 0.3 item 6): unresolved states and provisional
    children must be adverse_unknown. Known sizes never exceed the order size. Betfair's sizeVoided
    does not overlap sizeMatched (the Exchange Stream VAR example moves sm 2 to sv 2), so outside
    VOIDED the voided size counts towards the total; a VOIDED record keeps the matched size it voided
    (size_voided_p repeats it), so there voided is bounded on its own.
    """
    state, basis = o["state"], o["exposure_basis"]
    parts = ("size_matched_p", "size_remaining_p", "size_cancelled_p", "size_lapsed_p")
    total = sum(o[key] for key in parts) + (o["size_voided_p"] if state != "VOIDED" else 0)
    if total > o["size_p"] or o["size_voided_p"] > o["size_p"]:
        raise ValueError("matched, remaining, cancelled, lapsed and voided sizes must not exceed size_p")
    if (state in UNRESOLVED_STATES or o["provisional_child"] is True) and basis != "adverse_unknown":
        raise ValueError("an unresolved order or provisional child must have exposure_basis adverse_unknown")
    # RECORDED was never sent (submit is journaled before any byte goes out), so it carries none.
    exchange_side = state not in TERMINAL_STATES and state != "RECORDED"
    if exchange_side and o["size_remaining_p"] > 0 and basis not in ("adverse_unmatched", "adverse_unknown"):
        raise ValueError("an open order with remaining size must count it adversarially")
    if exchange_side and o["size_matched_p"] > 0 and basis == "none":
        raise ValueError("an order with matched size cannot have exposure_basis none")
