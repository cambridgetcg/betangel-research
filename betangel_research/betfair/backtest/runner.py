"""Run one frozen P0 contract over ingested and indexed archives: the runner core (task c2b2).

run_backtest(contract, data_root, lab_state_root, run_id, code_identity) always returns one JSON-ready result; its
status is refused (no trial started; the lock released), completed, failed (both trials started and closed as
failed, a receipt written) or uncertain (the ledger may hold more than the result can show, or a lock could not be
released: the runner lock is kept and the trial ids named, for a person to reconcile). It never places an order,
logs in or reads an account; it reads only the explicit files named below, each after path_guard has cleared it.

Preflight, before any trial starts (Astra's design agreement D4, D12, D14 and her runner clarifications):
1. guard_path the contract file, the data root and the lab-state root, before anything else touches them.
2. The contract must be valid_frozen and the supported P0 model: CohortRules (exactly the two P0 variants),
   AccountingRules (N1: the commission rate is checked before any trial is consumed), BootstrapPlan, delay_s 0.
3. check_data_root both roots (outside the repository and every Git worktree); they must be distinct and neither may
   hold the other. run_id is one component ([a-z0-9][a-z0-9_.-]{0,40}); code_identity is printable ASCII (1 to 200
   characters, as the trial ledger requires), for instance a commit; nothing here runs git.
4. The input descriptor: every archive under the data root (manifests/), each with its manifest (load_manifest:
   canonical bytes, its own sha256), its index head (canonical bytes at most MAX_JSON_BYTES, the index kind and
   schema, naming this archive and this manifest, its refused_markets well formed) and its markets file (the head's
   sha256 and record count). input_sha256 is the sha256 of the descriptor's canonical JSON ({kind, version, archives
   sorted by archive_sha256, each with archive, manifest, index and markets sha256 and the record count}). Every
   derived path is guarded before it is opened: manifests, heads, markets files, and each raw member before its
   replay.
5. A first pass over every markets file (lines at most MAX_RECORD_BYTES) checks the shape of each record the run
   reads (market id, provenance naming this archive with a plan and a member path, decision and outcome objects, its
   path month and every marketTime month it saw) and refuses a record any of whose months the CURRENT month gate
   refuses (stale_gate: an index built under an older gate never lets the run read sealed, exposed or later data;
   the manifest's gate snapshot is only reported, as manifest_gate_current) and two records of one (plan, market id)
   across archives, before anything is decided.
6. The per-contract runner lock, <lab>/contracts/<contract sha256>/runner.lock, made by exclusive create: one runner
   attempt (or reconcile) per contract at a time; a lock left behind refuses until a person removes it, never taken
   by force.
7. Under the lock, the trial ledger, <lab>/contracts/<contract sha256>/trial-ledger-v1.jsonl (one per contract,
   independent of run_id; Q1), is opened at the highest ledger head (by sequence) any receipt names, so a ledger
   deleted or cut behind a receipt refuses. Every run directory under <lab>/runs/<contract sha256>/ (an entry whose
   name is not a run id is ignored; a run directory must be a plain directory there, not a link elsewhere) is
   accounted for. A receipt is checked whole and refuses when it does not match (any read or shape failure included,
   never ignored): canonical bytes, kind and schema, this contract, its own run directory; its trials exactly the
   ledger's trials of that run id, at most one per contract variant (a run that crashed between its two starts has
   one), each start and close present in the ledger with the right trial id and kind; its status exactly the one the
   close kinds give (aborted if any aborted, else failed if any failed, else completed); its head at or after its
   closes; results_sha256 null unless completed, and for a completed run results.json and issuance-ledger.jsonl
   still having the digests the receipt and the closing entries name. Trials started and never closed, runs whose
   trials all closed without a receipt, and run directories with neither a receipt nor any ledger trial (a crash
   between creating it and the first start, or a ledger cut behind it) refuse until reconcile. A run_id that already
   named trials or a run directory refuses. Capacity: the contract's started entries plus one per variant must not
   exceed stopping_rule.max_trials (D4: both starts are reserved together; every start ever made counts).

The run: the run directory <lab>/runs/<contract sha256>/<run_id>/ is created (exclusive), one started entry per
variant is appended (trial id <run_id>.<variant>), then every record is decided (cohort.decide_market, replaying
the primary member when the decision needs a view), each market with an issued bet gets its outcome
(outcome.market_outcome), the bets are settled (accounting.settle), and metrics and the block bootstrap are computed
per variant (metrics). Records are streamed: only decisions and outcomes are kept. The markets files are rehashed
while read and must still match their heads. Artifacts, each written by exclusive create while hashed:
- issuance-ledger.jsonl: cohort.ledger_lines, the full issuance ledger.
- results.json: canonical JSON of the contract sha256, run_id, code identity, input descriptor and sha256,
  population counts (cohort classes and reasons, plus the index heads' source refusals as raw rows and distinct
  markets per reason), issuance counts and the ledger's sha256, per-variant metrics, unresolved reasons and bootstrap
  results, and engineering_acceptance with status not_evaluated: the in-run checks are task c2b3, so nothing here is
  an engineering pass. No wall-clock time is in it: the same contract, input and code give the same bytes but for
  run_id.
- Then one closing entry per trial (completed, its summary naming the results and issuance sha256; or failed with a
  bounded failure code when anything went wrong after the starts), and
- receipt.json: status, results and issuance sha256, each trial's start and close anchors (seq, entry_sha256), the
  ledger head after closing, the contract's started entries at that moment (cumulative_starts), and recorded_at.
The lock is released last. When the first start is refused before anything was written (a plain LedgerError), the
empty run directory is removed and the run is refused. A refused second start, a refused or uncertain close, a
WriteUncertainError or UnlockError, a receipt that cannot be written or a lock that cannot be released stop at once
with status uncertain and keep the lock: the ledger may hold more than this result shows, so a person reconciles
before anything retries (the trial ledger's Recovery rule).

reconcile(contract, lab_state_root, code_identity) is that explicit operator action, under the same lock: it appends
an aborted entry for every trial that started and never closed (the summary names the reason, who reconciled and the
pending trial ids); for each unaccounted run directory it first writes recovery-intent.json (the contract variants
to record, by exclusive create) and then records one started and one aborted entry per variant (every persisted
partial reservation consumes budget; input_sha256 is UNKNOWN_INPUT_SHA256, 64 zeros, because the input it was for is
unknown). A run with an intent whose variants are not all recorded is recovery_incomplete: runs refuse, no receipt
is published for it, and the next reconcile finishes it (an interruption cannot shrink the record; a budget
exhausted mid-recovery leaves it explicitly incomplete). A run that crashed between its own two starts has no intent
and keeps its one trial. Then every missing receipt is published from the ledger (reconciled_by set). Statuses are
honest: refused only when nothing was written (a file that could not be created counts as nothing); once an append
is confirmed or a file created, any later failure is uncertain, with the lock kept and the appended trials and
touched runs named. Nothing is deleted, no lock is taken by force and nothing is evaluated again. Presence is judged
with lstat throughout (receipts, intents, the ledger, run directories): only FileNotFoundError means absent; any
other inspection error refuses before anything is written.

Limits: the budget holds within one lab-state root and one contract sha256 (Q1): a new root, or a contract changed in
any way (max_trials included), starts its own ledger; keep one root per contract campaign. The guards and checks are
point-in-time: a concurrent writer that ignores the lock, or a filesystem changed between check and use, is outside
them (the lock and exclusive creates narrow, not close, that window).
"""

