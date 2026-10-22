"""RS-19's attended pilot executor (P1-08): pilot-register, PILOT ARM and the run loop, pilot-ack, pilot-gap-resolved,
pilot-end-session and pilot-capture.

docs/betfair-pilot-policy.md (RS-19) is the policy and docs/betfair-pilot-engineering.md (the note) the detail. Every
placement rule lives in the pure pilot.py (the fold, the one outcome/scope table, the stop ids) and the gateway (the
only order path, which this module never imports: the CLI hands it a factory that opens one gateway per placement or
settle point, each with a fresh ObservedClock and the session's preloaded credentials). This module adds the parts
the note gives the executor: the typed lines (always Yu's, at the terminal, through the live confirmer; nothing here
types or supplies one), the registration receipt, the armed-session file, ARM's pre-checks and its locked validation,
the scheduler (timing only, never evidence), one placement open per decision, the remainder cancel with KILL cases
1-4 and the handoff, settle points, capture reads, the end path and release, crash recovery (pilot-end-session) and
the ACK and gap-resolution notes. Ownership of the live recording state goes through admission.live_ownership.

What it reads: the journal on short-lived read-only connections (pilot_events_after), the Keychain once per session
(the credential loader), and Betfair only through the gateway and read sessions the CLI builds. What it writes: pilot
notes (stops, refusals, session_start, registration, ack, gap_resolved, capture notes) as plain appends, or as
compare-and-appends on the head Yu saw for the typed ones; the receipt, the armed-session file, cause copies and raw
capture bytes under the profile's pilot directory. Every terminal write goes through one best-effort writer. A signal
from the typed ARM line on only sets a flag (StopFlag), never prints or raises.
"""

import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3

from . import admission, pilot, pilot_gate
from .arming import FAILURE_CODES, STATIC_CHECKS, a7_causes, static_arming
from .auth import ledger_status
from .config import config_sha256, pilot_enabled
from .credentials import keychain_read, load_credentials, publish_anchor
from .errors import (AdmissionRefused, AuthError, BetfairError, CredentialError, JournalError, LockBusy,
                     LoginBudgetExceeded, LoginStopped, NotArmed, OrderRefused, ReadStopped, safe_code)
from .journal import EVENT_COLUMNS, OrderJournal, fold_events
from .paths import file_lock, kill_engaged
from .receipts import (atomic_write_bytes, atomic_write_json, bytes_sha256, git_identity, strict_json_loads,
                       utc_compact_from_ns, utc_from_ns)
from .schemas import UNRESOLVED_STATES

NS = pilot.NS
# The recorder's profile (recording design v3:27): pilot-register, ARM and pilot-capture run in it only (C13, C22). A
# constant, never a config key, so config_sha256 does not move.
RECORDER_PROFILE = "default"
OPEN_LEAD_NS = 150 * NS              # note :66: the lock wait, the reconcile budget, git, the anchor and the login
CASE4_WAIT_NS = 30 * NS              # plan :104: a kill with no outcome is waited for at most this long
CASE4_DRIFT_NS = 2 * NS              # C32: wall and monotonic time apart by more than this: hand off at once
KILL_RECENT_NS = 90 * NS             # pilot-capture's kill rule: a heuristic, not a bound (note :150, :168)
SETTLE_LEAD_NS = 180 * NS            # plan :105: no settle offer within 3 min of a decision
PROMPT_TIMEOUT_NS = 120 * NS         # presence.ANSWER_TIMEOUT_S
SETTLE_OVERHEAD_NS = 30 * NS
SLICE_S = 1.0                        # every wait outside an open: at most 1 s between flag checks (§3.6)
GAP_INTERVAL_NS = 300 * NS           # a long wait between opens is cut into gaps for capture and settle points
CAPTURE_LOGIN_NS = 15 * NS           # SCHED-4: a capture batch's login allowance, beside one read's http.timeout_s
PENDING_CAPTURE_LINE = "run pilot-capture now, before these markets settle (F77)"
ARMED_SCHEMA = "betangel.pilot-armed-session/v1"
ARMED_KEYS = frozenset({"schema", "session_id", "contract_sha256", "registration_receipt_sha256", "utc_day",
                        "until_ns", "session_end_ns", "admission_owner", "admission_generation", "pid",
                        "process_start_time", "armed_at"})
UNTIL_RE = re.compile(r"([01][0-9]|2[0-3])([0-5][0-9])")
STATIC_CODES = frozenset(code for check in STATIC_CHECKS for code in FAILURE_CODES[check])
LOGIN_RESET_LINE = "a login attempt has no outcome or failed; Yu decides on login-reset (note:125)"
RECORDED_LINE = ("then run reconcile --mode live{flag}{cont}; its open refuses the order recorded before its submit "
                 "(crash_before_submit)")
OWNER_LIVE_LINE = "owner_live: another live owner holds the owner lock"
LEDGER_LINE = ("login ledger: a login attempt from this run has no outcome (stopped mid-login). From 30 s after it, it "
               "counts as a failed or unknown login, and one more stops every caller, kill included. Release and the next "
               "PILOT ARM refuse until a later successful login or Yu's typed login-reset; Yu decides (note:125).")
STOP_LINE = "stop requested; no new placement; finishing cleanup, then the end"
KILL_CAPTURE_LINE = ("run pilot-capture once the kill has exited, at least 90 s after it began, before these markets "
                     "settle")
_SELECT = "SELECT " + ", ".join(EVENT_COLUMNS) + " FROM events WHERE seq > ? ORDER BY seq"


class Refused(Exception):
    """A pilot command refused before (or instead of) what it does: exit 2, 'Betfair order refused: <code>', after
    lines (best effort). Nothing was sent; what was written is stated where the refusal is raised."""

    def __init__(self, code, lines=()):
        super().__init__(code)
        self.code = code
        self.lines = list(lines)


# --- the terminal and the stop flag (§3.6, §4.1) ----------------------------------------------------------------


class Terminal:
    """One best-effort writer (§4.1): a write that raises OSError or ValueError sets lost, and nothing more is written.
    No journal append, file delete or release ever depends on a write succeeding."""

    def __init__(self, out, err):
        self._out, self._err = out, err
        self.lost = False

    def write(self, which, text):
        if self.lost:
            return
        stream = self._out if which == "out" else self._err
        try:
            stream.write(text)
            stream.flush()
        except (OSError, ValueError):
            self.lost = True

    def line(self, text):
        self.write("err", f"{text}\n")

    def lines(self, texts):
        for text in texts:
            self.line(text)

    def json(self, value):
        self.write("out", json.dumps(value, indent=2, sort_keys=True, allow_nan=False, default=str) + "\n")

    def stream(self, which):
        return _Stream(self, which)


class _Stream:
    """A file-like view of one side of a Terminal, for code that writes to ctx.out or ctx.err directly."""

    def __init__(self, terminal, which):
        self._terminal, self._which = terminal, which

    def write(self, text):
        self._terminal.write(self._which, text)
        return len(text)

    def flush(self):
        return None


_SIGNAL_NAMES = {signal.SIGINT: "SIGINT", signal.SIGTERM: "SIGTERM", signal.SIGHUP: "SIGHUP"}


class StopFlag:
    """The sticky stop flag (§3.6): from the typed ARM line on, any SIGINT, SIGTERM or SIGHUP means Yu stopped the
    session. The handler only sets requested, the first signal's name and hup; it never writes, prints or raises. The
    flag is never cleared."""

    SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __init__(self):
        self.requested = False
        self.name = None
        self.hup = False
        self.announced = False
        self._previous = {}

    def handler(self, signum, frame):
        self.requested = True
        if self.name is None:
            self.name = _SIGNAL_NAMES.get(signum, "signal")
        if signum == signal.SIGHUP:
            self.hup = True

    def install(self):
        """Put this handler on the three signals (idempotent; the first previous handlers are kept for restore)."""
        try:
            for sig in self.SIGNALS:
                previous = signal.signal(sig, self.handler)
                self._previous.setdefault(sig, previous)
        except ValueError:          # not the main thread: the process default stays
            pass

    def restore(self):
        previous, self._previous = self._previous, {}
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass

    def take_pending(self, signals):
        """Turn a gateway guard's deferred signals into the flag (§3.6 point 3), read before the next open."""
        if signals is None or not signals.pending:
            return
        self.requested = True
        if signal.SIGHUP in signals.pending:
            self.hup = True
        if self.name is None:
            self.name = _SIGNAL_NAMES.get(signals.pending[0], "signal")


# --- the journal reader and the recovery projection (§3.3) ------------------------------------------------------


def pilot_events_after(journal_path, since_seq):
    """{readable, events, kill_seq, kill_incomplete_seq, place_submit} for every event with seq > since_seq, read on
    its own short-lived ?mode=ro connection with fetchall() (never the gateway's connection or an open generator). kill_seq
    is the first kill event; kill_incomplete_seq the first HALT kill_incomplete after it; place_submit the first pilot
    place submit. Any sqlite, OS or JSON error gives readable False."""
    out = {"readable": False, "events": [], "kill_seq": None, "kill_incomplete_seq": None, "place_submit": None}
    db = None
    try:
        db = sqlite3.connect(Path(journal_path).absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None,
                             timeout=2)
        rows = db.execute(_SELECT, (int(since_seq),)).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return out
    finally:
        if db is not None:
            db.close()
    events = []
    try:
        for row in rows:
            event = dict(zip(EVENT_COLUMNS, row))
            event["payload"] = strict_json_loads(event.pop("payload_json"))
            if not isinstance(event["payload"], dict):
                return out
            events.append(event)
    except (ValueError, TypeError):
        return out
    for event in events:
        kind, payload = event["kind"], event["payload"]
        if kind == "kill" and out["kill_seq"] is None:
            out["kill_seq"] = event["seq"]
        elif (kind == "halt" and payload.get("reason") == "kill_incomplete" and out["kill_seq"] is not None
              and out["kill_incomplete_seq"] is None):
            out["kill_incomplete_seq"] = event["seq"]
        elif (kind == "submit" and payload.get("action") == "place" and "pilot_record_sha256" in payload
              and out["place_submit"] is None):
            out["place_submit"] = event
    out.update(readable=True, events=events)
    return out


def recovery_projection(events, *, contract, now_ns, market_hint=None):
    """The one journal-only recovery projection (§3.3): pure, no I/O. events must be the whole journal from seq 1.
    -> {state, core, handoffs, due_stops, armed, stop_reason, stop_source_seq, pending_capture, market_hint}.
    handoffs: every pilot record with a submit, in any session, whose order is unresolved (UNRESOLVED_STATES) or
    EXECUTABLE with a remainder. stop_reason for an armed session: unknown_unresolved, else remainder_left_executable,
    else session_abandoned. pending_capture: pilot bets with a bet id and no C read showing EXECUTION_COMPLETE."""
    state = pilot.fold(events, contract=contract, now_ns=now_ns)
    core = fold_events(events, mode="live", now_ns=now_ns, submitting_as_unknown=True)
    handoffs, pending = [], []
    for rec in state.place_records:
        order = core["orders"].get(rec.ref)
        if order is None:
            continue
        item = {"ref": rec.ref, "bet_id": order["bet_id"], "selection_id": rec.selection_id, "market_id": rec.market_id,
                "submit_seq": rec.submit_seq, "state": order["state"], "size_remaining_p": order["size_remaining_p"],
                "session_id": rec.session_id, "off_ns": rec.off_ns}
        if rec.submit_seq is not None and (order["state"] in UNRESOLVED_STATES or (
                order["state"] == "EXECUTABLE" and (order["size_remaining_p"] or 0) > 0)):
            handoffs.append(item)
        if order["bet_id"] is not None and not rec.unsent and not any(
                read["row"].get("status") == "EXECUTION_COMPLETE" for read in rec.c_reads):
            pending.append(item)
    unresolved = [h for h in handoffs if h["state"] in UNRESOLVED_STATES]
    reason = ("unknown_unresolved" if unresolved else "remainder_left_executable" if handoffs
              else "session_abandoned")
    cause = (unresolved or handoffs or [None])[0]
    return {"state": state, "core": core, "handoffs": handoffs, "due_stops": state.due_stops,
            "armed": pilot.session_armed(state, now_ns=now_ns), "stop_reason": reason,
            "stop_source_seq": None if cause is None else cause["submit_seq"], "pending_capture": pending,
            "market_hint": market_hint, "closed_markets": closed_markets(events)}


def handoff_text(item):
    """The exact handoff (note:110): the bet id, else the order ref, else 'order ref unknown' with the market id."""
    who = item.get("bet_id") or item.get("ref") or "order ref unknown"
    selection = item.get("selection_id") if item.get("selection_id") is not None else "unknown selection"
    market = item.get("market_id") or "unknown market"
    return (f"pilot order {who} (BACK £2.00 on {selection} in {market}) may still have an unmatched part; run "
            f"own-scope kill on the Air, or cancel it on the Betfair website, now.")


def case_one_handoff(events, *, seen_seq, market_id):
    """The case-1 handoff when the journal or its projection cannot be used (note:105): the market id, plus the order
    ref of any pilot place submit after seen_seq the raw rows show, else 'order ref unknown'."""
    refs = []
    for event in events or ():
        payload = event.get("payload") if isinstance(event, dict) else None
        if (isinstance(payload, dict) and event.get("kind") == "submit" and payload.get("action") == "place"
                and "pilot_record_sha256" in payload and (event.get("seq") or 0) > (seen_seq or 0)):
            refs += [m.get("customer_order_ref") for m in payload.get("transitions") or () if isinstance(m, dict)]
    return [handoff_text({"ref": ref, "market_id": market_id}) for ref in refs if isinstance(ref, str)] or \
        [handoff_text({"market_id": market_id})]


# --- journal writes: stops, refusals, flushing (§3.2, §6) ---------------------------------------------------------


def session_stop(contract, code, anchor, *, source_seq=None):
    """A session stop {"pilot": ...} with the shared id '<code>-<anchor>-session' (the gateway's _pilot_session_stop
    gives the same id for the same code and reservation)."""
    return {"pilot": pilot.stop_payload(contract, pilot.session_stop_id(code, anchor), "session",
                                        pilot.refusal_code(code), source_seq=source_seq)}


def refusal_note(contract, *, session_id, open_id, reservation_seq, code):
    """A refusal note as the gateway's _pilot_open_refusal builds it: the fold derives its review stop
    '<code>-<res>-<note seq>'."""
    return pilot.pilot_note("refusal", contract.sha256, session_id=session_id, open_id=open_id,
                            reservation_seq=reservation_seq, code=pilot.refusal_code(code), row_sha256=None,
                            book_sha256=None)


def append_pilot(journal, payload, *, expect_head=None, before_ns=None, **columns):
    """Validate one pilot record (validate_pilot_payload, validate_event_columns) and append it -> the event."""
    kind = pilot.validate_pilot_payload(payload)
    if kind is None:
        raise JournalError("pilot_record_invalid")
    pilot.validate_event_columns(kind, payload["pilot"], dict(columns, state_after=None))
    extra = {}
    if expect_head is not None:
        extra["expect_head"] = expect_head
    if before_ns is not None:
        extra["before_ns"] = before_ns
    return journal.append("note", payload, **columns, **extra)


def flush_due_held(journal, state):
    """Journal the fold's due stops exactly as it returns them, for a caller already holding .gateway.lock (it never
    takes the lock: a second flock from this process would conflict with its own, P108-R2-1) -> [stop ids]."""
    appended = []
    for stop in state.due_stops:
        append_pilot(journal, {"pilot": stop})
        appended.append(stop["stop_id"])
    return appended


def flush_due(journal, state, *, paths, config, clock):
    """flush_due_held under a brief .gateway.lock hold, for a caller holding no lock -> [stop ids]."""
    if not state.due_stops:
        return []
    with file_lock(paths.gateway_lock, timeout_s=config["http"]["order_deadline_s"] + 5, clock=clock):
        return flush_due_held(journal, state)


def head_of(events):
    """{seq, last_sha256} of the last event, {} for an empty journal (journal.py's empty head)."""
    return {"seq": events[-1]["seq"], "last_sha256": events[-1]["event_sha256"]} if events else {}


# --- the armed-session file (§5.1) ----------------------------------------------------------------------------


def armed_session_path(paths):
    return paths.pilot_dir / "armed-session.json"


