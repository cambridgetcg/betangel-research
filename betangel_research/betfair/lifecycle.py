"""Order lifecycle: the state machine, transitions, and the rules that turn reports into states.

The safety rule throughout: an outcome that is not definitely known is UNKNOWN, never zero.
A report instruction with matched size is never FAILED; a FAILURE that echoes a betId is
UNKNOWN until a read confirms it; any reference or count mismatch between what was sent and
what came back makes every order in the request UNKNOWN. Only codes in a definitive category
(terminal_*, market_state, planner_bug) can make an order FAILED. Exceptions map the same way:
a transport error after any byte was written is UNKNOWN. Nothing here resends anything.
"""

import copy
import re

from .errors import ApiError, JournalError, OrderRefused, TransportError, classify
from .schemas import (ORDER_STATES, TERMINAL_STATES as _TERMINAL, UNRESOLVED_STATES as _UNRESOLVED,
                      validate_cancel_report, validate_order_record, validate_place_report,
                      validate_replace_report)

STATES = ("RECORDED", "REFUSED", "SUBMITTING", "EXECUTABLE", "EXECUTION_COMPLETE", "FAILED", "UNKNOWN",
          "ABSENT_UNCONFIRMED", "ABSENT_ACCEPTED", "LAPSED", "CANCEL_PENDING", "REPLACE_PENDING", "REPLACED",
          "SETTLED", "VOIDED")
TERMINAL_STATES = _TERMINAL
UNRESOLVED_STATES = _UNRESOLVED
if frozenset(STATES) != ORDER_STATES:
    raise RuntimeError("lifecycle states differ from schemas.ORDER_STATES")

# Spec section 9.4, with four documented additions:
# - SUBMITTING -> REFUSED: the kill file was found engaged after the submit event was journaled
#   and before any byte was sent (spec section 9.3 step 10, REFUSED(kill_engaged_after_submit_journal)).
# - REPLACED -> SETTLED | VOIDED: the matched part of a replaced bet keeps its betId and settles.
# - ABSENT_UNCONFIRMED -> UNKNOWN: the bet was seen on the exchange again (a kill-sweep cancel or
#   a read found it) but its sizes are not yet known; it stays unresolved at full adverse exposure.
# - EXECUTABLE -> SETTLED (amendment A3, D3): only live settle-market writes it, after the operator
#   checked the website and the exchange shows the market CLOSED with none of our bets current. The
#   remaining size is kept as recorded (whether it matched is unknown) and booked at its worst case
#   (journal.settled_worst_loss_pence). Paper never writes it.
# The shared table keeps paper's edges; the live writers of replace, refresh, close-from-cleared and
# automatic settlement were removed in v1, so those edges are unreachable in live mode.
TRANSITIONS = {
    "RECORDED": frozenset({"REFUSED", "SUBMITTING"}),
    "SUBMITTING": frozenset({"EXECUTABLE", "EXECUTION_COMPLETE", "FAILED", "UNKNOWN", "REFUSED"}),
    "UNKNOWN": frozenset({"EXECUTABLE", "EXECUTION_COMPLETE", "LAPSED", "ABSENT_UNCONFIRMED", "UNKNOWN"}),
    "ABSENT_UNCONFIRMED": frozenset({"EXECUTABLE", "EXECUTION_COMPLETE", "LAPSED", "ABSENT_ACCEPTED", "UNKNOWN"}),
    "EXECUTABLE": frozenset({"EXECUTABLE", "EXECUTION_COMPLETE", "LAPSED", "CANCEL_PENDING", "REPLACE_PENDING",
                             "SETTLED"}),
    "CANCEL_PENDING": frozenset({"EXECUTABLE", "EXECUTION_COMPLETE", "UNKNOWN"}),
    "REPLACE_PENDING": frozenset({"REPLACED", "EXECUTION_COMPLETE", "EXECUTABLE", "UNKNOWN"}),
    "EXECUTION_COMPLETE": frozenset({"SETTLED", "VOIDED"}),
    "REPLACED": frozenset({"SETTLED", "VOIDED"}),
    "REFUSED": frozenset(),
    "FAILED": frozenset(),
    "ABSENT_ACCEPTED": frozenset(),
    "LAPSED": frozenset(),
    "SETTLED": frozenset(),
    "VOIDED": frozenset(),
}
if set(TRANSITIONS) != set(STATES):
    raise RuntimeError("TRANSITIONS must name every state")