from datetime import datetime, timezone
import hashlib
import re

from .accounting import AccountingRules, settle
from .archive import DEFAULT_LIMITS, ArchiveProblem, check_data_root, gate_snapshot, month_gate, safe_path, safe_target
from .cohort import CohortRules, decide_market, issuance_counts, ledger_lines, population_counts
from .contract import ContractFileError, canonical_json, load_contract, result_status, validate_contract
from .market_index import HEAD_FILE, HEAD_KIND, INDEX_SCHEMA_VERSION, MARKETS_FILE, IndexProblem, archive_shas
from .market_index import load_manifest, loads_strict
from .metrics import BootstrapPlan, as_record, block_bootstrap, metrics_by_variant
from .outcome import issued_bets, market_outcome
from .path_guard import PathDenied, guard_path
from .replay import ReplayProblem, replay_view
from .trial_ledger import LEDGER_FILE, LedgerError, TrialLedger, UnlockError, WriteUncertainError

RUNNER_SCHEMA_VERSION = 1
INPUT_KIND = "betangel_backtest_input"
RESULTS_KIND = "betangel_backtest_results"
RECEIPT_KIND = "betangel_backtest_receipt"
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,40}")
CODE_IDENTITY_RE = re.compile(r"[\x20-\x7e]{1,200}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
LOCK_NAME = "runner.lock"
ISSUANCE_FILE, RESULTS_FILE, RECEIPT_FILE = "issuance-ledger.jsonl", "results.json", "receipt.json"
RECOVERY_FILE, RECOVERY_KIND = "recovery-intent.json", "betangel_backtest_recovery_intent"
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
RECORDED_AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
UNKNOWN_INPUT_SHA256 = "0" * 64
CHUNK_BYTES = 1 << 20
MAX_JSON_BYTES = 256 << 20
MAX_RECORD_BYTES = 64 << 20
FAILURE_CHARS = 200


class RunRefused(Exception):
    """A refusal; str() is "<where>:<reason>"."""


class PublicationUncertain(Exception):
    """A write that failed after its file was created: the file may hold all, part or none of its bytes."""


def utc_now_text():
    """The current UTC time as the trial ledger's recorded_at text."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _guard(path, where, prospective=False):
    try:
        return guard_path(path, where, prospective)
    except PathDenied as error:
        raise RunRefused(str(error)) from None


def _present(path, where):
    """Whether anything (a dangling symlink included) is at path: only FileNotFoundError from lstat means absent;
    any other inspection error refuses (uninspectable), never read as absence."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise RunRefused(f"{where}:uninspectable") from None
    return True


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _failure(error):
    return f"{type(error).__name__}:{error}"[:FAILURE_CHARS]


def _read_capped(path, where, cap=MAX_JSON_BYTES):
    try:
        with path.open("rb") as handle:
            data = handle.read(cap + 1)
    except OSError:
        raise RunRefused(f"{where}:unreadable") from None
    if len(data) > cap:
        raise RunRefused(f"{where}:too_large")
    return data


def _canonical(value):
    """canonical_json(value) + newline, or None when the value has no canonical form (a lone surrogate, say)."""
    try:
        return canonical_json(value) + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        return None


def _directory(path, where):
    """path as an existing plain directory (created when missing, after guarding it)."""
    try:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RunRefused(f"{where}:not_a_directory")
        if not path.exists():
            _guard(path, where, prospective=True)
            path.mkdir()
    except OSError:
        raise RunRefused(f"{where}:unwritable") from None
    return _guard(path, where)


def _load_contract(path):
    try:
        contract = load_contract(path)
    except ContractFileError as error:
        raise RunRefused(f"contract:{error}") from None
    problems, digest = validate_contract(contract)
    if result_status(contract, problems) != "valid_frozen":
        raise RunRefused("contract:not_valid_frozen")
    try:
        rules = CohortRules.from_contract(contract)
        model = (rules, AccountingRules.from_contract(contract), BootstrapPlan.from_contract(contract))
    except ValueError as error:
        raise RunRefused(f"contract:not_supported:{error}") from None
    if rules.delay_s != 0:
        raise RunRefused("contract:not_supported:delay_s_other_than_0")
    return contract, digest, model


def _lab_root(lab_state_root):
    lab, problems = check_data_root(lab_state_root)
    if problems:
        raise RunRefused(problems[0].replace("data_root:", "lab_state_root:", 1))
    return lab


def _roots(data_root, lab_state_root):
    root, problems = check_data_root(data_root)
    if problems:
        raise RunRefused(problems[0])
    lab = _lab_root(lab_state_root)
    if root == lab or root in lab.parents or lab in root.parents:
        raise RunRefused("roots:not_distinct")
    return root, lab


def _identifiers(run_id, code_identity):
    if run_id is not None and not (isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id)):
        raise RunRefused("run_id:malformed")
    if not (isinstance(code_identity, str) and CODE_IDENTITY_RE.fullmatch(code_identity)):
        raise RunRefused("code_identity:malformed")


