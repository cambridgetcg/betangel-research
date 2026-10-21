"""OrderGateway: the single choke point between an order decision and Betfair (spec section 9.3).

This is the only module that names the Betfair order methods. Paper mode (the default) holds no
JSON-RPC client at all, so it structurally cannot reach an order endpoint; its fills come from the
deterministic paper-v1 model. Live mode needs a gateway opened for one command after every arming
check; then, for each order: the operator retypes it at the terminal, the gateway fetches its own
fresh book with the live key (a caller's book is refused), the risk guard decides, the guard mints
a five-second capability bound to the sha256 of the exact request body, the submit is journaled
(F_FULLFSYNC) before any byte is sent, the kill file is re-checked, SIGINT/SIGTERM/SIGHUP are
blocked around the send and the report journal, and the order is sent exactly once. A place request is
checked once more at its first byte (amendment A3, C2 and D4): the transport refuses to write it
after an absolute send-by time (the book's freshness, capped by the capability's expiry), once the
KILL file exists, or when the book has gone stale on either clock. Cancels are never gated, and
their capability is minted right before the send. An unknown outcome is reconciled, never resent.
The automatic HALTs an outcome triggers ride in the same report (or risk_decision) event as its
moves (R3#13), and a live report whose moves the fold must skip halts too (report_moves_skipped). Each
open() refuses the RECORDED orders an earlier process left (R3#14) and moves its mid-request orders
(live: UNKNOWN; paper: resolved exactly), both as compare-and-appends on the head they were folded
at. A place or live-probe whose open() found an earlier UNKNOWN order live is refused
(earlier_unknown_resolved_at_open): the operator typed it while that outcome was unknown.
Programmatic use (source="api") is paper-only. Paper fills and the paper exchange state ride in one
journal event (payload "paper_state", R3#12); open() rebuilds the paper exchange from the latest one.
A paper market whose CLOSED book has been stepped stays closed (re-review R2): its bulky state leaves
the paper state once its orders are final, but a compact marker stays, so no later order or book of it
is ever used (bounded: see PAPER_CLOSED_MARKERS_MAX). A CLOSED book that place or replace refuses, or that
paper-step ignores, closes its market the same way, journaled in a note before the refusal. Only a paper
gateway still holding its paper lock journals a paper state (_require_paper_lock): one whose .paper.lock
was unlinked or replaced would overwrite the state of the gateway holding the new lock with its stale copy.

v1 live surface (amendment A3, D1-D3): place (LIMIT, LAPSE), full cancels of our own bets, kill, the
live probe (place then cancel), reconciliation of unknown outcomes under the live evidence rule
(reconcile.py) and settle-market, by which the operator settles one CLOSED market by hand. Live
replace and live partial cancels were removed (refused before anything is read, journaled or sent);
nothing settles a live order automatically. Paper keeps replace, reductions and its settlement model.

The kill (amendment A3, D4) never takes or waits for .gateway.lock: it engages the KILL file, sweeps
our bets by our strategy refs and our journal's betIds, re-sweeps at 20 s and 35 s after the KILL
file and journals no order moves. A lock holder is never waited for or signalled: a place or
live-probe holder only stops reconciling once the KILL file exists. Nor does the kill wait for the
journal: its connection has a short busy timeout, and a kill event it cannot write (the journal
locked by another writer, or refused) is skipped, with every later one, while every cancel is still
sent; the kill then ends kill_incomplete. This is the one exception to "the submit is journaled
before any byte": the kill's cancel submit moves no order (no CANCEL_PENDING for a crash to
resolve), so the event is a record, and waiting for it could leave our bets live. A kill also ends
kill_incomplete when the account it logged into is not the journal's (read only; it writes no
registry), and whatever reading the journal raises (a row that is not JSON, a row SQLite cannot
decode, a file that cannot be opened, held as journal.UnreadableJournal) makes it untrusted, never
stops the sweep.

Every Keychain anchor write goes through credentials.publish_anchor, which serializes publications under
one per-profile lock, reads the journal head and the Keychain anchor inside it and writes only a head that
extends that anchor, so the anchor never moves backwards (re-review R1). The kill publishes only in close(),
after its sweep, waiting at most killswitch.JOURNAL_BUSY_TIMEOUT_S for another publisher; a publication it
cannot make only adds a warning, never stops or delays a cancel. Every other gateway wait for another
publisher is that short too, except the open() of a RISK_COMMANDS command, which refuses when it cannot
publish (_anchor_wait_s).
"""

import contextlib
import os
import secrets
import signal
import sqlite3

from .arming import (anchor_failures, checks_for, combine, dynamic_arming, register_account, static_arming,
                     token_matches)
from .config import config_sha256, money_pence
from .credentials import ANCHOR_PUBLISH_WAIT_S, anchor_read, publish_anchor
from .errors import (ApiError, BetfairError, ConfigError, CredentialError, Halted, JournalError, NotArmed,
                     OrderRefused, SecretLeak, classify, safe_code)
from .filters import MARKET_ID_RE
from .constants import MAX_IDS_PER_REQUEST
from .journal import fold_events, validate_paper_state
from .killswitch import JOURNAL_BUSY_TIMEOUT_S, engage, sweep
from .journal import settled_worst_loss_pence
from .lifecycle import (HALT_CATEGORIES, TRANSITIONS, classify_cancel_report, classify_place_report,
                        classify_replace_report, outcome_from_exception, report_transactions)
from .paper import PaperExchange, book_time_ms
from .paths import file_lock, kill_engaged
from .presence import order_echo_line, probe_echo_line
from .receipts import (SYSTEM_CLOCK, atomic_write_json, canonical_sha256, parse_utc_ns, strict_json_loads,
                       utc_compact_from_ns, utc_from_ns)
from .reconcile import average_price_h, pence, reconcile_unknown, startup_reconcile
from .risk import STATUS_EXPOSURE_BASIS, exposure_by_market
from .schemas import (TERMINAL_STATES, UNRESOLVED_STATES, validate_book_snapshot, validate_cancel_report,
                      validate_instruction_v1, validate_place_report)
from .secret import scrub
from .ticks import wire_price, wire_size
from .validate import make_customer_order_ref, make_customer_ref

PLACE_METHOD = "SportsAPING/v1.0/placeOrders"
CANCEL_METHOD = "SportsAPING/v1.0/cancelOrders"
SOURCES = ("cli_manual", "api")
LIVE_BANNER = "MODE=LIVE: this sends a real order to Betfair with real money."
ACCOUNT_WIDE_BANNER = ("MODE=LIVE: this cancels every unmatched bet on the Betfair account, Bet Angel's included; "
                       "matched bets cannot be undone.")
ACCOUNT_WIDE_PHRASE = "CANCEL ALL UNMATCHED ON ACCOUNT INCLUDING BET ANGEL STOPS"
RESUME_PHRASE = "RESUME LIVE"
PROBE_SELECTION_PRICE_H = 101
PROBE_SIZE_P = 100
CONSECUTIVE_UNKNOWN_HALT = 3
OPEN_RECONCILE_BUDGET_S = 120
EXECUTION_STATUSES = ("SUCCESS", "FAILURE", "PROCESSED_WITH_ERRORS", "TIMEOUT")
INSTRUCTION_STATUSES = ("SUCCESS", "FAILURE", "TIMEOUT")
# Commands whose login is an emergency login (skips the successful-login cap, never a ban or the failure cap).
EMERGENCY_COMMANDS = frozenset({"cancel", "reconcile", "reconcile-accept", "kill", "kill-account"})
# Commands whose open() reads today's exchange orders (spec section 9.9). kill and kill-account skip it:
# their sweep reads our bets itself and must not wait on anything else (a documented deviation).
# settle-market skips it too: it books the worst case whatever the exchange shows (amendment A3, D3).
STARTUP_RECONCILE_COMMANDS = frozenset({"place", "live-probe", "reconcile", "reconcile-accept", "cancel"})
# Commands whose open() also resolves pending UNKNOWNs before anything is sent (spec section 9.3 step 4). A
# gateway open for one of them stops reconciling once a KILL file exists (A7 refused one at open, so any KILL
# is new); the orders stay UNKNOWN at their worst case and the kill's sweep is not slowed (amendment A3, D4).
RESOLVE_ON_OPEN_COMMANDS = frozenset({"place", "live-probe"})
# Commands that can add risk or remove counted exposure: open() refuses unless the Keychain anchor was
# written first (waiting up to credentials.ANCHOR_PUBLISH_WAIT_S for another publisher, _anchor_wait_s).
RISK_COMMANDS = frozenset({"place", "live-probe", "reconcile-accept", "settle-market"})
# Commands whose operator lines are typed before open() takes the global lock (preconfirm).
PRECONFIRM_COMMANDS = frozenset({"place", "live-probe", "reconcile-accept", "settle-market"})
# kill and kill-account open without .gateway.lock (amendment A3, D4).
KILL_COMMANDS = ("kill", "kill-account")
UNTRUSTED_SWEEP_NOTE = "journal_untrusted_sweep_uses_config_strategy_ref"
SETTLE_BASIS = "operator_settled_worst_case"
# Journal states settle-market moves to SETTLED (EXECUTABLE keeps its recorded remainder, booked at worst case).
SETTLEABLE_STATES = ("EXECUTABLE", "EXECUTION_COMPLETE")
_STALE_STATES = ("SUBMITTING", "CANCEL_PENDING", "REPLACE_PENDING")
# place and live-probe refuse once their open() found an earlier UNKNOWN order live (finding: the unresolved-orders
# gate must not lift silently after the operator typed a new order, which may be a resend of the earlier one).
RESOLVED_AT_OPEN_RULE = "earlier_unknown_resolved_at_open"
PAPER_INTERRUPTED = "paper_interrupted_before_report"
# Re-review R2: the paper state's closed-market markers. A market whose CLOSED book the paper state has seen takes
# no later order and no later book but a CLOSED one at or after its close (PAPER_MARKET_CLOSED); the paper replay
# likewise never reads past a market's first CLOSED snapshot. Each marker ({market_id: closed_ms}, about 28 bytes;
# 256 of them about 7.2 KB) rides in every paper report, settle and note, so at most PAPER_CLOSED_MARKERS_MAX are
# kept: evicting one (the earliest close first) raises floor_ms to its close, and any book at or before the floor of
# a market holding no timed book in the paper state is refused (PAPER_CLOSED_FLOOR), so an evicted market's
# pre-close books stay unusable. The marker of a market whose kept book is not CLOSED is never evicted (see
# _evict_paper_closed), so the floor's exemption for a market holding a timed book never reopens a closed one.
PAPER_STATE_SCHEMA = 2
PAPER_CLOSED_MARKERS_MAX = 256
PAPER_MARKET_CLOSED = "paper_market_closed"
PAPER_CLOSED_FLOOR = "paper_book_at_or_before_closed_floor"
# What reading or folding a damaged journal can raise (a row SQLite cannot decode, a payload that is not JSON or
# not an object); an unfoldable journal reads as halted, and on the kill path as untrusted (amendment A3, D4).
_JOURNAL_READ_ERRORS = (BetfairError, sqlite3.Error, TypeError, KeyError, ValueError, ArithmeticError, AttributeError)
_NS_PER_S = 1_000_000_000


# --- report normalisation (Betfair -> the shared spec section 3 shapes) ------------------------------


def _code(value):
    if value is None or value == "":
        return None
    return safe_code(value)


def _bet_id(value):
    if value is None:
        return None, True
    if isinstance(value, str) and value.isdigit() and value.isascii() and 1 <= len(value) <= 20:
        return value, True
    return None, False


def _placed_date(value):
    if not isinstance(value, str):
        return None
    try:
        parse_utc_ns(value)
    except ValueError:
        return None
    return value


def _matched_pence(value):
    """Matched size as pence (None when absent); False when present but unusable."""
    if value is None:
        return None
    amount = pence(value)
    return False if amount is None else amount


def _place_item(report, side, *, ref):
    """One normalised place instruction report, or None when it cannot be trusted at all."""
    if not isinstance(report, dict):
        return None
    status = report.get("status")
    bet_id, ok = _bet_id(report.get("betId"))
    matched = _matched_pence(report.get("sizeMatched"))
    if status not in INSTRUCTION_STATUSES or not ok or matched is False:
        return None
    average = None
    if matched:
        average = average_price_h(report.get("averagePriceMatched"), side) if side in ("BACK", "LAY") else None
    order_status = report.get("orderStatus")
    return {"customer_order_ref": ref, "status": status, "error_code": _code(report.get("errorCode")),
            "order_status": order_status if isinstance(order_status, str) and order_status else None,
            "bet_id": bet_id, "placed_date": _placed_date(report.get("placedDate")),
            "average_price_matched_h": average, "size_matched_p": matched}


def _envelope(result, market_id):
    if not isinstance(result, dict) or result.get("status") not in EXECUTION_STATUSES:
        return None
    if result.get("marketId") not in (None, market_id):
        return None
    reports = result.get("instructionReports")
    if reports is None:
        reports = []
    if not isinstance(reports, list):
        return None
    return {"status": result["status"], "error_code": _code(result.get("errorCode")), "market_id": market_id,
            "instructions": reports, "source": "betfair"}


def normalise_place_report(result, *, market_id, sent):
    """Betfair PlaceExecutionReport -> the normalised place report, or None when unusable.

    sent: [{"customer_order_ref", "side"}] in request order. Each item keeps the echoed
    customerOrderRef (lifecycle pairs by it, else by position). The average matched price is
    rounded against us for the order's side.
    """
    envelope = _envelope(result, market_id)
    if envelope is None:
        return None
    sides = {entry["customer_order_ref"]: entry["side"] for entry in sent}
    items = []
    for position, report in enumerate(envelope["instructions"]):
        instruction = report.get("instruction") if isinstance(report, dict) else None
        echoed = instruction.get("customerOrderRef") if isinstance(instruction, dict) else None
        ref = echoed if isinstance(echoed, str) and echoed else None
        side = sides.get(ref) if ref is not None else (sent[position]["side"] if position < len(sent) else None)
        item = _place_item(report, side, ref=ref)
        if item is None:
            return None
        items.append(item)
    envelope["instructions"] = items
    try:
        return validate_place_report(envelope)
    except ValueError:
        return None


def _cancel_item(report):
    if not isinstance(report, dict) or report.get("status") not in INSTRUCTION_STATUSES:
        return None
    instruction = report.get("instruction") if isinstance(report.get("instruction"), dict) else {}
    bet_id, ok = _bet_id(instruction.get("betId"))
    reduction = instruction.get("sizeReduction")
    reduction_p = None if reduction is None else pence(reduction)
    cancelled = report.get("sizeCancelled")
    cancelled_p = None if cancelled is None else pence(cancelled)
    if not ok or (reduction is not None and not reduction_p):
        return None
    return {"bet_id": bet_id, "size_reduction_p": reduction_p, "status": report["status"],
            "error_code": _code(report.get("errorCode")), "size_cancelled_p": cancelled_p,
            "cancelled_date": _placed_date(report.get("cancelledDate"))}


def normalise_cancel_report(result, *, market_id):
    """Betfair CancelExecutionReport -> the normalised cancel report, or None when unusable."""
    envelope = _envelope(result, market_id)
    if envelope is None:
        return None
    items = [_cancel_item(report) for report in envelope["instructions"]]
    if any(item is None for item in items):
        return None
    envelope["instructions"] = items
    try:
        return validate_cancel_report(envelope)
    except ValueError:
        return None


# --- helpers -----------------------------------------------------------------------------------


