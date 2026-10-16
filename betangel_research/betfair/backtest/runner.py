"""Run one frozen P0 contract over ingested and indexed archives: the runner core (tasks c2b2, c2b3b1 and c2b3b3).

run_backtest(contract, data_root, lab_state_root, run_id, code_identity) always returns one JSON-ready result; its
status is refused (no trial started; the lock released), completed, failed (both trials started and closed as
failed, a receipt written) or uncertain (the ledger may hold more than the result can show, or a lock could not be
released: the runner lock is kept and the trial ids named, for a person to reconcile). It never places an order,
logs in or reads an account; it reads only the explicit files named below, each after path_guard has cleared it,
and the package's own sources, which certificate.current_key lists and hashes at preflight, before the starts and at
finalise (found from this module's path, symlinks refused, not path_guard'd: they are the code being run).

Preflight, before any trial starts (Astra's design agreement D4, D12, D14 and her runner clarifications):
1. guard_path the contract file, the data root and the lab-state root, before anything else touches them.
2. The contract must be valid_frozen and the supported P0 model: CohortRules (exactly the two P0 variants),
   AccountingRules (N1: the commission rate is checked before any trial is consumed), BootstrapPlan, delay_s 0; and
   inside the certified structural profile: certificate.profile_of(contract) equals certificate.P0_PROFILE by
   canonical bytes (decision 3), else contract:not_supported:not_the_certified_profile.
3. check_data_root both roots (outside the repository and every Git worktree); they must be distinct and neither may
   hold the other. run_id is one component ([a-z0-9][a-z0-9_.-]{0,40}); code_identity is printable ASCII (1 to 200
   characters, as the trial ledger requires), for instance a commit; nothing here runs git.
4. The engine certificate (decisions 4 and 5): certificate.load_certificate reads <lab>/certifications/<key>.json
   for the key of the sources on disk and this interpreter (certificate.current_key; there is no path override) and
   validates it whole against this protocol. Sources that cannot be described (source:<..>) and a certificate that
   is missing, unreadable, malformed, for another key or not certified (certification:<..>, certifications:<..>,
   certificate:<..>) refuse. Nothing is written: certify-engine (certify.py) makes the certificate. Then
   engineering.known_accounting_answers must pass over its exact, non-empty case set (a changed case set, an error or
   any other result refuses: engineering:known_accounting_answers_not_the_known_set, _error or _not_passed); that
   checked result is the one the run's acceptance reports.
5. The input descriptor: every archive under the data root (manifests/), each with its manifest (load_manifest:
   canonical bytes, its own sha256), its index head (canonical bytes at most MAX_JSON_BYTES, the index kind and
   schema, naming this archive and this manifest, its refused_markets well formed) and its markets file (the head's
   sha256 and record count). input_sha256 is the sha256 of the descriptor's canonical JSON ({kind, version, archives
   sorted by archive_sha256, each with archive, manifest, index and markets sha256 and the record count}). Every
   derived path is guarded before it is opened: manifests, heads, markets files, and each raw member before its
   replay or prefix check. Every manifest's limits must be the run's read limits (input:<sha>:limits_not_the_runs):
   the run replays and checks prefixes under the limits the members were ingested with, and the certificate's limits
   must be the run's read limits and the 64 MiB prefix limit (certification:limits_not_the_runs; decision 13). The
   index head does not record the limits it was built under, so this proves ingest and run agree, not how the index
   was built.
6. A first pass over every markets file (lines at most MAX_RECORD_BYTES) checks the shape of each record the run
   reads (market id, provenance naming this archive with a plan and a member path, decision and outcome objects, its
   path month and every marketTime month it saw) and refuses a record any of whose months the CURRENT month gate
   refuses (stale_gate: an index built under an older gate never lets the run read sealed, exposed or later data;
   the manifest's gate snapshot is only reported, as manifest_gate_current) and two records of one (plan, market id)
   across archives, before anything is decided. The same pass classifies every record under the contract's own
   population rules (cohort.classify: tier, evaluated split, anchor and as-of eligibility; no replay, no runner
   draw, no outcome; a record those rules cannot read refuses as malformed_record) and, only once the whole pass has
   verified every record and file, refuses a corpus with no record (inputs:no_records) or with no eligible record
   (inputs:no_eligible_records), before the lock or any write. The count only gates the start: every record still
   goes to the run (excluded, unclassifiable and no-view records are decided and reported as before), and one
   eligible record is enough to start the trials, even if every decision then abstains or is unsupported, or every
   outcome stays unresolved; such a run is charged.
7. The per-contract runner lock, <lab>/contracts/<contract sha256>/runner.lock, made by exclusive create: one runner
   attempt (or reconcile) per contract at a time; a lock left behind refuses until a person removes it, never taken
   by force.
8. Under the lock, the trial ledger, <lab>/contracts/<contract sha256>/trial-ledger-v1.jsonl (one per contract,
   independent of run_id; Q1), is opened at the highest ledger head (by sequence) any receipt names, so a ledger
   deleted or cut behind a receipt refuses. Every run directory under <lab>/runs/<contract sha256>/ (an entry whose
   name is not a run id is ignored; a run directory must be a plain directory there, not a link elsewhere) is
   accounted for. A receipt is checked whole and refuses when it does not match (any read or shape failure included,
   never ignored): canonical bytes, kind and schema, this contract, its own run directory; its trials exactly the
   ledger's trials of that run id, at most one per contract variant (a run that crashed between its two starts has
   one), each start and close present in the ledger with the right trial id and kind; its status exactly the one the
   close kinds give (aborted if any aborted, else failed if any failed, else completed); its head at or after its
   closes; results_sha256 null unless completed, and for a completed run results.json and issuance-ledger.jsonl
   still having the digests the receipt and the closing entries name. History is checked against the certificate
   each run recorded, never against today's key or certificate file (control D: an upgrade does not invalidate it):
   every start and close of a run names one certificate sha256 (or none: a start written before task c2b3b3, or an
   unaccounted run's reconstruction) and its receipt names the same; a completed run's results.certification holds
   the whole certificate it ran under, whose canonical bytes still have the recorded sha256 and which is certified
   for the recorded key, and component (ii), each close (its binding and its top-level field) and the receipt name
   that sha256. Trials started and never closed, runs whose trials all closed without a receipt, and run directories
   with neither a receipt nor any ledger trial (a crash between creating it and the first start, or a ledger cut
   behind it) refuse until reconcile.
   Then the stop rule (decision 7, control D): a completed close in the verified ledger whose binding says passed
   stops the contract (contract:engineering_passed:<runs>). It reads the ledger, not the run's files, so deleting
   them never frees the contract; but a run with a completed receipt is checked first, so its missing evidence
   refuses earlier under its own code (results:<run>:missing_path, issuance:<run>:missing_path, or
   receipt:missing:<run> for a deleted run directory), and only a run without a completed receipt (one trial
   completed, the other aborted) reaches this code with its files gone. Without one, a surviving pass claim stops it
   too, conservatively and without becoming a certified pass (contract:engineering_pass_claim_unbound:<runs>): a
   close of another kind carrying a passed binding, or, in a run directory without a completed receipt, a
   results.json of this contract (a regular file, read whole: canonical JSON with or without its final newline, its
   kind, this contract sha256, engineering_acceptance status passed). Any other cut write, or a file that is not
   canonical JSON, claims nothing; one that is not a regular file or cannot be inspected or read refuses. Neither
   the run_id, the code identity, the sources, the certificate nor the budget is read, so none of them clears it. A
   run_id that already named trials or a run directory refuses. Capacity: the contract's started entries plus one
   per variant must not exceed stopping_rule.max_trials (D4: starts are recorded sequentially; evaluation begins
   only after both are recorded, and every recorded start counts). Last, the current key is computed again and must
   still be the certificate's (decision 12):
   otherwise source:changed_before_start, or source:changed_before_start:<reason> when the sources can no longer be
   described, and nothing starts.

The run: the run directory <lab>/runs/<contract sha256>/<run_id>/ is created (exclusive), one started entry per variant
is appended (trial id <run_id>.<variant>; its summary names the certificate sha256), then every record is decided
(cohort.decide_market, replaying the primary member when the decision needs a view) and its primary-source prefix
checked (engineering.check_prefix under the run's limits and the 64 MiB prefix limit, after the member's path is
guarded, also for an excluded or stopped record that was never replayed), each result added to one
engineering.PrefixTally over every input (plan, market id) key from the first pass; each market with an issued bet gets
its outcome (outcome.market_outcome), the bets are settled (accounting.settle), and metrics and the block bootstrap are
computed per variant (metrics). Records are streamed: only decisions, issued bets and outcomes are kept. The markets
files are rehashed while read and must still match their heads. A member that fails its integrity checks
(ReplayProblem), a denied path or a tally refusal fails the run (its starts are spent): no record is left out. Once the
acceptance criteria are computed, the current key is computed once more (decision 12, control C's order): sources
changed or no longer describable fail the run (RuntimeError:source:changed_during_run, with :<reason> when they cannot
be described), its starts spent, with no results.json and so no pass. Artifacts, each written by exclusive create while
hashed:
- issuance-ledger.jsonl: cohort.ledger_lines, the full issuance ledger. It is then read back in full through the
  guard (at most its written size plus one byte): a complete read of other bytes is compared and fails the
  reconciliation criterion; a longer file, or one that cannot be read, fails the run. Results, closes and the
  receipt name the observed bytes' sha256 and size (the writer's digest is kept beside them in results).
- results.json (schema 2): canonical JSON of the contract sha256, run_id, code identity, input descriptor and
  sha256, population counts (cohort classes and reasons, plus the index heads' source refusals as raw rows and
  distinct markets per reason), issuance counts with the ledger's observed sha256 and size, per-variant metrics,
  unresolved reasons and bootstrap results, certification ({key, sha256, certificate}: the key, the sha256 of the
  stored certificate bytes and the whole certificate the run was bound to) and engineering_acceptance:
  known_accounting_answers (the preflight result), issuance_exposure_reconciliation (the run's decisions, issued
  bets, settlement and read-back ledger against stubs for every input key) and prefix_invariance with two
  components, primary_source_prefix_consistency (the tally) and synthetic_certification ({status passed,
  certificate_sha256}: a run starts only with a certified certificate), failed when a component failed and passed
  only when both passed; its status is engineering.engineering_status over the three, under the contract's rule and
  criteria version 1. No wall-clock time is in it: the same contract, input, code and certificate give the same
  bytes but for run_id.
- Then one closing entry per trial (completed, its summary naming the results sha256, the issuance ledger's sha256
  and size, the compact acceptance binding (status, rule, criteria version, the tagged sha256 of the acceptance's
  canonical JSON and the certificate sha256) and the certificate sha256; or failed, naming a bounded failure code
  and the certificate sha256, when anything went wrong after the starts), and
- receipt.json (schema 2): status, the certificate sha256 (whatever the status), the same evidence for a completed
  run (null otherwise), each trial's start and close anchors (seq, entry_sha256), the ledger head after closing, the
  contract's started entries at that moment (cumulative_starts), and recorded_at. A later run (or reconcile) checks
  a completed run's receipt, closes, files and recomputed binding against each other. Schema 1 receipts and results
  refuse (schema_not_supported): nothing migrates them (decision 11); nor does anything migrate a schema 2 completed
  result or receipt written before task c2b3b3, whose certificate fields are null: it refuses as malformed.
The lock is released last. When the first start is refused before anything was written (a plain LedgerError), the
empty run directory is removed and the run is refused. A refused second start, a refused or uncertain close, a
WriteUncertainError or UnlockError, a receipt that cannot be written or a lock that cannot be released stop at once
with status uncertain and keep the lock: the ledger may hold more than this result shows, so a person reconciles
before anything retries (the trial ledger's Recovery rule).

reconcile(contract, lab_state_root, code_identity) is that explicit operator action, under the same lock: it appends an
aborted entry for every trial that started and never closed (the summary names the reason, who reconciled, the pending
trial ids and the certificate sha256 that run's entries named). Before any recovery append or publication, it checks
every known ledger run's start/close certificate agreement. For already closed runs without receipts, it requires a
run directory and checks completed evidence, including the results' own certificate binding and agreement with the
closes. An incomplete intent that needs new starts cannot name a known certificate: those starts would name null.
For each unaccounted run directory it first writes recovery-intent.json (schema 1: the contract's variants, sorted,
by exclusive create) and records one started and one aborted entry per variant (every persisted reservation consumes
budget; input_sha256 is UNKNOWN_INPUT_SHA256, 64 zeros, because the input it was for is unknown, and certificate_sha256
is null: unknown, never invented). A run with an intent whose variants are not all recorded is recovery_incomplete: runs
refuse, no receipt is published for it, and the next reconcile finishes it (an interruption cannot shrink the record; a
budget exhausted mid-recovery leaves it explicitly incomplete). A run that crashed between its own two starts has no
intent and keeps its one trial. An intent whose variants are not exactly the contract's, sorted, refuses every run and
reconcile before anything is written (recovery:<run>:variants_not_the_contracts; an unsorted or repeated list is
malformed). Then every missing receipt is published from the ledger (reconciled_by set; a completed run's evidence and
acceptance binding come from its files and closes, which must agree; a non-completed run's stay null; the certificate
sha256 is the one its starts and closes name). reconcile needs no certificate, runs no stop rule and is never stopped
(control D: checking history does not recertify old code). It never writes results.json or a completed close, so it can
neither erase a pass claim nor promote one: an unaccounted directory holding a claim becomes aborted reservations and
still stops the contract as an unbound claim. Statuses are honest: refused only when nothing was written (a file that
could not be created counts as nothing); once an append is confirmed or a file created, any later failure is uncertain,
with the lock kept and the appended trials and touched runs named. Nothing is deleted, no lock is taken by force and
nothing is evaluated again. Presence is judged with lstat throughout (receipts, intents, the ledger, run directories):
only FileNotFoundError means absent; any other inspection error stops recovery, never treated as absence.
Receipts, recovery intents, results and issuance evidence must be regular files at inspection, before opening:
symlinks (including dangling ones), directories and FIFOs refuse as not_a_regular_file; inspection errors refuse as
uninspectable, missing required evidence as missing_path, and failed reads as unreadable. Later failures after
recovery writes still report uncertain under the rule above.

Limits: the budget and the stop rule hold within one lab-state root and one contract sha256 (Q1): a new root, or a
contract changed in any way (max_trials included), starts its own ledger; keep one root per contract campaign.
Losing a run's ledger entries together with its receipt and results frees the contract and refunds those starts: the
whole ledger and every receipt lost, or the root restored to an earlier state (a ledger cut behind every surviving
receipt's head is a valid shorter chain, and a cut that no receipt names cannot be seen). A forged chain rehashed
whole is outside them too. While its ledger entries survive, a pass whose run directory was deleted still blocks, as
receipt:missing:<run>. The guards and checks are point-in-time: a concurrent
writer that ignores the lock, or a filesystem changed between check and use, is outside them (the lock and exclusive
creates narrow, not close, that window). The certificate attests the sources on disk at preflight, before the starts
and at finalise, and this interpreter; that the code loaded is those sources is assumed (decision 5: a fresh CLI and
a stable checkout; bytecode, native libraries and the standard library are not attested). A stray file appearing in
the package during a run (an editor's swap file, .DS_Store) fails it at finalise, its starts spent: run from an
untouched checkout.
"""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import re