def _markets_digest(path):
    """(sha256, line count) of one markets file, read in chunks."""
    digest, lines = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            lines += chunk.count(b"\n")
    return digest.hexdigest(), lines


def _head_ok(head, sha, manifest_sha):
    refused = head.get("refused_markets")
    return ((head.get("kind"), head.get("schema_version"), head.get("archive_sha256"), head.get("manifest_sha256"))
            == (HEAD_KIND, INDEX_SCHEMA_VERSION, sha, manifest_sha)
            and isinstance(head.get("markets_sha256"), str) and type(head.get("markets")) is int
            and isinstance(refused, list)
            and all(isinstance(item, dict) and isinstance(item.get("market_id"), str)
                    and isinstance(item.get("reason"), str) for item in refused)
            and isinstance(head.get("files"), list)
            and all(isinstance(item, dict) and isinstance(item.get("status"), str)
                    and isinstance(item.get("reason"), (str, type(None))) for item in head["files"]))


def _inputs(root):
    """([archive input dict], descriptor, input_sha256) after the checks in the module docstring."""
    manifests = root / "manifests"
    if manifests.exists() or manifests.is_symlink():
        _guard(manifests, "manifests")
    try:
        shas = archive_shas(root)
    except IndexProblem as error:
        raise RunRefused(str(error)) from None
    if not shas:
        raise RunRefused("inputs:no_archive")
    archives, entries = [], []
    for sha in shas:
        where = f"input:{sha}"
        try:
            _guard(safe_path(root, "manifests", f"{sha}.json"), where)
            manifest, manifest_sha = load_manifest(root, sha)
            head_path = safe_path(root, "index", sha, HEAD_FILE)
            markets_path = safe_path(root, "index", sha, MARKETS_FILE)
        except (IndexProblem, ArchiveProblem) as error:
            raise RunRefused(f"{where}:{error}") from None
        if not (head_path.exists() or head_path.is_symlink()):
            raise RunRefused(f"{where}:not_indexed")
        data = _read_capped(_guard(head_path, where), f"{where}:head")
        markets_path = _guard(markets_path, where)
        try:
            head = loads_strict(data)
        except (ValueError, RecursionError):
            raise RunRefused(f"{where}:head_unreadable") from None
        if not isinstance(head, dict) or _canonical(head) != data:
            raise RunRefused(f"{where}:head_not_canonical")
        if not _head_ok(head, sha, manifest_sha):
            raise RunRefused(f"{where}:head_mismatch")
        try:
            markets_sha, count = _markets_digest(markets_path)
        except OSError:
            raise RunRefused(f"{where}:markets_unreadable") from None
        if (markets_sha, count) != (head["markets_sha256"], head["markets"]):
            raise RunRefused(f"{where}:markets_mismatch")
        archives.append({"archive_sha256": sha, "manifest": manifest, "head": head, "markets_path": markets_path,
                         "markets_sha256": markets_sha})
        entries.append({"archive_sha256": sha, "manifest_sha256": manifest_sha, "index_sha256": _sha256_bytes(data),
                        "markets_sha256": markets_sha, "markets": count,
                        "manifest_gate_current": manifest.get("month_gate") == gate_snapshot()})
    descriptor = {"kind": INPUT_KIND, "version": RUNNER_SCHEMA_VERSION, "archives": entries}
    return archives, descriptor, _sha256_bytes(canonical_json(descriptor))


def _records(archive):
    """Every record of one archive's markets file, in file order (lines at most MAX_RECORD_BYTES); the file is
    rehashed while read and must still be the bytes its head names (RunRefused otherwise)."""
    where = f"input:{archive['archive_sha256']}"
    digest = hashlib.sha256()
    try:
        with open(archive["markets_path"], "rb") as handle:
            while True:
                line = handle.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_RECORD_BYTES:
                    raise RunRefused(f"{where}:record_too_large")
                digest.update(line)
                yield loads_strict(line)
    except (OSError, ValueError, RecursionError):
        raise RunRefused(f"{where}:markets_unreadable") from None
    if digest.hexdigest() != archive["markets_sha256"]:
        raise RunRefused(f"{where}:markets_changed")


