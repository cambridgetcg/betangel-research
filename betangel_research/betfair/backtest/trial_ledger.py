"""Append-only, hash-chained JSONL trial ledger in one explicitly supplied run directory (plan section 10).

File: <run_dir>/trial-ledger-v1.jsonl, one entry per line, each line the entry's canonical JSON (contract's
canonical_json: sorted keys, compact separators, UTF-8; a line that does not re-serialise to the same bytes is
refused) and a newline. Entry keys (ENTRY_KEYS): ledger_schema (1); seq (0, 1, 2, ...); prev_sha256 (the previous
entry's entry_sha256, GENESIS for seq 0); kind (started, failed, aborted or completed); trial_id (the caller's id of
one trial); contract_sha256 and seeds, computed here from the parsed contract, which must be valid_frozen (plan
section 10 and the contract docstring: no ledger write for a draft or invalid contract); variant (one of the
contract's variant ids); input_sha256 (the caller's sha256 naming every input of the run, for instance over the task
(b) index heads); training_cutoff (null: schema v1 fits nothing, and a contract whose fitting.kind is not none is
refused); exposure_record (EXPOSED_MONTHS and NOT_PROSPECTIVE_DATES from contract.py, as exposed_months and
not_prospective_dates: the file hashes, the known minimum viewed and the unknown inspection extent of outcome data
downloaded before any freeze, which plan section 3 says the trial ledger carries); code_identity (the caller's text
naming the code, a commit for instance: nothing here runs git or any process); recorded_at (the caller's UTC time,
YYYY-MM-DDTHH:MM:SS[.ffffff]Z: this module reads no clock, and seq, not time, orders entries, so a clock step cannot
break the chain); summary (an object of JSON values without floats, at most MAX_DEPTH deep and MAX_SUMMARY_BYTES
canonical bytes: counts, pence, exact ratios as text, a failure code); entry_sha256 (sha256 of the canonical JSON of
every other key).

Lifecycle: started opens a trial_id never seen before; failed, aborted or completed closes a started trial, once,
with the same TRIAL_FIXED_KEYS (contract, seeds, variant, inputs, training cutoff, exposure record and code). A
closed trial cannot be closed again or restarted.
Every started trial stays in the ledger, failed and aborted ones included (docs/edge-framework.md: discarded trials
are a source of overfitting).

Trial budget: append refuses a started entry (entry:max_trials_reached) once this ledger holds the contract's
stopping_rule.max_trials started entries with the same contract_sha256, whatever their variant or how they closed:
a trial is one started entry, so a caller that starts one trial per variant spends one per variant. verify cannot
check the budget (an entry carries the contract's hash, not the contract), so a run report states the count beside
the contract. The budget holds only within one ledger file: a fresh directory starts from zero and deleting a
directory erases its trials. Keep one ledger per contract, in a directory named by contract_sha256 outside every
per-run output directory, never deleted; that the budget is not spread over several ledgers is a workflow control
this module cannot enforce.

verify(path, anchor=None) reads the whole file (a regular file, not a symlink, at most MAX_LEDGER_BYTES;
ledger:missing only when lstat finds nothing, ledger:uninspectable when it cannot look) and checks every line: at
most MAX_LINE_BYTES, UTF-8, strict JSON (no duplicate key, NaN, infinity or float, no integer beyond 64 bits),
exactly ENTRY_KEYS, canonical bytes, valid fields, entry_sha256 recomputed, seq equal to its line index, prev_sha256
equal to the previous entry_sha256, the lifecycle above, and a final newline. An edited entry (hash or canonical
mismatch), a deleted or reordered one (seq or prev mismatch), a cut inside a line (no final newline, or not JSON)
and an empty file are refused with LedgerError("line <n>:<reason>" or "ledger:<reason>"). A cut at a line boundary
leaves a valid shorter chain, as does an edit whose author recomputed every later hash: only a head kept elsewhere
detects those. Each entry_sha256 covers prev_sha256, so a head commits to every earlier entry. anchor = (seq,
entry_sha256), as append returned it and a run report keeps it (a 2-item list, as JSON gives it back, is taken too),
must be in the file: a missing seq is truncated_before_anchor, a different hash anchor_mismatch.

Writes (TrialLedger): open() verifies an existing ledger first (against the caller's anchor, if any) and remembers
its head; append() is compare-and-append against that head. Under a lock file beside the ledger (LOCK_FILE, made by
exclusive create: an existing one refuses with locked), append re-reads and re-verifies the file and refuses
head_moved unless its head is still the remembered one. Before writing anything it refuses an entry whose line would
fail verify, whose own values differ from what that line reads back as (a tuple, or a key that is not text: their
JSON is something else), that would exceed the trial budget, or that would take the file past MAX_LEDGER_BYTES
(entry:ledger_full). It writes the first entry by exclusive create (a ledger file that appeared meanwhile is
head_moved) or a later one in append mode with unbuffered writes; then re-verifies and confirms that its entry is
the new head (append_not_confirmed otherwise) before advancing the remembered head. Every refusal before the write
leaves the ledger file as it was; a failure once writing has begun (ledger:write_failed while writing,
ledger:append_not_confirmed when the confirming read fails or does not end at its entry) raises WriteUncertainError,
and the file may then hold the whole entry, a cut line, or more. No LedgerError but UnlockError advances the
remembered head. Two handles opened at the same head cannot both append: the second is refused. A writer that
ignores the lock still cannot interleave silently: its entry carries the head it saw, so the chain breaks visibly.
If the lock file cannot be removed after a confirmed append, append raises UnlockError (ledger:unlock_failed, a
LedgerError carrying the new head): the entry is recorded and the head advanced. entries() reads the file as it is
now, after checking it still holds the handle's head: a handle opened on a missing ledger returns () only while the
file is still missing, as lstat confirms (a path it cannot inspect, a directory without search permission for
instance, is ledger:uninspectable, never taken as missing), and reads and verifies one that appeared since (another
handle's entries count against the same budget). Nothing is synced to disk (os is off the backtest allowlist), so a
power loss can lose or cut the last entry; verify then refuses the cut or the anchor shows the loss. The lock file
is the only other file this module writes.

Recovery (for a person; nothing here repairs or deletes anything). The lock file holds one ASCII line (JSON, cut at
MAX_TEXT characters) naming the trial_id, kind, recorded_at and code_identity of the append that took it. A lock a
crash left behind refuses every append until a person removes it, after checking that no writer is still running for
that trial: removing a live writer's lock lets two writers fork the chain, which verify then refuses for good. A
ledger verify refuses (a cut line, a fork, an edit, too large) is kept as evidence, never edited or truncated back
into shape: move it aside unchanged and start a new ledger whose first entry's summary names the old file's sha256.
The old file's started trials still count against the contract's budget, which the new file cannot see. No
LedgerError is permission to start afresh. After WriteUncertainError the attempted entry may be in the file whole (a
started trial that now counts against the budget), cut (verify refuses the file: keep it as above) or followed by
another writer's entry. Before any retry, verify the file, look for the attempted trial_id and check the run's saved
anchor, then reopen with TrialLedger.open to continue from the true head.
"""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from .contract import EXPOSED_MONTHS, NOT_PROSPECTIVE_DATES, SEED_RANGE, canonical_json, validate_contract

