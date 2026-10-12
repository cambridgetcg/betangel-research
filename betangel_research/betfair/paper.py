"""Deterministic paper fill model paper-v1 and the offline paper replay over recorded snapshots.

Integer pence and hundredths only, no randomness, sorted keys: the same snapshots and orders
give byte-identical reports. The model under-fills on purpose (spec section 13): immediate fills take
only a fraction of displayed size, passive fills come only from traded volume at our exact price
after the displayed queue ahead of us is consumed, an unknown queue never fills, and suspensions
block fills. Reports use the same normalised shapes as Betfair's, so lifecycle cannot tell paper
from live. A paper exchange holds no transport, client or socket of any kind.
"""

from decimal import Decimal, ROUND_FLOOR
import hashlib
from pathlib import Path
import platform
import secrets
import shutil

from .constants import COMMISSION_SOURCES, MIN_BET_PENCE_GBP
from .filters import MARKET_ID_RE
from .journal import OrderJournal
from .lifecycle import classify_place_report
from .pnl import market_realized, order_pnl_pence
from .receipts import (SYSTEM_CLOCK, append_jsonl, atomic_write_json, bytes_sha256, canonical_sha256, code_hashes,
                       file_sha256, git_state, parse_utc_ns, strict_json_loads, utc_from_ns)
from .schemas import (validate_book_snapshot, validate_cancel_report, validate_place_report,
                      validate_replace_report)
from .ticks import is_on_ladder, parse_money, parse_price, to_hundredths
from .validate import make_customer_order_ref, make_customer_ref, profit_ratio_ok

PAPER_MODEL = "paper-v1"
PAPER_ASSUMPTIONS = (
    "An order becomes active submit_latency_ms after submission; the book used is the latest snapshot at or "
    "before activation (after every snapshot sharing that time), refused if older than max_snapshot_age_s or "
    "marked invalid.",
    "Immediate matches walk displayed levels best first and take at most floor(level size x "
    "displayed_size_fraction) per level, at that level's price; what our orders have taken at a price is "
    "subtracted from that level in every later snapshot of the market, because the feed never shows our "
    "paper fills; nothing fills beyond displayed depth.",
    "A resting BACK queues behind the displayed availableToLay size at its price and a resting LAY behind the "
    "displayed availableToBack size; beyond the displayed range the queue is unknown and the order never fills "
    "passively.",
    "Passive fills come only from traded volume increases at the order's exact price between consecutive OPEN "
    "snapshots, the earlier one at or after the order's activation, above the highest volume already seen "
    "there, divided by traded_volume_divisor, consumed by the queue ahead first and then by our earlier "
    "orders at that price in activation order; cancellations ahead never help and nothing fills by trading "
    "through.",
    "Negative traded volume changes and settlement-zeroing frames are ignored and flagged.",
    "Unmatched size lapses at the first in-play snapshot, when the runner is removed, when another runner newly "
    "shows as removed, or when the market version changes across a SUSPENDED snapshot (either side of the "
    "change); a plain suspension only blocks fills, and so does a version change between two OPEN snapshots.",
    "Settlement uses the WINNER and LOSER runner statuses of the CLOSED market; a removed runner (rule 4) or a dead "
    "heat excludes the market from P&L totals.",
    "Commission is charged on positive market totals through evaluation.settled_market_costs at the configured, "
    "labelled commission_rate.",
    "Unsupported in paper exactly as in live: in-play, PERSIST, FOK, minFill, betTarget, MOC/LOC, async and stakes "
    "below 1.00.",
)
STATE_KEYS = frozenset({"taken", "activated_ms", "traded_hwm", "activation", "sequence"})
PARAM_KEYS = frozenset({"model", "submit_latency_ms", "displayed_size_fraction", "passive_fills",
                        "traded_volume_divisor", "max_snapshot_age_s", "commission_rate", "commission_source"})
ORDER_FILE_KEYS = frozenset({"submit_pt", "market_id", "selection_id", "side", "price", "size"})
REPLAY_STATUS = "paper_simulation_not_execution_evidence"
LEDGER_KEYS = ("created_at", "experiment_id", "source_sha256", "config_sha256", "code_sha256", "report",
               "report_sha256", "status")
REMOVED_STATUSES = frozenset({"REMOVED", "REMOVED_VACANT"})


def _check_params(params):
    if not isinstance(params, dict) or set(params) != PARAM_KEYS:
        raise ValueError("paper params must have exactly the orders.paper keys")
    if params["model"] != PAPER_MODEL or params["passive_fills"] != "trd_queue_conservative":
        raise ValueError("paper params must use model paper-v1 with trd_queue_conservative passive fills")
    if type(params["submit_latency_ms"]) is not int or not 0 <= params["submit_latency_ms"] <= 60000:
        raise ValueError("submit_latency_ms must be an integer from 0 to 60000")
    for key, low, high in (("displayed_size_fraction", 0, 1), ("traded_volume_divisor", 1, 100),
                           ("max_snapshot_age_s", 0, 86400), ("commission_rate", 0, 1)):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f"{key} out of range")
    if params["displayed_size_fraction"] <= 0 or params["max_snapshot_age_s"] <= 0 or params["commission_rate"] >= 1:
        raise ValueError("paper params out of range")
    if params["commission_source"] not in COMMISSION_SOURCES:
        raise ValueError("commission_source must be a labelled source")
    return dict(params)


def _dec(value):
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _pence_floor(amount):
    return int((amount * 100).to_integral_value(rounding=ROUND_FLOOR))


def _book_time_ms(book):
    if book["source"] == "recording" and book["pt"] is not None:
        return book["pt"]
    return parse_utc_ns(book["observed_at"]) // 1_000_000


def book_time_ms(book):
    """The paper model's time of a book snapshot in ms (pt for recordings, else observed_at), or None."""
    try:
        return _book_time_ms(book)
    except (KeyError, TypeError, ValueError):
        return None


def _levels(levels):
    """[(price_h, size_gbp Decimal)]; raises ValueError for off-2dp prices or null sizes."""
    out = []
    for price, size in levels:
        if size is None:
            raise ValueError("null size")
        out.append((to_hundredths(price), _dec(size)))
    return out


def _traded(runner):
    """{price_h: volume Decimal} or None when the runner carries no traded data."""
    traded = runner["traded"]
    if traded is None:
        return None
    out = {}
    for price, volume in traded.items():
        if volume is None:
            raise ValueError("null traded volume")
        out[to_hundredths(Decimal(price))] = _dec(volume)
    return out


def paper_bet_id(ref):
    """Deterministic synthetic bet id (digits) for a paper order reference."""
    return str(int(hashlib.sha256(ref.encode("utf-8")).hexdigest()[:15], 16))