# A provisional replace child stays provisional while its own outcome is unknown.
_PROVISIONAL_STATES = frozenset({"RECORDED", "SUBMITTING", "UNKNOWN", "ABSENT_UNCONFIRMED"})
# States after which no size is left open: every penny of size_p must then be accounted for as
# matched, cancelled or lapsed. Size never silently vanishes (spec section 0.3 item 6).
SIZED_FINAL_STATES = frozenset({"EXECUTION_COMPLETE", "LAPSED", "REPLACED"})
DEFINITIVE_CATEGORIES = frozenset({"terminal_account", "terminal_input", "market_state", "planner_bug"})
REJECTED_EXCEPTION_CATEGORIES = frozenset({"session", "app_key", "rate_limited", "planner_bug",
                                           "terminal_account", "terminal_input"})
# Spec section 9.8 automatic HALT triggers keyed by error category (the gateway journals the halt).
HALT_CATEGORIES = {"terminal_account": "terminal_account", "terminal_input": "validator_gap",
                   "planner_bug": "validator_gap", "app_key": "app_key", "unrecognized": "unrecognized"}
# Paper model codes. Only a report whose source is "paper-v1" may carry them.
PAPER_REFUSAL_CODES = frozenset({"paper_book_stale", "paper_book_invalid", "paper_book_after_activation",
                                 "paper_book_in_recording_gap"})
PAPER_FAILED_CODES = frozenset({"paper_rejected_market_state", "paper_unknown_bet_id"})
OUTCOMES = ("UNKNOWN", "FAILED_NOT_SENT", "FAILED_REJECTED")

# Fields a transition may set. Identity fields (refs, market, selection, side, price, size, mode)
# never change after the intent.
MUTABLE_FIELDS = frozenset({
    "bet_id", "size_matched_p", "average_price_matched_h", "size_remaining_p", "size_cancelled_p",
    "size_lapsed_p", "size_voided_p", "submitted_at", "submitted_monotonic_ns", "body_sha256",
    "outcome_observed_at", "successful_polls", "last_report_at", "child_ref", "provisional_child",
    "error_code", "reconcile_attempts", "settled_profit_own_p", "settled_profit_betfair_p"})
_REASON_RE = re.compile(r"[A-Za-z0-9_\-.:]{1,80}")


def _remaining_unknown(order):
    return max(0, order["size_p"] - order["size_matched_p"] - order["size_cancelled_p"] - order["size_lapsed_p"])


def exposure_basis(order):
    """How exposure is counted for an order record (spec section 3 exposure_basis)."""
    state = order["state"]
    if state in UNRESOLVED_STATES or order.get("provisional_child") is True:
        return "adverse_unknown"
    if state not in TERMINAL_STATES and state != "RECORDED" and order["size_remaining_p"] > 0:
        return "adverse_unmatched"
    if order["size_matched_p"] > 0 and state not in ("SETTLED", "VOIDED"):
        return "matched_exact"
    return "none"