def _record_months(record, sha):
    """Every month the record's market touches (its path month and every marketTime month it saw, as the audit reads
    them), or None when the record's shape is not the index's."""
    provenance = record.get("provenance") if isinstance(record, dict) else None
    outcome = record.get("evaluator_only_outcome") if isinstance(record, dict) else None
    checks = record.get("checks") if isinstance(record, dict) else None
    if not (isinstance(record.get("market_id") if isinstance(record, dict) else None, str)
            and isinstance(provenance, dict) and provenance.get("archive_sha256") == sha
            and isinstance(provenance.get("plan"), str) and isinstance(provenance.get("member_path"), str)
            and isinstance(record.get("decision_time"), dict) and isinstance(outcome, dict)
            and isinstance(checks, dict) and isinstance(outcome.get("market_time_months"), list)):
        return None
    months = [checks.get("path_month"), *outcome["market_time_months"]]
    if not all(isinstance(month, str) and MONTH_RE.fullmatch(month) for month in months):
        return None
    return months


def _first_pass(archives):
    """The record count, after checking every record's shape and refusing a duplicate (plan, market id)."""
    seen, count = set(), 0
    for archive in archives:
        for record in _records(archive):
            months = _record_months(record, archive["archive_sha256"])
            if months is None:
                raise RunRefused(f"input:{archive['archive_sha256']}:malformed_record")
            if any(month_gate(month) for month in months):             # the current gate, whatever the index's
                raise RunRefused(f"input:{archive['archive_sha256']}:stale_gate:{record['market_id']}")
            key = (record["provenance"]["plan"], record["market_id"])
            if key in seen:
                raise RunRefused(f"cohort:{record['market_id']}:duplicate_market")
            seen.add(key)
            count += 1
    return count


def _source_refusals(archives):
    """Refusals at three separate levels, never added together: members the ingest refused (the manifest's refused
    list: their markets are unknown), index files not read whole or refused (the head's files), and the index's
    refused market rows (raw rows and distinct market ids per reason)."""
    members, files, rows, markets = {}, {}, {}, {}
    for archive in archives:
        for item in archive["manifest"].get("refused", []):
            members[item["reason"]] = members.get(item["reason"], 0) + 1
        for item in archive["head"]["files"]:
            if item["status"] != "indexed":
                key = f"{item['status']}:{item['reason']}"
                files[key] = files.get(key, 0) + 1
        for item in archive["head"]["refused_markets"]:
            rows[item["reason"]] = rows.get(item["reason"], 0) + 1
            markets.setdefault(item["reason"], set()).add(item["market_id"])
    return {"members_refused_at_ingest_by_reason": dict(sorted(members.items())),
            "index_files_not_indexed_by_status_reason": dict(sorted(files.items())),
            "market_rows_by_reason": dict(sorted(rows.items())),
            "distinct_markets_by_reason": {reason: len(ids) for reason, ids in sorted(markets.items())}}


class _Lock:
    def __init__(self, path, note):
        self.path, self.note, self.held = path, note, False

    def acquire(self):
        try:
            guard_path(self.path, "runner_lock", prospective=True)
        except PathDenied as error:
            reason = str(error).rsplit(":", 1)[1]
            raise RunRefused("runner_lock:held" if reason == "already_exists" else str(error)) from None
        try:
            handle = self.path.open("x", encoding="ascii")
        except FileExistsError:
            raise RunRefused("runner_lock:held") from None
        except OSError:
            raise RunRefused("runner_lock:unwritable") from None
        self.held = True                          # the file alone is the lock; its note only helps a person
        try:
            with handle:
                handle.write(self.note)
        except OSError:
            pass

    def release(self):
        """True when released (or never held); False when the lock file could not be removed."""
        if not self.held:
            return True
        try:
            self.path.unlink()
        except OSError:
            return False
        self.held = False
        return True


def _anchor_shape(value):
    return (isinstance(value, list) and len(value) == 2 and type(value[0]) is int and value[0] >= 0
            and isinstance(value[1], str) and SHA256_RE.fullmatch(value[1]) is not None)


def _status_of(kinds):
    """The receipt status the close kinds of one run give."""
    return "aborted" if "aborted" in kinds else "failed" if "failed" in kinds else "completed"


def _run_dirs(runs_dir):
    """{run id: directory} of every entry under runs_dir whose name is a run id (others are ignored); each must be a
    plain directory there (inspected with lstat: an entry that cannot be inspected refuses), its resolved form
    directly under runs_dir."""
    found = {}
    try:
        names = sorted(path.name for path in runs_dir.iterdir())
    except OSError:
        raise RunRefused("runs_dir:unreadable") from None
    for name in names:
        if not RUN_ID_RE.fullmatch(name):
            continue
        path = runs_dir / name
        _present(path, f"run:{name}")
        if path.is_symlink() or not path.is_dir() or _guard(path, f"run:{name}").parent != runs_dir:
            raise RunRefused(f"run:{name}:not_a_run_directory")
        found[name] = path
    return found


def _read_json(path, where, check):
    """The canonical JSON object at path that passes check, read through the guard; RunRefused otherwise."""
    data = _read_capped(_guard(path, where), where)
    try:
        value = loads_strict(data)
    except (ValueError, RecursionError):
        raise RunRefused(f"{where}:unreadable") from None
    if not (isinstance(value, dict) and _canonical(value) == data and check(value)):
        raise RunRefused(f"{where}:malformed")
    return value


