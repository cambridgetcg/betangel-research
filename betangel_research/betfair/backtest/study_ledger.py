"""The study ledger of the research lane: one append-only, hash-chained file that counts every pass over real data.

File: <ledger dir>/study-ledger-v1.jsonl. One entry per line: the entry's canonical JSON (contract.canonical_json)
and a newline. An entry has exactly ENTRY_KEYS, and no value is ever null or a float:
ledger_schema (1); seq (0, 1, 2, ...); prev_sha256 (the entry before, GENESIS for seq 0); entry_sha256 (sha256 of
the canonical JSON of every other key); kind (look: a blind pass; explore: a pass over explore labels only;
registered: the one test); study (the study id); lineage (the idea the study belongs to); partitions (the sorted
labels the pass touched); partitions_sha256 (sha256 of the split file); registration_sha256, input_sha256,
source_descriptor_sha256 and code_commit (as a study receipt names them); m and alpha (family size and alpha spent
as decimal text; 0 and "0" unless registered); tables_sha256 (sha256 of the pass's summary.json bytes); result
(completed, or engine_check_failed for a look only); recorded_at (UTC text from the caller's clock). The file holds
hashes, counts and codes only. The format is fixed for good by the first entry.

A run's identity is IDENTITY_KEYS. A run whose identity is already in the ledger is a repeat: it appends nothing
and counts against no budget, and its tables hash and result must equal the stored ones (ledger:repeat_differs).

Budgets (their sizes come from the parsed split file, partition.Split), checked before the read and again at the
append:
- look: at most max_looks identities per study (ledger:looks_spent).
- explore or registered: for each explore label touched, at most explore_budget such entries per lineage
  (ledger:explore_budget_spent). A registered entry counts as one.
- registered: no earlier registered entry of the same lineage on the same holdout label, whatever its study id
  (ledger:lineage_already_tested); and the alpha of every registered entry on that label plus this one may not
  pass alpha_lifetime (ledger:alpha_spent; exact fractions).
A request is also refused (ledger:entry:<reason>) when its split hash is not the given split's, when a label is
unknown, sealed, never or has no budget, when an explore entry touches a label that is not an explore label, or
when a registered entry touches no holdout label.

How a runner uses it. Before the read: StudyLedger.open(directory, expected) checks the directory (ledger_dir: it
must exist and pass the checks a study's --out gets: path_guard, outside the repository and every Git worktree, no
data-tree name in the path, apart from the data root), refuses a directory that holds a trial ledger
(ledger:trial_ledger_present; the lock file below is shared) or a lock file (ledger:locked), verifies the whole chain
and compares its head with expected, the text the operator typed: "none" when the file must be absent, else
"SEQ:SHA256" (ledger:head_not_expected otherwise; so a fresh or mistyped directory refuses). An anchor (seq, sha256)
kept elsewhere must be in the file as well. Then check(request, split) refuses a spent budget and returns the stored
entry for a repeat, else None. After the read, when the summary bytes exist and before anything is written:
record(request, split, tables_sha256, result, clock) appends one entry in one write, or for a repeat compares the
hashes and appends nothing. It returns the head, which the operator keeps for the next pass.

The chain helpers are trial_ledger's, imported unchanged: the lock file is trial_ledger's LOCK_FILE in the ledger
directory (an existing one refuses; a person removes a leftover one after checking no pass is running), the
append is compare-and-append against the head seen at open (ledger:head_moved), the line is verified before it is
written and confirmed after, and a failure once writing began is WriteUncertainError. verify_bytes makes every
check trial_ledger.verify_bytes makes except the P0 fields and the trial lifecycle. Every refusal is
LedgerError("ledger:<reason>"); a bad line is "ledger:line <n>:<reason>".

What this does not do. Nothing repairs a broken chain: a person keeps the bad file unchanged and moves it aside.
Nothing is synced to disk. The budgets hold for one ledger file only: a new directory starts from zero, so keeping
the file and typing its last head is the operator's duty. A cut at a line boundary is seen only by the expected
head or an anchor. This module reads no clock, no data and no registration. Standard library only.
"""

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
import re