def _average(fills, side):
    total = sum(size for size, _ in fills)
    if total == 0:
        return None
    weighted = sum(size * price for size, price in fills)
    # Rounded against us: a lower average for a BACK, a higher one for a LAY.
    return weighted // total if side == "BACK" else -((-weighted) // total)


class PaperExchange:
    """paper-v1 matching against snapshots. queues carries known queue-ahead pence per order ref
    between calls (None = unknown); an order without a known queue never fills passively.

    Our own orders never fill beyond what the market showed: our immediate takes at each market,
    selection, ladder and price are cumulative and are subtracted from that level in every later
    snapshot (the real feed never reflects paper fills), and the traded volume of one advance step
    is shared by our resting orders at a price in activation order, each after its own queue ahead.
    Passive fills count only volume above the highest traded volume already seen at that price,
    and only in intervals that start at or after the order's activation.

    state (export_state()) carries the takes, activation times, traded high-water marks and the
    activation order (passive-fill priority, R3#9) between processes; the gateway journals it as the
    payload "paper_state" of the same event as the fills it goes with (R3#12)."""

    def __init__(self, params, *, queues=None, state=None):
        self.params = _check_params(params)
        self._queues = dict(queues or {})
        self._taken = {}             # market_id -> {(selection, ladder, price_h): pence taken by our fills}
        self._activated_ms = {}      # order ref -> activation time (ms) of a resting order
        self._traded_hwm = {}        # market_id -> {(selection, price_h): highest traded volume seen}
        self._activation = {}        # order ref -> activation sequence number (passive fill priority)
        self._sequence = 0
        if state:
            self._load_state(state)

    def queue_state(self):
        return dict(sorted(self._queues.items()))

    def export_state(self):
        """JSON-ready takes, activation times, traded high-water marks and activation order (see the class
        docstring)."""
        taken = {market: {f"{sel}:{ladder}:{price}": pence for (sel, ladder, price), pence in sorted(levels.items())}
                 for market, levels in sorted(self._taken.items())}
        hwm = {market: {f"{sel}:{price}": str(volume) for (sel, price), volume in sorted(levels.items())}
               for market, levels in sorted(self._traded_hwm.items())}
        return {"taken": taken, "activated_ms": dict(sorted(self._activated_ms.items())), "traded_hwm": hwm,
                "activation": dict(sorted(self._activation.items())), "sequence": self._sequence}

    def _load_state(self, state):
        """State from export_state(); activation and sequence may be absent (states written before R3#9:
        their orders keep no priority and sort after every sequenced order, as before)."""
        if not isinstance(state, dict) or not set(state) <= STATE_KEYS:
            raise ValueError("paper exchange state keys must be taken, activated_ms, traded_hwm, activation and "
                             "sequence")
        for market, levels in (state.get("taken") or {}).items():
            parsed = {}
            for key, pence in levels.items():
                sel, ladder, price = key.split(":")
                if ladder not in ("back", "lay") or type(pence) is not int or pence < 0:
                    raise ValueError("paper taken state invalid")
                parsed[(int(sel), ladder, int(price))] = pence
            self._taken[str(market)] = parsed
        for ref, at_ms in (state.get("activated_ms") or {}).items():
            if not isinstance(ref, str) or type(at_ms) is not int:
                raise ValueError("paper activation state invalid")
            self._activated_ms[ref] = at_ms
        for market, levels in (state.get("traded_hwm") or {}).items():
            parsed = {}
            for key, volume in levels.items():
                sel, price = key.split(":")
                value = Decimal(volume)
                if not value.is_finite() or value < 0:
                    raise ValueError("paper traded high-water mark invalid")
                parsed[(int(sel), int(price))] = value
            self._traded_hwm[str(market)] = parsed
        sequence = state.get("sequence", 0)
        activation = state.get("activation") or {}
        if (type(sequence) is not int or sequence < 0 or not isinstance(activation, dict)
                or any(not isinstance(ref, str) or type(n) is not int or not 1 <= n <= sequence
                       for ref, n in activation.items())):
            raise ValueError("paper activation order invalid")
        self._activation = dict(activation)
        self._sequence = sequence

    # --- helpers -----------------------------------------------------------------------------

    def _activation_refusal(self, book, now_ms):
        """A paper refusal code for the activation book, or None if it can be used."""
        if book is None:
            return "paper_book_stale"
        try:
            validate_book_snapshot(book)
            for runner in book["runners"].values():
                _levels(runner["back"])
                _levels(runner["lay"])
        except (ValueError, TypeError):
            return "paper_book_invalid"
        if book["invalid_reason"] is not None:
            return "paper_book_invalid"
        activation = now_ms + self.params["submit_latency_ms"]
        book_ms = _book_time_ms(book)
        if book_ms > activation:
            return "paper_book_after_activation"
        if activation - book_ms > int(_dec(self.params["max_snapshot_age_s"]) * 1000):
            return "paper_book_stale"
        return None

    def _forget(self, ref):
        """An order with nothing left resting: drop its queue, activation time and priority."""
        self._queues.pop(ref, None)
        self._activated_ms.pop(ref, None)
        self._activation.pop(ref, None)

    def close_market(self, market_id, refs):
        """Drop a closed market's takes and traded high-water marks, and refs' queues, activation times and
        priorities (the gateway calls it once every paper order in that market is final)."""
        self._taken.pop(market_id, None)
        self._traded_hwm.pop(market_id, None)
        for ref in refs:
            self._forget(ref)

    def _taken_levels(self, book):
        """Pence our immediate fills have taken at each (selection, ladder, price) of this market so far.

        Never reset by a new snapshot: the feed does not show our paper fills, so an unchanged level in
        a later snapshot still holds only what we did not take."""
        return self._taken.setdefault(book["market_id"], {})

    def _immediate(self, order, runner, taken):
        """(fills [(size_p, price_h)], queue_ahead or None) for one order against one runner."""
        fraction = _dec(self.params["displayed_size_fraction"])
        side, price, remaining = order["side"], order["price_h"], order["size_p"]
        ladder_name = "back" if side == "BACK" else "lay"
        fills = []
        levels = _levels(runner[ladder_name])
        for level_price, level_size in levels:
            if remaining <= 0 or (level_price < price if side == "BACK" else level_price > price):
                break
            level_key = (int(order["selection_id"]), ladder_name, level_price)
            available = _pence_floor(level_size * fraction) - taken.get(level_key, 0)
            take = min(remaining, max(0, available))
            if take > 0:
                fills.append((take, level_price))
                taken[level_key] = taken.get(level_key, 0) + take
                remaining -= take
        queue = None
        if remaining > 0:
            # A resting BACK joins the unmatched backs shown in the lay ladder; a LAY the back ladder.
            ladder = _levels(runner["lay"] if side == "BACK" else runner["back"])
            if ladder:
                last = ladder[-1][0]
                inside = price <= last if side == "BACK" else price >= last
                if inside:
                    queue = sum(_pence_floor(size) for p, size in ladder if p == price)
        return fills, queue

    def _place_one(self, order, book, now_ms):
        ref = order["customer_order_ref"]
        base = {"customer_order_ref": ref, "status": "FAILURE", "error_code": None, "order_status": None,
                "bet_id": None, "placed_date": None, "average_price_matched_h": None, "size_matched_p": None}
        refusal = self._activation_refusal(book, now_ms)
        if refusal is not None:
            return dict(base, error_code=refusal)
        runner = book["runners"].get(str(order["selection_id"]))
        if (book["status"] != "OPEN" or book["inplay"] is not False or runner is None
                or runner["status"] != "ACTIVE"):
            return dict(base, error_code="paper_rejected_market_state")
        if (order["side"] not in ("BACK", "LAY") or not is_on_ladder(order["price_h"])
                or type(order["size_p"]) is not int or order["size_p"] < MIN_BET_PENCE_GBP):
            raise ValueError("paper orders must be validated LAPSE LIMIT orders of at least 1.00 on the ladder")
        fills, queue = self._immediate(order, runner, self._taken_levels(book))
        matched = sum(size for size, _ in fills)
        activation = now_ms + self.params["submit_latency_ms"]
        if matched < order["size_p"]:
            self._queues[ref] = queue
            self._activated_ms[ref] = activation
            self._sequence += 1
            self._activation[ref] = self._sequence
        return dict(base, status="SUCCESS", order_status="EXECUTION_COMPLETE" if matched == order["size_p"]
                    else "EXECUTABLE", bet_id=paper_bet_id(ref), placed_date=utc_from_ns(activation * 1_000_000),
                    average_price_matched_h=_average(fills, order["side"]), size_matched_p=matched)

    @staticmethod
    def _envelope(items, market_id):
        statuses = {item["status"] for item in items}
        if statuses == {"SUCCESS"}:
            status = "SUCCESS"
        elif statuses == {"FAILURE"}:
            status = "FAILURE"
        else:
            status = "PROCESSED_WITH_ERRORS"
        return {"status": status, "error_code": None, "market_id": market_id, "instructions": items,
                "source": PAPER_MODEL}

    # --- the exchange ------------------------------------------------------------------------

    def place(self, orders, *, book, now_ms, refusal=None):
        """Normalised place report for orders submitted at now_ms (activation adds the latency).

        refusal (a PAPER refusal code, for example paper_book_in_recording_gap) refuses every order
        without looking at the book."""
        if type(now_ms) is not int or not orders:
            raise ValueError("place needs orders and an integer now_ms")
        market_id = orders[0]["market_id"]
        if refusal is not None:
            items = [{"customer_order_ref": order["customer_order_ref"], "status": "FAILURE", "error_code": refusal,
                      "order_status": None, "bet_id": None, "placed_date": None, "average_price_matched_h": None,
                      "size_matched_p": None} for order in orders]
        else:
            items = [self._place_one(order, book, now_ms) for order in orders]
        return validate_place_report(self._envelope(items, market_id))

    def cancel(self, orders, cancels, *, now_ms):
        """Normalised cancel report; a reduction cancels min(reduction, remaining)."""
        if type(now_ms) is not int or not cancels:
            raise ValueError("cancel needs cancels and an integer now_ms")
        by_bet = {o["bet_id"]: o for o in orders if o["bet_id"] is not None}
        market_id = next(iter(by_bet.values()))["market_id"] if by_bet else orders[0]["market_id"]
        items = []
        for cancel in cancels:
            if isinstance(cancel, str):
                cancel = {"bet_id": cancel, "size_reduction_p": None}
            order = by_bet.get(cancel["bet_id"])
            reduction = cancel["size_reduction_p"]
            item = {"bet_id": cancel["bet_id"], "size_reduction_p": reduction, "status": "FAILURE",
                    "error_code": None, "size_cancelled_p": None, "cancelled_date": None}
            if order is None or order["state"] != "EXECUTABLE" or order["size_remaining_p"] <= 0:
                items.append(dict(item, error_code="INVALID_BET_ID" if order is None else "BET_TAKEN_OR_LAPSED"))
                continue
            remaining = order["size_remaining_p"]
            cancelled = remaining if reduction is None else min(reduction, remaining)
            if cancelled == remaining:
                self._forget(order["customer_order_ref"])
            items.append(dict(item, status="SUCCESS", size_cancelled_p=cancelled,
                              cancelled_date=utc_from_ns(now_ms * 1_000_000)))
        return validate_cancel_report(self._envelope(items, market_id))

    def replace(self, orders, replaces, *, book, now_ms):
        """Normalised replace report: cancel the remaining size, then place it at the new price.

        replaces: [{"bet_id", "new_price_h", "child_ref"}] (child_ref optional; it keys the new
        order's queue). If the book cannot be used, nothing is cancelled.
        """
        if type(now_ms) is not int or not replaces:
            raise ValueError("replace needs replaces and an integer now_ms")
        by_bet = {o["bet_id"]: o for o in orders if o["bet_id"] is not None}
        market_id = next(iter(by_bet.values()))["market_id"] if by_bet else book["market_id"]
        refusal = self._activation_refusal(book, now_ms)
        items = []
        for spec in replaces:
            order = by_bet.get(spec["bet_id"])
            item = {"bet_id": spec["bet_id"], "new_price_h": spec["new_price_h"], "status": "FAILURE",
                    "error_code": None, "cancel": None, "place": None}
            cancel_half = {"bet_id": spec["bet_id"], "size_reduction_p": None, "status": "FAILURE",
                           "error_code": None, "size_cancelled_p": None, "cancelled_date": None}
            if order is None or order["state"] != "EXECUTABLE" or order["size_remaining_p"] <= 0:
                code = "INVALID_BET_ID" if order is None else "BET_TAKEN_OR_LAPSED"
                items.append(dict(item, error_code=code, cancel=dict(cancel_half, error_code=code)))
                continue
            if refusal is not None or not is_on_ladder(spec["new_price_h"]):
                code = refusal or "paper_rejected_market_state"
                items.append(dict(item, error_code=code, cancel=dict(cancel_half, error_code=code)))
                continue
            remaining = order["size_remaining_p"]
            self._forget(order["customer_order_ref"])
            cancel_half = dict(cancel_half, status="SUCCESS", size_cancelled_p=remaining,
                               cancelled_date=utc_from_ns(now_ms * 1_000_000))
            child_ref = spec.get("child_ref") or f"replace-{spec['bet_id']}-{spec['new_price_h']}"
            child = {"customer_order_ref": child_ref, "market_id": order["market_id"],
                     "selection_id": order["selection_id"], "side": order["side"], "price_h": spec["new_price_h"],
                     "size_p": remaining}
            placed = self._place_one(child, book, now_ms)
            if not spec.get("child_ref"):
                self._forget(child_ref)
            placed["customer_order_ref"] = None
            if placed["status"] == "SUCCESS":
                items.append(dict(item, status="SUCCESS", cancel=cancel_half, place=placed))
            else:
                items.append(dict(item, error_code="CANCELLED_NOT_PLACED", cancel=cancel_half,
                                  place=dict(placed, error_code="CANCELLED_NOT_PLACED")))
        return validate_replace_report(self._envelope(items, market_id))

    def advance(self, open_orders, *, prev_book, book, now_ms, gap=False):
        """Passive fills and lapses from prev_book to book: [{"customer_order_ref", "state", "fields", "flags"}].

        Only orders that changed or were flagged are listed. now_ms is the new book's time. Lapses are
        judged on any schema-valid book (an invalid_reason only blocks fills); passive fills need two
        usable consecutive OPEN snapshots, the earlier one at or after the order's activation. gap
        (True when the interval spans a recording gap, so nothing between the two books was observed)
        blocks passive fills for the interval; its volume is never counted later either.
        """
        if type(now_ms) is not int:
            raise ValueError("now_ms must be an integer")
        if prev_book is not None and _market_of(prev_book) != _market_of(book):
            # R3#6: another market's book would fill, lapse and baseline this market's orders.
            raise ValueError("paper advance needs two books of the same market")
        updates = []
        valid, usable = _book_state(book)
        prev_valid, prev_usable = _book_state(prev_book) if prev_book is not None else (False, False)
        prev_ms = _book_time_ms(prev_book) if prev_valid else None
        version_changed = valid and prev_valid and prev_book["version"] != book["version"]
        suspension_seen = valid and prev_valid and "SUSPENDED" in (book["status"], prev_book["status"])
        newly_removed = valid and prev_valid and _newly_removed(prev_book, book)
        runners_changed = valid and prev_valid and set(book["runners"]) != set(prev_book["runners"])
        # A version change across a suspension, or with a runner newly removed (or the runner set
        # changed) when the suspension itself was not seen (conflated data, a recording gap), is a
        # material change: unmatched bets lapse. Any other OPEN-to-OPEN version change blocks fills.
        lapse_reason = None
        if version_changed and suspension_seen:
            lapse_reason = "lapsed_version_change_while_suspended"
        elif newly_removed or (version_changed and runners_changed):
            lapse_reason = "lapsed_version_change_runner_removed"
        divisor = _dec(self.params["traded_volume_divisor"])
        hwm = self._traded_hwm.setdefault(book["market_id"], {}) if valid else {}
        # Traded volume at one (selection, price) is shared by our orders there: each later order
        # (in activation order) sees only what the earlier ones did not fill.
        given = {}
        unsequenced = float("inf")
        ordered = sorted(open_orders, key=lambda o: (self._activation.get(o["customer_order_ref"], unsequenced),
                                                     o.get("submitted_at") or "", o["customer_order_ref"]))
        for order in ordered:
            if order["state"] != "EXECUTABLE" or order["size_remaining_p"] <= 0:
                continue
            ref, flags = order["customer_order_ref"], []
            if not valid:
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["book_invalid_no_fills"]})
                continue
            runner = book["runners"].get(str(order["selection_id"]))
            if book["inplay"] or runner is None or runner["status"] in REMOVED_STATUSES or lapse_reason:
                reason = "lapsed_inplay" if book["inplay"] else ("lapsed_runner_removed" if runner is None or
                                                                 runner["status"] in REMOVED_STATUSES
                                                                 else lapse_reason)
                updates.append(self._lapse(order, [reason]))
                continue
            if not usable:
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["book_invalid_no_fills"]})
                continue
            if gap:
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["passive_fill_blocked_recording_gap"]})
                continue
            if book["status"] != "OPEN" or not prev_usable or prev_book["status"] != "OPEN" or prev_book["inplay"]:
                continue
            if version_changed:
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["passive_fill_blocked_version_change"]})
                continue
            activated = self._activated_ms.get(ref)
            if activated is None or prev_ms < activated:
                # The interval starts before the order was active (or nobody knows when it was): its
                # volume may have traded before the order existed, so none of it counts.
                if activated is None:
                    self._activated_ms[ref] = _book_time_ms(book)
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["passive_fill_skipped_pre_activation_interval"]})
                continue
            prev_runner = prev_book["runners"].get(str(order["selection_id"]))
            try:
                now_traded = _traded(runner)
                prev_traded = _traded(prev_runner) if prev_runner is not None else None
            except (ValueError, ArithmeticError):
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["traded_volume_invalid_no_passive_fill"]})
                continue
            if now_traded is None or prev_traded is None:
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["passive_fill_unavailable_no_trd"]})
                continue
            if prev_traded and not any(now_traded.values()):
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["settlement_zeroing_ignored"]})
                continue
            group = (int(order["selection_id"]), order["price_h"])
            now_level = now_traded.get(order["price_h"], Decimal(0))
            prev_level = prev_traded.get(order["price_h"], Decimal(0))
            if now_level < prev_level:
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["negative_traded_delta_ignored"]})
                continue
            # Volume already seen once (a baseline that moved back, a dip and recovery) never counts again.
            delta = now_level - max(prev_level, hwm.get(group, Decimal(0)))
            if delta <= 0:
                if now_level > prev_level:
                    updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                    "flags": ["traded_volume_already_counted_ignored"]})
                continue
            queue = self._queues.get(ref)
            if queue is None:
                updates.append({"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {},
                                "flags": ["passive_fill_blocked_queue_unknown"]})
                continue
            consumed = int((delta * 100 / divisor).to_integral_value(rounding=ROUND_FLOOR))
            consumed = max(0, consumed - given.get(group, 0))
            if consumed <= queue:
                self._queues[ref] = queue - consumed
                continue
            fill = min(order["size_remaining_p"], consumed - queue)
            self._queues[ref] = 0
            given[group] = given.get(group, 0) + fill
            updates.append(self._fill(order, fill, flags))
        if valid:
            self._raise_traded_hwm(hwm, prev_book if prev_valid else None, book)
        updates.sort(key=lambda update: update["customer_order_ref"])
        return updates

    @staticmethod
    def _raise_traded_hwm(hwm, prev_book, book):
        for snapshot in (prev_book, book):
            if snapshot is None:
                continue
            for selection, runner in snapshot["runners"].items():
                try:
                    traded = _traded(runner)
                except (ValueError, ArithmeticError):
                    continue
                for price_h, volume in (traded or {}).items():
                    key = (int(selection), price_h)
                    if volume > hwm.get(key, Decimal(0)):
                        hwm[key] = volume

    def _fill(self, order, fill, flags):
        matched = order["size_matched_p"] + fill
        previous = order["average_price_matched_h"]
        fills = [(fill, order["price_h"])] + ([(order["size_matched_p"], previous)] if order["size_matched_p"] else [])
        remaining = order["size_remaining_p"] - fill
        if remaining == 0:
            self._forget(order["customer_order_ref"])
        return {"customer_order_ref": order["customer_order_ref"],
                "state": "EXECUTION_COMPLETE" if remaining == 0 else "EXECUTABLE",
                "fields": {"size_matched_p": matched, "average_price_matched_h": _average(fills, order["side"]),
                           "size_remaining_p": remaining},
                "flags": list(flags) + ["passive_fill"]}

    def _lapse(self, order, flags):
        self._forget(order["customer_order_ref"])
        lapsed = order["size_lapsed_p"] + order["size_remaining_p"]
        state = "EXECUTION_COMPLETE" if order["size_matched_p"] > 0 else "LAPSED"
        return {"customer_order_ref": order["customer_order_ref"], "state": state,
                "fields": {"size_lapsed_p": lapsed, "size_remaining_p": 0}, "flags": list(flags)}

    def settle(self, orders, *, market_definition):
        """Settle one CLOSED market from runner WINNER/LOSER statuses (book snapshot or stream definition)."""
        runners, closed, market_id, winners_expected = _runner_results(market_definition)
        if not closed:
            raise ValueError("settlement needs a CLOSED market")
        matched = [o for o in orders if o["size_matched_p"] > 0]
        realized = market_realized(matched, runners, commission_rate=self.params["commission_rate"])
        if winners_expected not in (None, 1) and realized["excluded_reason"] is None:
            realized = {"gross_pence": None, "commission_pence": None, "net_pence": None,
                        "excluded_reason": "number_of_winners_not_1"}
        settled = {}
        for order in sorted(matched, key=lambda o: o["customer_order_ref"]):
            result = runners.get(int(order["selection_id"]))
            if result in REMOVED_STATUSES:
                settled[order["customer_order_ref"]] = {"state": "VOIDED", "settled_profit_own_p": None}
                continue
            profit = None
            if (result in ("WINNER", "LOSER") and realized["excluded_reason"] is None
                    and order["average_price_matched_h"] is not None):
                profit = order_pnl_pence(order["side"], order["average_price_matched_h"], order["size_matched_p"],
                                         result)
            settled[order["customer_order_ref"]] = {"state": "SETTLED", "settled_profit_own_p": profit}
        return {"market_id": market_id, "excluded_reason": realized["excluded_reason"],
                "gross_pence": realized["gross_pence"], "commission_pence": realized["commission_pence"],
                "net_pence": realized["net_pence"], "commission_source": self.params["commission_source"],
                "orders": settled}