from .accounting import AccountingRules, settle
from .archive import DEFAULT_LIMITS, ArchiveProblem, check_data_root, gate_snapshot, month_gate, safe_path, safe_target
from .certificate import (CERTIFICATE_KIND, CERTIFIED, P0_PROFILE, CertificateProblem, current_key, load_certificate,
                          profile_of)
from .cohort import (ANCHOR_NOT_KNOWN, ELIGIBLE, NO_VIEW, CohortRules, classify, decide_market, issuance_counts,
                     ledger_lines, population_counts)
from .contract import ContractFileError, canonical_json, load_contract, result_status, validate_contract
from .engineering import (FAILED, INCOMPLETE, KAT_CASES, PASSED, PREFIX_LIMIT_BYTES, PrefixTally, check_prefix,
                          engineering_status, issuance_exposure_reconciliation, known_accounting_answers)
from .market_index import HEAD_FILE, HEAD_KIND, INDEX_SCHEMA_VERSION, MARKETS_FILE, IndexProblem, archive_shas
from .market_index import load_manifest, loads_strict
from .metrics import BootstrapPlan, as_record, block_bootstrap, metrics_by_variant
from .outcome import issued_bets, market_outcome
from .path_guard import PathDenied, guard_path
from .replay import ReplayProblem, _member, replay_view
from .trial_ledger import LEDGER_FILE, LedgerError, TrialLedger, UnlockError, WriteUncertainError

