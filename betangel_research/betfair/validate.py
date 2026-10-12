"""Pre-trade checks V1-V14 and V9b (spec section 10, rule R4), and the order reference generators.

Each validate_* function returns every failure as {"rule": "V<n>", "reason": snake_case,
"instruction": index or None}; an empty list means the request passed. Nothing is snapped,
rounded or defaulted: an off-ladder price, a stake with a fraction of a penny, an unexpected
key or a stale book is refused. "V0" is the instruction shape itself (exact v1 keys), which is
how unsupported features (FOK, minFill, betTarget, MOC/LOC, async) are refused.
"""

import re
import secrets

from .constants import (ACCOUNT_CURRENCY, CANCEL_INSTRUCTIONS_LIMIT, CUSTOMER_ORDER_REF_PATTERN,
                        CUSTOMER_REF_PATTERN, CUSTOMER_STRATEGY_REF_PATTERN, MIN_BET_PENCE_GBP,
                        PLACE_INSTRUCTIONS_LIMIT, PROFIT_RATIO_BOUNDS_PERCENT, REPLACE_INSTRUCTIONS_LIMIT, SIDES)
from .config import money_pence
from .errors import ConfigError
from .filters import MARKET_ID_RE
from .receipts import parse_utc_ns, utc_from_ns
from .schemas import INSTRUCTION_V1_KEYS, TERMINAL_STATES, UNRESOLVED_STATES, validate_book_snapshot
from .ticks import is_on_ladder, ticks_between, to_hundredths

_HEX16_RE = re.compile(r"[0-9a-f]{16}")
_BET_ID_RE = re.compile(r"[0-9]{1,20}")
_NS_PER_S = 1_000_000_000
# Spec section 9.8 target-set legs that prove an exchange bet is ours (amendment A3, D4: our strategy refs and
# our journal's betIds only; a bet seen only by its sourceIdKey is never a kill target).
KILL_TARGET_LEGS = ("strategy_ref", "bet_id")
KILL_TARGET_KEYS = frozenset({"bet_id", "market_id", "leg"})


def _stamp(now_ns):
    text = utc_from_ns(now_ns)          # "YYYY-MM-DDTHH:MM:SS.ffffffZ"
    return text[2:4] + text[5:7] + text[8:10] + text[11:13] + text[14:16] + text[17:19]


def _ref(prefix, now_ns, rng):
    if type(now_ns) is not int or now_ns < 0:
        raise ValueError("now_ns must be a non-negative integer")
    token = (rng or secrets.token_hex)(8)
    if not isinstance(token, str) or not _HEX16_RE.fullmatch(token):
        raise ValueError("reference rng must return 16 lowercase hex characters")
    return f"{prefix}{_stamp(now_ns)}-{token}"


def make_customer_ref(now_ns, rng=None):
    """'brq' + yymmddHHMMSS + '-' + 16 hex = 32 characters; used once, never reused."""
    return _ref("brq", now_ns, rng)


def make_customer_order_ref(now_ns, rng=None):
    """'bro' + yymmddHHMMSS + '-' + 16 hex = 32 characters; used once, never reused."""
    return _ref("bro", now_ns, rng)


def _round_half_up(cp):
    return (cp + 50) // 100


def _round_half_even(cp):
    quotient, remainder = divmod(cp, 100)
    if remainder > 50 or (remainder == 50 and quotient % 2 == 1):
        return quotient + 1
    return quotient


def profit_ratio_ok(size_p, price_h):
    """F39 INVALID_PROFIT_RATIO guard: the penny-rounded backer profit must lie within 80%-125%.

    Exact profit is size * (price - 1) in hundredths of a penny. Betfair's rounding mode is not
    documented, so both half-up and half-even rounding must pass.
    """
    if type(size_p) is not int or size_p <= 0 or type(price_h) is not int or price_h <= 100:
        raise ValueError("profit ratio needs positive integer pence and hundredths above 1.00")
    exact = size_p * (price_h - 100)
    low, high = PROFIT_RATIO_BOUNDS_PERCENT
    for rounded in (_round_half_up(exact), _round_half_even(exact)):
        scaled = rounded * 100 * 100            # rounded pence in hundredths of a penny, times 100%
        if scaled < low * exact or scaled > high * exact:
            return False
    return True


def _fail(failures, rule, reason, index=None):
    failures.append({"rule": rule, "reason": reason, "instruction": index})