def _market_of(book):
    return book.get("market_id") if isinstance(book, dict) else None


def _book_state(book):
    """(schema-valid, usable): usable also needs no invalid_reason."""
    try:
        validate_book_snapshot(book)
    except (ValueError, TypeError):
        return False, False
    return True, book["invalid_reason"] is None


def _newly_removed(prev_book, book):
    """True when a runner of book is REMOVED that prev_book did not show as removed (or did not have)."""
    for selection, runner in book["runners"].items():
        if runner["status"] in REMOVED_STATUSES:
            before = prev_book["runners"].get(selection)
            if before is None or before["status"] not in REMOVED_STATUSES:
                return True
    return False


def _runner_results(definition):
    """({selection_id: status}, closed, market_id, number_of_winners) from a snapshot or stream definition."""
    if not isinstance(definition, dict):
        raise ValueError("market_definition must be an object")
    runners = definition.get("runners")
    results = {}
    if isinstance(runners, dict):
        for selection, runner in runners.items():
            results[int(selection)] = runner["status"]
        winners_expected = definition.get("number_of_winners")
        market_id = definition.get("market_id")
    elif isinstance(runners, list):
        for runner in runners:
            results[int(runner["id"])] = runner["status"]
        winners_expected = definition.get("numberOfWinners")
        market_id = definition.get("id") or definition.get("market_id")
    else:
        raise ValueError("market_definition needs runners")
    return results, definition.get("status") == "CLOSED", market_id, winners_expected