# Results and receipts are schema 2 (engineering acceptance and its binding, task c2b3b1; the certificate fields bound
# in task c2b3b3); the input descriptor and the recovery intent keep schema 1, so their bytes and digests are
# unchanged. Schema 1 results and receipts refuse.
RESULTS_SCHEMA_VERSION = RECEIPT_SCHEMA_VERSION = 2
INPUT_SCHEMA_VERSION = RECOVERY_SCHEMA_VERSION = 1
ENGINEERING_CRITERIA_VERSION = 1
ACCEPTANCE_TAG = b"betangel-research/backtest/engineering-acceptance/v1\x00"
PREFIX_COMPONENTS = ("primary_source_prefix_consistency", "synthetic_certification")
# The known accounting answers this protocol requires, by case id and in order (Astra's decision 8): a changed case
# set is refused before any start, whatever engineering.KAT_CASES then holds.
KAT_CASE_IDS = ("s9_win_and_loss_in_two_markets", "s9_plus_and_minus_ten_in_one_market",
                "s9_plus_and_minus_ten_in_two_markets", "penny_rounding_against_us",
                "winnings_floor_and_commission_ceiling", "commission_half_up_rounding", "variants_are_never_netted",
                "void_is_unresolved_with_exposure", "dead_heat_is_unresolved_with_exposure",
                "own_runner_removed_after_decision_is_unresolved",
                "other_runner_removed_after_decision_is_unresolved", "result_revised_after_close_is_unresolved",
                "missing_bsp_is_unresolved")
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


