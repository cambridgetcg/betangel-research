"""RiskGuard: pure decisions R1-R12 over integer pence, adversarial exposure, and capability issue.

evaluate_* never reads a clock, a file or the network: the same config, book, journal fold,
arming result, funds and times give the same decision, decision_id included. Exposure is worked
out in hundredths of a penny and rounded against us only at the end. Matched size counts at its
journaled price; by default (hedge_credit False) a matched portion counts only its losses, never
the payoff it would earn when it wins, so one matched bet cannot offset another (amendment A3, C1:
Betfair can lower an already matched price after a non-runner, which a journal would not see). Only
a paper guard credits that payoff in R8-R10 (self.hedge_credit); live R12 uses the credited figure,
because there the smaller explained exposure is the stricter one. Every portion that could still
match, or whose outcome is unknown (SUBMITTING, UNKNOWN, ABSENT_UNCONFIRMED, CANCEL_PENDING, a
provisional replace child), counts at its full adverse loss. A REPLACE_PENDING parent and its provisional child count as the worse of the two, per
outcome. A replace (paper only: live replace was removed in v1, so evaluate_replace refuses live
mode) is judged on the state right after its submit (every parent REPLACE_PENDING with its new
provisional child), which covers every mix of per-instruction outcomes.

issue() is the only caller of capability._mint. It mints only for a decision this guard
approved, once, in live mode, only when the decision bound the request's refs, and only after
re-reading the prepared request body and checking that it carries exactly the approved market,
refs, version and instructions.
"""

import copy
import math
from decimal import Decimal, InvalidOperation
import re

from .capability import _mint
from .config import money_pence, order_strategy_ref, pilot_enabled
from .constants import (ABSOLUTE_CEILINGS, BETTING_METHOD_PREFIX, CEILING_PATHS, CUSTOMER_ORDER_REF_PATTERN,
                        CUSTOMER_REF_PATTERN)
from .errors import ConfigError, OrderRefused
from .lifecycle import TERMINAL_STATES
from .paths import kill_engaged as _kill_file_engaged
from .receipts import SYSTEM_CLOCK, bytes_sha256, canonical_sha256, parse_utc_ns, strict_json_loads, utc_from_ns
from .schemas import INSTRUCTION_V1_KEYS
from .ticks import is_on_ladder, to_hundredths, to_pence
from .validate import validate_cancel, validate_place, validate_replace

OTHER = "other"
DAY_NS = 86_400_000_000_000
_NS_PER_S = 1_000_000_000
_UNKNOWN_SIZE_STATES = frozenset({"SUBMITTING", "UNKNOWN", "ABSENT_UNCONFIRMED", "CANCEL_PENDING",
                                  "REPLACE_PENDING", "EXECUTABLE"})
PROBE_PRICE_H = 101
PROBE_SIZE_P = 100
# The verb each decision action may be sent with, read from "<prefix><verb>Orders". The method names
# themselves stay in gateway.py; this only parses the method the gateway prepared. No replace verb:
# live replace was removed in v1, so no capability can ever be minted for a replace body.
ACTION_VERBS = {"place": "place", "cancel": "cancel", "cancel_all": "cancel"}
# How status and journal summaries count exposure in each mode: as R8-R10 do (amendment A3, C1).
STATUS_EXPOSURE_BASIS = {"paper": "worst_case_with_hedge_credit", "live": "worst_case_no_hedge_credit"}
RULE_ORDER = ("halted", "not_armed", "unresolved_orders", "validation", "stake_cap", "rate_minute", "rate_hour",
              "rate_day", "transactions_hour", "market_exposure", "total_exposure", "daily_loss", "funds",
              "unexplained_account_exposure")


# --- exposure --------------------------------------------------------------------------------


def _pnl_cp(selection, side, size_p, price_h, outcome):
    """P&L in hundredths of a penny of one bet portion if `outcome` wins."""
    if side == "BACK":
        return size_p * (price_h - 100) if outcome == selection else -size_p * 100
    return -size_p * (price_h - 100) if outcome == selection else size_p * 100


def _adverse_size(order):
    """Size that could still match (or whose fate is unknown), at the order's own limit price."""
    state = order["state"]
    if state in _UNKNOWN_SIZE_STATES or (order.get("provisional_child") is True and state not in TERMINAL_STATES):
        unaccounted = order["size_p"] - order["size_matched_p"] - order["size_cancelled_p"] - order["size_lapsed_p"]
        return max(0, order["size_remaining_p"], unaccounted)
    return 0


def _matched_price(order):
    """Worst plausible matched price: limit for a LAY if higher, limit for a BACK if lower, or limit if unknown."""
    average = order["average_price_matched_h"]
    if average is None:
        return order["price_h"]
    return max(average, order["price_h"]) if order["side"] == "LAY" else min(average, order["price_h"])


def exact_contrib(order, outcome):
    """Pence (floored) the matched part of an order makes or loses if `outcome` wins."""
    if order["size_matched_p"] <= 0 or order["state"] in ("SETTLED", "VOIDED"):
        return 0
    return _pnl_cp(order["selection_id"], order["side"], order["size_matched_p"], _matched_price(order), outcome) // 100


def adverse_contrib(order, outcome):
    """Pence (floored, <= 0) the unmatched or unknown part of an order can lose if `outcome` wins."""
    size = _adverse_size(order)
    if size <= 0:
        return 0
    return min(0, _pnl_cp(order["selection_id"], order["side"], size, order["price_h"], outcome)) // 100