def _book_failures(book, market_id, config, *, mode, now_ns, now_monotonic_ns):
    """Market-level V1, V9, V9b, V10 and V12 failures, plus the validated book or None."""
    failures = []
    if not isinstance(market_id, str) or not MARKET_ID_RE.fullmatch(market_id):
        _fail(failures, "V1", "market_id_invalid")
    try:
        book = validate_book_snapshot(book)
    except (ValueError, TypeError):
        _fail(failures, "V9", "book_schema_invalid")
        return failures, None
    if book["market_id"] != market_id:
        _fail(failures, "V1", "book_market_mismatch")
    caps, rules, paper = (config["orders"][key] for key in ("caps", "rules", "paper"))
    if book["status"] != "OPEN":
        _fail(failures, "V9", "market_not_open")
    if book["inplay"] is not False:
        _fail(failures, "V9", "market_inplay")
    if book["version"] is None:
        _fail(failures, "V9", "market_version_missing")
    if not book["catalogue_present"]:
        _fail(failures, "V9", "catalogue_missing")
    if book["invalid_reason"] is not None:
        _fail(failures, "V9", "book_invalid")
    if book["betting_type"] != "ODDS":
        _fail(failures, "V9", "betting_type_not_odds")
    if book["number_of_winners"] != 1:
        _fail(failures, "V9", "number_of_winners_not_1")
    if book["market_type"] not in rules["market_types"]:
        _fail(failures, "V9", "market_type_not_allowed")
    if book["event_type_id"] not in rules["event_type_ids"]:
        _fail(failures, "V9", "event_type_not_allowed")
    if book["price_ladder_type"] != "CLASSIC":
        _fail(failures, "V9", "ladder_not_classic")
    if mode == "live":
        if book["market_time"] is None:
            _fail(failures, "V9b", "market_time_missing")
        else:
            until = parse_utc_ns(book["market_time"]) - now_ns
            if until <= 0:
                _fail(failures, "V9b", "market_time_passed")
            elif until > int(caps["max_minutes_before_off"] * 60 * _NS_PER_S):
                _fail(failures, "V9b", "market_time_too_far")
        # A18: the gateway's own fresh live-key fetch, measured on the in-process monotonic clock and,
        # because that clock stops while a Mac sleeps, on the wall clock as well.
        if book["fetched_by"] != "gateway_self_fetch":
            _fail(failures, "V10", "book_not_self_fetched")
        if book["app_key_slot"] != "live":
            _fail(failures, "V10", "book_not_live_key")
        if book["is_market_data_delayed"] is not False:
            _fail(failures, "V10", "market_data_delayed_or_unknown")
        if book["monotonic_ns"] is None or type(now_monotonic_ns) is not int:
            _fail(failures, "V10", "book_age_unmeasurable")
        else:
            age = now_monotonic_ns - book["monotonic_ns"]
            if age < 0:
                _fail(failures, "V10", "book_age_negative")
            elif age > int(caps["max_book_age_s"] * _NS_PER_S):
                _fail(failures, "V10", "book_stale")
            else:
                # The monotonic clock stops while a Mac sleeps: the wall-clock age must pass as well.
                wall_age = now_ns - parse_utc_ns(book["observed_at"])
                if wall_age < 0:
                    _fail(failures, "V10", "book_age_negative")
                elif wall_age > int(caps["max_book_age_s"] * _NS_PER_S):
                    _fail(failures, "V10", "book_stale_wall_clock")
    else:
        if book["source"] == "recording" and book["pt"] is not None:
            book_ns = book["pt"] * 1_000_000
        else:
            book_ns = parse_utc_ns(book["observed_at"])
        age = now_ns - book_ns
        if age < 0:
            _fail(failures, "V10", "book_age_negative")
        elif age > int(paper["max_snapshot_age_s"] * _NS_PER_S):
            _fail(failures, "V10", "book_stale")
    if config["account_currency"] != ACCOUNT_CURRENCY:
        _fail(failures, "V12", "currency_not_gbp")
    return failures, book


def _ref_failures(config, customer_ref, customer_order_refs, count):
    failures = []
    if not isinstance(config["orders"]["customer_strategy_ref"], str) or not re.fullmatch(
            CUSTOMER_STRATEGY_REF_PATTERN, config["orders"]["customer_strategy_ref"]):
        _fail(failures, "V11", "strategy_ref_invalid")
    if customer_ref is not None and (not isinstance(customer_ref, str)
                                     or not re.fullmatch(CUSTOMER_REF_PATTERN, customer_ref)):
        _fail(failures, "V11", "customer_ref_invalid")
    if customer_order_refs is not None:
        refs = list(customer_order_refs)
        if len(refs) != count or len(set(map(str, refs))) != len(refs):
            _fail(failures, "V11", "customer_order_refs_count_or_duplicate")
        for index, ref in enumerate(refs):
            if not isinstance(ref, str) or not re.fullmatch(CUSTOMER_ORDER_REF_PATTERN, ref):
                _fail(failures, "V11", "customer_order_ref_invalid", index)
    return failures