def _regular_file(path, where):
    """The guarded path of a regular leaf, inspected without following links or suppressing filesystem errors.
    This is a point-in-time check, not protection against a concurrent writer replacing it before open."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise RunRefused(f"{where}:missing_path") from None
    except OSError:
        raise RunRefused(f"{where}:uninspectable") from None
    # File-type mask and regular-file tag; direct lstat preserves errors that pathlib's boolean checks suppress.
    if mode & 0o170000 != 0o100000:
        raise RunRefused(f"{where}:not_a_regular_file")
    return _guard(path, where)


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
    descriptor = {"kind": INPUT_KIND, "version": INPUT_SCHEMA_VERSION, "archives": entries}
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
            and isinstance(provenance.get("member_sha256"), str) and SHA256_RE.fullmatch(provenance["member_sha256"])
            and isinstance(record.get("decision_time"), dict) and isinstance(outcome, dict)
            and isinstance(checks, dict) and isinstance(outcome.get("market_time_months"), list)):
        return None
    months = [checks.get("path_month"), *outcome["market_time_months"]]
    if not all(isinstance(month, str) and MONTH_RE.fullmatch(month) for month in months):
        return None
    return months


def _first_pass(archives, rules):
    """(record count, eligible record count, every (plan, market id) key), after checking every record's shape and
    month, refusing a duplicate (plan, market id), and checking that the primary member of every record whose
    prefix the run reads is the manifest's (replay._member); the markets files are verified as they are read."""
    seen, count, eligible = set(), 0, 0
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
            try:
                population = classify(rules, record)[0]
            except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                raise RunRefused(f"input:{archive['archive_sha256']}:malformed_record") from None
            eligible += 1 if population == ELIGIBLE else 0
            if population not in (NO_VIEW, ANCHOR_NOT_KNOWN):  # its primary is read: it must be the manifest's
                try:
                    _member(archive["manifest"], record)
                except ReplayProblem:
                    raise RunRefused(f"input:{archive['archive_sha256']}:member_not_the_manifests:"
                                     f"{record['market_id']}") from None
    return count, eligible, seen


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
    """The canonical JSON object at a regular path that passes check, read through the guard; RunRefused otherwise."""
    data = _read_capped(_regular_file(path, where), where)
    try:
        value = loads_strict(data)
    except (ValueError, RecursionError):
        raise RunRefused(f"{where}:unreadable") from None
    if not (isinstance(value, dict) and _canonical(value) == data and check(value)):
        raise RunRefused(f"{where}:malformed")
    return value


def _binding_shape(binding):
    """A compact engineering-acceptance binding (_binding): its exact keys and types."""
    return (isinstance(binding, dict)
            and sorted(binding) == ["acceptance_sha256", "certificate_sha256", "criteria_version", "rule", "status"]
            and binding["status"] in (PASSED, FAILED, INCOMPLETE) and isinstance(binding["rule"], str)
            and binding["criteria_version"] == ENGINEERING_CRITERIA_VERSION
            and isinstance(binding["acceptance_sha256"], str) and SHA256_RE.fullmatch(binding["acceptance_sha256"])
            and isinstance(binding["certificate_sha256"], str)
            and SHA256_RE.fullmatch(binding["certificate_sha256"]) is not None)


RECEIPT_KEYS = ("certificate_sha256", "contract_sha256", "cumulative_starts", "engineering_acceptance", "failure",
                "issuance_ledger_bytes", "issuance_ledger_sha256", "kind", "ledger_head", "reconciled_by",
                "recorded_at", "results_sha256", "run_id", "schema_version", "status", "trials")


def _receipt_shape(receipt, digest, run_id):
    """A schema 2 receipt's exact keys and types; a completed run's carries its evidence and no failure, any other
    run's carries a failure and null evidence. certificate_sha256 is a sha256 or null, and a completed receipt's is
    its binding's."""
    trials = receipt.get("trials")
    completed = receipt.get("status") == "completed"
    size = receipt.get("issuance_ledger_bytes")
    evidence = ("results_sha256", "issuance_ledger_sha256", "issuance_ledger_bytes", "engineering_acceptance")
    identity = (receipt.get("kind"), receipt.get("schema_version"), receipt.get("contract_sha256"),
                receipt.get("run_id"))
    return (sorted(receipt) == list(RECEIPT_KEYS) and identity == (RECEIPT_KIND, RECEIPT_SCHEMA_VERSION, digest, run_id)
            and receipt.get("status") in ("aborted", "completed", "failed")
            and ((_binding_shape(receipt["engineering_acceptance"]) and type(size) is int and size >= 0
                  and receipt["failure"] is None
                  and receipt.get("certificate_sha256") == receipt["engineering_acceptance"]["certificate_sha256"])
                 if completed else (all(receipt[key] is None for key in evidence) and receipt["failure"] is not None))
            and (receipt.get("certificate_sha256") is None or (isinstance(receipt["certificate_sha256"], str)
                                                               and SHA256_RE.fullmatch(receipt["certificate_sha256"])))
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
            == (RECOVERY_KIND, RECOVERY_SCHEMA_VERSION, digest, run_id)
            and isinstance(variants, list) and bool(variants) and variants == sorted(set(variants))
            and all(isinstance(v, str) for v in variants)
            and isinstance(intent.get("recorded_by"), str) and CODE_IDENTITY_RE.fullmatch(intent["recorded_by"]))