LEDGER_SCHEMA = 1
LEDGER_FILE = "trial-ledger-v1.jsonl"
LOCK_FILE = LEDGER_FILE + ".lock"
ENTRY_KINDS = ("aborted", "completed", "failed", "started")
CLOSING_KINDS = ("aborted", "completed", "failed")
ENTRY_KEYS = ("code_identity", "contract_sha256", "entry_sha256", "exposure_record", "input_sha256", "kind",
              "ledger_schema", "prev_sha256", "recorded_at", "seeds", "seq", "summary", "training_cutoff", "trial_id",
              "variant")
TRIAL_FIXED_KEYS = ("code_identity", "contract_sha256", "exposure_record", "input_sha256", "seeds", "training_cutoff",
                    "variant")
EXPOSURE_KEYS = ("exposed_months", "not_prospective_dates")
SEED_KEYS = ("bootstrap", "selection")
GENESIS = "0" * 64
MAX_SUMMARY_BYTES = 8192
MAX_DEPTH = 4
MAX_TEXT = 1024
MAX_LINE_BYTES = 16384
MAX_LEDGER_BYTES = 64 << 20
INT_RANGE = (-(2 ** 63), 2 ** 63 - 1)

_SHA_RE = re.compile(r"[0-9a-f]{64}")
_TRIAL_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_VARIANT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_IDENTITY_RE = re.compile(r"[\x20-\x7e]{1,200}")
_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RECORDED_AT_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,6})?Z")