from .archive import check_data_root
from .contract import canonical_json
from .movement import RATE_RE
from .movement_study import COMMIT_RE, SHA256_RE, _in_data_tree
from .partition import EXPLORE, HOLDOUT, LABEL_RE
from .path_guard import PathDenied, guard_path
from .trial_ledger import LEDGER_FILE as TRIAL_LEDGER_FILE
from .trial_ledger import (GENESIS, LOCK_FILE, MAX_LEDGER_BYTES, MAX_LINE_BYTES, LedgerError, UnlockError,
                           WriteUncertainError, _absent, _anchor, _loads, _Lock, _lock_note, _read,
                           _recorded_at_problem, _write_all, entry_digest)

LEDGER_SCHEMA = 1
LEDGER_FILE = "study-ledger-v1.jsonl"
LOOK, EXPLORE_KIND, REGISTERED = "look", "explore", "registered"
KINDS = (LOOK, EXPLORE_KIND, REGISTERED)
COMPLETED, ENGINE_CHECK_FAILED = RESULTS = ("completed", "engine_check_failed")
ENTRY_KEYS = ("alpha", "code_commit", "entry_sha256", "input_sha256", "kind", "ledger_schema", "lineage", "m",
              "partitions", "partitions_sha256", "prev_sha256", "recorded_at", "registration_sha256", "result", "seq",
              "source_descriptor_sha256", "study", "tables_sha256")
SHA_KEYS = ("entry_sha256", "input_sha256", "partitions_sha256", "prev_sha256", "registration_sha256",
            "source_descriptor_sha256", "tables_sha256")
IDENTITY_KEYS = ("study", "kind", "registration_sha256", "input_sha256", "source_descriptor_sha256", "code_commit")
NO_HEAD = "none"

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


@dataclass(frozen=True)
class PassRequest:
    """The plain values of one pass, known before the read. partitions: the labels the pass touches; m and alpha:
    the family size and the alpha spent as decimal text, 0 and "0" unless kind is registered."""

    study: str
    lineage: str
    kind: str
    partitions: tuple
    partitions_sha256: str
    registration_sha256: str
    input_sha256: str
    source_descriptor_sha256: str
    code_commit: str
    m: int = 0
    alpha: str = "0"


def identity(item):
    """The identity of a run, from a PassRequest or an entry."""
    return tuple(item[key] if isinstance(item, dict) else getattr(item, key) for key in IDENTITY_KEYS)


def parse_head(text):
    """None for "none", else (seq, entry_sha256) of the text "SEQ:SHA256"; ledger:head_malformed otherwise."""
    if text == NO_HEAD:
        return None
    seq, _, digest = text.partition(":") if isinstance(text, str) else ("", "", "")
    try:
        if not (seq.isascii() and seq.isdigit() and len(seq) <= 18 and str(int(seq)) == seq):
            raise LedgerError("ledger:head_malformed")
        return tuple(_anchor((int(seq), digest)))
    except LedgerError:
        raise LedgerError("ledger:head_malformed") from None


def head_text(head):
    """The text an operator types for a head: "none" or "SEQ:SHA256"."""
    return NO_HEAD if head is None else f"{head[0]}:{head[1]}"


def ledger_dir(path, root=None):
    """The resolved ledger directory after the checks in the module docstring; root is the resolved data root, if
    the caller has one. LedgerError("ledger:<reason>") otherwise. Nothing is created."""
    if path is None or not str(path).strip():
        raise LedgerError("ledger:missing_path")
    if _in_data_tree(Path(path).absolute()):
        raise LedgerError("ledger:under_a_data_tree")
    try:
        resolved = guard_path(path, "ledger")
    except PathDenied as error:
        raise LedgerError(str(error)) from None
    if _in_data_tree(resolved):
        raise LedgerError("ledger:under_a_data_tree")
    _, problems = check_data_root(resolved)
    if problems:
        raise LedgerError(problems[0].replace("data_root:", "ledger:", 1))
    if root is not None and (resolved == root or root in resolved.parents or resolved in root.parents):
        raise LedgerError("ledger:roots_not_distinct")
    return resolved