def _shape_failures(instruction, index, config):
    """V0 and V2-V6, V14 for one instruction; returns (failures, usable)."""
    failures = []
    if not isinstance(instruction, dict) or set(instruction) != INSTRUCTION_V1_KEYS:
        _fail(failures, "V0", "unsupported_order_feature_or_keys", index)
        return failures, False
    usable = True
    selection = instruction["selection_id"]
    if type(selection) is not int or selection <= 0:
        _fail(failures, "V2", "selection_id_invalid", index)
        usable = False
    if isinstance(instruction["handicap"], bool) or instruction["handicap"] not in (0, 0.0):
        _fail(failures, "V2", "handicap_not_zero", index)
    if instruction["side"] not in SIDES or not isinstance(instruction["side"], str):
        _fail(failures, "V3", "side_invalid", index)
        usable = False
    price = instruction["price_h"]
    if not is_on_ladder(price):
        _fail(failures, "V4", "price_off_classic_ladder", index)
        usable = False
    size = instruction["size_p"]
    if type(size) is not int or size <= 0:
        _fail(failures, "V5", "size_not_whole_pence", index)
        usable = False
    else:
        try:
            min_stake = money_pence(config["orders"]["rules"]["min_stake"])
        except ConfigError:
            min_stake = None
        if min_stake is None or min_stake < MIN_BET_PENCE_GBP:
            _fail(failures, "V5", "min_stake_below_1_00_unsupported", index)
        elif size < min_stake:
            _fail(failures, "V5", "size_below_min_stake", index)
    if instruction["persistence_type"] != "LAPSE":
        _fail(failures, "V6", "persistence_not_lapse", index)
    if usable and not profit_ratio_ok(size, price):
        _fail(failures, "V14", "profit_ratio_out_of_bounds", index)
    return failures, usable


def _reference_price_h(runner, side):
    """V13 reference: best back price for a BACK, best lay price for a LAY, otherwise ltp."""
    levels = runner["back"] if side == "BACK" else runner["lay"]
    value = levels[0][0] if levels else runner["ltp"]
    if value is None:
        return None
    try:
        return to_hundredths(value)
    except ValueError:
        return "off"


def _live_orders(open_orders, market_id, *, exclude_refs=()):
    """Our orders that can still match: EXECUTABLE with size left, unresolved ones, provisional children."""
    out = []
    for order in open_orders or ():
        if order["market_id"] != market_id or order["customer_order_ref"] in exclude_refs:
            continue
        state = order["state"]
        if state in TERMINAL_STATES or state in ("RECORDED", "EXECUTION_COMPLETE"):
            continue
        if state == "EXECUTABLE" and order["size_remaining_p"] <= 0:
            continue
        out.append(order)
    return out


def _self_match_failures(candidates, existing):
    """V8: on each selection, every LAY price must sit below every BACK price."""
    failures = []
    for index, (selection, side, price) in candidates:
        for other_selection, other_side, other_price in existing + [c for i, c in candidates if i != index]:
            if other_selection != selection or other_side == side:
                continue
            lay, back = (price, other_price) if side == "LAY" else (other_price, price)
            if lay >= back:
                _fail(failures, "V8", "self_match_possible", index)
                break
    return failures


def _check_instruction(index, instruction, *, book, config, mode, probe, failures):
    """V2 runner checks, V9 runner status and V13 deviation for one structurally usable instruction."""
    caps = config["orders"]["caps"]
    runner = book["runners"].get(str(instruction["selection_id"])) if book is not None else None
    if book is not None and runner is None:
        _fail(failures, "V2", "selection_not_in_book", index)
        return
    if runner is None:
        return
    if runner["status"] != "ACTIVE":
        _fail(failures, "V9", "runner_not_active", index)
    if probe:
        return
    reference = _reference_price_h(runner, instruction["side"])
    if reference is None:
        _fail(failures, "V13", "no_reference_price", index)
    elif reference == "off" or not is_on_ladder(reference):
        _fail(failures, "V13", "reference_price_off_ladder", index)
    elif ticks_between(instruction["price_h"], reference) > caps["max_price_deviation_ticks"]:
        _fail(failures, "V13", "price_deviation_too_large", index)