# Signals held around a send, and stopped on by the CLI (SIGHUP too: a closed terminal, R3#12/R3#13).
SEND_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextlib.contextmanager
def _signals_blocked():
    """SIGINT, SIGTERM and SIGHUP are held (not lost) from just before the send until the report is journaled."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set(SEND_SIGNALS))
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _intent_id():
    return "int-" + secrets.token_hex(8)


def _instruction_echo(instruction):
    try:
        validate_instruction_v1(instruction)
    except (ValueError, TypeError):
        return None
    return instruction


def _exception_payload(exc):
    return {"type": type(exc).__name__, "code": safe_code(getattr(exc, "code", None) or getattr(exc, "rule", None)
                                                         or type(exc).__name__),
            "category": getattr(exc, "category", None), "phase": getattr(exc, "phase", None),
            "may_have_arrived": getattr(exc, "may_have_arrived", None)}


def _decision_payload(decision):
    return {"decision_id": decision["decision_id"], "approved": decision["approved"], "rule": decision["rule"],
            "action": decision.get("action"), "detail": decision["detail"], "worst_case": decision["worst_case"],
            "evaluated_at": decision["evaluated_at"]}


def _order_view(order):
    return {"customer_order_ref": order["customer_order_ref"], "state": order["state"], "bet_id": order["bet_id"],
            "size_matched": order["size_matched_p"] / 100, "size_remaining": order["size_remaining_p"] / 100,
            "size_cancelled": order["size_cancelled_p"] / 100,
            "average_price_matched": (order["average_price_matched_h"] / 100
                                      if order["average_price_matched_h"] is not None else None),
            "error_code": order["error_code"]}


def _journal_read_code(exc):
    """The note for a journal the kill could not read: the JournalError's code, else journal_unreadable."""
    return safe_code(str(exc)) if isinstance(exc, JournalError) else "journal_unreadable"


def _two_dp(value):
    return f"{value // 100}.{value % 100:02d}"


def _gbp(value):
    return "\u00a3" + _two_dp(value)


def _paper_final(order):
    """A paper order nothing can change any more: terminal, or EXECUTION_COMPLETE with nothing matched."""
    return order["state"] in TERMINAL_STATES or (order["state"] == "EXECUTION_COMPLETE"
                                                 and order["size_matched_p"] == 0)


def _with_halts(payload, reasons):
    """payload with the global HALT reasons that ride in the same event (R3#13), if any."""
    return dict(payload, halts=sorted(set(reasons))) if reasons else payload