def _adverse_cp(order, outcome):
    size = _adverse_size(order)
    if size <= 0:
        return 0
    return min(0, _pnl_cp(order["selection_id"], order["side"], size, order["price_h"], outcome))


def _exact_cp(order, outcome, credit):
    """The matched part's P&L if `outcome` wins; without credit only its loss (min(0, P&L))."""
    if order["size_matched_p"] <= 0 or order["state"] in ("SETTLED", "VOIDED"):
        return 0
    pnl = _pnl_cp(order["selection_id"], order["side"], order["size_matched_p"], _matched_price(order), outcome)
    return pnl if credit else min(0, pnl)


def _outcomes(orders, outcome_ids):
    outcomes = {OTHER}
    for value in outcome_ids or ():
        if value != OTHER:
            outcomes.add(int(value))
    for order in orders:
        outcomes.add(int(order["selection_id"]))
    return sorted(outcomes, key=str)


def _pairs(orders):
    """(parent, child) for each provisional replace child whose parent is in the same order set."""
    by_ref = {o["customer_order_ref"]: o for o in orders}
    pairs = []
    for child in orders:
        parent = by_ref.get(child.get("parent_ref"))
        if child.get("provisional_child") is True and parent is not None and child is not parent:
            pairs.append((parent, child))
    return pairs


