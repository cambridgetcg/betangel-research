#!/usr/bin/env python3
"""Read-only lab-runbook extractors and closed publication projections.

Invoke with an absolute, pinned interpreter and ``-I -S -B``. This module imports
only the standard library. Runtime identity and ledger checks run fixed snippets
in a fresh ``-S -B`` child at the dedicated checkout; no user code is interpolated.
Raw reports stay local. Publication uses a generated-name registry and fixed field,
code and enum allowlists; unexpected data becomes metadata, never exception text.
"""

from datetime import date
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

HEADS = set("""preflight ledger receipt runner_lock contract input inputs run intent results issuance cohort replay
outcome accounting engineering roots ReplayProblem RunRefused LedgerError WriteUncertainError UnlockError OSError
RuntimeError ValueError TypeError KeyError PublicationUncertain CohortProblem reconcile recovery run_id run_dir runs
runs_dir contracts contract_dir data_root lab_state_root source certification certifications certificate code_identity
index manifests issuance_ledger manifest archive expect_archive expect_index_sha256 internal""".split())
CLASSES = set("eligible no_view anchor_not_known_by_decision excluded unclassifiable".split())
POP_REASONS = set("""plan_not_contract_tier month_not_evaluated event_type_not_horse_racing
country_not_in_population market_type_not_in_population eventTypeId countryCode marketType""".split())
STAGES = set("malformed_record stale_gate duplicate_market member_not_the_manifests".split())
REASONS = set("""missing unreadable unsafe_target member_hash_mismatch index_would_change write_failed
line_too_long decompressed_too_large not_json malformed_message invalid_or_decreasing_pt segmented
malformed_market_id malformed_runner_change malformed_image_flag malformed_market_definition malformed_runner
not_bz2 primary_source_refused month_sealed_holdout month_exposed_exploratory_only month_not_prospective
month_prospective_out_of_scope not_a_regular_file not_a_directory not_indexed head_unreadable head_not_canonical
head_mismatch markets_unreadable markets_mismatch markets_changed record_too_large limits_not_the_runs
records_count_mismatch missing_path unresolvable denied_live_state_tree uninspectable too_large not_canonical
unsupported names_another_archive malformed member_inconsistent no_records no_eligible_records
not_a_draft not_valid_draft unavailable malformed_sha256 unrecognised_problem not_supported""".split())
VARIANTS = {"ltp_favourite", "seeded_random_runner"}

# Exact public refusal vocabulary. Keyed families are deliberately spelt out:
# no path, member, exception or report value can manufacture a new public code.
EXTRACTOR_CODES = set("""extractor:shape extractor:unexpected paths:git_failed paths:not_absolute_normal
paths:parent_not_plain paths:denied_tree paths:worktree_overlap paths:root_overlap evidence:prefix_already_used
recovery:unexpected_entry_or_type recovery:directory_not_plain recovery:lock_remains
recovery:attempt_prefix recovery:checkpoint_not_regular recovery:state_check_not_regular recovery:no_run_json
recovery:state_check_not_zero recovery:newer_attempt
inputs:entry_set inputs:entry_type inputs:root_changed inputs:archive_count inputs:archive_changed
inputs:hash_changed:manifest inputs:hash_changed:head inputs:hash_changed:markets inputs:line_count_changed
identity:site_or_pycache identity:python_env identity:sys_path identity:not_the_dedicated_checkout
index:not_one_indexed_archive index:not_the_expected_unchanged_archive inventory:not_clean inventory:code_identity
inventory:identity_mismatch:source_descriptor_sha256 inventory:identity_mismatch:interpreter
inventory:identity_mismatch:gate_snapshot_sha256 contract:not_the_expected_transition contract:settings_changed
certificate:not_new_38_of_38 certificate:problems certificate:path_or_key certificate:not_regular certificate:bytes
certificate:stored_status_or_key certificate:stored_counts certificate:source certificate:interpreter
binding:parent_not_plain binding:file_not_regular binding:not_completed binding:run_id binding:contract binding:code
binding:input binding:records binding:population binding:certificate_key binding:certificate binding:stored_certificate
binding:results binding:receipt binding:issuance binding:issuance_size binding:unknown_acceptance
binding:acceptance_mismatch binding:exit_mismatch binding:starts binding:trial_id binding:start_input
binding:start_code_or_certificate binding:criteria_set binding:criterion_status binding:prefix_counts
binding:prefix_records binding:prefix_reasons binding:unexercised_variants run:state_not_certifications_only
runtime:check_failed author:worktree_state author:cache_present author:census_stderr author:census_failed
author:head_changed author:c0_head_mismatch author:tracked_bytes author:c1_diff
consent:text consent:identifiers notice:record notice:date notice:reference
parent:uninspectable parent:changed""".split())

FIXED_PAIRS = {
    "index": {"index_would_change", "gate_changed", "partial_file_present", "write_failed", "unsafe_target"},
    "manifest": {"missing", "unreadable", "not_canonical", "gate_changed", "malformed", "unsafe_target"},
    "ledger": {"trial_budget_exhausted", "unfinished_trials"},
    "certification": {"missing", "limits_not_the_runs"},
    "source": {"changed_before_start", "changed_since_import", "changed_during_certification", "unavailable"},
    "inputs": {"no_archive", "no_records", "no_eligible_records"},
    "contract": {"engineering_passed", "engineering_pass_claim_unbound", "not_valid_frozen", "not_supported"},
    "attempt": {"exists", "not_created", "unsafe_target", "not_a_directory"},
    "attempt_id": {"malformed"},
    "cases": {"missing_path", "uninspectable", "not_a_directory", "not_created", "unsafe_target", "unwritable"},
}
IO_ERRORS = {"PermissionError", "FileNotFoundError", "IsADirectoryError", "NotADirectoryError", "OSError"}


class ExtractorRefusal(ValueError):
    """A fixed local refusal; unexpected exception text is never a publication field."""


LEDGER_LINE_CODES = set("""truncated_line line_too_long not_strict_json keys_not_entry_keys not_canonical
entry_hash_mismatch seq_out_of_order prev_sha256_mismatch ledger_schema_unsupported seq_malformed
contract_sha256_malformed entry_sha256_malformed input_sha256_malformed prev_sha256_malformed kind_not_allowed
trial_id_malformed variant_malformed code_identity_malformed recorded_at_malformed training_cutoff_not_null
exposure_record_malformed seeds_malformed summary_not_an_object summary_too_deep summary_integer_too_large
summary_text_too_long summary_key_malformed summary_value_not_allowed summary_too_large trial_already_started
trial_not_started trial_already_closed trial_mismatch""".split())
LEDGER_CONTEXT_CODES = {"ledger:" + code for code in """uninspectable missing symlink not_a_regular_file
unreadable too_large empty truncated_before_anchor anchor_mismatch locked lock_failed write_failed head_moved
append_not_confirmed unlock_failed""".split()}
LEDGER_CONTEXT_CODES |= {"entry:" + code for code in LEDGER_LINE_CODES | {
    "not_canonical_json", "max_trials_reached", "ledger_full"}}
LEDGER_CONTEXT_CODES |= {"anchor:malformed", "run_dir:not_a_directory", "contract:not_valid_frozen",
                         "contract:fitting_not_supported", "variant:not_in_contract", "duplicate_key",
                         "float_not_allowed", "integer_too_large", "ledger_verify_failed"}


def ledger_code(value):
    """Only source-enumerated codes; the optional line is a count, never part of an arbitrary string."""
    if value is None:
        return {"code": None, "line": None}
    if not isinstance(value, str):
        return {"code": "unclassified", "line": None}
    if value in LEDGER_CONTEXT_CODES:
        return {"code": value, "line": None}
    match = re.fullmatch(r"line ([1-9][0-9]{0,7}):([a-z0-9_]+)", value)
    if match is not None and match[2] in LEDGER_LINE_CODES:
        # The file reader caps ledger bytes at 64 MiB. A final unterminated line adds at most one.
        number = int(match[1])
        if number <= (64 << 20) + 1:
            return {"code": match[2], "line": number}
    return {"code": "unclassified", "line": None}



def need(ok, reason="extractor:shape"):
    if not ok:
        raise ExtractorRefusal(reason)


def count(value):
    need(value is None or (type(value) is int and value >= 0))
    return value


def digest(value):
    need(value is None or (isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value)))
    return value


def identity_hash(value):
    need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40} tree [0-9a-f]{40}", value))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def enum(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unclassified"


