"""The gateway adapter for RS-19's registered pilot executor (P1-06): identity reads, the registration receipt,
the raw-evidence store, the note §5 first-byte predicate and the trigger-collision rule the gateway and the P1-08
scheduler share.

docs/betfair-pilot-policy.md (RS-19) is the policy and docs/betfair-pilot-engineering.md (the note) the detail.
Every placement rule (the fold, classification, authority, time triggers, record columns, R13) lives in the pure
pilot.py and is called from here or from the gateway, never copied; so do the note §9 code tables, the outcome/scope
table, refusal codes, stop ids and the place-report extractor (P1-08 §3.1, §3.2), which this module re-exports under
their P1-06 names. What pilot.py may not do lives here: reading the contract file, git and the state root, and
writing evidence. pilot never imports this module. Not offline: it runs git and reads and writes the state root.

Process identity (PA2, Astra's ruling on critique 14): the code hashes are pinned once, when this module is first
imported (with the gateway), from the module root the running package was imported from, and reused by every
gateway in the process. Every pilot-place open requires pinned == disk now == the registration receipt, and that
the checked repository is that module root. Python exposes no hash of the code it actually loaded, so a file
changed between its import and this pin, or a stale bytecode cache, is not seen; the pin narrows the window to
process start and the disk check catches drift after it.
"""

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import subprocess

from . import pilot
from .config import money_pence
from .errors import ConfigError
from .receipts import (atomic_write_bytes, atomic_write_json, canonical_sha256, code_hashes, git_identity,
                       parse_utc_ns, strict_json_loads, utc_compact_from_ns)
from .reconcile import CLOCK_STEP_TOLERANCE_S
from .schemas import TERMINAL_STATES

NS = pilot.NS
END_MARGIN_NS = 360 * NS                 # note §3: no reservation starts within 6 min of the session's end (Q13)
DRIFT_TOLERANCE_NS = int(CLOCK_STEP_TOLERANCE_S * NS)        # note §5 check 6 (as order-path:424)
DECISION_MARGIN_NS = 30 * NS             # critique 10: an optimisation; the journal's before_ns is the gate
TRIGGER_WAIT_NS = 2 * NS                 # Q12: a record trigger this close is waited out once, then step A again
TRIGGER_HORIZON_SLACK_NS = 5 * NS        # critique 9's horizon slack
RESERVE_ATTEMPTS = 3
CLASSIFY_ATTEMPTS = 5
MODULE_ROOT = Path(__file__).resolve().parents[2]

# The registration receipt (note §7; P1-06 Q4). pilot-register (P1-08) writes it as
# STATE_ROOT/profiles/<p>/pilot/registration-<contract12>-<UTC compact>.json, mode 0600, and journals the sha256 of
# its bytes in the registration note; every pilot-place open reads it back by that sha256.
REGISTRATION_SCHEMA = "betangel.pilot-registration/v1"
REGISTRATION_KEYS = frozenset({"schema", "contract_sha256", "code_commit", "code_tree", "code_hashes_digest",
                               "config_sha256", "registered_at", "journal_seq"})
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_GIT_ID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# The note §9 code tables live in pilot.py with the outcome/scope table (P1-08 §3.1); these names are kept so P1-06
# code and tests read them as before. One definition each: these are the same objects.
AUTHORITY_CODES = pilot.AUTHORITY_CODES
NO_NEW_STOP_CODES = pilot.NO_NEW_STOP_CODES
MARKET_LATCH = pilot.MARKET_LATCH
MARKET_V9_REASONS = pilot.MARKET_V9_REASONS
SESSION_LATCH = pilot.SESSION_LATCH
MANUAL_BET_OPEN = pilot.MANUAL_BET_OPEN
NO_NOTE_LATCH = pilot.NO_NOTE_LATCH
KILL_REASONS = pilot.KILL_REASONS
FIRST_BYTE_CODES = pilot.FIRST_BYTE_CODES
HALT_IMPLYING_OPEN_CODES = pilot.HALT_IMPLYING_OPEN_CODES
refusal_code = pilot.refusal_code
place_report_failure = pilot.place_report_failure


def _pin_code_hashes():
    try:
        return canonical_sha256(code_hashes(MODULE_ROOT))
    except OSError:
        return None


_PROCESS_CODE_HASHES_SHA256 = _pin_code_hashes()