def validate_place(market_id, instructions, *, book, open_orders, config, mode, now_ns, probe=False,
                   now_monotonic_ns=None, customer_ref=None, customer_order_refs=None):
    """Every V-rule failure for one place request (empty list = valid).

    open_orders: our order records (any market; filtered here) for the V8 self-match check.
    Live mode measures book age on the monotonic clock (A18), so now_monotonic_ns is required
    there; without it the book is refused as unmeasurable.
    """
    if mode not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    failures, book = _book_failures(book, market_id, config, mode=mode, now_ns=now_ns,
                                    now_monotonic_ns=now_monotonic_ns)
    if not isinstance(instructions, list) or not instructions:
        _fail(failures, "V7", "no_instructions")
        return failures
    limit = min(config["orders"]["caps"]["max_instructions_per_request"], PLACE_INSTRUCTIONS_LIMIT)
    if len(instructions) > limit:
        _fail(failures, "V7", "too_many_instructions")
    failures.extend(_ref_failures(config, customer_ref, customer_order_refs, len(instructions)))
    candidates = []
    for index, instruction in enumerate(instructions):
        shape, usable = _shape_failures(instruction, index, config)
        failures.extend(shape)
        if usable:
            _check_instruction(index, instruction, book=book, config=config, mode=mode, probe=probe,
                               failures=failures)
            candidates.append((index, (instruction["selection_id"], instruction["side"], instruction["price_h"])))
    existing = [(o["selection_id"], o["side"], o["price_h"]) for o in _live_orders(open_orders, market_id)]
    failures.extend(_self_match_failures(candidates, existing))
    return failures


def _by_bet_id(open_orders, market_id):
    found = {}
    for order in open_orders or ():
        if order["bet_id"] is not None and order["market_id"] == market_id:
            found[order["bet_id"]] = order
    return found


def validate_replace(market_id, replaces, *, book, open_orders, config, mode, now_ns, now_monotonic_ns=None,
                     customer_ref=None):
    """Every V-rule failure for one replace request; the new price is checked as a new order.

    replaces: [{"bet_id", "new_price_h"}]. Each bet must be ours, EXECUTABLE in this market with
    size remaining; V1-V14 and V9b then apply to (side, new price, remaining size).
    """
    if mode not in ("paper", "live"):
        raise ValueError("mode must be paper or live")
    failures, book = _book_failures(book, market_id, config, mode=mode, now_ns=now_ns,
                                    now_monotonic_ns=now_monotonic_ns)
    if not isinstance(replaces, list) or not replaces:
        _fail(failures, "V7", "no_instructions")
        return failures
    limit = min(config["orders"]["caps"]["max_instructions_per_request"], REPLACE_INSTRUCTIONS_LIMIT)
    if len(replaces) > limit:
        _fail(failures, "V7", "too_many_instructions")
    failures.extend(_ref_failures(config, customer_ref, None, len(replaces)))
    ours = _by_bet_id(open_orders, market_id)
    seen, candidates, parents = set(), [], []
    for index, item in enumerate(replaces):
        if not isinstance(item, dict) or set(item) != {"bet_id", "new_price_h"}:
            _fail(failures, "V0", "replace_keys_must_be_bet_id_and_new_price_h", index)
            continue
        bet_id = item["bet_id"]
        order = ours.get(bet_id) if isinstance(bet_id, str) and _BET_ID_RE.fullmatch(bet_id) else None
        if order is None:
            _fail(failures, "V2", "bet_not_ours_in_market", index)
            continue
        if bet_id in seen:
            _fail(failures, "V2", "duplicate_bet_id", index)
            continue
        seen.add(bet_id)
        if order["state"] != "EXECUTABLE" or order["size_remaining_p"] <= 0:
            _fail(failures, "V2", "bet_not_replaceable_state", index)
            continue
        if item["new_price_h"] == order["price_h"]:
            _fail(failures, "V4", "new_price_equals_current", index)
        instruction = {"selection_id": order["selection_id"], "handicap": 0, "side": order["side"],
                       "price_h": item["new_price_h"], "size_p": order["size_remaining_p"],
                       "persistence_type": "LAPSE"}
        shape, usable = _shape_failures(instruction, index, config)
        failures.extend(shape)
        if usable:
            _check_instruction(index, instruction, book=book, config=config, mode=mode, probe=False,
                               failures=failures)
            candidates.append((index, (order["selection_id"], order["side"], item["new_price_h"])))
            parents.append(order["customer_order_ref"])
    existing = [(o["selection_id"], o["side"], o["price_h"])
                for o in _live_orders(open_orders, market_id, exclude_refs=set(parents))]
    failures.extend(_self_match_failures(candidates, existing))
    return failures


