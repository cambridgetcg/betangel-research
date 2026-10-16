"""Draft-only, index-only P0 readiness inventory; invoke with python3 -B -m betangel_research.betfair.readiness.

Reads the explicit contract, manifest, index head and entire markets stream, plus the implementation's sources.
The stream includes outcome objects: only their gate months are used. No raw member or TAR is opened. No replay,
issuance, draw, settlement, metric, prefix evaluation, certificate lookup, history, lock or write is performed
by this module. Use -B or PYTHONDONTWRITEBYTECODE=1 to prevent the interpreter from writing bytecode caches.
Pure model construction checks the draft's supported settings; it does not evaluate those models.

The runner's pure private helpers supply path guards, input binding, record shape/gate checks and member matching.
Record diagnostics preserve its order and seen-set timing, but continue after each record's first problem. Only
clean records enter population/plan/prefix counts. A file failure discards all counts; successful output requires
the whole stream to have been consumed and its hash verified. A null input_preflight_would_refuse is not a run
permission: frozen validation, certification, history, budget and stop evidence remain unassessed.
Regular-file safety inspection precedes the runner's input reader. A directory or FIFO used as a manifest, head
or markets file is refused here as not_a_regular_file; the runner reports unreadable or may block on a FIFO.
With several faults, the first file-level diagnostic can also differ. Inventory requires parsed record count
to equal head.markets: an unterminated last record violates that invariant even if the runner accepts the newline
count and hash. This inventory-owned refusal is records_count_mismatch, distinct from the runner's markets_mismatch
binding failure.

Use an unchanged checkout and inputs with no concurrent writer. The archive-set precheck occurs before any head
or markets open; it does not lock against a later replacement. Source naming is not certification. In particular,
code_identity is only an operator label, and this module is outside the backtest source descriptor, so its own
source digest is reported too. Inventory stdout is one canonical JSON line; exits are 0 clean, 3 diagnostic problems,
2 refused. Argparse exits separately: --help is exit 0 with text; usage errors are exit 2 with empty stdout.
"""

import argparse
from dataclasses import asdict
import hashlib
from pathlib import Path
import re

from .backtest import certificate, runner
from .backtest.accounting import AccountingRules
from .backtest.archive import (DEFAULT_LIMITS, MARKET_ID_RE, ArchiveProblem, check_data_root, gate_snapshot,
                               month_gate, safe_path)
from .backtest.cohort import ANCHOR_NOT_KNOWN, ELIGIBLE, NO_VIEW, CohortRules, classify
from .backtest.contract import ContractFileError, canonical_json, load_contract, result_status, validate_contract
from .backtest.engineering import PREFIX_LIMIT_BYTES
from .backtest.market_index import HEAD_FILE, MARKETS_FILE, IndexProblem, archive_shas
from .backtest.metrics import BootstrapPlan
from .backtest.replay import ReplayProblem, _member

KIND, VERSION = "betangel_backtest_readiness", 1
MAX_PROBLEMS_LISTED = 100
STAGES = ("malformed_record", "stale_gate", "duplicate_market", "member_not_the_manifests")
NOT_ASSESSED = ("frozen_contract", "engine_certificate", "known_accounting_answers", "raw_prefix_assessability",
                "stopped_views", "market_conditions_2_to_6", "replay", "issuance", "draws", "settlement", "metrics",
                "prefix_evaluation", "runner_lock", "history", "trial_budget", "stop_evidence")
PATH_REASONS = ("missing_path", "unresolvable", "denied_live_state_tree", "not_a_regular_file", "uninspectable",
                "unreadable")
INPUT_REASONS = (*PATH_REASONS, "unsafe_target", "not_indexed", "head_unreadable", "head_not_canonical",
                 "head_mismatch", "markets_unreadable", "markets_mismatch", "markets_changed", "record_too_large")
MANIFEST_REASONS = ("missing", "unreadable", "not_json", "not_canonical", "unsupported", "names_another_archive",
                    "malformed", "member_inconsistent", "unsafe_target")
CONTRACT_REASONS = ("unreadable", "not_a_regular_file", "too_large", "not_utf8", "not_json", "duplicate_key",
                    "non_finite_number", "number_too_large")