# --- offline replay --------------------------------------------------------------------------


class _SimClock:
    """Simulation clock for the replay journal: time is publisher time, set by the replay loop."""

    def __init__(self, start_ms):
        self.ns = start_ms * 1_000_000

    def set_ms(self, ms):
        self.ns = max(self.ns, ms * 1_000_000)

    def time_ns(self):
        return self.ns

    def monotonic_ns(self):
        return self.ns

    def sleep(self, seconds):
        raise RuntimeError("the replay clock never sleeps")

    def utc_now(self):
        return utc_from_ns(self.ns)


def _load_orders(orders_path):
    data = strict_json_loads(Path(orders_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "orders"} or data["schema_version"] != 1:
        raise ValueError("orders file must be {schema_version: 1, orders: [...]}")
    if not isinstance(data["orders"], list) or not data["orders"]:
        raise ValueError("orders file needs at least one order")
    parsed = []
    for index, item in enumerate(data["orders"]):
        if not isinstance(item, dict) or set(item) != ORDER_FILE_KEYS:
            raise ValueError(f"order {index} keys must be exactly {sorted(ORDER_FILE_KEYS)}")
        if type(item["submit_pt"]) is not int or item["submit_pt"] < 0:
            raise ValueError(f"order {index} submit_pt must be an integer")
        if not isinstance(item["market_id"], str) or not MARKET_ID_RE.fullmatch(item["market_id"]):
            raise ValueError(f"order {index} market_id invalid")
        if type(item["selection_id"]) is not int or item["selection_id"] <= 0:
            raise ValueError(f"order {index} selection_id invalid")
        if item["side"] not in ("BACK", "LAY"):
            raise ValueError(f"order {index} side invalid")
        price_h, size_p = parse_price(item["price"]), parse_money(item["size"])
        if size_p < MIN_BET_PENCE_GBP:
            raise ValueError(f"order {index} stakes below 1.00 are unsupported")
        if not profit_ratio_ok(size_p, price_h):
            raise ValueError(f"order {index} fails the profit ratio rule")
        parsed.append({"index": index, "submit_pt": item["submit_pt"], "market_id": item["market_id"],
                       "selection_id": item["selection_id"], "side": item["side"], "price_h": price_h,
                       "size_p": size_p})
    return parsed


RECEIPT_ROLES = (("receipt_json", "receipt.json"), ("receipt_partial_json", "receipt.partial.json"))


def _dependencies(paths):
    """(dependencies, receipts) for the recordings (amendment A3, C4).

    dependencies: per recording, the presence and sha256 of every other file the replay reads for it
    (receipt.json, receipt.partial.json, the receipt sidecar) and which receipt it uses (the first
    present, as the recorder leaves receipt.json beside a closed session and receipt.partial.json
    beside one that never closed). receipts: that receipt per recording, parsed from the very bytes
    hashed, or None."""
    from .book import recv_sidecar_name
    dependencies, receipts = [], []
    for path in paths:
        entry, used, parsed = {"recording": path.name}, None, None
        for role, name in RECEIPT_ROLES:
            file = path.parent / name
            data = file.read_bytes() if file.is_file() else None
            entry[role] = {"present": data is not None, "sha256": bytes_sha256(data) if data is not None else None}
            if data is not None and used is None:
                used, parsed = name, strict_json_loads(data.decode("utf-8"))
        entry["receipt_used"] = used
        sidecar = recv_sidecar_name(path)
        present = sidecar is not None and (path.parent / sidecar).is_file()
        entry["recv_sidecar"] = {"name": sidecar, "present": present,
                                 "sha256": file_sha256(path.parent / sidecar) if present else None}
        dependencies.append(entry)
        receipts.append(parsed)
    return dependencies, receipts


def _infer_key_type(receipts):
    found = {value["key_type"] for value in receipts if isinstance(value, dict)
             and value.get("key_type") in ("delayed", "live", "historical", "synthetic")}
    if len(found) > 1:
        raise ValueError("recordings disagree on key_type")
    return (found.pop(), False) if found else ("historical", True)


def _receipt_windows(paths, receipts):
    """(windows, status): the recorded windows [(first_pt, last_pt, (market_id, ...))] of every recording's
    segment from the receipt that lists it (amendment A3, R3#7 and R3#8), sorted.

    A listed segment without an int first_pt..last_pt (lines 0: the connection dropped before the
    image) is an empty window: nothing was observed in it. A listed segment whose market_ids is not a
    list covers no market. A recording is unknown only when no receipt lists it; then windows is
    None and status says why ("unknown_no_receipt", or "unknown_segment_not_in_receipt" when some
    receipt exists but does not list it). Known and unknown recordings together are refused
    (ValueError recordings_mix_known_and_unknown_gap_windows): the unknown ones would switch the gap
    guard off for the known ones. Residual: a market a segment stopped carrying before it ended (an
    explicit-mode refresh) still counts as covered to the segment's last_pt; max_snapshot_age_s bounds
    how old its activation book can be."""
    windows, known, unknown = [], 0, set()
    for path, receipt in zip(paths, receipts):
        segments = receipt.get("segments") if isinstance(receipt, dict) else None
        listed = [seg for seg in (segments if isinstance(segments, list) else [])
                  if isinstance(seg, dict) and seg.get("path") == path.name]
        if not listed:
            unknown.add("unknown_no_receipt" if receipt is None else "unknown_segment_not_in_receipt")
            continue
        known += 1
        segment = listed[-1]
        first, last, ids = segment.get("first_pt"), segment.get("last_pt"), segment.get("market_ids")
        if type(first) is int and type(last) is int and first <= last:
            ids = tuple(sorted(m for m in ids if isinstance(m, str))) if isinstance(ids, list) else ()
            windows.append((first, last, ids))
    if known and unknown:
        raise ValueError("recordings_mix_known_and_unknown_gap_windows")
    if unknown:
        return None, "unknown_segment_not_in_receipt" if "unknown_segment_not_in_receipt" in unknown \
            else "unknown_no_receipt"
    return sorted(windows), "known"


def _same_window(windows, market_id, *pts):
    """True when one recorded window that lists market_id holds every pt (the market was observed
    throughout); False in a gap, across two windows, or in a window of other markets only."""
    return any(market_id in ids and all(first <= pt <= last for pt in pts) for first, last, ids in windows)


def _default_reader(path, *, key_type):
    try:
        from .book import snapshots_from_recording
    except ImportError:
        raise ValueError("betfair.book.snapshots_from_recording is unavailable") from None
    return snapshots_from_recording(path, key_type=key_type)


def _det_rng(seed):
    def rng(nbytes):
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:2 * nbytes]
    return rng