def _receipt_shape(receipt, digest, run_id):
    trials = receipt.get("trials")
    return ((receipt.get("kind"), receipt.get("schema_version"), receipt.get("contract_sha256"), receipt.get("run_id"))
            == (RECEIPT_KIND, RUNNER_SCHEMA_VERSION, digest, run_id)
            and receipt.get("status") in ("aborted", "completed", "failed")
            and _anchor_shape(receipt.get("ledger_head"))
            and type(receipt.get("cumulative_starts")) is int and receipt["cumulative_starts"] >= 0
            and isinstance(receipt.get("recorded_at"), str) and RECORDED_AT_RE.fullmatch(receipt["recorded_at"])
            and (receipt.get("failure") is None or (isinstance(receipt["failure"], str)
                                                    and len(receipt["failure"]) <= FAILURE_CHARS))
            and (receipt.get("reconciled_by") is None or (isinstance(receipt["reconciled_by"], str)
                                                          and CODE_IDENTITY_RE.fullmatch(receipt["reconciled_by"])))
            and all(receipt.get(key) is None or (isinstance(receipt[key], str) and SHA256_RE.fullmatch(receipt[key]))
                    for key in ("results_sha256", "issuance_ledger_sha256"))
            and isinstance(trials, list) and bool(trials)
            and all(isinstance(t, dict) and isinstance(t.get("variant"), str)
                    and t.get("trial_id") == f"{run_id}.{t.get('variant')}"
                    and _anchor_shape(t.get("started")) and _anchor_shape(t.get("closed"))
                    and (t.get("issued") is None or (type(t["issued"]) is int and t["issued"] >= 0))
                    for t in trials))


def _intent_shape(intent, digest, run_id):
    variants = intent.get("variants")
    return ((intent.get("kind"), intent.get("schema_version"), intent.get("contract_sha256"), intent.get("run_id"))
            == (RECOVERY_KIND, RUNNER_SCHEMA_VERSION, digest, run_id)
            and isinstance(variants, list) and bool(variants) and variants == sorted(set(variants))
            and all(isinstance(v, str) for v in variants)
            and isinstance(intent.get("recorded_by"), str) and CODE_IDENTITY_RE.fullmatch(intent["recorded_by"]))


def _file_sha(path, where):
    try:
        with _guard(path, where).open("rb") as handle:
            digest = hashlib.sha256()
            while True:
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
    except OSError:
        raise RunRefused(f"{where}:unreadable") from None


class _History:
    """The contract's ledger, opened at the highest receipt anchor, and every run directory checked against it (the
    module docstring). Nothing here writes."""

    def __init__(self, contract_dir, runs_dir, digest, variants):
        self.runs = _run_dirs(runs_dir)
        self.receipts, self.intents = {}, {}
        for run_id, run in self.runs.items():
            if _present(run / RECEIPT_FILE, f"receipt:{run_id}"):
                self.receipts[run_id] = _read_json(run / RECEIPT_FILE, f"receipt:{run_id}",
                                                   lambda value, r=run_id: _receipt_shape(value, digest, r))
            if _present(run / RECOVERY_FILE, f"recovery:{run_id}"):
                self.intents[run_id] = _read_json(run / RECOVERY_FILE, f"recovery:{run_id}",
                                                  lambda value, r=run_id: _intent_shape(value, digest, r)
                                                  and set(value["variants"]) <= set(variants))
        heads = [receipt["ledger_head"] for receipt in self.receipts.values()]
        anchor = max(heads, key=lambda head: head[0]) if heads else None
        try:
            self.ledger = TrialLedger.open(contract_dir, anchor=anchor)
            entries = self.ledger.entries()
        except LedgerError as error:
            raise RunRefused(f"ledger:{error}") from None
        self.every = {entry["trial_id"] for entry in entries}
        mine = [entry for entry in entries if entry["contract_sha256"] == digest]
        self.started = [entry for entry in mine if entry["kind"] == "started"]
        self.closed = {entry["trial_id"]: entry for entry in mine if entry["kind"] != "started"}
        self.trials_by_run = {}
        for entry in self.started:
            self.trials_by_run.setdefault(entry["trial_id"].rsplit(".", 1)[0], []).append(entry)
        self.unfinished = sorted(entry["trial_id"] for entry in self.started if entry["trial_id"] not in self.closed)
        self.incomplete_recovery = sorted(
            run for run, intent in self.intents.items()
            if {entry["variant"] for entry in self.trials_by_run.get(run, []) if entry["trial_id"] in self.closed}
            != set(intent["variants"]))
        by_seq = {entry["seq"]: entry for entry in entries}
        for run_id, receipt in self.receipts.items():
            self._check(run_id, receipt, self.runs[run_id], by_seq, variants)
        self.missing = sorted(run for run, trials in self.trials_by_run.items()
                              if run not in self.receipts and run not in self.incomplete_recovery
                              and all(t["trial_id"] in self.closed for t in trials))
        self.unaccounted = sorted(run for run in self.runs if run not in self.receipts and run not in self.intents
                                  and run not in self.trials_by_run)

    def starts_until(self, seq):
        return sum(1 for entry in self.started if entry["seq"] <= seq)

    def _check(self, run_id, receipt, run, by_seq, variants):
        def entry_at(anchor):
            entry = by_seq.get(anchor[0])
            return entry if entry is not None and entry["entry_sha256"] == anchor[1] else None

        if entry_at(receipt["ledger_head"]) is None:
            raise RunRefused(f"receipt:{run_id}:head_not_in_ledger")
        ledger_trials = sorted(entry["trial_id"] for entry in self.trials_by_run.get(run_id, []))
        listed = sorted(trial["trial_id"] for trial in receipt["trials"])
        named = [trial["variant"] for trial in receipt["trials"]]
        expected = self.intents[run_id]["variants"] if run_id in self.intents else None
        if (listed != ledger_trials or len(set(named)) != len(named) or not set(named) <= set(variants)
                or (expected is not None and sorted(named) != expected)):
            raise RunRefused(f"receipt:{run_id}:trials_not_the_ledgers")
        closes = []
        for trial in receipt["trials"]:
            start, close = entry_at(trial["started"]), entry_at(trial["closed"])
            if (start is None or close is None or start["kind"] != "started" or close["kind"] == "started"
                    or start["trial_id"] != trial["trial_id"] or close["trial_id"] != trial["trial_id"]
                    or self.closed.get(trial["trial_id"]) is not close):
                raise RunRefused(f"receipt:{run_id}:trial_not_in_ledger")
            closes.append(close)
        if receipt["ledger_head"][0] < max(close["seq"] for close in closes):
            raise RunRefused(f"receipt:{run_id}:head_before_its_closes")
        if receipt["cumulative_starts"] != self.starts_until(receipt["ledger_head"][0]):
            raise RunRefused(f"receipt:{run_id}:cumulative_starts_not_the_ledgers")
        status = _status_of({close["kind"] for close in closes})
        if receipt["status"] != status:
            raise RunRefused(f"receipt:{run_id}:status_not_the_ledgers")
        if status != "completed":
            if receipt.get("results_sha256") is not None:
                raise RunRefused(f"receipt:{run_id}:results_without_completion")
            return
        for name, key in ((RESULTS_FILE, "results_sha256"), (ISSUANCE_FILE, "issuance_ledger_sha256")):
            digest = _file_sha(run / name, f"{name}:{run_id}")
            if digest != receipt.get(key) or any(close["summary"].get(key) != digest for close in closes):
                raise RunRefused(f"receipt:{run_id}:{key}_mismatch")