def transition(order, new_state, *, reason, fields=None):
    """A new order record in new_state with fields applied; an illegal transition raises JournalError.

    Defaults keep sizes consistent when a report does not state them: SUBMITTING, UNKNOWN,
    ABSENT_UNCONFIRMED and EXECUTABLE keep everything not known to be matched, cancelled or lapsed
    as remaining (adverse). Final states leave nothing remaining, and EXECUTION_COMPLETE, LAPSED and
    REPLACED must account for the whole size as matched + cancelled + lapsed: a move that would
    let unaccounted size vanish raises JournalError("final_state_size_unaccounted"). The only
    default that accounts for size is EXECUTABLE -> LAPSED (the known remainder lapsed); a move out
    of an unresolved state must state its sizes. LAPSED never carries matched size (that is
    EXECUTION_COMPLETE with size_lapsed_p), since LAPSED cannot settle. exposure_basis is always
    recomputed, never taken from fields.
    """
    if new_state not in ORDER_STATES:
        raise JournalError("unknown_order_state")
    current = order.get("state") if isinstance(order, dict) else None
    if current not in TRANSITIONS or new_state not in TRANSITIONS[current]:
        raise JournalError(f"illegal_transition:{current}->{new_state}")
    if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
        raise JournalError("transition_reason_invalid")
    fields = dict(fields or {})
    unknown = set(fields) - MUTABLE_FIELDS
    if unknown:
        raise JournalError("transition_field_not_mutable")
    result = copy.deepcopy(order)
    result.update(copy.deepcopy(fields))
    result["state"] = new_state
    if "size_remaining_p" not in fields:
        if new_state in ("SUBMITTING", "UNKNOWN", "ABSENT_UNCONFIRMED", "EXECUTABLE"):
            result["size_remaining_p"] = _remaining_unknown(result)
        elif new_state == "LAPSED" and current == "EXECUTABLE" and "size_lapsed_p" not in fields:
            # The known open remainder of a live order lapsed.
            result["size_lapsed_p"] = result["size_lapsed_p"] + result["size_remaining_p"]
            result["size_remaining_p"] = 0
        elif new_state in ("EXECUTION_COMPLETE", "REFUSED", "FAILED", "ABSENT_ACCEPTED", "REPLACED", "LAPSED"):
            result["size_remaining_p"] = 0
    if new_state == "LAPSED" and _is_int(result["size_matched_p"]) and result["size_matched_p"] > 0:
        raise JournalError("lapsed_with_matched_size_use_execution_complete")
    if new_state in SIZED_FINAL_STATES:
        # Voided fragments leave the matched size (Betfair's sizeVoided), so they are part of the total.
        parts = [result[key] for key in ("size_matched_p", "size_cancelled_p", "size_lapsed_p", "size_remaining_p",
                                         "size_voided_p")]
        if all(_is_int(part) for part in parts) and (
                result["size_remaining_p"] != 0 or sum(parts) != result["size_p"]):
            raise JournalError("final_state_size_unaccounted")
    if result.get("provisional_child") is True and new_state not in _PROVISIONAL_STATES:
        result["provisional_child"] = False
    if new_state in ("REFUSED", "FAILED", "UNKNOWN", "ABSENT_UNCONFIRMED") and result.get("error_code") is None:
        result["error_code"] = reason
    result["exposure_basis"] = exposure_basis(result)
    try:
        validate_order_record(result)
    except ValueError:
        raise JournalError("transition_produces_invalid_order_record") from None
    return result


def _is_int(value):
    return type(value) is int


def _category(code, surface):
    if code is None:
        return None
    return classify(code, surface=surface)


def _definitive(code, surface):
    return _category(code, surface) in DEFINITIVE_CATEGORIES


def _all(refs, state, reason, extra=None):
    return {ref: (state, dict(extra or {}, error_code=reason)) for ref in refs}