def _journal_order(journal, sim, order, refs):
    ref, cref = refs
    instruction = {"selection_id": order["selection_id"], "handicap": 0, "side": order["side"],
                   "price_h": order["price_h"], "size_p": order["size_p"], "persistence_type": "LAPSE"}
    intent_id = f"paper-replay-{order['index']}"
    journal.append("intent", {"order": instruction, "customer_strategy_ref": "paper-replay", "is_probe": False,
                              "parent_ref": None, "provisional_child": False, "source": "paper_replay"},
                   intent_id=intent_id, customer_order_ref=ref, customer_ref=cref, market_id=order["market_id"],
                   state_after="RECORDED")
    journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": None,
                              "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                   intent_id=intent_id, market_id=order["market_id"])
    return intent_id


def _simulate(snapshots, orders, params, journal, sim, orders_sha, windows=None):
    """Run every market's timeline; returns (per-order results, per-market settlements).

    windows (from the receipts; None when unknown) are the recorded intervals with the markets each
    one carried. An order on M is refused (paper_book_in_recording_gap) unless one window listing M
    holds both its activation time and the timeline pt of its activation book, because otherwise M's
    book at activation was never observed; an interval between two books of M gives no passive fill
    unless one window listing M holds both."""
    exchange = PaperExchange(params)
    by_market = {}
    for pt, snapshot in snapshots:
        by_market.setdefault(snapshot["market_id"], []).append((pt, snapshot))
    results, markets = {}, {}
    pending = {}
    for order in orders:
        ref = make_customer_order_ref(order["submit_pt"] * 1_000_000, rng=_det_rng(f"{orders_sha}:{order['index']}:o"))
        cref = make_customer_ref(order["submit_pt"] * 1_000_000, rng=_det_rng(f"{orders_sha}:{order['index']}:c"))
        record = dict(order, customer_order_ref=ref, customer_ref=cref)
        pending.setdefault(order["market_id"], []).append(record)
        results[ref] = {"index": order["index"], "customer_order_ref": ref, "market_id": order["market_id"],
                        "selection_id": order["selection_id"], "side": order["side"], "price_h": order["price_h"],
                        "size_p": order["size_p"], "submit_pt": order["submit_pt"], "activation_pt": None,
                        "activation_book_pt": None, "final_state": "RECORDED", "size_matched_p": 0,
                        "average_price_matched_h": None, "size_lapsed_p": 0, "refusal": None, "flags": []}
    latency = params["submit_latency_ms"]

    def activate(records, book, book_pt):
        for record in records:
            ref = record["customer_order_ref"]
            sim.set_ms(record["submit_pt"])
            _journal_order(journal, sim, record, (ref, record["customer_ref"]))
            seen = [record["submit_pt"] + latency] + ([book_pt] if book is not None else [])
            in_gap = windows is not None and not _same_window(windows, record["market_id"], *seen)
            report = exchange.place([record], book=book, now_ms=record["submit_pt"],
                                    refusal="paper_book_in_recording_gap" if in_gap else None)
            sim.set_ms(record["submit_pt"] + latency)
            moves = classify_place_report(report, sent_refs=[ref])
            state, fields = moves[ref]
            journal.append("report", {"action": "place", "report": report, "transitions": [
                {"customer_order_ref": ref, "state": state, "reason": "paper_place_report", "fields": fields}]},
                market_id=record["market_id"])
            result = results[ref]
            result["activation_pt"] = record["submit_pt"] + latency
            result["activation_book_pt"] = _book_time_ms(book) if book is not None else None
            if state in ("REFUSED", "FAILED"):
                result["refusal"] = fields.get("error_code")

    for market_id in sorted(set(pending) | set(by_market)):
        timeline = sorted(by_market.get(market_id, []), key=lambda item: item[0])
        waiting = sorted(pending.get(market_id, []), key=lambda r: (r["submit_pt"], r["index"]))
        prev, prev_pt, closed_book = None, None, None
        for position, (pt, snapshot) in enumerate(timeline):
            due = [r for r in waiting if r["submit_pt"] + latency < pt]
            waiting = [r for r in waiting if r["submit_pt"] + latency >= pt]
            activate(due, prev, prev_pt)
            sim.set_ms(pt)
            if prev is not None:
                folded = journal.fold(now_ns=sim.ns, submitting_as_unknown=False)["orders"]
                open_orders = [o for o in folded.values() if o["market_id"] == market_id]
                gap = windows is not None and not _same_window(windows, market_id, prev_pt, pt)
                for update in exchange.advance(open_orders, prev_book=prev, book=snapshot, now_ms=pt, gap=gap):
                    ref = update["customer_order_ref"]
                    for flag in update["flags"]:
                        if flag not in results[ref]["flags"]:
                            results[ref]["flags"].append(flag)
                    if update["fields"] or update["state"] != folded[ref]["state"]:
                        journal.append("report", {"action": "place", "paper_advance": True, "transitions": [
                            {"customer_order_ref": ref, "state": update["state"], "reason": "paper_advance",
                             "fields": update["fields"]}]}, market_id=market_id)
            # Orders active exactly at pt use the LAST snapshot with that pt (spec section 13.2), so they wait
            # until every message published at pt has been applied.
            last_at_pt = position + 1 == len(timeline) or timeline[position + 1][0] != pt
            if last_at_pt or snapshot["status"] == "CLOSED":
                due = [r for r in waiting if r["submit_pt"] + latency == pt]
                waiting = [r for r in waiting if r["submit_pt"] + latency != pt]
                activate(due, snapshot, pt)
            prev, prev_pt = snapshot, pt
            if snapshot["status"] == "CLOSED":
                closed_book = snapshot
                break
        if waiting:
            activate(waiting, prev, prev_pt)
        folded = journal.fold(now_ns=sim.ns, submitting_as_unknown=False)["orders"]
        market_orders = [o for o in folded.values() if o["market_id"] == market_id]
        if closed_book is None:
            markets[market_id] = {"status": "not_closed_in_recording"}
            continue
        for order in market_orders:
            if order["state"] == "EXECUTABLE":
                lapse = exchange._lapse(order, ["lapsed_market_closed"])
                journal.append("report", {"action": "place", "paper_advance": True, "transitions": [
                    {"customer_order_ref": order["customer_order_ref"], "state": lapse["state"],
                     "reason": "paper_market_closed", "fields": lapse["fields"]}]}, market_id=market_id)
        folded = journal.fold(now_ns=sim.ns, submitting_as_unknown=False)["orders"]
        market_orders = [o for o in folded.values() if o["market_id"] == market_id]
        settlement = exchange.settle(market_orders, market_definition=closed_book)
        transitions = [{"customer_order_ref": ref, "state": item["state"], "reason": "paper_settlement",
                        "fields": {"settled_profit_own_p": item["settled_profit_own_p"]}}
                       for ref, item in settlement["orders"].items()]
        if transitions:
            journal.append("settle", {"commission_p": settlement["commission_pence"] or 0,
                                      "transitions": transitions}, market_id=market_id)
        markets[market_id] = dict(settlement, status="excluded" if settlement["excluded_reason"] else "settled")
    final = journal.fold(now_ns=sim.ns, submitting_as_unknown=False)["orders"]
    for ref, result in results.items():
        order = final.get(ref)
        if order is not None:
            result.update(final_state=order["state"], size_matched_p=order["size_matched_p"],
                          average_price_matched_h=order["average_price_matched_h"],
                          size_lapsed_p=order["size_lapsed_p"])
    return [results[ref] for ref in sorted(results, key=lambda r: results[r]["index"])], markets