def armed_session_check(paths):
    """[] | ["pilot_session_armed"] | ["pilot_session_state_unknown"]: any entry of that name counts; lstat fails
    closed, as kill_engaged does. No lock, no write."""
    try:
        os.lstat(armed_session_path(paths))
    except FileNotFoundError:
        return []
    except OSError:
        return ["pilot_session_state_unknown"]
    return ["pilot_session_armed"]


def write_armed_session(paths, record):
    """Write the armed-session file (0600, atomic) -> the sha256 of the bytes written."""
    if set(record) != ARMED_KEYS or record["schema"] != ARMED_SCHEMA:
        raise ValueError("armed_session_record_invalid")
    paths.ensure()
    pilot_gate.private_dir(paths.pilot_dir, paths.profile_dir)
    return atomic_write_json(armed_session_path(paths), record)


def read_armed_session(paths):
    """(sha256 of its bytes, its record) of a regular, non-symlink armed-session file; OSError or ValueError else."""
    path = armed_session_path(paths)
    if path.is_symlink() or not path.is_file():
        raise OSError("armed_session_not_a_regular_file")
    data = path.read_bytes()
    record = strict_json_loads(data.decode("utf-8"))
    if not isinstance(record, dict) or set(record) != ARMED_KEYS:
        raise ValueError("armed_session_record_invalid")
    return bytes_sha256(data), record


def delete_armed_session_if(paths, sha256=None):
    """Delete the armed-session file, only while its bytes still hash to sha256 when one is given -> "deleted",
    "missing", "changed" or "unreadable" (a file not ours, or unreadable, is left as it is)."""
    path = armed_session_path(paths)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if sha256 is not None:
        try:
            found, _ = read_armed_session(paths)
        except (OSError, ValueError):
            return "unreadable"
        if found != sha256:
            return "changed"
    try:
        os.unlink(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    return "deleted"


# --- the session credential loader (§3.5, C20b) ---------------------------------------------------------------


class _ArmStopped(Exception):
    """The stop flag was seen between two Keychain item reads: no more items are read."""


def load_session_credentials(config, paths, *, runner, stop=None, purposes=("live_orders", "read")):
    """Read each needed Keychain item once (username, password, the live key, the delayed key only when the read slot
    is delayed, the passphrase if configured) -> {"live_orders": ..., "read": ...}, built in memory only. The flag is
    checked between items, so no more are read after a stop. pilot-capture asks for the read purpose only."""
    cache = {}

    def loader(kind, account):
        if kind not in cache:
            if stop is not None and stop.requested:
                raise _ArmStopped()
            cache[kind] = keychain_read(kind, account, runner=runner)
        return cache[kind]

    return {purpose: load_credentials(config, purpose=purpose, profile=paths.profile, loader=loader)
            for purpose in purposes}


# --- checks shared by the commands ----------------------------------------------------------------------------


def cap_blocks(config, paths, *, clock):
    """The §3.8 budget pre-check: True when the local successful-login cap would refuse a normal login now (the
    ledger read only: no Betfair call, no lock). An unreadable ledger counts as blocked."""
    try:
        status = ledger_status(config, paths, clock=clock)
    except AuthError:
        return True
    return status["successes_last_600s"] >= config["session"]["max_successful_logins_per_10min"]


def consecutive_failures(config, paths, *, clock):
    try:
        return ledger_status(config, paths, clock=clock)["consecutive_failures"]
    except AuthError:
        return None


def admission_intact(paths, ticket, *, clock):
    """[] or codes, read only (§3.10; C14): ADMISSION still names this owner, generation and "admitted" with INHIBIT
    set (else admission_lost; unreadable too), no recorder holds .recorder.lock (else recorder_running), and this
    process still holds the owner lock. Never an admit continuation (it would drain and run step 8 mid-session)."""
    problems = []
    try:
        status = admission.admission_status(paths)
    except AdmissionRefused:
        return ["admission_lost"]
    current = status["admission"]
    named = None if current is None else (current["owner"], current["generation"], current["state"])
    if named != (ticket.owner, ticket.generation, "admitted") or not status["inhibit"] or not ticket.holds():
        problems.append("admission_lost")
    if admission.recorder_lock_held(paths, clock=clock):
        problems.append("recorder_running")
    return problems


def read_contract(env):
    """(contract, caps) of orders.pilot through the pilot context's local read-only reader; Refused when the config
    names no pilot or the contract cannot be read or parsed."""
    if not pilot_enabled(env.config):
        raise Refused("pilot_not_configured")
    section = env.config["orders"]["pilot"]
    try:
        raw = env.pilot_context.read_contract(section["contract_path"])
        return pilot.contract_from_bytes(raw), pilot.decimal_loads(raw)["caps"]
    except (OSError, pilot.PilotError, KeyError, TypeError):
        raise Refused("pilot_contract_unreadable") from None


def _fold_says_armed(config, events, now_ns, contract_reader):
    """pilot_session_live's fold term, lock free (see there)."""
    armed = None
    try:
        if pilot_enabled(config):
            section = config["orders"]["pilot"]
            reader = contract_reader or pilot_gate.PilotContext(repo_root=pilot_gate.MODULE_ROOT).read_contract
            contract = pilot.contract_from_bytes(reader(section["contract_path"]))
            if contract.sha256 == section["contract_sha256"] == pilot_gate.journal_contract_sha256(events):
                armed = pilot.session_armed(pilot.fold(events, contract=contract, now_ns=now_ns), now_ns=now_ns)
    except (OSError, KeyError, TypeError, ValueError, ArithmeticError, AttributeError, BetfairError):
        armed = None
    if armed is None:
        try:
            armed = pilot_gate.session_window_open(events, now_ns=now_ns)
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, BetfairError):
            armed = True
    return bool(armed)


def pilot_session_live(paths, config, events, now_ns, *, ticket=None, contract_reader=None, clock=None):
    """§4.13: True when a second process must not settle. Armed is the fold's answer when orders.pilot is configured
    and its local read-only contract parses and matches both orders.pilot and the journal's registered contract;
    otherwise (no orders.pilot, an unreadable or different contract, a fold that raises) it is the gateway's own
    fallback, the journal-only UNTIL window (pilot_gate.session_window_open), so the refusal ends at UNTIL (NP-1).
    With ticket None (no ownership) also True when the owner lock is held or unknown while ADMISSION names a
    pilot-session owner or cannot be read; with a ticket the lock term is dropped entirely (no probe, no holds(), no
    ADMISSION read: holding the owner lock already shuts out pilot-run)."""
    if _fold_says_armed(config, events, now_ns, contract_reader):
        return True
    if ticket is not None:
        return False
    if admission.owner_lock_state(paths, **({"clock": clock} if clock is not None else {})) == "free":
        return False
    try:
        current = admission.admission_status(paths)["admission"]
    except AdmissionRefused:
        return True
    return current is not None and current["owner"].startswith("pilot-session:")


def pilot_session_live_fold(paths, config, *, now_ns, contract_reader=None):
    """[] or ["pilot_session_armed"] or ["live_journal_unreadable"]: pilot_session_live's ticket branch, lock free, for
    Yu's typed recording-release (design v9 §3.5 step 3), which holds the owner lock. A live journal that lstat
    reports absent (ENOENT) has no session; any other lstat failure, or a journal pilot_events_after cannot read,
    refuses live_journal_unreadable (pilot_events_after alone reads an absent file as unreadable)."""
    path = paths.journal_path("live")
    try:
        os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError:
        return ["live_journal_unreadable"]
    read = pilot_events_after(path, 0)
    if not read["readable"]:
        return ["live_journal_unreadable"]
    if pilot_gate.latest_note(read["events"], "session_start") is None:
        return []
    return ["pilot_session_armed"] if _fold_says_armed(config, read["events"], now_ns, contract_reader) else []


def owner_live_lines(paths):
    """The owner_live line (design v9 §3.4.6), with the ADMISSION's owner when it can be read without a lock."""
    try:
        current = admission.admission_status(paths)["admission"]
    except AdmissionRefused:
        current = None
    if current is None:
        return [OWNER_LIVE_LINE]
    return [f"{OWNER_LIVE_LINE} (ADMISSION owner {current['owner']}, generation {current['generation']}, state "
            f"{current['state']})"]


def before_release_lines(paths, *, handoffs, generation=None, armed_file_kept=False, flag="", login_reset=False,
                         clock=None):
    """The "before recording-release" block (§4.8; shared with pilot-end-session and ARM pre-check 7a), only the lines
    that apply, in order: (a) each handoff, unchanged; (b) reconcile for an unresolved order; (c) settle-market for an
    EXECUTABLE one; (d) resume after a kill or HALT; (e) last, the recording-release line, naming generation N whenever
    a ticket was held or a read shows ADMISSION N, the INHIBIT-only form only when a read just confirmed ADMISSION
    absent with INHIBIT set, else that the recording state is unknown (P108-R2-3). admission_status is read before
    (b) and (c): their reconcile and settle-market commands carry --continue-admission N only when N is the ticket's or
    a successful read shows ADMISSION N (the fresh command with ADMISSION absent; no flag when the read fails). With a
    kept armed-session file the fold is still armed, so a settle-market continuation would refuse: the pilot-end-session
    line then comes right after the handoffs, before (b) and (c), and the last line has no prefix (design v9 §3.4.6,
    amending P1-08 §4.8 step 7)."""
    lines = [handoff_text(h) for h in handoffs]
    try:
        status = admission.admission_status(paths)
    except AdmissionRefused:
        status = None
    shown = generation if generation is not None else (
        status["admission"]["generation"] if status is not None and status["admission"] is not None else None)
    cont = f" --continue-admission {shown}" if shown is not None else ""
    if armed_file_kept:
        lines.append("run pilot-end-session --mode live first; it prints this block again")
    for item in handoffs:
        if item["state"] == "ABSENT_UNCONFIRMED":
            lines.append(f"then run reconcile --mode live{flag}{cont}; if the Betfair website and Bet Angel show no "
                         f"such bet, run reconcile --mode live{flag}{cont} --accept {item['ref']} --confirm-live TOKEN")
        elif item["state"] in UNRESOLVED_STATES:
            lines.append(f"then run reconcile --mode live{flag}{cont}")
        elif item["state"] == "EXECUTABLE":
            lines.append(f"it stays EXECUTABLE until you settle market {item['market_id']} with "
                         f"settle-market{flag}{cont} after it closes")
    extra = {"clock": clock} if clock is not None else {}
    problems = admission.live_state_problems(paths, **extra)
    if "order_recorded" in problems:                     # SIG-4: an intent with no submit blocks release until then
        lines.append(RECORDED_LINE.format(flag=flag, cont=cont))
    if "kill_file_engaged" in problems or "halt_active" in problems:
        lines.append("then resume once the kill has been dealt with")
    if login_reset:
        lines.append(LOGIN_RESET_LINE)
    if shown is not None:
        lines.append(f"recording stays off until Yu's typed recording-release (generation {shown})")
    elif status is None:
        lines.append("recording state unknown; run status before any release")
    elif status["inhibit"]:
        lines.append("recording stays off until Yu's typed recording-release in its INHIBIT-only form")
    return lines


def release_lines(result, flag=""):
    """The lines for release_own's result: released, or every problem (the login-reset line first for a login_* code),
    the owner and generation N, and that recording stays off until Yu's typed recording-release (P108-R2-3)."""
    if result["released"]:
        return [f"recording released (generation {result['generation']})"]
    lines = []
    if result["outcome"] == "inhibit_preexisting":
        lines.append(f"INHIBIT predates this command (or ownership is retained); recording stays off until Yu's typed "
                     f"recording-release (generation {result['generation']})")
        return lines
    if any(str(code).startswith("login_") for code in result["problems"]):
        lines.append(LOGIN_RESET_LINE)
    lines.append(f"release refused: {result['outcome']} ({', '.join(result['problems'])}); owner {result['owner']}, "
                 f"generation {result['generation']}")
    if "order_recorded" in result["problems"]:
        lines.append(RECORDED_LINE.format(flag=flag, cont=f" --continue-admission {result['generation']}"))
    lines.append(f"recording stays off until Yu's typed recording-release (generation {result['generation']})")
    return lines


def admit_refusal_lines(exc, paths):
    """§4.3 step 2 on AdmissionRefused: outcome, detail and every problem (the login-reset line first for a login_*
    code), then one admission_status read for the recording advice: the generation when ADMISSION exists, the INHIBIT-
    only form only when the read shows no ADMISSION with INHIBIT set, else that the state is unknown. Never the refused
    pending generation as a release target."""
    lines = []
    problems = list(exc.problems) or ([exc.detail] if exc.detail else [])
    if any(str(code).startswith("login_") for code in problems):
        lines.append(LOGIN_RESET_LINE)
    lines.append(f"admission refused: {exc.outcome}" + (f" ({', '.join(problems)})" if problems else ""))
    try:
        status = admission.admission_status(paths)
    except AdmissionRefused:
        return lines + ["recording state unknown; run status before any release"]
    if status["admission"] is not None:
        lines.append(f"recording stays off until Yu's typed recording-release (generation "
                     f"{status['admission']['generation']})")
    elif status["inhibit"]:
        lines.append("recording stays off until Yu's typed recording-release in its INHIBIT-only form")
    return lines


def gates(env, command):
    """Static arming for one P1-08 command (A2, A11, A13: the resume set): NotArmed with every failure. The Keychain
    anchor is read (A13), never a secret."""
    paths, clock = env.paths, env.clock
    path = paths.journal_path("live")
    existing = OrderJournal(path, mode="live", clock=clock) if path.exists() else None
    try:
        anchor, state = {}, None
        if existing is not None:
            anchor = existing.anchor()
            try:
                state = existing.fold(now_ns=clock.time_ns())
            except JournalError:
                state = None
        report = static_arming(env.config, command=command, paths=paths, cli_mode="live",
                               confirm_token=getattr(env.args, "confirm_live", None),
                               bet_angel_paused=bool(getattr(env.args, "bet_angel_paused", False)), env=env.environ,
                               confirmer=env.confirmer, keychain_runner=env.keychain_runner, journal_anchor=anchor,
                               now_ns=clock.time_ns(), journal_state=state, journal=existing)
    finally:
        if existing is not None:
            existing.close()
    if not report["armed"]:
        raise NotArmed(report["failures"][0], detail={"failures": report["failures"]})
    return report


def _publish(env, journal):
    """Best effort (C17, optional): publish the anchor after a typed note, as resume does; a failure only warns."""
    try:
        publish_anchor(journal, env.paths, runner=env.keychain_runner, clock=env.clock)
    except CredentialError as exc:
        env.terminal.line(f"Betfair warning: {safe_code(str(exc))}; the Keychain anchor lags the journal until the next "
                          f"live command writes it")


def _token_line(contract):
    return contract.sha256[:12]