def _write_new(path, chunks, where):
    """Write chunks to a new file (exclusive create, after guarding it); the sha256 of what was written. RunRefused
    when the file could not be created (nothing written); PublicationUncertain when anything failed after it was."""
    _guard(path, where, prospective=True)
    try:
        handle = path.open("xb")
    except OSError:
        raise RunRefused(f"{where}:not_created") from None
    digest = hashlib.sha256()
    try:
        with handle:
            for chunk in chunks:
                digest.update(chunk)
                handle.write(chunk)
    except Exception as error:
        raise PublicationUncertain(f"{where}:{_failure(error)}") from None
    return digest.hexdigest()


def _paths(lab, digest):
    contract_dir = _directory(_directory(lab / "contracts", "contracts") / digest, "contract_dir")
    runs_dir = _directory(_directory(lab / "runs", "runs") / digest, "runs_dir")
    ledger = contract_dir / LEDGER_FILE
    _guard(ledger, "ledger", prospective=not _present(ledger, "ledger"))
    return contract_dir, runs_dir


def _evaluate(archives, root, rules, accounting_rules, bootstrap_plan, limits):
    """(decisions, results settlement part) of the run: decide, settle, metrics and bootstrap."""
    manifests = {archive["archive_sha256"]: archive["manifest"] for archive in archives}

    def replay(record):
        sha = record["provenance"]["archive_sha256"]
        try:
            raw = safe_target(root, sha, record["provenance"]["member_path"])
        except ArchiveProblem as error:
            raise ReplayProblem(f"replay:{record['market_id']}:{error}") from None
        _guard(raw, f"raw:{record['market_id']}")
        return replay_view(root, manifests[sha], record, rules.offset_s, limits)

    decisions, bets, outcomes = [], [], []
    for archive in archives:
        for record in _records(archive):
            decision = decide_market(rules, record, replay)
            decisions.append(decision)
            market_bets = issued_bets(decision)
            if market_bets:
                bets.extend(market_bets)
                outcomes.append(market_outcome(record, decision))
    settlement = settle(bets, outcomes, accounting_rules)
    unresolved = {}
    for bet in settlement.bets:
        if bet.status == "unresolved":
            reasons = unresolved.setdefault(bet.variant, {})
            reasons[bet.reason] = reasons.get(bet.reason, 0) + 1
    metrics = {item.variant: as_record(item) for item in metrics_by_variant(settlement, rules.variants)}
    uncertainty = {variant: as_record(block_bootstrap(settlement, variant, bootstrap_plan))
                   for variant in rules.variants}
    return decisions, {"by_variant": metrics,
                       "unresolved_reasons": {v: dict(sorted(r.items())) for v, r in sorted(unresolved.items())},
                       "uncertainty": uncertainty}


def _append(ledger, kind, trial_id, variant, contract, input_sha256, code_identity, recorded_at, summary):
    return ledger.append(kind, trial_id=trial_id, contract=contract, variant=variant, input_sha256=input_sha256,
                         code_identity=code_identity, recorded_at=recorded_at, summary=summary)


def _receipt(digest, run_id, status, failure, results_sha, issuance_sha, trials, head, starts, recorded_at,
             reconciled_by):
    return {"kind": RECEIPT_KIND, "schema_version": RUNNER_SCHEMA_VERSION, "contract_sha256": digest,
            "run_id": run_id, "status": status, "failure": failure, "results_sha256": results_sha,
            "issuance_ledger_sha256": issuance_sha,
            "trials": [{"variant": variant, "trial_id": trial_id, "started": list(start), "closed": list(close),
                        "issued": issued} for variant, trial_id, start, close, issued in trials],
            "ledger_head": list(head), "cumulative_starts": starts, "recorded_at": recorded_at,
            "reconciled_by": reconciled_by}


def _uncertain(problem, lock, trial_ids=(), **extra):
    return {"status": "uncertain", "problems": [problem], "runner_lock": str(lock.path),
            "trial_ids": list(trial_ids), **extra}