def _summary(report):
    totals = report["simulated_execution"]["totals"]
    lines = ["# Paper replay (paper-v1)", "", "Paper simulation, not execution evidence. No order was sent anywhere.",
             "", "| Item | Value |", "| --- | --- |",
             f"| Orders | {len(report['simulated_execution']['orders'])} |",
             f"| Markets settled | {totals['markets_settled']} |",
             f"| Markets excluded | {totals['markets_excluded']} |",
             f"| Markets not closed in recording | {totals['markets_unsettled']} |",
             f"| Simulated gross (pence) | {totals['gross_pence']} |",
             f"| Simulated commission (pence) | {totals['commission_pence']} |",
             f"| Simulated net (pence) | {totals['net_pence']} |",
             f"| Commission source | {report['commission_source']} |", "",
             *[f"- {item}" for item in report["limitations"]], ""]
    return "\n".join(lines)


def run_paper_replay(recording_paths, orders_path, params, out_dir, *, repo_root, snapshot_reader=None,
                     key_type=None, clock=SYSTEM_CLOCK):
    """Deterministic offline replay of an orders file over recorded segments (spec section 12).

    Writes out_dir/{report.json, summary.md, journal.sqlite} (out_dir must not exist) and appends
    one row with exactly LEDGER_KEYS to out_dir.parent/run-ledger.jsonl. snapshot_reader(path,
    key_type=...) yields (pt, snapshot) and defaults to book.snapshots_from_recording.

    The report's inputs and the ledger's source_sha256 cover everything that decides the result
    besides code and params (amendment A3, C4): the recordings, the orders, every receipt and receipt
    sidecar beside a recording (role, presence, sha256, the receipt used) and what was resolved from
    them (key_type and its source, the recorded windows and their status).
    """
    out = Path(out_dir)
    if out.exists():
        raise ValueError("output already exists; choose a new run directory")
    params = _check_params(params)
    paths = [Path(p) for p in ([recording_paths] if isinstance(recording_paths, (str, Path)) else recording_paths)]
    if not paths:
        raise ValueError("at least one recording is required")
    for path in paths:
        if not path.is_file():
            raise ValueError("recording file missing")
    orders = _load_orders(orders_path)
    orders_sha = file_sha256(orders_path)
    dependencies, receipts = _dependencies(paths)
    inferred, key_type_source = False, "explicit"
    if key_type is None:
        key_type, inferred = _infer_key_type(receipts)
        key_type_source = "inferred_historical" if inferred else "receipt"
    reader = snapshot_reader or _default_reader
    windows, windows_status = _receipt_windows(paths, receipts)
    inputs = [{"name": p.name, "sha256": file_sha256(p), "bytes": p.stat().st_size} for p in paths]
    resolved = {"key_type": key_type, "key_type_source": key_type_source, "windows_status": windows_status,
                "windows": None if windows is None else [[first, last, list(ids)] for first, last, ids in windows]}
    snapshots = []
    for file_index, path in enumerate(paths):
        for position, (pt, snapshot) in enumerate(reader(path, key_type=key_type)):
            validate_book_snapshot(snapshot)
            snapshots.append((pt, file_index, position, snapshot))
    snapshots.sort(key=lambda item: (item[0], item[1], item[2]))
    ordered = [(pt, snapshot) for pt, _, _, snapshot in snapshots]
    delayed_seen = key_type == "delayed" or any(s["is_market_data_delayed"] for _, s in ordered)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / f".{out.name}.tmp-{secrets.token_hex(4)}"
    work.mkdir(mode=0o700)
    try:
        start_ms = min([o["submit_pt"] for o in orders] + [pt for pt, _ in ordered])
        sim = _SimClock(start_ms)
        journal = OrderJournal(work / "journal.sqlite", mode="paper", clock=sim)
        try:
            order_results, markets = _simulate(ordered, orders, params, journal, sim, orders_sha, windows)
            verification = journal.verify()
        finally:
            journal.close()
        settled = [m for m in markets.values() if m.get("status") == "settled"]
        divisor = params["traded_volume_divisor"]
        limitations = [
            "Paper simulation of fills from displayed books and traded volume; not execution evidence.",
            "pt is Betfair publisher time; queue positions are estimated from displayed sizes only.",
            f"Traded volume divisor {divisor} (2 is the PRO historical basis); the live API basis is unverified (T8).",
            "Commission rate and source are configured assumptions, echoed in commission_source.",
        ]
        if divisor < 2:
            limitations.append("traded_volume_divisor_below_2_less_conservative_than_pro_basis")
        if windows is None:
            limitations.append("recording_gaps_" + windows_status)
        else:
            limitations.append("Recording gaps are not forward-filled: an order is refused "
                               "(paper_book_in_recording_gap) unless one receipt segment listing its market holds "
                               "both its activation time and its activation book, and no passive fill spans two "
                               "segments or a gap.")
        if delayed_seen:
            limitations.append("delayed_data_paper_fills_unreliable")
        if inferred:
            limitations.append("key_type_inferred_historical_no_receipt")
        report = {
            "schema_version": 1, "status": REPLAY_STATUS, "model": PAPER_MODEL, "params": params,
            "assumptions": list(PAPER_ASSUMPTIONS),
            "inputs": {"recordings": inputs, "orders": {"name": Path(orders_path).name, "sha256": orders_sha,
                                                        "bytes": Path(orders_path).stat().st_size},
                       "dependencies": dependencies, "resolved": resolved},
            "key_type": key_type,
            "data_freshness": {"key_type": key_type, "delayed": delayed_seen,
                               "label": "delayed_data_paper_fills_unreliable" if delayed_seen else "as_recorded"},
            "commission_source": params["commission_source"], "commission_rate": params["commission_rate"],
            "simulated_execution": {
                "orders": order_results, "markets": markets,
                "totals": {"gross_pence": sum(m["gross_pence"] for m in settled),
                           "commission_pence": sum(m["commission_pence"] for m in settled),
                           "net_pence": sum(m["net_pence"] for m in settled),
                           "markets_settled": len(settled),
                           "markets_excluded": sum(1 for m in markets.values() if m.get("status") == "excluded"),
                           "markets_unsettled": sum(1 for m in markets.values()
                                                    if m.get("status") == "not_closed_in_recording")}},
            "economic_result": None, "execution_model": PAPER_MODEL, "limitations": limitations,
            "journal": {"path": "journal.sqlite", "events": verification["events"], "verify": verification["status"]},
            "code_sha256": code_hashes(repo_root), **git_state(repo_root), "python": platform.python_version(),
        }
        atomic_write_json(work / "report.json", report)
        (work / "summary.md").write_text(_summary(report), encoding="utf-8")
        work.rename(out)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    ledger = {"created_at": clock.utc_now(), "experiment_id": "paper-replay:" + orders_sha[:12],
              "source_sha256": canonical_sha256({"recordings": [i["sha256"] for i in inputs], "orders": orders_sha,
                                                 "dependencies": dependencies, "resolved": resolved}),
              "config_sha256": canonical_sha256(params), "code_sha256": report["code_sha256"],
              "report": str((out / "report.json").resolve()), "report_sha256": file_sha256(out / "report.json"),
              "status": REPLAY_STATUS}
    if tuple(sorted(ledger)) != tuple(sorted(LEDGER_KEYS)):
        raise RuntimeError("ledger row keys drifted")
    append_jsonl(out.parent / "run-ledger.jsonl", ledger)
    return report