def process_code_hashes_sha256():
    """The code-hash digest pinned once for this process (None if it could not be read: PA2 then refuses)."""
    return _PROCESS_CODE_HASHES_SHA256


class ObservedClock:
    """The one clock every part of a pilot gateway reads (P106-C2, P106-R2-1): the gateway itself, its journal,
    JSON-RPC client, read client, session and risk guard, and every helper the gateway hands its clock to. Each call
    passes straight through to the clock it wraps; each wall reading (time_ns as an int, utc_now as its text) is
    also reported to the one observer its pilot gateway sets, which keeps it in a placement open's high-water mark.
    A second, different observer is refused: a clock another gateway took over would stop reporting to the first.
    With no observer, or outside a pilot-place open, it is the wrapped clock exactly. Attributes it does not define
    (a test fake's advance, step_wall) come from the wrapped clock. P1-08's CLI builds one and hands it to every
    part; the gateway refuses a pilot whose parts do not share it."""

    __slots__ = ("_clock", "_observer")

    def __init__(self, clock):
        object.__setattr__(self, "_clock", clock)
        object.__setattr__(self, "_observer", None)

    def set_observer(self, observer):
        if self._observer is not None and self._observer != observer:
            raise ConfigError("observed_clock_has_another_gateway")
        object.__setattr__(self, "_observer", observer)

    def time_ns(self):
        value = self._clock.time_ns()
        if self._observer is not None:
            self._observer(value)
        return value

    def utc_now(self):
        text = self._clock.utc_now()
        if self._observer is not None:
            self._observer(text)
        return text

    def monotonic_ns(self):
        return self._clock.monotonic_ns()

    def sleep(self, seconds):
        return self._clock.sleep(seconds)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_clock"), name)


@dataclass(frozen=True)
class PilotContext:
    """What the pilot gateway reads its identity from (P1-08's CLI builds it; tests inject fakes). repo_root must be
    the module root the running package came from; contract_bytes_reader(relative path) -> bytes defaults to reading
    it under repo_root; code_hashes(repo_root) is the disk reader; pinned_code_hashes_sha256 defaults to the
    process pin."""
    repo_root: Path
    git_runner: object = subprocess.run
    contract_bytes_reader: object = None
    code_hashes: object = code_hashes
    module_root: Path = MODULE_ROOT
    pinned_code_hashes_sha256: object = None

    def read_contract(self, relative):
        if self.contract_bytes_reader is not None:
            raw = self.contract_bytes_reader(relative)
        else:
            raw = (Path(self.repo_root) / relative).read_bytes()
        if not isinstance(raw, bytes):
            raise OSError("contract bytes unreadable")
        return raw

    def pinned(self):
        return self.pinned_code_hashes_sha256 if self.pinned_code_hashes_sha256 is not None \
            else process_code_hashes_sha256()