class _History:
    """The contract's ledger, opened at the highest receipt anchor, and every run directory checked against it (the
    module docstring). Nothing here writes."""

    def __init__(self, contract_dir, runs_dir, digest, variants):
        self.digest = digest
        self.runs = _run_dirs(runs_dir)
        self.receipts, self.intents = {}, {}
        for run_id, run in self.runs.items():
            if _present(run / RECEIPT_FILE, f"receipt:{run_id}"):
                receipt = _read_json(run / RECEIPT_FILE, f"receipt:{run_id}", lambda value: True)
                if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:     # schema 1 history is never reread
                    raise RunRefused(f"receipt:{run_id}:schema_not_supported")
                if not _receipt_shape(receipt, digest, run_id):
                    raise RunRefused(f"receipt:{run_id}:malformed")
                self.receipts[run_id] = receipt
            if _present(run / RECOVERY_FILE, f"recovery:{run_id}"):
                intent = _read_json(run / RECOVERY_FILE, f"recovery:{run_id}",
                                    lambda value, r=run_id: _intent_shape(value, digest, r))
                if intent["variants"] != sorted(variants):                    # exactly the contract's, sorted
                    raise RunRefused(f"recovery:{run_id}:variants_not_the_contracts")
                self.intents[run_id] = intent
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

    def certificate_of(self, run_id):
        """The one certificate sha256 every start and close of run_id names, or None (unknown: an unaccounted
        reconstruction, or a start written before task c2b3b3; an absent field is None). RunRefused
        ledger:<run>:certificate_malformed when one is neither, ledger:<run>:certificates_disagree when they differ."""
        named = set()
        for start in self.trials_by_run.get(run_id, []):
            for entry in (start, self.closed.get(start["trial_id"])):
                if entry is None:
                    continue
                value = entry["summary"].get("certificate_sha256")
                if not (value is None or (isinstance(value, str) and SHA256_RE.fullmatch(value))):
                    raise RunRefused(f"ledger:{run_id}:certificate_malformed")
                named.add(value)
        if len(named) > 1:
            raise RunRefused(f"ledger:{run_id}:certificates_disagree")
        return next(iter(named), None)

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
        else:
            evidence = _completed_evidence(run, run_id, closes, self.digest)
            for key in ("results_sha256", "issuance_ledger_sha256", "issuance_ledger_bytes",
                        "engineering_acceptance"):
                if receipt.get(key) != evidence[key]:
                    raise RunRefused(f"receipt:{run_id}:{key}_mismatch")
        if receipt["certificate_sha256"] != self.certificate_of(run_id):    # both starts and every close
            raise RunRefused(f"receipt:{run_id}:certificate_sha256_mismatch")


def _file_bytes_sha(path, where):
    """(sha256, size) of a regular file read through the guard in chunks."""
    try:
        with _regular_file(path, where).open("rb") as handle:
            digest, size = hashlib.sha256(), 0
            while True:
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    return digest.hexdigest(), size
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        raise RunRefused(f"{where}:unreadable") from None


def _results_shape(results, digest, run_id):
    acceptance = results.get("engineering_acceptance")
    issuance = results.get("issuance")
    certification = results.get("certification")
    return ((results.get("kind"), results.get("schema_version"), results.get("contract_sha256"), results.get("run_id"))
            == (RESULTS_KIND, RESULTS_SCHEMA_VERSION, digest, run_id)
            and isinstance(acceptance, dict) and isinstance(issuance, dict)
            and isinstance(certification, dict) and sorted(certification) == ["certificate", "key", "sha256"]
            and all(isinstance(certification[key], str) and SHA256_RE.fullmatch(certification[key])
                    for key in ("key", "sha256"))
            and isinstance(certification["certificate"], dict))


def _completed_evidence(run, run_id, closes, digest):
    """The evidence a completed run's receipt must name, from its files, its closing entries and
    results.certification, which must agree: the results and issuance sha256, the issuance size, the
    engineering-acceptance binding recomputed from results.json and the certificate sha256. results.certification
    must be self-bound: its certificate's canonical bytes have its sha256, the certificate is certified for its key,
    and component (ii) names that sha256. Otherwise RunRefused results:<run>:malformed, schema_not_supported,
    issuance_not_the_ledgers, certificate_not_its_sha256, certificate_not_certified or
    certification_not_the_acceptances, or receipt:<run>:<what>_mismatch."""
    results_sha, _ = _file_bytes_sha(run / RESULTS_FILE, f"results:{run_id}")
    issuance_sha, issuance_size = _file_bytes_sha(run / ISSUANCE_FILE, f"issuance:{run_id}")
    files = {"results_sha256": results_sha, "issuance_ledger_sha256": issuance_sha,
             "issuance_ledger_bytes": issuance_size}
    if any(close["summary"].get("results_sha256") != results_sha for close in closes):   # a changed results file
        raise RunRefused(f"receipt:{run_id}:results_sha256_mismatch")
    results = _read_json(run / RESULTS_FILE, f"results:{run_id}", lambda value: True)
    if results.get("schema_version") != RESULTS_SCHEMA_VERSION:    # schema 1 history, whatever its closes hold
        raise RunRefused(f"results:{run_id}:schema_not_supported")
    for close in closes:
        for key, value in files.items():
            if close["summary"].get(key) != value:
                raise RunRefused(f"receipt:{run_id}:{key}_mismatch")
    if not _results_shape(results, digest, run_id):
        raise RunRefused(f"results:{run_id}:malformed")
    if (results["issuance"].get("ledger_sha256"), results["issuance"].get("ledger_bytes")) != (issuance_sha,
                                                                                               issuance_size):
        raise RunRefused(f"results:{run_id}:issuance_not_the_ledgers")
    certification = results["certification"]                 # self-bound: its own key and bytes, not today's key
    if _sha256_bytes(_canonical(certification["certificate"]) or b"") != certification["sha256"]:
        raise RunRefused(f"results:{run_id}:certificate_not_its_sha256")
    if (certification["certificate"].get("kind") != CERTIFICATE_KIND
            or certification["certificate"].get("key") != certification["key"]
            or certification["certificate"].get("status") != CERTIFIED):
        raise RunRefused(f"results:{run_id}:certificate_not_certified")
    if _component_ii(results["engineering_acceptance"]) != _certification_component(certification["sha256"]):
        raise RunRefused(f"results:{run_id}:certification_not_the_acceptances")
    binding = _binding(results["engineering_acceptance"], certification["sha256"])
    if any(close["summary"].get("engineering_acceptance") != binding for close in closes):
        raise RunRefused(f"receipt:{run_id}:engineering_acceptance_mismatch")
    if any(close["summary"].get("certificate_sha256") != certification["sha256"] for close in closes):
        raise RunRefused(f"receipt:{run_id}:certificate_sha256_mismatch")
    return dict(files, engineering_acceptance=binding, certificate_sha256=certification["sha256"])