def _field_problem(entry):
    """None when every field of an entry is well formed, else the first reason found."""
    if type(entry["ledger_schema"]) is not int or entry["ledger_schema"] != LEDGER_SCHEMA:
        return "ledger_schema_unsupported"
    if type(entry["seq"]) is not int or entry["seq"] < 0:
        return "seq_malformed"
    patterns = [(key, SHA256_RE) for key in SHA_KEYS] + [("code_commit", COMMIT_RE), ("study", _NAME_RE),
                                                          ("lineage", _NAME_RE)]
    for key, pattern in patterns:
        if not (isinstance(entry[key], str) and pattern.fullmatch(entry[key])):
            return f"{key}_malformed"
    kind, labels, m, alpha = entry["kind"], entry["partitions"], entry["m"], entry["alpha"]
    if not isinstance(kind, str) or kind not in KINDS:
        return "kind_not_allowed"
    if (type(labels) is not list or not labels or any(not (isinstance(label, str) and LABEL_RE.fullmatch(label))
                                                       for label in labels) or labels != sorted(set(labels))):
        return "partitions_malformed"
    if type(m) is not int or not isinstance(alpha, str):
        return "m_or_alpha_malformed"
    if kind == REGISTERED:
        if m < 1 or not RATE_RE.fullmatch(alpha) or not Fraction(Decimal(alpha)):
            return "m_or_alpha_malformed"
    elif (m, alpha) != (0, "0"):
        return "m_or_alpha_not_zero"
    result = entry["result"]
    if not isinstance(result, str) or result not in RESULTS or (result != COMPLETED and kind != LOOK):
        return "result_not_allowed"
    if _recorded_at_problem(entry["recorded_at"]):
        return "recorded_at_malformed"
    return None


def verify_bytes(data, anchor=None):
    """The entries of ledger bytes after every check in the module docstring; LedgerError otherwise."""
    if not data:
        raise LedgerError("ledger:empty")
    lines = data.split(b"\n")
    if lines[-1]:
        raise LedgerError(f"ledger:line {len(lines)}:truncated_line")
    entries, previous = [], GENESIS
    for index, line in enumerate(lines[:-1]):
        where = f"ledger:line {index + 1}"
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
        previous = entry["entry_sha256"]
        entries.append(entry)
    if anchor is not None:
        seq, digest = _checked_anchor(anchor)
        if seq >= len(entries):
            raise LedgerError("ledger:truncated_before_anchor")
        if entries[seq]["entry_sha256"] != digest:
            raise LedgerError("ledger:anchor_mismatch")
    return tuple(entries)


def _checked_anchor(anchor):
    try:
        return _anchor(anchor)
    except LedgerError:
        raise LedgerError("ledger:anchor_malformed") from None


def verify(path, anchor=None):
    """The entries of the ledger file at path; LedgerError when it cannot be trusted."""
    return verify_bytes(_read(Path(path)), anchor)


def _entry(request, seq, previous, tables_sha256, result, recorded_at):
    """The entry of a request, without its own hash; ledger:entry:partitions_malformed for labels that cannot be
    sorted into a list of text."""
    labels = request.partitions
    if type(labels) not in (tuple, list) or any(not isinstance(label, str) for label in labels):
        raise LedgerError("ledger:entry:partitions_malformed")
    return {"ledger_schema": LEDGER_SCHEMA, "seq": seq, "prev_sha256": previous, "kind": request.kind,
            "study": request.study, "lineage": request.lineage, "partitions": sorted(labels),
            "partitions_sha256": request.partitions_sha256, "registration_sha256": request.registration_sha256,
            "input_sha256": request.input_sha256, "source_descriptor_sha256": request.source_descriptor_sha256,
            "code_commit": request.code_commit, "m": request.m, "alpha": request.alpha,
            "tables_sha256": tables_sha256, "result": result, "recorded_at": recorded_at}


