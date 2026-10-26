"""Append-only, hash-chained SQLite order journal, and the fold that rebuilds order state from it.

Every append runs inside BEGIN IMMEDIATE, reads the chain head inside that transaction, and
commits (WAL, synchronous=FULL, fullfsync, checkpoint_fullfsync) before returning, so a submit
event is durable before any byte goes to the network and appends from several processes cannot
fork the chain. UPDATE and DELETE are refused by triggers on both tables. Payloads pass
refuse_forbidden and assert_clean, so credentials never reach the file.

append() also applies the event to the fold of everything already committed (an incremental
JournalFolder kept per connection and caught up inside the same transaction) and refuses it with
JournalError if the fold would: an illegal or inconsistent move, an unknown order, or a bad intent
is never committed, so one bad write can never make the append-only journal unfoldable. Only if
the committed prefix itself cannot be folded (tampering, or a file written by older code) are
appends limited to events that move no order: kill, halt and note, plus the kill sweep's own
cancel_intent, cancel submit and report events, so the kill switch still works.

append(..., expect_head=anchor) is a compare-and-append: inside the same BEGIN IMMEDIATE it
refuses (journal_head_moved) unless the head is still the {seq, last_sha256} the caller saw (an
empty dict for an empty journal). The resume command and every place or replace submit use it,
so nothing another writer journaled after the operator or the risk guard looked is overruled.

Event conventions read by fold_events (spec section 9.6):
- intent: one per order; columns intent_id, customer_order_ref, customer_ref, market_id;
  payload {"order": instruction v1, "customer_strategy_ref", "is_probe", "parent_ref",
  "provisional_child", "source"}. Creates the order in RECORDED. A provisional replace child
  must match its parent's market, selection, side and handicap, size at most the parent's
  remaining size, and its parent must be EXECUTABLE or REPLACE_PENDING.
- Any event may move orders: the columns customer_order_ref + state_after (with optional
  payload "reason" and "fields"), and/or payload "transitions": [{"customer_order_ref",
  "state", "reason", "fields"}]. Each move must be legal in lifecycle.TRANSITIONS. A stored
  SUBMITTING order reads as UNKNOWN (crash_between_submit_and_report) once its process is gone,
  so a move to LAPSED or ABSENT_UNCONFIRMED (legal only from UNKNOWN) is applied through UNKNOWN;
  a gateway may also journal the SUBMITTING -> UNKNOWN move explicitly at open().
- submit: payload {"action": place|replace|cancel|cancel_all, "instruction_count", "body_sha256",
  ...}. Place and replace instructions are counted for the rate windows and the transaction
  hour whatever their outcome.
- Any other event may add payload "transactions" (failed cancels) to its clock hour.
- report and risk_decision may carry payload "halts": [reason, ...] (amendment A3, R3#13): each
  reason halts globally in the same event as the moves that triggered it (a report's automatic
  HALT codes, three or more consecutive unknown outcomes, a risk decision's halt), so no process
  death between two appends can lose the HALT.
- Paper journals only: report, settle and note may carry payload "paper_state" {"schema_version": 2,
  "queues", "books", "exchange", "closed"} (amendment A3, R3#12): the paper exchange's state after the
  fills that event records, so fills and the state that prevents filling them twice are one durable
  write. "closed" is {"markets": {market_id: closed_ms}, "floor_ms": int or null}, the markers that
  outlive a closed market's dropped book (re-review R2; see the gateway). schema_version 1 (no
  "closed") is what code before R2 wrote; it is still read, never written. The fold ignores the
  paper state; the gateway opens on the latest one (last_paper_state()).
- funds_check {"funds"}, startup_reconcile {"exchange_orders_today",
  "exchange_realized_today_pence"}, halt {"reason", "scope": global|market} (anything but a
  market scope with a market_id column halts globally), kill, resume {"scope": "global"} or
  {"scope": "market"} with the market_id column (required; anything else is refused, and the
  fold never treats an unrecognised resume as a global clear). A global resume may carry
  "cleared_through_seq" (an integer below its own seq): it then clears only the halts and kills
  journaled at or below that seq, so a HALT written while the operator was typing RESUME LIVE
  survives the resume. settle {"commission_p": int >= 0
  (required; an unknown commission is journaled at its worst case), transitions to
  SETTLED/VOIDED}; live settle events come only from settle-market and carry "basis"
  ("operator_settled_worst_case"), the typed "echo", the listing's "shown_sha256" and
  "market_status" ("CLOSED"), with no profit: a SETTLED order without a profit counts its whole
  worst case (settled_worst_loss_pence) as realised loss. ref_roundtrip_verified.
"""

import copy
import os
from pathlib import Path
import re
import secrets
import sqlite3

from .errors import JournalError
from .filters import MARKET_ID_RE
from .lifecycle import TRANSITIONS, UNRESOLVED_STATES, exposure_basis, transition
from .receipts import SYSTEM_CLOCK, canonical_json, canonical_sha256, parse_utc_ns, strict_json_loads, utc_from_ns
from .schemas import ORDER_STATES, validate_instruction_v1, validate_order_record
from .secret import assert_clean, refuse_forbidden

SCHEMA_VERSION = 1
JOURNAL_MODES = ("paper", "live")
KINDS = ("intent", "risk_decision", "operator_confirm", "submit", "report", "reconcile", "cancel_intent",
         "replace_intent", "funds_check", "startup_reconcile", "halt", "resume", "kill", "settle",
         "probe_result", "ref_roundtrip_verified", "operator_accept", "note")
SUBMIT_ACTIONS = ("place", "replace", "cancel", "cancel_all")
SETTLE_BASES = ("operator_settled_worst_case",)
# Event kinds whose payload may carry "halts" (global HALT reasons applied with the event itself).
HALT_PAYLOAD_KINDS = ("report", "risk_decision")
# Event kinds whose payload may carry "paper_state" (paper journals only), and its exact keys.
PAPER_STATE_KINDS = ("report", "settle", "note")
PAPER_STATE_KEYS = frozenset({"schema_version", "queues", "books", "exchange"})       # schema_version 1
PAPER_STATE_V2_KEYS = PAPER_STATE_KEYS | {"closed"}                                  # schema_version 2 (R2)
PAPER_CLOSED_KEYS = frozenset({"markets", "floor_ms"})
GENESIS_PREV = "0" * 64
CLOCK_REGRESSION_NS = 5_000_000_000
DAY_NS = 86_400_000_000_000
INTENT_PAYLOAD_KEYS = frozenset({"order", "customer_strategy_ref", "is_probe", "parent_ref", "provisional_child",
                                 "source"})