class LedgerError(ValueError):
    """A ledger that cannot be trusted or an append that was refused; str() is "<where>:<reason>"."""


class WriteUncertainError(LedgerError):
    """ledger:write_failed or ledger:append_not_confirmed: append had begun writing, so the file may hold the whole
    entry, part of its line, or entries it could not confirm, and the handle's head did NOT advance. Verify the file
    and reconcile the attempted trial_id and the saved anchor before any retry (module docstring, Recovery)."""


class UnlockError(LedgerError):
    """ledger:unlock_failed: the entry WAS written and confirmed and the handle's head advanced to head, but the lock
    file could not be removed, so every later append refuses with ledger:locked until a person removes it."""

    def __init__(self, head):
        super().__init__("ledger:unlock_failed")
        self.head = head


def _refuse_duplicates(pairs):
    found = {}
    for key, value in pairs:
        if key in found:
            raise LedgerError("duplicate_key")
        found[key] = value
    return found


def _refuse_float(text):
    raise LedgerError("float_not_allowed")


def _bounded_int(text):
    if len(text) > 20 or not INT_RANGE[0] <= int(text) <= INT_RANGE[1]:
        raise LedgerError("integer_too_large")
    return int(text)


def _loads(text):
    return json.loads(text, object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_float,
                      parse_float=_refuse_float, parse_int=_bounded_int)


def entry_digest(entry):
    """sha256 of the canonical JSON of every key but entry_sha256."""
    return hashlib.sha256(canonical_json({k: v for k, v in entry.items() if k != "entry_sha256"})).hexdigest()


def _summary_problem(value, depth=0):
    """None when value is an allowed summary value at this depth, else the reason."""
    if depth > MAX_DEPTH:
        return "summary_too_deep"
    if value is None or type(value) is bool:
        return None
    if type(value) is int:                  # parse_int refuses a wider integer first: unreachable today, kept
        return None if INT_RANGE[0] <= value <= INT_RANGE[1] else "summary_integer_too_large"
    if type(value) is str:
        return None if len(value) <= MAX_TEXT else "summary_text_too_long"
    if type(value) is list:
        return next((p for p in (_summary_problem(item, depth + 1) for item in value) if p), None)
    if type(value) is dict:
        if any(not (isinstance(key, str) and _KEY_RE.fullmatch(key)) for key in value):
            return "summary_key_malformed"
        return next((p for p in (_summary_problem(item, depth + 1) for item in value.values()) if p), None)
    return "summary_value_not_allowed"


def _field_problem(entry):
    """None when every field of an entry is well formed, else the first reason found."""
    if type(entry["ledger_schema"]) is not int or entry["ledger_schema"] != LEDGER_SCHEMA:
        return "ledger_schema_unsupported"
    if type(entry["seq"]) is not int or entry["seq"] < 0:
        return "seq_malformed"
    for key in ("contract_sha256", "entry_sha256", "input_sha256", "prev_sha256"):
        if not (isinstance(entry[key], str) and _SHA_RE.fullmatch(entry[key])):
            return f"{key}_malformed"
    if entry["kind"] not in ENTRY_KINDS:
        return "kind_not_allowed"
    if not (isinstance(entry["trial_id"], str) and _TRIAL_ID_RE.fullmatch(entry["trial_id"])):
        return "trial_id_malformed"
    if not (isinstance(entry["variant"], str) and _VARIANT_RE.fullmatch(entry["variant"])):
        return "variant_malformed"
    if not (isinstance(entry["code_identity"], str) and _IDENTITY_RE.fullmatch(entry["code_identity"])):
        return "code_identity_malformed"
    if _recorded_at_problem(entry["recorded_at"]):
        return "recorded_at_malformed"
    if entry["training_cutoff"] is not None:
        return "training_cutoff_not_null"
    if _exposure_problem(entry["exposure_record"]):
        return "exposure_record_malformed"
    seeds = entry["seeds"]
    if (type(seeds) is not dict or sorted(seeds) != list(SEED_KEYS)
            or any(type(seeds[k]) is not int or not SEED_RANGE[0] <= seeds[k] <= SEED_RANGE[1] for k in SEED_KEYS)):
        return "seeds_malformed"
    if type(entry["summary"]) is not dict:
        return "summary_not_an_object"
    problem = _summary_problem(entry["summary"])
    if problem:
        return problem
    if len(canonical_json(entry["summary"])) > MAX_SUMMARY_BYTES:
        return "summary_too_large"
    return None