def _request_problem(draft, split):
    """None, or why a request (as its draft entry) can never be recorded under this split (module docstring)."""
    problem = _field_problem(draft)
    if problem:
        return problem
    if draft["partitions_sha256"] != split.sha256:
        return "split_not_the_requests"
    for label in draft["partitions"]:
        problem = split.label_problem(label)
        if problem:
            return f"partition_{problem}"
    roles = [split.role(label) for label in draft["partitions"]]
    if draft["kind"] == EXPLORE_KIND and roles.count(EXPLORE) != len(roles):
        return "explore_touches_another_role"
    if draft["kind"] == REGISTERED and HOLDOUT not in roles:
        return "registered_without_holdout"
    return None


def _budget_problem(entries, request, split):
    """None, or the first budget of the module docstring this request would break."""
    if request.kind == LOOK:
        looks = {identity(e) for e in entries if e["kind"] == LOOK and e["study"] == request.study}
        return "looks_spent" if len(looks) >= split.max_looks else None
    labels = sorted(request.partitions)
    for label in labels:
        if split.role(label) == EXPLORE:
            used = sum(1 for e in entries if e["kind"] != LOOK and e["lineage"] == request.lineage
                       and label in e["partitions"])
            if used >= split.explore_budget(label):
                return "explore_budget_spent"
    if request.kind == REGISTERED:
        tested = {label: [e for e in entries if e["kind"] == REGISTERED and label in e["partitions"]]
                  for label in labels if split.role(label) == HOLDOUT}
        if any(e["lineage"] == request.lineage for found in tested.values() for e in found):
            return "lineage_already_tested"
        for label, found in tested.items():
            spent = sum((Fraction(Decimal(e["alpha"])) for e in found), Fraction(Decimal(request.alpha)))
            if spent > split.alpha_lifetime(label):
                return "alpha_spent"
    return None


def _standing(entries, request, split):
    """The stored entry when the request repeats a run in entries, else None after the budget checks."""
    draft = dict(_entry(request, 0, GENESIS, GENESIS, COMPLETED, "2000-01-01T00:00:00Z"), entry_sha256=GENESIS)
    problem = _request_problem(draft, split)
    if problem:
        raise LedgerError(f"ledger:entry:{problem}")
    stored = next((e for e in entries if identity(e) == identity(request)), None)
    if stored is not None:
        if any(stored[key] != draft[key] for key in ("alpha", "lineage", "m", "partitions", "partitions_sha256")):
            raise LedgerError("ledger:repeat_differs")
        return stored
    problem = _budget_problem(entries, request, split)
    if problem:
        raise LedgerError(f"ledger:{problem}")
    return None