class GateRefusal(Exception):
    """A PA refusal code from an identity read (never quotes file content)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- identity (PA1-PA3) ---------------------------------------------------------------------------------------


def registration_path(paths, contract_sha256, now_ns):
    """Where pilot-register (P1-08) writes the receipt for a registration made at now_ns."""
    return paths.pilot_dir / f"registration-{contract_sha256[:12]}-{utc_compact_from_ns(now_ns)}.json"


def validate_registration(data):
    """The receipt dict, or GateRefusal("pa3_registration_unreadable"): exact keys and value shapes."""
    ok = (isinstance(data, dict) and set(data) == REGISTRATION_KEYS and data["schema"] == REGISTRATION_SCHEMA
          and all(isinstance(data[k], str) and _SHA_RE.fullmatch(data[k])
                  for k in ("contract_sha256", "code_hashes_digest", "config_sha256"))
          and all(isinstance(data[k], str) and _GIT_ID_RE.fullmatch(data[k]) for k in ("code_commit", "code_tree"))
          and type(data["journal_seq"]) is int and data["journal_seq"] >= 0)
    if ok:
        try:
            parse_utc_ns(data["registered_at"])
        except ValueError:
            ok = False
    if not ok:
        raise GateRefusal("pa3_registration_unreadable")
    return data


def read_registration(paths, receipt_sha256):
    """The registration receipt whose file bytes hash to receipt_sha256 (the session's, note §7), validated."""
    if not isinstance(receipt_sha256, str) or not _SHA_RE.fullmatch(receipt_sha256):
        raise GateRefusal("pa3_registration_unreadable")
    try:
        candidates = sorted(paths.pilot_dir.glob("registration-*.json"))
    except OSError:
        raise GateRefusal("pa3_registration_unreadable") from None
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if pilot.bytes_sha256(data) != receipt_sha256:
            continue
        try:
            return validate_registration(strict_json_loads(data.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            raise GateRefusal("pa3_registration_unreadable") from None
    raise GateRefusal("pa3_registration_unreadable")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def config_holds_contract(config, contract, caps):
    """[] or the config values that differ from contract p1 (PA3; critique 15; pilot-register uses it too): the
    strategy ref, the market and event types, and every cap the contract's caps block names (pence as integer
    pence, max_book_age_ms against max_book_age_s, counts and minutes as numbers; config_names is text)."""
    orders = config["orders"]
    found = []
    if orders["customer_strategy_ref"] != contract.customer_strategy_ref:
        found.append("customer_strategy_ref")
    if orders["rules"]["market_types"] != [contract.market_type]:
        found.append("rules.market_types")
    if orders["rules"]["event_type_ids"] != [contract.event_type_id]:
        found.append("rules.event_type_ids")
    if not isinstance(caps, dict):
        return found + ["caps"]
    for key in sorted(caps):
        value = caps[key]
        if key == "config_names":
            continue
        if type(value) is not int:
            found.append(f"caps.{key}")
            continue
        if key == "max_book_age_ms":
            have = _number(orders["caps"].get("max_book_age_s"))
            same = have is not None and have * 1000 == value
        elif key.endswith("_p"):
            name = key[:-2]
            source = orders["rules"] if name == "external_exposure_tolerance" else orders["caps"]
            try:
                same = name in source and money_pence(source[name], allow_zero=True) == value
            except ConfigError:
                same = False
        else:
            have = _number(orders["caps"].get(key))
            same = have is not None and have == value
        if not same:
            found.append(f"caps.{key}")
    return found


def code_identity(ctx, receipt):
    """PA2 failure codes ([] when the identity matches the receipt; note §7)."""
    found = []
    root = Path(ctx.repo_root).resolve()
    if root != Path(ctx.module_root).resolve():
        found.append("pa2_module_root_differs")
    git = git_identity(root, runner=ctx.git_runner)
    if git["commit"] is None or git["tree"] is None or git["dirty"] is None:
        found.append("pa2_identity_unknown")
    else:
        if git["commit"] != receipt["code_commit"]:
            found.append("pa2_commit_differs")
        if git["tree"] != receipt["code_tree"]:
            found.append("pa2_tree_differs")
        if git["dirty"]:
            found.append("pa2_tree_dirty")
    try:
        disk = canonical_sha256(ctx.code_hashes(root))
    except OSError:
        disk = None
    pinned = ctx.pinned()
    if disk is None or pinned is None:
        found.append("pa2_identity_unknown")
    elif not (pinned == disk == receipt["code_hashes_digest"]):
        found.append("pa2_code_hashes_differ")
    return sorted(set(found), key=found.index)


# --- journal reads that need no contract ------------------------------------------------------------------------


def _pilot_notes(events):
    for event in events:
        payload = event.get("payload")
        if event.get("kind") != "note" or not isinstance(payload, dict) or "pilot" not in payload:
            continue
        try:
            kind = pilot.validate_pilot_payload(payload)
        except pilot.PilotError:
            continue
        yield kind, payload["pilot"], event


def journal_contract_sha256(events):
    """The one contract sha256 the journal's valid pilot records name, or None (none, or more than one)."""
    found = {inner["contract_sha256"] for _, inner, _ in _pilot_notes(events)}
    return next(iter(found)) if len(found) == 1 else None


def latest_note(events, record_type, contract_sha256=None):
    """The inner payload of the latest valid pilot record of this type (for this contract, if named), or None."""
    found = None
    for kind, inner, _ in _pilot_notes(events):
        if kind == record_type and (contract_sha256 is None or inner["contract_sha256"] == contract_sha256):
            found = inner
    return found


def open_manual_refs(events, orders):
    """Refs of our orders that are not the pilot's (their intent's source is not "pilot") and are open on the
    account or matched and unsettled: not RECORDED (never sent), and either not terminal and not EXECUTION_COMPLETE
    with nothing matched, or matched and not yet SETTLED or VOIDED (a REPLACED bet's matched part settles later, as
    journal.py's unsettled liability counts it). orders is the core fold's (workflow #3)."""
    pilot_refs = {event.get("customer_order_ref") for event in events if event.get("kind") == "intent"
                  and isinstance(event.get("payload"), dict) and event["payload"].get("source") == "pilot"}

    def open_or_unsettled(order):
        if order["state"] == "RECORDED":
            return False
        if (order["size_matched_p"] or 0) > 0 and order["state"] not in ("SETTLED", "VOIDED"):
            return True
        return order["state"] not in TERMINAL_STATES and not (order["state"] == "EXECUTION_COMPLETE"
                                                               and order["size_matched_p"] == 0)

    return sorted(ref for ref, order in orders.items() if ref not in pilot_refs and open_or_unsettled(order))


def session_window_open(events, *, now_ns):
    """Whether any pilot session_start in the journal, for any contract, has not reached its end at now_ns
    (pilot.session_end_ns: UNTIL or 23:59:59Z). The manual-order guard (critique 13, Yu 2026-10-21) reads it from the
    durable journal alone, with or without orders.pilot or a PilotContext; a session a stop ended early still counts
    until its end (the conservative reading)."""
    return any(kind == "session_start" and now_ns < pilot.session_end_ns(inner)
               for kind, inner, _ in _pilot_notes(events))


# --- codes and latches ----------------------------------------------------------------------------------------


def latch_for(rule, reason=None):
    """'market', 'session', 'review' or 'none' for a refusal rule (and its first reason) on the pilot path."""
    if rule == "halted" and reason in KILL_REASONS:
        return "session"                         # critique 2: a KILL file seen ends the session in the journal
    if rule in NO_NOTE_LATCH:
        return "none"
    if rule == "validation:V9" and reason in MARKET_V9_REASONS:
        return "market"
    if rule in MARKET_LATCH:
        return "market"
    if rule in SESSION_LATCH:
        return "session"
    return "review"


def session_stop_id(code, *, reservation_seq, open_id):
    """'<code>-<res>-session', or '<code>-<open hex>-session' before any reservation (STOP_ID_RE): pilot's one
    builder, anchored at the reservation seq, else the open id's hex."""
    anchor = reservation_seq if reservation_seq is not None else open_id.split("-", 1)[-1].lower()
    return pilot.session_stop_id(code, anchor)


def trigger_blocks(state, *, t0_ns, evidence_deadline_ns, waited=False, look_ahead=True):
    """None, "wait" or "refuse": the reservation trigger rule (Q12; critique 9) for a reservation at t0_ns, the one
    rule the gateway's step A and the P1-08 scheduler's pre-check share. A record time trigger at or before the horizon
    t0 + (send deadline offset - window offset) + evidence deadline + slack collides with this placement: it is waited
    out once when it falls within TRIGGER_WAIT_NS of t0 ("wait", once only; waited says the open already waited), else
    refused. With look_ahead (the scheduler, which has not invoked the gateway yet), every trigger counts: a second one
    inside the horizon after the wait refuses at once, as the gateway would on its next pass. The gateway passes
    look_ahead False and re-reads its clock after the wait instead. Built on next_trigger_ns at max(t0, the state's
    time), so the gateway's own state (folded at t0) is read exactly as before."""
    contract = state.contract
    span = (contract.send_deadline_offset_s - contract.window_from_offset_s) * NS + evidence_deadline_ns \
        + TRIGGER_HORIZON_SLACK_NS
    at = max(t0_ns, state.now_ns)
    trigger = pilot.next_trigger_ns(replace(state, now_ns=at))
    if trigger is None or trigger > t0_ns + span:
        return None
    if waited or trigger - t0_ns > TRIGGER_WAIT_NS:
        return "refuse"
    if look_ahead:
        after = trigger + 1_000_000
        second = pilot.next_trigger_ns(replace(state, now_ns=max(after, state.now_ns)))
        if second is not None and second <= after + span:
            return "refuse"
    return "wait"


# --- raw reads and the evidence store (note §4 step 2; Q8, Q10) ---------------------------------------------------


def read_failure(read, *, deadline_ns):
    """None, or why a raw read cannot be evidence: a transport error or no body, a non-200 status, or an elapsed
    time that is negative or over the deadline on either clock, or wall and monotonic elapsed times apart by more
    than the drift tolerance (a sleep stops the monotonic clock). Q10: 5 s per request at most, a bound on the
    request's completion, not proof of the data's age."""
    if read.get("error") is not None:
        return "read_" + refusal_code(read["error"])[:40]
    if not isinstance(read.get("body"), bytes):
        return "read_no_body"
    if read.get("status") != 200:
        return "read_http_status"
    mono = read["received_monotonic_ns"] - read["sent_monotonic_ns"]
    wall = read["received_wall_ns"] - read["sent_wall_ns"]
    if mono < 0 or wall < 0:
        return "read_elapsed_negative"
    if mono > deadline_ns or wall > deadline_ns:
        return "read_elapsed_over_deadline"
    if abs(wall - mono) > DRIFT_TOLERANCE_NS:
        return "read_clock_drift"
    return None


def private_dir(path, top):
    """mkdir 0700 for path and each missing level below top (critique 13: made by the writer, never by ensure).
    Public for P1-08's writers (the receipt, the armed-session file, cause copies and raw captures)."""
    missing = []
    current = Path(path)
    while current != top and not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)


_private_dir = private_dir


def evidence_dir(paths, contract_sha256):
    return paths.pilot_dir / contract_sha256[:12] / "evidence"


def store_evidence(paths, contract_sha256, reservation_seq, name, read, body, failure):
    """Write the exact body bytes and a sidecar before classification (§5.3) -> the body's sha256. The same bytes
    object goes to the file, to its digest and to classify; raises OSError or SecretLeak on a failed write."""
    directory = evidence_dir(paths, contract_sha256)
    paths.ensure()
    _private_dir(directory, paths.profile_dir)
    digest = pilot.bytes_sha256(body)
    stem = f"{reservation_seq:08d}-{name}-{digest}"
    atomic_write_bytes(directory / f"{stem}.bin", body)
    atomic_write_json(directory / f"{stem}.json", {
        "method": read.get("method"), "params": read.get("params"), "status": read.get("status"),
        "error": read.get("error"), "read_failure": failure, "body_sha256": digest, "body_bytes": len(body),
        "sent_wall_ns": read.get("sent_wall_ns"), "sent_monotonic_ns": read.get("sent_monotonic_ns"),
        "received_wall_ns": read.get("received_wall_ns"), "received_monotonic_ns": read.get("received_monotonic_ns")})
    return digest


# --- the first-byte gate (note §5) --------------------------------------------------------------------------------


def first_byte_refusal(ctx, *, wall_ns, mono_ns, head):
    """None, or the code of note §5 checks 3-6 (checks 1 and 2, the KILL file and the send book's age, run first in
    the gateway). ctx: submit_head (seq, event_sha256) of our submit; submit_ns its recorded_at; seen_ns the largest
    time this open has observed, every own event's recorded_at included (P106-1); dbw and dbm the decision book's
    wall and monotonic stamps; end_ns the earliest of the send deadline, the session's end, the final date and the
    next record trigger. head is a fresh read of the journal head.
      3. the head is exactly our submit (any later event, from any writer, refuses);
      4. max(wall, submit_ns, seen_ns) is before end_ns (the expiry-clock edge: the fold judges the submit on its own
         recorded_at, stamped before the write lock);
      5. the decision book's wall time plus the monotonic time since it is before end_ns;
      6. wall and monotonic time since the decision book differ by at most 2 s."""
    if not isinstance(head, dict) or (head.get("seq"), head.get("last_sha256")) != tuple(ctx["submit_head"]):
        return "pilot_gate_head_moved"
    if max(wall_ns, ctx["submit_ns"], ctx["seen_ns"]) >= ctx["end_ns"]:
        return "pilot_gate_deadline"
    if ctx["dbw"] + (mono_ns - ctx["dbm"]) >= ctx["end_ns"]:
        return "pilot_gate_window"
    if abs((wall_ns - ctx["dbw"]) - (mono_ns - ctx["dbm"])) > DRIFT_TOLERANCE_NS:
        return "pilot_gate_clock_drift"
    return None