def _exposure_problem(record):
    """True unless record is {exposed_months, not_prospective_dates}, each {key: {field: text}}."""
    if type(record) is not dict or sorted(record) != list(EXPOSURE_KEYS):
        return True
    for items in record.values():
        if type(items) is not dict:
            return True
        for key, fields in items.items():
            if len(key) > 16 or type(fields) is not dict:
                return True
            if any(not (_KEY_RE.fullmatch(name) and type(text) is str and len(text) <= MAX_TEXT)
                   for name, text in fields.items()):
                return True
    return False


def _recorded_at_problem(text):
    match = _RECORDED_AT_RE.fullmatch(text) if isinstance(text, str) else None
    if not match:
        return True
    try:
        datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return True
    return False


def _absent(path):
    """True only when nothing, not even a symlink, is at path (lstat says FileNotFoundError); False when something
    is. Any other inspection error (no search permission on the directory, for instance) is
    LedgerError("ledger:uninspectable"): Path.exists and is_symlink both answer False then, which is not absence."""
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        raise LedgerError("ledger:uninspectable") from None
    return False


def _read(path):
    """The bytes of a ledger file: a regular file, not a symlink, at most MAX_LEDGER_BYTES."""
    if _absent(path):
        raise LedgerError("ledger:missing")
    try:
        if path.is_symlink():
            raise LedgerError("ledger:symlink")
        if not path.is_file():
            raise LedgerError("ledger:not_a_regular_file")
        with path.open("rb") as handle:
            data = handle.read(MAX_LEDGER_BYTES + 1)
    except OSError:
        raise LedgerError("ledger:unreadable") from None
    if len(data) > MAX_LEDGER_BYTES:
        raise LedgerError("ledger:too_large")
    return data


def _check_lifecycle(entry, trials):
    """None, or the reason entry breaks the trial lifecycle; trials maps trial_id -> [started entry, closed]."""
    trial = trials.get(entry["trial_id"])
    if entry["kind"] == "started":
        if trial is not None:
            return "trial_already_started"
        trials[entry["trial_id"]] = [entry, False]
        return None
    if trial is None:
        return "trial_not_started"
    if trial[1]:
        return "trial_already_closed"
    if any(entry[key] != trial[0][key] for key in TRIAL_FIXED_KEYS):
        return "trial_mismatch"
    trial[1] = True
    return None


def verify_bytes(data, anchor=None):
    """The entries of ledger bytes after every check in the module docstring; LedgerError otherwise."""
    if not data:
        raise LedgerError("ledger:empty")
    lines = data.split(b"\n")
    if lines[-1]:
        raise LedgerError(f"line {len(lines)}:truncated_line")
    entries, trials, previous = [], {}, GENESIS
    for index, line in enumerate(lines[:-1]):
        where = f"line {index + 1}"
        if len(line) > MAX_LINE_BYTES:
            raise LedgerError(f"{where}:line_too_long")
        try:
            entry = _loads(line.decode("utf-8"))
        except (LedgerError, UnicodeDecodeError, ValueError, RecursionError):
            raise LedgerError(f"{where}:not_strict_json") from None
        if not isinstance(entry, dict) or sorted(entry) != list(ENTRY_KEYS):
            raise LedgerError(f"{where}:keys_not_entry_keys")
        try:
            canonical = canonical_json(entry)
        except (TypeError, ValueError, RecursionError):            # an escaped lone surrogate has no UTF-8
            raise LedgerError(f"{where}:not_canonical") from None
        if canonical != line:
            raise LedgerError(f"{where}:not_canonical")
        problem = _field_problem(entry)
        if problem:
            raise LedgerError(f"{where}:{problem}")
        if entry["entry_sha256"] != entry_digest(entry):
            raise LedgerError(f"{where}:entry_hash_mismatch")
        if entry["seq"] != index:
            raise LedgerError(f"{where}:seq_out_of_order")
        if entry["prev_sha256"] != previous:
            raise LedgerError(f"{where}:prev_sha256_mismatch")
        problem = _check_lifecycle(entry, trials)
        if problem:
            raise LedgerError(f"{where}:{problem}")
        previous = entry["entry_sha256"]
        entries.append(entry)
    if anchor is not None:
        seq, digest = _anchor(anchor)
        if seq >= len(entries):
            raise LedgerError("ledger:truncated_before_anchor")
        if entries[seq]["entry_sha256"] != digest:
            raise LedgerError("ledger:anchor_mismatch")
    return tuple(entries)