def _cause_copy(env, path_text, prefix, stop_id):
    """Copy the cause file (non-empty) to pilot_dir/<prefix>-<stop-id>-<compact>.txt at 0600 with O_CREAT|O_EXCL (never
    over another cause), fsync file and directory -> (sha256, path). Refused on any failure."""
    paths = env.paths
    try:
        data = Path(path_text).read_bytes()
    except (OSError, TypeError):
        raise Refused(f"{prefix}_cause_unreadable") from None
    if not data.strip():
        raise Refused(f"{prefix}_cause_empty")
    paths.ensure()
    pilot_gate.private_dir(paths.pilot_dir, paths.profile_dir)
    target = paths.pilot_dir / f"{prefix}-cause-{stop_id}-{utc_compact_from_ns(env.clock.time_ns())}.txt"
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise Refused(f"{prefix}_cause_copy_failed") from None
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory = os.open(str(paths.pilot_dir), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        raise Refused(f"{prefix}_cause_copy_failed") from None
    return bytes_sha256(data), target


# --- pilot-register (§4.2) ------------------------------------------------------------------------------------


def register(env):
    """pilot-register: REGISTER PILOT <contract12> <code12> (note §3, §7; C17). Read-only pre-checks before the prompt,
    then under .gateway.lock: the receipt (exact keys, 0600), its self-check, and the registration note as a compare-
    and-append on the head Yu saw. A moved head leaves the receipt orphaned and inert (its path is printed)."""
    paths, config, clock, ctx = env.paths, env.config, env.clock, env.pilot_context
    if paths.profile != RECORDER_PROFILE:
        raise Refused("pilot_profile_not_recorder_profile")
    gates(env, "pilot-register")
    if not pilot_enabled(config):
        raise Refused("pilot_not_configured")
    section = config["orders"]["pilot"]
    try:
        raw = ctx.read_contract(section["contract_path"])
        contract = pilot.contract_from_bytes(raw)
        caps = pilot.decimal_loads(raw)["caps"]
    except (OSError, pilot.PilotError, KeyError, TypeError):
        raise Refused("pilot_contract_not_frozen") from None
    if contract.sha256 != section["contract_sha256"]:
        raise Refused("pilot_contract_differs")
    differs = pilot_gate.config_holds_contract(config, contract, caps)
    if differs:
        raise Refused("pilot_config_differs:" + ",".join(differs))
    git = git_identity(Path(ctx.repo_root).resolve(), runner=ctx.git_runner)
    candidate = {"code_commit": git["commit"], "code_tree": git["tree"], "code_hashes_digest": ctx.pinned()}
    codes = (["pa2_identity_unknown"] if None in candidate.values()
             else pilot_gate.code_identity(ctx, candidate))
    if codes:
        raise Refused("pilot_code_identity:" + ",".join(codes))
    path = paths.journal_path("live")
    paths.ensure()
    journal = OrderJournal(path, mode="live", clock=clock)
    try:
        events = list(journal.events())
        for kind, inner, _ in pilot_gate._pilot_notes(events):
            if inner["contract_sha256"] != contract.sha256:
                raise Refused("pilot_journal_other_contract")
            if kind == "registration" and inner["predecessor_contract_sha256"] is not None:
                raise Refused("pilot_journal_other_contract")
        core = fold_events(events, mode="live", now_ns=clock.time_ns())
        if kill_engaged(paths) or core["halted"]:
            raise Refused("pilot_register_halted")
        seen = head_of(events)
        code12 = candidate["code_commit"][:12]
        line = f"REGISTER PILOT {contract.sha256[:12]} {code12}"
        banner = (f"MODE=LIVE: REGISTER PILOT, profile {paths.profile}: contract {contract.sha256}, code commit "
                  f"{candidate['code_commit']} tree {candidate['code_tree']}, code hashes {candidate['code_hashes_digest']}"
                  f", config {config_sha256(config)}. It sends no order; it allows pilot placement only after PILOT ARM.")
        env.confirmer.confirm_line(line, banner=banner)
        with file_lock(paths.gateway_lock, timeout_s=config["http"]["order_deadline_s"] + 5, clock=clock):
            if kill_engaged(paths):
                raise Refused("pilot_register_halted")
            pilot_gate.private_dir(paths.pilot_dir, paths.profile_dir)
            now = clock.time_ns()
            receipt_path = pilot_gate.registration_path(paths, contract.sha256, now)
            if os.path.lexists(receipt_path):
                raise Refused("pilot_registration_path_exists")
            receipt = {"schema": pilot_gate.REGISTRATION_SCHEMA, "contract_sha256": contract.sha256,
                       "code_commit": candidate["code_commit"], "code_tree": candidate["code_tree"],
                       "code_hashes_digest": candidate["code_hashes_digest"], "config_sha256": config_sha256(config),
                       "registered_at": utc_from_ns(now), "journal_seq": seen.get("seq", 0)}
            receipt_sha = atomic_write_json(receipt_path, receipt, mode=0o600)
            try:
                pilot_gate.read_registration(paths, receipt_sha)
            except pilot_gate.GateRefusal:
                raise Refused("pilot_registration_unreadable", [f"receipt left at {receipt_path}"]) from None
            try:
                event = append_pilot(journal, pilot.pilot_note(
                    "registration", contract.sha256, registration_receipt_sha256=receipt_sha,
                    predecessor_contract_sha256=None), expect_head=seen)
            except JournalError as exc:
                if "journal_head_moved" in str(exc):
                    raise Refused("journal_changed_during_register",
                                  [f"the receipt is left inert at {receipt_path}"]) from None
                raise
        _publish(env, journal)
    finally:
        journal.close()
    env.terminal.json({"status": "registered", "receipt_path": str(receipt_path), "receipt_sha256": receipt_sha,
                       "note_seq": event["seq"]})
    return 0


# --- PILOT ARM (§4.3): the view, the pre-checks and the locked validation ----------------------------------------


def _until_ns(text, now_ns):
    """until_ns for HHMM on now's UTC day, refused unless now + 6 min < until < the day's end (C9)."""
    match = UNTIL_RE.fullmatch(text or "")
    if match is None:
        raise Refused("pilot_arm_until_invalid")
    day_start = now_ns - now_ns % pilot.DAY_NS
    until = day_start + (int(match.group(1)) * 3600 + int(match.group(2)) * 60) * NS
    if not now_ns + pilot_gate.END_MARGIN_NS < until < day_start + pilot.DAY_NS:
        raise Refused("pilot_arm_until_invalid")
    return until


def _view(env, now_ns):
    """One whole-journal read and the identity of contract, receipt, code and config -> a dict; Refused on an
    unreadable journal, an identity drift (with every code), no registration or a fold that raises."""
    paths, config = env.paths, env.config
    read = pilot_events_after(paths.journal_path("live"), 0)
    if not read["readable"]:
        raise Refused("pilot_journal_unreadable")
    events = read["events"]
    if not pilot_enabled(config):
        raise Refused("pilot_identity_differs:pilot_not_configured")
    section = config["orders"]["pilot"]
    codes, contract, caps = [], None, None
    try:
        raw = env.pilot_context.read_contract(section["contract_path"])
    except OSError:
        codes.append("pa1_contract_unreadable")
    else:
        try:
            contract = pilot.contract_from_bytes(raw)
            caps = pilot.decimal_loads(raw)["caps"]
        except pilot.PilotError:
            codes.append("pa1_contract_invalid")
    if contract is None:
        raise Refused("pilot_identity_differs:" + ",".join(codes))
    registered = pilot_gate.journal_contract_sha256(events)
    if contract.sha256 != section["contract_sha256"] or registered not in (None, contract.sha256):
        codes.append("pa1_contract_differs")
    note = pilot_gate.latest_note(events, "registration", contract.sha256)
    receipt = receipt_sha = None
    if note is not None:
        receipt_sha = note["registration_receipt_sha256"]
        try:
            receipt = pilot_gate.read_registration(paths, receipt_sha)
        except pilot_gate.GateRefusal:
            receipt = None
    if receipt is not None:
        if receipt["contract_sha256"] != contract.sha256:
            codes.append("pa1_contract_differs")
        codes += pilot_gate.code_identity(env.pilot_context, receipt)
        if config_sha256(config) != receipt["config_sha256"]:
            codes.append("pa3_config_differs")
    if pilot_gate.config_holds_contract(config, contract, caps):
        codes.append("pa3_config_not_contract")
    codes = sorted(set(codes), key=codes.index)
    if codes:
        raise Refused("pilot_identity_differs:" + ",".join(codes),
                      ["a code or config change needs a new receipt, Astra's acceptance and the go-live consent again "
                       "(note:87)"])
    if note is None:
        raise Refused("pilot_not_registered")
    if receipt is None:
        raise Refused("pilot_registration_unreadable")
    try:
        projection = recovery_projection(events, contract=contract, now_ns=now_ns)
    except (pilot.PilotError, JournalError, KeyError, TypeError, ValueError):
        raise Refused("pilot_fold_failed") from None
    return {"events": events, "contract": contract, "receipt": receipt, "receipt_sha256": receipt_sha,
            "state": projection["state"], "core": projection["core"], "projection": projection,
            "head": head_of(events)}


def _arm_checks(env, *, now_ns, day, until_ns, absence):
    """ARM's pre-checks in the order of §4.3 (2-6, 7a, 7b, 8-11, 11a, 11b; the profile is checked first, 7c and 7d by
    the caller). absence adds 7b, 11a and 11b, which run only before the prompt (P108-R2-1). Read only."""
    paths, config, clock = env.paths, env.config, env.clock
    if not now_ns + pilot_gate.END_MARGIN_NS < until_ns < now_ns - now_ns % pilot.DAY_NS + pilot.DAY_NS:
        raise Refused("pilot_arm_until_invalid")
    view = _view(env, now_ns)
    state, core, projection = view["state"], view["core"], view["projection"]
    if any(session["utc_day"] == day for session in state.sessions):
        raise Refused("pilot_arm_already_today")
    if state.l_fields_refuted:
        raise Refused("pilot_arm_l_fields_refuted")
    if state.ending is not None:
        raise Refused("pilot_arm_placing_ended")
    if projection["handoffs"]:
        raise Refused("pilot_order_unresolved", before_release_lines(
            paths, handoffs=projection["handoffs"], flag=env.paused_flag, clock=clock))
    if absence:
        found = armed_session_check(paths)
        if found == ["pilot_session_armed"]:
            raise Refused("pilot_end_session_needed", ["an armed-session file is left from an earlier run: run "
                                                       "pilot-end-session --mode live first"])
        if found:
            raise Refused("pilot_session_state_unknown")
    if kill_engaged(paths) or core["halted"] or core["clock_regression"]:
        raise Refused("pilot_arm_halted")
    if pilot_gate.open_manual_refs(view["events"], core["orders"]):
        raise Refused("manual_order_open")
    try:
        ledger = ledger_status(config, paths, clock=clock)
    except AuthError:
        raise Refused("pilot_arm_login_ledger") from None
    if ledger["consecutive_failures"] or ledger["banned_until"] is not None:
        raise Refused("pilot_arm_login_ledger")
    problems = admission.live_state_problems(paths, clock=clock) + admission.ledger_problems(config, paths, clock=clock)
    if problems:
        raise Refused("pilot_arm_release_would_refuse:" + ",".join(problems),
                      [RECORDED_LINE.format(flag=env.paused_flag, cont="")] if "order_recorded" in problems else [])
    if absence:
        try:
            status = admission.admission_status(paths)
        except AdmissionRefused:
            raise Refused("admission_state_unknown",
                          ["recording state unknown; run status before any release"]) from None
        current = status["admission"]
        if current is not None:
            step = (f"reconcile --continue-admission {current['generation']} first" if projection["handoffs"]
                    else f"Yu's typed recording-release (generation {current['generation']})")
            raise Refused("admission_held", [f"admission held by {current['owner']}, generation "
                                             f"{current['generation']}, state {current['state']}; next: {step}"])
        if status["inhibit"]:
            raise Refused("pilot_arm_release_would_refuse:inhibit_preexisting",
                          ["run your typed recording-release in its INHIBIT-only form first"])
    return view


def _review_ids(state):
    return [stop["stop_id"] for stop in state.active_stops if stop["scope"] == "review"]


def arm_banner(*, contract, day, profile, session_end_ns, pt4):
    end = utc_from_ns(session_end_ns)[11:19]
    last = utc_from_ns(session_end_ns - pilot_gate.END_MARGIN_NS)[11:19]
    return (f"MODE=LIVE: PILOT ARM starts one attended pilot session for contract {contract.sha256[:12]} on {day}, "
            f"profile {profile}. Until {end}Z, the executor may send real-money orders (BACK £2.00, at most "
            f"{5 if pt4 else 25} PLACE records today) with no per-order line (E1). No reservation starts after {last}Z. "
            f"This is today's only ARM, and any stop ends today's placing. Manual place and live-probe stay refused "
            f"until {end}Z, even if the session stops early. Recording turns off now: a running recorder is stopped "
            f"(SIGTERM, up to 90 s wait), and INHIBIT stays set. A clean end releases it. After any other end, recording "
            f"stays off until your typed recording-release. Each Keychain item is read once. Stay at this terminal. "
            f"Ctrl-C, SIGTERM or closing the terminal stops the session; a send already started still finishes its "
            f"cleanup.")


def run(env):
    """pilot-run: PILOT ARM (§4.3), the loop (§4.4-§4.7) and the one end path (§4.8) -> the exit code (0 a clean end
    released; 5 any other end once a session_start exists). Refusals before the session_start raise Refused."""
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    if paths.profile != RECORDER_PROFILE:
        raise Refused("pilot_profile_not_recorder_profile")
    gates(env, "pilot-arm")
    now = clock.time_ns()
    day = pilot.utc_day(now)
    until_ns = _until_ns(env.args.until, now)
    view = _arm_checks(env, now_ns=now, day=day, until_ns=until_ns, absence=True)
    journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
    try:
        if view["state"].due_stops:                                                  # 7c, the only pre-prompt write
            flush_due(journal, view["state"], paths=paths, config=config, clock=clock)
            view = _arm_checks(env, now_ns=now, day=day, until_ns=until_ns, absence=False)
        active = _review_ids(view["state"])                                          # 7d
        if active:
            raise Refused("pilot_review_stop_active:" + ",".join(active))
        contract, state = view["contract"], view["state"]
        session_id = f"s{day.replace('-', '')}-{secrets.token_hex(4)}"
        while any(s["session_id"] == session_id for s in state.sessions):
            session_id = f"s{day.replace('-', '')}-{secrets.token_hex(4)}"
        session_end = pilot.session_end_ns({"until_ns": until_ns, "utc_day": day})
        line = f"PILOT ARM {contract.sha256[:12]} {day.replace('-', '')} UNTIL {env.args.until}Z"
        env.confirmer.confirm_line(line, banner=arm_banner(contract=contract, day=day, profile=paths.profile,
                                                            session_end_ns=session_end,
                                                            pt4=state.pt4_receipt_seq is None))
        stop = StopFlag()
        stop.install()                                                               # step 1 (C29)
        try:
            return _armed(env, journal, view, stop, session_id=session_id, day=day, until_ns=until_ns,
                          session_end=session_end)
        finally:
            stop.restore()
    finally:
        journal.close()


def _armed(env, journal, view, stop, *, session_id, day, until_ns, session_end):
    """Steps 2-8 of §4.3, then the loop and the end path, all under the owner lock."""
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    contract = view["contract"]
    owner = f"pilot-session:{session_id}"
    purpose = f"RS-19 pilot {contract.sha256[:12]} {day.replace('-', '')}"
    try:
        with admission.live_ownership(paths, owner=owner, purpose=purpose, config=config, clock=clock,
                                      processes=env.processes) as ticket:
            return _arm_under_ownership(env, journal, view, stop, ticket, session_id=session_id, day=day,
                                        until_ns=until_ns, session_end=session_end)
    except admission.OwnershipRefused as exc:
        if exc.code == "owner_live":                           # design v9 §3.4.6: a manual owner is not a pilot-run
            raise Refused("owner_live", owner_live_lines(paths)) from None
        if exc.code == "owner_state_unknown":
            raise Refused("owner_state_unknown") from None
        lines = [f"admission held by {exc.owner}, generation {exc.generation}"] if exc.owner else []
        raise Refused(exc.code, lines) from None
    except AdmissionRefused as exc:
        raise Refused(exc.outcome, admit_refusal_lines(exc, paths)) from None


def _arm_under_ownership(env, journal, view, stop, ticket, *, session_id, day, until_ns, session_end):
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    contract = view["contract"]
    generation = ticket.generation
    armed_sha = None

    def refuse(code, lines=()):
        """Step 7: the armed file only while it is still ours, release_own, generation N printed in every case."""
        out = list(lines)
        if armed_sha is not None:
            deleted = delete_armed_session_if(paths, armed_sha)
            if deleted != "deleted":
                out.append(f"armed-session file {deleted}; left as it is at {armed_session_path(paths)}")
        elif armed_session_check(paths):
            out.append(f"an armed-session file not written by this ARM is left at {armed_session_path(paths)}")
        result = admission.release_own(ticket, paths, config=config, clock=clock)
        out += release_lines(result, env.paused_flag)
        out.append(f"owner {ticket.owner}, generation {generation}")
        raise Refused(code, out)

    if ticket.inhibit_preexisting:                                                     # the step 2 race
        refuse("pilot_arm_release_would_refuse:inhibit_preexisting")
    try:                                  # OWN-4: once the ticket exists, every unexpected end names N and the way out
        record = {"schema": ARMED_SCHEMA, "session_id": session_id, "contract_sha256": contract.sha256,
                  "registration_receipt_sha256": view["receipt_sha256"], "utc_day": day, "until_ns": until_ns,
                  "session_end_ns": session_end, "admission_owner": ticket.owner, "admission_generation": generation,
                  "pid": os.getpid(), "process_start_time": env.processes.start_time(os.getpid()) or "unknown",
                  "armed_at": clock.utc_now()}
        try:
            armed_sha = write_armed_session(paths, record)                                 # step 3
        except (OSError, ValueError, BetfairError):
            refuse("pilot_arm_session_file_unwritten")
        try:
            current = admission.admission_status(paths)["admission"]
        except AdmissionRefused:
            refuse("admission_state_unknown")
        if current is None or (current["owner"], current["generation"], current["state"]) != (ticket.owner, generation,
                                                                                             "admitted"):
            refuse("pilot_arm_ownership_changed")
        if stop.requested:                                                                  # step 4
            refuse("pilot_arm_stopped")
        try:
            credentials = load_session_credentials(config, paths, runner=env.keychain_runner, stop=stop)     # step 5
        except _ArmStopped:
            refuse("pilot_arm_stopped")
        except CredentialError:
            refuse("pilot_arm_credentials_unavailable")
        try:
            lock = file_lock(paths.gateway_lock, timeout_s=config["http"]["order_deadline_s"] + 5, clock=clock)
            held = lock.__enter__()
        except LockBusy:
            refuse("lock_busy")
        try:
            event = _arm_locked(env, journal, view, stop, ticket, held, session_id=session_id, day=day, until_ns=until_ns,
                                armed_sha=armed_sha)
        except Refused as exc:
            lock.__exit__(None, None, None)
            refuse(exc.code, exc.lines)
        except BaseException:
            lock.__exit__(None, None, None)
            raise
        lock.__exit__(None, None, None)                                                     # step 8
        session = {"session_id": session_id, "seq": event["seq"], "until_ns": until_ns, "utc_day": day,
                   "session_end_ns": session_end}
        run = Run(env, journal=journal, contract=contract, ticket=ticket, stop=stop, session=session,
                  credentials=credentials, armed_sha=armed_sha, receipt_sha256=view["receipt_sha256"])
    except Refused:
        raise
    except BaseException as exc:
        kept = bool(armed_session_check(paths))
        w.line(f"ARM did not finish ({safe_code(str(exc) or type(exc).__name__)}): owner {ticket.owner}, generation "
               f"{generation}; " + (f"armed-session file kept at {armed_session_path(paths)}; run pilot-end-session "
                                    f"--mode live, then " if kept else "recording stays off until ")
               + f"Yu's typed recording-release (generation {generation})")
        raise
    return run.go()


def _arm_locked(env, journal, view, stop, ticket, held, *, session_id, day, until_ns, armed_sha):
    """Step 6 under the one .gateway.lock hold: 6a the flag, 6b KILL, 6c this ticket's own state, 6d the content
    pre-checks on a fresh fold, 6e due stops (flushed on the held lock) and 7d, 6f the session_start compare-and-append on
    the head Yu saw, stamped before UNTIL - 6 min -> the event."""
    paths, config, clock = env.paths, env.config, env.clock
    if stop.requested:
        raise Refused("pilot_arm_stopped")
    if kill_engaged(paths):
        raise Refused("pilot_arm_halted")
    if not ticket.holds():
        raise Refused("owner_lock_lost")
    try:
        status = admission.admission_status(paths)
    except AdmissionRefused:
        raise Refused("admission_state_unknown") from None
    current = status["admission"]
    named = None if current is None else (current["owner"], current["generation"], current["state"])
    if named != (ticket.owner, ticket.generation, "admitted") or not status["inhibit"]:
        raise Refused("pilot_arm_ownership_changed")
    if admission.recorder_lock_held(paths, clock=clock):
        raise Refused("recorder_running")
    try:
        found_sha, record = read_armed_session(paths)
    except (OSError, ValueError):
        raise Refused("pilot_session_state_unknown") from None
    if (found_sha != armed_sha or record["session_id"] != session_id or record["admission_owner"] != ticket.owner
            or record["admission_generation"] != ticket.generation):
        raise Refused("pilot_arm_session_file_changed")
    now = clock.time_ns()
    if pilot.utc_day(now) != day:
        raise Refused("pilot_arm_until_invalid")
    fresh = _arm_checks(env, now_ns=now, day=day, until_ns=until_ns, absence=False)          # 6d
    if fresh["state"].due_stops:                                                             # 6e
        appended = flush_due_held(journal, fresh["state"])
        raise Refused("pilot_arm_due_stops", [f"journaled due stop {stop_id}; retype the ARM line"
                                              for stop_id in appended])
    active = _review_ids(fresh["state"])
    if active:
        raise Refused("pilot_review_stop_active:" + ",".join(active))
    try:                                                                                     # 6f
        event = append_pilot(journal, pilot.pilot_note(
            "session_start", view["contract"].sha256, session_id=session_id,
            registration_receipt_sha256=view["receipt_sha256"], utc_day=day, until_ns=until_ns),
            expect_head=view["head"], before_ns=until_ns - pilot_gate.END_MARGIN_NS)
    except JournalError as exc:
        if "journal_append_too_late" in str(exc):
            raise Refused("pilot_arm_until_invalid") from None
        if "journal_head_moved" in str(exc):
            raise Refused("journal_changed_during_arm") from None
        raise
    if not held.held():
        env.terminal.line("Betfair warning: .gateway.lock was replaced during ARM")
    return event


# --- the run: scheduler, placement opens, settle points, capture, the end path (§4.4-§4.8) ------------------------


class Run:
    """One armed session's loop. Every write it owes is a plain append through append_pilot; every Betfair call goes
    through a gateway the factory opens, or a read session it built; nothing here retries a refusal."""

    def __init__(self, env, *, journal, contract, ticket, stop, session, credentials, armed_sha, receipt_sha256):
        self.env, self.journal, self.contract, self.ticket, self.stop = env, journal, contract, ticket, stop
        self.session, self.credentials, self.armed_sha = session, credentials, armed_sha
        self.receipt_sha256 = receipt_sha256
        self.paths, self.config, self.clock, self.w = env.paths, env.config, env.clock, env.terminal
        self.read_sessions = []                   # (session, read client) this process opened: all logged out
        self.scheduler = None
        self.abnormal = None                      # the first abnormal cause (exception, kill, admission, login)
        self.handoff_due = False
        self.login_failed = False
        self.login_budget = False
        self.ledger_unresolved_login = False
        self.manual_bet = False
        self.end_code = None
        self.c_queue = {}                         # ref -> item (bet id known, EXECUTION_COMPLETE after its open)
        self.offered = set()
        self.failures_at_arm = consecutive_failures(self.config, self.paths, clock=self.clock)
        self.retired = False

    # -- small helpers ---------------------------------------------------------------------------------------------

    def now(self):
        return self.clock.time_ns()

    def mark_abnormal(self, cause):
        if self.abnormal is None:
            self.abnormal = cause

    def flag_seen(self):
        """True once the flag is set; prints the stop line once (best effort)."""
        if self.stop.requested:
            if not self.stop.announced:
                self.stop.announced = True
                self.w.line(STOP_LINE)
            return True
        return False

    def signal_stopped(self, exc):
        """A SignalStop the inner guard raised is Yu's stop; its signal names the summary's signal when unset."""
        self.stop.requested = True
        if self.stop.name is None and str(exc) in _SIGNAL_NAMES.values():
            self.stop.name = str(exc)

    def deferring(self, value):
        signals = getattr(self.env.ctx, "signals", None)
        if signals is not None:
            signals.deferring = value

    def owe(self, payload, **columns):
        """A write pilot-run owes (§3.6 SX-1, SX-2): deferring set for the append, restored, then pending read."""
        signals = getattr(self.env.ctx, "signals", None)
        before = signals.deferring if signals is not None else None
        if signals is not None:
            signals.deferring = True
        try:
            return append_pilot(self.journal, payload, **columns)
        finally:
            if signals is not None:
                signals.deferring = before
                self.stop.take_pending(signals)

    def stop_note(self, code, anchor, *, source_seq=None):
        """A session stop, once: an id already journaled in this session is not appended again (a helper completing
        an owed write after a signal may meet the stop the first attempt wrote)."""
        if self.journaled(pilot.session_stop_id(code, anchor)):
            return None
        return self.owe(session_stop(self.contract, code, anchor, source_seq=source_seq))

    def journaled(self, stop_id):
        """True when a stop note with stop_id is in the journal from this session's start (unreadable: False)."""
        read = self.read(max(0, self.session["seq"] - 1))
        return any(e["kind"] == "note" and (e["payload"].get("pilot") or {}).get("stop_id") == stop_id
                   for e in read["events"])

    def refusal(self, code, *, open_id=None, reservation_seq=None):
        return self.owe(refusal_note(self.contract, session_id=self.session["session_id"], open_id=open_id,
                                     reservation_seq=reservation_seq, code=code))

    def read(self, since=0):
        return pilot_events_after(self.paths.journal_path("live"), since)

    def fold(self):
        """A fresh read-only fold of the whole journal, or None."""
        read = self.read(0)
        if not read["readable"]:
            return None
        try:
            return recovery_projection(read["events"], contract=self.contract, now_ns=self.now())
        except (pilot.PilotError, JournalError, KeyError, TypeError, ValueError):
            return None

    def kill_seen(self):
        """§3.9: the KILL file or a kill / HALT kill_incomplete since the session_start (unreadable counts)."""
        if kill_engaged(self.paths):
            return True
        read = self.read(self.session["seq"])
        return not read["readable"] or read["kill_seq"] is not None or read["kill_incomplete_seq"] is not None

    def budget_blocks(self):
        return cap_blocks(self.config, self.paths, clock=self.clock)

    def sleep_until(self, target_ns, *, outside_open=True):
        """Wait in slices of at most 1 s on the injected clock, checking the flag (and the KILL file) after each ->
        True when target_ns was reached, False when the flag or a KILL file stopped the wait."""
        while self.now() < target_ns:
            if outside_open and (self.flag_seen() or kill_engaged(self.paths)):
                return False
            self.clock.sleep(min(SLICE_S, (target_ns - self.now()) / NS))
        return not (outside_open and self.flag_seen())

    def intact(self):
        """admission_intact before an action (§3.10): on failure the review note, no further login, abnormal."""
        problems = admission_intact(self.paths, self.ticket, clock=self.clock)
        if not problems:
            return True
        code = "recorder_running" if "recorder_running" in problems else "admission_lost"
        self.refusal(code)
        try:
            status = admission.admission_status(self.paths)
            seen = status["admission"]
            pid = (status["recorder_pid"] or {}).get("pid")
        except AdmissionRefused:
            seen, pid = None, None
        self.w.line(f"admission not intact ({', '.join(problems)}): owner {self.ticket.owner}, generation "
                    f"{self.ticket.generation}; seen {seen['owner'] + ' ' + str(seen['generation']) if seen else 'none'};"
                    f" recorder.pid {pid}")
        self.mark_abnormal(code)
        return False

    # -- login failures (§3.8) ------------------------------------------------------------------------------------

    def login_family(self, exc, *, reservation_seq=None):
        """Classify by exception type, LoginBudgetExceeded first (it subclasses AuthError), then by a ledger rise.
        -> True when it was a login-family failure (journaled), else False."""
        if isinstance(exc, (LoginStopped, ReadStopped)):        # a pilot read guard: no login was attempted
            if getattr(exc, "reason", None) in ("kill", "skipped_kill") or "kill" in str(exc):
                self.mark_abnormal("kill")
            return True
        if isinstance(exc, LoginBudgetExceeded):
            self.refusal("login_budget", reservation_seq=reservation_seq)
            self.login_budget = True
            self.mark_abnormal("login_budget")
            return True
        if isinstance(exc, AuthError) or self.ledger_rose():
            self.stop_note("login_failed", self.session["seq"], source_seq=self.session["seq"])
            self.login_failed = True
            self.mark_abnormal("login_failed")
            self.w.line(f"login failed ({safe_code(getattr(exc, 'status', None) or type(exc).__name__)}): the session "
                        f"ends; Yu decides on login-reset (note:125)")
            return True
        return False

    def ledger_rose(self):
        now = consecutive_failures(self.config, self.paths, clock=self.clock)
        return now is None or (self.failures_at_arm is not None and now > self.failures_at_arm)

    def ledger_check_after_signal(self):
        """After a SignalStop out of an open with no submit (§3.6): an unresolved or failed attempt now came from this
        run or one beside it; the summary says so. Read only; no new stop."""
        problems = admission.ledger_problems(self.config, self.paths, clock=self.clock)
        if "login_attempt_unresolved" in problems or "login_failure_after_barrier" in problems:
            self.ledger_unresolved_login = True

    # -- the scheduler (§4.4) -------------------------------------------------------------------------------------

    def read_client(self):
        """The scheduler's read session (credentials read), built once and logged out at the end."""
        if self.scheduler is None:
            self.scheduler = self.env.read_session_factory(self.credentials["read"], guard=self.read_guard)
            self.read_sessions.append(self.scheduler)
        return self.scheduler[1]

    def before_read(self):
        """The checks before any non-placement Betfair call: the flag, the kill (§3.9), admission_intact (§3.10)."""
        if self.flag_seen():
            return "stop"
        if self.kill_seen():
            self.mark_abnormal("kill")
            return "kill"
        if not self.intact():
            return "admission"
        return None

    def read_guard(self):
        """The guard of every pilot read session, asked at each login and each read attempt (P108-C3): "stop" once
        the flag is set, "kill" once a KILL file or a kill since the session start is seen (§3.9), else None."""
        if self.stop.requested:
            return "stop"
        if self.kill_seen():
            return "kill"
        return None

    def candidates(self):
        """Today's GB/IE WIN markets from the catalogue, first to start first: [(market_id, off_ns)], or None when the
        run must end (a stop, a kill, admission, a login failure, or the read failed)."""
        while self.budget_blocks():                        # no market to skip: wait in 1 s slices (§3.8)
            if self.before_read() is not None:
                return None
            self.clock.sleep(SLICE_S)
        if self.before_read() is not None:
            return None
        C = self.contract
        rest = {"eventTypeIds": [C.event_type_id], "marketCountries": sorted(C.country_codes),
                "marketTypeCodes": [C.market_type],
                "marketStartTime": {"from": utc_from_ns(self.now()), "to": utc_from_ns(self.session["session_end_ns"])}}
        try:
            rows = self.read_client().list_market_catalogue(rest, projections=("MARKET_START_TIME",), max_results=1000,
                                                            sort="FIRST_TO_START")
        except Exception as exc:          # a read the ledger shows as a login failure ends the session; else no market
            if not self.login_family(exc):
                self.end_code = "scheduler_read_failed"
            return None
        found = []
        for row in rows:
            try:
                found.append((row["marketId"], pilot.parse_utc_ns(row["marketStartTime"])))
            except (KeyError, TypeError, pilot.PilotError):
                continue
        return sorted(found, key=lambda item: (item[1], item[0]))

    def start_time(self, market_id):
        """The market's start time re-read uncached by marketIds (never catalogue_row's cache), or None."""
        if self.before_read() is not None or self.budget_blocks():
            return None
        try:
            rows = self.read_client().list_market_catalogue({"marketIds": [market_id]},
                                                            projections=("MARKET_START_TIME",), max_results=1)
            return pilot.parse_utc_ns(rows[0]["marketStartTime"]) if rows else None
        except Exception as exc:
            self.login_family(exc)
            return None

    def plan(self, state, candidates):
        """The next market to open, or None: reserved markets, the end margin, a trigger collision and a planned open
        whose time has passed are skipped (pre-checks only, never a retry, §13 ruling 1)."""
        end = self.session["session_end_ns"]
        deadline = int(self.config["orders"]["pilot"]["evidence_deadline_s"] * NS)
        now = self.now()
        for market_id, off in candidates:
            t0 = off + self.contract.window_from_offset_s * NS                  # off - 300 s, the reservation's time
            if market_id in state.reservations:
                continue
            if t0 + pilot_gate.END_MARGIN_NS >= end:                             # the end margin (gateway.py's too)
                continue
            if now > t0 - OPEN_LEAD_NS:                                          # its planned open has passed
                continue
            if pilot_gate.trigger_blocks(state, t0_ns=t0, evidence_deadline_ns=deadline) == "refuse":
                continue
            return market_id, off
        return None

    # -- the loop ---------------------------------------------------------------------------------------------------

    def go(self):
        """The loop, then the one end path whatever stopped it. An exception that escapes the loop (§4.3 step 8, §3.7)
        journals executor_exception, so the fold never shows an armed session with no live process."""
        try:
            self.loop()
        except Exception:
            self.mark_abnormal("executor_exception")
            with contextlib.suppress(Exception):
                self.refusal("executor_exception")
        except (self.env.signal_stop, KeyboardInterrupt):     # outside any open: Yu's stop
            self.stop.requested = True
            self.ledger_check_after_signal()             # a SignalStop out of a read session's login (§9.6)
        try:
            return self.end()
        except Exception as exc:                         # SIG-3: the end block still prints; never a release
            self.mark_abnormal("end_failed")
            w = self.w
            w.line(f"end: {safe_code(type(exc).__name__)}")
            after = self.fold()
            if after is None:
                w.lines(case_one_handoff([], seen_seq=0, market_id=None))
            w.lines(before_release_lines(self.paths, handoffs=after["handoffs"] if after is not None else [],
                                         generation=self.ticket.generation,
                                         armed_file_kept=bool(armed_session_check(self.paths)),
                                         flag=self.env.paused_flag, login_reset=self.login_failed, clock=self.clock))
            return 5

    def loop(self):
        candidates = None
        while True:
            if self.flag_seen():
                return
            if self.kill_seen():
                self.mark_abnormal("kill")
                return
            projection = self.fold()
            if projection is None:
                self.mark_abnormal("journal_unreadable")
                return
            state = projection["state"]
            if state.due_stops:
                try:
                    flush_due(self.journal, state, paths=self.paths, config=self.config, clock=self.clock)
                except LockBusy:
                    self.stop_note("lock_busy", self.session["seq"], source_seq=self.session["seq"])
                    return
                continue
            now = self.now()
            code = pilot.placement_authority(state, phase="reserve", now_ns=now, session_id=self.session["session_id"])
            if code is not None:
                self.end_code = pilot.AUTHORITY_CODES[code]
                return
            capacity = pilot.capacity_and_pause(self.contract, state, utc_day=pilot.utc_day(now),
                                                session_id=self.session["session_id"])
            if capacity["code"] == "cap":                  # Q16: end at the day limit, never a doomed reservation
                self.end_code = "day_limit"
                self.final_capture()
                return
            if candidates is None:
                candidates = self.candidates()
                if candidates is None:
                    return
            target = self.plan(state, candidates)
            if target is None:
                self.end_code = self.end_code or "no_plannable_market"
                if self.end_code == "no_plannable_market":
                    self.final_capture()
                return
            market_id, off = target
            open_at = off + self.contract.window_from_offset_s * NS - OPEN_LEAD_NS
            if not self.gap(projection, open_at):
                return
            if not self.wait_for_open(open_at):
                if not self.stop.requested and kill_engaged(self.paths):
                    self.mark_abnormal("kill")
                return
            fresh = self.start_time(market_id)
            if self.abnormal or self.stop.requested:
                return
            if fresh is None:
                candidates = [c for c in candidates if c[0] != market_id]
                continue
            if fresh != off:                               # the start moved: plan again on the new time
                candidates = sorted([c for c in candidates if c[0] != market_id] + [(market_id, fresh)],
                                    key=lambda item: (item[1], item[0]))
                continue
            outcome = self.placement(market_id, off)
            if outcome == "skip":
                candidates = [c for c in candidates if c[0] != market_id]
                continue
            if outcome != "continue":
                return
            candidates = [c for c in candidates if c[0] != market_id]

    def wait_for_open(self, open_at):
        """The wait to a planned open, in 1 s slices; every GAP_INTERVAL_NS of it is another gap (capture, settle), so
        a market's CLOSED book and L are read once its off has passed. -> False when the run must end."""
        while True:
            if not self.sleep_until(min(open_at, self.now() + GAP_INTERVAL_NS)):
                return False
            if self.now() >= open_at:
                return True
            projection = self.fold()
            if projection is None:
                self.mark_abnormal("journal_unreadable")
                return False
            if not self.gap(projection, open_at):
                return False

    def gap(self, projection, open_at):
        """Between opens: one capture batch (C first), then settle points when the next open is far enough away. ->
        False when the run must end."""
        if capture_entries(projection, queue=self.c_queue, session_id=self.session["session_id"], now_ns=self.now()):
            if self.capture_batch(open_at=open_at) == "end":     # C first, then the CLOSED book and L (§4.7)
                return False
        if self.flag_seen() or self.abnormal:
            return False
        if open_at - self.now() > SETTLE_LEAD_NS + PROMPT_TIMEOUT_NS + SETTLE_OVERHEAD_NS and not self.stop.hup:
            if self.settle_points(open_at) == "end":
                return False
        return not (self.flag_seen() or self.abnormal)

    # -- one placement open (§4.5) --------------------------------------------------------------------------------

    def placement(self, market_id, off):
        """One open, one reservation -> "continue", "skip" or "end"."""
        if self.flag_seen():
            return "end"
        if self.kill_seen():
            self.mark_abnormal("kill")
            return "end"
        if not self.intact():
            return "end"
        if self.budget_blocks():
            return "skip"                                  # the §3.8 pre-check: nothing journaled
        head = self.read(0)
        if not head["readable"]:
            self.mark_abnormal("journal_unreadable")
            return "end"
        seen_seq = head["events"][-1]["seq"] if head["events"] else 0
        # The open's one pre-entry head (§4.5 step 1): every classification of this open is against it.
        ctx = {"seen_seq": seen_seq, "market_id": market_id, "open_id": None, "submit": False, "retried": False,
               "in_cancel": False}
        signal_stop = self.env.signal_stop
        outcome = "end"
        try:
            # P108-R2-1: the gateway is kept in ctx before open() runs (the factory's confirm step), so its open
            # report and its kept pilot_place result reach finish_owed whatever line a signal lands on.
            with self.env.gateway_factory("pilot-place", credentials=self.credentials, stop=self.stop,
                                          confirm=lambda opened: ctx.__setitem__("gateway", opened)) as gateway:
                if self.stop.requested:                      # P108-1: the first statement inside the context
                    raise signal_stop("outer_stop")
                outcome = self.in_open(gateway, ctx, market_id, off)
        except signal_stop as exc:                           # before yield, or no submit: Yu's stop, orderly
            self.finish_owed(ctx)
            self.ledger_check_after_signal()
            self.signal_stopped(exc)
            outcome = "end"
        except _HandedOff as handed:                         # §3.7 already journaled what the exception owes
            if isinstance(handed.original, signal_stop):
                self.signal_stopped(handed.original)
            outcome = "end"
        except NotArmed as exc:                              # static, or under the lock (§4.5 step 2)
            self.not_armed(exc, ctx)
            outcome = "end"
        except LockBusy:
            self.stop_note("lock_busy", self.session["seq"], source_seq=self.session["seq"])
            outcome = "end"
        except Exception as exc:                             # the login family (§3.8), else "No submit" (§3.7)
            if not self.login_family(exc):
                self.no_submit_exception(ctx)
            outcome = "end"
        finally:
            self.stop.install()                              # §3.6 point 3: the outer handler first, unconditionally
            self.stop.take_pending(getattr(self.env.ctx, "signals", None))
        return outcome

    def in_open(self, gateway, ctx, market_id, off):
        report = gateway.open_report() or {}
        ctx["open_id"] = report.get("open_id") or gateway.open_id
        if not report.get("armed"):
            ctx["report"] = report
            self.deferring(True)                             # §3.6 (P108-C1): before the lookup that decides the note
            self.not_armed_at_open(report, ctx)
            self.stop.take_pending(getattr(self.env.ctx, "signals", None))
            return "end"
        try:
            result = self.place_once(gateway, ctx, market_id, off)
            if result is None:
                return "end"
            return self.after_place(gateway, ctx, market_id, result)
        except BaseException as exc:                         # §3.7: every placement action
            # Plain stores first, no call (a Python signal handler runs only at a call or function entry): the
            # exception is kept for finish_owed, and §3.7 step 0's deferral is on before the handler's own entry.
            ctx["exc"] = exc
            self.env.ctx.signals.deferring = True
            self.after_exception(exc, ctx, in_cancel=ctx["in_cancel"])
            raise _HandedOff(exc) from None

    def place_once(self, gateway, ctx, market_id, off):
        """Sleep inside the open to off - 300 s, then pilot_place; the first pilot_reserve_too_early is retried once,
        pre-append, in the same open (note §4:41; §13 ruling 1)."""
        t0 = off + self.contract.window_from_offset_s * NS
        while True:
            self.sleep_until(t0, outside_open=False)
            result = gateway.pilot_place(market_id, scheduler_off_ns=off, confirm_token=self.env.args.confirm_live)
            ctx["result"] = result                           # SIG-1: what the open owes is decided from this
            # §3.6 (P108-C1): deferral starts as soon as pilot_place returns, before any lookup that decides a write
            # this open owes, and lasts through that write (after_place); a signal meanwhile is only recorded.
            self.deferring(True)
            if result.get("pilot", {}).get("submit_seq") is not None:
                ctx["submit"] = True
            refusal = result.get("refusal") or {}
            if (result["pilot"]["latch"] is None and refusal.get("rule") == "pilot_reserve_too_early"
                    and not ctx["retried"]):
                ctx["result"] = None                         # still deferred: the first too-early owes nothing
                ctx["retried"] = True
                gateway.forget_pilot_result()
                self.deferring(False)                        # the retry stays interruptible
                pending = getattr(self.env.ctx, "signals", None)
                if pending is not None and pending.pending:
                    self.stop.take_pending(pending)
                    return None
                continue
            return result

    def after_place(self, gateway, ctx, market_id, result):
        """Step 5 (result mapping) and step 6 (the remainder cancel inside the same open)."""
        p, refusal = result["pilot"], result.get("refusal") or {}
        res = p["reservation_seq"]
        if p["latch"] is not None:
            if refusal.get("rule") == pilot.MANUAL_BET_OPEN:
                self.manual_bet = True
                self.w.line("placing needs the manual bet cancelled or settled, then RESUME LIVE, then a new PILOT ARM on "
                            "a later UTC day")
            if p["latch"] == "market":
                return self.remainder(gateway, ctx, market_id, p)
            self.end_code = refusal.get("rule") or f"latch_{p['latch']}"
            self.remainder(gateway, ctx, market_id, p)
            return "end"
        if refusal:
            rule = refusal.get("rule")
            scope = self.write_owed(ctx, result)
            if rule == pilot.MANUAL_BET_OPEN:
                self.manual_bet = True
            self.end_code = rule
            return "continue" if scope.action in ("skip_market",) else "end"
        if p["state"] == "FAILED":
            self.write_owed(ctx, result)
            self.end_code = self.failed_key(ctx, p)[0] or "failed"
            return "end"
        return self.remainder(gateway, ctx, market_id, p)

    def write_owed(self, ctx, result):
        """The write a latch-None result owes (§4.5 step 5), idempotently -> its scope: a refusal through the scope
        table (a second too-early in the open is review; the first is a retry and owes nothing), a review note unless
        the open is covered, a session stop unless journaled; a FAILED result its carried review stop unless
        journaled. Called by after_place and, after a signal, by finish_owed (SIG-1 d)."""
        p, refusal = result["pilot"], result.get("refusal") or {}
        res = p["reservation_seq"]
        if refusal:
            rule = refusal.get("rule")
            scope = pilot.scope_of(rule, (refusal.get("detail") or {}).get("reason"))
            if scope.action == "retry":
                if not ctx.get("retried"):
                    return scope
                scope = pilot.Scope("end", "review", rule)          # a second too-early in the same open
            if scope.durable == "review" and not self.covered(ctx):
                self.refusal(scope.code, open_id=ctx["open_id"], reservation_seq=res)
            elif scope.durable == "session":
                anchor = res if res is not None else self.session["seq"]
                self.stop_note(scope.code, anchor, source_seq=anchor)
            return scope
        if p["state"] == "FAILED":
            key = self.failed_key(ctx, p)
            if key[0] is not None and pilot.scope_of(key[0]).durable == "review" and key[1] is not None:
                stop_id = pilot.carried_review_stop_id(key[0], res, key[1])
                if not self.journaled(stop_id):
                    self.owe({"pilot": pilot.stop_payload(self.contract, stop_id, "review",
                                                          pilot.refusal_code(key[0]), source_seq=res)})
        return None

    def finish_owed(self, ctx):
        """§3.6: the orderly path first completes any executor write this open already owes (SIG-1 d): from a
        pilot_place result with latch None, or from an armed-False open report. Best effort; a failure marks the run
        abnormal, so it never ends clean."""
        if not ctx:
            return
        before = getattr(getattr(self.env.ctx, "signals", None), "deferring", None)
        self.deferring(True)
        try:
            result, report, gateway = ctx.get("result"), ctx.get("report"), ctx.get("gateway")
            if result is None and gateway is not None:         # P108-R2-1: returned, not yet saved by the caller
                result = gateway.last_pilot_result
            if report is None and result is None and gateway is not None:
                report = gateway.open_report()                 # open() returned it before the caller saved it
                if report is not None and report.get("armed"):
                    report = None
                if report is not None and ctx.get("open_id") is None:
                    ctx["open_id"] = report.get("open_id") or gateway.open_id
            exc = ctx.get("exc")
            if result is not None and result.get("pilot", {}).get("latch") is None:
                self.write_owed(ctx, result)
            elif report is not None:
                self.not_armed_at_open(report, ctx)
            elif (result is None and exc is not None and not ctx.get("exc_done")
                  and not isinstance(exc, self.env.signal_stop)):
                # A placement action raised and the signal landed before §3.7's handler began: the same
                # classification now (no submit: the login family, else executor_exception; a submit: its handoff).
                self.after_exception(exc, ctx, in_cancel=ctx.get("in_cancel", False))
        except Exception:
            self.mark_abnormal("owed_write_failed")
        finally:
            if before is not None:
                self.deferring(before)

    def failed_key(self, ctx, p):
        """(code, report seq) of the §3.1 FAILED key for this open's place report (FAILED_NOT_SENT and FAILED_REJECTED:
        exception.code; a refused report: place_report_failure)."""
        read = self.read(ctx["seen_seq"])
        for event in reversed(read["events"]):
            payload = event["payload"]
            if event["kind"] == "report" and payload.get("action") == "place":
                if payload.get("outcome") in ("FAILED_NOT_SENT", "FAILED_REJECTED"):
                    return (payload.get("exception") or {}).get("code"), event["seq"]
                if payload.get("outcome") == "reported":
                    return pilot.place_report_failure(payload.get("report")), event["seq"]
        return None, None

    def remainder(self, gateway, ctx, market_id, p):
        """Step 6: the remainder cancel inside the same open, before close(), with the KILL cases 1-4 (note:104-108)."""
        if p.get("submit_seq") is None:
            return "continue"
        res = p["reservation_seq"]
        item = {"ref": p.get("customer_order_ref"), "bet_id": p.get("bet_id"), "market_id": market_id,
                "selection_id": self.selection_of(p), "state": p.get("state")}
        if p.get("unknown") or p.get("state") in UNRESOLVED_STATES:
            self.handoff(item)
            self.stop_note("unknown_unresolved", res, source_seq=res)
            return "end"
        if p.get("state") == "EXECUTION_COMPLETE" and p.get("bet_id"):
            self.c_queue[item["ref"]] = item
            return "continue"
        if not (p.get("state") == "EXECUTABLE" and (p.get("size_remaining_p") or 0) > 0 and p.get("bet_id")):
            return "continue"
        case = self.kill_case(p["submit_seq"])
        if case == "unreadable":
            self.handoff(item)
            self.stop_note("remainder_left_executable", res, source_seq=res)
            return "end"
        if case == "handoff":
            self.handoff(item)
            self.stop_note("remainder_left_executable", res, source_seq=res)
            self.mark_abnormal("kill")
            return "end"
        ctx["in_cancel"] = True
        cancelled = gateway.cancel(market_id, [p["bet_id"]], source="pilot")
        ctx["in_cancel"] = False
        views = cancelled.get("orders") or []
        view = views[0] if views else None
        if cancelled.get("decision") != "accepted" or view is None or view["state"] in UNRESOLVED_STATES:
            self.handoff(item)
            self.stop_note("remainder_cancel_failed", res, source_seq=res)
            return "end"
        if view["state"] == "EXECUTABLE" and view["size_remaining"] > 0:
            self.handoff(item)
            self.stop_note("remainder_left_executable", res, source_seq=res)
            return "end"
        if view["state"] == "EXECUTION_COMPLETE" and view["bet_id"]:
            self.c_queue[item["ref"]] = dict(item, bet_id=view["bet_id"])
        if case == "kill":
            self.mark_abnormal("kill")
            return "end"
        return "continue"

    def selection_of(self, p):
        read = self.read(0)
        for event in read["events"]:
            inner = (event["payload"].get("pilot") or {}) if event["kind"] == "note" else {}
            if inner.get("type") == "decision" and inner.get("customer_order_ref") == p.get("customer_order_ref"):
                return inner.get("selection_id")
        return None

    def kill_case(self, submit_seq):
        """The first KILL case that applies after the submit (note:104-108): "cancel" (2: no kill event, a file-only
        KILL included), "kill" (3: a kill then kill_incomplete: cancel, then the kill path), "unreadable" (1) or
        "handoff" (4: a kill with no outcome, polled on fresh connections for up to 30 s of monotonic time from its
        first sight, at once when wall and monotonic time drift apart by more than 2 s). No Betfair read here."""
        read = self.read(submit_seq)
        if not read["readable"]:
            return "unreadable"
        if read["kill_seq"] is None:
            return "cancel"
        if read["kill_incomplete_seq"] is not None:
            return "kill"
        mono0, wall0 = self.clock.monotonic_ns(), self.clock.time_ns()
        while True:
            self.clock.sleep(SLICE_S)
            read = self.read(submit_seq)
            if not read["readable"]:
                return "unreadable"
            if read["kill_incomplete_seq"] is not None:
                return "kill"
            mono, wall = self.clock.monotonic_ns() - mono0, self.clock.time_ns() - wall0
            if abs(wall - mono) > CASE4_DRIFT_NS or mono >= CASE4_WAIT_NS:
                return "handoff"

    def handoff(self, item):
        self.handoff_due = True
        self.w.line(handoff_text(item))

    # -- failures out of the open (§4.5 steps 2-3) and exceptions after a submit (§3.7) -------------------------------

    def covered(self, ctx):
        """The open already has its review occurrence: a refusal note for this session carrying this open's id (or,
        with no id, any) since the open's pre-entry head. Flushed due-stop notes never count."""
        read = self.read(ctx["seen_seq"])
        for event in read["events"]:
            inner = (event["payload"].get("pilot") or {}) if event["kind"] == "note" else {}
            if (inner.get("type") == "refusal" and inner.get("session_id") == self.session["session_id"]
                    and (ctx["open_id"] is None or inner.get("open_id") == ctx["open_id"])):
                return True
        return False

    def not_armed(self, exc, ctx):
        """NotArmed raised out of the open. Covered (the gateway noted it under the lock): nothing. A static one: the
        A7 causes re-evaluated independently (the kill_file session stop for a KILL, deduped by its id; a review
        not_armed_before_lock for a clock regression, an unfoldable journal or an unexplained halted), and any other
        static failure is not_armed_before_lock too, at most one such note (Q1)."""
        failures = list((exc.detail or {}).get("failures") or [exc.rule])
        self.w.line("NotArmed: " + ", ".join(safe_code(code) for code in failures))
        if self.covered(ctx):
            return
        if exc.rule not in STATIC_CODES and "halted" not in failures:
            self.refusal(exc.rule, open_id=ctx["open_id"])
            return
        review, kill = any(code != "halted" for code in failures), False
        if "halted" in failures:
            read = self.read(0)
            try:
                state = fold_events(read["events"], mode="live", now_ns=self.now()) if read["readable"] else None
            except (JournalError, KeyError, TypeError, ValueError):
                state = None
            causes = a7_causes(self.paths, state if state is not None else {"halted": True})
            if causes["kill"] is not False:
                stop_id = pilot.session_stop_id("kill_file", self.session["seq"])
                kill = not any(e["kind"] == "note" and (e["payload"].get("pilot") or {}).get("stop_id") == stop_id
                               for e in read["events"])
            if causes["clock_regression"] or causes["unfoldable"]:
                review = True
            if not any(causes[key] for key in ("kill", "halt", "clock_regression", "unfoldable")) \
                    and causes["kill"] is not None:
                review = True                              # halted with no cause found: fail closed
        # Review before KILL (P108-C2): every cause is chosen before any append, and the review occurrence (its own
        # carrier) is journaled first, so a death between the two appends never leaves the session stop alone.
        if review:
            self.refusal("not_armed_before_lock", open_id=ctx["open_id"])
        if kill:
            self.stop_note("kill_file", self.session["seq"], source_seq=self.session["seq"])

    def not_armed_at_open(self, report, ctx):
        """Step 3: armed False. A17 gets its own review occurrence when no HALT holds and nothing else noted the open;
        any other failure, if the open is not covered, one note for the first review code or the first session stop."""
        failures = list(report.get("failures") or [])
        self.w.line("open not armed: " + ", ".join(safe_code(code) for code in failures))
        covered = self.covered(ctx)
        if "unresolved_or_foreign_orders" in failures:
            read = self.read(0)
            try:
                core = fold_events(read["events"], mode="live", now_ns=self.now()) if read["readable"] else None
            except (JournalError, KeyError, TypeError, ValueError):
                core = None
            if core is None:                     # SCOPE-1: the HALT state is unknown; an extra review stop is safe
                self.mark_abnormal("journal_unreadable")
            if (core is None or not core["halted"]) and not covered:
                self.refusal("unresolved_or_foreign_orders", open_id=ctx["open_id"])
            return
        if covered:
            return
        for code in failures:
            scope = pilot.scope_of(code)
            if scope.durable == "review":
                self.refusal(scope.code, open_id=ctx["open_id"])
                return
            if scope.durable == "session":
                self.stop_note(scope.code, self.session["seq"], source_seq=self.session["seq"])
                return

    def no_submit_exception(self, ctx):
        """Anything else out of an open that never submitted (§3.7 "No submit"): executor_exception, then abnormal."""
        self.refusal("executor_exception", open_id=ctx.get("open_id"))
        self.mark_abnormal("executor_exception")

    def after_exception(self, exc, ctx, *, in_cancel=False):
        """§3.7 inside the open: deferring on entry; the whole journal on a fresh connection; the handoff first when a
        submit of this open exists and its order is unresolved or EXECUTABLE with a remainder, or the journal cannot be
        read; then each append in its own try: executor_exception (not for Yu's stop or a login failure), the login
        family, and at most one session stop by the state read. No Betfair read, no cancel, no release; re-raised."""
        signal_stop = self.env.signal_stop
        self.deferring(True)
        ctx["exc_done"] = True                               # finish_owed never runs this handler a second time
        read = self.read(0)
        submit = None
        if read["readable"]:
            for event in read["events"]:
                if (event["seq"] > ctx["seen_seq"] and event["kind"] == "submit"
                        and event["payload"].get("action") == "place" and "pilot_record_sha256" in event["payload"]):
                    submit = event
                    break
        if read["readable"] and submit is None:              # "No submit": nothing of this open can be live
            ctx["submit"] = False
            if isinstance(exc, signal_stop):
                self.finish_owed(ctx)                        # SIG-1 d: the write the open owed, before the end
                self.ledger_check_after_signal()
                return
            if not self.login_family(exc):
                self.no_submit_exception(ctx)
            return
        ctx["submit"] = True
        # SIG-5: the cause by the same classification as step 3(a), set before any write so the end fails closed.
        self.mark_abnormal("signal_after_submit" if isinstance(exc, signal_stop)
                           else "login_budget" if isinstance(exc, LoginBudgetExceeded)
                           else "login_failed" if isinstance(exc, AuthError) else "executor_exception")
        order_state, item = None, None
        try:
            if not read["readable"]:
                raise ValueError("unreadable")
            projection = recovery_projection(read["events"], contract=self.contract, now_ns=self.now())
            refs = {m.get("customer_order_ref") for m in submit["payload"].get("transitions") or ()}
            order = next((o for ref, o in projection["core"]["orders"].items() if ref in refs), None)
            rec = next((r for r in projection["state"].place_records if r.ref in refs), None)
            order_state = order["state"] if order else None
            item = {"ref": order["customer_order_ref"] if order else None, "bet_id": order["bet_id"] if order else None,
                    "market_id": ctx["market_id"], "selection_id": rec.selection_id if rec else None}
            if order_state in UNRESOLVED_STATES or (order_state == "EXECUTABLE" and (order["size_remaining_p"] or 0) > 0):
                self.handoff(item)
        except Exception:
            order_state = None
            for line in case_one_handoff(read["events"], seen_seq=ctx["seen_seq"], market_id=ctx["market_id"]):
                self.handoff_due = True
                self.w.line(line)
        reservation = None
        if read["readable"]:
            for event in read["events"]:
                inner = (event["payload"].get("pilot") or {}) if event["kind"] == "note" else {}
                if inner.get("type") == "reservation" and inner.get("open_id") == ctx["open_id"]:
                    reservation = event["seq"]
        if not isinstance(exc, signal_stop):
            try:
                if not self.login_family(exc, reservation_seq=reservation):
                    self.refusal("executor_exception", open_id=ctx["open_id"], reservation_seq=reservation)
            except Exception:
                pass
        anchor = reservation if reservation is not None else self.session["seq"]
        try:
            if order_state is None or order_state in UNRESOLVED_STATES:
                self.stop_note("unknown_unresolved", anchor, source_seq=anchor)
            elif in_cancel:
                self.stop_note("remainder_cancel_failed", anchor, source_seq=anchor)
            elif order_state == "EXECUTABLE" and item is not None:
                self.stop_note("remainder_left_executable", anchor, source_seq=anchor)
        except Exception:
            pass

    # -- settle points (§4.6) ------------------------------------------------------------------------------------

    def settle_points(self, open_at):
        """Offer each eligible market once in this gap: a pilot order EXECUTION_COMPLETE in a market a capture read
        showed CLOSED, with no pilot order EXECUTABLE with a remainder or unresolved. -> None or "end"."""
        projection = self.fold()
        if not self.settle_orders_clear(projection):
            return None
        pilot_refs = {rec.ref for rec in projection["state"].place_records}
        orders = projection["core"]["orders"]
        closed = closed_markets(self.read(0)["events"])
        markets = sorted({orders[ref]["market_id"] for ref in pilot_refs if ref in orders
                          and orders[ref]["state"] == "EXECUTION_COMPLETE" and orders[ref]["market_id"] in closed})
        self.offered = set()
        for market_id in markets:
            if market_id in self.offered:
                continue
            if not self.settle_allowed(open_at):
                return None
            self.offered.add(market_id)
            if self.settle_one(market_id, open_at) == "end":
                return "end"
        return None

    @staticmethod
    def settle_orders_clear(projection):
        """The settle point's order condition (§4.6): a projection, and no pilot order unresolved or EXECUTABLE with
        a remainder."""
        if projection is None:
            return False
        orders = projection["core"]["orders"]
        return not any(ref in orders and (orders[ref]["state"] in UNRESOLVED_STATES or (
            orders[ref]["state"] == "EXECUTABLE" and (orders[ref]["size_remaining_p"] or 0) > 0))
            for ref in {rec.ref for rec in projection["state"].place_records})

    def settle_allowed(self, open_at):
        """The settle point's run conditions, in placement()'s order: the flag, HUP, an abnormal cause, the 3-min lead,
        the kill (§3.9), admission_intact (§3.10), then the login-budget pre-check (§3.8, SCHED-1)."""
        if self.stop.requested or self.stop.hup or self.abnormal:
            return False
        if open_at - self.now() <= SETTLE_LEAD_NS + PROMPT_TIMEOUT_NS + SETTLE_OVERHEAD_NS:
            return False
        if self.kill_seen():
            self.mark_abnormal("kill")
            return False
        if not self.intact():
            return False
        return not self.budget_blocks()

    @staticmethod
    def settle_skip(rule):
        """A settle refusal that only skips the offer (the scope table's skip_offer row: settle_market_*, the closed
        point, a missing, mismatched or timed-out typed line; SCOPE-3)."""
        return pilot.scope_of(rule).action == "skip_offer"

    def settle_one(self, market_id, open_at):
        signal_stop = self.env.signal_stop

        def confirm(gateway):
            gateway.preconfirm("settle-market", market_id=market_id)
            # Q13 after the typed line, under deferral (SIG-1 c): every run condition and the order condition again
            # (SCHED-2), then any signal that arrived; a stop raises before the open can log in.
            signals = getattr(self.env.ctx, "signals", None)
            before = signals.deferring if signals is not None else None
            if signals is not None:
                signals.deferring = True
            try:
                allowed = self.settle_allowed(open_at) and self.settle_orders_clear(self.fold())
            finally:
                if signals is not None:
                    signals.deferring = before
                    self.stop.take_pending(signals)
            if self.stop.requested:
                raise signal_stop("outer_stop")
            if not allowed:
                raise OrderRefused("settle_point_closed")

        ctx = {"seen_seq": 0, "market_id": market_id, "open_id": None}
        head = self.read(0)
        ctx["seen_seq"] = head["events"][-1]["seq"] if head["readable"] and head["events"] else 0
        try:
            with self.env.gateway_factory("settle-market", credentials=self.credentials, stop=self.stop,
                                          in_session_settle=True, confirm=confirm,
                                          read_guard=self.read_guard) as gateway:
                if self.stop.requested:
                    raise signal_stop("outer_stop")
                try:
                    gateway.settle_market(market_id, confirm_token=self.env.args.confirm_live)
                except OrderRefused as exc:
                    if self.settle_skip(exc.rule):
                        self.w.line(f"settle {market_id}: {exc.rule}; the order stays at worst case; settle later")
                        return None
                    raise
        except signal_stop as exc:
            self.finish_owed(ctx)
            self.ledger_check_after_signal()
            self.signal_stopped(exc)
            return "end"
        except NotArmed as exc:
            self.not_armed(exc, ctx)
            self.w.line("the order stays at worst case; settle later")
            return "end"
        except LockBusy:
            self.stop_note("lock_busy", self.session["seq"], source_seq=self.session["seq"])
            return "end"
        except OrderRefused as exc:
            if self.settle_skip(exc.rule):
                self.w.line(f"settle {market_id}: {exc.rule}; the order stays at worst case; settle later")
                return None
            if not self.login_family(exc):
                self.no_submit_exception(ctx)
            return "end"
        except Exception as exc:
            if not self.login_family(exc):
                self.no_submit_exception(ctx)
            return "end"
        finally:
            self.stop.install()
            self.stop.take_pending(getattr(self.env.ctx, "signals", None))
        return None

    # -- in-session capture (§4.7) --------------------------------------------------------------------------------

    def capture_bound_ns(self):
        """SCHED-4: how long one capture read can take with its login and the note's lock wait; a batch never starts,
        and never starts another read, within this of the next planned open."""
        http = self.config["http"]
        return int(http["timeout_s"] * NS) + CAPTURE_LOGIN_NS + int((http["order_deadline_s"] + 5) * NS)

    def final_capture(self):
        """SCHED-3: at an orderly end with the session still armed (the day limit, no plannable market), one C-only
        batch for the orders whose C read is still queued, under the same checks as any batch."""
        if self.c_queue and not (self.stop.requested or self.abnormal):
            self.capture_batch(kinds=("c",))

    def capture_batch(self, *, open_at=None, kinds=None):
        """One capture batch between opens (or the final C batch) -> None or "end". It reads on the scheduler's read
        session (SCHED-5: no new login per batch) and stops before the next open's bound (SCHED-4)."""
        projection = self.fold()
        if projection is None:
            return None
        entries = capture_entries(projection, queue=self.c_queue, session_id=self.session["session_id"],
                                  now_ns=self.now())
        if kinds is not None:
            entries = [entry for entry in entries if entry[0] in kinds]
        if not entries:
            return None
        deadline = None if open_at is None else open_at - self.capture_bound_ns()
        if deadline is not None and self.now() > deadline:
            return None                                    # too close to the open: the entries stay queued

        def client():
            return self.read_client()

        def before():
            found = self.before_read()
            return None if found is None else ("skipped_kill" if found == "kill" else found)

        try:
            outcome = capture(self.env, entries, read_client=client, journal=self.journal, contract=self.contract,
                              before_read=before, budget_blocks=self.budget_blocks, deadline_ns=deadline)
        except LockBusy:
            self.stop_note("lock_busy", self.session["seq"], source_seq=self.session["seq"])
            return "end"
        except Exception as exc:
            if not self.login_family(exc):
                self.refusal("executor_exception")
                self.mark_abnormal("executor_exception")
            return "end"
        for ref in outcome["completed"]:
            self.c_queue.pop(ref, None)
        if outcome["stopped"] in ("skipped_kill", "kill"):
            self.mark_abnormal("kill")
            return "end"
        if outcome["stopped"] in ("stop", "admission"):
            return "end"
        after = self.fold()
        if after is not None and after["state"].due_stops:
            try:
                flush_due(self.journal, after["state"], paths=self.paths, config=self.config, clock=self.clock)
            except LockBusy:
                self.stop_note("lock_busy", self.session["seq"], source_seq=self.session["seq"])
                return "end"
            return "end"                                  # an active stop ends placing
        return None

    # -- the end path (§4.8) --------------------------------------------------------------------------------------

    def end(self):
        """The one end path: close every session, fold, flush, the session_end stop while still armed, the summary, the
        clean decision, then release (clean) or the "before recording-release" block (abnormal)."""
        paths, config, w = self.paths, self.config, self.w
        step2 = True
        for session, _ in self.read_sessions:              # step 2: the scheduler's and every capture read session
            try:
                session.close()
            except Exception:
                step2 = False
        projection = self.fold()                           # step 3
        appended_end = False
        if projection is not None:
            try:
                if projection["state"].due_stops:
                    flush_due(self.journal, projection["state"], paths=paths, config=config, clock=self.clock)
                    projection = self.fold() or projection
                if pilot.session_armed(projection["state"], now_ns=self.now()):
                    self.owe(session_stop(self.contract, "session_end", self.session["seq"],
                                          source_seq=self.session["seq"]))
                    appended_end = True
                projection = self.fold() or projection
            except Exception as exc:                       # SIG-3: any failure here only ends abnormally
                self.mark_abnormal("end_append_failed")
                w.line(f"end: {safe_code(type(exc).__name__)}")
        self.summary(projection)                           # step 4
        check = pilot_stop_check(self.paths, contract=self.contract, session_id=self.session["session_id"],
                                 clock=self.clock)
        problems = check(paths) if projection is not None else ["pilot_stop_check_fold_failed"]
        clean = (not self.handoff_due and self.abnormal is None and step2 and not problems and projection is not None)
        if clean:                                          # step 6
            deleted = delete_armed_session_if(paths, self.armed_sha)
            if deleted not in ("deleted", "missing"):      # OWN-5: the file goes before release, or no release
                w.line(f"armed-session file {deleted}; left at {armed_session_path(paths)}")
                self.mark_abnormal("armed_file_not_deleted")
                clean = False
        if clean:
            if projection is not None and capture_entries(projection):
                w.line(self.pending_line(capture_entries(projection)))
            result = admission.release_own(self.ticket, paths, config=config, clock=self.clock, extra_checks=[check])
            w.lines(release_lines(result, self.env.paused_flag))
            if self.manual_bet:
                w.line("placing needs the manual bet cancelled or settled, then RESUME LIVE, then a new PILOT ARM on a "
                       "later UTC day")
            return 0 if result["released"] else 5
        after = self.fold()                                # step 7
        kept = after is None or pilot.session_armed(after["state"], now_ns=self.now())
        if not kept:
            deleted = delete_armed_session_if(paths, self.armed_sha)
            kept = deleted not in ("deleted", "missing")
        handoffs = after["handoffs"] if after is not None else []
        if after is None:
            w.lines(case_one_handoff([], seen_seq=0, market_id=None))
        pending = capture_entries(after) if after is not None else []       # C, the CLOSED book and L (§3.9)
        if pending:
            w.line(self.pending_line(pending))
        w.lines(before_release_lines(paths, handoffs=handoffs, generation=self.ticket.generation,
                                     armed_file_kept=kept, flag=self.env.paused_flag, login_reset=self.login_failed,
                                     clock=self.clock))
        if self.manual_bet:
            w.line("placing needs the manual bet cancelled or settled, then RESUME LIVE, then a new PILOT ARM on a later "
                   "UTC day")
        return 5

    def pending_line(self, pending):
        """The pending-captures line: how many reads of each kind are still owed, and when to run pilot-capture. Counts
        only, never a bet id or market id (note §12:185): pilot-capture rebuilds its entries from the journal, and the
        handoff lines carry the ids a person needs (note §8)."""
        counts = {kind: sum(1 for k, _ in pending if k == kind) for kind in ("c", "book", "l")}
        return (f"pending captures: c {counts['c']}, book {counts['book']}, l {counts['l']}; "
                + (KILL_CAPTURE_LINE if self.abnormal == "kill" else PENDING_CAPTURE_LINE))

    def summary(self, projection):
        """Counts and codes only (note:182): this session's reservations, records, SKIPs by code, sends, refusals by
        code, fills, cancels, UNKNOWNs, settles, stops and pending C reads; PT4 records apart."""
        out = {"status": "pilot_session_ended", "session_id": self.session["session_id"], "end_code": self.end_code,
               "abnormal": self.abnormal, "stop_requested": self.stop.requested, "signal": self.stop.name}
        if projection is not None:
            state, core = projection["state"], projection["core"]
            mine = [r for r in state.place_records if r.session_id == self.session["session_id"]]
            reservations = [m for m, r in state.reservations.items() if r["session_id"] == self.session["session_id"]]
            skips = {}
            for skip in state.skip_records:
                if skip["market_id"] in reservations:
                    skips[skip["code"]] = skips.get(skip["code"], 0) + 1
            refusals = {}
            for stop in state.stops:
                if stop["seq"] > self.session["seq"] and stop["scope"] == "review":
                    refusals[stop["reason"]] = refusals.get(stop["reason"], 0) + 1
            states = [core["orders"].get(r.ref, {}).get("state") for r in mine]
            out.update(reservations=len(reservations), place_records=len(mine),
                       pt4_records=sum(1 for r in mine if r.pt4), skips=skips,
                       sends=sum(1 for r in mine if r.submit_seq is not None), refusals=refusals,
                       fills=sum(1 for r in mine if (core["orders"].get(r.ref, {}).get("size_matched_p") or 0) > 0),
                       cancels=sum(1 for r in mine if (core["orders"].get(r.ref, {}).get("size_cancelled_p") or 0) > 0),
                       unknowns=sum(1 for s in states if s in UNRESOLVED_STATES),
                       settles=sum(1 for s in states if s == "SETTLED"),
                       stops=sum(1 for s in state.stops if s["seq"] > self.session["seq"]),
                       sessions=len(state.sessions), pending_c_reads=len(projection["pending_capture"]))
        self.w.json(out)
        if self.ledger_unresolved_login:
            self.w.line(LEDGER_LINE)


class _HandedOff(Exception):
    """Raised out of an open once §3.7's handler has journaled (and printed) what the original exception owes."""

    def __init__(self, original):
        super().__init__(type(original).__name__)
        self.original = original


def closed_markets(events):
    """Market ids a capture_book note showed CLOSED."""
    found = set()
    for event in events or ():
        inner = (event["payload"].get("pilot") or {}) if event["kind"] == "note" else {}
        if inner.get("type") == "capture_book" and inner.get("status") == "CLOSED" and event.get("market_id"):
            found.add(event["market_id"])
    return found


def pilot_stop_check(paths, *, contract, session_id, clock):
    """The release extra check (§4.8, Q12): -> check(paths) giving [] only when a fresh read-only fold succeeds, has no
    due stop, its latest session is this one and is ended by exactly its session_end stop (a) or reached UNTIL with no
    terminating stop (b), no other stop note sits above its session_start, and no review stop is active."""
    def check(checked_paths):
        read = pilot_events_after(checked_paths.journal_path("live"), 0)
        if not read["readable"]:
            return ["pilot_stop_check_unreadable"]
        now = clock.time_ns()
        try:
            state = pilot.fold(read["events"], contract=contract, now_ns=now)
        except (pilot.PilotError, KeyError, TypeError, ValueError):
            return ["pilot_stop_check_fold_failed"]
        problems = []
        if state.due_stops:
            problems.append("pilot_due_stops")
        session = state.sessions[-1] if state.sessions else None
        if session is None or session["session_id"] != session_id:
            problems.append("pilot_session_not_this")
        else:
            end_id = pilot.session_stop_id("session_end", session["seq"])
            end_seqs = [s["seq"] for s in state.stops if s["stop_id"] == end_id]
            ended = (session["ended_reason"] == "session_end" and end_seqs and session["ended_seq"] == end_seqs[0])
            reached = (session["ended_reason"] is None and session["ended_seq"] is None
                       and not pilot.session_armed(state, now_ns=now))
            if not (ended or reached):
                problems.append("pilot_session_not_ended")
            if any(s["seq"] > session["seq"] and s["stop_id"] != end_id for s in state.stops):
                problems.append("pilot_stop_in_session")
        if any(s["scope"] == "review" for s in state.active_stops):
            problems.append("pilot_review_stop_active")
        return problems
    return check


# --- capture (§4.7, §4.12) ------------------------------------------------------------------------------------


def capture_entries(projection, *, queue=None, session_id=None, now_ns=None):
    """What a batch reads, C first: each pilot bet with a bet id and no C read showing EXECUTION_COMPLETE (the
    in-session queue when given, else rebuilt from the journal), then the CLOSED book of each market with no CLOSED
    capture yet, then L for each bet whose market a capture showed CLOSED and that has no settled L row. In a session
    (session_id, now_ns) only that session's markets whose off has passed are read for the book and L.

    The book is read for every PLACE record's market, sent or not (contract decision-price series; P1-10 S1b). A bet's
    market is read as before. A record with no bet id gets a book entry (bet_id None) once its off has passed, until no
    read can change it. For an unsent record that is off + the BSP cutoff: its BSP is then decided "none", and
    definitely_unsent outranks void and removal. A sent record with no bet id is read until its deadline, because a
    CLOSED read up to then still decides void or removal (Astra S1b-1); after it the record is final (Codex review,
    PR #40)."""
    state, core = projection["state"], projection["core"]
    # Any CLOSED capture_book, not only one the BSP rule used: a CLOSED read after off + 24 h decides the BSP as
    # "none" without setting closed_read, and its bets still need L (P1-10 S1).
    closed = projection["closed_markets"]
    now = state.now_ns if now_ns is None else now_ns
    pending = {item["ref"]: item for item in projection["pending_capture"]}
    if queue is not None:
        pending = {ref: dict(item, **pending.get(ref, {})) for ref, item in queue.items() if ref in pending}
    entries = [("c", item) for item in pending.values()]
    books, ls, booked = [], [], set()
    for rec in state.place_records:
        if session_id is not None and (rec.session_id != session_id or now_ns is None or now_ns <= rec.off_ns):
            continue
        order = core["orders"].get(rec.ref)
        bet_id = None if order is None or rec.unsent else order["bet_id"]
        item = {"ref": rec.ref, "bet_id": bet_id, "market_id": rec.market_id,
                "selection_id": rec.selection_id, "off_ns": rec.off_ns}
        if bet_id is None:
            last_ns = rec.off_ns + state.contract.bsp_cutoff_s * NS if rec.unsent else rec.deadline_ns
            if rec.market_id not in closed and rec.market_id not in booked and rec.off_ns < now <= last_ns:
                books.append(("book", item))
                booked.add(rec.market_id)
        elif rec.market_id not in closed and rec.market_id not in booked:
            books.append(("book", item))
            booked.add(rec.market_id)
        elif rec.market_id in closed and not any(r["row"].get("betStatus") == "SETTLED" for r in rec.l_rows):
            ls.append(("l", item))
    return entries + books + ls


def capture(env, entries, *, read_client, journal, contract, before_read, budget_blocks, deadline_ns=None):
    """One capture batch over entries, C before L and the CLOSED book (F77): before each read the caller's checks, then
    the budget pre-check (a deferred read stops the batch: login_budget_deferred); one raw read; the exact bytes to
    pilot_dir/raw/<sha256> (0600, local); the pure extractor; a note only when ref, market and selection match the record,
    appended in a short .gateway.lock hold with no login. -> {"written", "completed", "stopped", "codes"}."""
    paths, config, clock = env.paths, env.config, env.clock
    deadline_s = config["http"]["timeout_s"]
    out = {"written": 0, "completed": [], "stopped": None, "codes": {}}

    def code(name):
        out["codes"][name] = out["codes"].get(name, 0) + 1

    for kind, item in entries:
        stopped = before_read()
        if stopped is not None:
            out["stopped"] = stopped
            code(stopped)
            break
        if budget_blocks():
            out["stopped"] = "login_budget_deferred"
            code("login_budget_deferred")
            break
        if deadline_ns is not None and clock.time_ns() > deadline_ns:     # SCHED-4: the next open comes first
            out["stopped"] = "capture_deferred_open"
            code("capture_deferred_open")
            break
        try:
            client = read_client()
            if kind == "c":
                read = client.raw_current_order(item["bet_id"], deadline_s=deadline_s)
            elif kind == "book":
                read = client.raw_closed_book(item["market_id"], deadline_s=deadline_s)
            else:
                settled_from = utc_from_ns(max(0, (item.get("off_ns") or clock.time_ns()) - 86400 * NS))
                read = client.raw_cleared_order(item["bet_id"], settled_from_utc=settled_from, deadline_s=deadline_s)
        except (LoginStopped, ReadStopped) as exc:       # the read session's guard refused a login or a read
            reason = getattr(exc, "reason", None) or ("kill" if "kill" in str(exc) else "stop")
            out["stopped"] = "skipped_kill" if reason in ("kill", "skipped_kill") else reason
            code(out["stopped"])
            break
        body = read.get("body")
        if read.get("error") is not None or not isinstance(body, bytes) or read.get("status") != 200:
            out["stopped"] = "read_failed"
            code("read_failed")
            break
        digest = store_raw(paths, body)
        try:
            if kind == "c":
                row = pilot.extract_current_row(body, bet_id=item["bet_id"])
                record = ("capture_c", {"bet_id": item["bet_id"], "raw_sha256": digest, "row": row},
                          {"customer_order_ref": item["ref"]})
            elif kind == "book":
                book = pilot.extract_closed_book(body, market_id=item["market_id"])
                record = ("capture_book", {"raw_sha256": digest, "status": book["status"],
                                           "bsp_reconciled": book["bsp_reconciled"], "runners": book["runners"]},
                          {"market_id": item["market_id"]})
            else:
                row = pilot.extract_cleared_row(body, bet_id=item["bet_id"])
                record = ("capture_l", {"bet_id": item["bet_id"], "raw_sha256": digest, "row": row},
                          {"customer_order_ref": item["ref"]})
        except pilot.PilotError as exc:
            if kind == "c" and exc.code == "evidence_not_single":
                out["completed"].append(item["ref"])        # the bet is no longer listed: the entry ends
                code("c_bet_not_listed")
            else:
                code(f"{kind}_{exc.code}")
            continue
        if kind in ("c", "l"):
            row = record[1]["row"]
            if (row.get("customerOrderRef") != item["ref"] or row.get("marketId") != item["market_id"]
                    or row.get("selectionId") != item["selection_id"]):
                code(f"{kind}_row_differs")
                continue
        elif str(item["selection_id"]) not in record[1]["runners"]:
            code("book_runner_missing")
            continue
        with file_lock(paths.gateway_lock, timeout_s=config["http"]["order_deadline_s"] + 5, clock=clock):
            append_pilot(journal, pilot.pilot_note(record[0], contract.sha256, **record[1]), **record[2])
        out["written"] += 1
        if kind == "c" and record[1]["row"].get("status") == "EXECUTION_COMPLETE":
            out["completed"].append(item["ref"])
    return out


def store_raw(paths, body):
    """The raw capture bytes at pilot_dir/raw/<sha256>, 0600, local only (note :168, :187) -> the sha256."""
    paths.ensure()
    directory = paths.pilot_dir / "raw"
    pilot_gate.private_dir(directory, paths.profile_dir)
    digest = bytes_sha256(body)
    target = directory / digest
    if not target.exists():
        atomic_write_bytes(target, body)
    return digest


# --- pilot-end-session (§4.9) ---------------------------------------------------------------------------------


def end_session(env):
    """pilot-end-session: restrict-only crash recovery (C6, C12, C24; Q5). No typed line, token, login, Keychain
    credential read, Betfair read, place or release. The owner lock is taken LOCK_NB and held to the end."""
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    paths.ensure()
    try:
        lock = file_lock(paths.live_owner_lock, timeout_s=0, clock=clock)
        lock.__enter__()
    except LockBusy:
        raise Refused("owner_live", owner_live_lines(paths)) from None       # design v9 §3.4.6
    except OSError:
        raise Refused("pilot_session_state_unknown") from None
    journal = None
    try:
        try:
            contract, _ = read_contract(env)
        except Refused:
            contract = None
        read = pilot_events_after(paths.journal_path("live"), 0)
        try:
            if contract is None or not read["readable"]:
                raise ValueError("unreadable")
            projection = recovery_projection(read["events"], contract=contract, now_ns=clock.time_ns())
        except Exception:
            w.lines(case_one_handoff(read["events"], seen_seq=0, market_id=None))
            return 5
        w.lines(handoff_text(h) for h in projection["handoffs"])
        journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
        flushed = flush_due(journal, projection["state"], paths=paths, config=config, clock=clock)
        read = pilot_events_after(paths.journal_path("live"), 0)
        projection = recovery_projection(read["events"], contract=contract, now_ns=clock.time_ns())
        stopped = None
        if projection["armed"]:
            session = projection["state"].sessions[-1]
            anchor = session["seq"]
            source = projection["stop_source_seq"] or anchor
            append_pilot(journal, session_stop(contract, projection["stop_reason"], anchor, source_seq=source))
            stopped = projection["stop_reason"]
        deleted = delete_armed_session_if(paths)
        w.json({"status": "pilot_session_ended_by_recovery", "flushed_stops": len(flushed), "session_stop": stopped,
                "armed_session_file": deleted, "handoffs": len(projection["handoffs"])})
        w.lines(before_release_lines(paths, handoffs=projection["handoffs"], flag=env.paused_flag, clock=clock))
        return 5 if projection["handoffs"] else 0
    finally:
        if journal is not None:
            journal.close()
        lock.__exit__(None, None, None)


# --- pilot-ack and pilot-gap-resolved (§4.10, §4.11) ----------------------------------------------------------


def _typed_note_view(env, journal):
    """Pre-prompt: flush due stops under a brief lock, then a read-only fold -> (contract, state, head, events)."""
    contract, _ = read_contract(env)
    read = pilot_events_after(env.paths.journal_path("live"), 0)
    if not read["readable"]:
        raise Refused("pilot_journal_unreadable")
    try:
        state = pilot.fold(read["events"], contract=contract, now_ns=env.clock.time_ns())
    except pilot.PilotError:
        raise Refused("pilot_fold_failed") from None
    if state.due_stops:
        flush_due(journal, state, paths=env.paths, config=env.config, clock=env.clock)
        read = pilot_events_after(env.paths.journal_path("live"), 0)
        state = pilot.fold(read["events"], contract=contract, now_ns=env.clock.time_ns())
    return contract, state, head_of(read["events"]), read["events"]


def journal_reconciled(journal, core):
    """Q9: the chain verifies, no row is unresolved and no HALT is active (an operational check, not an exchange one)."""
    return (journal.verify()["status"] == "ok" and not core["halted"]
            and not any(o["state"] in UNRESOLVED_STATES for o in core["orders"].values()))


def ack(env):
    """pilot-ack: PILOT ACK <contract12> <stop-id> (note:101; Q8, Q9, C18). The cause copied before the prompt for a
    non-pause stop; under the lock the due stops flushed on the held lock (anything appended refuses), ack_check on a
    fresh head, then the ack note compare-and-appended on the head Yu saw."""
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    gates(env, "pilot-ack")
    stop_id = env.args.stop_id
    journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
    try:
        contract, state, seen, events = _typed_note_view(env, journal)
        stop = next((s for s in state.stops if s["stop_id"] == stop_id), None)
        pause = re.fullmatch(r"pause-[1-9][0-9]{0,9}", stop_id or "") is not None
        core = fold_events(events, mode="live", now_ns=clock.time_ns())
        code = pilot.ack_check(state, stop_id, journal_reconciled=journal_reconciled(journal, core),
                               unresolved_order=any(o["state"] in UNRESOLVED_STATES for o in core["orders"].values()),
                               cause_written=env.args.cause_file is not None)
        if code is not None:                     # read only, before the cause copy: a doomed ACK leaves no file
            raise Refused(code)
        cause_written = False
        if env.args.cause_file is not None:
            digest, target = _cause_copy(env, env.args.cause_file, "ack", stop_id)
            cause_written = True
            w.line(f"cause copied to {target}, sha256 {digest}")
        banner = (f"MODE=LIVE: PILOT ACK of stop {stop_id} (scope {stop['scope'] if stop else 'unknown'}, reason "
                  f"{stop['reason'] if stop else 'unknown'}, source seq {stop['source_seq'] if stop else 'unknown'}). "
                  f"It does not reopen today's session; the next placing needs a new ARM on a later UTC day."
                  + ("" if cause_written or pause else " No cause was given."))
        env.confirmer.confirm_line(f"PILOT ACK {contract.sha256[:12]} {stop_id}", banner=banner)
        with file_lock(paths.gateway_lock, timeout_s=config["http"]["order_deadline_s"] + 5, clock=clock):
            read = pilot_events_after(paths.journal_path("live"), 0)
            state = pilot.fold(read["events"], contract=contract, now_ns=clock.time_ns())
            appended = flush_due_held(journal, state)
            if appended:
                raise Refused("pilot_ack_due_stops", [f"journaled {stop}" for stop in appended]
                              + ["retype; the banner now shows the journaled stops"])
            core = fold_events(read["events"], mode="live", now_ns=clock.time_ns())
            code = pilot.ack_check(state, stop_id, journal_reconciled=journal_reconciled(journal, core),
                                   unresolved_order=any(o["state"] in UNRESOLVED_STATES
                                                        for o in core["orders"].values()),
                                   cause_written=cause_written)
            if code is not None:
                raise Refused(code)
            try:
                event = append_pilot(journal, pilot.pilot_note("ack", contract.sha256, stop_id=stop_id,
                                                               cause_written=cause_written), expect_head=seen)
            except JournalError as exc:
                if "journal_head_moved" in str(exc):
                    raise Refused("journal_changed_during_ack") from None
                raise
        _publish(env, journal)
    finally:
        journal.close()
    w.json({"status": "acknowledged", "stop_id": stop_id, "cause_written": cause_written, "note_seq": event["seq"]})
    return 0


def gap_resolved(env):
    """pilot-gap-resolved: PILOT GAP RESOLVED <contract12> <stop-id> (Q10). Binds one ledger-gap occurrence and its
    decision seq; never acknowledges (PILOT ACK still follows)."""
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    gates(env, "pilot-gap-resolved")
    stop_id = env.args.stop_id
    journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
    try:
        contract, state, seen, _ = _typed_note_view(env, journal)
        code = pilot.gap_resolve_check(state, stop_id)
        if code is not None:
            raise Refused(code)
        digest, target = _cause_copy(env, env.args.cause_file, "gap", stop_id)
        w.line(f"cause copied to {target}, sha256 {digest}")
        banner = (f"MODE=LIVE: PILOT GAP RESOLVED records that ledger gap {stop_id} is resolved, with its cause written. "
                  f"It does not acknowledge the stop: PILOT ACK still follows.")
        env.confirmer.confirm_line(f"PILOT GAP RESOLVED {contract.sha256[:12]} {stop_id}", banner=banner)
        with file_lock(paths.gateway_lock, timeout_s=config["http"]["order_deadline_s"] + 5, clock=clock):
            read = pilot_events_after(paths.journal_path("live"), 0)
            state = pilot.fold(read["events"], contract=contract, now_ns=clock.time_ns())
            if flush_due_held(journal, state):
                raise Refused("pilot_gap_due_stops", ["retype; the banner now shows the journaled stops"])
            code = pilot.gap_resolve_check(state, stop_id)
            if code is not None:
                raise Refused(code)
            try:
                event = append_pilot(journal, pilot.pilot_note(
                    "gap_resolved", contract.sha256, stop_id=stop_id, decision_seq=pilot.gap_decision_seq(state, stop_id),
                    cause_written=True), expect_head=seen)
            except JournalError as exc:
                if "journal_head_moved" in str(exc):
                    raise Refused("journal_changed_during_gap_resolved") from None
                raise
        _publish(env, journal)
    finally:
        journal.close()
    w.json({"status": "gap_resolved", "stop_id": stop_id, "note_seq": event["seq"]})
    return 0


# --- pilot-capture (§4.12) ------------------------------------------------------------------------------------


def capture_command(env):
    """pilot-capture: Yu's capture after the session, never scheduled. require_present, then the gates, then the three
    ownership phases before any credential read or login: the owner lock; the before-admit checks (an armed session,
    a recent kill), read only; the admission (fresh, or an explicit continuation with cleanup False)."""
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    if paths.profile != RECORDER_PROFILE:
        raise Refused("pilot_profile_not_recorder_profile")
    env.confirmer.require_present()
    gates(env, "pilot-capture")
    contract, _ = read_contract(env)
    start = {}

    def before_admit(checked_paths):
        """The armed refusal (fold only, no lock probe) and the kill start check (rule (a)); read only."""
        read = pilot_events_after(checked_paths.journal_path("live"), 0)
        now = clock.time_ns()
        if not read["readable"]:
            return ["pilot_session_armed"]
        try:
            state = pilot.fold(read["events"], contract=contract, now_ns=now)
        except (pilot.PilotError, KeyError, TypeError, ValueError):
            return ["pilot_session_armed"]
        if pilot.session_armed(state, now_ns=now):
            return ["pilot_session_armed"]
        start["head"] = read["events"][-1]["seq"] if read["events"] else 0
        start["kill"] = _kill_identity(checked_paths)
        if start["kill"] == "unknown":
            return ["kill_in_progress"]
        if start["kill"] is not None and now - start["kill"][1] < KILL_RECENT_NS:
            return ["kill_in_progress"]
        for event in read["events"]:
            if event["kind"] == "kill":
                later = any(e["seq"] > event["seq"] and e["kind"] == "halt"
                            and e["payload"].get("reason") == "kill_incomplete" for e in read["events"])
                try:
                    at = pilot.parse_utc_ns(event["recorded_at"])
                except pilot.PilotError:
                    return ["kill_in_progress"]
                if not later and (now - at < KILL_RECENT_NS or now < at):
                    return ["kill_in_progress"]
        return []

    generation = env.args.continue_admission
    owner = f"pilot-capture:{os.getpid()}"
    try:
        with admission.live_ownership(paths, owner=owner, purpose=None if generation else "RS-19 pilot capture",
                                      config=config, continue_generation=generation, cleanup=False,
                                      before_admit=before_admit, clock=clock, processes=env.processes) as ticket:
            return _capture_owned(env, ticket, contract, start)
    except admission.OwnershipRefused as exc:
        if exc.code == "kill_in_progress":
            later = utc_from_ns(clock.time_ns() + KILL_RECENT_NS)
            raise Refused("kill_in_progress", [f"a kill may still be running; rerun pilot-capture once the kill has "
                                               f"exited, at least 90 s after it began (not before {later})"]) from None
        if exc.code == "admission_held":
            if exc.state == "admitted":
                raise Refused("admission_held", [f"admission held by {exc.owner}, generation {exc.generation}, state "
                                                 f"admitted; rerun with --continue-admission {exc.generation}"]) from None
            raise Refused("admission_held", [                    # OWN-3: a pending generation is never continued
                f"admission held by {exc.owner}, generation {exc.generation}, state {exc.state or 'unknown'} (its "
                f"admit never finished; nothing logged in under it); recording stays off until Yu's typed "
                f"recording-release (generation {exc.generation})"]) from None
        if exc.code == "continuation_needs_admitted_generation":
            raise Refused(exc.code, [f"admission {exc.owner} generation {exc.generation} state {exc.state}; recording "
                                     f"stays off until Yu's typed recording-release (generation {exc.generation})"]
                          ) from None
        if exc.code in ("owner_live", "owner_state_unknown"):
            raise Refused(exc.code) from None
        raise Refused(exc.code, [f"admission {exc.owner} generation {exc.generation} state {exc.state}"]
                      if exc.owner else []) from None
    except AdmissionRefused as exc:
        lines = admit_refusal_lines(exc, paths)
        if exc.outcome == "login_ledger_not_clean" and generation:
            lines.append(f"Yu decides on login-reset; rerun pilot-capture --continue-admission {generation} after it")
        raise Refused(exc.outcome, lines) from None


def _kill_identity(paths):
    """None (no KILL entry), (inode, mtime ns) of it, or "unknown" (an lstat error other than ENOENT)."""
    try:
        info = os.lstat(paths.kill_file)
    except FileNotFoundError:
        return None
    except OSError:
        return "unknown"
    return info.st_ino, info.st_mtime_ns


def _capture_owned(env, ticket, contract, start):
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    w.line(f"pilot-capture: owner {ticket.command_owner}, admission {ticket.owner}, generation {ticket.generation} "
           f"({ticket.kind})")
    if ticket.inhibit_preexisting and ticket.kind == "fresh":
        w.line(f"INHIBIT predates this capture; recording stays off after it until Yu's typed recording-release "
               f"(generation {ticket.generation})")
    try:
        credentials = load_session_credentials(config, paths, runner=env.keychain_runner, purposes=("read",))
    except CredentialError as exc:                           # OWN-2: nothing read or logged in; an orderly end
        w.line(f"credentials unavailable ({safe_code(str(exc))}); nothing was read or logged in")
        if ticket.kind == "fresh" and not ticket.inhibit_preexisting:
            w.lines(release_lines(admission.release_own(ticket, paths, config=config, clock=clock), env.paused_flag))
        elif ticket.kind == "fresh":
            w.line(f"recording stays off until Yu's typed recording-release (generation {ticket.generation})")
        return 5
    try:
        return _capture_body(env, ticket, contract, start, credentials)
    except BaseException:                                    # OWN-2: an abnormal end still names generation N
        if ticket.kind == "fresh":
            w.line(f"recording stays off until Yu's typed recording-release (generation {ticket.generation})")
        raise


def _capture_body(env, ticket, contract, start, credentials):
    paths, config, clock, w = env.paths, env.config, env.clock, env.terminal
    journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
    sessions = []
    outcome = {"stopped": None, "written": 0, "codes": {}}
    failure = None
    try:
        read = pilot_events_after(paths.journal_path("live"), 0)
        projection = recovery_projection(read["events"], contract=contract, now_ns=clock.time_ns())
        entries = capture_entries(projection)

        def client():
            if not sessions:
                sessions.append(env.read_session_factory(credentials["read"], guard=lambda: kill_rule()))
            return sessions[0][1]

        def kill_rule():
            """Rule (b): a kill or HALT kill_incomplete above start_head, a KILL entry new or changed since the start, or
            anything unreadable skips the rest."""
            after = pilot_events_after(paths.journal_path("live"), start.get("head", 0))
            if not after["readable"] or after["kill_seq"] is not None or any(
                    e["kind"] == "halt" and e["payload"].get("reason") == "kill_incomplete" for e in after["events"]):
                return "skipped_kill"
            return "skipped_kill" if _kill_identity(paths) != start.get("kill") else None

        try:
            outcome = capture(env, entries, read_client=client, journal=journal, contract=contract,
                              before_read=kill_rule, budget_blocks=lambda: cap_blocks(config, paths, clock=clock))
        except LoginBudgetExceeded:
            session_id = (projection["state"].sessions[-1]["session_id"] if projection["state"].sessions else None)
            with file_lock(paths.gateway_lock, timeout_s=config["http"]["order_deadline_s"] + 5, clock=clock):
                append_pilot(journal, refusal_note(contract, session_id=session_id, open_id=None, reservation_seq=None,
                                                   code="login_budget"))
            failure = "login_budget"
        except AuthError:
            failure = "login_failed"
        after = pilot_events_after(paths.journal_path("live"), 0)
        try:
            state = pilot.fold(after["events"], contract=contract, now_ns=clock.time_ns())
            if state.due_stops:
                flush_due(journal, state, paths=paths, config=config, clock=clock)
        except pilot.PilotError:
            failure = failure or "fold_failed"
    finally:
        for session, _ in sessions:
            try:
                session.close()
            except Exception:
                pass
        journal.close()
    w.json({"status": "captured", "written": outcome["written"], "codes": outcome["codes"],
            "stopped": outcome["stopped"], "failure": failure})
    if failure == "login_failed":
        w.line(LOGIN_RESET_LINE)
    clean = failure is None and outcome["stopped"] != "skipped_kill"
    if ticket.kind == "fresh" and clean and not ticket.inhibit_preexisting:
        result = admission.release_own(ticket, paths, config=config, clock=clock)
        w.lines(release_lines(result, env.paused_flag))
        return 0 if result["released"] else 5
    if ticket.kind == "fresh":
        w.line(f"recording stays off until Yu's typed recording-release (generation {ticket.generation})")
    return 0 if clean else 5