def worst_case_market_loss(orders, outcome_ids, *, hedge_credit=False):
    """L_m = max(0, max over outcomes of -sum of contributions), in pence (rounded up), >= 0.

    hedge_credit False (the default) counts each matched portion's loss only; True also credits
    what a matched portion wins, so it can offset another order's loss in the same outcome."""
    orders = list(orders)
    paired = _pairs(orders)
    in_pair = {id(o) for pair in paired for o in pair}
    worst_cp = 0
    for outcome in _outcomes(orders, outcome_ids):
        total = 0
        for order in orders:
            total += _exact_cp(order, outcome, hedge_credit)
            if id(order) not in in_pair:
                total += _adverse_cp(order, outcome)
        for parent, child in paired:
            total += min(_adverse_cp(parent, outcome), _adverse_cp(child, outcome))
        worst_cp = max(worst_cp, -total)
    return -((-worst_cp) // 100)


def _pseudo_order(market_id, instruction, ref, *, parent_ref=None):
    """A not-yet-sent instruction as an order record counted fully adverse (state SUBMITTING);
    with parent_ref, the provisional replace child of that parent."""
    return {"customer_order_ref": ref, "market_id": market_id, "selection_id": instruction["selection_id"],
            "side": instruction["side"], "price_h": instruction["price_h"], "size_p": instruction["size_p"],
            "state": "SUBMITTING", "size_matched_p": 0, "average_price_matched_h": None,
            "size_remaining_p": instruction["size_p"], "size_cancelled_p": 0, "size_lapsed_p": 0,
            "provisional_child": True if parent_ref is not None else None, "parent_ref": parent_ref}


def exposure_by_market(orders, *, book_market_id=None, book_runner_ids=(), hedge_credit=False):
    """{market_id: L_m} over every market holding orders; the book's runners join its market's outcomes.
    hedge_credit as in worst_case_market_loss (default False: no credit)."""
    grouped = {}
    for order in orders:
        grouped.setdefault(order["market_id"], []).append(order)
    out = {}
    for market_id, market_orders in grouped.items():
        runners = book_runner_ids if market_id == book_market_id else ()
        out[market_id] = worst_case_market_loss(market_orders, runners, hedge_credit=hedge_credit)
    return out


# --- helpers ---------------------------------------------------------------------------------


def _gbp_floor_pence(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
        return int((Decimal(repr(value)) * 100).to_integral_value(rounding="ROUND_FLOOR"))
    except (InvalidOperation, OverflowError, ValueError):
        return None


def _gbp_ceil_pence(value):
    floor = _gbp_floor_pence(-value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return None if floor is None else -floor


def _liability_pence(side, size_p, price_h):
    if side == "BACK":
        return size_p
    return -((-size_p * (price_h - 100)) // 100)


def _usable(instruction):
    return (isinstance(instruction, dict) and set(instruction) == INSTRUCTION_V1_KEYS
            and type(instruction["selection_id"]) is int and instruction["selection_id"] > 0
            and instruction["side"] in ("BACK", "LAY") and is_on_ladder(instruction["price_h"])
            and type(instruction["size_p"]) is int and instruction["size_p"] > 0)


def _digest(value):
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError):
        return bytes_sha256(repr(value).encode("utf-8"))


def _failure(rule, reason, index=None):
    return {"rule": rule, "reason": reason, "instruction": index}


# --- the guard -------------------------------------------------------------------------------


class RiskGuard:
    """Pure order decisions for one mode. paths (optional) lets R1 read the kill file itself;
    otherwise callers pass kill_engaged, and without either R1 fails closed."""

    def __init__(self, config, *, mode, clock=SYSTEM_CLOCK, paths=None):
        if mode not in ("paper", "live"):
            raise ConfigError("risk_mode_invalid")
        self.mode = mode
        # R8-R10 credit a matched portion's winning payoff in paper only (amendment A3, C1); live counts
        # every matched portion at its loss alone, whatever its average price has become since.
        self.hedge_credit = mode == "paper"
        self.clock = clock
        self.paths = paths
        self.config = copy.deepcopy(config)
        orders = self.config["orders"]
        caps, rules = orders["caps"], orders["rules"]
        for key, path in CEILING_PATHS.items():
            value = self.config
            for part in path:
                value = value[part]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value <= ABSOLUTE_CEILINGS[key]:
                raise ConfigError(f"{key}_above_absolute_ceiling")
        self.caps_p = {key: money_pence(caps[key]) for key in ("max_stake_per_order", "max_liability_per_order",
                                                              "max_market_exposure", "max_total_exposure",
                                                              "max_daily_loss")}
        self.caps_p["funds_reserve"] = money_pence(caps["funds_reserve"], allow_zero=True)
        self.caps_p["external_exposure_tolerance"] = money_pence(rules["external_exposure_tolerance"], allow_zero=True)
        self.config_sha256 = canonical_sha256(self.config)
        self._approved = {}
        self._issued = set()

    # --- shared rules ------------------------------------------------------------------------

    def _r1(self, failures, state, kill_engaged):
        """R1. With paths the kill file is always read (fail-closed) and ORed with the caller's
        value, so a stale False cannot hide an engaged KILL file; any caller value other than
        None or False counts as engaged. With neither source, the kill state is unknown: refused."""
        from_file = _kill_file_engaged(self.paths) if self.paths is not None else None
        if (kill_engaged is not None and kill_engaged is not False) or from_file is True:
            kill_engaged = True
        elif kill_engaged is None and from_file is None:
            kill_engaged = None
        else:
            kill_engaged = False
        if kill_engaged is None:
            failures.append(_failure("halted", "kill_state_unknown"))
        elif kill_engaged:
            failures.append(_failure("halted", "kill_engaged"))
        if state["halted"]:
            failures.append(_failure("halted", "journal_halted"))
        if state["clock_regression"]:
            failures.append(_failure("halted", "clock_regression"))
        return kill_engaged

    def _r2(self, failures, arming):
        if self.mode != "live":
            return
        if not isinstance(arming, dict) or arming.get("armed") is not True or arming.get("failures"):
            failures.append(_failure("not_armed", "arming_failed"))

    def _r3(self, failures, state, market_id):
        if market_id in state["unresolved_markets"]:
            failures.append(_failure("unresolved_orders", "market_unresolved"))
        elif self.mode == "live" and state["unresolved_markets"]:
            failures.append(_failure("unresolved_orders", "unresolved_orders_anywhere_live"))

    def _r5(self, failures, new_orders):
        caps = self.caps_p
        for index, order in new_orders:
            if order["size_p"] > caps["max_stake_per_order"]:
                failures.append(_failure("stake_cap", "stake_above_max_stake_per_order", index))
            liability_cp = order["size_p"] * (order["price_h"] - 100)
            if order["side"] == "LAY" and liability_cp > caps["max_liability_per_order"] * 100:
                failures.append(_failure("stake_cap", "liability_above_max_liability_per_order", index))

    def _r6_r7(self, failures, state, now_ns, placements, transactions_added=None):
        """R6 counts `placements`; R7 adds transactions_added (default the placements): a replace
        reserves its worst case, 1 plus 1 more if its cancel half fails."""
        caps = self.config["orders"]["caps"]
        if transactions_added is None:
            transactions_added = placements
        times = state["submitted_instruction_wall_ns"]
        # Windows have no upper bound: a clock stepped back cannot reopen them.
        minute = sum(1 for t in times if t > now_ns - 60 * _NS_PER_S)
        hour = sum(1 for t in times if t > now_ns - 3600 * _NS_PER_S)
        day_start = now_ns - now_ns % DAY_NS
        day = sum(1 for t in times if t >= day_start)
        floor = state["exchange_floor"] or {}
        day = max(day, state["orders_today"] or 0, floor.get("orders_today") or 0)
        if minute + placements > caps["max_orders_per_minute"]:
            failures.append(_failure("rate_minute", "orders_per_minute_cap"))
        if hour + placements > caps["max_orders_per_hour"]:
            failures.append(_failure("rate_hour", "orders_per_hour_cap"))
        if day + placements > caps["max_orders_per_day"]:
            failures.append(_failure("rate_day", "orders_per_day_cap"))
        key = utc_from_ns(now_ns)[:13]
        transactions = sum(n for hour_key, n in state["transactions_by_clock_hour"].items() if hour_key >= key)
        if transactions + transactions_added > caps["max_transactions_per_hour"]:
            failures.append(_failure("transactions_hour", "transactions_per_hour_cap"))
        return {"minute": minute, "hour": hour, "day": day, "transactions_hour": transactions}

    def _realized_loss(self, state):
        """R10 realised loss today: the worse of the journal and exchange values (profits never extend).

        An unknown value (None) counts as -(worst-case loss of today's settled orders), so the
        worse of the known values and that figure is used; the matched liability of today's
        unsettled orders is then added on top as well (it is also inside the exposure sum, so
        this double counts on purpose rather than undercount). Both figures come from the fold
        (settled_matched_liability_today_pence, unsettled_matched_liability_today_pence); when
        either is missing or invalid the whole daily cap counts as used, so R10 refuses (amendment
        A3, A26: fail closed instead of recomputing).
        """
        values = [state["realized_today_pence"]]
        if state["exchange_floor"] is not None:
            values.append(state["exchange_floor"].get("realized_today_pence"))
        known = [v for v in values if type(v) is int]
        if len(known) == len(values):
            return max(0, -min(known))
        settled = state.get("settled_matched_liability_today_pence")
        unsettled = state.get("unsettled_matched_liability_today_pence")
        if type(settled) is not int or settled < 0 or type(unsettled) is not int or unsettled < 0:
            return self.caps_p["max_daily_loss"]
        return max(0, -min(known + [-settled])) + unsettled

    def _exposure(self, state, market_id, book, *, add=(), replace_refs=(), hedge_credit=None):
        """{"market", "total"} L_m with `add` counted adversarially; orders in replace_refs are
        counted as REPLACE_PENDING parents (paired with their provisional children in `add`).
        hedge_credit None means this guard's own (self.hedge_credit: paper only)."""
        credit = self.hedge_credit if hedge_credit is None else hedge_credit
        orders = []
        for order in state["orders"].values():
            if order["customer_order_ref"] in replace_refs:
                order = dict(order, state="REPLACE_PENDING")
            orders.append(order)
        orders.extend(add)
        runners = ()
        if isinstance(book, dict) and isinstance(book.get("runners"), dict):
            runners = [int(k) for k in book["runners"] if isinstance(k, str) and k.isdigit() and k.isascii()]
        by_market = exposure_by_market(orders, book_market_id=market_id, book_runner_ids=runners, hedge_credit=credit)
        return {"market": by_market.get(market_id, 0), "total": sum(by_market.values())}

    def _exposure_rules(self, failures, state, market_id, book, scenarios):
        """R8-R10 on the worst of the given scenarios (lists of (add, replace_refs))."""
        results = [self._exposure(state, market_id, book, add=add, replace_refs=refs) for add, refs in scenarios]
        market = max(r["market"] for r in results)
        total = max(r["total"] for r in results)
        realized_loss = self._realized_loss(state)
        caps = self.caps_p
        if market > caps["max_market_exposure"]:
            failures.append(_failure("market_exposure", "worst_case_market_loss_above_cap"))
        if total > caps["max_total_exposure"]:
            failures.append(_failure("total_exposure", "worst_case_total_loss_above_cap"))
        if realized_loss + total > caps["max_daily_loss"]:
            failures.append(_failure("daily_loss", "daily_loss_budget_exceeded"))
        return {"market_pence": market, "total_pence": total, "daily_used_pence": realized_loss + total}

    def _funds_rules(self, failures, state, funds, now_ns, new_orders, before_total, funds_checked_at=None,
                     floor_ns=None):
        """R11 and R12 (live). Returns detail fields; the R12 result is always reported.

        Funds are never assumed fresh: funds passed in must say when they were fetched
        (funds_checked_at, or a "checked_at" key), and otherwise the journal's latest funds_check
        (state funds + funds_checked_at) is used. Either way the age must be within funds_max_age_s at
        now_ns, and not negative at now_ns or at floor_ns (a pilot decision's raw reading, when now_ns is
        its later retained time).
        """
        detail = {"required_stake_pence": None, "available_pence": None, "external_exposure_pence": None,
                  "halt": False, "halt_reason": None}
        if self.mode != "live":
            return detail
        caps = self.config["orders"]["caps"]
        if funds is not None:
            document = funds
            checked = funds_checked_at
            if checked is None and isinstance(funds, dict):
                checked = funds.get("checked_at")
        else:
            document, checked = state["funds"], state["funds_checked_at"]
        if not isinstance(document, dict):
            failures.append(_failure("funds", "funds_unknown_refresh_required"))
            return detail
        if checked is None:
            failures.append(_failure("funds", "funds_age_unknown_refresh_required"))
        else:
            try:
                checked_ns = parse_utc_ns(checked)
                age_ok = (0 <= now_ns - checked_ns <= int(caps["funds_max_age_s"] * _NS_PER_S)
                          and (floor_ns is None or floor_ns >= checked_ns))
            except (TypeError, ValueError):
                age_ok = False
            if not age_ok:
                failures.append(_failure("funds", "funds_stale_refresh_required"))
        available = _gbp_floor_pence(document.get("availableToBetBalance"))
        required = sum(_liability_pence(o["side"], o["size_p"], o["price_h"]) for _, o in new_orders)
        detail["required_stake_pence"], detail["available_pence"] = required, available
        if available is None:
            failures.append(_failure("funds", "available_to_bet_unknown"))
        elif required > available - self.caps_p["funds_reserve"]:
            failures.append(_failure("funds", "insufficient_available_to_bet"))
        exposure = document.get("exposure")
        exposure_p = _gbp_ceil_pence(abs(exposure)) if isinstance(exposure, (int, float)) and not isinstance(
            exposure, bool) else None
        if exposure_p is None:
            failures.append(_failure("funds", "account_exposure_unknown"))
            return detail
        external = exposure_p - before_total     # the sign of exposure is unverified (T7), hence abs()
        detail["external_exposure_pence"] = external
        if external > self.caps_p["external_exposure_tolerance"]:
            failures.append(_failure("unexplained_account_exposure", "account_exposure_beyond_own_orders"))
            detail["halt"], detail["halt_reason"] = True, "unexplained_account_exposure"
        return detail

    def _decide(self, action, market_id, failures, *, inputs, worst_case, now_ns, detail, instructions,
                market_version=None, customer_ref=None, customer_order_refs=None, probe=False, extra=None):
        order = {name: rank for rank, name in enumerate(RULE_ORDER)}
        failures = sorted(failures, key=lambda f: order[f["rule"].split(":", 1)[0]])
        decision = {
            "decision_id": "dec-" + _digest(dict(inputs, action=action, mode=self.mode,
                                                  config_sha256=self.config_sha256))[:24],
            "approved": not failures, "rule": failures[0]["rule"] if failures else None,
            "detail": dict(detail, failures=failures), "worst_case": worst_case,
            "evaluated_at": utc_from_ns(now_ns), "action": action, "market_id": market_id, "mode": self.mode,
            "instructions": instructions, "market_version": market_version, "customer_ref": customer_ref,
            "customer_order_refs": customer_order_refs, "probe": probe,
            "evaluated_monotonic_ns": inputs.get("now_monotonic_ns")}
        decision.update(extra or {})
        decision = copy.deepcopy(decision)
        if decision["approved"]:
            self._approved[decision["decision_id"]] = canonical_sha256(decision)
        return decision

    # --- public decisions --------------------------------------------------------------------

    def _refs_required(self, failures, customer_ref, customer_order_refs, *, action):
        """Live V11: the decision must bind the refs journaled with the intent (sections 9.3, 9.7)."""
        if self.mode != "live":
            return
        if customer_ref is None:
            failures.append(_failure("validation:V11", "customer_ref_required_live"))
        if action == "place" and customer_order_refs is None:
            failures.append(_failure("validation:V11", "customer_order_refs_required_live"))

    def evaluate_place(self, market_id, instructions, *, book, state, arming, funds=None, now_ns, probe=False,
                       now_monotonic_ns=None, kill_engaged=None, customer_ref=None, customer_order_refs=None,
                       funds_checked_at=None, source=None, pilot_binding=None, raw_now_ns=None):
        """R1-R12 and V0-V14/V9b for one place request -> the decision.

        source and pilot_binding (P1-06): under a pilot-enabled config (orders.pilot) the decision binds the
        customerStrategyRef its source carries (config.order_strategy_ref: the pilot's for source "pilot", the
        fixed manual ref otherwise) into its decision_id and its body check (_params_match). pilot_binding,
        for source "pilot" only, is {"record_sha256", "contract_sha256"}: the PLACE record's digest and the
        contract's, bound into decision_id and carried as the decision's pilot_record_sha256 (note §4,
        "Binding"); a malformed one is a programming error (ValueError). Without either, decisions and their
        decision_id are exactly as before.

        raw_now_ns (an RS-19 pilot decision, P106-R3-1 as corrected in round 4): now_ns is then the open's retained
        time and raw_now_ns the raw wall reading taken for this decision. Staleness (V9b, V10's wall age, R11's
        funds age) is judged at now_ns, an upper bound; the R6/R7 windows at raw_now_ns, so a transient forward
        reading cannot move a window's start; and a book or funds read after either reading is a negative age
        (V10 book_age_negative, R11). Without it, everything is judged at now_ns, as before."""
        if type(now_ns) is not int:
            raise ValueError("now_ns must be an integer")
        if raw_now_ns is not None and type(raw_now_ns) is not int:
            raise ValueError("raw_now_ns must be an integer")
        window_ns = now_ns if raw_now_ns is None else raw_now_ns
        if (source == "pilot") != (pilot_binding is not None):
            raise ValueError("pilot_binding is required for source pilot and only for it")
        if pilot_binding is not None and (
                not isinstance(pilot_binding, dict) or set(pilot_binding) != {"record_sha256", "contract_sha256"}
                or not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in pilot_binding.values())):
            raise ValueError("pilot_binding must be {record_sha256, contract_sha256} as sha256 hex")
        inputs = {"market_id": market_id, "instructions": instructions, "book": book, "state": state,
                  "arming": arming, "funds": funds, "now_ns": now_ns, "probe": probe,
                  "now_monotonic_ns": now_monotonic_ns, "kill_engaged": kill_engaged, "customer_ref": customer_ref,
                  "customer_order_refs": customer_order_refs, "funds_checked_at": funds_checked_at}
        if raw_now_ns is not None:
            inputs["raw_now_ns"] = raw_now_ns
        extra = {}
        if pilot_enabled(self.config):
            extra["customer_strategy_ref"] = inputs["customer_strategy_ref"] = order_strategy_ref(self.config, source)
        if pilot_binding is not None:
            inputs["pilot_binding"] = dict(pilot_binding)
            extra["pilot_record_sha256"] = pilot_binding["record_sha256"]
        if now_monotonic_ns is None and self.mode == "live":
            now_monotonic_ns = self.clock.monotonic_ns()
            inputs["now_monotonic_ns"] = now_monotonic_ns
        failures = []
        inputs["kill_engaged"] = self._r1(failures, state, kill_engaged)
        self._r2(failures, arming)
        self._r3(failures, state, market_id)
        for item in validate_place(market_id, instructions, book=book, open_orders=list(state["orders"].values()),
                                   config=self.config, mode=self.mode, now_ns=now_ns, probe=probe,
                                   now_monotonic_ns=now_monotonic_ns, customer_ref=customer_ref,
                                   customer_order_refs=customer_order_refs, floor_ns=raw_now_ns):
            failures.append(_failure("validation:" + item["rule"], item["reason"], item["instruction"]))
        self._refs_required(failures, customer_ref, customer_order_refs, action="place")
        listed = instructions if isinstance(instructions, list) else []
        if probe and (len(listed) != 1 or not _usable(listed[0]) or listed[0]["side"] != "LAY"
                      or listed[0]["price_h"] != PROBE_PRICE_H or listed[0]["size_p"] != PROBE_SIZE_P):
            failures.append(_failure("validation:V0", "probe_must_be_lay_1_00_at_1_01"))
        new = [(i, instruction) for i, instruction in enumerate(listed) if _usable(instruction)]
        new_orders = [(i, _pseudo_order(market_id, ins, f"new-{i}")) for i, ins in new]
        self._r5(failures, new_orders)
        rates = self._r6_r7(failures, state, window_ns, len(listed))
        # R12's explained exposure is the credited (smaller) figure, so the unexplained remainder it
        # leaves is at least as large as without credit: R12 is never loosened by C1 (amendment A3, A11).
        before = self._exposure(state, market_id, book, hedge_credit=True)
        worst = self._exposure_rules(failures, state, market_id, book, [([o for _, o in new_orders], ())])
        detail = self._funds_rules(failures, state, funds, now_ns, new_orders, before["total"],
                                   funds_checked_at=funds_checked_at, floor_ns=raw_now_ns)
        detail["rates"] = rates
        version = book.get("version") if isinstance(book, dict) else None
        return self._decide("place", market_id, failures, inputs=inputs, worst_case=worst, now_ns=now_ns,
                            detail=detail, instructions=copy.deepcopy([ins for _, ins in new]),
                            market_version=version, customer_ref=customer_ref,
                            customer_order_refs=list(customer_order_refs) if customer_order_refs is not None else None,
                            probe=bool(probe), extra=extra)

    def evaluate_pilot_reservation(self, market_id, *, state, now_ns, size_p, kill_engaged=None, raw_now_ns=None):
        """P1-06 critique 1: the rules the journal already predicts, checked before a pilot reservation takes a
        market (and, through its PLACE record, a lifetime slot): R1 (halted, KILL, clock regression), R3 (an
        unresolved order anywhere), R6 (the minute, hour and day windows), R8 and R9 (market and total worst case)
        and R10 (the daily loss budget), for one more size_p BACK counted at its full worst case. The guard's own
        rule functions; [] or the failures in RULE_ORDER. evaluate_place still runs every rule at the decision.
        raw_now_ns: the raw wall reading the R6 windows are judged at, when now_ns is the open's retained time
        (as evaluate_place)."""
        if type(now_ns) is not int or type(size_p) is not int or size_p <= 0:
            raise ValueError("now_ns and size_p must be integers")
        if raw_now_ns is not None and type(raw_now_ns) is not int:
            raise ValueError("raw_now_ns must be an integer")
        failures = []
        self._r1(failures, state, kill_engaged)
        self._r3(failures, state, market_id)
        self._r6_r7(failures, state, now_ns if raw_now_ns is None else raw_now_ns, 1)
        # A BACK loses its whole stake in every outcome but its own, whatever its price.
        back = {"selection_id": 1, "handicap": 0, "side": "BACK", "price_h": 101, "size_p": size_p,
                "persistence_type": "LAPSE"}
        self._exposure_rules(failures, state, market_id, None, [([_pseudo_order(market_id, back, "pilot-new")], ())])
        order = {name: rank for rank, name in enumerate(RULE_ORDER)}
        kept = [f for f in failures if f["rule"] != "transactions_hour"]
        return sorted(kept, key=lambda f: order[f["rule"]])

    def evaluate_replace(self, market_id, replaces, *, book, state, arming, funds=None, now_ns,
                         now_monotonic_ns=None, kill_engaged=None, customer_ref=None, funds_checked_at=None):
        """Paper only: live replace was removed in v1 (amendment A3, D2), so a live guard refuses at once."""
        if self.mode == "live":
            raise OrderRefused("live_replace_removed_v1")
        if type(now_ns) is not int:
            raise ValueError("now_ns must be an integer")
        inputs = {"market_id": market_id, "replaces": replaces, "book": book, "state": state, "arming": arming,
                  "funds": funds, "now_ns": now_ns, "now_monotonic_ns": now_monotonic_ns,
                  "kill_engaged": kill_engaged, "customer_ref": customer_ref, "funds_checked_at": funds_checked_at}
        failures = []
        inputs["kill_engaged"] = self._r1(failures, state, kill_engaged)
        self._r2(failures, arming)
        self._r3(failures, state, market_id)
        open_orders = list(state["orders"].values())
        validation = validate_replace(market_id, replaces, book=book, open_orders=open_orders, config=self.config,
                                      mode=self.mode, now_ns=now_ns, now_monotonic_ns=now_monotonic_ns,
                                      customer_ref=customer_ref)
        self._refs_required(failures, customer_ref, None, action="replace")
        by_bet = {o["bet_id"]: o for o in open_orders if o["bet_id"] is not None and o["market_id"] == market_id}
        listed = replaces if isinstance(replaces, list) else []
        new_orders, parents, normalised = [], [], []
        for index, item in enumerate(listed):
            bet_id = item.get("bet_id") if isinstance(item, dict) else None
            parent = by_bet.get(bet_id) if isinstance(bet_id, str) else None
            if parent is None or parent["state"] != "EXECUTABLE" or not is_on_ladder(item.get("new_price_h")):
                continue
            instruction = {"selection_id": parent["selection_id"], "handicap": 0, "side": parent["side"],
                           "price_h": item["new_price_h"], "size_p": parent["size_remaining_p"],
                           "persistence_type": "LAPSE"}
            if instruction["size_p"] <= 0:
                continue
            new_orders.append((index, _pseudo_order(market_id, instruction, f"new-{index}",
                                                    parent_ref=parent["customer_order_ref"])))
            parents.append(parent["customer_order_ref"])
            normalised.append({"bet_id": item["bet_id"], "new_price_h": item["new_price_h"]})
        for item in validation:
            failures.append(_failure("validation:" + item["rule"], item["reason"], item["instruction"]))
        self._r5(failures, new_orders)
        rates = self._r6_r7(failures, state, now_ns, len(listed), transactions_added=2 * len(listed))
        before = self._exposure(state, market_id, book)
        # The state right after the submit: each parent REPLACE_PENDING with its provisional child,
        # counted per outcome as the worse of the two. That bounds every mix of per-instruction
        # outcomes (each cancel-then-place succeeds or fails on its own), so it is at least
        # max(L(a) all cancels fail, L(b) all succeed) of spec section 10.
        worst = self._exposure_rules(failures, state, market_id, book,
                                     [([o for _, o in new_orders], set(parents))])
        detail = self._funds_rules(failures, state, funds, now_ns, new_orders, before["total"],
                                   funds_checked_at=funds_checked_at)
        detail["rates"] = rates
        version = book.get("version") if isinstance(book, dict) else None
        return self._decide("replace", market_id, failures, inputs=inputs, worst_case=worst, now_ns=now_ns,
                            detail=detail, instructions=normalised, market_version=version,
                            customer_ref=customer_ref)

    def evaluate_cancel(self, market_id, cancels, *, state, now_ns, account_wide=False, customer_ref=None,
                        kill_targets=None):
        """Cancels are allowed while halted or killed; only validate_cancel applies.

        Any bet id of ours whose order is still open or unresolved may be cancelled (a cancel only
        reduces exposure). kill_targets is the spec section 9.8 kill sweep's evidence for bets found on
        the exchange: [{"bet_id", "market_id", "leg": "strategy_ref"|"bet_id"}];
        it is accepted only once the journal is halted (the kill appends kill and halt first).
        account_wide=True is the kill switch's cancel of every unmatched bet on the account
        (market_id None, no instructions); it is never the default. In live mode issue() mints a
        cancel capability only if customer_ref was bound here.
        """
        if type(now_ns) is not int:
            raise ValueError("now_ns must be an integer")
        inputs = {"market_id": market_id, "cancels": cancels, "state": state, "now_ns": now_ns,
                  "account_wide": account_wide, "customer_ref": customer_ref, "kill_targets": kill_targets}
        failures, normalised = [], []
        if customer_ref is not None and (not isinstance(customer_ref, str)
                                         or not re.fullmatch(CUSTOMER_REF_PATTERN, customer_ref)):
            failures.append(_failure("validation:V11", "customer_ref_invalid"))
        if account_wide:
            if market_id is not None or cancels:
                failures.append(_failure("validation:V0", "account_wide_cancel_takes_no_market_or_bets"))
            action = "cancel_all"
        else:
            action = "cancel"
            if kill_targets is not None and state["halted"] is not True:
                failures.append(_failure("validation:V0", "kill_targets_need_journal_halt"))
            for item in validate_cancel(market_id, cancels, open_orders=list(state["orders"].values()),
                                        kill_targets=kill_targets):
                failures.append(_failure("validation:" + item["rule"], item["reason"], item["instruction"]))
            for item in cancels if isinstance(cancels, list) else []:
                if isinstance(item, str):
                    item = {"bet_id": item, "size_reduction_p": None}
                if isinstance(item, dict) and set(item) == {"bet_id", "size_reduction_p"}:
                    normalised.append({"bet_id": item["bet_id"], "size_reduction_p": item["size_reduction_p"]})
        before = self._exposure(state, market_id, None)
        worst = {"market_pence": before["market"], "total_pence": before["total"],
                 "daily_used_pence": self._realized_loss(state) + before["total"]}
        return self._decide(action, market_id, failures, inputs=inputs, worst_case=worst, now_ns=now_ns,
                            detail={"kill_sweep": kill_targets is not None}, instructions=normalised,
                            customer_ref=customer_ref)

    # --- capability ---------------------------------------------------------------------------

    def issue(self, decision, prepared, *, now_monotonic_ns):
        """Mint the single-use capability for an approved live decision and its exact request body."""
        if self.mode != "live":
            raise OrderRefused("capability_mode")
        if not isinstance(decision, dict) or decision.get("approved") is not True:
            raise OrderRefused("capability_unapproved")
        decision_id = decision.get("decision_id")
        try:
            known = self._approved.get(decision_id) == canonical_sha256(decision)
        except (TypeError, ValueError):
            known = False
        if not known:
            raise OrderRefused("capability_decision_unknown")
        if decision_id in self._issued:
            raise OrderRefused("capability_decision_reused")
        self._issued.add(decision_id)
        if decision["action"] == "place":
            # The decision is only as fresh as the book it checked: mint within max_book_age_s of it.
            evaluated = decision["evaluated_monotonic_ns"]
            limit = int(self.config["orders"]["caps"]["max_book_age_s"] * _NS_PER_S)
            if (type(evaluated) is not int or type(now_monotonic_ns) is not int
                    or not 0 <= now_monotonic_ns - evaluated <= limit):
                raise OrderRefused("capability_decision_stale")
        # The body must carry the refs journaled with the intent: a decision without them binds nothing.
        if decision.get("customer_ref") is None or (decision["action"] == "place"
                                                    and decision.get("customer_order_refs") is None):
            raise OrderRefused("capability_refs_unbound")
        if (not isinstance(prepared, dict) or not isinstance(prepared.get("body"), bytes)
                or not isinstance(prepared.get("method"), str)
                or prepared.get("body_sha256") != bytes_sha256(prepared["body"])):
            raise OrderRefused("capability_body_mismatch")
        try:
            envelope = strict_json_loads(prepared["body"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OrderRefused("capability_body_mismatch") from None
        if (not isinstance(envelope, dict) or set(envelope) != {"jsonrpc", "method", "params", "id"}
                or envelope["jsonrpc"] != "2.0" or envelope["method"] != prepared["method"]
                or _method_verb(prepared["method"]) != ACTION_VERBS.get(decision.get("action"))
                or not _params_match(decision, envelope["params"], self.config)):
            raise OrderRefused("capability_body_mismatch")
        return _mint(decision_id, prepared["method"], prepared["body_sha256"], self.mode, now_monotonic_ns)


# --- body binding ----------------------------------------------------------------------------


def _method_verb(method):
    """The verb of '<betting prefix><verb>Orders' ('place', 'cancel', or any other word, which ACTION_VERBS
    never names); None for anything else."""
    if not method.startswith(BETTING_METHOD_PREFIX) or not method.endswith("Orders"):
        return None
    return method[len(BETTING_METHOD_PREFIX):-len("Orders")] or None


def _price_h(value):
    try:
        return to_hundredths(value)
    except ValueError:
        return None


def _size_p(value):
    try:
        return to_pence(value)
    except ValueError:
        return None


def _ref_ok(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _params_match(decision, params, config):
    """True only if the JSON-RPC params carry exactly what the decision approved."""
    if not isinstance(params, dict):
        return False
    action, instructions = decision["action"], decision["instructions"]
    if not _ref_ok(params.get("customerRef"), CUSTOMER_REF_PATTERN):
        return False
    if decision["customer_ref"] is not None and params["customerRef"] != decision["customer_ref"]:
        return False
    if action == "cancel_all":
        return set(params) == {"customerRef"}
    if params.get("marketId") != decision["market_id"] or not isinstance(params.get("instructions"), list):
        return False
    sent = params["instructions"]
    if len(sent) != len(instructions):
        return False
    if action == "place":
        version = decision["market_version"]
        if type(version) is not int or params.get("marketVersion") != {"version": version}:
            return False
    if action == "place":
        if set(params) != {"marketId", "customerRef", "customerStrategyRef", "marketVersion", "instructions"}:
            return False
        # The ref the decision bound for its source (P1-06 critique 13), else the config's as before.
        if params["customerStrategyRef"] != decision.get("customer_strategy_ref",
                                                          config["orders"]["customer_strategy_ref"]):
            return False
        refs = decision["customer_order_refs"]
        for index, (wire, approved) in enumerate(zip(sent, instructions)):
            if not isinstance(wire, dict) or set(wire) != {"orderType", "selectionId", "handicap", "side",
                                                           "customerOrderRef", "limitOrder"}:
                return False
            limit = wire["limitOrder"]
            if (wire["orderType"] != "LIMIT" or type(wire["selectionId"]) is not int
                    or wire["selectionId"] != approved["selection_id"] or wire["handicap"] not in (0, 0.0)
                    or isinstance(wire["handicap"], bool) or wire["side"] != approved["side"]
                    or not _ref_ok(wire["customerOrderRef"], CUSTOMER_ORDER_REF_PATTERN)
                    or (refs is not None and wire["customerOrderRef"] != refs[index])
                    or not isinstance(limit, dict) or set(limit) != {"size", "price", "persistenceType"}
                    or limit["persistenceType"] != "LAPSE" or _size_p(limit["size"]) != approved["size_p"]
                    or _price_h(limit["price"]) != approved["price_h"]):
                return False
        return len({w["customerOrderRef"] for w in sent}) == len(sent)
    if action == "cancel":
        if set(params) != {"marketId", "customerRef", "instructions"}:
            return False
        for wire, approved in zip(sent, instructions):
            reduction = approved["size_reduction_p"]
            if (not isinstance(wire, dict) or set(wire) != {"betId", "sizeReduction"}
                    or wire["betId"] != approved["bet_id"]):
                return False
            if reduction is None and wire["sizeReduction"] is not None:
                return False
            if reduction is not None and _size_p(wire["sizeReduction"]) != reduction:
                return False
        return True
    return False