def _anchor(anchor):
    """anchor checked: (seq, entry_sha256) as a tuple or a 2-item list (a head read back from a JSON report)."""
    if (type(anchor) not in (tuple, list) or len(anchor) != 2 or type(anchor[0]) is not int or anchor[0] < 0
            or not (isinstance(anchor[1], str) and _SHA_RE.fullmatch(anchor[1]))):
        raise LedgerError("anchor:malformed")
    return anchor


def verify(path, anchor=None):
    """The entries of the ledger file at path (module docstring); LedgerError when it cannot be trusted."""
    return verify_bytes(_read(Path(path)), anchor)


class _Lock:
    """The exclusive-create lock file beside the ledger, holding a one-line note of the append (module docstring),
    removed on exit. A failure to remove it after a clean append is reported by append as UnlockError."""

    def __init__(self, run_dir, note):
        self.path = run_dir / LOCK_FILE
        self.note = note
        self.held = False
        self.unlock_failed = False

    def __enter__(self):
        try:
            handle = self.path.open("x", encoding="ascii")
        except FileExistsError:
            raise LedgerError("ledger:locked") from None
        except OSError:
            raise LedgerError("ledger:lock_failed") from None
        self.held = True
        try:
            with handle:
                handle.write(self.note)
        except OSError:
            pass                                # the file alone is the lock; its note only helps a person judge it
        return self

    def __exit__(self, kind, error, trace):
        if self.held:
            self.held = False
            try:
                self.path.unlink()
            except OSError:
                self.unlock_failed = kind is None
        return False


def _lock_note(kind, trial_id, recorded_at, code_identity):
    """One ASCII line (JSON, cut at MAX_TEXT) naming the append that holds the lock; any value that is not text is
    given by its repr, so the note never fails, whatever the caller passed (append validates the values later)."""
    values = {"code_identity": code_identity, "kind": kind, "recorded_at": recorded_at, "trial_id": trial_id}
    text = {name: value if isinstance(value, str) else repr(value) for name, value in values.items()}
    return json.dumps(text, sort_keys=True, ensure_ascii=True)[:MAX_TEXT] + "\n"


def _write_all(handle, data):
    view = memoryview(data)
    while view:
        written = handle.write(view)
        if not written:
            raise WriteUncertainError("ledger:write_failed")
        view = view[written:]