# "pilot": the registered pilot executor's orders (RS-19, engineering note §2 E3), live journals only (append).
INTENT_SOURCES = ("cli_manual", "api", "paper_replay", "pilot")
EVENT_COLUMNS = ("seq", "event_id", "recorded_at", "monotonic_ns", "kind", "mode", "intent_id",
                 "customer_order_ref", "customer_ref", "bet_id", "market_id", "state_after", "payload_json",
                 "prev_sha256", "event_sha256")
FOLD_KEYS = frozenset({
    "orders", "halted", "halt_reasons", "unresolved_markets", "submitted_instruction_wall_ns",
    "transactions_by_clock_hour", "orders_today", "realized_today_pence", "unsettled_matched_liability_today_pence",
    "strategy_refs_seen", "ref_roundtrip_verified", "last_submit_wall_ns", "consecutive_unknown",
    "clock_regression", "funds", "funds_checked_at", "exchange_floor", "settled_matched_liability_today_pence"})
RESUME_SCOPES = ("global", "market")
# Event kinds that may still be appended (without order moves) when the committed prefix cannot be
# folded: the kill switch's own events, and the cancel requests its sweep journals before sending.
SAFETY_KINDS = ("kill", "halt", "note", "cancel_intent", "report")
SAFETY_SUBMIT_ACTIONS = ("cancel", "cancel_all")
_CHILD_PARENT_STATES = ("EXECUTABLE", "REPLACE_PENDING")

# What a damaged committed event (a payload of the wrong shape, invalid JSON, ...) can raise while it is folded.
_FOLD_ERRORS = (TypeError, KeyError, ValueError, ArithmeticError, AttributeError)
_KIND_LIST = ",".join(f"'{k}'" for k in KINDS)
SCHEMA_STATEMENTS = (
    "CREATE TABLE journal_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE events ("
    " seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, recorded_at TEXT NOT NULL,"
    " monotonic_ns INTEGER NOT NULL,"
    f" kind TEXT NOT NULL CHECK (kind IN ({_KIND_LIST})),"
    " mode TEXT NOT NULL CHECK (mode IN ('paper','live')),"
    " intent_id TEXT, customer_order_ref TEXT, customer_ref TEXT, bet_id TEXT, market_id TEXT, state_after TEXT,"
    " payload_json TEXT NOT NULL, prev_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL)",
    "CREATE UNIQUE INDEX one_intent_per_order_ref ON events(customer_order_ref) WHERE kind='intent'",
    "CREATE INDEX events_ref ON events(customer_order_ref)",
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events"
    " BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events"
    " BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END",
    "CREATE TRIGGER journal_meta_no_update BEFORE UPDATE ON journal_meta"
    " BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END",
    "CREATE TRIGGER journal_meta_no_delete BEFORE DELETE ON journal_meta"
    " BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END",
)
_SELECT_SQL = "SELECT " + ", ".join(EVENT_COLUMNS) + " FROM events"
_INSERT_SQL = ("INSERT INTO events (" + ", ".join(EVENT_COLUMNS) + ") VALUES ("
               + ", ".join("?" * len(EVENT_COLUMNS)) + ")")
REQUIRED_TABLES = frozenset({"journal_meta", "events"})
REQUIRED_TRIGGERS = frozenset({"events_no_update", "events_no_delete", "journal_meta_no_update",
                               "journal_meta_no_delete"})
REQUIRED_INDEXES = frozenset({"one_intent_per_order_ref", "events_ref"})

_INTENT_ID_RE = re.compile(r"[A-Za-z0-9_\-.:]{1,80}")
_ORDER_REF_RE = re.compile(r"[A-Za-z0-9-]{1,32}")
_CUSTOMER_REF_RE = re.compile(r"[A-Za-z0-9:\-._+*;~]{1,32}")
_BET_ID_RE = re.compile(r"[0-9]{1,20}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")
_HALT_REASON_RE = re.compile(r"[A-Za-z0-9_\-.:]{1,80}")


def _check_column(value, pattern, name):
    if value is not None and (not isinstance(value, str) or not pattern.fullmatch(value)):
        raise JournalError(f"journal_column_invalid:{name}")


def _check_transitions(payload):
    moves = payload.get("transitions")
    if moves is None:
        return
    if not isinstance(moves, list):
        raise JournalError("journal_transitions_must_be_list")
    for move in moves:
        if not isinstance(move, dict) or not set(move) <= {"customer_order_ref", "state", "reason", "fields"}:
            raise JournalError("journal_transition_shape_invalid")
        _check_column(move.get("customer_order_ref"), _ORDER_REF_RE, "transition_ref")
        if move.get("customer_order_ref") is None or move.get("state") not in ORDER_STATES:
            raise JournalError("journal_transition_shape_invalid")
        if not isinstance(move.get("fields", {}), dict):
            raise JournalError("journal_transition_shape_invalid")


def _ms(value):
    return type(value) is int and value >= 0


def validate_paper_state(value):
    """The shape of a journaled paper state (the exchange part is checked by PaperExchange when loaded).

    schema_version 2 adds "closed" (re-review R2): {"markets": {market_id: closed_ms}, "floor_ms": null or
    int}, every time a non-negative integer of ms; schema_version 1 (written before R2) has no "closed"."""
    version = value.get("schema_version") if isinstance(value, dict) else None
    keys = {1: PAPER_STATE_KEYS, 2: PAPER_STATE_V2_KEYS}.get(version) if type(version) is int else None
    if (keys is None or set(value) != keys
            or not all(isinstance(value[key], dict) for key in keys - {"schema_version"})):
        raise JournalError("paper_state_invalid")
    if version == 2:
        closed = value["closed"]
        markets = closed.get("markets")
        if (set(closed) != PAPER_CLOSED_KEYS or not isinstance(markets, dict)
                or not (closed["floor_ms"] is None or _ms(closed["floor_ms"]))
                or not all(isinstance(m, str) and MARKET_ID_RE.fullmatch(m) and _ms(ms) for m, ms in markets.items())):
            raise JournalError("paper_state_invalid")
    return value