def run_backtest(contract_path, data_root, lab_state_root, run_id, code_identity, clock=utc_now_text,
                 limits=DEFAULT_LIMITS):
    """The JSON-ready result of one run (module docstring)."""
    try:
        for where, path in (("contract", contract_path), ("data_root", data_root),
                            ("lab_state_root", lab_state_root)):
            _guard(path, where)
        contract, digest, (rules, accounting_rules, bootstrap_plan) = _load_contract(_guard(contract_path, "contract"))
        root, lab = _roots(data_root, lab_state_root)
        _identifiers(run_id, code_identity)
        archives, descriptor, input_sha256 = _inputs(root)
        records = _first_pass(archives)
        contract_dir, runs_dir = _paths(lab, digest)
        lock = _Lock(contract_dir / LOCK_NAME, f"run {run_id} {code_identity}\n")
        lock.acquire()
    except RunRefused as error:
        return {"status": "refused", "problems": [str(error)]}
    except Exception as error:                                 # unexpected, before any lock: refused, named
        return {"status": "refused", "problems": [f"preflight:{_failure(error)}"]}
    trials = [(variant, f"{run_id}.{variant}") for variant in rules.variants]
    try:
        history = _History(contract_dir, runs_dir, digest, rules.variants)
        if history.unfinished:
            raise RunRefused("ledger:unfinished_trials:" + ",".join(history.unfinished))
        if history.incomplete_recovery:
            raise RunRefused("run:recovery_incomplete:" + ",".join(history.incomplete_recovery))
        if history.missing:
            raise RunRefused("receipt:missing:" + ",".join(history.missing))
        if history.unaccounted:
            raise RunRefused("run:unaccounted:" + ",".join(history.unaccounted))
        if any(trial_id in history.every for _, trial_id in trials) or _present(runs_dir / run_id, "run_dir"):
            raise RunRefused("run_id:already_used")
        if len(history.started) + len(trials) > contract["stopping_rule"]["max_trials"]:
            raise RunRefused("ledger:trial_budget_exhausted")
        run_dir = _guard(runs_dir / run_id, "run_dir", prospective=True)
        run_dir.mkdir()
    except Exception as error:
        problem = str(error) if isinstance(error, RunRefused) else f"preflight:{_failure(error)}"
        if not lock.release():
            return _uncertain(problem, lock, (), lock_release="failed")
        return {"status": "refused", "problems": [problem]}
    return _run(contract, digest, rules, accounting_rules, bootstrap_plan, archives, descriptor, input_sha256, records,
                root, history, trials, run_id, run_dir, code_identity, clock, limits, lock)


def _run(contract, digest, rules, accounting_rules, bootstrap_plan, archives, descriptor, input_sha256, records,
         root, history, trials, run_id, run_dir, code_identity, clock, limits, lock):
    ledger, ids = history.ledger, [trial_id for _, trial_id in trials]
    started = {}
    for index, (variant, trial_id) in enumerate(trials):
        try:
            started[variant] = _append(ledger, "started", trial_id, variant, contract, input_sha256, code_identity,
                                       clock(), {})
        except LedgerError as error:
            if index == 0 and not isinstance(error, (WriteUncertainError, UnlockError)):
                try:
                    run_dir.rmdir()                     # still empty: nothing was started or written
                except OSError:
                    return _uncertain(f"ledger:{error}", lock, ids)
                if not lock.release():
                    return _uncertain(f"ledger:{error}", lock, ids, lock_release="failed")
                return {"status": "refused", "problems": [f"ledger:{error}"]}
            return _uncertain(f"ledger:{error}", lock, ids, started=sorted(started))
    results_sha, issuance_sha, failure, issued = None, None, None, {variant: 0 for variant, _ in trials}
    try:
        decisions, settlement_part = _evaluate(archives, root, rules, accounting_rules, bootstrap_plan, limits)
        lines = ledger_lines(decisions)
        issuance_sha = _write_new(run_dir / ISSUANCE_FILE, iter(lines), "issuance_ledger")
        counts = issuance_counts(rules, decisions)
        issued = {variant: counts[variant]["issued"] for variant, _ in trials}
        results = {"kind": RESULTS_KIND, "schema_version": RUNNER_SCHEMA_VERSION, "contract_sha256": digest,
                   "run_id": run_id, "code_identity": code_identity, "input": descriptor, "input_sha256": input_sha256,
                   "population": {"records": records, **population_counts(decisions),
                                  "source_refusals": _source_refusals(archives)},
                   "issuance": {"ledger_sha256": issuance_sha, "rows": len(lines), "by_variant": counts},
                   "settlement": settlement_part,
                   "engineering_acceptance": {"status": "not_evaluated",
                                              "reason": "the in-run engineering checks are task c2b3"}}
        results_sha = _write_new(run_dir / RESULTS_FILE, [canonical_json(results) + b"\n"], "results")
    except Exception as error:                                  # anything after the starts closes them as failed
        failure, results_sha = _failure(error), None
    closed = {}
    summary = ({"results_sha256": results_sha, "issuance_ledger_sha256": issuance_sha} if failure is None
               else {"failure": failure})
    for variant, trial_id in trials:
        try:
            closed[variant] = _append(ledger, "completed" if failure is None else "failed", trial_id, variant,
                                      contract, input_sha256, code_identity, clock(), summary)
        except LedgerError as error:
            return _uncertain(f"ledger:{error}", lock, ids, closed=sorted(closed), failure=failure)
    status = "completed" if failure is None else "failed"
    receipt = _receipt(digest, run_id, status, failure, results_sha, issuance_sha if failure is None else None,
                       [(variant, trial_id, started[variant], closed[variant], issued[variant])
                        for variant, trial_id in trials],
                       ledger.head, len(history.started) + len(trials), clock(), None)
    try:
        receipt_sha = _write_new(run_dir / RECEIPT_FILE, [canonical_json(receipt) + b"\n"], "receipt")
    except Exception as error:
        return _uncertain(f"receipt:{_failure(error)}", lock, ids, receipt=receipt)
    if not lock.release():
        return _uncertain("runner_lock:release_failed", lock, ids, receipt_sha256=receipt_sha)
    return {"status": status, "problems": [] if failure is None else [failure], "run_dir": str(run_dir),
            "results_sha256": results_sha, "receipt_sha256": receipt_sha, "ledger_head": receipt["ledger_head"],
            "cumulative_starts": receipt["cumulative_starts"]}