class OrderGateway:
    """The only order path. See the module docstring; spec section 2.17 for the interface."""

    def __init__(self, config, *, paths, mode, journal, guard, paper=None, rpc=None, read=None, confirmer=None,
                 clock=SYSTEM_CLOCK, env=None, keychain_runner=None, on_send=None):
        if mode not in ("paper", "live"):
            raise ConfigError("gateway_mode_invalid")
        if getattr(journal, "mode", None) != mode or getattr(guard, "mode", None) != mode:
            raise ConfigError("gateway_journal_or_guard_mode_mismatch")
        if mode == "paper":
            if rpc is not None:
                raise ConfigError("paper_gateway_must_not_hold_rpc")
            if read is not None and getattr(read, "app_key_slot", None) != "delayed":
                raise ConfigError("paper_gateway_read_must_use_delayed_key")
        else:
            if paper is not None:
                raise ConfigError("live_gateway_must_not_hold_paper_exchange")
            if rpc is None or rpc.app_key_slot != "live":
                raise ConfigError("live_gateway_needs_live_key_rpc")
            if read is None or getattr(read, "rpc", None) is not rpc:
                raise ConfigError("live_gateway_read_must_wrap_the_same_rpc")
            if confirmer is None:
                raise ConfigError("live_gateway_needs_confirmer")
        self.config = config
        self.paths = paths
        self.mode = mode
        self.journal = journal
        self.guard = guard
        self.paper = paper
        self.rpc = rpc
        self.read = read
        self.confirmer = confirmer
        self.clock = clock
        # A10 sees the real environment unless the caller hands one in (never an empty default).
        self.env = dict(os.environ) if env is None else env
        self.keychain_runner = keychain_runner
        # on_send(): called by _send right before the signals are masked for an order request (R3#16); the
        # CLI defers SIGINT/SIGTERM/SIGHUP from then on, so the report, step 15 and the JSON still happen.
        self.on_send = on_send
        self.strategy_ref = config["orders"]["customer_strategy_ref"]
        self._stack = None
        self._command = None
        self._static = None
        self._report = None
        self._account_details = None
        self._startup = None
        self._paper_books = {}
        self._paper_closed = {}           # paper: {market_id: closed_ms} of markets stepped CLOSED (re-review R2)
        self._paper_closed_floor_ms = None   # paper: the latest close evicted from _paper_closed, or None
        self._seqs = []
        self._preconfirmed = []           # operator lines typed before open() (live), consumed once each
        self._settle_typed = {}           # settle-market: typed line -> sha256 of what was shown (live)
        # Kill path only: the chain, the anchor check, the fold or the account check failed, so the journal
        # cannot list all our bets; the sweep uses the config strategy ref only, no anchor is written and the
        # kill is incomplete.
        self._journal_untrusted = False
        # Live kill only: the code of the first kill event the journal could not take (None while every write
        # worked). No later kill event is attempted; the cancels are still sent; the kill is incomplete.
        self._kill_write_failed = None
        self.login_kind = None            # "emergency" or "normal": the login a live open() made
        self._resolved_at_open = []       # place/live-probe: earlier UNKNOWN orders this open() found live
        self._lock = None                 # the HeldLock of the gateway (live) or paper lock while open
        self._last_event = None           # the last event this object appended

    # --- journal plumbing ----------------------------------------------------------------------

    def _append(self, kind, payload, *, expect_head=None, **columns):
        """journal.append, recording the event. A live kill's write is best effort (amendment A3, D4):
        its events move no order, so after the first one that fails (locked journal, refusal, a journal
        that cannot be read at all) none is attempted and None is returned, and the kill goes on sending
        its cancels and ends incomplete. A paper event carrying a paper state is refused unless the paper lock is
        still held (_require_paper_lock), whichever path journals it."""
        if expect_head is not None:
            columns["expect_head"] = expect_head
        if self.mode == "paper" and isinstance(payload, dict) and "paper_state" in payload:
            self._require_paper_lock()
        kill = self.mode == "live" and self._command in KILL_COMMANDS
        if kill and self._kill_write_failed is not None:
            return None
        try:
            event = self.journal.append(kind, payload, **columns)
        except Exception as exc:          # the kill: whatever the write raised, never stop the cancels
            if not kill:
                raise
            self._kill_write_failed = safe_code(str(exc) if isinstance(exc, BetfairError) else type(exc).__name__)
            return None
        self._seqs.append(event["seq"])
        self._last_event = event
        return event

    def _state(self):
        return self.journal.fold(now_ns=self.clock.time_ns())

    def _halt(self, reason, *, market_id=None):
        scope = "market" if market_id is not None else "global"
        return self._append("halt", {"reason": safe_code(reason), "scope": scope}, market_id=market_id)

    def _moves_payload(self, moves, state, reason):
        """Journal transitions for {ref: (state, fields)}; moves the fold would refuse are left out."""
        transitions, skipped = [], []
        for ref in sorted(moves):
            target, fields = moves[ref]
            current = state["orders"].get(ref)
            if current is None or target not in TRANSITIONS.get(current["state"], ()):
                skipped.append(ref)
                continue
            transitions.append({"customer_order_ref": ref, "state": target, "reason": reason, "fields": dict(fields)})
        return transitions, skipped

    def _append_moves(self, kind, payload, moves, *, reason, market_id, intent_id=None):
        """Journal one event carrying moves; if the fold refuses them, journal the event without moves and
        keep every affected order where it was (unresolved orders stay unresolved). A paper report or
        settle also carries the paper exchange state as it is now (R3#12), so a fill and the takes,
        queues and traded baseline that stop it filling twice are one durable write."""
        if self.mode == "paper" and kind in ("report", "settle") and self.paper is not None:
            payload = dict(payload, paper_state=self._paper_state())
        if not moves:
            # Nothing to move needs no fold (the kill sweep also journals over an unfoldable journal).
            return self._append(kind, dict(payload, transitions=[]), market_id=market_id, intent_id=intent_id)
        state = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
        transitions, skipped = self._moves_payload(moves, state, reason)
        body = dict(payload, transitions=transitions)
        if skipped:
            body["skipped_moves"] = skipped
            if self.mode == "live" and kind == "report":
                # An order request's report that cannot move an order (it is no longer where the request left
                # it) is never dropped silently: a global HALT rides in the same event (R3#13).
                body["halts"] = sorted(set(body.get("halts") or ()) | {"report_moves_skipped"})
        try:
            return self._append(kind, body, market_id=market_id, intent_id=intent_id)
        except JournalError as exc:
            body = dict(payload, transitions=[], refused_moves=sorted(moves), fold_refusal=safe_code(str(exc)))
            event = self._append(kind, body, market_id=market_id, intent_id=intent_id)
            if self.mode == "live":
                self._halt("journal_refused_report_moves", market_id=market_id)
            return event

    def _result(self, intent_id, refs, *, decision, refusal=None, extra=None):
        orders = {}
        if refs:
            state = self._fold_or_none() if self._journal_untrusted else self._state()
            orders = state["orders"] if state is not None else {}
        result = {"intent_id": intent_id, "mode": self.mode, "decision": decision, "refusal": refusal,
                  "orders": [_order_view(orders[ref]) for ref in refs if ref in orders],
                  "journal_seq": [self._seqs[0], self._seqs[-1]] if self._seqs else None}
        if extra:
            result.update(extra)
        return result

    # --- open / close --------------------------------------------------------------------------

    def _paper_state(self):
        return {"schema_version": PAPER_STATE_SCHEMA, "queues": self.paper.queue_state(), "books": self._paper_books,
                "exchange": self.paper.export_state(),
                "closed": {"markets": dict(sorted(self._paper_closed.items())),
                           "floor_ms": self._paper_closed_floor_ms}}

    def _load_paper_state(self):
        """The latest journaled paper state (R3#12); profiles/<p>/orders/paper-state.json (written by code
        before R3#12, never written now) only while no journal event carries one. A state written before
        re-review R2 (schema_version 1) gets its closed-market markers from the journal."""
        try:
            data = self.journal.last_paper_state()
            if data is None:
                path = self.paths.orders_dir / "paper-state.json"
                if not path.exists():
                    return {"queues": {}, "books": {}, "exchange": None, "closed": {"markets": {}, "floor_ms": None}}
                data = strict_json_loads(path.read_text(encoding="utf-8"))
                data = validate_paper_state(dict(data, exchange=data.get("exchange") or {}))
            data = validate_paper_state(data)
            if "closed" not in data:
                data = dict(data, closed={"markets": self._legacy_closed_markers(), "floor_ms": None})
            return data
        except (OSError, ValueError, TypeError, AttributeError, KeyError, JournalError):
            raise JournalError("paper_state_unreadable") from None

    def _legacy_closed_markers(self):
        """Closed-market markers for a paper state written before re-review R2 (schema_version 1, no markers).
        Round 4 dropped a closed market's book in the paper-step's closing note (paper_advance_state, the
        market in its market_id column), and a step keeps its market's book otherwise, so a market such a note
        journals no book for was closed there. That CLOSED book's time was not kept: the note's recorded_at
        stands in, which only refuses more (a CLOSED re-step before it is ignored and the floor rises higher)."""
        markets = {}
        for event in self.journal.events():
            payload, market = event["payload"], event["market_id"]
            state = payload.get("paper_state")
            if (event["kind"] == "note" and payload.get("reason") == "paper_advance_state" and market is not None
                    and isinstance(state, dict) and isinstance(state.get("books"), dict)
                    and market not in state["books"]):
                at_ms = parse_utc_ns(event["recorded_at"]) // 1_000_000
                markets[market] = max(at_ms, markets.get(market, at_ms))
        return markets

    def _verify_journal(self):
        verified = self.journal.verify()
        if verified["status"] != "ok":
            raise JournalError("journal_chain_broken")
        return verified

    def _interrupted_move(self, order):
        """The open-time move for an order an earlier process left mid-request (SUBMITTING, CANCEL_PENDING or
        REPLACE_PENDING). Live: UNKNOWN (crash_between_submit_and_report), since the request may have reached
        Betfair. Paper: resolved exactly (paper_interrupted_before_report), since the paper exchange state is
        journaled only with a report (R3#12), so the paper exchange rebuilt at this open never saw the
        interrupted request: a place never happened (FAILED; a provisional replace child REFUSED), and a
        cancel or replace never touched its bet (EXECUTABLE with the recorded remainder)."""
        if self.mode == "live":
            return "UNKNOWN", {}
        if order["state"] == "SUBMITTING":
            return ("REFUSED" if order["provisional_child"] is True else "FAILED"), {"error_code": PAPER_INTERRUPTED}
        return "EXECUTABLE", {"size_remaining_p": order["size_remaining_p"], "error_code": PAPER_INTERRUPTED}

    def _append_open_moves(self, moves, reason, head):
        """One reconcile event with moves computed from the fold taken at head, compare-and-appended on head.
        -> the new head, or None when another writer moved the head first (nothing is journaled)."""
        transitions = [{"customer_order_ref": ref, "state": target, "reason": reason, "fields": dict(fields)}
                       for ref, (target, fields) in sorted(moves.items())]
        try:
            event = self._append("reconcile", {"reason": reason, "transitions": transitions}, expect_head=head)
        except JournalError as exc:
            if "journal_head_moved" not in str(exc):
                raise
            return None
        return {"seq": event["seq"], "last_sha256": event["event_sha256"]}

    def _stale_to_unknown(self):
        """At open, under the lock: what an earlier process left mid-request or unsent. -> (refs moved out of
        a mid-request state, refs moved RECORDED -> REFUSED).

        Mid-request orders move as _interrupted_move says. RECORDED orders are REFUSED (crash_before_submit,
        R3#14): a submit is journaled before any byte is sent, so a RECORDED order from an earlier process was
        never sent; that also ends a provisional paper replace child orphaned before its submit, which would
        otherwise count as adverse exposure for good. Both moves are computed from one fold and journaled as
        compare-and-appends on the head read before that fold, so they apply only to orders still in the
        state they were computed from: another gateway (say one whose .gateway.lock file was replaced under
        this one) that journals a submit meanwhile makes the move refused, never turns a sent order REFUSED.
        A refused move leaves the orders as they are until the next open (a stored SUBMITTING order reads as
        UNKNOWN, and a RECORDED place counts no exposure)."""
        head = self.journal.anchor()
        state = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
        stale = {ref: self._interrupted_move(o) for ref, o in state["orders"].items() if o["state"] in _STALE_STATES}
        orphans = {ref: ("REFUSED", {"error_code": "crash_before_submit"}) for ref, o in state["orders"].items()
                   if o["state"] == "RECORDED"}
        moved = {"stale": [], "orphans": []}
        stale_reason = "crash_between_submit_and_report" if self.mode == "live" else PAPER_INTERRUPTED
        for key, moves, reason in (("stale", stale, stale_reason), ("orphans", orphans, "crash_before_submit")):
            if not moves or head is None:
                continue
            head = self._append_open_moves(moves, reason, head)
            if head is not None:
                moved[key] = sorted(moves)
        return moved["stale"], moved["orphans"]

    def open(self, *, command, confirm_token=None, bet_angel_paused=False):
        """Take the lock and prepare one command; live also logs in and reconciles. -> the arming report."""
        if self._command is not None:
            raise OrderRefused("gateway_already_open")
        checks_for(command)
        if self.mode == "paper":
            self._stack = contextlib.ExitStack()
            try:
                self._lock = self._stack.enter_context(file_lock(self.paths.paper_lock, clock=self.clock))
                self._verify_journal()
                state = self._load_paper_state()
                # Rebuilt at every open: the journal, not this object, holds what other processes did.
                try:
                    self.paper = PaperExchange(self.config["orders"]["paper"], queues=state["queues"],
                                               state=state["exchange"])
                except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                    raise JournalError("paper_state_unreadable") from None
                self._paper_books = dict(state["books"])
                self._paper_closed = dict(state["closed"]["markets"])
                self._paper_closed_floor_ms = state["closed"]["floor_ms"]
                self._evict_paper_closed()
                stale, orphans = self._stale_to_unknown()
            except BaseException:
                self._release()
                raise
            self._command = command
            self._report = {"mode": "paper", "command": command, "armed": True, "failures": [], "checked": [],
                            "interrupted_resolved": stale, "recorded_refused": orphans}
            return dict(self._report)
        return self._open_live(command, confirm_token, bet_angel_paused)

    def _fold_or_none(self):
        """The fold, or None when the journal cannot be read or folded (whatever that raised)."""
        try:
            return self.journal.fold(now_ns=self.clock.time_ns())
        except _JOURNAL_READ_ERRORS:
            return None

    def _keychain_anchor_ok(self):
        """A13 again, under the lock: the Keychain anchor matches the journal, including the event at its seq."""
        try:
            keychain = anchor_read(self.paths.profile, runner=self.keychain_runner)
        except CredentialError:
            return ["journal_anchor_unreadable"]
        return anchor_failures(self.journal.anchor(), keychain, journal=self.journal,
                               journal_state=self._fold_or_none())

    def _write_anchor(self, *, wait_s=ANCHOR_PUBLISH_WAIT_S):
        """Publish the journal's current head as the Keychain anchor (credentials.publish_anchor: serialized
        with every other publisher, the head read inside its lock and written only if it extends the Keychain
        anchor, so neither a slower writer nor a journal rolled back under this session can put back an older
        head). Nothing while the journal is empty, and never over an untrusted journal: the kill path sweeps
        despite one, but must not bless it. Raises CredentialError when it cannot publish."""
        if self._journal_untrusted:
            return False
        published = publish_anchor(self.journal, self.paths, runner=self.keychain_runner, clock=self.clock,
                                   wait_s=wait_s)
        return published is not None

    @staticmethod
    def _anchor_wait_s(command, *, refuses):
        """How long a publication waits for another publisher of the anchor (the kill's close or resume, which
        hold the anchor lock without .gateway.lock; say one stuck in a Keychain dialog or suspended).
        credentials.ANCHOR_PUBLISH_WAIT_S only where a failed publication refuses the command: the open() of a
        RISK_COMMANDS command. Everywhere else (the kill; cancel and reconcile, whose open() only notes the
        failure; every close(), which only reports it) killswitch.JOURNAL_BUSY_TIMEOUT_S: a longer wait gains
        nothing (the anchor lags the head, which A13 accepts, until the next publication), while it would delay
        the cancel this open() is for and hold .gateway.lock, which a second manual cancel waits for only
        order_deadline_s + 5 s before it exits lock_busy with its bet still live."""
        return ANCHOR_PUBLISH_WAIT_S if refuses and command in RISK_COMMANDS else JOURNAL_BUSY_TIMEOUT_S

    def _open_live(self, command, confirm_token, bet_angel_paused):
        if command == "replace":
            raise OrderRefused("live_replace_removed_v1")          # D2: before any check, read, lock or login
        if command in KILL_COMMANDS:
            return self._open_kill(command, confirm_token, bet_angel_paused)
        now_ns = self.clock.time_ns()
        journal_state = self._fold_or_none()
        static = static_arming(self.config, command=command, paths=self.paths, cli_mode="live",
                               confirm_token=confirm_token, bet_angel_paused=bet_angel_paused, env=self.env,
                               confirmer=self.confirmer, keychain_runner=self.keychain_runner,
                               journal_anchor=self.journal.anchor(), now_ns=now_ns,
                               journal_state=journal_state if journal_state is not None else {"halted": True},
                               journal=self.journal)          # an unfoldable journal reads as halted (A7)
        if not static["armed"]:
            raise NotArmed(static["failures"][0], detail={"failures": static["failures"]})
        deadline_s = self.config["http"]["order_deadline_s"] + 5
        self._stack = contextlib.ExitStack()
        session = self.rpc.session
        notes = []
        try:
            self._lock = self._stack.enter_context(file_lock(self.paths.gateway_lock, timeout_s=deadline_s,
                                                             clock=self.clock))
            self._verify_journal()
            failures = self._keychain_anchor_ok()
            if failures:
                raise NotArmed(failures[0], detail={"failures": failures})
            stale, orphans = self._stale_to_unknown()
            emergency = command in EMERGENCY_COMMANDS
            session.login(emergency=emergency, coexistence_ack=bool(bet_angel_paused))
            self.login_kind = "emergency" if emergency else "normal"
            registry_failures = []
            try:
                fingerprint = session.account_fingerprint()
                register_account(self.paths, fingerprint, self.paths.profile)
                self.journal.set_account_fingerprint(fingerprint)
            except (NotArmed, JournalError):
                registry_failures.append("account_registered_to_other_profile")
            except SecretLeak:
                # The leak guard refused the registry write: a named arming failure, never an escape from open().
                registry_failures.append("account_registry_write_refused")
            if command in STARTUP_RECONCILE_COMMANDS:
                if "A16" in checks_for(command):
                    self._account_details = self.read.get_account_details()
                self._startup = self._startup_reconcile()
                if command in RESOLVE_ON_OPEN_COMMANDS:
                    resolved = self._resolve_unknowns(budget_s=OPEN_RECONCILE_BUDGET_S, stop_on_kill=True)
                    self._resolved_at_open = [r["customer_order_ref"] for r in resolved
                                              if r["final_state"] not in UNRESOLVED_STATES]
            journal_state = self._state()
            dynamic = dynamic_arming(self.config, command=command, session=session,
                                     account_details=self._account_details, journal_state=journal_state,
                                     startup=self._startup, book=None, paths=self.paths,
                                     journal_fingerprint=self.journal.account_fingerprint())
            # The Keychain anchor follows the journal before anything is sent: a crash later in this
            # process then leaves an anchor at or below the journal head, never none (spec section 9.9).
            try:
                self._write_anchor(wait_s=self._anchor_wait_s(command, refuses=True))
            except CredentialError:
                if command in RISK_COMMANDS:
                    raise NotArmed("journal_anchor_write_failed") from None
                notes.append("journal_anchor_write_failed")
        except BaseException:
            self._release()
            raise
        report = combine(static, dynamic)
        for code in registry_failures:
            if code not in report["failures"]:
                report["failures"].append(code)
                report["armed"] = False
        self._command, self._static = command, static
        self._report = dict(report, mode="live", command=command, stale_to_unknown=stale, recorded_refused=orphans,
                            notes=notes)
        return dict(self._report)

    def _open_kill(self, command, confirm_token, bet_angel_paused):
        """open() for kill and kill-account (amendment A3, D4). .gateway.lock is never taken, so nothing
        waits for a holder. Static arming is A4 (kill-account adds A11) and the state-root part of A13.
        Then: the chain check, the one anchor check (Keychain read first, then the journal head, so a
        concurrent appender cannot cause a false mismatch), the emergency login, the account check and
        a fold. No anchor is written here: the kill publishes it only in close(), after the sweep, so no
        Keychain write and no wait for another publisher comes before a cancel (re-review R1). A broken
        chain, a failed anchor check, an account other than the journal's or an unfoldable journal never
        stops the sweep: the journal is untrusted, the sweep uses the config strategy ref only, no anchor
        is written and the kill ends kill_incomplete. The account check
        reads only: the logged-in account's fingerprint against the one the journal recorded (a kill
        on another account would find none of our bets there and report them gone). No account-registry
        write: the kill has no A15, and an unlocked read-modify-write of accounts.json could lose another
        profile's registration (F76)."""
        static = static_arming(self.config, command=command, paths=self.paths, cli_mode="live",
                               confirm_token=confirm_token, bet_angel_paused=bet_angel_paused, env=self.env,
                               confirmer=self.confirmer, keychain_runner=self.keychain_runner, journal_anchor={},
                               now_ns=self.clock.time_ns())
        if not static["armed"]:
            raise NotArmed(static["failures"][0], detail={"failures": static["failures"]})
        notes = []
        self._journal_untrusted = False
        failures = self._kill_journal_check()
        emergency = command in EMERGENCY_COMMANDS
        self.rpc.session.login(emergency=emergency, coexistence_ack=bool(bet_angel_paused))
        self.login_kind = "emergency" if emergency else "normal"
        try:
            recorded = self.journal.account_fingerprint()
        except Exception as exc:          # the kill: a journal it cannot read never stops the sweep
            recorded, failures = None, sorted(set(failures) | {_journal_read_code(exc)})
        if recorded is not None and recorded != self.rpc.session.account_fingerprint():
            failures = failures + ["account_differs_from_journal"]
        if not failures and self._fold_or_none() is None:
            failures = ["journal_unfoldable"]
        if failures:
            self._journal_untrusted = True
            notes.extend(failures + [UNTRUSTED_SWEEP_NOTE])
        self._command, self._static = command, static
        self._report = dict(static, mode="live", command=command, stale_to_unknown=[], recorded_refused=[],
                            notes=notes)
        return dict(self._report)

    def _kill_journal_check(self):
        """The kill's chain and anchor check -> [] or failure codes; never raises. Whatever reading the journal
        raises (a row SQLite cannot decode, a payload that is not JSON, a file that is not a journal at all)
        is a failure like a broken chain: the journal is untrusted and the sweep still runs (amendment A3, D4)."""
        try:
            verified = self.journal.verify()
        except Exception as exc:
            return [_journal_read_code(exc)]
        if verified["status"] != "ok":
            return ["journal_chain_broken"]
        try:
            return self._keychain_anchor_ok()
        except Exception as exc:
            return [_journal_read_code(exc)]

    def _startup_reconcile(self):
        now_ns = self.clock.time_ns()
        since = utc_from_ns(now_ns - now_ns % (86_400 * _NS_PER_S))
        state = self._state()
        refs = sorted({self.strategy_ref} | set(state["strategy_refs_seen"]))
        startup = startup_reconcile(self.read, state, strategy_refs=refs, live_app_key=self.rpc.session.app_key(),
                                    since_utc=since, clock=self.clock)
        self._append("startup_reconcile", {"exchange_orders_today": startup["exchange_orders_today"],
                                           "exchange_realized_today_pence": startup["exchange_realized_today_pence"],
                                           "foreign": startup["foreign"], "exchange_open": startup["exchange_open"],
                                           "contradictions": startup["contradictions"],
                                           "read_failures": startup["read_failures"], "notes": startup["notes"]})
        if startup["foreign"]:
            self._halt("foreign_strategy_order")
        if startup["contradictions"]:
            self._halt("exchange_journal_contradiction")
        return startup

    def _no_orders_state(self):
        """The kill's stand-in state: no orders known, halted. Its cancels run on it, so the kill journals
        no order moves (A6); an untrusted journal's sweep reads our bets on it (config strategy ref only)."""
        state = fold_events([], mode=self.mode, now_ns=self.clock.time_ns())
        state.update(halted=True, halt_reasons=["kill"])
        return state

    def _release(self):
        self._lock = None
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()

    def close(self):
        """Live: publish the journal anchor to the Keychain. Then release the lock. (The paper state is
        journaled with every change, R3#12; nothing is saved here.) A kill publishes only here, after its
        sweep. Every close waits at most killswitch.JOURNAL_BUSY_TIMEOUT_S for another publisher
        (_anchor_wait_s): a publication it cannot make is reported (anchor_error) and leaves the anchor behind
        the journal head, which A13 accepts, or (journal_anchor_not_extended) ahead of a journal rolled back
        under this session, which A13 then refuses."""
        out = {"mode": self.mode, "anchor_written": False, "lock_released": self._stack is not None}
        try:
            if self.mode == "live" and self._command is not None:
                wait_s = self._anchor_wait_s(self._command, refuses=False)
                try:
                    out["anchor_written"] = self._write_anchor(wait_s=wait_s)
                except CredentialError as exc:
                    out["anchor_error"] = safe_code(str(exc))
        finally:
            self._release()
            self._command = None
        return out

    # --- shared checks -------------------------------------------------------------------------

    def _check_source(self, source):
        if source not in SOURCES:
            raise ValueError("source must be cli_manual or api")
        if self.mode == "live" and source != "cli_manual":
            raise OrderRefused("api_source_paper_only")

    def _require_open(self, *commands):
        if self._command is None:
            raise OrderRefused("gateway_not_open")
        if self.mode == "live" and commands and self._command not in commands:
            raise OrderRefused("gateway_opened_for_another_command")

    def _require_paper_lock(self):
        """Paper: the paper lock must still exclude every other paper gateway (its path still names the locked file,
        as _submit_refusal checks before a submit) before this gateway journals a paper state, or before a paper-step,
        cancel or replace, which journal one. open() loads the latest paper state once and nothing re-reads it, so a
        gateway whose .paper.lock was unlinked or replaced (a second gateway may then hold a new lock and journal its
        own fills, takes and closed markers) would make its stale copy the latest one: every later open would lose the
        other gateway's takes (R3#12, the same displayed size taken twice) and closed markers (re-review R2, a market
        reopened after its result). Raises OrderRefused gateway_lock_lost; nothing is journaled by the refusal."""
        if self._lock is None or not self._lock.held():
            raise OrderRefused("gateway_lock_lost")

    def _require_armed(self):
        """Live: a failed check from open() is final for this process, so refuse before any prompt or read."""
        if self.mode == "live" and not (self._report or {}).get("armed"):
            failures = list((self._report or {}).get("failures") or ["not_armed"])
            raise NotArmed(failures[0], detail={"failures": failures})

    def _self_fetch(self, market_id):
        """The gateway's own book: live key in live mode (A18), the delayed read client in paper."""
        if self.read is None:
            raise OrderRefused("paper_book_required")
        try:
            return self.read.market_snapshot(market_id, fetched_by="gateway_self_fetch")
        except (BetfairError, ValueError) as exc:
            raise OrderRefused("book_fetch_failed", detail={"code": safe_code(getattr(exc, "code", None)
                                                                               or type(exc).__name__)}) from None

    def _book_age_ok(self, book):
        """The book is fresh on both clocks: the monotonic clock (immune to wall steps) stops while a Mac
        sleeps, so the wall-clock age of observed_at is checked too."""
        taken = book.get("monotonic_ns") if isinstance(book, dict) else None
        if type(taken) is not int:
            return False
        limit = int(self.config["orders"]["caps"]["max_book_age_s"] * _NS_PER_S)
        age = self.clock.monotonic_ns() - taken
        wall_age = self._book_wall_age_ns(book)
        return 0 <= age <= limit and wall_age is not None and 0 <= wall_age <= limit

    def _book_wall_age_ns(self, book):
        try:
            return self.clock.time_ns() - parse_utc_ns(book["observed_at"])
        except (KeyError, TypeError, ValueError):
            return None

    def _send_bounds(self, book):
        """(send_by_monotonic_ns, send_gate) for a live place request (amendment A3, C2 and D4).

        send_by is the book's monotonic time plus max_book_age_s (the API client caps it at the
        capability's expiry). The gate runs inside the transport right before the first request byte:
        kill_engaged_before_send once the KILL file exists (fail-closed), else book_stale_before_send
        when the book is too old on either clock (the monotonic clock stops while a Mac sleeps), else
        nothing. A refusal there is FAILED(transport_not_sent): zero bytes were written."""
        taken = book.get("monotonic_ns") if isinstance(book, dict) else None
        limit = int(self.config["orders"]["caps"]["max_book_age_s"] * _NS_PER_S)
        send_by = taken + limit if type(taken) is int else self.clock.monotonic_ns()

        def gate():
            if kill_engaged(self.paths):
                return "kill_engaged_before_send"
            if not self._book_age_ok(book):
                return "book_stale_before_send"
            return None

        return send_by, gate

    def _live_arming(self, command, book):
        dynamic = dynamic_arming(self.config, command=command, session=self.rpc.session,
                                 account_details=self._account_details, journal_state=self._state(),
                                 startup=self._startup, book=book, paths=self.paths,
                                 journal_fingerprint=self.journal.account_fingerprint(),
                                 now_monotonic_ns=self.clock.monotonic_ns(), now_ns=self.clock.time_ns())
        return combine(self._static, dynamic)

    def _funds(self):
        """Fresh getAccountFunds, journaled as funds_check: (funds, checked_at)."""
        checked_at = self.clock.utc_now()
        try:
            funds = self.read.get_account_funds()
        except (BetfairError, ValueError):
            return None, None
        self._append("funds_check", {"funds": funds})
        return funds, checked_at

    # --- operator presence (spec section 9.3 step 3) --------------------------------------------------

    def preconfirm(self, command, *, market_id=None, instructions=None, selection_id=None, accept_ref=None):
        """Live: the operator types each line at the terminal BEFORE open() takes the global lock, so a
        prompt left unanswered never blocks another process's kill (spec section 9.3 steps 3 then 4).

        command is place (instructions), live-probe (selection_id), reconcile-accept (accept_ref) or
        settle-market (market_id; the banner lists our orders in that market and their worst case).
        Each typed line is kept for this object and consumed once by the matching call after open():
        place, live-probe and reconcile-accept journal it as operator_confirm; settle-market carries it
        in its settle event. Live replace was removed in v1: there is nothing to type for it. Paper:
        nothing to type.
        """
        if self.mode != "live":
            return []
        if self._command is not None:
            raise OrderRefused("preconfirm_after_open")
        if command == "place":
            if not isinstance(instructions, list) or not instructions or any(_instruction_echo(i) is None
                                                                             for i in instructions):
                raise OrderRefused("validation:V0", detail={"reason": "instructions_must_be_v1"})
            lines = [order_echo_line(market_id, i["selection_id"], i["side"], i["price_h"], i["size_p"])
                     for i in instructions]
        elif command == "live-probe":
            lines = [probe_echo_line(market_id, selection_id)]
        elif command == "reconcile-accept":
            if not isinstance(accept_ref, str) or not accept_ref:
                raise ValueError("accept_ref must be an order ref")
            line = f"ACCEPT ABSENT {accept_ref}"
            self.confirmer.confirm_line(line, banner=self._accept_banner(accept_ref))
            self._preconfirmed.append(line)
            return [line]
        elif command == "settle-market":
            return [self._preconfirm_settle(market_id)]
        else:
            raise ValueError("preconfirm is for place, live-probe, reconcile-accept or settle-market")
        for line in lines:
            self.confirmer.confirm_line(line, banner=LIVE_BANNER)
            self._preconfirmed.append(line)
        return list(lines)

    def _accept_banner(self, ref):
        """reconcile --accept's banner, from a read-only fold: nothing is sent, and what stops being counted."""
        order = self.journal.fold(now_ns=self.clock.time_ns())["orders"].get(ref)
        if order is None:
            raise OrderRefused("accept_unknown_order")
        worst = exposure_by_market([order]).get(order["market_id"], 0)
        return (f"MODE=LIVE: ACCEPT ABSENT sends no order. It stops counting order {ref} (market {order['market_id']} "
                f"sel {order['selection_id']} {order['side']} @ {_two_dp(order['price_h'])} size "
                f"{_two_dp(order['size_p'])}, now {order['state']}, worst case {_gbp(worst)}) as possibly live, and "
                f"lets new orders through. Do this only after the Betfair website or Bet Angel shows no such bet.")

    def _take_confirmations(self, lines, intent_id):
        """Consume one preconfirmed copy of each line (refused if any is missing) and journal them."""
        pool = list(self._preconfirmed)
        for line in lines:
            if line not in pool:
                raise OrderRefused("operator_confirmation_missing")
            pool.remove(line)
        self._preconfirmed = pool
        for line in lines:
            self._append("operator_confirm", {"echo": line, "typed_before_lock": True}, intent_id=intent_id)

    def _send(self, prepared, journal_report, *, capability=None, mint=None, send_by_monotonic_ns=None,
              send_gate=None):
        """Send once with SIGINT/SIGTERM/SIGHUP blocked until journal_report(result, exc) has run.

        mint (cancels): a callable minting the capability here, after the submit append, so a slow
        journal never expires it; a refusal from it is journaled like any other (capability_* is
        FAILED_NOT_SENT). on_send (the CLI's hook, never the kill's) runs under the mask right before
        the request is handed to the API client, after any mint, so a mint refusal is never reported
        as a request sent: from then on the CLI defers those signals until the command has printed
        its result (R3#16). send_by_monotonic_ns and send_gate go to the transport (place only)."""
        with _signals_blocked():
            unexpected = None
            try:
                if mint is not None:
                    capability = mint()
                if self.on_send is not None:
                    self.on_send()
                bounds = {}
                if send_gate is not None or send_by_monotonic_ns is not None:
                    bounds = {"send_by_monotonic_ns": send_by_monotonic_ns, "send_gate": send_gate}
                result, exc = self.rpc.send_order(capability, prepared, **bounds), None
            except BetfairError as error:
                result, exc = None, error
            except Exception as error:          # recorded as an unknown outcome, then re-raised
                result, exc, unexpected = None, error, error
            outcome = journal_report(result, exc)
        if unexpected is not None:
            raise unexpected
        return outcome

    def _report_halts(self, normalised, exc, moves=None, unknown_before=None):
        """The global HALT reasons that ride in a live order report event (R3#13; paper: none).

        Spec section 9.8's automatic triggers for one order-call outcome: terminal_account,
        terminal_input and planner_bug (validator_gap), app_key and unrecognized codes, an unparseable
        report or an unexpected exception. Plus consecutive_unknown_outcomes on every report whose
        moves send an order to UNKNOWN once unknown_before + 1 (the fold's consecutive_unknown taken
        before the send) reaches CONSECUTIVE_UNKNOWN_HALT, so a fourth unknown outcome after a resume
        halts again (resume does not reset the count). unknown_before None: the kill's reports, which
        move nothing."""
        if self.mode != "live":
            return []
        reasons = set()
        if (unknown_before is not None and any(state == "UNKNOWN" for state, _ in (moves or {}).values())
                and unknown_before + 1 >= CONSECUTIVE_UNKNOWN_HALT):
            reasons.add("consecutive_unknown_outcomes")
        if exc is not None:
            if isinstance(exc, ApiError) and exc.category in HALT_CATEGORIES:
                reasons.add(HALT_CATEGORIES[exc.category])
            if not isinstance(exc, BetfairError):
                reasons.add("unrecognized")
        elif normalised is None:
            reasons.add("unrecognized")
        else:
            codes = [("execution_report", normalised["error_code"])]
            for item in normalised["instructions"]:
                codes.append(("instruction_report", item["error_code"]))
                for half in ("cancel", "place"):
                    if isinstance(item.get(half), dict):
                        codes.append(("instruction_report", item[half]["error_code"]))
            for surface, code in codes:
                if code is not None and classify(code, surface=surface) in HALT_CATEGORIES:
                    reasons.add(HALT_CATEGORIES[classify(code, surface=surface)])
        return sorted(reasons)

    def _submit_refusal(self, basis, own_from):
        """None when a place or replace may be submitted, else the refusal code.

        The lock must still exclude every other gateway (its path names the locked file), and only
        this request's own events may have reached the journal since `basis`, the head the risk
        guard's fold was taken at: anything another writer journaled meanwhile (a second gateway's
        order, a kill, a halt) was not in that decision, so the decision no longer stands.
        """
        if self._lock is None or not self._lock.held():
            return "gateway_lock_lost"
        start = basis.get("seq", 0)
        own = self._seqs[own_from:]
        if own != list(range(start + 1, start + 1 + len(own))):
            return "journal_changed_since_decision"
        return None

    def _own_head(self, basis, own_from):
        """The journal head this request expects at its submit: its own last event, else the basis."""
        if len(self._seqs) > own_from and self._last_event is not None:
            return {"seq": self._last_event["seq"], "last_sha256": self._last_event["event_sha256"]}
        return dict(basis)

    def _append_submit(self, payload, *, basis, own_from, **columns):
        """Journal a place or replace submit only if _submit_refusal passes, as a compare-and-append on
        the expected head (so nothing can land between the check and the write). -> refusal code or None."""
        refusal = self._submit_refusal(basis, own_from)
        if refusal is not None:
            return refusal
        try:
            self._append("submit", payload, expect_head=self._own_head(basis, own_from), **columns)
        except JournalError as exc:
            if "journal_head_moved" not in str(exc):
                raise
            return "journal_changed_since_decision"
        return None

    def _new_refs(self, count, now_ns):
        state = self._state()
        refs = []
        while len(refs) < count:
            ref = make_customer_order_ref(now_ns)
            if ref not in state["orders"] and ref not in refs:
                refs.append(ref)
        return make_customer_ref(now_ns), refs

    # --- place ---------------------------------------------------------------------------------

    def place(self, market_id, instructions, *, book=None, source, confirm_token=None):
        """Place v1 LIMIT instructions -> {"intent_id", "mode", "decision", "refusal", "orders", "journal_seq"}."""
        self._check_source(source)
        self._seqs = []
        if self.mode == "live":
            if book is not None:
                raise OrderRefused("book_source_not_allowed_live")
            self._require_open("place")
            return self._place_live(market_id, instructions, confirm_token, command="place", probe=False)
        self._require_open()
        if book is None:
            book = self._self_fetch(market_id)
        self._refuse_paper_book(market_id, book)
        return self._place_core(market_id, instructions, book=book, source=source, intent_id=_intent_id(),
                                arming={"armed": True, "failures": [], "checked": []}, probe=False)

    def _place_live(self, market_id, instructions, confirm_token, *, command, probe, echo_lines=None):
        if not token_matches(self.config, confirm_token, self.clock.time_ns()):
            raise NotArmed("confirm_token_mismatch")
        self._require_armed()
        if self._resolved_at_open:
            return self._resolved_refusal()
        if not isinstance(instructions, list) or not instructions or any(_instruction_echo(i) is None
                                                                         for i in instructions):
            raise OrderRefused("validation:V0", detail={"reason": "instructions_must_be_v1"})
        intent_id = _intent_id()
        if echo_lines is None:
            echo_lines = [order_echo_line(market_id, i["selection_id"], i["side"], i["price_h"], i["size_p"])
                          for i in instructions]
        self._take_confirmations(echo_lines, intent_id)
        # Funds first, then the book (amendment A3, C2): the book's age window, which bounds the send,
        # then covers only the decision, the journal appends and the connect. The book is fetched after
        # the operator typed the order, so it is never older than the echo.
        funds, checked_at = self._funds()
        book = self._self_fetch(market_id)
        if not self._book_age_ok(book):
            book = self._self_fetch(market_id)
        arming = self._live_arming(command, book)
        return self._place_core(market_id, instructions, book=book, source="cli_manual", intent_id=intent_id,
                                arming=arming, probe=probe, funds=funds, funds_checked_at=checked_at)

    def _refuse_refs(self, payload, refs, reason, *, intent_id, customer_ref=None):
        """A refusing risk_decision moving this request's own unsent refs to REFUSED. A ref another writer
        already refused (another gateway's open-time pass, after its lock file was replaced) is left as it is
        rather than failing the append: none of them was submitted, so nothing was sent either way."""
        state = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
        transitions, _ = self._moves_payload({ref: ("REFUSED", {}) for ref in refs}, state, reason)
        columns = {"customer_ref": customer_ref} if customer_ref is not None else {}
        self._append("risk_decision", dict(payload, transitions=transitions), intent_id=intent_id, **columns)

    def _resolved_refusal(self):
        """place or live-probe whose open() found an earlier UNKNOWN order of ours live (EXECUTABLE or
        EXECUTION_COMPLETE): refused before the decision, with nothing journaled or sent. The operator typed
        this order while the earlier one's outcome was unknown, so it may be a resend of a bet that is live;
        the result lists those orders and the operator runs the command again if the order is still wanted."""
        refs = list(self._resolved_at_open)
        orders = self._state()["orders"]
        detail = {"orders": [{"customer_order_ref": ref, "market_id": orders[ref]["market_id"]}
                             for ref in refs if ref in orders]}
        return self._result(None, refs, decision="refused", refusal={"rule": RESOLVED_AT_OPEN_RULE, "detail": detail})

    def _place_core(self, market_id, instructions, *, book, source, intent_id, arming, probe, funds=None,
                    funds_checked_at=None):
        now_ns = self.clock.time_ns()
        listed = instructions if isinstance(instructions, list) else []
        basis, own_from = self.journal.anchor(), len(self._seqs)        # the head the decision is taken at
        cref, refs = self._new_refs(len(listed), now_ns)
        decision = self.guard.evaluate_place(
            market_id, instructions, book=book, state=self._state(), arming=arming, funds=funds, now_ns=now_ns,
            probe=probe, now_monotonic_ns=self.clock.monotonic_ns(), kill_engaged=kill_engaged(self.paths),
            customer_ref=cref, customer_order_refs=refs, funds_checked_at=funds_checked_at)
        journaled = []
        for ref, instruction in zip(refs, listed):
            if _instruction_echo(instruction) is None or not isinstance(market_id, str):
                continue
            try:
                self._append("intent", {"order": instruction, "customer_strategy_ref": self.strategy_ref,
                                        "is_probe": bool(probe), "parent_ref": None, "provisional_child": False,
                                        "source": source}, intent_id=intent_id, customer_order_ref=ref,
                             customer_ref=cref, market_id=market_id, state_after="RECORDED")
            except JournalError:
                continue
            journaled.append(ref)
        payload = _decision_payload(decision)
        if not decision["approved"]:
            halt = decision["detail"].get("halt") and safe_code(decision["detail"].get("halt_reason") or "risk_halt")
            # A risk halt (R12) rides in the refusing risk_decision itself (R3#13).
            self._refuse_refs(_with_halts(payload, [halt] if halt else []), journaled, "risk_refused",
                              intent_id=intent_id, customer_ref=cref)
            return self._result(intent_id, journaled, decision="refused",
                                refusal={"rule": decision["rule"], "detail": decision["detail"]})
        self._append("risk_decision", payload, intent_id=intent_id, customer_ref=cref)
        records = [{"customer_order_ref": ref, "market_id": market_id, "selection_id": ins["selection_id"],
                    "side": ins["side"], "price_h": ins["price_h"], "size_p": ins["size_p"]}
                   for ref, ins in zip(refs, listed)]
        prepared = capability = params = None
        if self.mode == "live":
            params = {"marketId": market_id, "customerRef": cref, "customerStrategyRef": self.strategy_ref,
                      "marketVersion": {"version": decision["market_version"]},
                      "instructions": [{"orderType": "LIMIT", "selectionId": ins["selection_id"], "handicap": 0,
                                        "side": ins["side"], "customerOrderRef": ref,
                                        "limitOrder": {"size": wire_size(ins["size_p"]),
                                                       "price": wire_price(ins["price_h"]),
                                                       "persistenceType": "LAPSE"}}
                                       for ref, ins in zip(refs, listed)]}
            try:
                prepared = self.rpc.prepare_order(PLACE_METHOD, params)
                capability = self.guard.issue(decision, prepared, now_monotonic_ns=self.clock.monotonic_ns())
            except OrderRefused as exc:
                self._refuse_refs({"decision_id": decision["decision_id"], "approved": False, "rule": exc.rule},
                                  refs, exc.rule, intent_id=intent_id)
                return self._result(intent_id, refs, decision="refused", refusal={"rule": exc.rule, "detail": {}})
        refusal = self._append_submit(
            {"action": "place", "instruction_count": len(refs),
             "body_sha256": prepared["body_sha256"] if prepared else None, "decision_id": decision["decision_id"],
             "probe": bool(probe), "params": params,
             "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"} for ref in refs]},
            basis=basis, own_from=own_from, intent_id=intent_id, customer_ref=cref, market_id=market_id)
        if refusal is not None:
            lost = ["gateway_lock_lost"] if self.mode == "live" and refusal == "gateway_lock_lost" else []
            self._refuse_refs(_with_halts({"decision_id": decision["decision_id"], "approved": False, "rule": refusal},
                                          lost), journaled, refusal, intent_id=intent_id)
            return self._result(intent_id, journaled, decision="refused", refusal={"rule": refusal, "detail": {}})
        if kill_engaged(self.paths):
            self._append_moves("report", _with_halts({"action": "place", "outcome": "not_sent_kill_engaged"},
                                                     ["kill_engaged_after_submit_journal"]),
                               {ref: ("REFUSED", {"error_code": "kill_engaged_after_submit_journal"}) for ref in refs},
                               reason="kill_engaged_after_submit_journal", market_id=market_id, intent_id=intent_id)
            return self._result(intent_id, refs, decision="refused",
                                refusal={"rule": "halted", "detail": {"reason": "kill_engaged_after_submit_journal"}})
        before = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
        submitting = before["orders"]
        sent = [{"customer_order_ref": ref, "side": record["side"]} for ref, record in zip(refs, records)]
        extra = {}

        def journal_report(result, exc):
            if exc is not None:
                verdict = outcome_from_exception(exc)
                # The cause (e.g. kill_engaged_before_send at the first byte) also goes to the caller, so the
                # operator's line can say why nothing was sent (codes only, as journaled).
                extra["send_exception"] = dict(_exception_payload(exc), outcome=verdict)
                if verdict == "UNKNOWN":
                    moves = {ref: ("UNKNOWN", {"error_code": "exception_unknown_outcome"}) for ref in refs}
                else:
                    reason = "transport_not_sent" if verdict == "FAILED_NOT_SENT" else "rejected_before_processing"
                    moves = {ref: ("FAILED", {"error_code": reason}) for ref in refs}
                halts = self._report_halts(None, exc, moves, before["consecutive_unknown"])
                self._append_moves("report", _with_halts({"action": "place", "report": None,
                                                          "exception": _exception_payload(exc), "outcome": verdict},
                                                         halts),
                                   moves, reason="place_exception", market_id=market_id, intent_id=intent_id)
                return None, moves
            if self.mode == "paper":
                normalised = result
            else:
                normalised = normalise_place_report(result, market_id=market_id, sent=sent)
            if normalised is None:
                moves = {ref: ("UNKNOWN", {"error_code": "report_unparseable"}) for ref in refs}
            else:
                moves = classify_place_report(normalised, sent_refs=refs,
                                              orders={ref: submitting[ref] for ref in refs if ref in submitting})
            halts = self._report_halts(normalised, None, moves, before["consecutive_unknown"])
            self._append_moves("report", _with_halts({"action": "place", "report": normalised, "outcome": "reported"},
                                                     halts),
                               moves, reason="place_report", market_id=market_id, intent_id=intent_id)
            return normalised, moves

        if self.mode == "live":
            send_by, gate = self._send_bounds(book)
            normalised, moves = self._send(prepared, journal_report, capability=capability,
                                           send_by_monotonic_ns=send_by, send_gate=gate)
            # No cancel of our own here after a kill (spec step 16 was deleted, amendment A3 D4): the kill's
            # re-sweeps cover this bet, and a duplicate cancel only left an UNKNOWN behind.
            self._reconcile_refs([ref for ref, (state, _) in moves.items() if state == "UNKNOWN"])
        else:
            report = self.paper.place(records, book=book, now_ms=now_ns // 1_000_000)
            self._keep_paper_book(market_id, book)
            journal_report(report, None)
        return self._result(intent_id, refs, decision="accepted", extra=extra)

    def _yields_to_kill(self, command):
        """True once a KILL file exists while a place or live-probe gateway is open (amendment A3, D4): it
        stops reconciling, so it does not compete with the kill's reads (F18); its orders stay UNKNOWN at
        their worst case. Other commands simply finish (they may run while killed)."""
        return command in RESOLVE_ON_OPEN_COMMANDS and kill_engaged(self.paths)

    def _reconcile_refs(self, refs):
        """Reconcile this request's unresolved orders (spec section 9.3 step 15); a place or live-probe
        stops once a KILL file exists."""
        if self.mode != "live" or self.read is None:
            return
        for ref in refs:
            if self._yields_to_kill(self._command):
                return
            state = self._state()
            order = state["orders"].get(ref)
            if order is None or order["state"] not in ("UNKNOWN", "ABSENT_UNCONFIRMED"):
                continue
            self._reconcile_one(order, state)

    def _reconcile_one(self, order, state, *, accept=False):
        known = sorted({o["bet_id"] for o in state["orders"].values() if o["bet_id"] is not None})
        refs = sorted({self.strategy_ref} | set(state["strategy_refs_seen"]))
        live_key = self.rpc.session.app_key() if self.mode == "live" and self.rpc is not None else None
        result = reconcile_unknown(self.read, order, cfg=self.config, clock=self.clock, strategy_refs=refs,
                                   known_bet_ids=known, live_app_key=live_key)
        for event in result["events"]:
            payload = dict(event["payload"])
            if event["kind"] == "halt":
                self._append("halt", payload, market_id=event["market_id"])
                continue
            moves = {move["customer_order_ref"]: (move["state"], move["fields"])
                     for move in payload.pop("transitions", [])}
            self._append_moves("reconcile", payload, moves, reason=safe_code(result["reason"]),
                               market_id=event["market_id"])
        return result

    def _resolve_unknowns(self, *, budget_s, states=("UNKNOWN",), stop_on_kill=False):
        """Reconcile every order in states, newest first, within budget_s (monotonic); with stop_on_kill
        (place and live-probe opening) it stops once a KILL file exists. One fold picks the targets, and
        the journal is folded again only after a reconcile that wrote events, so the cost follows the
        unresolved orders, not every order ever journaled."""
        start = self.clock.monotonic_ns()
        results = []
        state = self._state()
        targets = sorted((ref for ref, order in state["orders"].items() if order["state"] in states), reverse=True)
        stale = False
        for ref in targets:
            if self.clock.monotonic_ns() - start > budget_s * _NS_PER_S or (stop_on_kill and kill_engaged(self.paths)):
                break
            if stale:
                state, stale = self._state(), False
            order = state["orders"].get(ref)
            if order is None or order["state"] not in states:
                continue
            result = self._reconcile_one(order, state)
            results.append(result)
            stale = bool(result["events"])
        return results

    # --- cancel --------------------------------------------------------------------------------

    def cancel(self, market_id, bet_ids, *, source, size_reductions=None):
        """Cancel our own bets by betId (allowed while halted or killed). Live cancels are full cancels in
        v1 (amendment A3): a size reduction is refused before anything is checked, read or journaled. A paper
        gateway that lost its paper lock refuses before anything is journaled (_require_paper_lock)."""
        self._check_source(source)
        if self.mode == "live" and size_reductions is not None and any(r is not None for r in size_reductions):
            raise OrderRefused("live_partial_cancel_removed_v1")
        self._seqs = []
        self._require_open()
        if self.mode == "paper":
            self._require_paper_lock()
        if isinstance(bet_ids, str) or not isinstance(bet_ids, (list, tuple)):
            raise ValueError("bet_ids must be a list")
        reductions = list(size_reductions) if size_reductions is not None else [None] * len(bet_ids)
        if len(reductions) != len(bet_ids):
            raise ValueError("size_reductions must match bet_ids")
        self._require_armed()
        cancels = [{"bet_id": bet, "size_reduction_p": reduction} for bet, reduction in zip(bet_ids, reductions)]
        return self._cancel(market_id, cancels, kill_targets=None, reconcile=True)

    def _cancel(self, market_id, cancels, *, kill_targets, reconcile, state=None):
        """state: the kill's no-orders stand-in (no fold is then taken and no order is moved).

        Live cancels carry no send-by and no first-byte gate (a late cancel of our own bet adds no risk);
        their capability is minted inside _send, right after the submit append (amendment A3, C2)."""
        now_ns = self.clock.time_ns()
        standin = state is not None
        if state is None:
            state = self._state()
        cref = make_customer_ref(now_ns)
        intent_id = _intent_id()
        decision = self.guard.evaluate_cancel(market_id, cancels, state=state, now_ns=now_ns, customer_ref=cref,
                                              kill_targets=kill_targets)
        bet_ids = [c["bet_id"] for c in cancels if isinstance(c, dict)]
        listed = [b for b in bet_ids if isinstance(b, str) and b.isdigit() and b.isascii()]
        self._append("cancel_intent", dict(_decision_payload(decision), bet_ids=listed),
                     intent_id=intent_id, customer_ref=cref,
                     market_id=market_id if isinstance(market_id, str) and MARKET_ID_RE.fullmatch(market_id) else None)
        if not decision["approved"]:
            return self._result(intent_id, [], decision="refused",
                                refusal={"rule": decision["rule"], "detail": decision["detail"]})
        ours = {o["bet_id"]: o for o in state["orders"].values() if o["bet_id"] is not None
                and o["market_id"] == market_id}
        refs = [ours[b]["customer_order_ref"] for b in bet_ids if b in ours]
        prepared = params = None
        if self.mode == "live":
            params = {"marketId": market_id, "customerRef": cref,
                      "instructions": [{"betId": c["bet_id"], "sizeReduction": None if c["size_reduction_p"] is None
                                        else wire_size(c["size_reduction_p"])} for c in cancels]}
            try:
                prepared = self.rpc.prepare_order(CANCEL_METHOD, params)
            except OrderRefused as exc:
                return self._result(intent_id, refs, decision="refused", refusal={"rule": exc.rule, "detail": {}})
        pending = {ours[b]["customer_order_ref"]: ("CANCEL_PENDING", {}) for b in bet_ids
                   if b in ours and ours[b]["state"] == "EXECUTABLE"}
        self._append("submit", {"action": "cancel", "instruction_count": len(cancels),
                                "body_sha256": prepared["body_sha256"] if prepared else None,
                                "decision_id": decision["decision_id"], "params": params,
                                "transitions": [{"customer_order_ref": ref, "state": "CANCEL_PENDING"}
                                                for ref in sorted(pending)]},
                     intent_id=intent_id, customer_ref=cref, market_id=market_id)
        after = None if standin else self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
        after_submit = {} if after is None else after["orders"]
        unknown_before = None if after is None else after["consecutive_unknown"]
        by_bet = {after_submit[r]["bet_id"]: after_submit[r] for r in refs}

        def journal_report(result, exc):
            if exc is not None:
                verdict = outcome_from_exception(exc)
                moves = {}
                for bet in bet_ids:
                    order = by_bet.get(bet)
                    if order is None:
                        continue
                    if verdict == "UNKNOWN":
                        moves[order["customer_order_ref"]] = ("UNKNOWN", {"error_code": "cancel_outcome_unknown"})
                    elif order["state"] == "CANCEL_PENDING":
                        moves[order["customer_order_ref"]] = ("EXECUTABLE", {
                            "size_remaining_p": order["size_remaining_p"], "error_code": "cancel_not_processed"})
                halts = self._report_halts(None, exc, moves, unknown_before)
                self._append_moves("report", _with_halts({"action": "cancel", "report": None,
                                                          "exception": _exception_payload(exc), "outcome": verdict,
                                                          "transactions": report_transactions(
                                                              "cancel", None, sent_count=len(cancels))}, halts),
                                   moves, reason="cancel_exception", market_id=market_id, intent_id=intent_id)
                return None, moves
            normalised = result if self.mode == "paper" else normalise_cancel_report(result, market_id=market_id)
            if normalised is None:
                classified = {bet: ("UNKNOWN", {"error_code": "report_unparseable"}) for bet in bet_ids}
            else:
                classified = classify_cancel_report(normalised, sent_bet_ids=bet_ids, orders=by_bet)
            moves = {by_bet[bet]["customer_order_ref"]: move for bet, move in classified.items() if bet in by_bet}
            halts = self._report_halts(normalised, None, moves, unknown_before)
            self._append_moves("report", _with_halts({"action": "cancel", "report": normalised, "outcome": "reported",
                                                      "transactions": report_transactions(
                                                          "cancel", normalised, sent_count=len(cancels))}, halts),
                               moves, reason="cancel_report", market_id=market_id, intent_id=intent_id)
            return normalised, moves

        if self.mode == "live":
            def mint():
                return self.guard.issue(decision, prepared, now_monotonic_ns=self.clock.monotonic_ns())

            normalised, moves = self._send(prepared, journal_report, mint=mint)
            if reconcile:
                self._reconcile_refs([ref for ref, (s, _) in moves.items() if s == "UNKNOWN"])
        else:
            before = [dict(o) for o in state["orders"].values() if o["bet_id"] in bet_ids]
            report = self.paper.cancel(before or [{"bet_id": None, "market_id": market_id}], cancels,
                                       now_ms=now_ns // 1_000_000)
            journal_report(report, None)
        return self._result(intent_id, refs, decision="accepted")

    # --- replace (paper only) ------------------------------------------------------------------

    def replace(self, market_id, replaces, *, book=None, source, confirm_token=None):
        """Paper only: replace our EXECUTABLE paper bets at new prices: [{"bet_id", "new_price_h"}].

        Live replace was removed in v1 (amendment A3, D2): a live gateway refuses here, before any
        check, read, journal write or request. confirm_token is accepted (and ignored) for the paper
        signature only. A gateway that lost its paper lock refuses before anything is read or journaled
        (_require_paper_lock): its refusal report would carry its stale paper state."""
        self._check_source(source)
        if self.mode == "live":
            raise OrderRefused("live_replace_removed_v1")
        self._seqs = []
        if not isinstance(replaces, list) or not replaces:
            raise ValueError("replaces must be a non-empty list")
        self._require_open()
        self._require_paper_lock()
        if book is None:
            book = self._self_fetch(market_id)
        self._refuse_paper_book(market_id, book)
        return self._replace_core(market_id, replaces, book=book, intent_id=_intent_id(),
                                  arming={"armed": True, "failures": [], "checked": []}, source=source)

    def _replace_core(self, market_id, replaces, *, book, intent_id, arming, source):
        """Paper only (live replace was removed in v1; evaluate_replace refuses live before anything is
        journaled): decision, provisional children, submit, paper report."""
        now_ns = self.clock.time_ns()
        basis, own_from = self.journal.anchor(), len(self._seqs)        # the head the decision is taken at
        state = self._state()
        cref = make_customer_ref(now_ns)
        decision = self.guard.evaluate_replace(
            market_id, replaces, book=book, state=state, arming=arming, now_ns=now_ns,
            now_monotonic_ns=self.clock.monotonic_ns(), kill_engaged=kill_engaged(self.paths), customer_ref=cref)
        self._append("replace_intent", _decision_payload(decision), intent_id=intent_id, customer_ref=cref,
                     market_id=market_id)
        if not decision["approved"]:
            if decision["detail"].get("halt"):
                self._halt(decision["detail"].get("halt_reason") or "risk_halt")
            return self._result(intent_id, [], decision="refused",
                                refusal={"rule": decision["rule"], "detail": decision["detail"]})
        ours = {o["bet_id"]: o for o in state["orders"].values() if o["bet_id"] is not None
                and o["market_id"] == market_id}
        pairs = []                        # (parent record, child ref, new_price_h)
        for item in decision["instructions"]:
            parent = ours[item["bet_id"]]
            child_ref = self._new_refs(1, now_ns)[1][0]
            child = {"selection_id": parent["selection_id"], "handicap": 0, "side": parent["side"],
                     "price_h": item["new_price_h"], "size_p": parent["size_remaining_p"], "persistence_type": "LAPSE"}
            self._append("intent", {"order": child, "customer_strategy_ref": self.strategy_ref, "is_probe": False,
                                    "parent_ref": parent["customer_order_ref"], "provisional_child": True,
                                    "source": source},
                         intent_id=intent_id, customer_order_ref=child_ref, customer_ref=cref, market_id=market_id,
                         state_after="RECORDED")
            pairs.append((parent, child_ref, item["new_price_h"]))
        refs = [r for parent, child_ref, _ in pairs for r in (parent["customer_order_ref"], child_ref)]
        transitions = []
        for parent, child_ref, _ in pairs:
            transitions.append({"customer_order_ref": parent["customer_order_ref"], "state": "REPLACE_PENDING"})
            transitions.append({"customer_order_ref": child_ref, "state": "SUBMITTING"})
        refusal = self._append_submit(
            {"action": "replace", "instruction_count": len(pairs), "body_sha256": None,
             "decision_id": decision["decision_id"], "params": None, "transitions": transitions},
            basis=basis, own_from=own_from, intent_id=intent_id, customer_ref=cref, market_id=market_id)
        if refusal is not None:
            self._append_moves("report", {"action": "replace", "outcome": "not_sent_" + refusal},
                               {child_ref: ("REFUSED", {"error_code": refusal}) for _, child_ref, _ in pairs},
                               reason=refusal, market_id=market_id, intent_id=intent_id)
            return self._result(intent_id, refs, decision="refused", refusal={"rule": refusal, "detail": {}})
        if kill_engaged(self.paths):
            self._halt("kill_engaged_after_submit_journal")
            moves = {}
            for parent, child_ref, _ in pairs:
                moves[parent["customer_order_ref"]] = ("EXECUTABLE", {"size_remaining_p": parent["size_remaining_p"]})
                moves[child_ref] = ("REFUSED", {"error_code": "kill_engaged_after_submit_journal"})
            self._append_moves("report", {"action": "replace", "outcome": "not_sent_kill_engaged"}, moves,
                               reason="kill_engaged_after_submit_journal", market_id=market_id, intent_id=intent_id)
            return self._result(intent_id, refs, decision="refused",
                                refusal={"rule": "halted", "detail": {"reason": "kill_engaged_after_submit_journal"}})
        after = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)["orders"]
        sent = [{"bet_id": parent["bet_id"], "new_price_h": price, "order": after[parent["customer_order_ref"]]}
                for parent, _, price in pairs]
        children = {parent["customer_order_ref"]: child_ref for parent, child_ref, _ in pairs}
        parents = [dict(parent) for parent, _, _ in pairs]
        specs = [{"bet_id": parent["bet_id"], "new_price_h": price, "child_ref": child_ref}
                 for parent, child_ref, price in pairs]
        report = self.paper.replace(parents, specs, book=book, now_ms=now_ns // 1_000_000)
        self._keep_paper_book(market_id, book)
        moves = classify_replace_report(report, sent=sent, provisional_children=children)
        self._append_moves("report", {"action": "replace", "report": report, "outcome": "reported",
                                      "transactions": report_transactions("replace", report, sent_count=len(pairs))},
                           moves, reason="replace_report", market_id=market_id, intent_id=intent_id)
        return self._result(intent_id, refs, decision="accepted")

    # --- paper advance (paper-step) ------------------------------------------------------------

    def _paper_book_older(self, market_id, book):
        """True when book is older than the last book this market's paper state used (its traded-volume
        baseline must never move back, or volume already counted would count again)."""
        prev = self._paper_books.get(market_id)
        if prev is None:
            return False
        then, now = book_time_ms(prev), book_time_ms(book)
        return then is not None and now is not None and now < then

    def _paper_closed_ms(self, market_id):
        """The time (ms) of the latest CLOSED book the paper state has seen for market_id, or None: its marker,
        or its kept CLOSED book (a market still holding an unfinished order keeps one, and paper states from before
        round 4 kept every one); a kept CLOSED book without a usable time counts as closed at 0."""
        times = [self._paper_closed[market_id]] if market_id in self._paper_closed else []
        kept = self._paper_books.get(market_id)
        if isinstance(kept, dict) and kept.get("status") == "CLOSED":
            at = book_time_ms(kept)
            times.append(0 if at is None else at)
        return max(times) if times else None

    def _paper_book_refusal(self, market_id, book, *, step):
        """Re-review R2: a refusal code from the closed-market markers for book (or an order on it), or None.

        A market the paper state has seen CLOSED takes no order (place, replace) and no later book but a CLOSED
        one at or after its close (a step that settles what an earlier one could not, or nothing):
        paper_market_closed. Betfair reopening a CLOSED market is not in the fact register; paper treats CLOSED
        as final, as the paper replay does. Without a marker (none yet, or evicted), a book at or before the
        floor is refused unless the market holds a timed book in the paper state (whose own chronology rule
        then refuses anything older): paper_book_at_or_before_closed_floor. An evicted market's state was
        dropped at its close and a new one starts only from a book after the floor, so its pre-close books can
        never be used again. A closed market holding a kept book is never spared here: its kept book is CLOSED,
        or its marker is never evicted (_evict_paper_closed)."""
        at = book_time_ms(book) if isinstance(book, dict) else None
        closed_ms = self._paper_closed_ms(market_id)
        if closed_ms is not None:
            if step and book.get("status") == "CLOSED" and at is not None and at >= closed_ms:
                return None
            return PAPER_MARKET_CLOSED
        floor = self._paper_closed_floor_ms
        kept = self._paper_books.get(market_id)
        if (floor is not None and at is not None and at <= floor
                and (kept is None or book_time_ms(kept) is None)):
            return PAPER_CLOSED_FLOOR
        return None

    def _refuse_paper_book(self, market_id, book):
        """Paper place and replace: a closed market or a book at or before the closed floor (re-review R2), then a
        book older than the market's last one, are refused. A market already closed is refused with nothing
        journaled; a CLOSED book of a market not yet closed is first recorded (_note_paper_closed), whatever is
        refused here or later (V9 market_not_open)."""
        refusal = self._paper_book_refusal(market_id, book, step=False)
        if refusal is None and self._paper_book_older(market_id, book):
            refusal = "paper_book_older_than_last_seen"
        self._note_paper_closed(market_id, book)
        if refusal is not None:
            raise OrderRefused(refusal)

    def _note_paper_closed(self, market_id, book):
        """Re-review R2 follow-up: a CLOSED book handed to place or replace (refused there, or at V9
        market_not_open), or one paper-step ignores, still closes its market. Without this, a pre-close OPEN book
        handed in afterwards (within max_snapshot_age_s) was taken and settled against a result already shown.

        Only a schema-valid CLOSED book of market_id itself with a usable time counts (a book of another market is
        refused as before, R3#6), and only for a market the paper state does not already hold closed, so a
        paper_market_closed refusal still journals nothing. The market is marked at the book's time (as a stepped
        CLOSED book marks it), and a market holding a kept book keeps this CLOSED one instead when it is not older
        (as a CLOSED step leaves it), so the market stays closed if its marker is later evicted. When the CLOSED
        book is older than the kept book, the kept (not CLOSED) book stays and its marker is never evicted
        (_evict_paper_closed), so the market stays closed either way. The new paper state is journaled at once,
        before the call journals anything else, in a note (kind note, reason paper_market_closed, the market in
        its market_id column), the paper-step's way of journaling a paper state that moves no order; the
        request's own decision is taken on the head after it. A gateway that lost its paper lock refuses here
        (gateway_lock_lost, _require_paper_lock) before anything changes or is journaled: its stale paper state
        would otherwise overwrite the state of the gateway holding the new lock."""
        if not (isinstance(book, dict) and isinstance(market_id, str) and book.get("market_id") == market_id
                and book.get("status") == "CLOSED"):
            return
        try:
            validate_book_snapshot(book)
        except (ValueError, TypeError, KeyError, AttributeError):
            return
        at = book_time_ms(book)
        if type(at) is not int or at < 0 or self._paper_closed_ms(market_id) is not None:
            return
        self._require_paper_lock()
        before = canonical_sha256(self._paper_state())
        # The kept book first, so the eviction in _mark_paper_closed sees whether it is CLOSED now.
        if market_id in self._paper_books:
            self._keep_paper_book(market_id, book)
        self._mark_paper_closed(market_id, at)
        if canonical_sha256(self._paper_state()) != before:
            self._append("note", {"reason": PAPER_MARKET_CLOSED, "paper_state": self._paper_state()},
                         market_id=market_id)

    def _mark_paper_closed(self, market_id, closed_ms):
        """Re-review R2: the market's marker, at its latest CLOSED book; the earliest closes beyond
        PAPER_CLOSED_MARKERS_MAX leave the markers for the floor."""
        self._paper_closed[market_id] = max(closed_ms, self._paper_closed.get(market_id, closed_ms))
        self._evict_paper_closed()

    def _evict_paper_closed(self):
        """The earliest closes beyond PAPER_CLOSED_MARKERS_MAX leave the markers, each raising floor_ms to its close,
        except the marker of a market whose kept book is not CLOSED. Only a CLOSED book older than the market's kept
        book leaves such a pair (_note_paper_closed cannot keep it), and that marker alone closes the market: evicted,
        the floor would spare the market (it holds a timed book) and its kept book's chronology would take any later
        book, so an order resting from before the close could fill and settle after it. Such a market rides whole in
        the paper state anyway (its book, queues and takes), so its marker adds little; once a CLOSED step not older
        than its kept book is taken, the kept book is CLOSED and the marker may go."""
        excess = len(self._paper_closed) - PAPER_CLOSED_MARKERS_MAX
        if excess <= 0:
            return
        kept = self._paper_books
        evictable = [m for m in self._paper_closed
                     if kept.get(m) is None or (isinstance(kept[m], dict) and kept[m].get("status") == "CLOSED")]
        for market in sorted(evictable, key=lambda m: (self._paper_closed[m], m))[:excess]:
            at = self._paper_closed.pop(market)
            floor = self._paper_closed_floor_ms
            self._paper_closed_floor_ms = at if floor is None else max(floor, at)

    def _keep_paper_book(self, market_id, book):
        if not self._paper_book_older(market_id, book):
            self._paper_books[market_id] = book

    def advance(self, market_id, *, book=None):
        """Paper only: passive fills and lapses on a new book; a CLOSED book also settles the market.

        A book of another market is refused before anything is read or changed (R3#6). A book older than
        the market's last one, any book but a CLOSED one at or after its close of a market already closed,
        and a book at or before the closed floor (re-review R2) are ignored, with nothing journaled, except
        that an ignored CLOSED book of a market not yet closed closes it (_note_paper_closed, one note).
        Every event journaled here carries the paper state after this step (its new book and, at CLOSED, the
        market's closed marker included); a step that changes that state without moving an order journals it
        in a note (paper_advance_state), which carries the step's non-empty flags when no report of the step
        carried them. A gateway that lost its paper lock refuses first, before any read
        (gateway_lock_lost, _require_paper_lock): every step may journal its paper state."""
        if self.mode != "paper":
            raise OrderRefused("advance_is_paper_only")
        self._seqs = []
        self._require_open()
        self._require_paper_lock()
        if book is None:
            book = self._self_fetch(market_id)
        if not isinstance(book, dict) or book.get("market_id") != market_id:
            raise OrderRefused("paper_book_market_mismatch")
        prev = self._paper_books.get(market_id)
        state = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
        refusal = self._paper_book_refusal(market_id, book, step=True)
        if refusal is None and self._paper_book_older(market_id, book):
            refusal = "paper_book_older_than_previous"
        if refusal is not None:
            self._note_paper_closed(market_id, book)
            refs = sorted(r for r, o in state["orders"].items() if o["market_id"] == market_id)
            return self._result(None, refs, decision="ignored", refusal={"rule": refusal, "detail": {}},
                                extra={"settlement": None, "flags": {}})
        open_orders = [o for o in state["orders"].values() if o["market_id"] == market_id]
        book_ms = book_time_ms(book)
        if book_ms is None:
            raise OrderRefused("paper_book_invalid")
        moves = {}
        flags = {}
        before = canonical_sha256(self._paper_state())
        for update in self.paper.advance(open_orders, prev_book=prev, book=book, now_ms=book_ms):
            ref = update["customer_order_ref"]
            flags[ref] = update["flags"]
            if update["fields"] or update["state"] != state["orders"][ref]["state"]:
                moves[ref] = (update["state"], update["fields"])
        self._keep_paper_book(market_id, book)
        if book.get("status") == "CLOSED":
            # Re-review R2: from this step on, every event's paper state marks the market closed.
            self._mark_paper_closed(market_id, book_ms)
        journaled = len(self._seqs)
        # A move report carries every order's flags. A step without one keeps its non-empty flags in the next
        # event it journals anyway (the closing lapses or the paper_advance_state note) and adds no event for
        # them, so the journal keeps at most one flag list per order per journaled step; a step that journals
        # nothing keeps its flags only in the result.
        pending_flags = {} if moves else {ref: f for ref, f in flags.items() if f}
        if moves:
            self._append_moves("report", {"action": "place", "paper_advance": True, "flags": flags}, moves,
                               reason="paper_advance", market_id=market_id)
        settlement = None
        if book.get("status") == "CLOSED":
            state = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
            lapses = {}
            for order in state["orders"].values():
                if order["market_id"] == market_id and order["state"] == "EXECUTABLE":
                    target = "EXECUTION_COMPLETE" if order["size_matched_p"] > 0 else "LAPSED"
                    lapses[order["customer_order_ref"]] = (target, {
                        "size_lapsed_p": order["size_lapsed_p"] + order["size_remaining_p"], "size_remaining_p": 0})
            if lapses:
                closing = {"action": "place", "paper_advance": True}
                if pending_flags:
                    closing["flags"], pending_flags = pending_flags, {}
                self._append_moves("report", closing, lapses, reason="paper_market_closed", market_id=market_id)
            state = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
            market_orders = [o for o in state["orders"].values() if o["market_id"] == market_id
                             and o["state"] in ("EXECUTION_COMPLETE", "REPLACED")]
            if market_orders:
                settlement = self.paper.settle(market_orders, market_definition=book)
                settle_moves = {ref: (item["state"], {"settled_profit_own_p": item["settled_profit_own_p"]})
                                for ref, item in settlement["orders"].items()}
                if settle_moves:
                    self._append_moves("settle", {"commission_p": settlement["commission_pence"] or 0},
                                       settle_moves, reason="paper_settlement", market_id=market_id)
            self._close_paper_market(market_id)
        # The paper state as journaled by this step's last event (else as it was before the step).
        journaled_state = (canonical_sha256(self._last_event["payload"]["paper_state"])
                           if len(self._seqs) > journaled and "paper_state" in self._last_event["payload"] else before)
        if canonical_sha256(self._paper_state()) != journaled_state:
            note = {"reason": "paper_advance_state", "paper_state": self._paper_state()}
            if pending_flags:
                note["flags"] = pending_flags
            self._append("note", note, market_id=market_id)
        state = self._state()
        refs = sorted(r for r, o in state["orders"].items() if o["market_id"] == market_id)
        return self._result(None, refs, decision="advanced", extra={"settlement": settlement, "flags": flags})

    def _close_paper_market(self, market_id):
        """Paper, after a CLOSED book: once every order in market_id is final (terminal, or EXECUTION_COMPLETE
        with nothing matched, i.e. fully cancelled or lapsed, which nothing settles), drop the market from the
        paper state: its book, takes and traded high-water marks, and its orders' queues and priorities. The
        paper state rides in every paper report, settle and note event (R3#12), so a closed market kept there
        made each event, and the journal, grow with every market ever stepped. The market's closed marker
        (re-review R2) stays: without it a pre-close book would start the market again, take liquidity after the
        result was known and settle a new bet on it."""
        state = self.journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
        refs = [ref for ref, o in state["orders"].items() if o["market_id"] == market_id]
        if not all(_paper_final(state["orders"][ref]) for ref in refs):
            return
        self._paper_books.pop(market_id, None)
        self.paper.close_market(market_id, refs)

    # --- probe ---------------------------------------------------------------------------------

    def probe(self, market_id, selection_id, *, confirm_token):
        """Spec section 9.10: LAY 1.00 @ 1.01, looked up by ref, then cancelled at once; the only way to set
        ref_roundtrip_verified.

        Place then cancel only: the replace step was removed with live replace in v1."""
        if self.mode != "live":
            raise OrderRefused("probe_is_live_only")
        self._seqs = []
        self._require_open("live-probe")
        instruction = {"selection_id": selection_id, "handicap": 0, "side": "LAY", "price_h": PROBE_SELECTION_PRICE_H,
                       "size_p": PROBE_SIZE_P, "persistence_type": "LAPSE"}
        line = probe_echo_line(market_id, selection_id)
        placed = self._place_live(market_id, [instruction], confirm_token, command="live-probe", probe=True,
                                  echo_lines=[line])
        out = {"place": placed, "cancel": None, "found_by_ref": False, "ref_roundtrip_verified": False}
        if placed["decision"] != "accepted" or not placed["orders"]:
            return out
        ref = placed["orders"][0]["customer_order_ref"]
        started_ns = self.clock.time_ns()

        def current_by_ref():
            return self._probe_read(lambda: list(self.read.iter_current_orders(
                customer_order_refs=[ref], order_projection="ALL")))

        # Look the bet up by ref before the cancel. In the first live probe (2026-10-21) the fully unmatched bet,
        # once cancelled, was absent from both later listCurrentOrders reads and from the cleared views before
        # settlement, so a lookup after the cancel may never verify the ref. The bet stays live until the lookup
        # ends: one read, or up to the read client's retries and a relogin (about 35-45 s at the default config).
        # It can lose at most £0.01. The cancel runs even if the lookup raises, and also under a KILL: it only
        # reduces risk, and a file-only KILL sweeps nothing. If a kill's sweep got there first, the duplicate
        # cancel leaves a conservative UNKNOWN and the CLI hands the bet to the operator (exit 5).
        current, current_error, killed = None, None, False
        try:
            current, current_error = current_by_ref()
        finally:
            order = self._state()["orders"][ref]
            killed = kill_engaged(self.paths)
            if order["state"] == "EXECUTABLE" and order["bet_id"] is not None:
                out["cancel"] = self._cancel(market_id, [{"bet_id": order["bet_id"], "size_reduction_p": None}],
                                             kill_targets=None, reconcile=True)
        after, after_error = None, "skipped_kill"
        cleared = {"CANCELLED": None, "LAPSED": None}
        if not killed:
            # Best effort: these reads only describe what happened; the round trip is already decided above.
            # Under a KILL they are skipped so they do not compete with the kill's reads (F18).
            try:
                after, after_error = current_by_ref()
            except Exception as exc:
                after, after_error = None, safe_code(type(exc).__name__)
            for status in ("CANCELLED", "LAPSED"):
                cleared[status], _ = self._probe_read(lambda: list(self.read.iter_cleared_orders(
                    status, settled_from_utc=utc_from_ns(started_ns - 60 * _NS_PER_S), customer_order_refs=[ref])))
        found = any(isinstance(item, dict) and item.get("customerOrderRef") == ref for item in current or [])
        directory = self.paths.live_tests_dir / f"probe-{utc_compact_from_ns(self.clock.time_ns())}-{ref[-8:]}"
        self.paths.ensure()
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        receipts = {"current_orders.json": scrub(current), "current_orders_after_cancel.json": scrub(after),
                    "cleared_cancelled.json": scrub(cleared["CANCELLED"]),
                    "cleared_lapsed.json": scrub(cleared["LAPSED"])}
        hashes = {name: atomic_write_json(directory / name, {"items": value}) for name, value in receipts.items()}
        self._append("probe_result", {"found_by_ref": found, "lookup": "before_cancel", "kill_engaged": killed,
                                      "current_count": len(current or []), "current_read_error": current_error,
                                      "current_after_cancel_count": len(after or []),
                                      "current_after_cancel_read_error": after_error,
                                      "cleared_cancelled_count": len(cleared["CANCELLED"] or []),
                                      "cleared_lapsed_count": len(cleared["LAPSED"] or []),
                                      "receipt_sha256": hashes},
                     customer_order_ref=None, market_id=market_id)
        if found:
            self._append("ref_roundtrip_verified", {"customer_order_ref_found": True})
        out.update(found_by_ref=found, ref_roundtrip_verified=found, receipt_dir=str(directory))
        return out

    @staticmethod
    def _probe_read(fetch):
        try:
            return fetch(), None
        except (BetfairError, ValueError) as exc:
            return None, safe_code(getattr(exc, "code", None) or type(exc).__name__)

    # --- reconcile -----------------------------------------------------------------------------

    def reconcile(self, *, accept_ref=None, confirm_token=None):
        """Live: resolve UNKNOWN and ABSENT_UNCONFIRMED orders under the live evidence rule (reconcile.py:
        exchange evidence may establish a bet and raise its known matched size, never lower its counted
        worst case). Nothing is refreshed, closed or settled here (amendment A3, D3): a CLOSED market is
        settled by hand with settle-market. --accept moves one ABSENT_UNCONFIRMED order to ABSENT_ACCEPTED
        after a final clean pass and the operator's typed confirmation."""
        self._seqs = []
        if accept_ref is not None:
            # ABSENT_ACCEPTED removes adverse exposure: it needs a gateway opened (and armed) for
            # reconcile-accept, which adds A1, A2 and A11 to the reconcile checks.
            self._require_open("reconcile-accept")
        else:
            self._require_open("reconcile", "reconcile-accept")
        self._require_armed()
        if accept_ref is not None:
            return self._accept(accept_ref, confirm_token)
        if self.mode == "paper":
            return {"mode": "paper", "status": "paper_has_no_exchange_to_reconcile",
                    "unresolved": [r for r, o in self._state()["orders"].items() if o["state"] in UNRESOLVED_STATES]}
        results = self._resolve_unknowns(budget_s=OPEN_RECONCILE_BUDGET_S, states=("UNKNOWN", "ABSENT_UNCONFIRMED"))
        cleared = self._clear_reconciled_market_halts()
        orders = self._state()["orders"]
        # error_code shows what the exchange evidence was, e.g. exchange_reduction_withheld_until_settle: an
        # EXECUTABLE order Betfair shows cancelled, lapsed or voided, counted at worst case until settle-market.
        # unresolved: every order still unresolved after this pass (attempted or not): new orders stay blocked.
        return {"mode": "live", "status": "reconciled", "reconciled": [
            {"customer_order_ref": r["customer_order_ref"], "final_state": r["final_state"], "reason": r["reason"],
             "error_code": (orders.get(r["customer_order_ref"]) or {}).get("error_code"), "halt": r["halt"]}
            for r in results], "market_halts_cleared": cleared,
            "unresolved": [{"customer_order_ref": ref, "market_id": o["market_id"], "state": o["state"],
                            "bet_id": o["bet_id"], "error_code": o["error_code"]}
                           for ref, o in sorted(orders.items()) if o["state"] in UNRESOLVED_STATES],
            "journal_seq": [self._seqs[0], self._seqs[-1]] if self._seqs else None}

    def _clear_reconciled_market_halts(self):
        """Market-scope HALTs (found_order_inconsistent, matched_size_known_never_absent, a legacy
        unresolved_replace and the like) last until that market is reconciled: once it holds no unresolved
        order or provisional child, the operator's reconcile (or settle-market) journals a market resume.
        A global HALT is never cleared here; it needs resume (token and typed phrase)."""
        halted = set()
        for event in self.journal.events():
            scope = event["payload"].get("scope")
            if event["kind"] == "halt" and scope == "market" and event["market_id"] is not None:
                halted.add(event["market_id"])
            elif event["kind"] == "resume" and scope == "market":
                halted.discard(event["market_id"])
        state = self._state()
        busy = {o["market_id"] for o in state["orders"].values()
                if o["state"] in UNRESOLVED_STATES or (o["provisional_child"] is True and o["state"] != "RECORDED")}
        cleared = sorted(halted - busy)
        for market_id in cleared:
            self._append("resume", {"scope": "market", "reason": "market_reconciled"}, market_id=market_id)
        return cleared

    def _startup_unclean_for(self, market_id):
        """Why this open()'s startup reconcile cannot back an ABSENT_ACCEPTED in market_id ([] if it can):
        no startup pass, a read that failed, or a foreign or contradicting bet in that market."""
        startup = self._startup
        if not isinstance(startup, dict):
            return ["startup_reconcile_missing"]
        reasons = []
        if startup.get("foreign") is None or startup.get("read_failures"):
            reasons.append("startup_read_failed")
        for key, code in (("foreign", "foreign_bet_in_market"), ("contradictions", "contradicting_bet_in_market")):
            if any(item.get("market_id") == market_id for item in startup.get(key) or ()):
                reasons.append(code)
        return reasons

    def _accept(self, ref, confirm_token):
        state = self._state()
        order = state["orders"].get(ref)
        if order is None:
            raise OrderRefused("accept_unknown_order")
        if order["size_matched_p"] > 0:
            # Part of it is known to have matched, so the bet exists: it can never be accepted as absent.
            raise OrderRefused("accept_order_has_matched_size")
        if self.mode == "paper":
            if order["state"] not in ("UNKNOWN", "ABSENT_UNCONFIRMED"):
                raise OrderRefused("accept_needs_unresolved_order")
            if order["state"] == "UNKNOWN":
                self._append_moves("reconcile", {"reason": "paper_operator_accept"},
                                   {ref: ("ABSENT_UNCONFIRMED", {})}, reason="paper_operator_accept",
                                   market_id=order["market_id"])
            self._append_moves("operator_accept", {"echo": None}, {ref: ("ABSENT_ACCEPTED", {})},
                               reason="paper_operator_accept", market_id=order["market_id"])
            return {"mode": "paper", "status": "accepted", "customer_order_ref": ref}
        if order["state"] != "ABSENT_UNCONFIRMED":
            raise OrderRefused("accept_needs_absent_unconfirmed")
        if not token_matches(self.config, confirm_token, self.clock.time_ns()):
            raise NotArmed("confirm_token_mismatch")
        phrase = f"ACCEPT ABSENT {ref}"
        self._take_confirmations([phrase], None)          # typed before open() took the lock
        unclean = self._startup_unclean_for(order["market_id"])
        if unclean:
            raise OrderRefused("accept_startup_not_clean", detail={"reasons": unclean})
        result = self._reconcile_one(order, state)
        if not result["clean"] or result["final_state"] != "ABSENT_UNCONFIRMED":
            raise OrderRefused("accept_final_search_not_clean", detail={"final_state": result["final_state"]})
        self._append_moves("operator_accept", {"echo": phrase}, {ref: ("ABSENT_ACCEPTED", {})},
                           reason="operator_accept", market_id=order["market_id"])
        cleared = self._clear_reconciled_market_halts()
        return {"mode": "live", "status": "accepted", "customer_order_ref": ref, "market_halts_cleared": cleared,
                "journal_seq": [self._seqs[0], self._seqs[-1]] if self._seqs else None}

    # --- settle-market (live, by hand; amendment A3 section 3.1) ---------------------------------

    def _settle_listing(self, state, market_id):
        """P0 on one fold of the journal: (shown, to_settle) or OrderRefused(settle_market_*).

        ours: every order in market_id except RECORDED and REFUSED (never sent). Refused when there are
        none, when any is unresolved (or a provisional child), or when none is EXECUTABLE or
        EXECUTION_COMPLETE. shown is what the operator is shown and its sha256 is bound to the typed line:
        one row per order of ours, the market's worst case counted now (L_M) and the sum of the per-order
        worst cases booked as realised loss (which can exceed L_M)."""
        ours = sorted((o for o in state["orders"].values()
                       if o["market_id"] == market_id and o["state"] not in ("RECORDED", "REFUSED")),
                      key=lambda o: o["customer_order_ref"])
        if not ours:
            raise OrderRefused("settle_market_no_orders_in_market")
        unresolved = [o["customer_order_ref"] for o in ours
                      if o["state"] in UNRESOLVED_STATES or o["provisional_child"] is True]
        if unresolved:
            raise OrderRefused("settle_market_unresolved_orders", detail={"refs": unresolved})
        to_settle = [o for o in ours if o["state"] in SETTLEABLE_STATES]
        if not to_settle:
            raise OrderRefused("settle_market_nothing_to_settle")
        rows = [{"customer_order_ref": o["customer_order_ref"], "bet_id": o["bet_id"], "side": o["side"],
                 "selection_id": o["selection_id"], "price_h": o["price_h"], "size_p": o["size_p"],
                 "size_matched_p": o["size_matched_p"], "size_remaining_p": o["size_remaining_p"],
                 "state": o["state"],
                 "settled_worst_loss_pence": settled_worst_loss_pence(o) if o["state"] in SETTLEABLE_STATES else None}
                for o in ours]
        exposure = exposure_by_market(list(state["orders"].values())).get(market_id, 0)
        shown = {"market_id": market_id, "orders": rows, "worst_case_exposure_pence": exposure,
                 "booked_realized_loss_pence": sum(settled_worst_loss_pence(o) for o in to_settle)}
        return shown, to_settle

    @staticmethod
    def _settle_banner(shown):
        def gbp(value):
            return f"\u00a3{value // 100}.{value % 100:02d}"

        def two_dp(value):
            return f"{value // 100}.{value % 100:02d}"

        lines = ["MODE=LIVE: SETTLE MARKET BY HAND. No order is sent.",
                 "Do this only after the market has settled: check this market's settled bets on the Betfair "
                 "website (or in Bet Angel) first.",
                 "After you type, settle-market checks that Betfair shows the market CLOSED and none of our bets "
                 "in it as current; if not, it refuses and nothing is settled.",
                 "Our orders in this market (journal):"]
        for row in shown["orders"]:
            worst = ("worst-case loss " + gbp(row["settled_worst_loss_pence"])
                     if row["settled_worst_loss_pence"] is not None else "not settled now")
            lines.append(f"  {row['customer_order_ref']} bet {row['bet_id'] or 'none'} {row['side']} "
                         f"sel {row['selection_id']} @ {two_dp(row['price_h'])} size {two_dp(row['size_p'])} "
                         f"matched {two_dp(row['size_matched_p'])} remaining {two_dp(row['size_remaining_p'])} "
                         f"{row['state']} {worst}")
        lines += ["Worst-case exposure counted now in this market (no hedge credit): "
                  + gbp(shown["worst_case_exposure_pence"]),
                  "Booked as today's (UTC) realised loss for the daily cap: " + gbp(shown["booked_realized_loss_pence"])
                  + " (each order's worst case added up; it can exceed the line above)",
                  "Orders the kill cancelled, or whose cancel report was lost, are still counted here and are "
                  "booked at worst case.",
                  "Betfair's settled P&L is not used: see orders-cleared or the Betfair website.",
                  "Settling stops counting these orders as open exposure. It cannot be undone."]
        return "\n".join(lines)

    def _preconfirm_settle(self, market_id):
        """settle-market's typed line, before the lock: P0 on a read-only fold, then the banner and
        SETTLE MARKET <M>. The sha256 of what was shown is kept with the line for settle_market()."""
        if not isinstance(market_id, str) or not MARKET_ID_RE.fullmatch(market_id):
            raise ValueError("market_id must look like 1.<digits>")
        shown, _ = self._settle_listing(self.journal.fold(now_ns=self.clock.time_ns()), market_id)
        line = f"SETTLE MARKET {market_id}"
        self.confirmer.confirm_line(line, banner=self._settle_banner(shown))
        self._settle_typed[line] = canonical_sha256(shown)
        return line

    def _market_status(self, market_id):
        """P2: one single-id listMarketBook; "CLOSED" or OrderRefused(settle_market_*)."""
        try:
            rows = self.read.list_market_book([market_id], price_data=())
        except (BetfairError, ValueError) as exc:
            raise OrderRefused("settle_market_book_read_failed", detail={
                "code": safe_code(getattr(exc, "code", None) or type(exc).__name__)}) from None
        if isinstance(rows, list) and not rows:
            raise OrderRefused("settle_market_book_missing")
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
        status = row.get("status") if row is not None and row.get("marketId") == market_id else None
        if status != "CLOSED":
            raise OrderRefused("settle_market_not_closed",
                               detail={"status": safe_code(status) if isinstance(status, str) and status else None})
        return status

    def _settle_bets_current(self, market_id, state, ours):
        """P3: none of our bets current in the market, read by our strategy refs in the market and by the
        journal's betIds (both legs, batched); a read error or any item refuses. Premise, inferred (F77):
        listCurrentOrders stops listing a bet once its market is settled."""
        refs = sorted({self.strategy_ref} | set(state["strategy_refs_seen"]))
        bets = sorted({o["bet_id"] for o in ours if o["bet_id"] is not None})
        items = []
        try:
            for start in range(0, len(refs), MAX_IDS_PER_REQUEST):
                items.extend(self.read.iter_current_orders(
                    customer_strategy_refs=refs[start:start + MAX_IDS_PER_REQUEST], market_ids=[market_id],
                    order_projection="ALL"))
            for start in range(0, len(bets), MAX_IDS_PER_REQUEST):
                items.extend(self.read.iter_current_orders(bet_ids=bets[start:start + MAX_IDS_PER_REQUEST],
                                                           order_projection="ALL"))
        except (BetfairError, ValueError) as exc:
            raise OrderRefused("settle_market_current_read_failed", detail={
                "code": safe_code(getattr(exc, "code", None) or type(exc).__name__)}) from None
        if items:
            listed = sorted({item.get("betId") for item in items if isinstance(item, dict)
                             and isinstance(item.get("betId"), str) and item["betId"].isdigit()
                             and item["betId"].isascii()})
            raise OrderRefused("settle_market_bets_still_current", detail={"count": len(items), "bet_ids": listed})

    def settle_market(self, market_id, *, confirm_token):
        """Live only: the operator settles one CLOSED market by hand (amendment A3, D3).

        After open() (the line was typed before the lock, preconfirm): the token, the typed line and the
        sha256 of what was shown; P0 on a fresh fold (the listing must be the one shown); P2 the market
        is CLOSED on listMarketBook; P3 none of our bets is current in it. Then one settle event,
        compare-and-appended on the head the fold was taken at, moves every EXECUTABLE and
        EXECUTION_COMPLETE order of ours in the market to SETTLED with no profit, so each is booked at
        its worst case (journal.settled_worst_loss_pence) as today's realised loss. Betfair's P&L is
        neither read nor journaled; no order is sent."""
        if self.mode != "live":
            raise OrderRefused("settle_market_is_live_only")
        self._seqs = []
        self._require_open("settle-market")
        self._require_armed()
        if not token_matches(self.config, confirm_token, self.clock.time_ns()):
            raise NotArmed("confirm_token_mismatch")
        line = f"SETTLE MARKET {market_id}"
        shown_sha256 = self._settle_typed.pop(line, None)
        if shown_sha256 is None:
            raise OrderRefused("operator_confirmation_missing")
        basis = self.journal.anchor()
        state = self._state()
        shown, to_settle = self._settle_listing(state, market_id)
        if canonical_sha256(shown) != shown_sha256:
            raise OrderRefused("settle_market_journal_changed")
        status = self._market_status(market_id)
        ours = [o for o in state["orders"].values()
                if o["market_id"] == market_id and o["state"] not in ("RECORDED", "REFUSED")]
        self._settle_bets_current(market_id, state, ours)
        transitions = [{"customer_order_ref": o["customer_order_ref"], "state": "SETTLED",
                        "reason": "operator_settle_market",
                        "fields": {"settled_profit_own_p": None, "settled_profit_betfair_p": None}}
                       for o in to_settle]
        payload = {"basis": SETTLE_BASIS, "echo": line, "shown_sha256": shown_sha256, "market_status": status,
                   "commission_p": 0, "transitions": transitions}
        try:
            self._append("settle", payload, expect_head=basis, market_id=market_id)
        except JournalError as exc:
            if "journal_head_moved" in str(exc):
                raise OrderRefused("settle_market_journal_changed") from None
            raise
        cleared = self._clear_reconciled_market_halts()
        orders = self._state()["orders"]
        return {"mode": "live", "status": "settled", "market_id": market_id,
                "orders": [_order_view(orders[o["customer_order_ref"]]) for o in to_settle],
                "worst_case_exposure_before_pence": shown["worst_case_exposure_pence"],
                "booked_realized_loss_pence": shown["booked_realized_loss_pence"], "market_halts_cleared": cleared,
                "journal_seq": [self._seqs[0], self._seqs[-1]]}

    # --- kill / resume / status ----------------------------------------------------------------

    def _journal_kill(self, reason, scope):
        """kill + halt events, unless the journal is already halted by a kill since the last resume. Best
        effort: a journal that cannot be read or folded gets them anyway (when it can take them at all)."""
        try:
            state = self._state()
            if state["halted"] and "kill" in state["halt_reasons"] and scope == "own":
                return
        except Exception:          # never stops the kill (amendment A3, D4)
            pass
        self._append("kill", {"reason": safe_code(reason) if reason else "kill", "scope": scope})
        self._halt("kill")

    def kill(self, *, scope="own", reason, confirm_account_wide=False, engaged_monotonic_ns=None, progress=None):
        """Engage the kill file and HALT first; then (live) sweep our own bets, and with scope "account"
        afterwards send the account-wide cancel. The own sweep always runs first, so nothing about the
        account-wide step (a missing flag, a mistyped phrase, a refusal or a failed cancel) can stop
        our own bets being cancelled; any such outcome makes the kill kill_incomplete instead.

        Live (amendment A3, D4): no lock is held or awaited. engaged_monotonic_ns is T0, read right after
        the CLI engaged the KILL file; it is now (after this call's own engage) when None or when this
        call is the one that created the file. The re-sweeps run at T0 + 20 s and T0 + 35 s. Every cancel
        runs on the no-orders stand-in, so the kill journals cancel_intent, submit and report with no order
        moves: the journal keeps counting our bets at their worst case until an order report, reconcile
        --accept or settle-market says otherwise. An untrusted journal (open() could not verify,
        anchor-check or fold it, or the account is not the journal's) sweeps the config strategy ref only
        and always ends kill_incomplete. A kill event the journal cannot take stops the kill's later
        journal writes, never its cancels, and makes it kill_incomplete (journal_write_failed). A kill that
        ends incomplete journals HALT kill_incomplete when it can (journal_halt_kill_incomplete).
        progress(text) gets the operator's lines."""
        self._seqs = []
        if scope not in ("own", "account"):
            raise ValueError("scope must be own or account")
        try:
            engaged = engage(self.paths, reason, clock=self.clock)
        except OSError as exc:
            # The HALT and the sweep still run; without the KILL file the kill is never complete.
            engaged = {"status": "kill_file_not_engaged", "error": safe_code(type(exc).__name__)}
        if type(engaged_monotonic_ns) is not int or engaged.get("status") == "kill_engaged":
            # No T0 given, or this call created the KILL file (the CLI's engage failed or the file was removed
            # meanwhile): orders could be sent until now, so the re-sweeps are timed from now.
            engaged_monotonic_ns = self.clock.monotonic_ns()
        file_failed = engaged.get("status") == "kill_file_not_engaged"
        self._journal_kill(engaged.get("reason", "kill"), scope)
        if self.mode == "paper":
            return {"mode": "paper", "status": "kill_incomplete" if file_failed else "kill_complete", "kill": engaged,
                    "note": "paper_has_no_exchange_orders"}
        wide = scope == "account" and confirm_account_wide is True
        if wide:
            self._require_open("kill-account")          # A11 (a terminal) was checked when it opened
        else:
            self._require_open("kill", "kill-account")
        state = None if self._journal_untrusted else self._fold_or_none()
        if state is None:
            self._journal_untrusted = True
            state = self._no_orders_state()              # no strategy_refs_seen and no betIds: config ref only
            self._append("note", {"reason": UNTRUSTED_SWEEP_NOTE})     # best effort: never stops the sweep
        refs = sorted({self.strategy_ref} | set(state["strategy_refs_seen"]))
        standin = self._no_orders_state()

        def cancel_fn(market_id, bet_ids, kill_targets):
            return self._cancel(market_id, [{"bet_id": b, "size_reduction_p": None} for b in bet_ids],
                                kill_targets=kill_targets, reconcile=False, state=standin)

        out = {"mode": "live", "kill": engaged, "scope": scope}
        out.update(sweep(self.read, state, cancel_fn=cancel_fn, strategy_refs=refs, clock=self.clock,
                         engaged_monotonic_ns=engaged_monotonic_ns, progress=progress))
        out["notes"] = sorted(set(out["notes"]) | set((self._report or {}).get("notes") or ()))     # why, from open()
        if self._journal_untrusted:
            out["notes"] = sorted(set(out["notes"]) | {UNTRUSTED_SWEEP_NOTE})
            out["status"] = "kill_incomplete"            # our bets cannot be fully enumerated
        if scope == "account":
            out["account_wide"] = self._account_wide(wide, standin)
            if out["account_wide"].get("status") != "SUCCESS" or out["account_wide"].get("exception") is not None:
                out["status"] = "kill_incomplete"
        if file_failed or self._kill_write_failed is not None:
            out["status"] = "kill_incomplete"
        if out["status"] != "kill_complete":
            halted = self._halt("kill_incomplete")
            out["journal_halt_kill_incomplete"] = "appended" if halted is not None else "failed"
        if self._kill_write_failed is not None:
            out["journal_write_failed"] = self._kill_write_failed
            out["notes"] = sorted(set(out["notes"]) | {"kill_journal_write_failed"})
        return out

    def _account_wide(self, confirmed, standin):
        """The account-wide cancel as a result dict; it never raises past the kill (spec section 9.8 step 7)."""
        if not confirmed:
            return {"status": "not_sent", "reason": "account_wide_needs_confirm_flag", "exception": None}
        try:
            return self._cancel_all(standin)
        except BetfairError as exc:
            return {"status": "not_sent", "exception": None,
                    "reason": safe_code(getattr(exc, "rule", None) or getattr(exc, "code", None)
                                        or type(exc).__name__)}
        except Exception as exc:          # journaled by _send when it came from the send; never a traceback
            return {"status": "not_sent", "reason": "unexpected_error", "exception": _exception_payload(exc)}

    def _cancel_all(self, standin):
        """One account-wide cancel (no marketId, no instructions), lock-free like the rest of the kill. No
        journal scan for an earlier unreported one: Betfair rejects a concurrent cancel-all or answers
        ERROR_IN_MATCHER (F31), which ends this kill kill_incomplete. It moves no order (stand-in state)."""
        self.confirmer.confirm_line(ACCOUNT_WIDE_PHRASE, banner=ACCOUNT_WIDE_BANNER)
        now_ns = self.clock.time_ns()
        cref = make_customer_ref(now_ns)
        intent_id = _intent_id()
        decision = self.guard.evaluate_cancel(None, [], state=standin, now_ns=now_ns, account_wide=True,
                                              customer_ref=cref)
        self._append("cancel_intent", dict(_decision_payload(decision), account_wide=True, echo=ACCOUNT_WIDE_PHRASE),
                     intent_id=intent_id, customer_ref=cref)
        if not decision["approved"]:
            raise OrderRefused(decision["rule"] or "account_wide_cancel_refused")
        prepared = self.rpc.prepare_order(CANCEL_METHOD, {"customerRef": cref})
        self._append("submit", {"action": "cancel_all", "instruction_count": 0, "body_sha256": prepared["body_sha256"],
                                "decision_id": decision["decision_id"]}, intent_id=intent_id, customer_ref=cref)

        def journal_report(result, exc):
            status = None
            if exc is None and isinstance(result, dict):
                status = result.get("status") if result.get("status") in EXECUTION_STATUSES else "unparseable"
            payload = {"action": "cancel_all", "status": status, "error_code": _code(result.get("errorCode"))
                       if isinstance(result, dict) else None,
                       "exception": _exception_payload(exc) if exc is not None else None,
                       "transactions": report_transactions("cancel_all", result if isinstance(result, dict) else None,
                                                           sent_count=1)}
            self._append("report", _with_halts(payload, self._report_halts(None, exc) if exc is not None else []),
                         intent_id=intent_id)
            return payload

        def mint():
            return self.guard.issue(decision, prepared, now_monotonic_ns=self.clock.monotonic_ns())

        return self._send(prepared, journal_report, mint=mint)

    def resume(self, *, confirm_token=None):
        """Clear the journal HALT (see resume_journal); needs no login. Refused on an open gateway: a live
        resume takes the global lock itself."""
        if self._command is not None:
            raise OrderRefused("resume_needs_a_closed_gateway")
        self._seqs = []
        result = resume_journal(self.config, paths=self.paths, mode=self.mode, journal=self.journal,
                                confirmer=self.confirmer, confirm_token=confirm_token,
                                keychain_runner=self.keychain_runner, env=self.env, clock=self.clock)
        self._seqs.append(result["journal_seq"])
        return result

    def status(self):
        """Offline summary: kill (fail-closed), HALT, unresolved orders, rates and adversarial exposure."""
        now_ns = self.clock.time_ns()
        state = self.journal.fold(now_ns=now_ns)
        counts = {}
        for order in state["orders"].values():
            counts[order["state"]] = counts.get(order["state"], 0) + 1
        # Every order, as RiskGuard's R8-R10 count them: a REPLACED bet's matched part is at risk until it
        # settles, and live gives a matched portion no hedge credit (amendment A3, C1).
        exposure = exposure_by_market(list(state["orders"].values()), hedge_credit=(self.mode == "paper"))
        minute = sum(1 for t in state["submitted_instruction_wall_ns"] if t > now_ns - 60 * _NS_PER_S)
        hour = sum(1 for t in state["submitted_instruction_wall_ns"] if t > now_ns - 3600 * _NS_PER_S)
        return {"mode": self.mode, "kill_engaged": kill_engaged(self.paths), "halted": state["halted"],
                "halt_reasons": state["halt_reasons"], "clock_regression": state["clock_regression"],
                "unresolved_markets": state["unresolved_markets"], "orders_by_state": dict(sorted(counts.items())),
                "rates": {"last_minute": minute, "last_hour": hour, "today": state["orders_today"],
                          "transactions_by_clock_hour": state["transactions_by_clock_hour"]},
                "exposure_pence_by_market": exposure, "exposure_pence_total": sum(exposure.values()),
                "exposure_basis": STATUS_EXPOSURE_BASIS[self.mode],
                "realized_today_pence": state["realized_today_pence"],
                "ref_roundtrip_verified": state["ref_roundtrip_verified"],
                "config_sha256": config_sha256(self.config), "utc_date": utc_from_ns(now_ns)[:10],
                "max_stake_per_order_pence": money_pence(self.config["orders"]["caps"]["max_stake_per_order"])}


def resume_journal(config, *, paths, mode, journal, confirmer, confirm_token, keychain_runner, env, clock):
    """Append the resume event that clears the journal HALT (spec section 9.8).

    Refused while the kill file exists (the operator removes it by hand first). Live needs A2 (the
    token), A11 (a terminal) and A13 (the state root and anchor, including the anchored event's hash,
    so a rolled-back and re-extended journal is never resumed or re-anchored) on a journal whose
    chain verifies, then the typed phrase RESUME LIVE. No login and no network: the CLI's live
    resume needs no credentials.

    The prompt shows the journal head and the halt reasons being cleared, and is answered without
    any lock held (an unanswered prompt never blocks a kill). After the phrase, under the global
    gateway lock, the kill file is checked again and the resume is a compare-and-append on the head
    the operator saw: if anything was journaled meanwhile (a kill, a halt from another process) the
    resume is refused, and the event itself carries cleared_through_seq, so the fold clears only
    what was shown. A kill engaged after that check still leaves a HALT after the resume.
    """
    if kill_engaged(paths):
        raise Halted("kill_file_still_present")
    if journal.mode != mode:
        raise ConfigError("resume_journal_mode_mismatch")
    seen = journal.anchor()                          # the head whose halts this resume clears
    if mode != "live":
        event = _append_resume(journal, seen)
        state = journal.fold(now_ns=clock.time_ns())
        return {"mode": mode, "status": "resumed", "halted": state["halted"], "journal_seq": event["seq"],
                "cleared_through_seq": seen.get("seq", 0)}
    if journal.verify()["status"] != "ok":
        raise NotArmed("journal_chain_broken")
    try:
        state = journal.fold(now_ns=clock.time_ns())
    except JournalError:
        state = None
    report = static_arming(config, command="resume", paths=paths, cli_mode="live", confirm_token=confirm_token,
                           bet_angel_paused=False, env=dict(os.environ) if env is None else env,
                           confirmer=confirmer, keychain_runner=keychain_runner, journal_anchor=seen,
                           now_ns=clock.time_ns(), journal_state=state, journal=journal)
    if not report["armed"]:
        raise NotArmed(report["failures"][0], detail={"failures": report["failures"]})
    reasons = list(state["halt_reasons"]) if state is not None else ["journal_unfoldable"]
    banner = ("MODE=LIVE: RESUME sends no order. It clears the journal HALT as of journal seq "
              f"{seen.get('seq', 0)} (halt reasons: {', '.join(safe_code(r) for r in reasons) or 'none'}); after "
              "it, live place and live-probe can send orders again once every other arming condition passes.")
    confirmer.confirm_line(RESUME_PHRASE, banner=banner)
    timeout_s = config["http"]["order_deadline_s"] + 5
    with file_lock(paths.gateway_lock, timeout_s=timeout_s, clock=clock):
        if kill_engaged(paths):
            raise Halted("kill_engaged_during_resume_prompt")
        event = _append_resume(journal, seen)
        if kill_engaged(paths):
            # Engaged between the check and the append: keep a HALT after this resume.
            journal.append("halt", {"reason": "kill", "scope": "global"})
            raise Halted("kill_engaged_during_resume_prompt")
    state = journal.fold(now_ns=clock.time_ns())
    return {"mode": mode, "status": "resumed", "halted": state["halted"], "journal_seq": event["seq"],
            "cleared_through_seq": seen.get("seq", 0), "cleared_halt_reasons": reasons}


def _append_resume(journal, seen):
    """The global resume as a compare-and-append on the head the operator saw."""
    try:
        return journal.append("resume", {"scope": "global", "cleared_through_seq": seen.get("seq", 0)},
                              expect_head=seen)
    except JournalError as exc:
        if "journal_head_moved" in str(exc):
            raise Halted("journal_changed_during_resume_prompt") from None
        raise