def _check_payload(kind, payload, columns):
    if kind == "intent":
        if set(payload) != INTENT_PAYLOAD_KEYS:
            raise JournalError("intent_payload_keys_must_be_exact")
        try:
            validate_instruction_v1(payload["order"])
        except ValueError:
            raise JournalError("intent_instruction_invalid") from None
        if (columns["intent_id"] is None or columns["customer_order_ref"] is None or columns["customer_ref"] is None
                or columns["market_id"] is None or columns["state_after"] not in (None, "RECORDED")):
            raise JournalError("intent_columns_incomplete")
        if not isinstance(payload["customer_strategy_ref"], str) or not re.fullmatch(
                r"[A-Za-z0-9-]{1,15}", payload["customer_strategy_ref"]):
            raise JournalError("intent_strategy_ref_invalid")
        if type(payload["is_probe"]) is not bool or type(payload["provisional_child"]) is not bool:
            raise JournalError("intent_flags_must_be_boolean")
        _check_column(payload["parent_ref"], _ORDER_REF_RE, "parent_ref")
        if payload["provisional_child"] != (payload["parent_ref"] is not None):
            raise JournalError("intent_provisional_child_needs_parent_ref")
        if payload["source"] not in INTENT_SOURCES:
            raise JournalError("intent_source_invalid")
    if kind == "submit":
        if payload.get("action") not in SUBMIT_ACTIONS:
            raise JournalError("submit_action_invalid")
        count = payload.get("instruction_count")
        if type(count) is not int or count < 0 or (payload["action"] in ("place", "replace") and count < 1):
            raise JournalError("submit_instruction_count_invalid")
        digest = payload.get("body_sha256")
        if digest is not None and (not isinstance(digest, str) or not _SHA_RE.fullmatch(digest)):
            raise JournalError("submit_body_sha256_invalid")
    if kind == "settle":
        commission = payload.get("commission_p")
        if type(commission) is not int or commission < 0:
            raise JournalError("settle_commission_p_must_be_non_negative_integer")
        # settle-market's typed confirmation rides in its settle event (no separate operator_confirm).
        if "basis" in payload and payload["basis"] not in SETTLE_BASES:
            raise JournalError("settle_basis_invalid")
        if "echo" in payload and (not isinstance(payload["echo"], str) or not payload["echo"]):
            raise JournalError("settle_echo_invalid")
        shown = payload.get("shown_sha256")
        if "shown_sha256" in payload and (not isinstance(shown, str) or not _SHA_RE.fullmatch(shown)):
            raise JournalError("settle_shown_sha256_invalid")
        if "market_status" in payload and payload["market_status"] != "CLOSED":
            raise JournalError("settle_market_status_invalid")
    if kind == "resume":
        scope = payload.get("scope")
        if scope not in RESUME_SCOPES:
            raise JournalError("resume_scope_invalid")
        if scope == "market" and columns["market_id"] is None:
            raise JournalError("resume_market_scope_needs_market_id")
        bound = payload.get("cleared_through_seq")
        if "cleared_through_seq" in payload and (scope != "global" or type(bound) is not int or bound < 0):
            raise JournalError("resume_cleared_through_seq_invalid")
    if "halts" in payload:
        halts = payload["halts"]
        if (kind not in HALT_PAYLOAD_KINDS or not isinstance(halts, list) or not halts
                or len(set(map(str, halts))) != len(halts)
                or not all(isinstance(r, str) and _HALT_REASON_RE.fullmatch(r) for r in halts)):
            raise JournalError("journal_halts_invalid")
    if "paper_state" in payload:
        if kind not in PAPER_STATE_KINDS:
            raise JournalError("paper_state_invalid")
        validate_paper_state(payload["paper_state"])
    extra = payload.get("transactions")
    if extra is not None and (type(extra) is not int or extra < 0 or kind == "submit"):
        raise JournalError("journal_transactions_invalid")
    _check_transitions(payload)


def prepare_event(kind, payload, *, intent_id=None, customer_order_ref=None, customer_ref=None, bet_id=None,
                  market_id=None, state_after=None):
    """Validate one event before it is written: (columns, canonical payload text, parsed payload).

    Refuses unknown kinds, malformed columns, credential-shaped keys or registered secret values
    anywhere, non-strict JSON (NaN, Infinity) and kind-specific payload shapes.
    """
    if kind not in KINDS:
        raise JournalError("journal_kind_invalid")
    if not isinstance(payload, dict):
        raise JournalError("journal_payload_must_be_object")
    _check_column(intent_id, _INTENT_ID_RE, "intent_id")
    _check_column(customer_order_ref, _ORDER_REF_RE, "customer_order_ref")
    _check_column(customer_ref, _CUSTOMER_REF_RE, "customer_ref")
    _check_column(bet_id, _BET_ID_RE, "bet_id")
    _check_column(market_id, MARKET_ID_RE, "market_id")
    if state_after is not None and state_after not in ORDER_STATES:
        raise JournalError("journal_column_invalid:state_after")
    if state_after is not None and customer_order_ref is None:
        raise JournalError("journal_state_after_needs_customer_order_ref")
    columns = {"intent_id": intent_id, "customer_order_ref": customer_order_ref, "customer_ref": customer_ref,
               "bet_id": bet_id, "market_id": market_id, "state_after": state_after}
    refuse_forbidden(payload, where="journal")
    try:
        payload_json = canonical_json(payload).decode("utf-8")
        payload = strict_json_loads(payload_json)
    except (TypeError, ValueError):
        raise JournalError("journal_payload_not_strict_json") from None
    _check_payload(kind, payload, columns)
    assert_clean(payload_json, where="journal")
    for value in columns.values():
        if value is not None:
            assert_clean(value, where="journal")
    return columns, payload_json, payload