def _publish(path, value, where, run_id, touched):
    """Write one canonical JSON file for reconcile; run_id joins touched once the file exists (a refusal before it
    was created leaves nothing behind and touched unchanged)."""
    try:
        _write_new(path, [canonical_json(value) + b"\n"], where)
    except PublicationUncertain:
        touched.append(run_id)
        raise
    touched.append(run_id)


def reconcile(contract_path, lab_state_root, code_identity, clock=utc_now_text):
    """The explicit operator action in the module docstring. A JSON-ready result: reconciled (the trials aborted, the
    runs recorded, the receipts published), refused (nothing written anywhere; the lock released), or uncertain
    (something was written, or may have been, before a failure: the lock is kept and every trial id appended and
    every run touched named)."""
    try:
        _guard(contract_path, "contract")
        _guard(lab_state_root, "lab_state_root")
        contract, digest, (rules, _, _) = _load_contract(_guard(contract_path, "contract"))
        lab = _lab_root(lab_state_root)
        _identifiers(None, code_identity)
        contract_dir, runs_dir = _paths(lab, digest)
        lock = _Lock(contract_dir / LOCK_NAME, f"reconcile {code_identity}\n")
        lock.acquire()
    except RunRefused as error:
        return {"status": "refused", "problems": [str(error)]}
    except Exception as error:
        return {"status": "refused", "problems": [f"preflight:{_failure(error)}"]}
    appended, touched = [], []
    try:
        history = _History(contract_dir, runs_dir, digest, rules.variants)
        aborted, recorded = list(history.unfinished), list(history.unaccounted)
        for entry in history.started:
            if entry["trial_id"] in aborted:
                _append(history.ledger, "aborted", entry["trial_id"], entry["variant"], contract,
                        entry["input_sha256"], entry["code_identity"], clock(),
                        {"reason": "reconciled_unfinished", "reconciled_by": code_identity, "pending": aborted})
                appended.append(entry["trial_id"])
        for run_id in recorded:                      # the intent first, so an interruption cannot shrink the record
            intent = {"kind": RECOVERY_KIND, "schema_version": RUNNER_SCHEMA_VERSION, "contract_sha256": digest,
                      "run_id": run_id, "variants": sorted(rules.variants), "reason": "unaccounted_run",
                      "recorded_by": code_identity}
            _publish(history.runs[run_id] / RECOVERY_FILE, intent, f"recovery:{run_id}", run_id, touched)
        history = _History(contract_dir, runs_dir, digest, rules.variants)
        for run_id in history.incomplete_recovery:
            have = {entry["variant"] for entry in history.trials_by_run.get(run_id, [])}
            for variant in history.intents[run_id]["variants"]:
                if variant in have:
                    continue
                trial_id = f"{run_id}.{variant}"
                for kind, summary in (("started", {}), ("aborted", {"reason": "reconciled_unaccounted_run",
                                                                    "reconciled_by": code_identity})):
                    _append(history.ledger, kind, trial_id, variant, contract, UNKNOWN_INPUT_SHA256, code_identity,
                            clock(), summary)
                    appended.append(f"{trial_id}:{kind}")
        history = _History(contract_dir, runs_dir, digest, rules.variants)
        if history.unfinished or history.incomplete_recovery:
            raise RunRefused("reconcile:still_pending:" + ",".join(history.unfinished + history.incomplete_recovery))
        receipted = []
        for run_id in history.missing:
            run_dir = history.runs.get(run_id)
            if run_dir is None:
                raise RunRefused(f"receipt:{run_id}:no_run_directory")
            trials = sorted(history.trials_by_run[run_id], key=lambda entry: entry["variant"])
            closes = [history.closed[entry["trial_id"]] for entry in trials]
            status = _status_of({close["kind"] for close in closes})
            results_sha = issuance_sha = None
            if status == "completed":
                results_sha = _file_sha(run_dir / RESULTS_FILE, f"results:{run_id}")
                issuance_sha = _file_sha(run_dir / ISSUANCE_FILE, f"issuance:{run_id}")
                if any((close["summary"].get("results_sha256"), close["summary"].get("issuance_ledger_sha256"))
                       != (results_sha, issuance_sha) for close in closes):
                    raise RunRefused(f"receipt:{run_id}:results_mismatch")
            head = history.ledger.head
            anchors = [(entry["variant"], entry["trial_id"], (entry["seq"], entry["entry_sha256"]),
                        (close["seq"], close["entry_sha256"]), None) for entry, close in zip(trials, closes)]
            receipt = _receipt(digest, run_id, status, None if status == "completed" else "reconciled", results_sha,
                               issuance_sha, anchors, head, history.starts_until(head[0]), clock(), code_identity)
            _publish(run_dir / RECEIPT_FILE, receipt, f"receipt:{run_id}", run_id, touched)
            receipted.append(run_id)
    except Exception as error:
        problem = str(error) if isinstance(error, RunRefused) else _failure(error)
        wrote = appended or touched or isinstance(error, (WriteUncertainError, UnlockError, PublicationUncertain))
        if not wrote and lock.release():
            return {"status": "refused", "problems": [problem]}
        return _uncertain(problem, lock, appended, runs_touched=touched)
    if not lock.release():
        return _uncertain("runner_lock:release_failed", lock, appended, runs_touched=touched)
    return {"status": "reconciled", "aborted": aborted, "recorded_runs": recorded, "receipted": receipted,
            "ledger_head": None if history.ledger.head is None else list(history.ledger.head)}