MODEL_REASONS = ("contract:invalid", "decision_timing:anchor_not_supported", "decision_timing:delay_s_null",
                 "variants:not_the_p0_variants", "contract_invalid", "contract_staking_not_supported",
                 "commission_rate_not_a_decimal_fraction", "commission_rate_too_precise", "rounding_not_supported",
                 "settlement_cases_not_the_contract_cases", "settlement_rule_not_supported", "variants_malformed",
                 "uncertainty_not_supported", "resamples_out_of_range", "confidence_level_not_a_fraction_in_0_1",
                 "seed_out_of_range", "block_not_supported")


class InventoryRefused(Exception):
    """An inventory-owned, bounded code, never arbitrary input or exception text."""


def _known(error, codes, fallback):
    """Translate a helper exception only when its entire argument is one of the enumerated codes."""
    value = error.args[0] if len(error.args) == 1 else None
    return next((code for code in codes if value == code), fallback)


def _guard(path, where):
    try:
        return runner._guard(path, where)
    except runner.RunRefused as error:
        raise InventoryRefused(_known(error, (f"{where}:{code}" for code in PATH_REASONS),
                                      f"{where}:unresolvable")) from None


def _input_code(error, sha):
    where = f"input:{sha}"
    codes = [f"{where}:{reason}" for reason in INPUT_REASONS]
    codes += [f"{where}:head:{reason}" for reason in ("unreadable", "too_large")]
    codes += [f"{where}:manifest:{sha}:{reason}" for reason in MANIFEST_REASONS]
    codes += [f"manifests:{reason}" for reason in (*PATH_REASONS, "unsafe_target", "not_a_directory")]
    return _known(error, codes, "inputs:unrecognised_problem")


def _market_id(record):
    value = record.get("market_id") if isinstance(record, dict) else None
    return value if isinstance(value, str) and MARKET_ID_RE.fullmatch(value) else "invalid"


def _plan(record):
    provenance = record.get("provenance") if isinstance(record, dict) else None
    value = provenance.get("plan") if isinstance(provenance, dict) else None
    return value if value in ("BASIC", "ADVANCED", "PRO") else "invalid"


def _record_code(sha, record, stage):
    if stage == "duplicate_market":
        return f"cohort:{_market_id(record)}:duplicate_market"
    suffix = "" if stage == "malformed_record" else f":{_market_id(record)}"
    return f"input:{sha}:{stage}{suffix}"


def _classify(archive, rules, record, seen):
    """(stage or None, classification or None), in runner._first_pass order, including seen.add timing."""
    months = runner._record_months(record, archive["archive_sha256"])
    if months is None:
        return "malformed_record", None
    if any(month_gate(month) for month in months):
        return "stale_gate", None
    key = (record["provenance"]["plan"], record["market_id"])
    if key in seen:
        return "duplicate_market", None
    seen.add(key)
    try:
        classification = classify(rules, record)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return "malformed_record", None
    if classification[0] not in (NO_VIEW, ANCHOR_NOT_KNOWN):
        try:
            _member(archive["manifest"], record)
        except ReplayProblem:
            return "member_not_the_manifests", None
    return None, classification


def _increment(counts, key):
    counts[key] = counts.get(key, 0) + 1


def _reason(value):
    # These are code labels, not free-form messages. Unknown strings collapse to a single bounded category.
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else "other"


def _source_refusals(archives):
    """Project only the runner's refusal fields, sanitising labels before its distinct-market aggregation."""
    projected = []
    for archive in archives:
        files = [{"status": item["status"] if item["status"] in ("indexed", "unreadable", "refused") else "other",
                  "reason": None if item["status"] == "indexed" or item["reason"] is None else _reason(item["reason"])}
                 for item in archive["head"]["files"]]
        projected.append({"manifest": {"refused": [{"reason": _reason(item["reason"])}
                                                   for item in archive["manifest"].get("refused", [])]},
                          "head": {"files": files, "refused_markets": [
                              {"market_id": item["market_id"], "reason": _reason(item["reason"])}
                              for item in archive["head"]["refused_markets"]]}})
    return runner._source_refusals(projected)