def code(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return "unclassified"
    parts = value.split(":")
    if parts[0] == "raw":
        return "raw:" + parts[-1] if parts[-1] in REASONS else "unclassified"
    if parts[0] == "cohort" and parts[-1] == "duplicate_market":
        return "cohort:duplicate_market"
    if parts[0] == "input":
        for reason in STAGES | REASONS:
            if reason in parts[2:]:
                return "input:" + reason
    if parts[0] in {"index", "manifest"}:
        if len(parts) == 3 and re.fullmatch("[0-9a-f]{64}", parts[1]) and parts[2] in FIXED_PAIRS[parts[0]]:
            return parts[0] + ":" + parts[2]
        if (len(parts) == 4 and parts[0] == "index" and re.fullmatch("[0-9a-f]{64}", parts[1])
                and parts[2] == "io" and parts[3] in IO_ERRORS):
            return "index:io:" + parts[3]
    elif len(parts) >= 2 and parts[1] in FIXED_PAIRS.get(parts[0], ()):
        return parts[0] + ":" + parts[1]
    return parts[0] if parts[0] in HEADS else "unclassified"


def count_map(value, allowed):
    if value is None:
        return None
    need(isinstance(value, dict))
    out = {}
    for key, number in value.items():
        name = key if key in allowed else "unclassified"
        number = count(number)
        need(number is not None)
        out[name] = out.get(name, 0) + number
    return out


def population(value):
    if value is None:
        return None
    need(isinstance(value, dict))
    reasons = value.get("by_reason")
    need(reasons is None or isinstance(reasons, dict))
    projected = None if reasons is None else {}
    if reasons is not None:
        for name, counts in reasons.items():
            key = name if name in CLASSES else "unclassified"
            target = projected.setdefault(key, {})
            for reason, number in count_map(counts, POP_REASONS).items():
                target[reason] = target.get(reason, 0) + number
    return {"records": count(value.get("records")), "by_class": count_map(value.get("by_class"), CLASSES),
            "by_reason": projected}


def interpreter(value):
    if value is None:
        return None
    # Copy only the local, non-secret interpreter identity after byte-value equality; never raw report strings.
    current = {"implementation": sys.implementation.name, "cache_tag": sys.implementation.cache_tag,
               "version_info": list(sys.version_info[:5]), "version": sys.version, "platform": sys.platform,
               "byteorder": sys.byteorder, "maxsize": sys.maxsize, "float_repr_style": sys.float_repr_style,
               "optimize": sys.flags.optimize}
    need(value == current)
    return current


def input_descriptor(value):
    if value is None:
        return None
    need(isinstance(value, dict) and value.get("kind") == "betangel_backtest_input" and value.get("version") == 1)
    need(isinstance(value.get("archives"), list))
    archives = []
    for item in value["archives"]:
        need(isinstance(item, dict))
        hashes = {k: digest(item.get(k)) for k in (
            "archive_sha256", "manifest_sha256", "index_sha256", "markets_sha256")}
        need(all(hashes.values()))
        gate = item.get("manifest_gate_current")
        need(type(gate) is bool)
        archives.append({**hashes, "markets": count(item.get("markets")), "manifest_gate_current": gate})
    return {"kind": "betangel_backtest_input", "version": 1, "archives": archives}


def head(value):
    if value is None:
        return None
    need(isinstance(value, list) and len(value) == 2)
    need(type(value[0]) is int and value[0] >= 0 and digest(value[1]) is not None)
    return {"seq": count(value[0])}


def sequence_count(value):
    need(value is None or isinstance(value, list))
    return None if value is None else len(value)


def variant_list(value):
    need(value is None or (isinstance(value, list) and all(v in VARIANTS for v in value)))
    return None if value is None else sorted(value)


CENSUS_REASONS = {"target:not_an_absolute_normal_path", "parent:not_a_plain_directory",
                  "target:not_a_plain_directory", "listing:over_200_entries"}
LAYOUT_REASONS = {"clean_layout", "state_history", "data_layout", "parent_layout", "uninspectable"}


def project(mode, raw, lock=None):
    if mode == "raw":
        return {"classification": "unclassified"}
    if mode == "checkpoint":
        # Registry selects only a generated session checkpoint containing parameters and helper source, never data.
        return {"checkpoint_sha256": hashlib.sha256(raw).hexdigest()}
    if mode == "digest":
        value = raw.decode("ascii").strip()
        need(re.fullmatch(r"[0-9a-f]{64}", value) is not None)
        return {"sha256": value}
    if mode == "exit":
        text = raw.decode("ascii").strip()
        match = re.fullmatch(r"(?:block_exit=)?(0|[1-9][0-9]{0,2})", text)
        need(match is not None and int(match[1]) <= 255)
        return {"exit": int(match[1])}
    if mode == "liveness":
        lines = raw.decode("ascii").splitlines()
        rows = [re.fullmatch(r"job_pgrep_exit=([01]) writer_pgrep_exit=([01])", line) for line in lines]
        rows = [row for row in rows if row]
        need(len(rows) == 1)
        states = [line.split("=", 1)[1] for line in lines if line in (
            "block_stdout=present", "block_stdout=missing")]
        need(len(states) == 1)
        return {"job_pgrep_exit": int(rows[0][1]), "writer_pgrep_exit": int(rows[0][2]),
                "block_stdout": states[0]}
    if mode == "check":
        text = raw.decode("ascii").strip()
        if text.startswith("{"):
            value = json.loads(text)
            if isinstance(value, dict) and value.get("status") == "refused" and "code" in value:
                return {"status": "refused", "code": enum(value["code"], EXTRACTOR_CODES)}
        known = {"identity_baseline_ok", "unchanged", "clean_inventory_and_identity", "present_and_empty",
                 "only_plain_certifications", "no_locks", "no_locks_contract_directory_absent", "reconciled",
                 "completed", "failed", "refused", "uncertain", "unknown", "no_json",
                 "lock_note_matches", "lock_note_mismatch", "settings_equal_except_status_and_frozen_on",
                 "notice_recorded", "consent_recorded", "recovery_attempt_current", "parent_census_equal"}
        if text in known:
            return {"code": text}
        if not text:
            return {"code": "empty_output"}
        match = re.fullmatch(r"([0-9a-f]{40}) tree ([0-9a-f]{40})", text)
        if match:
            return {"commit": match[1], "tree": match[2]}
        return {"classification": "unclassified"}
    if mode == "census":
        rows = [json.loads(line) for line in raw.splitlines()]
        known = {"incoming", "raw", "manifests", "index", "certifications", "contracts", "runs", "work",
                 "trial-ledger-v1.jsonl", "runner.lock", "trial-ledger-v1.jsonl.lock",
                 "head-v1.json", "markets-v1.jsonl", "historic", "lab-state", "historic-pro", "suite.noindex"}
        out = []
        for row in rows:
            need(isinstance(row, dict))
            if "name" in row:
                name = row["name"]
                safe = name in known
                mode_text = row.get("mode", "")
                kind = mode_text[:1] if isinstance(mode_text, str) else "?"
                out.append({"name": name if safe else "unclassified", "bytes": count(row.get("size")),
                            "type": {"-": "file", "d": "directory", "l": "symlink"}.get(kind, "other")})
        status = enum(rows[-1].get("status"), {"absent", "present_and_empty", "present_with_entries",
                                              "refused", "uninspectable"})
        reason = rows[-1].get("reason")
        return {"status": status, "entries": out, "reason_code": enum(reason, CENSUS_REASONS) if reason else None}
    value = json.loads(raw)
    need(isinstance(value, dict))
    if (mode in {"certificate", "binding", "l2", "l3", "inputs", "entries", "identity"}
            or (mode == "ledger" and isinstance(value.get("code"), str) and value["code"] in EXTRACTOR_CODES)):
        if value.get("status") == "refused" and "code" in value:
            return {"status": "refused", "code": enum(value["code"], EXTRACTOR_CODES)}
    if mode == "contract":
        problems = value.get("problems")
        need(isinstance(problems, list))
        return {"status": enum(value.get("status"), {"valid_draft", "valid_frozen", "invalid"}),
                "contract_sha256": digest(value.get("contract_sha256")), "problem_count": len(problems)}
    if mode == "space":
        return {"available_kib": count(value.get("available_kib")),
                "markets_bytes": count(value.get("markets_bytes"))}
    if mode == "layout":
        return {"status": enum(value.get("status"), {"accepted", "refused"}),
                "reason": enum(value.get("reason"), LAYOUT_REASONS)}
    if mode == "entries":
        allowed = {"trial-ledger-v1.jsonl", "runner.lock", "trial-ledger-v1.jsonl.lock"}
        need(isinstance(value.get("entries"), list) and isinstance(value.get("locks"), list))
        return {"directory_status": enum(value.get("directory_status"), {"absent", "present"}),
                "entries": [enum(name, allowed) for name in value["entries"]],
                "locks": [enum(name, allowed - {"trial-ledger-v1.jsonl"}) for name in value["locks"]]}
    if mode == "l2":
        output = {"status": enum(value.get("status"), {"indexed", "empty", "refused"})}
        archives = value.get("archives")
        need(archives is None or isinstance(archives, list))
        output["archives"] = None if archives is None else [{
            **{key: digest(item.get(key)) for key in ("archive_sha256", "index_sha256", "markets_sha256")},
            **{key: count(item.get(key)) for key in (
                "markets", "markets_refused", "files", "files_indexed", "files_unreadable", "files_refused")},
            "state": enum(item.get("state"), {"unchanged", "written"})} for item in archives]
        problems = value.get("problems")
        need(problems is None or isinstance(problems, list))
        output["problem_codes"] = None if problems is None else [code(p) for p in problems]
        return output
    if mode == "l3":
        naming = value.get("naming")
        need(naming is None or isinstance(naming, dict))
        safe_naming = None
        if naming is not None:
            identity = naming.get("code_identity")
            need(isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{40} tree [0-9a-f]{40}", identity))
            safe_naming = {**{key: digest(naming.get(key)) for key in (
                "source_descriptor_sha256", "readiness_source_sha256", "gate_snapshot_sha256")},
                "code_identity_sha256": identity_hash(identity), "interpreter": interpreter(naming.get("interpreter"))}
        records = value.get("records")
        need(records is None or isinstance(records, dict))
        sources = value.get("source_refusals")
        need(sources is None or isinstance(sources, dict))
        source_keys = ("members_refused_at_ingest_by_reason", "index_files_not_indexed_by_status_reason",
                       "market_rows_by_reason", "distinct_markets_by_reason")
        file_reasons = {s + ":" + reason for s in ("refused", "unreadable", "other") for reason in REASONS}
        return {"status": enum(value.get("status"), {"completed", "completed_with_problems", "refused"}),
                "refusal_code": code(value.get("refusal")), "naming": safe_naming,
                "input": input_descriptor(value.get("input")), "input_sha256": digest(value.get("input_sha256")),
                "records": None if records is None else {k: count(records.get(k)) for k in (
                    "lines", "classified", "with_problems")},
                "record_problems": count_map(value.get("record_problems"), STAGES),
                "input_preflight_code": code(value.get("input_preflight_would_refuse")),
                "population": population(None if value.get("population") is None else {
                    **value["population"], "records": None if records is None else records.get("lines")}),
                "records_by_plan": count_map(value.get("records_by_plan"), {"BASIC", "ADVANCED", "PRO", "invalid"}),
                "prefix_candidates": count(value.get("prefix_candidates")),
                "source_refusals": None if sources is None else {
                    k: count_map(sources.get(k), file_reasons if "status_reason" in k else REASONS)
                    for k in source_keys}}
    if mode in {"run", "recovery"}:
        statuses = {"uncertain", "refused", "reconciled"} if mode == "recovery" else {
            "completed", "failed", "refused", "uncertain"}
        output = {"status": enum(value.get("status"), statuses),
                  "runner_lock_path_matches": (
                      None if value.get("runner_lock") is None else value.get("runner_lock") == lock),
                  "failure_code": code(value.get("failure")), "ledger_head": head(value.get("ledger_head"))}
        if mode == "run":
            acceptance = value.get("engineering_acceptance")
            output["engineering_acceptance"] = None if acceptance is None else enum(
                acceptance, {"passed", "failed", "incomplete"})
            output["cumulative_starts"] = count(value.get("cumulative_starts"))
        problems = value.get("problems")
        need(problems is None or isinstance(problems, list))
        output["problem_codes"] = None if problems is None else [code(p) for p in problems]
        for key in ("trial_ids", "runs_touched", "aborted", "recorded_runs", "receipted"):
            output[key + "_count"] = sequence_count(value.get(key))
        for key in ("started", "closed"):
            output[key] = variant_list(value.get(key))
        need(value.get("lock_release") in {None, "failed"})
        output["lock_release"] = value.get("lock_release")
        receipt = value.get("receipt")
        if receipt is not None:
            need(isinstance(receipt, dict))
            trials = receipt.get("trials")
            need(trials is None or isinstance(trials, list))
            binding = receipt.get("engineering_acceptance")
            need(binding is None or isinstance(binding, dict))
            output["receipt"] = {
                "status": enum(receipt.get("status"), {"completed", "failed", "aborted"}),
                "acceptance_status": None if binding is None else enum(
                    binding.get("status"), {"passed", "failed", "incomplete"}),
                "failure_code": code(receipt.get("failure")), "ledger_head": head(receipt.get("ledger_head")),
                **{key: digest(receipt.get(key)) for key in ("contract_sha256", "certificate_sha256")},
                "trial_heads": None if trials is None else [
                    {key: head(trial.get(key)) for key in ("started", "closed")} for trial in trials]}
        return output
    if mode in {"certificate", "certify"}:
        counts = value.get("counts")
        need(counts is None or isinstance(counts, dict))
        output = {"status": enum(value.get("status"), {
            "certified", "already_certified", "not_certified", "refused", "failed", "uncertain"}),
            "counts": None if counts is None else {key: count(counts.get(key)) for key in (
                "cases", "as_expected", "not_as_expected", "error")}, "key": digest(value.get("key"))}
        if mode == "certificate":
            output.update(sha256=digest(value.get("sha256")), source={"sha256": digest(value["source"]["sha256"])},
                          interpreter=interpreter(value.get("interpreter")))
        else:
            problems = value.get("problems")
            need(problems is None or isinstance(problems, list))
            output.update(certificate_sha256=digest(value.get("certificate_sha256")),
                          source_descriptor_sha256=digest(value.get("source_descriptor_sha256")),
                          failed_cases_count=sequence_count(value.get("failed_cases")),
                          problem_codes=None if problems is None else [code(p) for p in problems])
        return output
    if mode == "binding":
        acceptance = {"passed", "failed", "incomplete"}
        cert = value.get("certificate")
        need(isinstance(cert, dict))
        criteria = value.get("engineering_acceptance")
        need(isinstance(criteria, dict))
        criterion_names = {"known_accounting_answers", "issuance_exposure_reconciliation", "prefix_invariance"}
        need(set(criteria) == criterion_names)
        problem_codes = set("""known_answer_mismatch settlement:not_a_settlement settlement:other_variants
records:duplicate ledger:not_the_decisions bets:not_the_issued_rows settled:not_the_issued_rows issuance:error
metrics:error rows:variant_set decisions:ineligible_with_rows rows:decision_mismatch decisions:missing
decisions:unknown_record bets:duplicate settled:duplicate reconciliation:issued_count_mismatch
reconciliation:exposure_mismatch counts_do_not_reconcile failed_against_record comparison_failed missing_records
unexercised no_comparisons unissued_variants synthetic_not_passed unclassified_problem""".split())
        projected = {}
        for key, criterion in criteria.items():
            need(isinstance(criterion, dict) and isinstance(criterion.get("problem_codes"), list))
            projected[key] = {"status": enum(criterion.get("status"), acceptance), "problem_codes": [
                enum(item, problem_codes) for item in criterion["problem_codes"]]}
        tally = value.get("prefix_tally")
        need(isinstance(tally, dict))
        prefix_reasons = {"prefix_buffer_limit"} | {"unknown_pt_before_boundary:" + name for name in (
            "line_too_long", "decompressed_too_large", "not_json", "malformed_message",
            "invalid_or_decreasing_pt", "not_bz2")}
        counts = {key: count(tally.get(key)) for key in ("records", "candidates", "compared", "passed", "failed",
                                                       "missing")}
        starts = value.get("started")
        need(starts is None or isinstance(starts, list))
        selected = None
        if starts is not None:
            selected = [{"variant": enum(entry.get("variant"), VARIANTS),
                         "code_identity_sha256": identity_hash(entry["code_identity"]),
                         **{key: digest(entry.get(key)) for key in (
                             "input_sha256", "contract_sha256", "certificate_sha256")}} for entry in starts]
        return {"input": input_descriptor(value.get("input")), "input_sha256": digest(value.get("input_sha256")),
                "population": population(value.get("population")),
                "code_identity_sha256": identity_hash(value["code_identity"]),
                "contract_sha256": digest(value.get("contract_sha256")),
                "certificate": {key: digest(cert.get(key)) for key in ("key", "sha256")},
                "acceptance_status": enum(value.get("acceptance_status"), acceptance), "started": selected,
                "engineering_acceptance": projected,
                "prefix_tally": {**counts, "unexercised_by_reason": count_map(
                    tally.get("unexercised_by_reason"), prefix_reasons),
                    "unissued_variants": variant_list(tally.get("unissued_variants"))},
                "unexercised_variants": variant_list(value.get("unexercised_variants"))}
    if mode == "identity":
        return {"key": digest(value.get("key")),
                "source_descriptor_sha256": digest(value.get("source_descriptor_sha256")),
                "gate_snapshot_sha256": digest(value.get("gate_snapshot_sha256")),
                "interpreter": interpreter(value.get("interpreter"))}
    if mode == "inputs":
        files = value.get("files")
        need(isinstance(files, dict))
        return {"archive_sha256": digest(value.get("archive_sha256")), "files": {
            key: {"sha256": digest(files[key].get("sha256")), "bytes": count(files[key].get("bytes")),
                  "lines": count(files[key].get("lines"))} for key in ("manifest", "head", "markets")}}
    if mode == "ledger":
        status = enum(value.get("status"), {"verified", "refused"})
        return {"status": status, **ledger_code(value.get("code")), "entries": count(value.get("entries")),
                "by_kind": count_map(value.get("by_kind"), {"started", "completed", "failed", "aborted"}),
                "head": head(value.get("head"))}
    raise ValueError("publication:unknown_mode")


def command_census(args):
    argv = ["census", *args]
    def stop(code):
        print(json.dumps({"status": "refused", "reason": code}))
        sys.exit(2)
    p = Path(argv[1])
    if not p.is_absolute() or ".." in p.parts:
        stop("target:not_an_absolute_normal_path")
    try:
        for parent in reversed(p.parents):
            s = os.lstat(parent)
            if not stat.S_ISDIR(s.st_mode):
                stop("parent:not_a_plain_directory")
        print(json.dumps({"target": str(p), "parent_realpath": os.path.realpath(p.parent, strict=True)}))
        try:
            s = os.lstat(p)
        except FileNotFoundError:
            print(json.dumps({"status": "absent"}))
            sys.exit(0)
        print(json.dumps({"path": str(p), "mode": stat.filemode(s.st_mode), "size": s.st_size}))
        if not stat.S_ISDIR(s.st_mode):
            stop("target:not_a_plain_directory")
        print(json.dumps({"realpath": os.path.realpath(p, strict=True)}))
        n = 0
        with os.scandir(p) as entries:
            for entry in entries:
                n += 1
                if n > 200:
                    stop("listing:over_200_entries")
                info = entry.stat(follow_symlinks=False)
                print(json.dumps({"name": entry.name, "mode": stat.filemode(info.st_mode), "size": info.st_size}))
        print(json.dumps({"status": "present_and_empty" if n == 0 else "present_with_entries", "entries": n}))
    except OSError as error:
        print(json.dumps({"status": "uninspectable", "errno": error.errno}))
        sys.exit(2)


def command_paths(args):
    argv = ["paths", *args]
    checkout, *targets = map(Path, argv[1:])
    def need(ok, code):
        if not ok:
            sys.exit(code)
    def parts(path):
        return tuple(p.casefold() for p in path.parts)
    def overlaps(a, b):
        x, y = parts(a), parts(b)
        return x[:len(y)] == y or y[:len(x)] == x
    try:
        raw = subprocess.check_output(["/usr/bin/git", "-C", str(checkout), "worktree", "list", "--porcelain", "-z"],
                                      stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError):
        sys.exit("paths:git_failed")
    worktrees = [Path(os.fsdecode(v[9:])).resolve(strict=True) for v in raw.split(b"\0") if v.startswith(b"worktree ")]
    denied = (("library", "application support", "betangel-research", "betfair"),
              (".config", "betangel-research", "betfair"))
    for path in targets:
        need(path.is_absolute() and ".." not in path.parts, "paths:not_absolute_normal")
        for parent in reversed(path.parents):
            need(stat.S_ISDIR(parent.lstat().st_mode), "paths:parent_not_plain")
        p = parts(path)
        need(not any(p[i:i + len(d)] == d for d in denied for i in range(len(p))), "paths:denied_tree")
        need(not any(overlaps(path, tree) for tree in worktrees), "paths:worktree_overlap")
    for i, path in enumerate(targets):
        need(not any(overlaps(path, other) for other in targets[:i]), "paths:root_overlap")
    print(json.dumps({"targets": list(map(str, targets)), "worktrees": list(map(str, worktrees)),
                      "component_check": "disjoint"}))


def command_new_prefix(args):
    argv = ["new_prefix", *args]
    prefix = argv[2]
    need(re.fullmatch(PREFIX_PATTERN, prefix) is not None)
    for name in os.listdir(argv[1]):
        if name.startswith((prefix + "-", prefix + ".", "session-" + prefix + "-", "session-" + prefix + ".")):
            sys.exit("evidence:prefix_already_used")


def command_lock_entries(args):
    argv = ["lock_entries", *args]
    path = Path(argv[1])
    allowed = {"trial-ledger-v1.jsonl", "runner.lock", "trial-ledger-v1.jsonl.lock"}
    need(not path.is_symlink())
    if not path.exists():
        print(json.dumps({"directory_status": "absent", "entries": [], "locks": []}))
        sys.exit(0)
    found = {e.name: e.stat(follow_symlinks=False).st_mode for e in os.scandir(path)}
    if not set(found) <= allowed or not all(stat.S_ISREG(mode) for mode in found.values()):
        sys.exit("recovery:unexpected_entry_or_type")
    print(json.dumps({"directory_status": "present", "entries": sorted(found),
                      "locks": sorted(k for k in found if k.endswith(".lock"))}))


def command_lock_match(args):
    argv = ["lock_match", *args]
    text, run_id, code = Path(argv[1]).read_text().strip(), argv[2], argv[3]
    matched = text in {"run " + run_id + " " + code, "reconcile " + code}
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        matched = (value.get("kind") in {"started", "completed", "failed", "aborted"}
                   and value.get("code_identity") == code
                   and value.get("trial_id") in {run_id + "." + v for v in ("ltp_favourite", "seeded_random_runner")})
    print("lock_note_matches" if matched else "lock_note_mismatch")
    sys.exit(0 if matched else 2)


def command_no_locks(args):
    argv = ["no_locks", *args]
    root = Path(argv[1])
    for path in (*reversed(root.parents), root, root / "contracts", root / "contracts" / argv[2]):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            if path in (root / "contracts", root / "contracts" / argv[2]):
                print("no_locks_contract_directory_absent")
                sys.exit(0)
            raise
        if not stat.S_ISDIR(mode):
            sys.exit("recovery:directory_not_plain")
    if any(e.name.endswith(".lock") for e in os.scandir(path)):
        sys.exit("recovery:lock_remains")
    print("no_locks")


def command_inputs(args):
    argv = ["inputs", *args]
    root, archive, expected = Path(argv[1]), argv[2], argv[3]
    def need(ok, code):
        if not ok:
            sys.exit(code)
    def entries(path, wanted):
        found = {e.name: e.stat(follow_symlinks=False).st_mode for e in os.scandir(path)}
        need(set(found) == set(wanted), "inputs:entry_set")
        need(all(test(found[name]) for name, test in wanted.items()), "inputs:entry_type")
    need(str(root.resolve(strict=True)) == str(root), "inputs:root_changed")
    entries(root, {k: stat.S_ISDIR for k in ("incoming", "raw", "manifests", "index")})
    entries(root / "manifests", {archive + ".json": stat.S_ISREG})
    entries(root / "index", {archive: stat.S_ISDIR})
    entries(root / "index" / archive, {k: stat.S_ISREG for k in ("head-v1.json", "markets-v1.jsonl")})
    paths = {"manifest": root / "manifests" / (archive + ".json"),
             "head": root / "index" / archive / "head-v1.json",
             "markets": root / "index" / archive / "markets-v1.jsonl"}
    files = {}
    for name, path in paths.items():
        h, lines = hashlib.sha256(), 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                h.update(chunk)
                lines += chunk.count(b"\n")
        files[name] = {"path": str(path), "sha256": h.hexdigest(), "bytes": path.stat().st_size}
        if name == "markets":
            files[name]["lines"] = lines
    if expected != "-":
        l3 = json.loads(Path(expected).read_bytes())
        need(len(l3["input"]["archives"]) == 1, "inputs:archive_count")
        a = l3["input"]["archives"][0]
        need(a["archive_sha256"] == archive, "inputs:archive_changed")
        for name, key in (("manifest", "manifest_sha256"), ("head", "index_sha256"), ("markets", "markets_sha256")):
            need(files[name]["sha256"] == a[key], "inputs:hash_changed:" + name)
        need(files["markets"]["lines"] == a["markets"], "inputs:line_count_changed")
    print(json.dumps({"data_root": str(root), "archive_sha256": archive, "files": files}, sort_keys=True))


def command_identity_gate(args):
    argv = ["identity_gate", *args]
    value = json.loads(Path(argv[1]).read_bytes())
    if (value["no_site"], value["enable_user_site"], value["pycache_prefix"],
            value["sitecustomize"], value["usercustomize"]) != (1, None, None, False, False):
        sys.exit("identity:site_or_pycache")
    if not set(value["python_env"]) <= {"PYTHONDONTWRITEBYTECODE"}:
        sys.exit("identity:python_env")
    if value["sys_path"] != [""] + sys.path:
        sys.exit("identity:sys_path")
    print("identity_baseline_ok")


def command_l2_check(args):
    argv = ["l2_check", *args]
    value = json.loads(Path(argv[1]).read_bytes())
    if value.get("status") != "indexed" or len(value.get("archives", [])) != 1:
        sys.exit("index:not_one_indexed_archive")
    a = value["archives"][0]
    if a["state"] != "unchanged" or a["archive_sha256"] != argv[2]:
        sys.exit("index:not_the_expected_unchanged_archive")
    print("unchanged")


def command_l3_check(args):
    argv = ["l3_check", *args]
    l3, identity = [json.loads(Path(name).read_bytes()) for name in argv[1:3]]
    if (l3["status"], l3["refusal"], l3["input_preflight_would_refuse"], l3["records"]["with_problems"]) != (
            "completed", None, None, 0):
        sys.exit("inventory:not_clean")
    if l3["naming"]["code_identity"] != argv[3]:
        sys.exit("inventory:code_identity")
    for key in ("source_descriptor_sha256", "interpreter", "gate_snapshot_sha256"):
        if l3["naming"][key] != identity[key]:
            sys.exit("inventory:identity_mismatch:" + key)
    print("clean_inventory_and_identity")


def command_freeze_diff(args):
    argv = ["freeze_diff", *args]
    before, after = [json.loads(Path(p).read_bytes()) for p in argv[1:]]
    if (before["status"], before["frozen_on"], after["status"]) != ("draft", None, "frozen"):
        sys.exit("contract:not_the_expected_transition")
    for value in (before, after):
        del value["status"], value["frozen_on"]
    if before != after:
        sys.exit("contract:settings_changed")
    print("settings_equal_except_status_and_frozen_on")


def command_certificate_check(args):
    argv = ["certificate_check", *args]
    state, key, attempt = Path(argv[1]), argv[2], argv[3]
    def need(ok, code):
        if not ok:
            sys.exit(code)
    output, l3, identity = [json.loads(Path(name).read_bytes()) for name in argv[4:7]]
    counts = {"cases": 38, "as_expected": 38, "not_as_expected": 0, "error": 0}
    need(output["status"] == "certified" and output["counts"] == counts, "certificate:not_new_38_of_38")
    need(not output["failed_cases"] and not output["problems"], "certificate:problems")
    path = state / "certifications" / (key + ".json")
    need(output["key"] == key and output["certificate_path"] == str(path), "certificate:path_or_key")
    need(stat.S_ISREG(path.lstat().st_mode), "certificate:not_regular")
    raw = path.read_bytes()
    sha, stored = hashlib.sha256(raw).hexdigest(), json.loads(raw)
    need(sha == output["certificate_sha256"], "certificate:bytes")
    need(stored["key"] == key and stored["status"] == "certified", "certificate:stored_status_or_key")
    need(stored["counts"] == counts, "certificate:stored_counts")
    need(stored["source"]["sha256"] == l3["naming"]["source_descriptor_sha256"] ==
         identity["source_descriptor_sha256"] == output["source_descriptor_sha256"], "certificate:source")
    need(stored["interpreter"] == l3["naming"]["interpreter"] == identity["interpreter"], "certificate:interpreter")
    print(json.dumps({"path": str(path), "sha256": sha, "key": key, "status": stored["status"], "counts": counts,
                      "source": {"sha256": stored["source"]["sha256"]}, "interpreter": stored["interpreter"],
                      "attempt_path": str(state / "certifications" / "work" / attempt)}, sort_keys=True))


def command_run_binding(args):
    argv = ["run_binding", *args]
    evidence, state, contract = map(Path, argv[1:4])
    digest, run_id, code = argv[4:7]
    run_rc = int(argv[7])
    def need(ok, reason):
        if not ok:
            sys.exit(reason)
    def plain(path):
        for parent in reversed(path.parents):
            need(stat.S_ISDIR(parent.lstat().st_mode), "binding:parent_not_plain")
        need(stat.S_ISREG(path.lstat().st_mode), "binding:file_not_regular")
        return path
    def sha(path):
        with plain(path).open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    def read(path):
        return json.loads(plain(path).read_bytes())
    l3, cert, output = [read(Path(name)) for name in argv[8:11]]
    run = state / "runs" / digest / run_id
    paths = {"results": run / "results.json", "receipt": run / "receipt.json",
             "issuance": run / "issuance-ledger.jsonl",
             "trial_ledger": state / "contracts" / digest / "trial-ledger-v1.jsonl"}
    hashes = {name: {"path": str(path), "sha256": sha(path)} for name, path in paths.items()}
    result, receipt, frozen = read(paths["results"]), read(paths["receipt"]), read(contract)
    with plain(paths["trial_ledger"]).open() as handle:
        entries = [json.loads(line) for line in handle]
    starts = [e for e in entries if e["kind"] == "started" and e["trial_id"].startswith(run_id + ".")]
    need(output["status"] == receipt["status"] == "completed", "binding:not_completed")
    need(result["run_id"] == receipt["run_id"] == run_id, "binding:run_id")
    need(result["contract_sha256"] == receipt["contract_sha256"] == digest, "binding:contract")
    need(result["code_identity"] == code, "binding:code")
    need(result["input"] == l3["input"] and result["input_sha256"] == l3["input_sha256"], "binding:input")
    pop = {k: result["population"][k] for k in ("records", "by_class", "by_reason")}
    need(l3["records"]["with_problems"] == 0 and pop["records"] == l3["records"]["lines"], "binding:records")
    need(all(pop[k] == l3["population"][k] for k in ("by_class", "by_reason")), "binding:population")
    need(result["certification"]["key"] == cert["key"], "binding:certificate_key")
    need(result["certification"]["sha256"] == receipt["certificate_sha256"] == cert["sha256"], "binding:certificate")
    need(sha(Path(cert["path"])) == cert["sha256"], "binding:stored_certificate")
    need(receipt["results_sha256"] == output["results_sha256"] == hashes["results"]["sha256"], "binding:results")
    need(output["receipt_sha256"] == hashes["receipt"]["sha256"], "binding:receipt")
    need(receipt["issuance_ledger_sha256"] == hashes["issuance"]["sha256"], "binding:issuance")
    need(receipt["issuance_ledger_bytes"] == paths["issuance"].stat().st_size, "binding:issuance_size")
    status = result["engineering_acceptance"]["status"]
    need(status in {"passed", "failed", "incomplete"}, "binding:unknown_acceptance")
    need(status == receipt["engineering_acceptance"]["status"] == output["engineering_acceptance"],
         "binding:acceptance_mismatch")
    need(run_rc == (0 if status == "passed" else 4), "binding:exit_mismatch")
    variants = {v["id"] for v in frozen["variants"]}
    need(len(starts) == len(variants) and {e["variant"] for e in starts} == variants, "binding:starts")
    selected = []
    for entry in starts:
        need(entry["trial_id"] == run_id + "." + entry["variant"], "binding:trial_id")
        need(entry["input_sha256"] == l3["input_sha256"] and entry["contract_sha256"] == digest, "binding:start_input")
        need(entry["code_identity"] == code and entry["summary"]["certificate_sha256"] == cert["sha256"],
             "binding:start_code_or_certificate")
        selected.append({**{k: entry[k] for k in (
            "trial_id", "variant", "input_sha256", "code_identity", "contract_sha256")},
                         "certificate_sha256": entry["summary"]["certificate_sha256"]})
    criteria = result["engineering_acceptance"]["criteria"]
    need(set(criteria) == {"known_accounting_answers", "issuance_exposure_reconciliation", "prefix_invariance"},
         "binding:criteria_set")
    need(all(c["status"] in {"passed", "failed", "incomplete"} for c in criteria.values()), "binding:criterion_status")
    projection = {name: {"status": c["status"], "problem_codes": []} for name, c in criteria.items()}
    kat, rec, pref = (criteria[k] for k in (
        "known_accounting_answers", "issuance_exposure_reconciliation", "prefix_invariance"))
    if kat["failed_cases"]:
        projection["known_accounting_answers"]["problem_codes"] = ["known_answer_mismatch"]
    def problem_code(value):
        # Output constants only: exception tails, counts, decision ids and variant data never escape.
        exact = {"settlement:not_a_settlement", "settlement:other_variants", "records:duplicate",
                 "ledger:not_the_decisions", "bets:not_the_issued_rows", "settled:not_the_issued_rows"}
        if value in exact:
            return value
        patterns = ((r"issuance:.*", "issuance:error"), (r"metrics:.*", "metrics:error"),
                    (r"rows:[0-9]+_without_exactly_the_contract_variants", "rows:variant_set"),
                    (r"decisions:[0-9]+_not_eligible_with_rows", "decisions:ineligible_with_rows"),
                    (r"rows:[0-9]+_not_their_decisions", "rows:decision_mismatch"),
                    (r"decisions:missing_for_[0-9]+_records", "decisions:missing"),
                    (r"decisions:[0-9]+_without_an_input_record", "decisions:unknown_record"),
                    (r"bets:duplicate:.*", "bets:duplicate"), (r"settled:duplicate:.*", "settled:duplicate"),
                    (r"[^:]+:issued_not_resolved_plus_unresolved", "reconciliation:issued_count_mismatch"),
                    (r"[^:]+:issued_stake_not_s_r_plus_s_u", "reconciliation:exposure_mismatch"))
        return next((code for pattern, code in patterns if re.fullmatch(pattern, value)), "unclassified_problem")
    projection["issuance_exposure_reconciliation"]["problem_codes"] = sorted(set(map(problem_code, rec["problems"])))
    tally = pref["components"]["primary_source_prefix_consistency"]
    counts = {k: tally["counts"][k] for k in ("records", "candidates", "compared", "passed", "failed", "missing")}
    need(all(type(v) is int and v >= 0 for v in counts.values()), "binding:prefix_counts")
    need(counts["records"] == l3["records"]["lines"], "binding:prefix_records")
    reasons = tally["unexercised_by_reason"]
    allowed_reasons = {"prefix_buffer_limit"} | {"unknown_pt_before_boundary:" + r for r in (
        "line_too_long", "decompressed_too_large", "not_json", "malformed_message",
        "invalid_or_decreasing_pt", "not_bz2")}
    need(set(reasons) <= allowed_reasons and all(type(v) is int and v >= 0 for v in reasons.values()),
         "binding:prefix_reasons")
    need(set(rec["unexercised_variants"]) <= variants and set(tally["unissued_variants"]) <= variants,
         "binding:unexercised_variants")
    checks = ((not tally["counts_reconcile"], "counts_do_not_reconcile"),
              (tally["counts"]["failed_against_record"] > 0, "failed_against_record"),
              (counts["failed"] > 0, "comparison_failed"), (counts["missing"] > 0, "missing_records"),
              (bool(reasons), "unexercised"), (counts["compared"] == 0, "no_comparisons"),
              (bool(tally["unissued_variants"]), "unissued_variants"),
              (pref["components"]["synthetic_certification"]["status"] != "passed", "synthetic_not_passed"))
    projection["prefix_invariance"]["problem_codes"] = [code for failed, code in checks if failed]
    print(json.dumps({"artifacts": hashes, "input": result["input"], "input_sha256": result["input_sha256"],
                      "population": pop, "code_identity": code, "contract_sha256": digest,
                      "certificate": {"key": cert["key"], "sha256": cert["sha256"]},
                      "acceptance_status": status, "started": selected, "engineering_acceptance": projection,
                      "prefix_tally": {**counts, "unexercised_by_reason": reasons,
                                       "unissued_variants": tally["unissued_variants"]},
                      "unexercised_variants": rec["unexercised_variants"]}, sort_keys=True))


def command_run_state(args):
    argv = ["run_state", *args]
    entries = {e.name: e.stat(follow_symlinks=False).st_mode for e in os.scandir(argv[1])}
    if set(entries) != {"certifications"} or not stat.S_ISDIR(entries["certifications"]):
        sys.exit("run:state_not_certifications_only")
    print("only_plain_certifications")


def command_run_status(args):
    argv = ["run_status", *args]
    try:
        path = Path(argv[1])
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError()
        value = json.loads(path.read_bytes())
        status = value.get("status") if isinstance(value, dict) else None
        print(status if isinstance(status, str) and status in {"completed", "failed", "refused", "uncertain"}
              else "unknown")
    except (OSError, ValueError):
        print("no_json")


IDENTITY_CODE = """import hashlib, json, os, site, sys
from dataclasses import asdict
from betangel_research.betfair.backtest import certificate as c
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, gate_snapshot
from betangel_research.betfair.backtest.contract import canonical_json
key, _, source, interpreter = c.current_key()
gate = gate_snapshot()
if os.getcwd() != sys.argv[1] or str(c.source_root()) != sys.argv[1]:
    print(json.dumps({"status": "refused", "code": "identity:not_the_dedicated_checkout"}))
    sys.exit(2)
print(json.dumps({"executable": sys.executable, "executable_realpath": os.path.realpath(sys.executable),
                  "cwd": os.getcwd(), "source_root": str(c.source_root()),
                  "python_env": {k: v for k, v in sorted(os.environ.items()) if k.startswith("PYTHON")},
                  "pycache_prefix": sys.pycache_prefix, "no_user_site": sys.flags.no_user_site,
                  "no_site": sys.flags.no_site, "enable_user_site": site.ENABLE_USER_SITE,
                  "sys_path": sys.path, "sitecustomize": "sitecustomize" in sys.modules,
                  "usercustomize": "usercustomize" in sys.modules,
                  "interpreter": interpreter, "key": key, "source_descriptor_sha256": source,
                  "read_limits": asdict(DEFAULT_LIMITS), "gate_snapshot": gate,
                  "gate_snapshot_sha256": hashlib.sha256(canonical_json(gate)).hexdigest()}, sort_keys=True))
"""
LEDGER_CODE = """import collections, json, re, sys
from betangel_research.betfair.backtest.trial_ledger import LedgerError, verify
try:
    entries = verify(sys.argv[1])
except LedgerError as error:
    code = str(error) if re.fullmatch(r"(line [0-9]+:)?[a-z0-9_:]+", str(error)) else "unclassified"
    print(json.dumps({"status": "refused", "code": code}))
    sys.exit(2)
print(json.dumps({"status": "verified", "entries": len(entries),
                  "by_kind": dict(collections.Counter(e["kind"] for e in entries)),
                  "head": [entries[-1]["seq"], entries[-1]["entry_sha256"]]}))
"""


# The literal registry is shared by publication, generated-name checks and tests.
PREFIX_PATTERN = (r"(?:step[0-9]+[a-z]?|l[123]|step5b-return|step5-fetch|step9-verify|step9-recheck|"
                  r"step9-ledger|step9-runner|restore)-[0-9]+")
FREEZE_PATTERN = r"freeze-(?:c0-draft|c0-flip-only|c1)-[0-9]+"
SUFFIXES = """
census.jsonl entries.json census-compare.txt entries-compare.txt
runner-stat.txt runner-note.txt ledger-stat.txt ledger-note.txt
before-census-.jsonl before-census-_index.jsonl before-census-_index_{archive}.jsonl
before-census-_manifests.jsonl after-census-.jsonl after-census-_index.jsonl
 after-census-_index_{archive}.jsonl after-census-_manifests.jsonl
run-before-census-.jsonl run-before-census-_index.jsonl run-before-census-_index_{archive}.jsonl
run-before-census-_manifests.jsonl after-census.jsonl after-entries.json after-files.json
 after-writers.txt after.jsonl attempt-session.sha before-census.jsonl before-entries.json
before-files.json before-index-sha.txt before-git.txt before.jsonl binding.json blob-compare.txt
c0-contract.json c1-contract.json cert-key.txt certificate-check.json certifications.jsonl
certify.json check.txt checkout.txt consent.sha contract-sha.txt contract.json data-certifications.jsonl
 data-contracts.jsonl data-runs.jsonl data.jsonl diff.txt empty.txt git.txt identity-compare.txt
identity-gate.txt identity.json index-sha.txt index.json input-compare.txt inventory.json
job-session.sha job.block.exit job.block.stderr job.block.stdout job.launch.stderr job.launch.stdout
job.pid launch.txt ledger-lock-check.txt ledger-lock.stat ledger-lock.txt ledger-verify.json
liveness.txt locks-census.jsonl locks-entries.json mkdir.txt no-locks.txt notice-and-consent.md
parent-certifications.jsonl parent-contracts.jsonl parent-runs.jsonl parent.jsonl paths.txt
pre-session.sha realpath.txt recheck-census-compare.txt recheck-census.jsonl recheck-entries-compare.txt
recheck-entries.json recheck-git.txt recheck-ledger-lock-check.txt recheck-ledger-lock.stat
recheck-ledger-lock.txt recheck-ledger-note.txt recheck-ledger-stat.txt recheck-liveness.txt
recheck-runner-lock-check.txt recheck-runner-lock.stat recheck-runner-lock.txt recheck-runner-note.txt
recheck-runner-stat.txt recheck-writers.txt reconcile.json reconciled.txt recovery-check.json
review-session.sha rm-ledger-lock.txt rm-runner-lock.txt run-before-files.json run-git.txt
run-identity-compare.txt run-identity.json run-input-compare.txt run-writers.txt run.json
runner-lock-check.txt runner-lock.stat runner-lock.txt session.sha settings.txt space.txt
state-check.txt state-compare.txt state.jsonl status.txt token-C1.txt token-C1_TREE.txt
 token-CERT_KEY.txt token-CONTRACT_SHA.txt token-RUN_ID.txt worktrees.txt writers.txt compare.txt
notice.md notice.sha token-C0.txt token-C0_TREE.txt token-BASIC_SHA.txt token-L2_INDEX_SHA.txt
layout-check.txt parent-compare.txt fetch.txt tree.txt job-space.txt final-liveness.txt
notice-check.txt token-L2_SPACE_BUDGET_KIB.txt
bundle-check.txt after-git.txt bundle-sha.txt tool-sha.txt shell-sha.txt lab_runbook.py lab_runbook.zsh
""".split()
AUTHOR_SUFFIXES = ("interpreter.json git.txt diff.txt suite.log suite.exit publication.json "
                   "status-before.z status-after.z cache-before.txt cache-after.txt "
                   "index-before.z index-after.z git-after.txt contract-head.json "
                   "status-before.z.stderr status-after.z.stderr cache-before.txt.stderr cache-after.txt.stderr "
                   "index-before.z.stderr index-after.z.stderr").split()
MODES = {"raw", "exit", "liveness", "check", "census", "l2", "l3", "run", "recovery", "certificate",
         "certify", "binding", "identity", "inputs", "ledger", "checkpoint", "entries", "contract", "digest",
         "space", "layout"}


def suffix_mode(suffix):
    if suffix.endswith(".stderr"):
        return "raw"
    if suffix.endswith(".exit"):
        return "exit"
    exact = {"contract.json": "contract", "layout-check.txt": "layout", "parent-compare.txt": "check",
             "settings.txt": "check", "identity-gate.txt": "check"}
    if suffix in exact:
        return exact[suffix]
    endings = (("-sha.txt", "digest"), ("cert-key.txt", "digest"), ("identity.json", "identity"),
               ("index.json", "l2"), ("inventory.json", "l3"), ("files.json", "inputs"),
               ("certificate-check.json", "certificate"), ("certify.json", "certify"),
               ("binding.json", "binding"), ("run.json", "run"), ("reconcile.json", "recovery"),
               ("ledger-verify.json", "ledger"), ("liveness.txt", "liveness"), ("entries.json", "entries"),
               ("space.txt", "space"), (".jsonl", "census"))
    for ending, mode in endings:
        if suffix.endswith(ending):
            return mode
    if suffix.endswith(("git.txt", "status.txt", "check.txt", "empty.txt", "no-locks.txt", "reconciled.txt")):
        return "check"
    return "raw"


def registered_name(name):
    """Return the sole projection mode for a generated filename, or None. Never echo unknown names."""
    if re.fullmatch(r"session-" + PREFIX_PATTERN + r"(?:-pre|-attempt|-review|-job)?\.zsh", name):
        return "checkpoint"
    if re.fullmatch(r"session-" + FREEZE_PATTERN + r"\.zsh", name):
        return "checkpoint"
    if re.fullmatch(r"betangel-runbook-bootstrap-step1-[0-9]+\.(stdout|stderr|exit)", name):
        return "exit" if name.endswith(".exit") else "raw"
    match = re.fullmatch("(" + PREFIX_PATTERN + r")-(.+)", name)
    if match:
        suffix = match[2]
        # Publication artifacts can be reprinted but must never recursively publish themselves.
        if re.fullmatch(r"(?:final|review)-publication\.jsonl(?:\.stderr|\.exit)?", suffix):
            return "publication"
        for base in SUFFIXES:
            pattern = re.escape(base).replace(r"\{archive\}", r"[0-9a-f]{64}")
            direct = base.endswith((".stderr", ".stdout", ".exit", ".pid", ".md"))
            if re.fullmatch(pattern + ("" if direct else r"(?:\.stderr|\.exit)?"), suffix):
                return suffix_mode(suffix)
        return None
    match = re.fullmatch("(" + FREEZE_PATTERN + r")-(.+)", name)
    if match and match[2] in AUTHOR_SUFFIXES:
        return "raw"
    return None


def publish(mode, filename, lock="", public_name=None):
    """Print one safe JSON projection, including a fixed refusal on every anticipated failure."""
    public_name = public_name or "unclassified-evidence"
    registered = registered_name(public_name)
    if mode not in MODES or registered not in {mode, "run" if mode == "recovery" else mode}:
        print(json.dumps({"status": "unclassified", "code": "publication:unregistered_name",
                          "raw_report": {"classification": "unclassified", "bytes": None,
                                         "lab_local_file": "unclassified-evidence"}}, sort_keys=True))
        return 2
    path = Path(filename)
    metadata = {"classification": "unclassified", "bytes": None, "lab_local_file": public_name}
    envelope = {"raw_report": metadata}
    # Captured stderr is itself a terminal artifact: never manufacture .stderr.stderr.
    stderr_name = public_name + ".stderr"
    if registered_name(stderr_name) == "raw":
        stderr = {"classification": "unclassified", "bytes": None, "lab_local_file": stderr_name}
        try:
            with os.fdopen(os.open(path.with_name(path.name + ".stderr"),
                                   os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
                info = os.fstat(handle.fileno())
                if stat.S_ISREG(info.st_mode):
                    stderr["bytes"] = info.st_size
        except OSError:
            pass
        envelope["stderr"] = stderr
    try:
        need(path.name == public_name)
        # One nonblocking, no-follow descriptor binds metadata, type, cap and bytes.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
            info = os.fstat(handle.fileno())
            metadata["bytes"] = info.st_size
            need(stat.S_ISREG(info.st_mode))
            if mode == "raw":
                print(json.dumps({"classification": "unclassified", **envelope}, sort_keys=True))
                return 0
            limit = 32 * 1024 * 1024
            need(info.st_size <= limit)
            raw = handle.read(limit + 1)
            need(len(raw) <= limit)
        if mode in {"run", "recovery"}:
            try:
                json.loads(raw)
            except (ValueError, UnicodeError):
                print(json.dumps({"status": "no_json", "code": "publication:unclassified", **envelope},
                                 sort_keys=True))
                return 0
        output = project(mode, raw, lock)
    except FileNotFoundError:
        status = "no_file" if mode in {"run", "recovery"} else "unclassified"
        print(json.dumps({"status": status, "code": "publication:missing_file", **envelope}, sort_keys=True))
        return 2
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, AttributeError, RecursionError):
        print(json.dumps({"status": "unclassified", "code": "publication:unclassified", **envelope},
                         sort_keys=True))
        return 2
    print(json.dumps({**output, **envelope}, sort_keys=True))
    if (mode == "check" and public_name.endswith(("runner-lock-check.txt", "ledger-lock-check.txt"))
            and output.get("code") == "lock_note_mismatch"):
        return 2
    mismatch = (mode in {"run", "recovery"} and output["status"] == "uncertain"
                and not output["runner_lock_path_matches"])
    return 2 if mismatch else 0


def command_publish(args):
    need(len(args) == 4)
    return publish(*args)


def command_publish_attempt(args):
    need(len(args) >= 4)
    evidence, prefix, archive, lock = args[:4]
    need(re.fullmatch(PREFIX_PATTERN, prefix) and re.fullmatch(r"[0-9a-f]{64}", archive))
    options = args[4:]
    need(len(options) % 2 == 0)
    extra = {}
    for option, value in zip(options[::2], options[1::2]):
        need(option in {"--bootstrap", "--recovery-run"} and option not in extra)
        extra[option] = value
    directory, rc = Path(evidence), 0
    names = []
    for suffix in SUFFIXES:
        suffix = suffix.format(archive=archive)
        direct = suffix.endswith((".stderr", ".stdout", ".exit", ".pid", ".md"))
        for extension in ("",) if direct else ("", ".stderr", ".exit"):
            names.append(prefix + "-" + suffix + extension)
    names += ["session-" + prefix + suffix + ".zsh" for suffix in ("", "-pre", "-attempt", "-review", "-job")]
    for name in dict.fromkeys(names):
        path = directory / name
        if path.exists() or path.is_symlink():
            code = publish(registered_name(name), str(path), lock, name)
            rc = max(rc, code)
    if "--bootstrap" in extra:
        for suffix in ("stdout", "stderr", "exit"):
            path = Path(extra["--bootstrap"] + "." + suffix)
            code = publish("exit" if suffix == "exit" else "raw", str(path), lock, path.name)
            rc = max(rc, code)
    if "--recovery-run" in extra:
        path = Path(extra["--recovery-run"])
        code = publish("run", str(path), lock, path.name)
        rc = max(rc, code)
    return rc


def command_registry(args):
    need(len(args) == 1)
    names = [entry.name for entry in os.scandir(args[0])]
    count_unknown = sum(registered_name(name) is None for name in names)
    print(json.dumps({"status": "registered" if count_unknown == 0 else "refused",
                      "registered_count": len(names) - count_unknown, "unregistered_count": count_unknown}))
    return 0 if count_unknown == 0 else 2


def command_space(args):
    need(len(args) in {2, 3})
    root, archive = Path(args[0]), args[1]
    need(root.is_absolute() and re.fullmatch(r"[0-9a-f]{64}", archive))
    path = root / "index" / archive / "markets-v1.jsonl"
    for directory in (*reversed(root.parents), root, root / "index", path.parent):
        need(stat.S_ISDIR(directory.lstat().st_mode))
    need(stat.S_ISREG(path.lstat().st_mode))
    space = os.statvfs(root)
    available = (space.f_bavail * space.f_frsize) // 1024
    budget = None
    if len(args) == 3:
        need(re.fullmatch(r"[1-9][0-9]*", args[2]))
        budget = int(args[2])
    accepted = budget is None or available >= budget
    print(json.dumps({"available_kib": available, "markets_bytes": path.stat().st_size,
                      "code": "space:available" if accepted else "space:below_budget"}, sort_keys=True))
    return 0 if accepted else 2


def command_parent_compare(args):
    def pairs(items):
        value = dict(items)
        need(len(value) == len(items), "parent:uninspectable")
        return value

    def census(filename):
        try:
            with os.fdopen(os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
                need(stat.S_ISREG(os.fstat(handle.fileno()).st_mode), "parent:uninspectable")
                rows = [json.loads(line, object_pairs_hook=pairs) for line in handle.read().splitlines()]
            need(4 <= len(rows) <= 204 and all(isinstance(row, dict) for row in rows), "parent:uninspectable")
            need(set(rows[0]) == {"target", "parent_realpath"} and
                 all(isinstance(value, str) for value in rows[0].values()), "parent:uninspectable")
            need(set(rows[2]) == {"realpath"} and isinstance(rows[2]["realpath"], str), "parent:uninspectable")
            for row, key in [(rows[1], "path"), *((row, "name") for row in rows[3:-1])]:
                need(set(row) == {key, "mode", "size"} and isinstance(row[key], str) and row[key] and
                     isinstance(row["mode"], str) and re.fullmatch(r"[bcdlps?-][rwxStTs-]{9}", row["mode"]) and
                     type(row["size"]) is int and row["size"] >= 0, "parent:uninspectable")
            need(rows[1]["mode"].startswith("d"), "parent:uninspectable")
            names = [row["name"] for row in rows[3:-1]]
            need(len(names) == len(set(names)), "parent:uninspectable")
            status = "present_with_entries" if names else "present_and_empty"
            need(rows[-1] == {"status": status, "entries": len(names)} and
                 type(rows[-1].get("entries")) is int, "parent:uninspectable")
            return rows
        except (OSError, ValueError, TypeError, UnicodeError):
            raise ExtractorRefusal("parent:uninspectable") from None

    before, after = map(census, args)
    need(len(before) == len(after), "parent:changed")
    for left, right in zip(before, after):
        if left.get("name") in {"historic-pro", "suite.noindex"}:
            left = {key: value for key, value in left.items() if key != "size"}
            right = {key: value for key, value in right.items() if key != "size"}
        need(left == right, "parent:changed")
    print("parent_census_equal")


def command_l1_layout(args):
    need(len(args) == 3)
    rows = [[json.loads(line) for line in Path(name).read_bytes().splitlines()] for name in args]
    state, data, parent = rows

    def entries(census):
        need(census and all(isinstance(row, dict) for row in census))
        found = [row for row in census if "name" in row]
        names = {row["name"] for row in found}
        need(len(names) == len(found))
        return names, all(isinstance(row.get("mode"), str) and row["mode"].startswith("d") for row in found)

    reason = "clean_layout"
    if any(not value or value[-1].get("status") not in {
            "absent", "present_and_empty", "present_with_entries"} for value in rows):
        reason = "uninspectable"
    elif state[-1]["status"] not in {"absent", "present_and_empty"} or entries(state)[0]:
        reason = "state_history"
    elif data[-1]["status"] != "present_with_entries" or entries(data) != (
            {"incoming", "raw", "manifests", "index"}, True):
        reason = "data_layout"
    elif (parent[-1]["status"] not in {"present_and_empty", "present_with_entries"}
          or not entries(parent)[0] <= {"historic", "lab-state", "historic-pro", "suite.noindex"}
          or not entries(parent)[1]):
        reason = "parent_layout"
    print(json.dumps({"status": "accepted" if reason == "clean_layout" else "refused", "reason": reason}))
    return 0 if reason == "clean_layout" else 2


def runtime_check(checkout, source, arguments):
    root = Path(checkout)
    need(root.is_absolute() and root.resolve(strict=True) == root and Path.cwd() == root)
    # Import from a checked dedicated cwd exactly as the package CLI does. Arguments are never code.
    result = subprocess.run([sys.executable, "-S", "-B", "-", *arguments], input=source.encode(), cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode not in {0, 2}:
        print('{"status":"refused","code":"runtime:check_failed"}')
        return 2
    try:
        value = json.loads(result.stdout)
        need(isinstance(value, dict))
    except (ValueError, UnicodeError):
        print('{"status":"refused","code":"runtime:check_failed"}')
        return 2
    print(json.dumps(value, sort_keys=True))
    return result.returncode


def command_identity(args):
    need(len(args) == 1)
    return runtime_check(args[0], IDENTITY_CODE, args)


def command_ledger_verify(args):
    need(len(args) == 2)
    return runtime_check(args[0], LEDGER_CODE, args[1:])


def command_index_sha(args):
    need(len(args) == 1)
    value = json.loads(Path(args[0]).read_bytes())["files"]["head"]["sha256"]
    need(digest(value) is not None)
    print(value)


def command_contract_sha(args):
    need(len(args) == 1)
    value = json.loads(Path(args[0]).read_bytes())
    need(value.get("status") == "valid_frozen")
    sha = digest(value["contract_sha256"])
    need(sha is not None)
    print(sha)


def command_cert_key(args):
    need(len(args) == 1)
    value = json.loads(Path(args[0]).read_bytes())["key"]
    need(digest(value) is not None)
    print(value)


def command_reconciled(args):
    need(len(args) == 1)
    value = json.loads(Path(args[0]).read_bytes())
    need(value.get("status") == "reconciled")
    print("reconciled")


def command_author_interpreter(args):
    need(not args and sys.version_info[:2] == (3, 15))
    print(json.dumps({"executable": os.path.realpath(sys.executable), "version": sys.version}, sort_keys=True))


def author_state(label, status_path, cache_path, index_path):
    need(label in {"c0-draft", "c0-flip-only", "c1"})
    for path in (status_path, cache_path, index_path):
        need(path.with_name(path.name + ".stderr").read_bytes() == b"", "author:census_stderr")
    expected = (b" M research/experiments/backtest-p0-bsp-v1.json\0"
                if label == "c0-flip-only" else b"")
    need(status_path.read_bytes() == expected, "author:worktree_state")
    index = index_path.read_bytes()
    need(bool(index) and index.endswith(b"\0") and all(row.startswith(b"H ") for row in index.split(b"\0")[:-1]),
         "author:worktree_state")
    need(cache_path.read_bytes() == b"", "author:cache_present")
    return "contract_modified" if label == "c0-flip-only" else "clean"


def command_author_state(args):
    need(len(args) == 4)
    state = author_state(args[0], Path(args[1]), Path(args[2]), Path(args[3]))
    print(json.dumps({"status": "accepted", "worktree_state": state}, sort_keys=True))


def command_author_bytes(args):
    need(len(args) == 1 and args[0] in {"c0-draft", "c0-flip-only", "c1"})

    def git(*arguments):
        result = subprocess.run(["git", *arguments], capture_output=True, check=False)
        need(result.returncode == 0, "author:tracked_bytes")
        return result.stdout

    try:
        tree = git("ls-tree", "-rz", "--full-tree", "HEAD")
        need(bool(tree) and tree.endswith(b"\0"), "author:tracked_bytes")
        checked = skipped = 0
        for row in tree.split(b"\0")[:-1]:
            metadata, separator, name = row.partition(b"\t")
            fields = metadata.split()
            need(separator and len(fields) == 3 and fields[0] in {b"100644", b"100755"}
                 and fields[1] == b"blob" and re.fullmatch(rb"[0-9a-f]{40}", fields[2]), "author:tracked_bytes")
            path = Path(os.fsdecode(name))
            need(name and not path.is_absolute() and ".." not in path.parts
                 and stat.S_ISREG(path.lstat().st_mode), "author:tracked_bytes")
            if args[0] == "c0-flip-only" and name == b"research/experiments/backtest-p0-bsp-v1.json":
                skipped += 1
                continue
            need(git("hash-object", "--no-filters", "--", os.fsdecode(name)).strip() == fields[2],
                 "author:tracked_bytes")
            checked += 1
        need(checked > 0 and skipped == int(args[0] == "c0-flip-only"), "author:tracked_bytes")
    except (OSError, subprocess.SubprocessError):
        raise ExtractorRefusal("author:tracked_bytes") from None
    print(json.dumps({"status": "accepted", "checked": checked, "skipped": skipped}, sort_keys=True))


def command_author_suite(args):
    need(len(args) == 3)
    root, prefix, label = Path(args[0]), args[1], args[2]
    need(label in {"c0-draft", "c0-flip-only", "c1"})
    need(re.fullmatch(r"freeze-" + re.escape(label) + r"-[0-9]+", prefix))
    states = [author_state(label, root / (prefix + "-status-" + stage + ".z"),
                           root / (prefix + "-cache-" + stage + ".txt"),
                           root / (prefix + "-index-" + stage + ".z")) for stage in ("before", "after")]
    need(states[0] == states[1], "author:worktree_state")
    need((root / (prefix + "-git.txt")).read_bytes() == (root / (prefix + "-git-after.txt")).read_bytes(),
         "author:head_changed")
    log = root / (prefix + "-suite.log")
    text = log.read_text()
    totals = re.findall(r"^Ran ([0-9]+) tests? in ", text, re.M)
    results = re.findall(r"^(OK|FAILED)(?: \([^\n]*\))?$", text, re.M)
    exits = (root / (prefix + "-suite.exit")).read_text().strip()
    git = (root / (prefix + "-git.txt")).read_text().splitlines()
    diff = root / (prefix + "-diff.txt")
    paths = diff.read_text().splitlines()
    need(label != "c1" or paths == ["research/experiments/backtest-p0-bsp-v1.json"], "author:c1_diff")
    print(json.dumps({
        "status": results[0] if len(results) == 1 else "unclassified",
        "tests": int(totals[0]) if len(totals) == 1 else None,
        "exit": int(exits) if re.fullmatch(r"(0|[1-9][0-9]{0,2})", exits) and int(exits) <= 255 else None,
        "git": git if len(git) == 2 and all(re.fullmatch(r"[0-9a-f]{40}", v) for v in git) else "unclassified",
        "interpreter": {"implementation": sys.implementation.name, "version": sys.version},
        "diff_only_contract": None if label == "c0-draft" else paths == [
            "research/experiments/backtest-p0-bsp-v1.json"],
        "worktree_state": states[0],
        "diff_count": len(paths),
        "log": {"classification": "unclassified", "bytes": log.stat().st_size, "lab_local_file": log.name},
        "diff": {"classification": "unclassified", "bytes": diff.stat().st_size, "lab_local_file": diff.name},
    }, sort_keys=True))


def command_empty_state(args):
    need(len(args) == 1)
    rows = [json.loads(line) for line in Path(args[0]).read_bytes().splitlines()]
    need(bool(rows) and all(isinstance(row, dict) for row in rows))
    need(rows[-1].get("status") == "present_and_empty" and not any("name" in row for row in rows))
    print("present_and_empty")


def command_extract_digest(args):
    need(len(args) == 2 and args[1] in {"index", "contract", "certificate-key"})
    function = {"index": command_index_sha, "contract": command_contract_sha, "certificate-key": command_cert_key}
    return function[args[1]](args[:1])


def command_job_pattern(args):
    need(len(args) == 1 and Path(args[0]).is_absolute())
    print("runbook-job[ ]" + re.escape(args[0]) + " ")


def notice_rows(path):
    need(stat.S_ISREG(path.lstat().st_mode), "notice:record")
    rows = path.read_bytes().decode("utf-8").split("\n")
    need(not any(separator in row for row in rows for separator in "\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"),
         "notice:record")
    keys = [row.partition("=")[0] for row in rows if row]
    need(all(re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", row) is not None for row in rows if row)
         and len(keys) == len(set(keys)), "notice:record")
    dates = [row[5:] for row in rows if row.startswith("DATE=")]
    references = [row[12:] for row in rows if row.startswith("MESSAGE_REF=")]
    need(len(dates) == len(references) == 1, "notice:record")
    need(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", dates[0]) is not None, "notice:date")
    try:
        valid_date = date.fromisoformat(dates[0]).isoformat() == dates[0]
    except ValueError:
        valid_date = False
    need(valid_date, "notice:date")
    need(bool(references[0].strip()) and "<" not in references[0] and ">" not in references[0], "notice:reference")
    return rows


def command_notice(args):
    need(len(args) == 1)
    notice_rows(Path(args[0]))
    print("notice_recorded")


def command_consent(args):
    need(len(args) == 3)
    rows = notice_rows(Path(args[0]))
    for key in ("NOTICE_TEXT", "CONSENT_TEXT"):
        values = [row[len(key) + 1:] for row in rows if row.startswith(key + "=")]
        need(len(values) == 1 and bool(values[0].strip()) and "<" not in values[0] and ">" not in values[0],
             "consent:text")
    inventory, certificate = [json.loads(Path(path).read_bytes()) for path in args[1:]]
    for key, value in (("INPUT_SHA", inventory["input_sha256"]), ("CERT_SHA", certificate["sha256"])):
        need(isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None, "consent:identifiers")
        need([row for row in rows if row.startswith(key + "=")] == [key + "=" + value], "consent:identifiers")
    print("consent_recorded")


def command_recovery_attempt(args):
    need(len(args) == 2)
    root, prefix = Path(args[0]), args[1]
    need(re.fullmatch(r"step8-[0-9]+", prefix) is not None, "recovery:attempt_prefix")
    checkpoint = root / ("session-" + prefix + "-job.zsh")
    state_check = root / (prefix + "-state-check.txt.exit")
    run = root / (prefix + "-run.json")
    for path, code in ((checkpoint, "recovery:checkpoint_not_regular"),
                       (state_check, "recovery:state_check_not_regular"), (run, "recovery:no_run_json")):
        try:
            regular = stat.S_ISREG(path.lstat().st_mode)
        except OSError:
            regular = False
        need(regular, code)
    need(state_check.read_bytes().strip() == b"0", "recovery:state_check_not_zero")
    attempt = int(prefix.split("-")[-1])
    with os.scandir(root) as entries:
        for entry in entries:
            match = re.fullmatch(r"session-step8-([0-9]+)-job\.zsh", entry.name)
            need(match is None or entry.name == checkpoint.name or int(match[1]) < attempt, "recovery:newer_attempt")
    print("recovery_attempt_current")


COMMANDS = {
    "publish": (4, command_publish), "publish-attempt": (None, command_publish_attempt),
    "registry": (1, command_registry), "census": (1, command_census), "paths": (5, command_paths),
    "new-prefix": (2, command_new_prefix), "lock-entries": (1, command_lock_entries),
    "lock-match": (3, command_lock_match), "no-locks": (2, command_no_locks), "inputs": (3, command_inputs),
    "identity": (1, command_identity), "identity-gate": (1, command_identity_gate),
    "l2-check": (2, command_l2_check), "l3-check": (3, command_l3_check), "freeze-diff": (2, command_freeze_diff),
    "certificate-check": (6, command_certificate_check), "run-binding": (10, command_run_binding),
    "run-state": (1, command_run_state), "empty-state": (1, command_empty_state), "run-status": (1, command_run_status),
    "l1-layout": (3, command_l1_layout), "parent-compare": (2, command_parent_compare),
    "space": (None, command_space), "ledger-verify": (2, command_ledger_verify),
    "extract-digest": (2, command_extract_digest), "index-sha": (1, command_index_sha),
    "contract-sha": (1, command_contract_sha), "cert-key": (1, command_cert_key),
    "reconciled": (1, command_reconciled), "notice": (1, command_notice), "consent": (3, command_consent),
    "job-pattern": (1, command_job_pattern), "recovery-attempt": (2, command_recovery_attempt),
    "author-state": (4, command_author_state), "author-suite": (3, command_author_suite),
    "author-interpreter": (0, command_author_interpreter), "author-bytes": (1, command_author_bytes),
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        need(bool(args) and args[0] in COMMANDS)
        size, function = COMMANDS[args[0]]
        need(size is None or len(args[1:]) == size)
        result = function(args[1:])
        return 0 if result is None else result
    except SystemExit as error:
        if type(error.code) is int and error.code in {0, 2}:
            return error.code
        # Local details never reach terminal publication, including unexpected exception messages.
        code = error.code if isinstance(error.code, str) and error.code in EXTRACTOR_CODES else "extractor:unexpected"
        print(json.dumps({"status": "refused", "code": code}))
        return 2
    except ExtractorRefusal as error:
        code = error.args[0] if error.args[0] in EXTRACTOR_CODES else "extractor:unexpected"
        print(json.dumps({"status": "refused", "code": code}))
        return 2
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, AttributeError, RecursionError,
            subprocess.SubprocessError):
        print('{"status":"refused","code":"extractor:unexpected"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