def _place_instruction(item, *, execution_code, source, order=None):
    """(state, fields) for one place instruction report, first matching rule of spec section 9.4.

    Beyond the spec rules, two report shapes are anomalous and so UNKNOWN (reconciled by ref):
    a success or matched size without a betId (the bet could be neither cancelled nor confirmed by
    betId), and EXECUTION_COMPLETE that does not account for the whole size (nothing matched, or,
    when the SUBMITTING order is given, less than its size). A matched size larger than the order
    is UNKNOWN without sizes.
    """
    status, code = item["status"], item["error_code"]
    matched, bet_id, order_status = item["size_matched_p"], item["bet_id"], item["order_status"]
    fields = {"bet_id": bet_id, "size_matched_p": matched or 0, "average_price_matched_h":
              item["average_price_matched_h"] if matched else None, "error_code": code}
    open_size = None if order is None else _remaining_unknown(order)
    if source == "paper-v1" and status == "FAILURE" and not matched and bet_id is None:
        if code in PAPER_REFUSAL_CODES:
            return "REFUSED", fields
        if code in PAPER_FAILED_CODES:
            return "FAILED", fields
    if matched and open_size is not None and matched > open_size:
        return "UNKNOWN", {"bet_id": bet_id, "error_code": "matched_size_exceeds_order"}
    if matched:
        if bet_id is None:
            return "UNKNOWN", dict(fields, error_code=code or "bet_id_missing_with_matched_size")
        if order_status == "EXECUTION_COMPLETE" and open_size is not None and matched != open_size:
            return "UNKNOWN", dict(fields, error_code=code or "complete_size_unaccounted")
        if order_status in ("EXECUTABLE", "EXECUTION_COMPLETE"):
            return order_status, fields
        return "UNKNOWN", dict(fields, error_code=code or "matched_without_order_status")
    if bet_id is not None and status == "FAILURE":
        return "UNKNOWN", dict(fields, error_code=code or "bet_id_echoed_on_failure")
    if status == "SUCCESS":
        if order_status == "EXECUTION_COMPLETE" and matched is None:
            # Complete with no matched size stated: the whole order may have matched. Never zero.
            return "UNKNOWN", dict(fields, error_code=code or "matched_size_missing")
        if order_status == "EXECUTION_COMPLETE":
            # Complete with nothing matched: where the size went (lapsed? cancelled?) is not stated.
            return "UNKNOWN", dict(fields, error_code=code or "complete_size_unaccounted")
        if order_status == "EXECUTABLE":
            if bet_id is None:
                return "UNKNOWN", dict(fields, error_code=code or "bet_id_missing_on_success")
            return order_status, fields
        return "UNKNOWN", dict(fields, error_code=code or "unexpected_order_status")
    if status == "FAILURE" and bet_id is None:
        if code is not None and _definitive(code, "instruction_report"):
            return "FAILED", fields
        if code is None and execution_code is not None and _definitive(execution_code, "execution_report"):
            return "FAILED", dict(fields, error_code=execution_code)
    return "UNKNOWN", dict(fields, error_code=code or ("timeout" if status == "TIMEOUT" else "unclassified_outcome"))


def _match_by_ref(items, sent, key):
    """Pair report items with sent keys: by echoed key, else by position. None on any mismatch."""
    if len(items) != len(sent) or len(set(sent)) != len(sent):
        return None
    paired = {}
    for position, item in enumerate(items):
        echoed = item[key]
        target = echoed if echoed is not None else sent[position]
        if target not in sent or target in paired:
            return None
        paired[target] = item
    return paired


def classify_place_report(result, *, sent_refs, orders=None):
    """{customer_order_ref: (state, fields)} for every ref sent in one place request.

    orders (optional, recommended) maps ref -> the SUBMITTING order record. With it, an
    EXECUTION_COMPLETE whose matched size is less than the order's size is UNKNOWN here; without
    it, such a move is refused later by transition() (final_state_size_unaccounted).
    """
    sent = list(sent_refs)
    orders = orders or {}
    try:
        report = validate_place_report(result)
    except ValueError:
        return _all(sent, "UNKNOWN", "report_invalid")
    items = report["instructions"]
    execution_code = report["error_code"]
    if not items:
        if report["status"] == "FAILURE" and execution_code is not None and _definitive(execution_code,
                                                                                         "execution_report"):
            return _all(sent, "FAILED", execution_code)
        return _all(sent, "UNKNOWN", execution_code or ("timeout" if report["status"] == "TIMEOUT"
                                                        else "report_without_instructions"))
    paired = _match_by_ref(items, sent, "customer_order_ref")
    if paired is None:
        return _all(sent, "UNKNOWN", "report_ref_mismatch")
    return {ref: _place_instruction(paired[ref], execution_code=execution_code, source=report["source"],
                                    order=orders.get(ref)) for ref in sent}