def _draft(path):
    try:
        contract = load_contract(path)
    except ContractFileError as error:
        code = _known(error, CONTRACT_REASONS, "unreadable")
        raise InventoryRefused(f"contract:{code}") from None
    problems, digest = validate_contract(contract)
    if result_status(contract, problems) != "valid_draft":
        code = "not_a_draft" if isinstance(contract, dict) and contract.get("status") == "frozen" else "not_valid_draft"
        raise InventoryRefused(f"contract:{code}")
    try:
        rules = CohortRules.from_contract(contract)
        AccountingRules.from_contract(contract)
        BootstrapPlan.from_contract(contract)
    except ValueError as error:
        code = _known(error, MODEL_REASONS, "model_not_supported")
        raise InventoryRefused(f"contract:not_supported:{code}") from None
    if rules.delay_s != 0:
        raise InventoryRefused("contract:not_supported:delay_s_other_than_0")
    if canonical_json(certificate.profile_of(contract)) != canonical_json(certificate.P0_PROFILE):
        raise InventoryRefused("contract:not_supported:not_the_certified_profile")
    return contract, digest, rules


def _inputs(root, sha, index_sha):
    manifests = root / "manifests"
    if manifests.exists() or manifests.is_symlink():
        _guard(manifests, "manifests")
    try:
        if archive_shas(root) != [sha]:
            raise InventoryRefused("inputs:not_the_expected_archives")
        # The runner's readers assume regular index files. Inspect before opening so a FIFO cannot hang inventory.
        for parts in (("manifests", f"{sha}.json"), ("index", sha, HEAD_FILE), ("index", sha, MARKETS_FILE)):
            path = safe_path(root, *parts)
            if parts[-1] == HEAD_FILE and not path.exists():
                raise InventoryRefused(f"input:{sha}:not_indexed")
            runner._regular_file(path, f"input:{sha}")
        archives, descriptor, digest = runner._inputs(root)
    except (runner.RunRefused, IndexProblem, ArchiveProblem) as error:
        # safe_path's single token is also known; no path/member/error text is ever copied to stdout.
        code = f"input:{sha}:unsafe_target" if error.args == ("unsafe_target",) else _input_code(error, sha)
        raise InventoryRefused(code) from None
    if [item["archive_sha256"] for item in descriptor["archives"]] != [sha]:
        raise InventoryRefused("inputs:not_the_expected_archives")
    if descriptor["archives"][0]["index_sha256"] != index_sha:
        raise InventoryRefused("inputs:index_not_the_verified")
    return archives, descriptor, digest