def _cancellable(order):
    """Our order may still have unmatched size on the exchange: open with size left, or unresolved."""
    if order["state"] == "EXECUTABLE":
        return order["size_remaining_p"] > 0
    return order["state"] in UNRESOLVED_STATES or (order["provisional_child"] is True
                                                   and order["state"] not in TERMINAL_STATES)


def _proven_targets(kill_targets, market_id, failures):
    """Bet ids the kill sweep found on the exchange in this market by one of its target-set legs."""
    proven = set()
    if kill_targets is None:
        return proven
    if not isinstance(kill_targets, (list, tuple)):
        _fail(failures, "V0", "kill_targets_invalid")
        return proven
    for target in kill_targets:
        if (not isinstance(target, dict) or set(target) != KILL_TARGET_KEYS or target["leg"] not in KILL_TARGET_LEGS
                or not isinstance(target["bet_id"], str) or not _BET_ID_RE.fullmatch(target["bet_id"])):
            _fail(failures, "V0", "kill_targets_invalid")
            return set()
        if target["market_id"] == market_id:
            proven.add(target["bet_id"])
    return proven


def validate_cancel(market_id, cancels, *, open_orders, kill_targets=None):
    """Cancel checks: one valid market, 1-60 instructions, unique bet ids that are ours.

    cancels: [{"bet_id", "size_reduction_p"}] or bare bet id strings (full cancel). "Ours" means
    a journal order in this market that may still have unmatched size: EXECUTABLE with size
    left, or unresolved (UNKNOWN, ABSENT_UNCONFIRMED, CANCEL_PENDING, REPLACE_PENDING, a
    provisional replace child). A cancel only ever reduces exposure, so the kill sweep must be
    able to reach every one of them. kill_targets (the sweep only) adds bets found on the
    exchange by the spec section 9.8 legs: [{"bet_id", "market_id", "leg"}], full cancels only. A
    partial reduction needs an EXECUTABLE order (a known remainder) and must leave a remainder
    that passes the profit-ratio rule.
    """
    failures = []
    if not isinstance(market_id, str) or not MARKET_ID_RE.fullmatch(market_id):
        _fail(failures, "V1", "market_id_invalid")
    if not isinstance(cancels, list) or not cancels:
        _fail(failures, "V7", "no_instructions")
        return failures
    if len(cancels) > CANCEL_INSTRUCTIONS_LIMIT:
        _fail(failures, "V7", "too_many_instructions")
    ours = _by_bet_id(open_orders, market_id) if isinstance(market_id, str) else {}
    proven = _proven_targets(kill_targets, market_id, failures)
    seen = set()
    for index, item in enumerate(cancels):
        if isinstance(item, str):
            item = {"bet_id": item, "size_reduction_p": None}
        if not isinstance(item, dict) or set(item) != {"bet_id", "size_reduction_p"}:
            _fail(failures, "V0", "cancel_keys_must_be_bet_id_and_size_reduction_p", index)
            continue
        bet_id = item["bet_id"]
        if not isinstance(bet_id, str) or not _BET_ID_RE.fullmatch(bet_id):
            _fail(failures, "V2", "bet_id_invalid", index)
            continue
        if bet_id in seen:
            _fail(failures, "V2", "duplicate_bet_id", index)
            continue
        seen.add(bet_id)
        order = ours.get(bet_id)
        reduction = item["size_reduction_p"]
        if order is None or not _cancellable(order):
            if bet_id in proven and reduction is None:
                continue
            _fail(failures, "V2", "bet_not_ours_in_market" if order is None else "bet_not_cancellable_state", index)
            continue
        if reduction is not None:
            if order["state"] != "EXECUTABLE":
                _fail(failures, "V5", "size_reduction_needs_executable_order", index)
            elif type(reduction) is not int or not 0 < reduction < order["size_remaining_p"]:
                _fail(failures, "V5", "size_reduction_invalid", index)
            elif not profit_ratio_ok(order["size_remaining_p"] - reduction, order["price_h"]):
                _fail(failures, "V14", "remainder_profit_ratio_out_of_bounds", index)
    return failures