class OrderJournal:
    """One journal file per mode. Opening checks the schema, triggers and stored mode.

    busy_timeout_s: how long a write waits for another connection's write transaction before SQLite
    raises sqlite3.OperationalError ("database is locked"). 30 s by default; the kill uses a short one
    (its events move no order, so a write it cannot make never stops a cancel, amendment A3 D4).
    Opening an existing journal reads only (WAL readers never wait for a writer); only a new file
    takes the write lock, to create the schema.
    """

    def __init__(self, path, *, mode, clock=SYSTEM_CLOCK, busy_timeout_s=30):
        if mode not in JOURNAL_MODES:
            raise JournalError("journal_mode_invalid")
        if isinstance(busy_timeout_s, bool) or not isinstance(busy_timeout_s, (int, float)) or not (
                0 < busy_timeout_s <= 30):
            raise JournalError("journal_busy_timeout_invalid")
        self.mode = mode
        self.clock = clock
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.path.exists():
            # Created 0600 before SQLite opens it; the -wal and -shm files inherit this mode.
            os.close(os.open(str(self.path), os.O_WRONLY | os.O_CREAT, 0o600))
        self._db = sqlite3.connect(str(self.path), isolation_level=None, timeout=busy_timeout_s)
        self._busy_timeout_ms = int(busy_timeout_s * 1000)
        self._fold_cache = None          # (seq, event_sha256, JournalFolder) of committed events
        try:
            self._configure()
            self._open_schema()
        except BaseException:
            self._db.close()
            raise

    # --- setup ------------------------------------------------------------------------------

    def _configure(self):
        db = self._db
        if db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise JournalError("journal_wal_unavailable")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA fullfsync=ON")
        db.execute("PRAGMA checkpoint_fullfsync=ON")
        db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")

    def _names(self, kind):
        return {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}

    def _open_schema(self):
        db = self._db
        if db.execute("PRAGMA user_version").fetchone()[0] != 0:
            # The schema was created (and is never changed afterwards): check it without the write lock, so
            # opening never waits behind another connection's append (a kill must not, amendment A3 D4).
            self._check_schema()
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            tables = self._names("table")
            if not tables and db.execute("PRAGMA user_version").fetchone()[0] == 0:
                for statement in SCHEMA_STATEMENTS:
                    db.execute(statement)
                now = self.clock.utc_now()
                db.executemany("INSERT INTO journal_meta (key, value) VALUES (?, ?)",
                               [("schema_version", str(SCHEMA_VERSION)), ("mode", self.mode), ("created_at", now)])
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        self._check_schema()

    def _check_schema(self):
        db = self._db
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise JournalError("journal_schema_version_mismatch")
        if not REQUIRED_TABLES <= self._names("table") or not REQUIRED_TRIGGERS <= self._names("trigger"):
            raise JournalError("journal_schema_incomplete")
        if not REQUIRED_INDEXES <= self._names("index"):
            raise JournalError("journal_schema_incomplete")
        columns = tuple(row[1] for row in db.execute("PRAGMA table_info(events)"))
        if columns != EVENT_COLUMNS:
            raise JournalError("journal_schema_incomplete")
        meta = self.meta()
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise JournalError("journal_schema_version_mismatch")
        if meta.get("mode") != self.mode:
            raise JournalError("journal_mode_mismatch")

    # --- reads ------------------------------------------------------------------------------

    def meta(self):
        return {key: value for key, value in self._db.execute("SELECT key, value FROM journal_meta")}

    def account_fingerprint(self):
        return self.meta().get("account_fingerprint")

    def _event(self, row):
        event = dict(zip(EVENT_COLUMNS, row))
        event["payload"] = strict_json_loads(event.pop("payload_json"))
        return event

    def events(self, since_seq=0):
        if type(since_seq) is not int or since_seq < 0:
            raise JournalError("since_seq_invalid")
        cursor = self._db.execute(_SELECT_SQL + " WHERE seq > ? ORDER BY seq", (since_seq,))
        for row in cursor:
            yield self._event(row)

    def verify(self):
        """Recompute the whole chain: {"status": "ok"|"broken", "events", "first_bad_seq"}."""
        previous, expected, count = GENESIS_PREV, 1, 0
        for row in self._db.execute(_SELECT_SQL + " ORDER BY seq"):
            raw = dict(zip(EVENT_COLUMNS, row))
            count += 1
            try:
                payload = strict_json_loads(raw["payload_json"])
                canonical = canonical_json(payload).decode("utf-8")
            except ValueError:
                return {"status": "broken", "events": count, "first_bad_seq": raw["seq"]}
            event = {k: v for k, v in raw.items() if k != "payload_json"}
            event["payload"] = payload
            if (raw["seq"] != expected or raw["prev_sha256"] != previous or raw["mode"] != self.mode
                    or canonical != raw["payload_json"] or _event_sha256(event) != raw["event_sha256"]):
                return {"status": "broken", "events": count, "first_bad_seq": raw["seq"]}
            previous, expected = raw["event_sha256"], expected + 1
        return {"status": "ok", "events": count, "first_bad_seq": None}

    def last_paper_state(self):
        """The payload "paper_state" of the latest event that carries one, or None (see the module docstring)."""
        for (text,) in self._db.execute("SELECT payload_json FROM events ORDER BY seq DESC"):
            payload = strict_json_loads(text)
            if "paper_state" in payload:
                return payload["paper_state"]
        return None

    def anchor(self):
        first = self._db.execute("SELECT event_sha256 FROM events ORDER BY seq LIMIT 1").fetchone()
        last = self._db.execute("SELECT seq, event_sha256 FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        if first is None:
            return {}
        return {"genesis_sha256": first[0], "seq": last[0], "last_sha256": last[1]}

    def head_fresh(self):
        """{"seq", "last_sha256"} of the committed head ({} when empty), read on a separate short-lived
        read-only connection (P1-06 Q3): this object's own connection can return a stale head while one of its
        events() generators is still open. Every read error propagates; the first-byte gate refuses on it."""
        db = sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None,
                             timeout=self._busy_timeout_ms / 1000)
        try:
            row = db.execute("SELECT seq, event_sha256 FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        finally:
            db.close()
        return {} if row is None else {"seq": row[0], "last_sha256": row[1]}

    def fold(self, *, now_ns, submitting_as_unknown=True):
        """fold_events of every committed event: the same state, caught up from this connection's committed-fold
        cache (the incremental folder append keeps) rather than refolded from event 1 (P1-08 timing; the pilot send
        window reads it three times). JournalFolder.state is pure in the folder and now_ns, and the folder at a head
        is the same whether built at once or event by event, so the state is identical by construction; a test pins it
        across appends from another connection, rollbacks and refused appends.

        The head and the catch-up rows are read under one read snapshot. The cache is keyed by (seq, event_sha256) and
        re-checked against the file (a mismatch rebuilds from event 1). Neither this nor the full fold verifies the
        hash chain (verify() does), so the cache changes nothing there. Any failure of the cached path (an unfoldable
        event, an unreadable row) falls back to the full fold, so a broken journal raises exactly what it raised
        before."""
        if type(now_ns) is not int:
            raise JournalError("fold_now_ns_must_be_integer")
        db = self._db
        try:
            if db.in_transaction:                     # left by an interrupted call: a read never extends it
                db.execute("ROLLBACK")
            try:
                db.execute("BEGIN")                   # one read snapshot for the head and the catch-up
                head = db.execute("SELECT seq, event_sha256 FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                folder = self._committed_folder(tuple(head) if head else None)
            finally:
                if db.in_transaction:
                    db.execute("COMMIT")
        except (JournalError, sqlite3.Error):
            self._fold_cache = None
            if db.in_transaction:
                # A ROLLBACK or COMMIT that failed left this connection inside a transaction: the fallback would read
                # through it (a stale or uncommitted snapshot), so fail closed instead (P1-08 residual 2).
                raise JournalError("journal_transaction_stuck") from None
            return fold_events(list(self.events()), mode=self.mode, now_ns=now_ns,
                               submitting_as_unknown=submitting_as_unknown)
        return folder.state(now_ns=now_ns, submitting_as_unknown=submitting_as_unknown)

    # --- writes -----------------------------------------------------------------------------

    def _committed_folder(self, head):
        """The fold of every committed event up to head, caught up from the per-connection cache.

        Runs inside the append transaction, so no other writer can move the head meanwhile.
        Raises JournalError if the committed events cannot be folded.
        """
        head_seq, head_sha = head if head else (0, GENESIS_PREV)
        cache = self._fold_cache
        if cache is not None:
            cached_seq, cached_sha, folder = cache
            if cached_seq == head_seq and cached_sha == head_sha:
                return folder
            row = self._db.execute("SELECT event_sha256 FROM events WHERE seq=?", (cached_seq,)).fetchone()
            if cached_seq > head_seq or (cached_seq > 0 and (row is None or row[0] != cached_sha)):
                cache = None                   # the file changed under us: rebuild from the start
        self._fold_cache = None
        if cache is None:
            start, folder = 0, JournalFolder(self.mode)
        else:
            start, folder = cache[0], cache[2]
        last_seq, last_sha = start, (cache[1] if cache is not None else GENESIS_PREV)
        try:
            for row in self._db.execute(_SELECT_SQL + " WHERE seq > ? AND seq <= ? ORDER BY seq", (start, head_seq)):
                event = self._event(row)
                folder.apply(event)
                last_seq, last_sha = event["seq"], event["event_sha256"]
        except _FOLD_ERRORS:
            raise JournalError("journal_committed_event_unfoldable") from None
        if (last_seq, last_sha) != (head_seq, head_sha):
            raise JournalError("journal_head_moved_during_fold")
        self._fold_cache = (last_seq, last_sha, folder)
        return folder

    def append(self, kind, payload, *, intent_id=None, customer_order_ref=None, customer_ref=None, bet_id=None,
               market_id=None, state_after=None, expect_head=None, before_ns=None, not_before_ns=None):
        """Commit one event durably and return it (with seq and event_sha256).

        The event is first applied to a copy of the committed fold; if that raises, nothing is
        written and the JournalError propagates (see the module docstring). expect_head (an
        anchor() dict, {} for an empty journal) makes the append conditional: JournalError
        journal_head_moved unless the head inside the write transaction is exactly that.
        before_ns (the pilot's authority boundary, P1-06 P106-2) makes it conditional on time as well:
        JournalError journal_append_too_late, and nothing written, unless the event's recorded_at (stamped before
        the write lock is taken) is before it, and the wall clock read again once the lock is held and again just
        before COMMIT is too. The commit's own flush can still finish after before_ns; the pilot fold judges the
        event by its recorded_at, and the first-byte gate re-checks a submit's time. Without it nothing changes.
        not_before_ns (a frontier note's trigger time, P1-10 S3a) is a lower bound on the stamp actually inserted:
        JournalError journal_append_too_early, and nothing written, when recorded_at is below it; equality is
        written. The fold sees only that stamp, which keeps microseconds, so a wall clock at or past not_before_ns
        can still stamp below it. recorded_at is fixed before BEGIN IMMEDIATE, so the check runs before the write
        lock is taken, with the same result. Without it nothing changes.
        A transaction an interrupted call left open on this connection is rolled back first (nothing it held was
        committed), and BEGIN IMMEDIATE sits inside the block whose handlers roll back only an open transaction.
        """
        if before_ns is not None and type(before_ns) is not int:
            raise JournalError("before_ns_invalid")
        if not_before_ns is not None and type(not_before_ns) is not int:
            raise JournalError("not_before_ns_invalid")
        expected = None
        if expect_head is not None:
            if not isinstance(expect_head, dict):
                raise JournalError("expect_head_invalid")
            if expect_head:
                if (type(expect_head.get("seq")) is not int or expect_head["seq"] < 1
                        or not isinstance(expect_head.get("last_sha256"), str)
                        or not _SHA_RE.fullmatch(expect_head["last_sha256"])):
                    raise JournalError("expect_head_invalid")
                expected = (expect_head["seq"], expect_head["last_sha256"])
            else:
                expected = (0, GENESIS_PREV)
        columns, payload_json, payload = prepare_event(kind, payload, intent_id=intent_id,
                                                       customer_order_ref=customer_order_ref,
                                                       customer_ref=customer_ref, bet_id=bet_id,
                                                       market_id=market_id, state_after=state_after)
        if "paper_state" in payload and self.mode != "paper":
            raise JournalError("paper_state_in_live_journal")
        if kind == "intent" and payload.get("source") == "pilot" and self.mode != "live":
            raise JournalError("pilot_intent_in_paper_journal")
        recorded_at = self.clock.utc_now()
        monotonic_ns = self.clock.monotonic_ns()
        if not_before_ns is not None and parse_utc_ns(recorded_at) < not_before_ns:
            raise JournalError("journal_append_too_early")      # recorded_at is already fixed: refused before the lock
        db = self._db
        if db.in_transaction:
            db.execute("ROLLBACK")
        candidate = None
        try:
            db.execute("BEGIN IMMEDIATE")
            head = db.execute("SELECT seq, event_sha256 FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            if expected is not None and (tuple(head) if head else (0, GENESIS_PREV)) != expected:
                raise JournalError("journal_head_moved")
            if before_ns is not None and (parse_utc_ns(recorded_at) >= before_ns
                                          or self.clock.time_ns() >= before_ns):
                raise JournalError("journal_append_too_late")
            seq, previous = (head[0] + 1, head[1]) if head else (1, GENESIS_PREV)
            event = {"seq": seq, "event_id": "ev-" + secrets.token_hex(16), "recorded_at": recorded_at,
                     "monotonic_ns": monotonic_ns, "kind": kind, "mode": self.mode, **columns,
                     "payload": payload, "prev_sha256": previous}
            event["event_sha256"] = _event_sha256(event)
            try:
                folder = self._committed_folder(head)
            except JournalError as exc:
                if not _safety_append(kind, payload) or _moves(event):
                    raise JournalError(f"journal_unfoldable:{exc}") from None
            else:
                candidate = folder.copy()
                try:
                    candidate.apply(event)     # raises JournalError: nothing is written
                except JournalError:
                    raise
                except _FOLD_ERRORS:
                    raise JournalError("journal_event_unfoldable") from None
            db.execute(_INSERT_SQL,
                       (seq, event["event_id"], recorded_at, monotonic_ns, kind, self.mode, intent_id,
                        customer_order_ref, customer_ref, bet_id, market_id, state_after, payload_json, previous,
                        event["event_sha256"]))
            if before_ns is not None and self.clock.time_ns() >= before_ns:
                raise JournalError("journal_append_too_late")      # the fold above can take time: rolled back
            db.execute("COMMIT")
        except sqlite3.IntegrityError:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise JournalError("customer_order_ref_reused" if kind == "intent" else "journal_integrity_error") from None
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        if candidate is not None:
            self._fold_cache = (seq, event["event_sha256"], candidate)
        return copy.deepcopy(event)

    def set_account_fingerprint(self, fp):
        """Store the account fingerprint once; a different value later raises JournalError."""
        if not isinstance(fp, str) or not _FINGERPRINT_RE.fullmatch(fp):
            raise JournalError("account_fingerprint_invalid")
        db = self._db
        # append's shape: a transaction an interrupted call left open is rolled back first, BEGIN IMMEDIATE sits
        # inside the block, and the handler rolls back only an open transaction, so a failed COMMIT keeps its own
        # error (P1-08 residual 2).
        if db.in_transaction:
            db.execute("ROLLBACK")
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM journal_meta WHERE key='account_fingerprint'").fetchone()
            if row is None:
                db.execute("INSERT INTO journal_meta (key, value) VALUES ('account_fingerprint', ?)", (fp,))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        if row is not None and row[0] != fp:
            raise JournalError("account_fingerprint_mismatch")

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class UnreadableJournal:
    """Stands in for a live journal file that exists but cannot be opened (not a database, no permission,
    another schema), on the kill path only: a journal the kill cannot open never stops its sweep (amendment
    A3, D4). Every read and every write raises JournalError("journal_unreadable"); nothing is created,
    opened or replaced, so the file stays as it was for the operator to inspect."""

    def __init__(self, mode):
        if mode not in JOURNAL_MODES:
            raise JournalError("journal_mode_invalid")
        self.mode = mode

    def _unreadable(self, *args, **kwargs):
        raise JournalError("journal_unreadable")

    meta = account_fingerprint = events = verify = last_paper_state = anchor = fold = _unreadable
    append = set_account_fingerprint = _unreadable

    def close(self):
        return None


READ_ONLY_BUSY_TIMEOUT_S = 2


def _regular_file_identity(path):
    """(st_dev, st_ino) of the regular file at path, from lstat; None only when the directory entry is definitely
    absent (ENOENT). A symlink (dangling or not), any other non-regular entry, or a path lstat cannot inspect
    (EACCES, ELOOP, ENOTDIR, ...) raises JournalError("journal_unreadable_read_only")."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise JournalError("journal_unreadable_read_only") from None
    # os.path only (no stat module: paper's import closure stays as REUSE_FINDINGS records it). Not a link, and
    # what the path names is a regular file: then the entry itself is one.
    if os.path.islink(path) or not os.path.isfile(path):
        raise JournalError("journal_unreadable_read_only")
    return info.st_dev, info.st_ino


def read_only_fold(path, *, mode, now_ns, busy_timeout_s=READ_ONLY_BUSY_TIMEOUT_S):
    """The fold of a journal file opened with SQLite's mode=ro, or None when no journal exists.

    For recording admission's release (admission.py): the connection can never write an event or take
    the write lock, and nothing is created when the file is absent. Only a definite absence of the
    directory entry (lstat ENOENT) means no journal; a symlink (dangling or not), a non-regular entry or a
    path that cannot be inspected fails closed, and so does an entry replaced while it was read. Anything
    else fails closed too with JournalError("journal_unreadable_read_only"): a busy or locked file, one
    that is not a database, the wrong schema or mode, an event that is not strict JSON, or a committed
    prefix that cannot be folded. The schema checks are OrderJournal's, without the write transaction it
    takes for a new file."""
    path = Path(path)
    identity = _regular_file_identity(path)
    if identity is None:
        return None
    db = None
    try:
        db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=busy_timeout_s,
                             isolation_level=None)
        db.execute(f"PRAGMA busy_timeout={int(busy_timeout_s * 1000)}")
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise JournalError("journal_schema_version_mismatch")
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        columns = tuple(row[1] for row in db.execute("PRAGMA table_info(events)"))
        if not (REQUIRED_TABLES | REQUIRED_TRIGGERS | REQUIRED_INDEXES) <= names or columns != EVENT_COLUMNS:
            raise JournalError("journal_schema_incomplete")
        meta = {key: value for key, value in db.execute("SELECT key, value FROM journal_meta")}
        if meta.get("schema_version") != str(SCHEMA_VERSION) or meta.get("mode") != mode:
            raise JournalError("journal_mode_mismatch")
        events = []
        for row in db.execute(_SELECT_SQL + " ORDER BY seq"):
            event = dict(zip(EVENT_COLUMNS, row))
            event["payload"] = strict_json_loads(event.pop("payload_json"))
            events.append(event)
        if _regular_file_identity(path) != identity:          # replaced (or swapped for a link) while read
            raise JournalError("journal_replaced_while_read")
        return fold_events(events, mode=mode, now_ns=now_ns)
    except (sqlite3.Error, JournalError) + _FOLD_ERRORS:
        raise JournalError("journal_unreadable_read_only") from None
    finally:
        if db is not None:
            db.close()


def _safety_append(kind, payload):
    """True for the events the kill switch needs while the committed prefix cannot be folded."""
    if kind == "submit":
        return payload.get("action") in SAFETY_SUBMIT_ACTIONS
    return kind in SAFETY_KINDS


def _event_sha256(event):
    """canonical_sha256 of every column except event_sha256, with the payload parsed."""
    return canonical_sha256({key: value for key, value in event.items() if key != "event_sha256"})


# --- fold ------------------------------------------------------------------------------------


def _new_order(event, mode):
    payload = event["payload"]
    instruction = payload["order"]
    order = {
        "intent_id": event["intent_id"], "customer_order_ref": event["customer_order_ref"],
        "customer_ref": event["customer_ref"], "customer_strategy_ref": payload["customer_strategy_ref"],
        "mode": mode, "market_id": event["market_id"], "selection_id": instruction["selection_id"],
        "handicap": instruction["handicap"], "side": instruction["side"], "price_h": instruction["price_h"],
        "size_p": instruction["size_p"], "persistence_type": instruction["persistence_type"], "state": "RECORDED",
        "bet_id": None, "size_matched_p": 0, "average_price_matched_h": None, "size_remaining_p": 0,
        "size_cancelled_p": 0, "size_lapsed_p": 0, "size_voided_p": 0, "submitted_at": None,
        "submitted_monotonic_ns": None, "body_sha256": None, "outcome_observed_at": None, "successful_polls": 0,
        "last_report_at": None, "parent_ref": payload["parent_ref"], "child_ref": None,
        "provisional_child": True if payload["provisional_child"] else None, "is_probe": payload["is_probe"],
        "error_code": None, "reconcile_attempts": 0, "settled_profit_own_p": None,
        "settled_profit_betfair_p": None, "exposure_basis": "none"}
    order["exposure_basis"] = exposure_basis(order)      # a provisional replace child is adverse_unknown
    try:
        return validate_order_record(order)
    except ValueError:
        raise JournalError("intent_builds_invalid_order_record") from None


def _moves(event):
    payload = event["payload"]
    moves = []
    if event["kind"] != "intent" and event["customer_order_ref"] is not None and event["state_after"] is not None:
        moves.append({"customer_order_ref": event["customer_order_ref"], "state": event["state_after"],
                      "reason": payload.get("reason") or event["kind"], "fields": payload.get("fields") or {}})
    for move in payload.get("transitions") or []:
        moves.append({"customer_order_ref": move["customer_order_ref"], "state": move["state"],
                      "reason": move.get("reason") or event["kind"], "fields": move.get("fields") or {}})
    return moves


def matched_worst_loss_pence(order):
    """Largest loss the matched part of an order can produce (BACK: stake; LAY: liability), in pence."""
    matched = order["size_matched_p"]
    if matched <= 0:
        return 0
    if order["side"] == "BACK":
        return matched
    price = max(order["average_price_matched_h"] or 0, order["price_h"])
    return -((-matched * (price - 100)) // 100)


def settled_worst_loss_pence(order):
    """Largest loss a settled order can have produced, in pence: its matched worst case plus its recorded
    remaining size at the order's own limit (BACK: stake; LAY: liability, rounded up).

    Live settle-market moves an EXECUTABLE order to SETTLED with its remaining size as recorded (whether
    it matched later is not known), so that remainder is booked at its worst case too. Paper SETTLED
    orders always have nothing remaining, so for them this is matched_worst_loss_pence."""
    remaining = order["size_remaining_p"]
    if remaining <= 0:
        return matched_worst_loss_pence(order)
    if order["side"] == "BACK":
        extra = remaining
    else:
        extra = -((-remaining * (order["price_h"] - 100)) // 100)
    return matched_worst_loss_pence(order) + extra


def _settled_profit(order):
    own, betfair = order["settled_profit_own_p"], order["settled_profit_betfair_p"]
    if own is not None and betfair is not None:
        return min(own, betfair)
    if own is not None:
        return own
    if betfair is not None:
        return betfair
    return -settled_worst_loss_pence(order)


class JournalFolder:
    """Incremental fold: apply() events in seq order, then state() at any now_ns.

    fold_events (and OrderJournal.fold) rebuild a fresh folder from every event, so state never
    depends on how it was built; the incremental form only saves repeated work.
    """

    def __init__(self, mode):
        if mode not in JOURNAL_MODES:
            raise JournalError("journal_mode_invalid")
        self.mode = mode
        self.orders = {}
        self.halted, self.halt_reasons, self.halted_markets = False, [], set()
        self.halt_seqs = []          # the seq of each entry in halt_reasons (None when an event has none)
        self.submitted, self.hours = [], {}
        self.strategy_refs, self.roundtrip = set(), False
        self.last_submit, self.consecutive_unknown, self.latest_ns = None, 0, None
        self.funds, self.funds_checked_at, self.floor = None, None, None
        self.settlements = []        # (recorded ns, order ref) for transitions to SETTLED/VOIDED
        self.commissions = []        # (recorded ns, pence) from settle events
        self.events = 0

    def copy(self):
        """An independent folder in the same state (apply never mutates an order record in place)."""
        other = JournalFolder.__new__(JournalFolder)
        other.__dict__.update(self.__dict__)
        other.orders = dict(self.orders)
        other.halt_reasons = list(self.halt_reasons)
        other.halt_seqs = list(self.halt_seqs)
        other.halted_markets = set(self.halted_markets)
        other.submitted = list(self.submitted)
        other.hours = dict(self.hours)
        other.strategy_refs = set(self.strategy_refs)
        other.settlements = list(self.settlements)
        other.commissions = list(self.commissions)
        return other

    def apply(self, event):
        if event["mode"] != self.mode:
            raise JournalError("journal_event_mode_mismatch")
        orders = self.orders
        kind, payload = event["kind"], event["payload"]
        at_ns = parse_utc_ns(event["recorded_at"])
        self.latest_ns = at_ns if self.latest_ns is None else max(self.latest_ns, at_ns)
        hour = utc_from_ns(at_ns)[:13]
        self.events += 1
        if kind == "intent":
            ref = event["customer_order_ref"]
            if ref in orders:
                raise JournalError("customer_order_ref_reused")
            order = _new_order(event, self.mode)
            parent_ref = order["parent_ref"]
            if parent_ref is not None:
                parent = orders.get(parent_ref)
                if parent is None or parent["market_id"] != order["market_id"]:
                    raise JournalError("replace_child_without_parent")
                if (order["selection_id"], order["side"], order["handicap"]) != (
                        parent["selection_id"], parent["side"], parent["handicap"]):
                    raise JournalError("replace_child_differs_from_parent")
                if parent["state"] not in _CHILD_PARENT_STATES or order["size_p"] > parent["size_remaining_p"]:
                    raise JournalError("replace_child_parent_not_replaceable")
                orders[parent_ref] = dict(parent, child_ref=ref)
            orders[ref] = order
            self.strategy_refs.add(order["customer_strategy_ref"])
        if kind == "submit":
            self.last_submit = at_ns
            if payload["action"] in ("place", "replace"):
                count = payload["instruction_count"]
                self.submitted.extend([at_ns] * count)
                self.hours[hour] = self.hours.get(hour, 0) + count
        elif payload.get("transactions"):
            self.hours[hour] = self.hours.get(hour, 0) + payload["transactions"]
        went_unknown, moved = False, False
        for move in _moves(event):
            ref = move["customer_order_ref"]
            if ref not in orders:
                raise JournalError("transition_for_unknown_order")
            order = orders[ref]
            fields = dict(move["fields"])
            state = move["state"]
            if (order["state"] == "SUBMITTING" and state not in TRANSITIONS["SUBMITTING"]
                    and state in TRANSITIONS["UNKNOWN"]):
                # Every reader sees a stored SUBMITTING order as UNKNOWN; apply the move through it.
                order = transition(order, "UNKNOWN", reason="crash_between_submit_and_report",
                                   fields={"outcome_observed_at": event["recorded_at"]})
            if kind == "submit" and state == "SUBMITTING":
                fields.setdefault("submitted_at", event["recorded_at"])
                fields.setdefault("submitted_monotonic_ns", event["monotonic_ns"])
                fields.setdefault("body_sha256", payload.get("body_sha256"))
            if kind in ("report", "reconcile", "settle", "operator_accept"):
                fields.setdefault("last_report_at", event["recorded_at"])
            if kind == "reconcile":
                fields.setdefault("reconcile_attempts", order["reconcile_attempts"] + 1)
            if state == "UNKNOWN" and order["state"] != "UNKNOWN":
                fields.setdefault("outcome_observed_at", event["recorded_at"])
            orders[ref] = transition(order, state, reason=move["reason"], fields=fields)
            moved = True
            went_unknown |= state == "UNKNOWN"
            if state in ("SETTLED", "VOIDED"):
                self.settlements.append((at_ns, ref))
        if kind == "report" and moved:
            self.consecutive_unknown = self.consecutive_unknown + 1 if went_unknown else 0
        if kind in HALT_PAYLOAD_KINDS:
            # Global HALTs carried by the event that triggered them (R3#13): applied with its moves, atomically.
            for reason in payload.get("halts") or ():
                self.halted = True
                self.halt_reasons.append(str(reason))
                self.halt_seqs.append(event.get("seq"))
        if kind == "funds_check":
            self.funds, self.funds_checked_at = copy.deepcopy(payload.get("funds")), event["recorded_at"]
        elif kind == "startup_reconcile":
            self.floor = {"day_start": at_ns - at_ns % DAY_NS,
                          "orders_today": payload.get("exchange_orders_today"),
                          "realized_today_pence": payload.get("exchange_realized_today_pence")}
        elif kind == "halt":
            if payload.get("scope") == "market" and event["market_id"] is not None:
                self.halted_markets.add(event["market_id"])
            else:
                self.halted = True
                self.halt_reasons.append(str(payload.get("reason") or "halt"))
                self.halt_seqs.append(event.get("seq"))
        elif kind == "kill":
            self.halted = True
            self.halt_reasons.append("kill")
            self.halt_seqs.append(event.get("seq"))
        elif kind == "resume":
            # Fail closed: only an explicit global resume clears the HALT; anything unrecognised is a no-op.
            scope = payload.get("scope")
            if scope == "market" and event["market_id"] is not None:
                self.halted_markets.discard(event["market_id"])
            elif scope == "global" and "cleared_through_seq" in payload:
                # Only what the operator saw: halts journaled after that seq stay.
                bound = payload["cleared_through_seq"]
                if type(bound) is not int or bound < 0 or (event.get("seq") is not None and bound >= event["seq"]):
                    raise JournalError("resume_cleared_through_seq_invalid")
                kept = [(s, r) for s, r in zip(self.halt_seqs, self.halt_reasons) if s is None or s > bound]
                self.halt_seqs = [s for s, _ in kept]
                self.halt_reasons = [r for _, r in kept]
                self.halted = bool(kept)
            elif scope == "global":
                self.halted, self.halt_reasons, self.halt_seqs = False, [], []
        elif kind == "ref_roundtrip_verified":
            self.roundtrip = True
        elif kind == "settle":
            commission = payload.get("commission_p")
            if type(commission) is int and commission > 0:
                self.commissions.append((at_ns, commission))

    def state(self, *, now_ns, submitting_as_unknown=True):
        """The spec section 9.6 dict at now_ns; every order record is a fresh copy."""
        if type(now_ns) is not int:
            raise JournalError("fold_now_ns_must_be_integer")
        day_start = now_ns - now_ns % DAY_NS
        orders = {ref: dict(order) for ref, order in self.orders.items()}
        if submitting_as_unknown:
            for ref, order in orders.items():
                if order["state"] == "SUBMITTING":
                    orders[ref] = transition(order, "UNKNOWN", reason="crash_between_submit_and_report")
        unresolved = set(self.halted_markets)
        for order in orders.values():
            if order["state"] in UNRESOLVED_STATES or (order["provisional_child"] is True
                                                        and order["state"] != "RECORDED"):
                unresolved.add(order["market_id"])
        realized = -sum(amount for at_ns, amount in self.commissions if at_ns >= day_start)
        settled_liability = 0
        for at_ns, ref in self.settlements:
            if at_ns >= day_start and orders[ref]["state"] == "SETTLED":
                realized += _settled_profit(orders[ref])
                settled_liability += settled_worst_loss_pence(orders[ref])
        unsettled_liability = 0
        for order in orders.values():
            submitted_at = order["submitted_at"]
            if (order["state"] not in ("SETTLED", "VOIDED") and order["size_matched_p"] > 0
                    and submitted_at is not None and parse_utc_ns(submitted_at) >= day_start):
                unsettled_liability += matched_worst_loss_pence(order)
        floor = self.floor
        exchange_floor = None
        if floor is not None and floor["day_start"] == day_start:
            exchange_floor = {"orders_today": floor["orders_today"],
                              "realized_today_pence": floor["realized_today_pence"]}
        return {
            "orders": orders, "halted": self.halted, "halt_reasons": list(self.halt_reasons),
            "unresolved_markets": sorted(unresolved), "submitted_instruction_wall_ns": list(self.submitted),
            "transactions_by_clock_hour": dict(self.hours),
            "orders_today": sum(1 for t in self.submitted if t >= day_start), "realized_today_pence": realized,
            "unsettled_matched_liability_today_pence": unsettled_liability,
            "strategy_refs_seen": sorted(self.strategy_refs), "ref_roundtrip_verified": self.roundtrip,
            "last_submit_wall_ns": self.last_submit, "consecutive_unknown": self.consecutive_unknown,
            "clock_regression": self.latest_ns is not None and self.latest_ns > now_ns + CLOCK_REGRESSION_NS,
            "funds": copy.deepcopy(self.funds), "funds_checked_at": self.funds_checked_at,
            "exchange_floor": exchange_floor, "settled_matched_liability_today_pence": settled_liability}


def fold_events(events, *, mode, now_ns, submitting_as_unknown=True):
    """Spec section 9.6 state from journal events (any iterable of event dicts in seq order).

    Pure: the same events and now_ns give the same state. With submitting_as_unknown (the default,
    used when a process opens the journal), every order still SUBMITTING is UNKNOWN
    (crash_between_submit_and_report): its submit was journaled but no report ever was.
    Settlement and commission count towards realized_today_pence when recorded on now's UTC day;
    settled_matched_liability_today_pence (an addition to spec section 9.6) is the worst-case loss of
    the orders settled that day (settled_worst_loss_pence: matched part plus any recorded remainder),
    which R10 uses when a realised value is unknown.
    """
    folder = JournalFolder(mode)
    for event in events:
        folder.apply(event)
    return folder.state(now_ns=now_ns, submitting_as_unknown=submitting_as_unknown)