class TrialLedger:
    """A handle on the ledger of one run directory, remembering the head it last saw (module docstring)."""

    def __init__(self, run_dir, head):
        self.run_dir = run_dir
        self._head = head

    @classmethod
    def open(cls, run_dir, anchor=None):
        """Verify an existing ledger (against anchor, if given) and remember its head; a missing one is empty,
        unless an anchor says it had entries. A ledger path that cannot be inspected is ledger:uninspectable, never
        taken as missing."""
        run_dir = Path(run_dir)
        if not run_dir.is_dir():
            raise LedgerError("run_dir:not_a_directory")
        path = run_dir / LEDGER_FILE
        if _absent(path):
            if anchor is not None:
                _anchor(anchor)
                raise LedgerError("ledger:truncated_before_anchor")
            return cls(run_dir, None)
        entries = verify(path, anchor)
        return cls(run_dir, (entries[-1]["seq"], entries[-1]["entry_sha256"]))

    @property
    def path(self):
        return self.run_dir / LEDGER_FILE

    @property
    def head(self):
        """(seq, entry_sha256) of the last entry this handle saw or wrote, or None for an empty ledger."""
        return self._head

    def entries(self):
        """Every entry now in the file, after verifying it still holds this handle's head; () only while no ledger
        exists, confirmed by lstat (ledger:uninspectable when the path cannot be inspected). A handle opened on a
        missing ledger reads and verifies one that appeared since, so a stale handle cannot report another handle's
        trials, or a cut first write, as an empty ledger."""
        path = self.path
        if self._head is None and _absent(path):
            return ()
        return verify(path, self._head)

    def append(self, kind, *, trial_id, contract, variant, input_sha256, code_identity, recorded_at, summary):
        """Compare-and-append one entry; returns the new head (seq, entry_sha256). WriteUncertainError (write_failed,
        append_not_confirmed) once writing began: the file may hold the whole entry or part of it, so verify and
        reconcile before any retry (module docstring, Recovery). Every other LedgerError is raised before the ledger
        file is written. UnlockError: written and confirmed, but the lock file stayed behind. The head advances only
        on success or UnlockError."""
        problems, digest = validate_contract(contract)
        if problems or contract.get("status") != "frozen":
            raise LedgerError("contract:not_valid_frozen")
        if contract["fitting"]["kind"] != "none":       # validate_contract refuses walk_forward first: kept as a guard
            raise LedgerError("contract:fitting_not_supported")
        if variant not in [item["id"] for item in contract["variants"]]:
            raise LedgerError("variant:not_in_contract")
        entry = {"ledger_schema": LEDGER_SCHEMA, "kind": kind, "trial_id": trial_id, "contract_sha256": digest,
                 "seeds": {key: contract["seeds"][key] for key in SEED_KEYS}, "variant": variant,
                 "input_sha256": input_sha256, "training_cutoff": None,
                 "exposure_record": {"exposed_months": EXPOSED_MONTHS, "not_prospective_dates": NOT_PROSPECTIVE_DATES},
                 "code_identity": code_identity, "recorded_at": recorded_at, "summary": summary}
        path = self.path
        lock = _Lock(self.run_dir, _lock_note(kind, trial_id, recorded_at, code_identity))
        with lock:
            if self._head is None:
                if not _absent(path):
                    raise LedgerError("ledger:head_moved")
                data, entries = b"", ()
            else:
                data = _read(path)
                entries = verify_bytes(data)
                if (entries[-1]["seq"], entries[-1]["entry_sha256"]) != self._head:
                    raise LedgerError("ledger:head_moved")
            entry.update(seq=len(entries), prev_sha256=entries[-1]["entry_sha256"] if entries else GENESIS)
            try:
                entry["entry_sha256"] = entry_digest(entry)
                line = canonical_json(entry) + b"\n"
            except (TypeError, ValueError, RecursionError):
                raise LedgerError("entry:not_canonical_json") from None
            # Refuse before writing anything: the line as verify will read it, then the caller's own values, which
            # must be what that line reads back as (a tuple or a non-text key would serialise to something else and
            # make the written entry fail its confirmation).
            try:
                verify_bytes(data + line)
            except LedgerError as error:
                raise LedgerError("entry:" + str(error).split(":", 1)[1]) from None
            problem = _field_problem(entry)
            if problem:
                raise LedgerError(f"entry:{problem}")
            if kind == "started":
                started = sum(1 for e in entries if e["kind"] == "started" and e["contract_sha256"] == digest)
                if started >= contract["stopping_rule"]["max_trials"]:
                    raise LedgerError("entry:max_trials_reached")
            if len(data) + len(line) > MAX_LEDGER_BYTES:
                raise LedgerError("entry:ledger_full")
            try:
                with path.open("xb" if self._head is None else "ab", buffering=0) as handle:
                    _write_all(handle, line)
            except FileExistsError:
                raise LedgerError("ledger:head_moved") from None
            except OSError:
                raise WriteUncertainError("ledger:write_failed") from None
            try:
                after = verify(path)
            except LedgerError as error:
                raise WriteUncertainError("ledger:append_not_confirmed") from error
            if len(after) != len(entries) + 1 or after[-1] != entry:
                raise WriteUncertainError("ledger:append_not_confirmed")
            self._head = (entry["seq"], entry["entry_sha256"])
        if lock.unlock_failed:
            raise UnlockError(self._head)
        return self._head