def _cancel_instruction(item, order):
    status, code, cancelled = item["status"], item["error_code"], item["size_cancelled_p"]
    if order is not None and order["state"] != "CANCEL_PENDING":
        # A kill-sweep cancel of an order that was already unresolved: its remaining size is not
        # authoritative, so nothing is sized from this report. It stays UNKNOWN for reconcile.
        order = None
    if status == "SUCCESS" and cancelled is not None and order is not None:
        remaining = order["size_remaining_p"]
        reduction = item["size_reduction_p"]
        total = order["size_cancelled_p"] + cancelled
        if cancelled > remaining:
            return "UNKNOWN", {"error_code": "cancel_size_inconsistent_refresh_needed"}
        if reduction is None:
            if cancelled == remaining:
                return "EXECUTION_COMPLETE", {"size_cancelled_p": total, "size_remaining_p": 0, "error_code": code}
            # Less was cancelled than was open: the difference matched (or lapsed) at an unknown price.
            return "UNKNOWN", {"size_cancelled_p": total, "size_remaining_p": remaining - cancelled,
                               "error_code": "cancel_size_short_refresh_needed"}
        if cancelled <= min(reduction, remaining):
            left = remaining - cancelled
            if cancelled == reduction or left == 0:
                state = "EXECUTABLE" if left > 0 else "EXECUTION_COMPLETE"
                return state, {"size_cancelled_p": total, "size_remaining_p": left, "error_code": code}
        return "UNKNOWN", {"error_code": "cancel_size_inconsistent_refresh_needed"}
    if status == "FAILURE":
        if code == "BET_TAKEN_OR_LAPSED":
            return "UNKNOWN", {"error_code": code}
        if code is not None and _definitive(code, "instruction_report") and order is not None:
            # The cancel failed; the order is as it was, with its full remaining exposure.
            return "EXECUTABLE", {"size_remaining_p": order["size_remaining_p"], "error_code": code}
    if status == "SUCCESS":
        return "UNKNOWN", {"error_code": code or "cancelled_sizes_unconfirmed_refresh_needed"}
    return "UNKNOWN", {"error_code": code or ("timeout" if status == "TIMEOUT" else "cancel_outcome_unknown")}


def classify_cancel_report(result, *, sent_bet_ids, orders=None):
    """{bet_id: (state, fields)} for every bet id sent in one cancel request.

    orders maps bet_id -> the CANCEL_PENDING order record; without it nothing can be sized, so
    every instruction that needs sizes is UNKNOWN. A bet whose order is not CANCEL_PENDING (a
    kill-sweep cancel of an UNKNOWN, ABSENT_UNCONFIRMED or REPLACE_PENDING order, or of a
    provisional child) is always UNKNOWN: legal from each of those states, and resolved by a
    betId refresh. A bet with no order in the journal (found on the exchange by the sweep) has
    nothing to transition; its result is reported for the record only.
    """
    sent = list(sent_bet_ids)
    orders = orders or {}
    try:
        report = validate_cancel_report(result)
    except ValueError:
        return _all(sent, "UNKNOWN", "report_invalid")
    items = report["instructions"]
    execution_code = report["error_code"]
    if not items:
        if (report["status"] == "FAILURE" and execution_code is not None
                and _definitive(execution_code, "execution_report")
                and all(b in orders and orders[b]["state"] == "CANCEL_PENDING" for b in sent)):
            return {b: ("EXECUTABLE", {"size_remaining_p": orders[b]["size_remaining_p"], "error_code": execution_code})
                    for b in sent}
        return _all(sent, "UNKNOWN", execution_code or "report_without_instructions")
    paired = _match_by_ref(items, sent, "bet_id")
    if paired is None:
        return _all(sent, "UNKNOWN", "report_ref_mismatch")
    return {b: _cancel_instruction(paired[b], orders.get(b)) for b in sent}