def _says_passed(value):
    """Whether value (an acceptance or its binding) is an object whose status is exactly passed."""
    return isinstance(value, dict) and value.get("status") == PASSED


def _certification_component(certificate_sha256):
    """prefix_invariance component (ii) of a run bound to a certified certificate (a run starts only with one)."""
    return {"status": PASSED, "certificate_sha256": certificate_sha256}


def _component_ii(acceptance):
    """The synthetic_certification component an acceptance holds, or None at any level that is not an object."""
    value = acceptance
    for key in ("criteria", "prefix_invariance", "components", PREFIX_COMPONENTS[1]):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _stop_evidence(history):
    """(passed, unbound) run ids of this contract (decision 7, control D). passed: a completed close of the verified
    ledger whose binding says passed. unbound, only when nothing passed: a close of another kind carrying a passed
    binding, or a canonical results.json of this contract saying passed in a run directory whose receipt is not a
    completed one (those were checked whole). Canonical means canonical JSON with or without its final newline: the
    one cut write that still parses keeps the whole claim, so it claims; any other cut, or a file that is not canonical
    JSON, claims nothing. One that is not a regular file or cannot be inspected or read refuses."""
    passed, unbound = set(), set()
    for close in history.closed.values():
        if _says_passed(close["summary"].get("engineering_acceptance")):
            (passed if close["kind"] == "completed" else unbound).add(close["trial_id"].rsplit(".", 1)[0])
    if passed:
        return sorted(passed), []
    for run_id, run in history.runs.items():
        receipt = history.receipts.get(run_id)
        if receipt is not None and receipt["status"] == "completed":
            continue
        target, where = run / RESULTS_FILE, f"results:{run_id}"
        if not _present(target, where):
            continue
        data = _read_capped(_regular_file(target, where), where)
        try:
            value = loads_strict(data)
        except (ValueError, RecursionError):
            continue
        canonical = _canonical(value) if isinstance(value, dict) else None
        if (canonical is not None and data in (canonical, canonical[:-1]) and value.get("kind") == RESULTS_KIND
                and value.get("contract_sha256") == history.digest
                and _says_passed(value.get("engineering_acceptance"))):
            unbound.add(run_id)
    return [], sorted(unbound)


def _binding(acceptance, certificate_sha256):
    """The compact binding of a run's engineering acceptance that closes and receipts carry (the trial ledger's
    summary bounds keep the full result in results.json): its status, rule and criteria version, the tagged sha256 of
    its canonical JSON, and the sha256 of the certificate the run was bound to."""
    return {"status": acceptance.get("status"), "rule": acceptance.get("rule"),
            "criteria_version": acceptance.get("criteria_version"),
            "acceptance_sha256": hashlib.sha256(ACCEPTANCE_TAG + canonical_json(acceptance)).hexdigest(),
            "certificate_sha256": certificate_sha256}


def _certification(lab):
    """{key, sha256, certificate}: the certified certificate stored in the lab-state root for the current key (these
    sources and this interpreter); RunRefused source:<..>, certification:<..>, certifications:<..> or certificate:<..>
    otherwise (guard denials included)."""
    try:
        key = current_key()[0]
        certificate, sha256 = load_certificate(lab, key)
    except CertificateProblem as error:
        raise RunRefused(str(error)) from None
    return {"key": key, "sha256": sha256, "certificate": certificate}


def _source_problem(key, when):
    """None while the current key (these sources, this interpreter) is still the bound one; else source:<when>, or
    source:<when>:<the descriptor's refusal> when the sources can no longer be described (decision 12)."""
    try:
        now = current_key()[0]
    except CertificateProblem as error:
        return f"source:{when}:{error}"
    return None if now == key else f"source:{when}"


def _known_answers():
    """known_accounting_answers over exactly KAT_CASE_IDS, required passed before any start; the checked result,
    with the case ids it covered, is the one the run's acceptance reports."""
    ids = tuple(case.get("id") if isinstance(case, dict) else None for case in KAT_CASES)
    if ids != KAT_CASE_IDS:
        raise RunRefused("engineering:known_accounting_answers_not_the_known_set")
    try:
        result = known_accounting_answers()
    except Exception:
        raise RunRefused("engineering:known_accounting_answers_error") from None
    if result != {"status": PASSED, "cases": len(KAT_CASE_IDS), "failed_cases": []}:
        raise RunRefused("engineering:known_accounting_answers_not_passed")
    return dict(result, case_ids=list(KAT_CASE_IDS))


def _read_back(path, written, where):
    """The bytes of a file this run wrote (written bytes long), read again in full through the guard. A complete
    read of other bytes is returned for the comparison to fail; a file longer than written, or one that cannot be
    read, is an operational failure (RuntimeError)."""
    try:
        with _guard(path, where).open("rb") as handle:
            data = handle.read(written + 1)
    except (OSError, RunRefused) as error:
        raise RuntimeError(f"{where}:read_back_failed:{_failure(error)}") from None
    if len(data) > written:
        raise RuntimeError(f"{where}:read_back_longer_than_written")
    return data


def _acceptance(rule, kat, reconciliation, tally, certificate_sha256):
    """results.engineering_acceptance: the three criteria, prefix_invariance with its two components (the synthetic
    certification passed, naming the bound certificate's sha256), and engineering_status over them."""
    components = {PREFIX_COMPONENTS[0]: tally,
                  PREFIX_COMPONENTS[1]: _certification_component(certificate_sha256)}
    statuses = [component["status"] for component in components.values()]
    prefix_status = FAILED if FAILED in statuses else PASSED if statuses == [PASSED, PASSED] else INCOMPLETE
    criteria = {"issuance_exposure_reconciliation": reconciliation, "known_accounting_answers": kat,
                "prefix_invariance": {"status": prefix_status, "components": components}}
    return {"status": engineering_status(criteria), "rule": rule, "criteria_version": ENGINEERING_CRITERIA_VERSION,
            "criteria": criteria}


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