def inventory(contract_path, data_root, expect_archive, expect_index_sha256, code_identity):
    """Return one JSON-ready inventory (or refusal). Explicit inputs only; no output path or lab-state argument."""
    envelope = {"kind": KIND, "version": VERSION}
    try:
        contract_path = _guard(contract_path, "contract")
        _guard(data_root, "data_root")
        contract, digest, rules = _draft(contract_path)
        root, problems = check_data_root(data_root)
        if problems:
            codes = (f"data_root:{code}" for code in ("missing", "inside_repository", "inside_git_worktree",
                                                     "not_a_directory", "unreadable"))
            raise InventoryRefused(_known(InventoryRefused(problems[0]), codes, "data_root:unreadable"))
        for value, label in ((expect_archive, "expect_archive"), (expect_index_sha256, "expect_index_sha256")):
            if not isinstance(value, str) or not runner.SHA256_RE.fullmatch(value):
                raise InventoryRefused(f"{label}:malformed_sha256")
        if not isinstance(code_identity, str) or not runner.CODE_IDENTITY_RE.fullmatch(code_identity):
            raise InventoryRefused("code_identity:malformed")
        archives, descriptor, input_sha = _inputs(root, expect_archive, expect_index_sha256)
        same_limits = all(archive["manifest"].get("limits") == asdict(DEFAULT_LIMITS) for archive in archives)
        first_problem = None if same_limits else f"input:{expect_archive}:limits_not_the_runs"
        seen, by_class, by_reason, by_plan = set(), {}, {}, {}
        stages = dict.fromkeys(STAGES, 0)
        listed, lines, clean, prefixes = [], 0, 0, 0
        for archive in archives:
            try:
                for record in runner._records(archive):
                    lines += 1
                    stage, classification = _classify(archive, rules, record, seen)
                    if stage is not None:
                        stages[stage] += 1
                        first_problem = first_problem or _record_code(archive["archive_sha256"], record, stage)
                        if len(listed) < MAX_PROBLEMS_LISTED:
                            listed.append([_plan(record), _market_id(record), stage])
                        continue
                    population, reason, _ = classification
                    clean += 1
                    _increment(by_class, population)
                    _increment(by_plan, _plan(record))
                    if reason is not None:
                        _increment(by_reason.setdefault(population, {}), reason)
                    if population not in (NO_VIEW, ANCHOR_NOT_KNOWN):
                        prefixes += 1
            except runner.RunRefused as error:
                raise InventoryRefused(_input_code(error, archive["archive_sha256"])) from None
        if lines != sum(item["markets"] for item in descriptor["archives"]):
            raise InventoryRefused(f"input:{expect_archive}:records_count_mismatch")
        if not lines:
            first_problem = first_problem or "inputs:no_records"
        elif not by_class.get(ELIGIBLE, 0):
            first_problem = first_problem or "inputs:no_eligible_records"
        _, _, source_sha, interpreter = certificate.current_key()
        tool_source = certificate._source_file(certificate.source_root(), "betangel_research/betfair/readiness.py")
        return {**envelope, "status": "completed_with_problems" if first_problem else "completed", "refusal": None,
                "naming": {"code_identity": code_identity, "source_descriptor_sha256": source_sha,
                           "readiness_source_sha256": tool_source["sha256"], "interpreter": interpreter,
                           "gate_snapshot_sha256": hashlib.sha256(canonical_json(gate_snapshot())).hexdigest()},
                "contract": {"status": "valid_draft", "contract_sha256": digest, "candidate_settings": {
                    "tier": rules.tier, "countries": sorted(rules.countries),
                    "market_types": sorted(rules.market_types),
                    "evaluate_split": contract["population"]["evaluate_split"],
                    "split_months": [rules.start_month, rules.end_month],
                    "anchor": contract["decision_timing"]["anchor"],
                    "offset_s": rules.offset_s, "delay_s": rules.delay_s, "variants": list(rules.variants)}},
                "limits": {"read": asdict(DEFAULT_LIMITS), "prefix_limit_bytes": PREFIX_LIMIT_BYTES,
                           "manifest_limits_are_the_runs": same_limits,
                           "certificate_limits": "not_checked_before_certification",
                           "index_build_limits": "not_recorded_in_head"},
                "input": descriptor, "input_sha256": input_sha,
                "records": {"lines": lines, "classified": clean, "with_problems": sum(stages.values())},
                "record_problems": stages, "record_problems_listed": listed,
                "input_preflight_would_refuse": first_problem,
                "population": {"by_class": by_class, "by_reason": by_reason}, "records_by_plan": by_plan,
                "prefix_candidates": prefixes, "raw_prefix_assessability": "unknown",
                "source_refusals": _source_refusals(archives), "not_assessed": list(NOT_ASSESSED)}
    except InventoryRefused as error:
        return {**envelope, "status": "refused", "refusal": error.args[0]}
    except certificate.CertificateProblem:
        return {**envelope, "status": "refused", "refusal": "source:unavailable"}
    except Exception as error:
        name = type(error).__name__
        name = name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "Exception"
        return {**envelope, "status": "refused", "refusal": f"internal:{name}"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--expect-archive", required=True)
    parser.add_argument("--expect-index-sha256", required=True)
    parser.add_argument("--code-identity", required=True)
    args = parser.parse_args(argv)
    result = inventory(args.contract, args.data_root, args.expect_archive, args.expect_index_sha256, args.code_identity)
    print(canonical_json(result).decode("utf-8"))
    return {"completed": 0, "completed_with_problems": 3, "refused": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