class StudyLedger:
    """A handle on the study ledger of one directory, remembering the head it last saw (module docstring)."""

    def __init__(self, directory, head):
        self.directory = directory
        self._head = head

    @classmethod
    def open(cls, directory, expected, root=None, anchor=None):
        """Every check a pass makes before the read but the budgets (module docstring). expected is the operator's
        text, "none" or "SEQ:SHA256"; it has no default."""
        expected = parse_head(expected)
        directory = ledger_dir(directory, root)
        if not _absent(directory / TRIAL_LEDGER_FILE):
            raise LedgerError("ledger:trial_ledger_present")
        if not _absent(directory / LOCK_FILE):
            raise LedgerError("ledger:locked")
        if _absent(directory / LEDGER_FILE):
            if anchor is not None:
                _checked_anchor(anchor)
                raise LedgerError("ledger:truncated_before_anchor")
            head = None
        else:
            entries = verify(directory / LEDGER_FILE, anchor)
            head = (entries[-1]["seq"], entries[-1]["entry_sha256"])
        if head != expected:
            raise LedgerError("ledger:head_not_expected")
        return cls(directory, head)

    @property
    def path(self):
        return self.directory / LEDGER_FILE

    @property
    def head(self):
        """(seq, entry_sha256) of the last entry this handle saw or wrote, or None for no ledger yet."""
        return self._head

    def entries(self):
        """Every entry now in the file, after verifying it still holds this handle's head; () only while no ledger
        file exists."""
        if self._head is None and _absent(self.path):
            return ()
        return verify(self.path, self._head)

    def counts(self, lineage):
        """How many looks and registered passes the ledger holds for a lineage, for the receipt."""
        kinds = [e["kind"] for e in self.entries() if e["lineage"] == lineage]
        return {"looks": kinds.count(LOOK), "registered": kinds.count(REGISTERED)}

    def check(self, request, split):
        """Before the read: the stored entry when the request is a repeat, else None; LedgerError for a request the
        ledger would refuse to record."""
        return _standing(self.entries(), request, split)

    def record(self, request, split, tables_sha256, result, clock):
        """After the read: append the pass's one entry, or for a repeat compare tables_sha256 and result with the
        stored ones and append nothing. Returns the head (seq, entry_sha256). clock() gives recorded_at.
        WriteUncertainError once writing began; every other LedgerError but UnlockError leaves the file as it was."""
        recorded_at = clock()
        lock = _Lock(self.directory, _lock_note(request.kind, request.study, recorded_at, request.code_commit))
        with lock:
            if self._head is None:
                if not _absent(self.path):
                    raise LedgerError("ledger:head_moved")
                data, entries = b"", ()
            else:
                data = _read(self.path)
                entries = verify_bytes(data)
                if (entries[-1]["seq"], entries[-1]["entry_sha256"]) != self._head:
                    raise LedgerError("ledger:head_moved")
            stored = _standing(entries, request, split)
            if stored is not None:
                if (stored["tables_sha256"], stored["result"]) != (tables_sha256, result):
                    raise LedgerError("ledger:repeat_differs")
            else:
                self._append(data, entries, _entry(request, len(entries), entries[-1]["entry_sha256"] if entries
                                                   else GENESIS, tables_sha256, result, recorded_at))
        if lock.unlock_failed:
            raise UnlockError(self._head)
        return self._head

    def _append(self, data, entries, entry):
        """Write one verified line and confirm it is the new head (trial_ledger's append, from the digest on)."""
        try:
            entry["entry_sha256"] = entry_digest(entry)
            line = canonical_json(entry) + b"\n"
        except (TypeError, ValueError, RecursionError):
            raise LedgerError("ledger:entry:not_canonical_json") from None
        try:
            if verify_bytes(data + line)[-1] != entry:
                raise LedgerError("ledger:entry:not_canonical_json")
        except LedgerError as error:
            raise LedgerError("ledger:entry:" + str(error).rsplit(":", 1)[1]) from None
        if len(data) + len(line) > MAX_LEDGER_BYTES:
            raise LedgerError("ledger:entry:ledger_full")
        try:
            with self.path.open("xb" if self._head is None else "ab", buffering=0) as handle:
                _write_all(handle, line)
        except FileExistsError:
            raise LedgerError("ledger:head_moved") from None
        except OSError:
            raise WriteUncertainError("ledger:write_failed") from None
        try:
            after = verify(self.path)
        except LedgerError as error:
            raise WriteUncertainError("ledger:append_not_confirmed") from error
        if len(after) != len(entries) + 1 or after[-1] != entry:
            raise WriteUncertainError("ledger:append_not_confirmed")
        self._head = (entry["seq"], entry["entry_sha256"])