def _replace_instruction(item, parent, child_ref, source):
    """{ref: (state, fields)} for one replace instruction (parent and its provisional child)."""
    parent_ref = parent["customer_order_ref"]
    cancel, place = item["cancel"], item["place"]
    unknown = {parent_ref: ("UNKNOWN", {"error_code": item["error_code"] or "replace_outcome_unknown"})}
    if child_ref is not None:
        unknown[child_ref] = ("UNKNOWN", {"error_code": "replace_child_unresolved"})
    place_bet = place["bet_id"] if place else None
    place_matched = place["size_matched_p"] if place else None
    child_size = parent["size_remaining_p"]
    cancel_ok = (cancel is not None and cancel["status"] == "SUCCESS"
                 and cancel["size_cancelled_p"] == parent["size_remaining_p"])
    if item["status"] == "SUCCESS" and cancel_ok and place is not None and place["status"] == "SUCCESS":
        if (place_bet is None or place["order_status"] not in ("EXECUTABLE", "EXECUTION_COMPLETE")
                or (place_matched is not None and place_matched > child_size)
                or (place["order_status"] == "EXECUTION_COMPLETE" and place_matched != child_size)):
            return unknown
        if child_ref is None:
            # The new bet is live but has no provisional child to record it: never REPLACED with
            # the replacement untracked. The parent stays UNKNOWN (market blocked) for reconcile.
            return {parent_ref: ("UNKNOWN", {"error_code": "replace_child_untracked"})}
        return {parent_ref: ("REPLACED", {"size_cancelled_p": parent["size_cancelled_p"] + cancel["size_cancelled_p"],
                                          "size_remaining_p": 0, "child_ref": child_ref,
                                          "error_code": item["error_code"]}),
                child_ref: (place["order_status"], {
                    "bet_id": place_bet, "size_matched_p": place_matched or 0,
                    "average_price_matched_h": place["average_price_matched_h"] if place_matched else None,
                    "provisional_child": False, "error_code": place["error_code"]})}
    place_definitely_absent = place is None or (place["status"] == "FAILURE" and place_bet is None
                                                and not place_matched)
    if cancel_ok and place_definitely_absent and (
            item["error_code"] == "CANCELLED_NOT_PLACED"
            or (place is not None and place["error_code"] == "CANCELLED_NOT_PLACED")):
        out = {parent_ref: ("EXECUTION_COMPLETE", {
            "size_cancelled_p": parent["size_cancelled_p"] + cancel["size_cancelled_p"], "size_remaining_p": 0,
            "error_code": "CANCELLED_NOT_PLACED"})}
        if child_ref is not None:
            out[child_ref] = ("FAILED", {"provisional_child": False, "error_code": "CANCELLED_NOT_PLACED"})
        return out
    cancel_code = cancel["error_code"] if cancel is not None else None
    paper_refusal = source == "paper-v1" and cancel_code in PAPER_REFUSAL_CODES | PAPER_FAILED_CODES
    cancel_failed = (cancel is not None and cancel["status"] == "FAILURE" and cancel_code is not None
                     and (paper_refusal or _definitive(cancel_code, "instruction_report")))
    if cancel_failed and place_definitely_absent:
        out = {parent_ref: ("EXECUTABLE", {"size_remaining_p": parent["size_remaining_p"],
                                           "error_code": cancel["error_code"]})}
        if child_ref is not None:
            out[child_ref] = ("FAILED", {"provisional_child": False, "error_code": "replace_cancel_failed"})
        return out
    return unknown