def _evaluate(archives, root, rules, accounting_rules, bootstrap_plan, limits, keys):
    """(decisions, issued bets, settlement, results settlement part, prefix tally result) of the run: decide, check
    each record's primary-source prefix (engineering.check_prefix, into one PrefixTally over every input key), settle,
    metrics and bootstrap. A member that fails integrity (ReplayProblem), a denied path or a tally refusal raises:
    the run fails rather than continue without that record."""
    manifests = {archive["archive_sha256"]: archive["manifest"] for archive in archives}

    def primary(record):
        sha = record["provenance"]["archive_sha256"]
        try:
            raw = safe_target(root, sha, record["provenance"]["member_path"])
        except ArchiveProblem as error:
            raise ReplayProblem(f"replay:{record['market_id']}:{error}") from None
        _guard(raw, f"raw:{record['market_id']}")
        return sha

    def replay(record):
        return replay_view(root, manifests[primary(record)], record, rules.offset_s, limits)

    tally = PrefixTally(rules.variants, keys, limits, PREFIX_LIMIT_BYTES)
    decisions, bets, outcomes = [], [], []
    for archive in archives:
        for record in _records(archive):
            decision = decide_market(rules, record, replay)
            if decision.population not in (NO_VIEW, ANCHOR_NOT_KNOWN):  # its primary is read again: guard it
                primary(record)
            tally.add(check_prefix(rules, record, decision, root, archive["manifest"], limits, PREFIX_LIMIT_BYTES),
                      decision)
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
    return decisions, bets, settlement, {
        "by_variant": metrics,
        "unresolved_reasons": {v: dict(sorted(r.items())) for v, r in sorted(unresolved.items())},
        "uncertainty": uncertainty}, tally.result()


def _append(ledger, kind, trial_id, variant, contract, input_sha256, code_identity, recorded_at, summary):
    return ledger.append(kind, trial_id=trial_id, contract=contract, variant=variant, input_sha256=input_sha256,
                         code_identity=code_identity, recorded_at=recorded_at, summary=summary)


def _receipt(digest, run_id, status, failure, evidence, trials, head, starts, recorded_at, reconciled_by,
             certificate_sha256=None):
    """evidence: _completed_evidence's keys for a completed run, else None (its fields stay null)."""
    evidence = evidence or {}
    return {"kind": RECEIPT_KIND, "schema_version": RECEIPT_SCHEMA_VERSION, "contract_sha256": digest,
            "run_id": run_id, "status": status, "failure": failure,
            "results_sha256": evidence.get("results_sha256"),
            "issuance_ledger_sha256": evidence.get("issuance_ledger_sha256"),
            "issuance_ledger_bytes": evidence.get("issuance_ledger_bytes"),
            "engineering_acceptance": evidence.get("engineering_acceptance"), "certificate_sha256": certificate_sha256,
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
        try:
            in_profile = canonical_json(profile_of(contract)) == canonical_json(P0_PROFILE)
        except (KeyError, TypeError, AttributeError, ValueError):
            in_profile = False
        if not in_profile:                                       # decision 3: the certified structural profile
            raise RunRefused("contract:not_supported:not_the_certified_profile")
        root, lab = _roots(data_root, lab_state_root)
        _identifiers(run_id, code_identity)
        certification = _certification(lab)
        kat = _known_answers()
        archives, descriptor, input_sha256 = _inputs(root)
        for archive in archives:                    # ingested under the read limits this run replays with
            if archive["manifest"].get("limits") != asdict(limits):
                raise RunRefused(f"input:{archive['archive_sha256']}:limits_not_the_runs")
        if canonical_json(certification["certificate"]["limits"]) != canonical_json(      # and certified under
                {"read": asdict(limits), "prefix_limit_bytes": PREFIX_LIMIT_BYTES}):
            raise RunRefused("certification:limits_not_the_runs")
        records, eligible, keys = _first_pass(archives, rules)
        if not records:
            raise RunRefused("inputs:no_records")
        if not eligible:
            raise RunRefused("inputs:no_eligible_records")
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
        passed, unbound = _stop_evidence(history)                # the stop rule (decision 7, control D)
        if passed:
            raise RunRefused("contract:engineering_passed:" + ",".join(passed))
        if unbound:
            raise RunRefused("contract:engineering_pass_claim_unbound:" + ",".join(unbound))
        if any(trial_id in history.every for _, trial_id in trials) or _present(runs_dir / run_id, "run_dir"):
            raise RunRefused("run_id:already_used")
        if len(history.started) + len(trials) > contract["stopping_rule"]["max_trials"]:
            raise RunRefused("ledger:trial_budget_exhausted")
        problem = _source_problem(certification["key"], "changed_before_start")    # decision 12
        if problem:
            raise RunRefused(problem)
        run_dir = _guard(runs_dir / run_id, "run_dir", prospective=True)
        run_dir.mkdir()
    except Exception as error:
        problem = str(error) if isinstance(error, RunRefused) else f"preflight:{_failure(error)}"
        if not lock.release():
            return _uncertain(problem, lock, (), lock_release="failed")
        return {"status": "refused", "problems": [problem]}
    return _run(contract, digest, rules, accounting_rules, bootstrap_plan, archives, descriptor, input_sha256, records,
                keys, kat, certification, root, history, trials, run_id, run_dir, code_identity, clock, limits, lock)


def _run(contract, digest, rules, accounting_rules, bootstrap_plan, archives, descriptor, input_sha256, records,
         keys, kat, certification, root, history, trials, run_id, run_dir, code_identity, clock, limits, lock):
    ledger, ids = history.ledger, [trial_id for _, trial_id in trials]
    started = {}
    for index, (variant, trial_id) in enumerate(trials):
        try:
            started[variant] = _append(ledger, "started", trial_id, variant, contract, input_sha256, code_identity,
                                       clock(), {"certificate_sha256": certification["sha256"]})
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
    evidence, failure, issued = None, None, {variant: 0 for variant, _ in trials}
    try:
        decisions, bets, settlement, settlement_part, tally = _evaluate(archives, root, rules, accounting_rules,
                                                                         bootstrap_plan, limits, keys)
        lines = ledger_lines(decisions)
        writer_sha = _write_new(run_dir / ISSUANCE_FILE, iter(lines), "issuance_ledger")
        observed = _read_back(run_dir / ISSUANCE_FILE, sum(len(line) for line in lines), "issuance_ledger")
        counts = issuance_counts(rules, decisions)
        issued = {variant: counts[variant]["issued"] for variant, _ in trials}
        stubs = [{"market_id": market_id, "provenance": {"plan": plan}} for plan, market_id in sorted(keys)]
        reconciliation = issuance_exposure_reconciliation(rules, stubs, decisions, observed, bets, settlement)
        acceptance = _acceptance(contract["engineering_acceptance"]["rule"], kat, reconciliation, tally,
                                 certification["sha256"])
        problem = _source_problem(certification["key"], "changed_during_run")   # decision 12: failed, charged
        if problem:
            raise RuntimeError(problem)
        issuance_sha = _sha256_bytes(observed)
        results = {"kind": RESULTS_KIND, "schema_version": RESULTS_SCHEMA_VERSION, "contract_sha256": digest,
                   "run_id": run_id, "code_identity": code_identity, "input": descriptor, "input_sha256": input_sha256,
                   "population": {"records": records, **population_counts(decisions),
                                  "source_refusals": _source_refusals(archives)},
                   "issuance": {"ledger_sha256": issuance_sha, "ledger_bytes": len(observed),
                                "writer_sha256": writer_sha, "rows": len(lines), "by_variant": counts},
                   "settlement": settlement_part, "certification": certification, "engineering_acceptance": acceptance}
        results_sha = _write_new(run_dir / RESULTS_FILE, [canonical_json(results) + b"\n"], "results")
        evidence = {"results_sha256": results_sha, "issuance_ledger_sha256": issuance_sha,
                    "issuance_ledger_bytes": len(observed),
                    "engineering_acceptance": _binding(acceptance, certification["sha256"]),
                    "certificate_sha256": certification["sha256"]}
    except Exception as error:                                  # anything after the starts closes them as failed
        failure, evidence = _failure(error), None
    closed = {}
    summary = dict(evidence) if failure is None else {"failure": failure, "certificate_sha256": certification["sha256"]}
    for variant, trial_id in trials:
        try:
            closed[variant] = _append(ledger, "completed" if failure is None else "failed", trial_id, variant,
                                      contract, input_sha256, code_identity, clock(), summary)
        except LedgerError as error:
            return _uncertain(f"ledger:{error}", lock, ids, closed=sorted(closed), failure=failure)
    status = "completed" if failure is None else "failed"
    receipt = _receipt(digest, run_id, status, failure, evidence,
                       [(variant, trial_id, started[variant], closed[variant], issued[variant])
                        for variant, trial_id in trials],
                       ledger.head, len(history.started) + len(trials), clock(), None, certification["sha256"])
    try:
        receipt_sha = _write_new(run_dir / RECEIPT_FILE, [canonical_json(receipt) + b"\n"], "receipt")
    except Exception as error:
        return _uncertain(f"receipt:{_failure(error)}", lock, ids, receipt=receipt)
    if not lock.release():
        return _uncertain("runner_lock:release_failed", lock, ids, receipt_sha256=receipt_sha)
    return {"status": status, "problems": [] if failure is None else [failure], "run_dir": str(run_dir),
            "results_sha256": receipt["results_sha256"], "receipt_sha256": receipt_sha,
            "engineering_acceptance": evidence["engineering_acceptance"]["status"] if evidence else None,
            "ledger_head": receipt["ledger_head"], "cumulative_starts": receipt["cumulative_starts"]}


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
        certificates = {run_id: history.certificate_of(run_id)       # every known run, before any recovery write
                        for run_id in sorted(history.trials_by_run)}
        for run_id in history.missing:                              # already closed; recovery will not alter them
            if run_id not in history.runs:
                raise RunRefused(f"receipt:{run_id}:no_run_directory")
            closes = [history.closed[entry["trial_id"]] for entry in history.trials_by_run[run_id]]
            if _status_of({close["kind"] for close in closes}) == "completed":
                _completed_evidence(history.runs[run_id], run_id, closes, digest)
        for run_id in history.incomplete_recovery:
            have = {entry["variant"] for entry in history.trials_by_run.get(run_id, [])}
            if certificates.get(run_id) is not None and any(
                    variant not in have for variant in history.intents[run_id]["variants"]):
                raise RunRefused(f"ledger:{run_id}:certificates_disagree")
        for entry in history.started:
            if entry["trial_id"] in aborted:
                _append(history.ledger, "aborted", entry["trial_id"], entry["variant"], contract,
                        entry["input_sha256"], entry["code_identity"], clock(),
                        {"reason": "reconciled_unfinished", "reconciled_by": code_identity, "pending": aborted,
                         "certificate_sha256": certificates[entry["trial_id"].rsplit(".", 1)[0]]})
                appended.append(entry["trial_id"])
        for run_id in recorded:                      # the intent first, so an interruption cannot shrink the record
            intent = {"kind": RECOVERY_KIND, "schema_version": RECOVERY_SCHEMA_VERSION, "contract_sha256": digest,
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
                for kind, summary in (("started", {"certificate_sha256": None}),       # unknown, never invented
                                      ("aborted", {"reason": "reconciled_unaccounted_run",
                                                   "reconciled_by": code_identity, "certificate_sha256": None})):
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
            evidence = (_completed_evidence(run_dir, run_id, closes, digest) if status == "completed" else None)
            certificate_sha256 = history.certificate_of(run_id)
            head = history.ledger.head
            anchors = [(entry["variant"], entry["trial_id"], (entry["seq"], entry["entry_sha256"]),
                        (close["seq"], close["entry_sha256"]), None) for entry, close in zip(trials, closes)]
            receipt = _receipt(digest, run_id, status, None if status == "completed" else "reconciled", evidence,
                               anchors, head, history.starts_until(head[0]), clock(), code_identity, certificate_sha256)
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