def classify_replace_report(result, *, sent, provisional_children):
    """{customer_order_ref: (state, fields)} for each replaced parent and its provisional child.

    sent: [{"bet_id", "new_price_h", "order": parent record (REPLACE_PENDING)}] in request order.
    provisional_children: {parent customer_order_ref: child customer_order_ref}. Every parent
    must have one (spec section 9.4: the child is journaled at replace submit); a successful replace
    of a parent without one is UNKNOWN("replace_child_untracked"), never REPLACED.
    """
    sent = list(sent)
    parents = [entry["order"] for entry in sent]
    refs = []
    for parent in parents:
        refs.append(parent["customer_order_ref"])
        if provisional_children.get(parent["customer_order_ref"]) is not None:
            refs.append(provisional_children[parent["customer_order_ref"]])
    try:
        report = validate_replace_report(result)
    except ValueError:
        return _all(refs, "UNKNOWN", "report_invalid")
    items = report["instructions"]
    execution_code = report["error_code"]
    if not items:
        if report["status"] == "FAILURE" and execution_code is not None and _definitive(execution_code,
                                                                                         "execution_report"):
            out = {}
            for parent in parents:
                ref = parent["customer_order_ref"]
                out[ref] = ("EXECUTABLE", {"size_remaining_p": parent["size_remaining_p"],
                                           "error_code": execution_code})
                if provisional_children.get(ref) is not None:
                    out[provisional_children[ref]] = ("FAILED", {"provisional_child": False,
                                                                 "error_code": execution_code})
            return out
        return _all(refs, "UNKNOWN", execution_code or "report_without_instructions")
    paired = _match_by_ref(items, [entry["bet_id"] for entry in sent], "bet_id")
    if paired is None:
        return _all(refs, "UNKNOWN", "report_ref_mismatch")
    out = {}
    for entry in sent:
        item = paired[entry["bet_id"]]
        if item["new_price_h"] is not None and item["new_price_h"] != entry["new_price_h"]:
            parent_ref = entry["order"]["customer_order_ref"]
            out.update(_all([r for r in (parent_ref, provisional_children.get(parent_ref)) if r],
                            "UNKNOWN", "report_price_mismatch"))
            continue
        out.update(_replace_instruction(item, entry["order"],
                                        provisional_children.get(entry["order"]["customer_order_ref"]),
                                        report["source"]))
    return out


def report_transactions(action, result, *, sent_count=1):
    """Spec section 10 R7 transactions a report adds beyond those counted at submit.

    A placement (and a replace) is counted once at submit whatever its outcome; a failed cancel
    counts 1, as does the failed cancel half of a replace. An unknown outcome (no report, or a
    report without instruction rows) counts every sent instruction as failed: the conservative
    reading. result None means the call raised.
    """
    if action not in ("place", "cancel", "replace", "cancel_all"):
        raise ValueError("unknown report action")
    if type(sent_count) is not int or sent_count < 0:
        raise ValueError("sent_count must be a non-negative integer")
    if action == "place":
        return 0
    items = result.get("instructions") if isinstance(result, dict) else None
    if not items:
        if isinstance(result, dict) and result.get("status") == "SUCCESS":
            return 0
        return max(1, sent_count) if action == "cancel_all" else sent_count
    count = 0
    for item in items:
        if not isinstance(item, dict):
            count += 1
        elif action in ("cancel", "cancel_all"):
            count += item.get("status") != "SUCCESS"
        else:
            cancel = item.get("cancel")
            count += not isinstance(cancel, dict) or cancel.get("status") != "SUCCESS"
    return count


def outcome_from_exception(exc):
    """How an exception raised by send_order leaves the orders it carried."""
    if isinstance(exc, TransportError):
        return "UNKNOWN" if exc.may_have_arrived else "FAILED_NOT_SENT"
    if isinstance(exc, ApiError):
        if exc.category in REJECTED_EXCEPTION_CATEGORIES:
            return "FAILED_REJECTED"
        return "UNKNOWN"
    if isinstance(exc, OrderRefused) and isinstance(exc.rule, str) and (
            exc.rule.startswith("capability_") or exc.rule == "send_bound_not_for_this_method"):
        # The capability is consumed (or, for a send bound on the wrong method, burned unused) before the
        # transport is called: nothing was sent (amendment A3, C2).
        return "FAILED_NOT_SENT"
    return "UNKNOWN"


def halt_reason(category):
    """The spec section 9.8 HALT reason for an order-call error category, or None."""
    return HALT_CATEGORIES.get(category)
