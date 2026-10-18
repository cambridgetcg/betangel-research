"""Synthetic publication, recovery and shell-helper checks; no lab, licensed data or network.

The script is a C0 operator tool, not part of the certifiable runtime. These tests
exercise its allowlists and failure paths independently of the markdown walkthrough.
"""

import ast
from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "scripts" / "lab_runbook.py"
SHELL = ROOT / "scripts" / "lab_runbook.zsh"
SPEC = importlib.util.spec_from_file_location("reviewed_lab_runbook", TOOL)
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)
SHA = "a" * 64
CODE = "b" * 40 + " tree " + "c" * 40
SENTINELS = (
    "1.987654321", "314159265", "271828182",
    "market_1.987654321", "event_314159265", "selection_271828182", "price_12.34567", "bsp_98.76543",
    "settlement_secret", "stake_54321_pence", "issuance_row_secret", "raw_problem_secret",
    "exception_secret", "_failure_secret", "receipt_secret",
)
EXPECTED_EXTRACTOR_CODES = set("""extractor:shape extractor:unexpected paths:git_failed paths:not_absolute_normal
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
consent:text consent:identifiers notice:record notice:date notice:reference""".split())


def current_interpreter():
    return {"implementation": sys.implementation.name, "cache_tag": sys.implementation.cache_tag,
            "version_info": list(sys.version_info[:5]), "version": sys.version, "platform": sys.platform,
            "byteorder": sys.byteorder, "maxsize": sys.maxsize, "float_repr_style": sys.float_repr_style,
            "optimize": sys.flags.optimize}


def input_descriptor():
    return {"kind": "betangel_backtest_input", "version": 1, "archives": [{
        "archive_sha256": SHA, "manifest_sha256": SHA, "index_sha256": SHA, "markets_sha256": SHA,
        "manifest_gate_current": True, "markets": 12}]}


def fixtures():
    identity = {"key": SHA, "source_descriptor_sha256": SHA, "gate_snapshot_sha256": SHA,
                "interpreter": current_interpreter()}
    counts = {"cases": 38, "as_expected": 38, "not_as_expected": 0, "error": 0}
    criteria = {name: {"status": "passed", "problem_codes": ["counts_do_not_reconcile"]} for name in (
        "known_accounting_answers", "issuance_exposure_reconciliation", "prefix_invariance")}
    binding = {"input": input_descriptor(), "input_sha256": SHA,
               "population": {"records": 12, "by_class": {"eligible": 12},
                              "by_reason": {"excluded": {"country_not_in_population": 2}}},
               "code_identity": CODE, "contract_sha256": SHA, "certificate": {"key": SHA, "sha256": SHA},
               "acceptance_status": "passed", "started": [{"variant": "ltp_favourite",
                   "trial_id": "run-1.987654321.ltp_favourite",
                   "code_identity": CODE, "input_sha256": SHA, "contract_sha256": SHA, "certificate_sha256": SHA}],
               "engineering_acceptance": criteria,
               "prefix_tally": {"records": 12, "candidates": 12, "compared": 12, "passed": 12,
                                "failed": 0, "missing": 0, "unexercised_by_reason": {"prefix_buffer_limit": 1},
                                "unissued_variants": ["ltp_favourite"]},
               "unexercised_variants": ["seeded_random_runner"]}
    receipt = {"status": "completed", "failure": "ReplayProblem:prefix:1.987654321:member_missing",
               "ledger_head": [4, SHA], "contract_sha256": SHA, "certificate_sha256": SHA,
               "results_sha256": "d" * 64, "issuance_ledger_sha256": "e" * 64, "issuance_ledger_bytes": 987654321,
               "engineering_acceptance": {"status": "passed"},
               "trials": [{"started": [1, SHA], "closed": [3, SHA], "issued": 987654321}]}
    run = {"status": "completed", "engineering_acceptance": "passed", "cumulative_starts": 2,
           "results_sha256": "d" * 64, "receipt_sha256": "e" * 64,
           "failure": "ReplayProblem:prefix:1.987654321:member_missing", "ledger_head": [4, SHA],
           "problems": ["cohort:1.987654321:duplicate_market"], "trial_ids": ["run-1.ltp_favourite"],
           "runs_touched": ["run-1"], "aborted": ["run-1.ltp_favourite"], "recorded_runs": ["run-1"],
           "receipted": ["run-1"], "started": ["ltp_favourite"], "closed": ["ltp_favourite"],
           "lock_release": "failed", "receipt": receipt}
    l3 = {"status": "completed_with_problems", "refusal": "cohort:1.987654321:duplicate_market",
          "naming": {"code_identity": CODE, **identity, "readiness_source_sha256": SHA},
          "input": input_descriptor(), "input_sha256": SHA,
          "records": {"lines": 12, "classified": 10, "with_problems": 2},
          "record_problems": {"duplicate_market": 1},
          "input_preflight_would_refuse": "input:" + SHA + ":stale_gate:1.987654321",
          "population": {"by_class": {"eligible": 10},
                         "by_reason": {"excluded": {"country_not_in_population": 2}}},
          "records_by_plan": {"BASIC": 12}, "prefix_candidates": 10,
          "source_refusals": {"members_refused_at_ingest_by_reason": {"missing": 1},
                              "index_files_not_indexed_by_status_reason": {"refused:missing": 1},
                              "market_rows_by_reason": {"missing": 1}, "distinct_markets_by_reason": {"missing": 1}}}
    return {
        "raw": ("step1-01-paths.txt", b"local output"),
        "exit": ("step8-01-run.json.exit", b"2\n"),
        "liveness": ("restore-01-liveness.txt", b"job_pgrep_exit=0 writer_pgrep_exit=1\nblock_stdout=present\n"),
        "check": ("step1-01-git.txt", CODE.encode()),
        "census": ("l1-01-data.jsonl", b'{"name":"historic","mode":"drwx------","size":3}\n'
                   b'{"status":"present_with_entries"}\n'),
        "l2": ("l2-01-index.json", {"status": "refused", "archives": [{"archive_sha256": SHA,
               "index_sha256": SHA, "markets_sha256": SHA, "markets": 12, "markets_refused": 2,
               "files": 12, "files_indexed": 10, "files_unreadable": 1, "files_refused": 1,
               "state": "unchanged"}], "problems": ["raw:314159265/1.987654321.bz2:missing"]}),
        "l3": ("l3-01-inventory.json", l3),
        "run": ("step8-01-run.json", run),
        "recovery": ("step9-runner-01-reconcile.json", {**run, "status": "refused"}),
        "certificate": ("step6-01-certificate-check.json", {"status": "certified", "counts": counts,
                        "key": SHA, "sha256": SHA, "source": {"sha256": SHA},
                        "interpreter": current_interpreter()}),
        "certify": ("step6-01-certify.json", {"status": "certified", "counts": counts, "key": SHA,
                    "certificate_sha256": SHA, "source_descriptor_sha256": SHA,
                    "failed_cases": ["case-1"], "problems": ["attempt:exists"]}),
        "binding": ("step8-01-binding.json", binding),
        "identity": ("step1-01-identity.json", identity),
        "inputs": ("l2-01-before-files.json", {"archive_sha256": SHA, "files": {
            key: {"sha256": SHA, "bytes": 256, "lines": 12, "path": "/private/market_1.987654321"}
            for key in ("manifest", "head", "markets")}}),
        "ledger": ("step9-verify-01-ledger-verify.json", {"status": "refused", "code": "line 3:truncated_line",
                   "entries": 4, "by_kind": {"started": 2, "completed": 2}, "head": [4, SHA]}),
        "checkpoint": ("session-step1-01.zsh", b"# synthetic non-secret operator parameters\n"),
        "entries": ("step9-01-entries.json", {"directory_status": "present", "entries": ["runner.lock"],
                                            "locks": ["runner.lock"]}),
        "contract": ("step5b-01-contract.json", {"status": "valid_frozen", "contract_sha256": SHA, "problems": []}),
        "digest": ("step5b-01-contract-sha.txt", (SHA + "\n").encode()),
        "space": ("l2-01-space.txt", {"available_kib": 1024, "markets_bytes": 256}),
        "layout": ("l1-01-layout-check.txt", {"status": "accepted", "reason": "clean_layout"}),
    }


def leaves(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaves(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from leaves(child, path + (index,))
    else:
        yield path


def mutated(value, path, replacement):
    result = copy.deepcopy(value)
    cursor = result
    for name in path[:-1]:
        cursor = cursor[name]
    cursor[path[-1]] = replacement
    return result


def key_paths(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield path + (key,)
            yield from key_paths(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from key_paths(child, path + (index,))


def renamed_key(value, path, replacement):
    result = copy.deepcopy(value)
    cursor = result
    for name in path[:-1]:
        cursor = cursor[name]
    cursor[replacement] = cursor.pop(path[-1])
    return result


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runbook-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def invoke(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = lab.main(list(map(str, arguments)))
        return rc, output.getvalue()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        return path

    def author_states(self, prefix, label):
        value = b" M research/experiments/backtest-p0-bsp-v1.json\0" if label == "c0-flip-only" else b""
        for stage in ("before", "after"):
            self.write(prefix + "-status-" + stage + ".z", value)
            self.write(prefix + "-cache-" + stage + ".txt", b"")
            self.write(prefix + "-index-" + stage + ".z", b"H tracked.txt\0")
            for suffix in ("status-" + stage + ".z", "cache-" + stage + ".txt", "index-" + stage + ".z"):
                self.write(prefix + "-" + suffix + ".stderr", b"")
        self.write(prefix + "-git-after.txt", (self.root / (prefix + "-git.txt")).read_bytes())

    def publish(self, mode, value, name=None, lock="expected-lock"):
        name = name or fixtures()[mode][0]
        path = self.write(name, value)
        return self.invoke("publish", mode, path, lock, name)


class PublicationTests(ToolTest):
    def test_all_modes_have_a_valid_projection(self):
        self.assertEqual(set(fixtures()), lab.MODES)
        for mode, (name, value) in fixtures().items():
            with self.subTest(mode=mode):
                rc, text = self.publish(mode, value, name)
                self.assertEqual(rc, 0, text)
                self.assertIn("raw_report", json.loads(text))

    def test_every_mode_rejects_raw_outcome_and_exception_payloads(self):
        """Exercise every mode with every sensitive category, not merely one report fixture."""
        for mode, (name, value) in fixtures().items():
            for sentinel in SENTINELS:
                with self.subTest(mode=mode, sentinel=sentinel):
                    payload = {"market": sentinel, "event": sentinel, "selection": sentinel,
                               "ltp": sentinel, "bsp": sentinel, "settlement": sentinel, "stakes": sentinel,
                               "issuance": [sentinel], "problems": [sentinel], "exception": sentinel,
                               "_failure": sentinel, "receipt": sentinel}
                    if isinstance(value, dict):
                        raw = {**copy.deepcopy(value), **payload}
                    elif mode == "census":
                        raw = (json.dumps({"name": sentinel, "mode": "-rw-------", "size": 123}) + "\n" +
                               json.dumps({"status": "present_with_entries", "reason": sentinel})).encode()
                    else:
                        raw = sentinel.encode()
                    rc, text = self.publish(mode, raw, name)
                    self.assertIn(rc, (0, 2))
                    self.assertNotIn(sentinel, text)
                    if mode != "checkpoint":
                        self.assertNotIn(hashlib.sha256(sentinel.encode()).hexdigest(), text)
                    self.assertIn("raw_report", json.loads(text))

    def test_every_structured_field_is_nonleaking_under_sentinel_substitution(self):
        for mode, (name, value) in fixtures().items():
            if not isinstance(value, dict):
                continue
            for path in leaves(value):
                for sentinel in SENTINELS:
                    with self.subTest(mode=mode, path=path, sentinel=sentinel):
                        rc, text = self.publish(mode, mutated(value, path, sentinel), name)
                        self.assertIn(rc, (0, 2))
                        self.assertNotIn(sentinel, text)
                        self.assertNotIn(hashlib.sha256(sentinel.encode()).hexdigest(), text)

    def test_dictionary_keys_cannot_publish_ids_or_their_unsalted_hashes(self):
        for mode, (name, value) in fixtures().items():
            if not isinstance(value, dict):
                continue
            for path in key_paths(value):
                for sentinel in SENTINELS:
                    with self.subTest(mode=mode, path=path, sentinel=sentinel):
                        rc, text = self.publish(mode, renamed_key(value, path, sentinel), name)
                        self.assertIn(rc, (0, 2))
                        self.assertNotIn(sentinel, text)
                        self.assertNotIn(hashlib.sha256(sentinel.encode()).hexdigest(), text)

    def test_numeric_outcomes_are_not_accepted_as_counts(self):
        for value in (12.3456789, True, -2468, "1.23456789"):
            rc, text = self.publish("space", {"available_kib": value, "markets_bytes": 256})
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(text)["code"], "publication:unclassified")

    def test_unregistered_name_is_never_echoed(self):
        for name in ("market_1.987654321.json", "step8-01-stolen.json", "../event_314159265.json"):
            output = io.StringIO()
            with redirect_stdout(output):
                rc = lab.publish("raw", self.root / "missing", "", name)
            self.assertEqual(rc, 2)
            self.assertNotIn(name, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["code"], "publication:unregistered_name")

    def test_registered_name_cannot_select_a_different_projection(self):
        path = self.write("step8-01-run.json", b"settlement_secret")
        rc, text = self.invoke("publish", "checkpoint", path, "", path.name)
        self.assertEqual(rc, 2)
        self.assertNotIn(hashlib.sha256(path.read_bytes()).hexdigest(), text)

    def test_missing_file_prints_fixed_code_for_every_mode(self):
        for mode, (name, _) in fixtures().items():
            with self.subTest(mode=mode):
                rc, text = self.invoke("publish", mode, self.root / name, "", name)
                self.assertEqual(rc, 2)
                self.assertEqual(json.loads(text)["code"], "publication:missing_file")

    def test_symlinks_are_not_opened(self):
        target = self.write("private", b"settlement_secret")
        name = "step8-01-run.json"
        (self.root / name).symlink_to(target)
        rc, text = self.invoke("publish", "run", self.root / name, "", name)
        self.assertEqual(rc, 2)
        self.assertNotIn("settlement_secret", text)

    def test_l2_and_l3_raw_member_problems_have_only_fixed_codes(self):
        for mode, report in (
            ("l2", {"status": "refused", "problems": ["raw:event_314159265/market_1.987654321:missing"]}),
            ("l3", {"status": "refused", "refusal": "input:event_314159265:member_not_the_manifests",
                    "record_problems_listed": ["market_1.987654321"]}),
        ):
            rc, text = self.publish(mode, report)
            self.assertEqual(rc, 0)
            self.assertNotIn("314159265", text)
            self.assertNotIn("987654321", text)
            self.assertIn("raw:missing" if mode == "l2" else "input:member_not_the_manifests", text)

    def test_uncertain_mismatch_preserves_rc2_and_bundle_continues(self):
        self.write("step9-runner-01-reconcile.json", {"status": "uncertain", "runner_lock": "wrong"})
        self.write("step9-runner-01-status.txt", b"reconciled\n")
        rc, text = self.invoke("publish-attempt", self.root, "step9-runner-01", SHA, "expected")
        self.assertEqual(rc, 2)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertTrue(any(row.get("status") == "uncertain" for row in rows))
        self.assertTrue(any(row.get("code") == "reconciled" for row in rows))

    def test_passed_and_missing_runner_lock_use_null(self):
        rc, text = self.publish("run", {"status": "completed", "engineering_acceptance": "passed"})
        self.assertEqual(rc, 0)
        self.assertIsNone(json.loads(text)["runner_lock_path_matches"])
        rc, text = self.publish("recovery", {"status": "uncertain"})
        self.assertEqual(rc, 2)
        value = json.loads(text)
        self.assertIsNone(value["runner_lock_path_matches"])
        self.assertIsNone(value["started"])
        self.assertIsNone(value["closed"])

    def test_stderr_has_no_unsalted_hash_or_phantom_stderr(self):
        name = "l2-01-index.json.stderr"
        rc, text = self.publish("raw", b"raw_problem_secret", name)
        self.assertEqual(rc, 0)
        self.assertNotIn("stderr.stderr", text)
        self.assertNotIn("sha256", text)
        self.assertNotIn("raw_problem_secret", text)

    def test_contract_and_digest_modes_publish_the_identifiers_needed_by_notice(self):
        rc, text = self.publish("contract", {"status": "valid_frozen", "contract_sha256": SHA,
                                              "problems": ["market_1.987654321"]})
        self.assertEqual(rc, 0)
        value = json.loads(text)
        self.assertEqual((value["status"], value["contract_sha256"], value["problem_count"]),
                         ("valid_frozen", SHA, 1))
        for name in ("l2-02-index-sha.txt", "step5b-03-contract-sha.txt", "step5b-03-cert-key.txt"):
            self.assertEqual(self.publish("digest", SHA.encode(), name)[0], 0)

    def test_ledger_code_is_a_fixed_enum_not_a_lowercase_identifier_channel(self):
        for code, expected in (("line 3:truncated_line", "truncated_line"),
                               ("line 3:market_987654321", "unclassified"),
                               ("ledger:market_987654321", "unclassified")):
            rc, text = self.publish("ledger", {"status": "refused", "code": code})
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(text)["code"], expected)
            self.assertNotIn("987654321", text)

    def test_runner_heads_are_source_enumerated(self):
        for head in ("PublicationUncertain", "CohortProblem", "reconcile", "recovery", "run_id", "run_dir", "runs",
                     "runs_dir", "contracts", "contract_dir", "data_root", "lab_state_root", "source",
                     "certification", "code_identity", "index", "manifests", "issuance_ledger"):
            self.assertEqual(lab.code(head + ":market_1.987654321"), head)


class GateTests(ToolTest):
    def census(self, name, entries=(), status=None):
        rows = [{"name": n, "mode": kind + "rwx------", "size": 0} for n, kind in entries]
        rows.append({"status": status or ("present_with_entries" if entries else "present_and_empty")})
        return self.write(name, b"\n".join(json.dumps(row).encode() for row in rows) + b"\n")

    def test_l1_layout_allows_only_the_exact_fresh_layout(self):
        state = self.census("state", status="absent")
        data = self.census("data", [(name, "d") for name in ("incoming", "raw", "manifests", "index")])
        parent = self.census("parent", [("historic", "d")])
        rc, text = self.invoke("l1-layout", state, data, parent)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(text)["reason"], "clean_layout")
        self.census("state")
        self.assertEqual(self.invoke("l1-layout", state, data, parent)[0], 0)

    def test_l1_history_unexplained_names_and_symlinks_fail_with_fixed_reasons(self):
        state = self.census("state", status="absent")
        data = self.census("data", [(name, "d") for name in ("incoming", "raw", "manifests", "index")])
        parent = self.census("parent", [("historic", "d")])
        for target, entries, expected in (
            (state, [("contracts", "d")], "state_history"),
            (data, [("market_1.987654321", "d")], "data_layout"),
            (parent, [("market_1.987654321", "d")], "parent_layout"),
            (parent, [("historic", "l")], "parent_layout"),
        ):
            old = target.read_bytes()
            self.census(target.name, entries)
            rc, text = self.invoke("l1-layout", state, data, parent)
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(text)["reason"], expected)
            self.assertNotIn("987654321", text)
            target.write_bytes(old)

    def test_census_knows_parent_names_but_redacts_unknown_and_hex_names(self):
        raw = self.census("raw", [("historic", "d"), ("lab-state", "d"), (SHA, "d"),
                                  ("market_1.987654321", "d")]).read_bytes()
        rc, text = self.publish("census", raw)
        self.assertEqual(rc, 0)
        value = json.loads(text)
        self.assertEqual([row["name"] for row in value["entries"]],
                         ["historic", "lab-state", "unclassified", "unclassified"])
        self.assertNotIn(SHA, text)

    def test_census_never_opens_entries_or_follows_symlinks(self):
        directory = self.root / "directory"
        directory.mkdir()
        target = self.write("secret", b"settlement_secret")
        (directory / "link").symlink_to(target)
        with mock.patch.object(Path, "open", side_effect=AssertionError("census opened an entry")):
            rc, text = self.invoke("census", directory)
        self.assertEqual(rc, 0)
        self.assertNotIn("settlement_secret", text)
        self.assertIn('"mode": "l', text)

    def test_census_absent_target_and_refused_parent_are_distinct(self):
        rc, text = self.invoke("census", self.root / "absent")
        self.assertEqual(rc, 0)
        self.assertIn('"status": "absent"', text)
        rc, text = self.invoke("census", self.root / "absent" / "child")
        self.assertEqual(rc, 2)
        self.assertIn('"status": "uninspectable"', text)

    def test_space_uses_statvfs_and_gates_the_reviewed_budget(self):
        data = self.root / "data"
        path = data / "index" / SHA / "markets-v1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * 31)
        space = mock.Mock(f_bavail=16, f_frsize=1024)
        with mock.patch.object(lab.os, "statvfs", return_value=space):
            rc, text = self.invoke("space", data, SHA, "16")
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(text)["available_kib"], 16)
            self.assertEqual(json.loads(text)["markets_bytes"], 31)
            rc, text = self.invoke("space", data, SHA, "17")
            self.assertEqual(rc, 2)
            self.assertEqual(json.loads(text)["code"], "space:below_budget")

    def test_notice_requires_one_valid_date_and_message_reference(self):
        for body, accepted in (
            ("DATE=2026-10-16\nMESSAGE_REF=local-reviewed-message-7\n", True),
            ("DATE=2026-02-30\nMESSAGE_REF=local-reviewed-message-7\n", False),
            ("DATE=2026-10-16\nMESSAGE_REF=<fill in>\n", False),
            ("DATE=2026-10-16\nMESSAGE_REF=\n", False),
            ("DATE=2026-10-16\nDATE=2026-10-16\nMESSAGE_REF=x\n", False),
        ):
            path = self.write("notice", body.encode())
            rc, text = self.invoke("notice", path)
            self.assertEqual(rc, 0 if accepted else 2)
            self.assertNotIn("local-reviewed-message", text)

    def test_recovery_refuses_a_stale_step8_checkpoint(self):
        self.write("session-step8-01-job.zsh", b"# old\n")
        self.write("step8-01-state-check.txt.exit", b"0\n")
        self.write("step8-01-run.json", b"")
        self.assertEqual(self.invoke("recovery-attempt", self.root, "step8-01")[0], 0)
        self.write("session-step8-02-job.zsh", b"# new\n")
        rc, text = self.invoke("recovery-attempt", self.root, "step8-01")
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(text)["code"], "recovery:newer_attempt")

    def test_recovery_refuses_a_same_number_checkpoint_alias(self):
        self.write("session-step8-01-job.zsh", b"# current\n")
        self.write("step8-01-state-check.txt.exit", b"0\n")
        self.write("step8-01-run.json", b"")
        self.write("session-step8-0-job.zsh", b"# earlier is unambiguous\n")
        self.assertEqual(self.invoke("recovery-attempt", self.root, "step8-01")[0], 0)
        self.write("session-step8-1-job.zsh", b"# same number, different attempt namespace\n")
        rc, text = self.invoke("recovery-attempt", self.root, "step8-01")
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(text)["code"], "recovery:newer_attempt")

    def test_missing_run_before_start_cannot_enter_lock_recovery(self):
        self.write("session-step8-01-job.zsh", b"# pending\n")
        self.write("step8-01-state-check.txt.exit", b"0\n")
        self.assertEqual(self.invoke("recovery-attempt", self.root, "step8-01")[0], 2)

    def test_missing_extractor_files_print_fixed_refusals(self):
        missing = self.root / "market_1.987654321"
        for command, args in (
            ("identity-gate", [missing]), ("l2-check", [missing, SHA]), ("l3-check", [missing, missing, CODE]),
            ("empty-state", [missing]), ("contract-sha", [missing]), ("index-sha", [missing]),
            ("cert-key", [missing]), ("notice", [missing]),
            ("certificate-check", [self.root, SHA, "attempt-1", missing, missing, missing]),
            ("run-binding", [self.root, self.root, missing, SHA, "run-1", CODE, "0", missing, missing, missing]),
        ):
            with self.subTest(command=command):
                rc, text = self.invoke(command, *args)
                self.assertEqual(rc, 2)
                self.assertEqual(json.loads(text)["code"], "extractor:unexpected")
                self.assertNotIn("987654321", text)

    def test_prefix_collision_refuses_without_making_a_second_file(self):
        self.write("l2-01-index.json", b"")
        before = sorted(self.root.iterdir())
        self.assertEqual(self.invoke("new-prefix", self.root, "l2-01")[0], 2)
        self.assertEqual(self.invoke("new-prefix", self.root, "l2-02")[0], 0)
        self.assertEqual(before, sorted(self.root.iterdir()))

    def test_freeze_diff_refuses_any_other_setting(self):
        original = {"status": "draft", "frozen_on": None, "setting": 3}
        before = self.write("before", original)
        after = self.write("after", {**original, "status": "frozen", "frozen_on": "2026-10-16"})
        self.assertEqual(self.invoke("freeze-diff", before, after)[0], 0)
        self.write("after", {**original, "status": "frozen", "frozen_on": "2026-10-16", "setting": 4})
        self.assertEqual(self.invoke("freeze-diff", before, after)[0], 2)

    def test_author_interpreter_requires_315(self):
        with mock.patch.object(lab.sys, "version_info", (3, 15, 0, "final", 0)):
            rc, text = self.invoke("author-interpreter")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(text)["executable"], os.path.realpath(sys.executable))
        with mock.patch.object(lab.sys, "version_info", (3, 14, 6, "final", 0)):
            rc, text = self.invoke("author-interpreter")
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(text)["code"], "extractor:shape")

    def test_author_suite_contract_diff_is_nonvacuous(self):
        for label, changed, expected in (("c0-draft", b"", None), ("c0-flip-only", b"", False),
                                         ("c1", b"", False),
                                         ("c1", b"research/experiments/backtest-p0-bsp-v1.json\n", True)):
            prefix = "freeze-" + label + "-01"
            self.write(prefix + "-suite.log", b"Ran 15 tests in 0.01s\nOK\nraw_problem_secret\n")
            self.write(prefix + "-suite.exit", b"0\n")
            self.write(prefix + "-git.txt", ("a" * 40 + "\n" + "b" * 40 + "\n").encode())
            self.write(prefix + "-diff.txt", changed)
            self.author_states(prefix, label)
            rc, text = self.invoke("author-suite", self.root, prefix, label)
            if label == "c1" and not expected:
                self.assertEqual((rc, json.loads(text)),
                                 (2, {"status": "refused", "code": "author:c1_diff"}))
            else:
                self.assertEqual(rc, 0)
                self.assertIs(json.loads(text)["diff_only_contract"], expected)
            self.assertNotIn("raw_problem_secret", text)

    def test_runtime_subprocess_is_fixed_code_and_suppresses_raw_failure(self):
        result = subprocess.CompletedProcess([], 1, b"market_1.987654321", b"event_314159265")
        with mock.patch.object(lab.subprocess, "run", return_value=result) as run:
            with mock.patch.object(lab.Path, "cwd", return_value=self.root):
                rc, text = self.invoke("identity", self.root)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(text)["code"], "runtime:check_failed")
        self.assertNotIn("987654321", text)
        args = run.call_args.args[0]
        self.assertEqual(args, [sys.executable, "-S", "-B", "-", str(self.root)])
        self.assertEqual(run.call_args.kwargs["input"], lab.IDENTITY_CODE.encode())
        self.assertNotIn("betangel_research", " ".join(args))


class ProducerAndBoundaryTests(ToolTest):
    def test_real_problem_shapes_keep_fixed_codes_and_no_tail_ids(self):
        from betangel_research.betfair import readiness
        from betangel_research.betfair.backtest import runner
        from betangel_research.betfair.backtest.replay import ReplayProblem
        market = "1.987654321"
        rows = [(readiness._record_code(SHA, {"market_id": market}, stage),
                 "cohort:duplicate_market" if stage == "duplicate_market" else "input:" + stage)
                for stage in readiness.STAGES]
        rows += [("input:" + SHA + ":member_not_the_manifests:" + market, "input:member_not_the_manifests"),
                 ("cohort:" + market + ":view_for_another_decision", "cohort"),
                 (runner._failure(ReplayProblem("prefix:" + market + ":member_missing")), "ReplayProblem"),
                 ("raw:314159265/" + market + ".bz2:missing", "raw:missing"),
                 ("raw:" + market, "unclassified")]
        for raw, expected in rows:
            with self.subTest(raw=raw):
                self.assertEqual(lab.code(raw), expected)
                for mode, key in (("l2", "problems"), ("l3", "refusal"), ("l3", "input_preflight_would_refuse"),
                                  ("run", "failure"), ("recovery", "failure")):
                    value = copy.deepcopy(fixtures()[mode][1])
                    value[key] = [raw] if key == "problems" else raw
                    rc, text = self.publish(mode, value)
                    self.assertEqual(rc, 0, text)
                    self.assertNotIn(market, text)
                    self.assertNotIn(hashlib.sha256(market.encode()).hexdigest(), text)
                value = copy.deepcopy(fixtures()["run"][1])
                value["receipt"]["failure"] = raw
                self.assertNotIn(market, self.publish("run", value)[1])

    def test_each_fixed_pair_has_an_exact_safe_projection(self):
        for head, reasons in lab.FIXED_PAIRS.items():
            for reason in reasons:
                raw = head + ":" + (SHA + ":" if head in {"index", "manifest"} else "") + reason
                self.assertEqual(lab.code(raw), head + ":" + reason, raw)
                if head not in {"index", "manifest"}:
                    self.assertEqual(lab.code(raw + ":1.987654321"), head + ":" + reason)
        for error in lab.IO_ERRORS:
            self.assertEqual(lab.code("index:" + SHA + ":io:" + error), "index:io:" + error)
        for raw in ("index:1.987654321:missing", "index:" + SHA + ":io:1.987654321",
                    "manifest:" + SHA + ":missing:1.987654321", "attempt:1.987654321"):
            self.assertNotIn("1.987654321", lab.code(raw))

    def test_every_fixed_extractor_refusal_survives_each_public_projection(self):
        modes = ("check", "certificate", "binding", "l2", "l3", "inputs", "entries", "identity", "ledger")
        for code in sorted(lab.EXTRACTOR_CODES):
            with self.subTest(code=code):
                with mock.patch.dict(lab.COMMANDS, {"probe": (0, mock.Mock(side_effect=SystemExit(code)))}):
                    rc, text = self.invoke("probe")
                self.assertEqual((rc, json.loads(text)), (2, {"status": "refused", "code": code}))
                for mode in modes:
                    published = self.publish(mode, text.encode())
                    self.assertEqual(published[0], 0, published[1])
                    self.assertEqual(json.loads(published[1])["code"], code)
        for error in (SystemExit("market_1.987654321"), ValueError("receipt_secret")):
            with mock.patch.dict(lab.COMMANDS, {"probe": (0, mock.Mock(side_effect=error))}):
                self.assertEqual(json.loads(self.invoke("probe")[1])["code"], "extractor:unexpected")

    def test_literal_and_partial_consent_records_refuse_until_all_fields_are_real(self):
        l3 = self.write("inventory", {"input_sha256": SHA})
        cert = self.write("certificate", {"sha256": "d" * 64})
        rows = ["DATE=2026-10-16", "MESSAGE_REF=synthetic-message-1", "NOTICE_TEXT=sent a notice",
                "CONSENT_TEXT=explicit synthetic consent", "INPUT_SHA=" + SHA, "CERT_SHA=" + "d" * 64]
        for mode in ("valid", "template", "notice_missing", "consent_missing", "notice_blank", "consent_blank",
                     "notice_duplicate", "consent_duplicate", "input_missing", "cert_missing", "input_wrong",
                     "cert_wrong", "input_duplicate", "cert_duplicate"):
            changed = list(rows)
            if mode == "template":
                changed[2:4] = ["NOTICE_TEXT=<已發出通知>", "CONSENT_TEXT=<實際同意內容>"]
            elif mode != "valid":
                field, operation = mode.split("_")
                index = {"notice": 2, "consent": 3, "input": 4, "cert": 5}[field]
                if operation == "missing":
                    del changed[index]
                elif operation == "duplicate":
                    changed.append(changed[index])
                else:
                    changed[index] = changed[index].split("=")[0] + ("=   " if operation == "blank" else "=" + SHA)
                    if field == "input" and operation == "wrong":
                        changed[index] = "INPUT_SHA=" + "e" * 64
            path = self.write("notice", ("\n".join(changed) + "\n").encode())
            rc, text = self.invoke("consent", path, l3, cert)
            self.assertEqual(rc, 0 if mode == "valid" else 2, (mode, text))
            self.assertNotIn("synthetic-message", text)
        for name, field, index in (("inventory", "input_sha256", 4), ("certificate", "sha256", 5)):
            self.write("inventory", {"input_sha256": SHA})
            self.write("certificate", {"sha256": "d" * 64})
            changed = list(rows)
            changed[index] = changed[index].split("=")[0] + "=1.987654321"
            self.write(name, {field: "1.987654321"})
            path = self.write("notice", ("\n".join(changed) + "\n").encode())
            self.assertEqual(self.invoke("consent", path, l3, cert)[0], 2)

    def test_census_fields_and_keys_do_not_bypass_type_or_code_projection(self):
        row = {"name": "historic", "mode": "lrwx------", "size": 4}
        def project(value):
            return self.publish("census", (json.dumps(value) + '\n{"status":"present_with_entries"}\n').encode())
        rc, text = project(row)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(text)["entries"], [{"name": "historic", "bytes": 4, "type": "symlink"}])
        for key in row:
            for sentinel in SENTINELS:
                replacements = ({**row, key: sentinel},
                                {**{k: v for k, v in row.items() if k != key}, sentinel: row[key]})
                for value in replacements:
                    rc, text = project(value)
                    self.assertIn(rc, (0, 2))
                    self.assertNotIn(sentinel, text)
                    self.assertNotIn(hashlib.sha256(sentinel.encode()).hexdigest(), text)
        for size in (-1, True, 12.34567):
            self.assertEqual(project({**row, "size": size})[0], 2)

    def test_null_input_hash_and_noncanonical_author_fields_are_refused(self):
        for key in ("archive_sha256", "manifest_sha256", "index_sha256", "markets_sha256"):
            value = copy.deepcopy(fixtures()["l3"][1])
            value["input"]["archives"][0][key] = None
            self.assertEqual(self.publish("l3", value)[0], 2)
        prefix = "freeze-c1-01"
        self.write(prefix + "-suite.log", b"Ran 1 test in 0.01s\nOK\n")
        self.write(prefix + "-diff.txt", b"research/experiments/backtest-p0-bsp-v1.json\n")
        for value, expected in ((b"0\n", 0), (b"256\n", None), (b"1.987654321\n", None)):
            self.write(prefix + "-suite.exit", value)
            self.write(prefix + "-git.txt", b"1.987654321\nreceipt_secret\n")
            self.author_states(prefix, "c1")
            rc, text = self.invoke("author-suite", self.root, prefix, "c1")
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(text)["exit"], expected)
            self.assertEqual(json.loads(text)["git"], "unclassified")
            self.assertNotIn("1.987654321", text)

    def test_projection_boundaries_and_receipt_acceptance(self):
        for raw in (b"256", b"-1", b"00", b"block_exit=256", b"2\n0"):
            self.assertEqual(self.publish("exit", raw)[0], 2)
        for raw in (b"A" * 64, b"a" * 63, b"a" * 65):
            self.assertEqual(self.publish("digest", raw)[0], 2)
        for raw in (b"job_pgrep_exit=1 writer_pgrep_exit=1\n" * 2 + b"block_stdout=present\n",
                    b"job_pgrep_exit=2 writer_pgrep_exit=1\nblock_stdout=present\n"):
            self.assertEqual(self.publish("liveness", raw)[0], 2)
        rc, text = self.publish("liveness", b"1.987654321\njob_pgrep_exit=1 writer_pgrep_exit=1\n"
                                b"block_stdout=present\n")
        self.assertEqual(rc, 0)
        self.assertNotIn("1.987654321", text)
        for head in ([None, None], [5, None], [None, SHA], [True, SHA], [-1, SHA]):
            self.assertEqual(self.publish("ledger", {"head": head})[0], 2)
        for binding in (None, {"status": "passed"}, {"status": "market_1.987654321"}):
            raw = copy.deepcopy(fixtures()["run"][1])
            raw["receipt"]["engineering_acceptance"] = binding
            rc, text = self.publish("run", raw)
            self.assertEqual(rc, 0)
            expected = None if binding is None else "passed" if binding["status"] == "passed" else "unclassified"
            self.assertEqual(json.loads(text)["receipt"]["acceptance_status"], expected)
        self.assertEqual(json.loads(self.publish("l3", fixtures()["l3"][1])[1])["population"]["records"], 12)
        result = json.loads(self.publish("binding", fixtures()["binding"][1])[1])
        self.assertEqual(result["prefix_tally"]["unissued_variants"], ["ltp_favourite"])
        self.assertNotIn("unexercised_variants", result["prefix_tally"])
        self.assertNotIn(CODE, json.dumps(result))
        self.assertEqual(result["code_identity_sha256"], hashlib.sha256(CODE.encode()).hexdigest())

    def test_bundle_includes_bootstrap_all_checkpoints_and_recovery_run_rc(self):
        names = {"session-step1-01" + suffix + ".zsh" for suffix in ("", "-pre", "-attempt", "-review", "-job")}
        for name in names:
            self.write(name, b"# parameters only\n")
        bootstrap = self.root / "betangel-runbook-bootstrap-step1-01"
        for extension in ("stdout", "stderr", "exit"):
            name = bootstrap.name + "." + extension
            self.write(name, b"0\n" if extension == "exit" else b"market_1.987654321")
            names.add(name)
        rc, text = self.invoke("publish-attempt", self.root, "step1-01", SHA, "", "--bootstrap", bootstrap)
        self.assertEqual(rc, 0, text)
        self.assertEqual({json.loads(row)["raw_report"]["lab_local_file"] for row in text.splitlines()}, names)
        self.assertNotIn("987654321", text)
        run = self.write("step8-01-run.json", {"status": "uncertain", "runner_lock": "wrong"})
        rc, text = self.invoke("publish-attempt", self.root, "step9-01", SHA, "right", "--recovery-run", run)
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(text)["runner_lock_path_matches"])

    def test_publication_fd_cannot_follow_swapped_symlinks_or_block_on_fifos(self):
        name = "step8-01-run.json"
        path = self.write(name, {"status": "refused"})
        target = self.write("private", {"status": "completed"})
        real_open = os.open
        for swap in ("symlink", "fifo"):
            def replace_then_open(filename, flags, *args, **kwargs):
                if Path(filename) == path:
                    path.unlink()
                    if swap == "symlink":
                        path.symlink_to(target)
                    else:
                        os.mkfifo(path)
                return real_open(filename, flags, *args, **kwargs)
            with mock.patch.object(lab.os, "open", side_effect=replace_then_open):
                rc, text = self.invoke("publish", "run", path, "", name)
            self.assertEqual(rc, 2)
            self.assertNotIn('"status": "completed"', text)
            path.unlink()
            path.write_text('{"status":"refused"}')

    def test_publication_uses_fd_metadata_caps_bytes_and_keeps_the_open_inode(self):
        name = "step8-01-run.json"
        path = self.write(name, {"status": "refused"})
        original_size = path.stat().st_size
        real_fstat = os.fstat
        def swap_after_open(fd):
            info = real_fstat(fd)
            path.unlink()
            path.write_text('{"status":"completed"}')
            return info
        with mock.patch.object(lab.os, "fstat", side_effect=swap_after_open):
            rc, text = self.invoke("publish", "run", path, "", name)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(text)["status"], "refused")
        self.assertEqual(json.loads(text)["raw_report"]["bytes"], original_size)
        other = self.write("other", b"private")
        self.assertEqual(self.invoke("publish", "run", other, "", name)[0], 2)
        with path.open("wb") as handle:
            handle.truncate(32 * 1024 * 1024 + 1)
        self.assertEqual(self.invoke("publish", "run", path, "", name)[0], 2)


class ExtractorBranchTests(ToolTest):
    def assert_refused(self, arguments, code=None):
        rc, text = self.invoke(*arguments)
        self.assertEqual(rc, 2, text)
        value = json.loads(text)
        self.assertEqual(value["status"], "refused")
        if code is not None:
            self.assertEqual(value["code"], code)
        self.assertNotIn("1.987654321", text)

    def test_identity_and_l2_l3_each_guard(self):
        identity = {"no_site": 1, "enable_user_site": None, "pycache_prefix": None, "sitecustomize": False,
                    "usercustomize": False, "python_env": {}, "sys_path": [""] + sys.path}
        path = self.write("identity", identity)
        self.assertEqual(self.invoke("identity-gate", path)[0], 0)
        for key, value, code in (("no_site", 0, "site_or_pycache"), ("enable_user_site", True, "site_or_pycache"),
                                 ("pycache_prefix", "x", "site_or_pycache"), ("sitecustomize", True, "site_or_pycache"),
                                 ("usercustomize", True, "site_or_pycache"),
                                 ("python_env", {"PYTHONPATH": "x"}, "python_env"), ("sys_path", [], "sys_path")):
            self.write("identity", {**identity, key: value})
            self.assert_refused(("identity-gate", path), "identity:" + code)
        l2 = {"status": "indexed", "archives": [{"state": "unchanged", "archive_sha256": SHA}]}
        path = self.write("l2", l2)
        self.assertEqual(self.invoke("l2-check", path, SHA)[0], 0)
        for keys, value, code in ((["status"], "refused", "not_one_indexed_archive"),
                                  (["archives"], [], "not_one_indexed_archive"),
                                  (["archives", 0, "state"], "written", "not_the_expected_unchanged_archive"),
                                  (["archives", 0, "archive_sha256"], "b" * 64, "not_the_expected_unchanged_archive")):
            self.write("l2", mutated(l2, keys, value))
            self.assert_refused(("l2-check", path, SHA), "index:" + code)
        identity = {"source_descriptor_sha256": SHA, "interpreter": {"v": 1}, "gate_snapshot_sha256": SHA}
        ip = self.write("identity", identity)
        l3 = {"status": "completed", "refusal": None, "input_preflight_would_refuse": None,
              "records": {"with_problems": 0}, "naming": {"code_identity": CODE, **identity}}
        path = self.write("l3", l3)
        self.assertEqual(self.invoke("l3-check", path, ip, CODE)[0], 0)
        for keys, value, code in ((["status"], "refused", "not_clean"), (["refusal"], "input:x", "not_clean"),
                                  (["input_preflight_would_refuse"], "input:x", "not_clean"),
                                  (["records", "with_problems"], 1, "not_clean"),
                                  (["naming", "code_identity"], "d" * 40, "code_identity")):
            self.write("l3", mutated(l3, keys, value))
            self.assert_refused(("l3-check", path, ip, CODE), "inventory:" + code)
        for key in identity:
            self.write("l3", mutated(l3, ("naming", key), "different"))
            self.assert_refused(("l3-check", path, ip, CODE), "inventory:identity_mismatch:" + key)

    def test_runtime_child_rc2_invalid_json_and_wrong_cwd_refuse(self):
        for rc, output, expected in ((2, b'{"status":"refused","code":"ledger:locked"}', "ledger:locked"),
                                      (0, b"not-json", "runtime:check_failed"),
                                      (0, b"[]", "runtime:check_failed")):
            with mock.patch.object(lab.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, output)):
                with mock.patch.object(Path, "cwd", return_value=self.root):
                    self.assert_refused(("ledger-verify", self.root, self.root / "ledger"), expected)
        with mock.patch.object(lab.subprocess, "run") as child:
            self.assert_refused(("identity", self.root), "extractor:shape")
        child.assert_not_called()

    def test_locks_gate_real_files_symlinks_extra_entries_and_notes(self):
        state = self.root / "state"
        state.mkdir()
        contract = state / "contracts" / SHA
        self.assertEqual(self.invoke("no-locks", state, SHA)[0], 0)
        contract.mkdir(parents=True)
        self.assertEqual(self.invoke("lock-entries", contract)[0], 0)
        self.assertEqual(self.invoke("no-locks", state, SHA)[0], 0)
        for name in ("runner.lock", "trial-ledger-v1.jsonl.lock"):
            lock = contract / name
            lock.write_text("lock")
            self.assert_refused(("no-locks", state, SHA), "recovery:lock_remains")
            lock.unlink()
        for kind in ("file", "directory", "link"):
            path = contract / ("extra" if kind == "file" else "runner.lock")
            path.write_text("x") if kind == "file" else path.mkdir() if kind == "directory" else path.symlink_to(state)
            self.assert_refused(("lock-entries", contract), "recovery:unexpected_entry_or_type")
            path.rmdir() if kind == "directory" else path.unlink()
        alias = state / "alias"
        alias.symlink_to(contract)
        self.assert_refused(("lock-entries", alias), "extractor:shape")
        self.assert_refused(("no-locks", alias, SHA), "recovery:directory_not_plain")
        for raw, good in (("run run-1 " + CODE, True), ("reconcile " + CODE, True),
                          ("run another " + CODE, False), ("reconcile wrong", False)):
            path = self.write("lock", raw.encode())
            self.assertEqual(self.invoke("lock-match", path, "run-1", CODE)[0], 0 if good else 2)
        note = {"kind": "started", "code_identity": CODE, "trial_id": "run-1.ltp_favourite"}
        path = self.write("lock", note)
        self.assertEqual(self.invoke("lock-match", path, "run-1", CODE)[0], 0)
        for key in note:
            self.write("lock", {**note, key: "1.987654321"})
            self.assertEqual(self.invoke("lock-match", path, "run-1", CODE), (2, "lock_note_mismatch\n"))

    def test_paths_are_pinned_and_each_disjointness_gate_refuses(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        raw = b"worktree " + os.fsencode(checkout) + b"\0"
        targets = [self.root / name for name in ("data", "state", "evidence", "bootstrap")]
        with mock.patch.object(lab.subprocess, "check_output", return_value=raw) as git:
            self.assertEqual(self.invoke("paths", checkout, *targets)[0], 0)
            self.assertEqual(git.call_args.args[0][0], "/usr/bin/git")
            for path, code in ((Path("relative"), "not_absolute_normal"),
                               (checkout / "evidence", "worktree_overlap"),
                               (self.root / ".config/betangel-research/betfair/out", "denied_tree"),
                               (targets[1] / "nested", "root_overlap")):
                if path.is_absolute():
                    path.parent.mkdir(parents=True, exist_ok=True)
                self.assert_refused(("paths", checkout, path, *targets[1:]), "paths:" + code)
            link = self.root / "link"
            link.symlink_to(checkout)
            self.assert_refused(("paths", checkout, link / "child", *targets[1:]), "paths:parent_not_plain")
        for error in (subprocess.CalledProcessError(128, ["git"], stderr=b"market_1.987654321"), OSError("secret")):
            with mock.patch.object(lab.subprocess, "check_output", side_effect=error):
                self.assert_refused(("paths", checkout, *targets), "paths:git_failed")

    def test_inputs_exact_layout_identity_hashes_and_record_count(self):
        data = self.root / "data"
        for directory in ("incoming", "raw", "manifests", "index/" + SHA):
            (data / directory).mkdir(parents=True)
        files = {"manifest": data / "manifests" / (SHA + ".json"),
                 "head": data / "index" / SHA / "head-v1.json", "markets": data / "index" / SHA / "markets-v1.jsonl"}
        for path in files.values():
            path.write_bytes(b"{}\n")
        archive = {"archive_sha256": SHA, "markets": 1,
                   **{key + "_sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in files.items() for key in ({"head": "index"}.get(name, name),)}}
        l3 = {"input": {"archives": [archive]}}
        expected = self.write("l3", l3)
        self.assertEqual(self.invoke("inputs", data, SHA, expected)[0], 0)
        alias = self.root / "alias"
        alias.symlink_to(data)
        self.assert_refused(("inputs", alias, SHA, expected), "inputs:root_changed")
        extra = data / "index" / SHA / "markets.partial"
        extra.write_text("x")
        self.assert_refused(("inputs", data, SHA, expected), "inputs:entry_set")
        extra.unlink()
        files["head"].unlink()
        files["head"].symlink_to(files["manifest"])
        self.assert_refused(("inputs", data, SHA, expected), "inputs:entry_type")
        files["head"].unlink()
        files["head"].write_bytes(b"{}\n")
        for key, value, code in (("archive_sha256", "b" * 64, "archive_changed"), ("markets", 2, "line_count_changed"),
                                  ("manifest_sha256", "b" * 64, "hash_changed:manifest"),
                                  ("index_sha256", "b" * 64, "hash_changed:head"),
                                  ("markets_sha256", "b" * 64, "hash_changed:markets")):
            self.write("l3", {"input": {"archives": [{**archive, key: value}]}})
            self.assert_refused(("inputs", data, SHA, expected), "inputs:" + code)
        self.write("l3", {"input": {"archives": [archive, archive]}})
        self.assert_refused(("inputs", data, SHA, expected), "inputs:archive_count")

    def test_remaining_state_digest_prefix_census_and_freeze_guards(self):
        state = self.root / "state"
        (state / "certifications").mkdir(parents=True)
        self.assertEqual(self.invoke("run-state", state)[0], 0)
        (state / "contracts").mkdir()
        self.assert_refused(("run-state", state), "run:state_not_certifications_only")
        for command, value in (("reconciled", {"status": "uncertain"}),
                               ("contract-sha", {"status": "valid_draft", "contract_sha256": SHA}),
                               ("index-sha", {"files": {"head": {"sha256": "1.987654321"}}}),
                               ("cert-key", {"key": "1.987654321"})):
            self.assert_refused((command, self.write("report", value)))
        for prefix in ("../x", "market-01", "hidden_01-2"):
            self.assert_refused(("new-prefix", self.root, prefix))
        before = self.write("before", {"status": "frozen", "frozen_on": "2026-10-16", "s": 1})
        after = self.write("after", {"status": "frozen", "frozen_on": "2026-10-16", "s": 1})
        self.assert_refused(("freeze-diff", before, after), "contract:not_the_expected_transition")
        empty = self.write("empty", b'{"name":"x"}\n{"status":"present_and_empty"}\n')
        self.assert_refused(("empty-state", empty))
        listing = self.root / "listing"
        listing.mkdir()
        for number in range(201):
            (listing / str(number)).touch()
        self.assertEqual(self.invoke("census", listing)[0], 2)
        link = self.root / "link"
        empty_target = self.root / "empty-target"
        empty_target.mkdir()
        link.symlink_to(empty_target)
        for target in (link, link / "child"):
            self.assertEqual(self.invoke("census", target)[0], 2)
        for status in ("1.987654321", None, {}):
            self.assertEqual(self.invoke("run-status", self.write("run", {"status": status})), (0, "unknown\n"))
        weird = self.root / "evidence[1]+(job).zsh"
        pattern = self.invoke("job-pattern", weird)[1].strip()
        self.assertIsNotNone(re.search(pattern, "runbook-job " + str(weird) + " operation"))
        self.assertIsNone(re.search(pattern, "runbook-job " + str(weird).replace("[1]+", "111") + " operation"))

    def test_zero_space_budget_state_receipt_and_wrong_l1_directory_types(self):
        data = self.root / "data"
        self.write("data/index/" + SHA + "/markets-v1.jsonl", b"{}\n")
        self.assert_refused(("space", data, SHA, "0"), "extractor:shape")
        self.write("session-step8-01-job.zsh", b"# parameters\n")
        self.write("step8-01-run.json", b"{}\n")
        state = self.write("step8-01-state-check.txt.exit", b"2\n")
        self.assert_refused(("recovery-attempt", self.root, "step8-01"), "recovery:state_check_not_zero")
        state.write_bytes(b"0\n")
        self.assertEqual(self.invoke("recovery-attempt", self.root, "step8-01")[0], 0)
        rows = [{"name": key, "mode": "drwx------", "size": 0} for key in ("incoming", "raw", "manifests", "index")]
        rows[1]["mode"] = "lrwx------"
        data_census = self.write("data-census", b"".join(json.dumps(row).encode() + b"\n" for row in rows) +
                                 b'{"status":"present_with_entries"}\n')
        absent = self.write("absent-census", b'{"status":"absent"}\n')
        parent = self.write("parent-census", b'{"status":"present_and_empty"}\n')
        rc, text = self.invoke("l1-layout", absent, data_census, parent)
        self.assertEqual((rc, json.loads(text)["reason"]), (2, "data_layout"))


class RunbookRegressionTests(ToolTest):
    def assert_no_sentinel(self, text):
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, text)
            self.assertNotIn(hashlib.sha256(sentinel.encode()).hexdigest(), text)

    def test_named_refusal_vocabulary_is_independent_of_the_implementation(self):
        self.assertEqual(lab.EXTRACTOR_CODES, EXPECTED_EXTRACTOR_CODES)
        for error in (SystemExit("inputs:market_987654321"), lab.ExtractorRefusal("market_1.987654321"),
                      lab.ExtractorRefusal("inputs:market_987654321")):
            with mock.patch.dict(lab.COMMANDS, {"probe": (0, mock.Mock(side_effect=error))}):
                rc, text = self.invoke("probe")
            self.assertEqual((rc, json.loads(text)), (2, {"status": "refused", "code": "extractor:unexpected"}))

    def test_refusal_projection_and_check_shapes_cannot_publish_raw_codes(self):
        for mode in ("check", "certificate", "binding", "l2", "l3", "inputs", "entries", "identity", "ledger"):
            for sentinel in SENTINELS:
                rc, text = self.publish(mode, {"status": "refused", "code": sentinel})
                self.assertEqual(rc, 0, (mode, text))
                self.assertEqual(json.loads(text)["code"], "unclassified")
                self.assert_no_sentinel(text)
        for raw in (b"unchanged\nmarket_1.987654321", b"1.987654321 tree 314159265"):
            rc, text = self.publish("check", raw)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(text).get("classification"), "unclassified")
            self.assert_no_sentinel(text)
        for name, code in (("step5b-01-settings.txt", "contract:settings_changed"),
                           ("step1-01-identity-gate.txt", "identity:sys_path")):
            self.assertEqual(lab.registered_name(name), "check")
            rc, text = self.publish("check", {"status": "refused", "code": code}, name)
            self.assertEqual((rc, json.loads(text)["code"]), (0, code))

    def test_digest_fields_require_the_whole_hash_and_checkpoints_hash_exact_bytes(self):
        fields = {"contract": ("contract_sha256",), "l3": ("input_sha256",), "identity": ("key",),
                  "certificate": ("sha256",), "binding": ("certificate", "key"),
                  "run": ("receipt", "certificate_sha256")}
        for mode, path in fields.items():
            for value in (SHA + "market_1.987654321", SHA + "a"):
                rc, text = self.publish(mode, mutated(fixtures()[mode][1], path, value))
                self.assertEqual(rc, 2, (mode, text))
                self.assert_no_sentinel(text)
        self.assertEqual(self.publish("digest", (SHA + " market_1.987654321\n").encode())[0], 2)
        raw = b"  # exact checkpoint bytes\n\n"
        rc, text = self.publish("checkpoint", raw)
        self.assertEqual((rc, json.loads(text)["checkpoint_sha256"]), (0, hashlib.sha256(raw).hexdigest()))

    def test_liveness_census_and_ledger_heads_are_narrow_projections(self):
        row = "job_pgrep_exit=1 writer_pgrep_exit=1\n"
        for raw in (row + "block_stdout=market_1.987654321\n", row + "block_stdout=present\nblock_stdout=missing\n",
                    "market_1.987654321 " + row + "block_stdout=present\n"):
            rc, text = self.publish("liveness", raw.encode())
            self.assertEqual(rc, 2)
            self.assert_no_sentinel(text)
        rc, text = self.publish("census", b'{"status":"market_1.987654321"}\n')
        self.assertEqual((rc, json.loads(text)["status"]), (0, "unclassified"))
        self.assert_no_sentinel(text)
        for mode in ("ledger", "run", "recovery"):
            value = json.loads(self.publish(mode, fixtures()[mode][1])[1])
            fields = [value.get("head"), value.get("ledger_head")]
            if "receipt" in value:
                fields += [value["receipt"]["ledger_head"], value["receipt"]["trial_heads"]]
            self.assertNotIn(SHA, json.dumps(fields))
        self.assertEqual(lab.code("index:market_1.987654321:gate_changed"), "index")

    def test_every_registry_variable_slot_rejects_identifier_text(self):
        names = ("l2-01-before-census-_index_market_1.987654321.jsonl", "freeze-c1-01-market_1.987654321",
                 "session-step1-01-market_1.987654321.zsh", "betangel-runbook-bootstrap-market_1.987654321.stdout",
                 "freeze-market_1.987654321-01-git.txt", "step1-market_1.987654321-identity.json")
        for name in names:
            self.assertIsNone(lab.registered_name(name), name)
            rc, text = self.invoke("publish", "raw", self.root / name, "", name)
            self.assertEqual((rc, json.loads(text)["code"]), (2, "publication:unregistered_name"))
            self.assert_no_sentinel(text)

    def test_bundle_reports_nonregular_and_malformed_entries_then_continues(self):
        target = self.write("target", b"market_1.987654321")
        for kind in ("symlink", "fifo"):
            path = self.root / "step8-01-run.json"
            path.symlink_to(target) if kind == "symlink" else os.mkfifo(path)
            rc, text = self.invoke("publish-attempt", self.root, "step8-01", SHA, "")
            self.assertEqual(rc, 2, text)
            self.assertEqual(json.loads(text)["raw_report"]["lab_local_file"], path.name)
            self.assert_no_sentinel(text)
            path.unlink()
        self.write("step6-01-certificate-check.json", {"status": "certified"})
        self.write("step6-01-status.txt", b"completed\n")
        rc, text = self.invoke("publish-attempt", self.root, "step6-01", SHA, "")
        rows = [json.loads(row) for row in text.splitlines()]
        self.assertEqual(rc, 2)
        self.assertTrue(any(row.get("code") == "publication:unclassified" for row in rows))
        self.assertTrue(any(row.get("code") == "completed" for row in rows))

    def test_lock_note_mismatch_controls_bundle_rc_independently_of_lock_path(self):
        run = self.write("step8-01-run.json", {"status": "uncertain", "runner_lock": "expected"})
        for kind in ("runner", "ledger"):
            path = self.write("step9-01-" + kind + "-lock-check.txt", b"lock_note_mismatch\n")
            rc, text = self.invoke("publish-attempt", self.root, "step9-01", SHA, "expected", "--recovery-run", run)
            rows = [json.loads(row) for row in text.splitlines()]
            self.assertEqual(rc, 2)
            self.assertTrue(any(row.get("code") == "lock_note_mismatch" for row in rows))
            self.assertTrue(any(row.get("runner_lock_path_matches") is True for row in rows))
            self.assertNotIn('"runner_lock_matches"', text)
            path.unlink()
        for status in ("completed", "failed"):
            self.write(run.name, {"status": status})
            rc, text = self.invoke("publish-attempt", self.root, "step9-01", SHA, "", "--recovery-run", run)
            self.assertEqual((rc, json.loads(text)["status"]), (0, status))
        for partial in ("expect", "expected-tail", ""):
            for mode in ("run", "recovery"):
                rc, text = self.publish(mode, {"status": "uncertain", "runner_lock": partial}, lock="expected")
                self.assertEqual((rc, json.loads(text)["runner_lock_path_matches"]), (2, False))

    def test_identity_child_wrong_checkout_is_a_named_json_refusal(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = lab.runtime_check(ROOT, lab.IDENTITY_CODE, [str(self.root)])
        self.assertEqual((rc, json.loads(output.getvalue())),
                         (2, {"status": "refused", "code": "identity:not_the_dedicated_checkout"}))

    def test_notice_consent_duplicate_and_single_bracket_edges(self):
        rows = ["DATE=2026-10-17", "MESSAGE_REF=synthetic", "NOTICE_TEXT=notice sent", "CONSENT_TEXT=consent given",
                "INPUT_SHA=" + SHA, "CERT_SHA=" + SHA]
        inventory = self.write("inventory", {"input_sha256": SHA})
        certificate = self.write("certificate", {"sha256": SHA})
        for index in (1, 2, 3):
            for bracket in ("<", ">"):
                changed = list(rows)
                changed[index] += bracket
                path = self.write("notice", "\n".join(changed).encode())
                rc, text = self.invoke("consent", path, inventory, certificate)
                expected = "notice:reference" if index == 1 else "consent:text"
                self.assertEqual(rc, 2, text)
                self.assertEqual(json.loads(text)["code"], expected)
        path = self.write("notice", "\n".join(rows + [rows[1]]).encode())
        rc, text = self.invoke("notice", path)
        self.assertEqual(rc, 2, text)
        self.assertEqual(json.loads(text)["code"], "notice:record")

    def test_lock_notes_and_no_follow_extractor_edges(self):
        for value in (("run run-1 wrong").encode(),
                      {"kind": "started", "code_identity": CODE, "trial_id": "run-1.unknown_variant"}):
            path = self.write("note", value)
            self.assertEqual(self.invoke("lock-match", path, "run-1", CODE), (2, "lock_note_mismatch\n"))
        target = self.write("target", {"status": "completed"})
        self.write("session-step8-01-job.zsh", b"# checkpoint\n")
        self.write("step8-01-state-check.txt.exit", b"0\n")
        linked = self.root / "step8-01-run.json"
        linked.symlink_to(target)
        self.assertEqual(self.invoke("run-status", linked), (0, "no_json\n"))
        self.assertEqual(self.invoke("recovery-attempt", self.root, "step8-01")[0], 2)
        directory = self.root / "locks"
        directory.mkdir()
        (directory / "runner.lock").symlink_to(target)
        self.assertEqual(self.invoke("lock-entries", directory)[0], 2)
        for path in (self.root / "missing", self.root / "missing-parent" / "missing"):
            self.assertEqual(self.invoke("no-locks", path, SHA)[0], 2)

    def test_secondary_gates_reject_extra_archives_status_session_alias_and_history(self):
        archive = {"state": "unchanged", "archive_sha256": SHA}
        path = self.write("index", {"status": "indexed", "archives": [archive, archive]})
        self.assertEqual(self.invoke("l2-check", path, SHA)[0], 2)
        before = self.write("before", {"status": "draft", "frozen_on": None})
        after = self.write("after", {"status": "draft", "frozen_on": None})
        self.assertEqual(self.invoke("freeze-diff", before, after)[0], 2)
        self.write("session-l2-17-review.zsh", b"# earlier checkpoint\n")
        self.assertEqual(self.invoke("new-prefix", self.root, "l2-17")[0], 2)
        self.write("data/index/" + SHA + "/markets-v1.jsonl", b"{}\n")
        alias = self.root / "alias"
        alias.symlink_to(self.root / "data")
        self.assertEqual(self.invoke("space", alias, SHA)[0], 2)
        state = self.write("state", b'{"status":"absent"}\n')
        data = self.write("data-census", b"".join(json.dumps({"name": n, "mode": "drwx------"}).encode() + b"\n"
                          for n in ("incoming", "raw", "manifests", "index")) + b'{"status":"present_with_entries"}\n')
        parent = self.write("parent", b'{"name":"contracts","mode":"drwx------"}\n'
                            b'{"status":"present_with_entries"}\n')
        rc, text = self.invoke("l1-layout", state, data, parent)
        self.assertEqual((rc, json.loads(text)["reason"]), (2, "parent_layout"))

    def test_casefold_library_denial_and_author_multiple_result_shapes(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        raw = b"worktree " + os.fsencode(checkout) + b"\0"
        denied = self.root / "LiBrArY" / "Application Support" / "Betangel-Research" / "Betfair" / "out"
        denied.parent.mkdir(parents=True)
        with mock.patch.object(lab.subprocess, "check_output", return_value=raw):
            rc, text = self.invoke("paths", checkout, denied, self.root / "state", self.root / "ev", self.root / "boot")
        self.assertEqual(rc, 2, text)
        self.assertEqual(json.loads(text)["code"], "paths:denied_tree")
        prefix = "freeze-c1-01"
        self.write(prefix + "-suite.log", b"Ran 1 test in 0.01s\nFAILED (failures=1)\nOK\n")
        self.write(prefix + "-suite.exit", b"0\n")
        self.write(prefix + "-git.txt", ("b" * 40 + "\n" + "c" * 40 + "\n").encode())
        self.write(prefix + "-diff.txt", b"research/experiments/backtest-p0-bsp-v1.json\nscripts/other.py\n")
        self.author_states(prefix, "c1")
        rc, text = self.invoke("author-suite", self.root, prefix, "c1")
        self.assertEqual((rc, json.loads(text)), (2, {"status": "refused", "code": "author:c1_diff"}))
        self.write(prefix + "-diff.txt", b"research/experiments/backtest-p0-bsp-v1.json\n")
        rc, text = self.invoke("author-suite", self.root, prefix, "c1")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(text)["status"], "unclassified")


class BindingBranchTests(ToolTest):
    assert_refused = ExtractorBranchTests.assert_refused
    # This materializes the producers' real field shapes, rebinding artifact hashes
    # after each fixture change so a target gate cannot hide behind an earlier hash gate.
    def binding_fixture(self):
        variants = sorted(lab.VARIANTS)
        criteria = {
            "known_accounting_answers": {"status": "passed", "failed_cases": []},
            "issuance_exposure_reconciliation": {"status": "passed", "problems": [], "unexercised_variants": []},
            "prefix_invariance": {"status": "passed", "components": {
                "synthetic_certification": {"status": "passed"},
                "primary_source_prefix_consistency": {"counts": {"records": 12, "candidates": 12,
                    "compared": 12, "passed": 12, "failed": 0, "missing": 0, "failed_against_record": 0},
                    "counts_reconcile": True, "unexercised_by_reason": {}, "unissued_variants": []}}}}
        pop = {"records": 12, "by_class": {"eligible": 12}, "by_reason": {}}
        result = {"run_id": "run-1", "contract_sha256": SHA, "code_identity": CODE, "input": input_descriptor(),
                  "input_sha256": SHA, "population": pop, "certification": {"key": SHA},
                  "engineering_acceptance": {"status": "passed", "criteria": criteria},
                  "settlement": {"market_1.987654321": {"ltp": 12.345, "stake": 9876}}}
        receipt = {"status": "completed", "run_id": "run-1", "contract_sha256": SHA,
                   "engineering_acceptance": {"status": "passed"}}
        output = {"status": "completed", "engineering_acceptance": "passed"}
        l3 = {"input": input_descriptor(), "input_sha256": SHA, "records": {"lines": 12, "with_problems": 0},
              "population": {"by_class": {"eligible": 12}, "by_reason": {}}}
        entries = [{"kind": "started", "trial_id": "run-1." + variant, "variant": variant,
                    "input_sha256": SHA, "contract_sha256": SHA, "code_identity": CODE, "summary": {}}
                   for variant in variants]
        return {"result": result, "receipt": receipt, "output": output, "l3": l3, "entries": entries,
                "contract": {"variants": [{"id": variant} for variant in variants]}, "cert": {"key": SHA},
                "run_rc": 0}

    def materialize(self, fixture, late=None):
        state = self.root / "state"
        run = state / "runs" / SHA / "run-1"
        certificate = self.write("stored-certificate", {"status": "certified"})
        cert_sha = hashlib.sha256(certificate.read_bytes()).hexdigest()
        fixture["cert"].update(path=str(certificate), sha256=cert_sha)
        fixture["result"]["certification"]["sha256"] = cert_sha
        fixture["receipt"]["certificate_sha256"] = cert_sha
        for entry in fixture["entries"]:
            entry["summary"]["certificate_sha256"] = cert_sha
        issuance = self.write(str(run / "issuance-ledger.jsonl"), b'{"market":"1.987654321","stake":9876}\n')
        fixture["receipt"]["issuance_ledger_sha256"] = hashlib.sha256(issuance.read_bytes()).hexdigest()
        fixture["receipt"]["issuance_ledger_bytes"] = issuance.stat().st_size
        if late:
            late(fixture)
        result = self.write(str(run / "results.json"), fixture["result"])
        result_sha = hashlib.sha256(result.read_bytes()).hexdigest()
        fixture["receipt"].setdefault("results_sha256", result_sha)
        fixture["output"].setdefault("results_sha256", result_sha)
        receipt = self.write(str(run / "receipt.json"), fixture["receipt"])
        fixture["output"].setdefault("receipt_sha256", hashlib.sha256(receipt.read_bytes()).hexdigest())
        self.write(str(state / "contracts" / SHA / "trial-ledger-v1.jsonl"),
                   b"".join(json.dumps(row).encode() + b"\n" for row in fixture["entries"]))
        for name in ("contract", "l3", "cert", "output"):
            self.write(name, fixture[name])
        return ("run-binding", self.root, state, self.root / "contract", SHA, "run-1", CODE,
                fixture["run_rc"], self.root / "l3", self.root / "cert", self.root / "output")

    def test_binding_accepts_pass_failed_and_incomplete_without_outcomes(self):
        for status, rc in (("passed", 0), ("failed", 4), ("incomplete", 4)):
            fixture = self.binding_fixture()
            for name in ("result", "receipt"):
                fixture[name]["engineering_acceptance"]["status"] = status
            fixture["output"]["engineering_acceptance"] = status
            fixture["run_rc"] = rc
            rc, text = self.invoke(*self.materialize(fixture))
            self.assertEqual(rc, 0, text)
            value = json.loads(text)
            self.assertEqual(value["acceptance_status"], status)
            self.assertEqual(len(value["started"]), 2)
            self.assertNotIn("1.987654321", text)
            self.assertNotIn("settlement", text)
            pub_rc, published = self.publish("binding", value)
            self.assertEqual(pub_rc, 0, published)
            self.assertNotIn("1.987654321", published)

    def test_each_binding_guard_has_a_positive_baseline_and_a_targeted_refusal(self):
        changes = [
            (("output", "status"), "failed", "not_completed"),
            (("result", "run_id"), "wrong", "run_id"),
            (("result", "contract_sha256"), "b" * 64, "contract"),
            (("result", "code_identity"), "wrong", "code"),
            (("result", "input_sha256"), "b" * 64, "input"),
            (("result", "input", "archives", 0, "markets"), 13, "input"),
            (("l3", "records", "with_problems"), 1, "records"),
            (("result", "population", "records"), 13, "records"),
            (("result", "population", "by_class"), {"eligible": 11}, "population"),
            (("result", "certification", "key"), "b" * 64, "certificate_key"),
            (("result", "engineering_acceptance", "status"), "unknown", "unknown_acceptance"),
            (("receipt", "engineering_acceptance", "status"), "failed", "acceptance_mismatch"),
            (("run_rc",), 4, "exit_mismatch"),
            (("entries",), [], "starts"),
            (("entries", 0, "trial_id"), "run-1.bad", "trial_id"),
            (("entries", 0, "input_sha256"), "b" * 64, "start_input"),
            (("entries", 0, "contract_sha256"), "b" * 64, "start_input"),
            (("entries", 0, "code_identity"), "wrong", "start_code_or_certificate"),
            (("result", "engineering_acceptance", "criteria"), {}, "criteria_set"),
            (("result", "engineering_acceptance", "criteria", "known_accounting_answers", "status"),
             "unknown", "criterion_status"),
        ]
        tally = ("result", "engineering_acceptance", "criteria", "prefix_invariance", "components",
                 "primary_source_prefix_consistency")
        changes += [(tally + ("counts", "compared"), -1, "prefix_counts"),
                    (tally + ("counts", "records"), 13, "prefix_records"),
                    (tally + ("unexercised_by_reason",), {"1.987654321": 1}, "prefix_reasons"),
                    (tally + ("unexercised_by_reason",), {"prefix_buffer_limit": -1}, "prefix_reasons"),
                    (tally + ("unissued_variants",), ["1.987654321"], "unexercised_variants"),
                    (("result", "engineering_acceptance", "criteria", "issuance_exposure_reconciliation",
                      "unexercised_variants"), ["1.987654321"], "unexercised_variants")]
        for path, value, code in changes:
            with self.subTest(code=code, path=path):
                fixture = mutated(self.binding_fixture(), path, value)
                self.assert_refused(self.materialize(fixture), "binding:" + code)
        late = [(("receipt", "certificate_sha256"), "b" * 64, "certificate"),
                (("receipt", "results_sha256"), "b" * 64, "results"),
                (("output", "receipt_sha256"), "b" * 64, "receipt"),
                (("receipt", "issuance_ledger_sha256"), "b" * 64, "issuance"),
                (("receipt", "issuance_ledger_bytes"), 0, "issuance_size"),
                (("entries", 0, "summary", "certificate_sha256"), "b" * 64, "start_code_or_certificate")]
        for path, value, code in late:
            with self.subTest(code=code):
                def change(fixture):
                    changed = mutated(fixture, path, value)
                    fixture.clear()
                    fixture.update(changed)
                self.assert_refused(self.materialize(self.binding_fixture(), change), "binding:" + code)
        arguments = self.materialize(self.binding_fixture())
        (self.root / "stored-certificate").write_text("changed")
        self.assert_refused(arguments, "binding:stored_certificate")
        arguments = self.materialize(self.binding_fixture())
        result = self.root / "state/runs" / SHA / "run-1/results.json"
        result.unlink()
        result.symlink_to(self.root / "output")
        self.assert_refused(arguments, "binding:file_not_regular")
        result.unlink()
        alias = self.root / "alias"
        alias.symlink_to(self.root)
        arguments = list(self.materialize(self.binding_fixture()))
        arguments[8] = alias / "l3"
        self.assert_refused(arguments, "binding:parent_not_plain")

    def test_binding_builder_sanitizes_real_problem_and_decision_id_tails(self):
        from betangel_research.betfair.backtest import cohort
        draft = json.loads((ROOT / "research/experiments/backtest-p0-bsp-v1.json").read_bytes())
        draft["status"], draft["frozen_on"] = "frozen", "2026-10-16"
        rules = cohort.CohortRules.from_contract(draft)
        decision = cohort.decision_id(rules, "1.987654321", 271828182)
        fixture = self.binding_fixture()
        criterion = fixture["result"]["engineering_acceptance"]["criteria"]["issuance_exposure_reconciliation"]
        criterion["problems"] = ["bets:duplicate:ltp_favourite:" + decision, "settled:duplicate:x:" + decision,
                                 "issuance:1.987654321", "metrics:1.987654321", "rows:2_not_their_decisions",
                                 "decisions:missing_for_2_records", "1.987654321"]
        rc, text = self.invoke(*self.materialize(fixture))
        self.assertEqual(rc, 0, text)
        self.assertNotIn(decision, text)
        self.assertNotIn("1.987654321", text)
        rc, text = self.publish("binding", json.loads(text))
        self.assertEqual(rc, 0, text)
        self.assertNotIn(decision, text)

    def test_certificate_each_guard_uses_bound_stored_bytes(self):
        def fixture():
            counts = {"cases": 38, "as_expected": 38, "not_as_expected": 0, "error": 0}
            identity = {"source_descriptor_sha256": SHA, "interpreter": {"version": 1}}
            return {"stored": {"key": SHA, "status": "certified", "counts": counts,
                               "source": {"sha256": SHA}, "interpreter": {"version": 1}},
                    "output": {"status": "certified", "counts": counts, "failed_cases": [], "problems": [],
                               "key": SHA, "source_descriptor_sha256": SHA},
                    "l3": {"naming": copy.deepcopy(identity)}, "identity": identity}
        def arguments(value):
            path = self.write("cert-state/certifications/" + SHA + ".json", value["stored"])
            value["output"].setdefault("certificate_path", str(path))
            value["output"].setdefault("certificate_sha256", hashlib.sha256(path.read_bytes()).hexdigest())
            for name in ("output", "l3", "identity"):
                self.write(name, value[name])
            return ("certificate-check", self.root / "cert-state", SHA, "attempt-1",
                    self.root / "output", self.root / "l3", self.root / "identity")
        self.assertEqual(self.invoke(*arguments(fixture()))[0], 0)
        changes = [(("output", "status"), "already_certified", "not_new_38_of_38"),
                   (("output", "counts"), {}, "not_new_38_of_38"),
                   (("output", "failed_cases"), ["case"], "problems"),
                   (("output", "problems"), ["1.987654321"], "problems"),
                   (("output", "key"), "b" * 64, "path_or_key"),
                   (("output", "certificate_path"), "/wrong", "path_or_key"),
                   (("output", "certificate_sha256"), "b" * 64, "bytes"),
                   (("stored", "key"), "b" * 64, "stored_status_or_key"),
                   (("stored", "status"), "failed", "stored_status_or_key"),
                   (("stored", "counts"), {}, "stored_counts")]
        for target in ("stored", "output", "identity", "l3"):
            source_path = {"stored": ("source", "sha256"), "l3": ("naming", "source_descriptor_sha256")}.get(
                target, ("source_descriptor_sha256",))
            changes.append(((target, *source_path), "b" * 64, "source"))
        for target in ("stored", "identity", "l3"):
            changes.append(((target, *(("naming", "interpreter") if target == "l3" else ("interpreter",))),
                            {"version": 2}, "interpreter"))
        for path, value, code in changes:
            with self.subTest(code=code, path=path):
                self.assert_refused(arguments(mutated(fixture(), path, value)), "certificate:" + code)
        args = arguments(fixture())
        path = self.root / "cert-state/certifications" / (SHA + ".json")
        path.unlink()
        path.symlink_to(self.root / "output")
        self.assert_refused(args, "certificate:not_regular")


class RegistryAndShellTests(ToolTest):
    def shell_environment(self):
        tool = self.root / "step1-01-lab_runbook.py"
        shell = self.root / "step1-01-lab_runbook.zsh"
        shutil.copyfile(TOOL, tool)
        shutil.copyfile(SHELL, shell)
        required = set()
        for names in re.findall(r"(?m)^\s+typeset -p ([A-Z0-9_ ]+)$", SHELL.read_text()):
            required.update(names.split())
        return {**{name: "" for name in required}, "PATH": "/usr/bin:/bin", "LAB_EVIDENCE": str(self.root),
                "LAB_PY": sys.executable, "LAB_TOOL": str(tool), "LAB_SHELL": str(shell),
                "LAB_TOOL_SHA": hashlib.sha256(tool.read_bytes()).hexdigest(),
                "LAB_SHELL_SHA": hashlib.sha256(shell.read_bytes()).hexdigest(), "BASIC_SHA": SHA,
                "LAB_LOCK": "expected", "STEP1_PREFIX": "step1-01", "STEP9_PREFIX": "step9-01"}

    def run_shell(self, source, env=None):
        return subprocess.run(["/bin/zsh", "-f"], input='set -C -u\nsource "$LAB_SHELL" || exit 2\n' + source,
                              cwd=self.root, env=env or self.shell_environment(),
                              capture_output=True, text=True, check=False, timeout=15)

    def make_checkpoint(self):
        env = self.shell_environment()
        checkout = self.root / "checkout"
        checkout.mkdir()
        executable = self.root / "recorded-python"
        executable.symlink_to(sys.executable)
        env.update(LAB_CHECKOUT=str(checkout), LAB_PY=str(executable))
        result = subprocess.run(["/bin/zsh", "-f"], input='''set -C -u
source "$LAB_SHELL" || exit 2
lab_save_session session-step1-01 >/dev/null || exit 2
''', env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        checkout.rmdir()
        return self.root / "session-step1-01.zsh", executable

    def source_checkpoint(self, checkpoint, flag):
        env = {"PATH": "/usr/bin:/bin", "CHECKPOINT": str(checkpoint), "LAB_PUBLICATION_ONLY": str(flag)}
        return subprocess.run(["/bin/zsh", "-f"], input='''set -C -u
source "$CHECKPOINT" || exit 2
print -r -- publication_source_ready
''', cwd=self.root, env=env, capture_output=True, text=True, check=False)

    def test_module_imports_only_stdlib_and_has_no_runtime_imports(self):
        tree = ast.parse(TOOL.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module.split(".")[0])
        self.assertTrue(set(imports) <= set(sys.stdlib_module_names), imports)
        self.assertFalse(set(imports) & {"socket", "ssl", "http", "urllib"})
        self.assertNotIn(TOOL.stem.casefold(), {name.casefold() for name in sys.stdlib_module_names})

    def test_registry_reports_unknown_count_without_names(self):
        self.write("l2-01-index.json", b"{}")
        self.write("market_1.987654321", b"private")
        rc, text = self.invoke("registry", self.root)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(text)["unregistered_count"], 1)
        self.assertNotIn("987654321", text)

    def test_known_capture_extensions_are_registered_once(self):
        self.assertEqual(lab.registered_name("l2-02-index.json.stderr"), "raw")
        self.assertIsNone(lab.registered_name("l2-02-index.json.stderr.stderr"))
        self.assertEqual(lab.registered_name("step8-02-job.block.exit"), "exit")
        self.assertIsNone(lab.registered_name("step8-02-job.block.stderr.stderr"))
        self.assertEqual(lab.registered_name("step9-01-final-publication.jsonl"), "publication")
        self.assertEqual(lab.registered_name("step1-02-lab_runbook.py"), "raw")

    def test_registry_has_no_dead_suffixes(self):
        source = SHELL.read_text() + (ROOT / "docs/backtest-plan.md").read_text()
        derived = {a + "-census-" + b + ".jsonl" for a in ("before", "after", "run-before")
                   for b in ("", "_index", "_index_{archive}", "_manifests")}
        derived |= {a + "-" + b for a in ("before", "after", "locks") for b in ("census.jsonl", "entries.json")}
        derived |= {a + "-" + b + ".jsonl" for a in ("parent", "data")
                    for b in ("certifications", "contracts", "runs")}
        derived |= {a + "-session.sha" for a in ("attempt", "job", "pre", "review")}
        derived |= {"job." + a for a in ("block.stdout", "block.stderr", "launch.stdout", "launch.stderr", "pid")}
        derived |= {"recheck-" + a for a in ("census-compare.txt", "census.jsonl", "entries-compare.txt",
                    "entries.json", "git.txt", "ledger-lock-check.txt", "ledger-lock.stat", "ledger-lock.txt",
                    "ledger-note.txt", "ledger-stat.txt", "liveness.txt", "runner-lock-check.txt", "runner-lock.stat",
                    "runner-lock.txt", "runner-note.txt", "runner-stat.txt", "writers.txt")}
        derived |= {"run-identity.json", "run-identity-compare.txt", "ledger-lock-check.txt", "runner-lock-check.txt"}
        derived |= {"token-" + a + ".txt" for a in ("C0", "C0_TREE", "C1", "C1_TREE", "BASIC_SHA", "L2_INDEX_SHA",
                    "CONTRACT_SHA", "CERT_KEY", "RUN_ID", "L2_SPACE_BUDGET_KIB")}
        for suffix in lab.SUFFIXES:
            self.assertTrue(suffix in source or suffix in derived, suffix)
        for dead in ("before.txt", "transfer.txt", "tree-check.txt", "space-check.txt", "tools-check.txt",
                     "attempt-check.txt", "tooling.txt", "shell-blob.txt", "python-blob.txt", "tools-copy.txt",
                     "shell-copy.txt"):
            self.assertIsNone(lab.registered_name("step5b-01-" + dead))

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_pending_job_publication_only_refuses_without_consuming_final_names(self):
        for prefix in ("l2-11", "step6-11", "step8-11"):
            self.write("session-" + prefix + "-job.zsh", b"# parameters\n")
            env = {**self.shell_environment(), "PREFIX": prefix}
            source = '''set -C -u
source "$LAB_SHELL" || exit 2
LAB_PUBLICATION_ONLY=1
lab_liveness() { print -r -- 'job_pgrep_exit=0 writer_pgrep_exit=0'; print -r -- 'block_stdout=present'; }
lab_publication "$PREFIX" final
'''
            result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("publication:job_pending", result.stdout)
            self.assertFalse(any(p.name.startswith(prefix + "-final-") for p in self.root.iterdir()))
        source = source.replace("job_pgrep_exit=0 writer_pgrep_exit=0", "job_pgrep_exit=1 writer_pgrep_exit=1")
        result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "step8-11-final-liveness.txt").is_file())
        self.assertIn('"publication_rc":0', result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_real_liveness_reads_argv_even_without_pid_and_refuses_bad_process_queries(self):
        source = '''pgrep() {
  if (( $# == 3 )); then [[ "$1" = -d, && "$2" = -f ]] || return 99
  else [[ $# == 4 && "$1" = -d, && "$2" = -f && "$3" = -- ]] || return 99; fi
  "$LAB_PY" -I -S -B -c 'import json,os,re,sys
if os.environ["PGREP_ERROR"]: sys.exit(int(os.environ["PGREP_ERROR"]))
matches=[str(pid) for pid,argv in json.loads(os.environ["PROCESSES"]) if re.search(sys.argv[1],argv)]
if matches: print(",".join(matches))
sys.exit(0 if matches else 1)' "$@[-1]"
}
ps() { print -r -- '123 1 S 00:01 /bin/zsh'; }
lab_liveness step8-01-job
'''
        env = {**self.shell_environment(), "PGREP_ERROR": ""}
        checkpoint = str(self.root / "session-step8-01-job.zsh")
        processes = [[123, "runbook-job " + checkpoint + " hash lab_run_job /output"],
                     [124, "python -S -B -m betangel_research.betfair.backtest run-backtest"]]
        cases = ((processes, "0 writer_pgrep_exit=0"), (processes[:1], "0 writer_pgrep_exit=1"),
                 (processes[1:], "1 writer_pgrep_exit=0"), ([[125, "unrelated"]], "1 writer_pgrep_exit=1"))
        for rows, expected in cases:
            result = self.run_shell(source, {**env, "PROCESSES": json.dumps(rows)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("job_pgrep_exit=" + expected, result.stdout)
            self.assertIn("recorded_wrapper_pid=absent", result.stdout)
        for rc in (2, 3):
            result = self.run_shell(source, {**env, "PGREP_ERROR": str(rc), "PROCESSES": "[]"})
            self.assertEqual(result.returncode, 2)
        pid = self.write("step8-01-job.pid", b"not-a-pid\n")
        result = self.run_shell(source, {**env, "PROCESSES": "[]"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("liveness:pid_refused", result.stdout)
        pid.unlink()
        pid.symlink_to(self.write("other-pid", b"123\n"))
        self.assertEqual(self.run_shell(source, {**env, "PROCESSES": "[]"}).returncode, 2)

    @unittest.skipUnless(all(Path(p).exists() for p in ("/bin/zsh", "/usr/bin/nohup", "/usr/bin/caffeinate")),
                         "macOS detach tools are not installed")
    def test_real_detach_keeps_rc4_and_rehashes_checkpoint_before_source(self):
        env = self.shell_environment()
        for tamper in (False, True):
            prefix = "step8-" + ("42" if tamper else "41") + "-job"
            checkpoint = self.write("session-" + prefix + ".zsh", b'''lab_probe_job() {
  print -r -- "publication_only=$LAB_PUBLICATION_ONLY"
  return 4
}
''')
            source = '''lab_probe_job() { return 4; }
functions[/usr/bin/nohup]='
if [[ "$TAMPER" = 1 ]]; then
  print -r -- "print -r -- tampered_source_ran" >> "$CHECKPOINT"
fi
command /usr/bin/nohup "$@"
'
lab_detach "$PREFIX" "$CHECKPOINT" lab_probe_job
'''
            result = self.run_shell(source, {**env, "PREFIX": prefix, "CHECKPOINT": str(checkpoint),
                                             "TAMPER": str(int(tamper))})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = self.root / (prefix + ".block.exit")
            deadline = time.monotonic() + 5
            while (not receipt.is_file() or not receipt.read_bytes()) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(receipt.is_file(), result.stdout + result.stderr)
            self.assertEqual(receipt.read_text().strip(), "2" if tamper else "4")
            output = (self.root / (prefix + ".block.stdout")).read_text()
            self.assertNotIn("tampered_source_ran", output)
            self.assertEqual(output, "" if tamper else "publication_only=0\n")
            self.assertRegex((self.root / (prefix + ".pid")).read_text(), r"^[0-9]+\n$")

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_final_publication_mixed_changing_and_invalid_receipt_paths(self):
        env = self.shell_environment()
        cases = (("orphan", "1 0", "1 0", None, "publication:job_pending", False),
                 ("wrapper", "0 1", "0 1", None, "publication:job_pending", False),
                 ("changed", "1 1", "0 1", None, "publication:liveness_changed", True),
                 ("writer_changed", "1 1", "1 0", None, "publication:liveness_changed", True),
                 ("empty", "1 0", "1 0", b"", "publication:job_pending", False),
                 ("invalid", "1 0", "1 0", b"999\n", "publication:job_pending", False))
        source = '''lab_liveness() {
  if [[ -e "$LAB_EVIDENCE/$PREFIX-observed" ]]; then pair=(${=SECOND}); else pair=(${=FIRST}); fi
  [[ -e "$LAB_EVIDENCE/$PREFIX-observed" ]] || print -r -- observed > "$LAB_EVIDENCE/$PREFIX-observed"
  print -r -- "job_pgrep_exit=$pair[1] writer_pgrep_exit=$pair[2]"
  print -r -- 'block_stdout=present'
}
lab_publication "$PREFIX" final
'''
        for index, (name, first, second, receipt, code, captured) in enumerate(cases):
            prefix = "step8-" + str(50 + index)
            self.write("session-" + prefix + "-job.zsh", b"# checkpoint\n")
            if receipt is not None:
                self.write(prefix + "-job.block.exit", receipt)
            result = self.run_shell(source, {**env, "PREFIX": prefix, "FIRST": first, "SECOND": second})
            self.assertEqual(result.returncode, 2, (name, result.stdout, result.stderr))
            self.assertIn(code, result.stdout)
            if captured:
                self.assertNotIn("job_pending", result.stdout)
            self.assertEqual((self.root / (prefix + "-final-liveness.txt")).exists(), captured)
            self.assertFalse((self.root / (prefix + "-final-publication.jsonl")).exists())
        prefix = "step8-52"
        result = self.run_shell(source, {**env, "PREFIX": prefix, "FIRST": "1 1", "SECOND": "1 1"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("publication:final_liveness_already_used", result.stdout)
        # The first wrapper-live observation consumed no final labels. A later,
        # fresh observation of both processes gone can publish that same job.
        prefix = "step8-51"
        self.assertFalse((self.root / (prefix + "-final-liveness.txt")).exists())
        result = self.run_shell(source, {**env, "PREFIX": prefix, "FIRST": "1 1", "SECOND": "1 1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"publication_rc":0', result.stdout)
        self.assertIn("job_pgrep_exit=1 writer_pgrep_exit=1",
                      (self.root / (prefix + "-final-liveness.txt")).read_text())

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_review_phase_cannot_bypass_job_gate_or_publish_other_steps(self):
        env = self.shell_environment()
        for prefix in ("l2-61", "step6-61", "step8-61"):
            self.write("session-" + prefix + "-job.zsh", b"# checkpoint\n")
            source = '''lab_liveness() { print -r -- 'job_pgrep_exit=0 writer_pgrep_exit=0'; }
lab_publication "$PREFIX" review
'''
            result = self.run_shell(source, {**env, "PREFIX": prefix})
            self.assertEqual(result.returncode, 2)
            self.assertFalse((self.root / (prefix + "-review-publication.jsonl")).exists())

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_no_writer_refuses_pgrep_errors_and_prefix_requires_exact_success_name(self):
        env = self.shell_environment()
        for rc in (2, 3):
            result = self.run_shell('pgrep() { [[ $# == 3 && "$1" = -fl && "$2" = -- ]] || return 99; '
                                    'return "$RC"; }\nlab_no_writer "$LABEL"\n',
                                    {**env, "RC": str(rc), "LABEL": "step8-" + str(rc) + "-writers.txt"})
            self.assertNotEqual(result.returncode, 0)
        for prefix, stem in (("l2-01-job", "l2"), ("l2-01x", "l2"), ("step9-verify-01", "step9")):
            self.write("session-" + prefix + ".zsh", b"# alias\n")
            for operation in ("lab_prefix_valid", "lab_require_success"):
                result = self.run_shell(operation + ' "$PREFIX" "$STEM"\n',
                                        {**env, "PREFIX": prefix, "STEM": stem})
                self.assertEqual(result.returncode, 2, (prefix, operation, result.stdout))

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_shell_copy_hash_blocks_source_and_identity_drift_blocks_continuation(self):
        checkpoint, _ = self.make_checkpoint()
        shell = self.root / "step1-01-lab_runbook.zsh"
        with shell.open("a") as handle:
            handle.write("\nprint -r -- unverified_shell_ran\n")
        result = self.source_checkpoint(checkpoint, 1)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("unverified_shell_ran", result.stdout)
        env = self.shell_environment()
        shell.write_text(shell.read_text() + "\n# changed after pin\n")
        result = self.run_shell("lab_ready\n", env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("runbook:tool_hash_mismatch", result.stdout)
        env = self.shell_environment()
        self.write("step1-01-identity.json", b'{"identity":"baseline"}\n')
        for operation in ("lab_restore_identity", "lab_identity_check step5b-01"):
            result = self.run_shell('lab_identity() { print -r -- \'{"identity":"drift"}\'; }\n' + operation + "\n",
                                    {**env, "RESTORE_PREFIX": "restore-71"})
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_reprint_tampering_reused_publication_and_nonzero_bundle_are_refused(self):
        env = self.shell_environment()
        self.write("l2-81-check.txt", b"unchanged\n")
        result = self.run_shell("lab_publication l2-81 final\n", env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = self.run_shell("lab_publication l2-81 final\n", env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("publication:already_exists", result.stdout)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        bundle = self.root / "l2-81-final-publication.jsonl"
        original_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
        for tamper in (False, True):
            if tamper:
                bundle.write_text("market_1.987654321\n")
            result = self.run_shell('lab_reprint l2-81 final "$SHA_EXPECTED"\n',
                                    {**env, "SHA_EXPECTED": original_sha if tamper else "a" * 64})
            self.assertEqual(result.returncode, 2)
            self.assertIn("publication:reprint_hash_mismatch", result.stdout)
            self.assertNotIn("market_1.987654321", result.stdout)
        self.write("step8-81-job.block.exit", b"0\n")
        result = self.run_shell('lab_publication() { return 2; }\nlab_finished_publication step8-81\n', env)
        self.assertEqual(result.returncode, 2)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_direct_notice_and_l2_job_require_exact_whole_line_tokens(self):
        env = {**self.shell_environment(), "C0": "b" * 40, "C0_TREE": "c" * 40,
               "L2_INDEX_SHA": SHA, "L2_SPACE_BUDGET_KIB": "1", "L2_PREFIX": "l2-91"}
        values = {key: env[key] for key in ("C0", "C0_TREE", "BASIC_SHA", "L2_INDEX_SHA", "L2_SPACE_BUDGET_KIB")}
        for index, key in enumerate(values):
            prefix = "l2-" + str(90 + index)
            bad = {**values, key: values[key] + "x"}
            self.write(prefix + "-notice.md", ("DATE=2026-10-17\nMESSAGE_REF=synthetic\n" +
                       "\n".join(k + "=" + v for k, v in bad.items())).encode())
            result = self.run_shell('lab_notice "$PREFIX" ' + " ".join(values) + "\n", {**env, "PREFIX": prefix})
            self.assertEqual(result.returncode, 2, (key, result.stdout, result.stderr))
        prefix = "l2-99"
        self.write(prefix + "-notice.md", ("DATE=2026-10-17\nMESSAGE_REF=synthetic\n" +
                   "\n".join(k + "=" + v for k, v in values.items() if k != "L2_SPACE_BUDGET_KIB")).encode())
        result = self.run_shell('''lab_preflight() { return 0; }
lab_no_writer() { return 0; }
lab_l2_job
''', {**env, "L2_PREFIX": prefix})
        self.assertEqual(result.returncode, 2)
        self.assertIn("notice:token_mismatch", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_cert_finish_pending_and_poll_symlink_receipt_never_continue(self):
        env = {**self.shell_environment(), "STEP6_PREFIX": "step6-01"}
        result = self.run_shell('''lab_capture() { print -r -- forbidden_capture; return 0; }
lab_checkpoint() { print -r -- forbidden_checkpoint; }
lab_cert_finish
''', env)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("forbidden", result.stdout)
        receipt = self.root / "step6-01-job.block.exit"
        receipt.symlink_to(self.write("receipt", b"0\n"))
        result = self.run_shell("lab_poll step6-01-job\n", env)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("block_exit=", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_recovery_begin_live_stale_absent_and_wrong_note_refuse_with_bounded_evidence(self):
        base = self.shell_environment()
        source = '''lab_preflight() { print -r -- "$C1 tree $C1_TREE"; }
lab_identity_check() { return 0; }
lab_no_writer() { return 0; }
lab_checkpoint() { print -r -- "checkpoint_saved=$1"; }
lab_liveness() {
  print -r -- "job_pgrep_exit=$JOB_RC writer_pgrep_exit=1"
  print -r -- 'block_stdout=present'
}
lab_recovery_begin
'''
        for index, mode in enumerate(("good", "live", "stale", "absent", "wrong_note")):
            step8, step9 = "step8-" + str(110 + index * 2), "step9-" + str(110 + index)
            state = self.root / ("state-" + str(index))
            contract = state / "contracts" / SHA
            contract.mkdir(parents=True)
            lock = contract / "runner.lock"
            if mode != "absent":
                lock.write_text("run run-1 " + ("wrong" if mode == "wrong_note" else CODE))
            checkpoint = self.write("session-" + step8 + "-job.zsh", b"# exact attempt\n")
            self.write(step8 + "-state-check.txt.exit", b"0\n")
            self.write(step8 + "-run.json", {"status": "uncertain", "runner_lock": str(lock)})
            if mode == "stale":
                self.write("session-step8-" + str(111 + index * 2) + "-job.zsh", b"# later attempt\n")
            env = {**base, "STEP8_PREFIX": step8, "STEP9_PREFIX": step9, "LAB_STATE": str(state),
                   "CONTRACT_SHA": SHA, "C1": "b" * 40, "C1_TREE": "c" * 40, "RUN_ID": "run-1",
                   "RESTORE_FILE": str(checkpoint), "JOB_RC": "0" if mode == "live" else "1"}
            result = self.run_shell(source, env)
            self.assertEqual(result.returncode, 0 if mode == "good" else 2,
                             (mode, result.stdout, result.stderr))
            self.assertEqual("checkpoint_saved=" + step9 + "\n" in result.stdout, mode == "good")
            if mode == "stale":
                self.assertFalse(any(path.name.startswith(step9 + "-") for path in self.root.iterdir()))
            if mode == "absent":
                self.assertIn("recovery:runner_lock_absent", result.stdout)
            if mode == "wrong_note":
                self.assertIn("recovery:lock_note_mismatch", result.stdout)
        self.assertFalse(any(path.name.endswith("rm-runner-lock.txt") for path in self.root.iterdir()))

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_recovery_recheck_compares_fresh_census_entries_liveness_and_lock_note(self):
        state = self.root / "state"
        contract = state / "contracts" / SHA
        contract.mkdir(parents=True)
        lock = contract / "runner.lock"
        lock.write_text("run run-1 " + CODE)
        env = {**self.shell_environment(), "STEP8_PREFIX": "step8-01", "STEP9_PREFIX": "step9-01",
               "LAB_STATE": str(state), "CONTRACT_SHA": SHA, "C1": "b" * 40, "C1_TREE": "c" * 40,
               "RUN_ID": "run-1", "LAB_LOCK": str(lock),
               "LAB_LEDGER_LOCK": str(contract / "trial-ledger-v1.jsonl.lock"),
               "RECOVERY_CENSUS_PREFIX": "step9-01-before"}
        baseline = '''lab_lock_census step9-01-before || exit 2
lab_lock_note step9-01-runner-lock "$LAB_LOCK"
'''
        result = self.run_shell(baseline, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source = '''lab_preflight() { print -r -- "$C1 tree $C1_TREE"; }
lab_no_writer() { return 0; }
lab_liveness() {
  print -r -- "job_pgrep_exit=$JOB_RC writer_pgrep_exit=1"
  print -r -- 'block_stdout=present'
}
lab_recovery_recheck "$PREFIX"
'''
        for index, mode in enumerate(("good", "live", "entries", "note")):
            if mode == "entries":
                (contract / "trial-ledger-v1.jsonl").write_text("{}\n")
            if mode == "note":
                (contract / "trial-ledger-v1.jsonl").unlink()
                lock.write_text("run run-2 " + CODE)
            result = self.run_shell(source, {**env, "PREFIX": "step9-recheck-" + str(index),
                                             "JOB_RC": "0" if mode == "live" else "1"})
            self.assertEqual(result.returncode, 0 if mode == "good" else 2,
                             (mode, result.stdout, result.stderr))
            self.assertTrue(lock.exists())
        result = self.run_shell('lab_lock_match step9-verify-01-ledger-lock "$LAB_LEDGER_LOCK"\n',
                                {**env, "LAB_LEDGER_LOCK": str(lock)})
        self.assertEqual(result.returncode, 2)
        self.assertIn("recovery:ledger_lock_note_mismatch", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_recovery_recheck_detects_an_individually_valid_but_changed_lock_note(self):
        env = {**self.shell_environment(), "STEP8_PREFIX": "step8-01", "STEP9_PREFIX": "step9-01",
               "RECOVERY_CENSUS_PREFIX": "step9-01-before", "C1": "b" * 40, "C1_TREE": "c" * 40,
               "RUN_ID": "run-1", "LAB_LOCK": str(self.root / "runner.lock"),
               "LAB_LEDGER_LOCK": str(self.root / "ledger.lock")}
        self.write("runner.lock", ("reconcile " + CODE).encode())
        self.write("step9-01-runner-lock.stat", b"synthetic stable stat\n")
        self.write("step9-01-runner-lock.txt", ("run run-1 " + CODE).encode())
        self.write("step9-01-before-census.jsonl", b"{}\n")
        self.write("step9-01-before-entries.json", b"{}\n")
        source = '''lab_preflight() { return 0; }
lab_no_writer() { return 0; }
lab_liveness() { print -r -- 'job_pgrep_exit=1 writer_pgrep_exit=1'; }
lab_lock_census() {
  lab_capture "$1-census.jsonl" print -r -- '{}' || return 2
  lab_capture "$1-entries.json" print -r -- '{}'
}
lab_lock_note() {
  lab_capture "$1.stat" print -r -- 'synthetic stable stat' || return 2
  lab_capture "$1.txt" cat "$2"
}
lab_recovery_recheck step9-recheck-71
'''
        result = self.run_shell(source, env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual((self.root / "step9-recheck-71-runner-lock-check.txt.exit").read_text(), "0\n")
        self.assertEqual((self.root / "step9-recheck-71-runner-note.txt.exit").read_text(), "1\n")

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_step7_helper_refuses_placeholder_and_each_wrong_identifier(self):
        env = {**self.shell_environment(), "STEP7_PREFIX": "step7-01", "L3_PREFIX": "l3-01",
               "STEP6_PREFIX": "step6-01", "RUN_ID": "run-1", "C1": "b" * 40, "C1_TREE": "c" * 40,
               "CONTRACT_SHA": SHA, "CERT_KEY": SHA}
        self.write("l3-01-inventory.json", {"input_sha256": SHA})
        self.write("step6-01-certificate-check.json", {"sha256": SHA})
        identifiers = {key: env[key] for key in ("RUN_ID", "C1", "C1_TREE", "CONTRACT_SHA", "CERT_KEY")}
        source = '''set -C -u
source "$LAB_SHELL" || exit 2
lab_checkpoint() { print -r -- checkpoint_saved; }
lab_consent_finish
'''
        cases = [("template", {**identifiers}, "<notice>", "<consent>")]
        cases += [(key, {**identifiers, key: "wrong"}, "sent notice", "received consent") for key in identifiers]
        cases += [("valid", identifiers, "sent notice", "received consent")]
        for index, (label, ids, notice, consent) in enumerate(cases):
            prefix = "step7-" + str(index)
            body = "\n".join(["DATE=2026-10-16", "MESSAGE_REF=synthetic", "INPUT_SHA=" + SHA, "CERT_SHA=" + SHA,
                              "NOTICE_TEXT=" + notice, "CONSENT_TEXT=" + consent,
                              *[key + "=" + value for key, value in ids.items()]])
            self.write(prefix + "-notice-and-consent.md", body.encode())
            result = subprocess.run(["/bin/zsh", "-f"], input=source, env={**env, "STEP7_PREFIX": prefix},
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0 if label == "valid" else 2, (label, result.stdout, result.stderr))
            self.assertEqual("checkpoint_saved" in result.stdout, label == "valid")

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_l2_launch_checks_notice_and_budget_before_job_checkpoint(self):
        env = {**self.shell_environment(), "L2_PREFIX": "l2-01", "C0": "b" * 40, "C0_TREE": "c" * 40,
               "L2_INDEX_SHA": SHA, "L2_SPACE_BUDGET_KIB": "1", "LAB_DATA": str(self.root / "data")}
        self.write("session-l2-01-review.zsh", b"# reviewed\n")
        self.write("data/index/" + SHA + "/markets-v1.jsonl", b"{}\n")
        source = '''set -C -u
source "$LAB_SHELL" || exit 2
lab_checkpoint() { print -r -- checkpoint_saved; }
lab_detach() { print -r -- detached; }
lab_l2_launch
'''
        ids = {key: env[key] for key in ("C0", "C0_TREE", "BASIC_SHA", "L2_INDEX_SHA", "L2_SPACE_BUDGET_KIB")}
        for label in ("missing", "placeholder", "wrong_id", "extended_id", "duplicate_id", "duplicate_budget",
                      "zero_budget", "huge_budget", "valid"):
            path = self.root / "l2-01-notice.md"
            if path.exists():
                path.unlink()
            values = dict(ids)
            budget = "0" if label == "zero_budget" else "999999999999999" if label == "huge_budget" else "1"
            values["L2_SPACE_BUDGET_KIB"] = budget
            if label == "wrong_id":
                values["L2_INDEX_SHA"] = "wrong"
            if label == "extended_id":
                values["L2_INDEX_SHA"] += "x"
            if label != "missing":
                body = ("DATE=2026-10-16\nMESSAGE_REF=" +
                           ("<template>" if label == "placeholder" else "synthetic") + "\n" +
                           "\n".join(key + "=" + value for key, value in values.items()))
                if label in {"duplicate_id", "duplicate_budget"}:
                    body += "\n" + ("C0=wrong" if label == "duplicate_id" else "L2_SPACE_BUDGET_KIB=2")
                self.write(path.name, body.encode())
            before = set(self.root.iterdir())
            result = subprocess.run(["/bin/zsh", "-f"], input=source,
                                    env={**env, "L2_SPACE_BUDGET_KIB": budget},
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0 if label == "valid" else 2, (label, result.stdout, result.stderr))
            self.assertEqual("checkpoint_saved" in result.stdout, label == "valid")
            if label != "valid":
                self.assertEqual(set(self.root.iterdir()), before)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_recovery_requires_verification_before_consuming_recheck_prefix(self):
        env = {**self.shell_environment(), "STEP9_RECHECK_PREFIX": "step9-recheck-01",
               "STEP9_VERIFY_PREFIX": "step9-verify-01", "LAB_LEDGER_LOCK": str(self.root / "ledger.lock")}
        self.write("session-step9-01.zsh", b"# success\n")
        self.write("ledger.lock", b"lock\n")
        before = set(self.root.iterdir())
        result = subprocess.run(["/bin/zsh", "-f"], input='''set -C -u
source "$LAB_SHELL" || exit 2
lab_recovery_review
''', env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("recovery:verify_required", result.stdout)
        self.assertEqual(set(self.root.iterdir()), before)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_writer_discovery_matches_cli_forms_and_excludes_diagnostic_code(self):
        source = '''set -C -u
source "$LAB_SHELL" || exit 2
pgrep() {
  [[ $# == 3 && "$1" = -fl && "$2" = -- ]] || return 99
  "$LAB_PY" -I -S -B -c 'import os,re,sys;found=re.search(sys.argv[1],os.environ["PROC_ARGV"])
if found: print("123 controlled-writer")
sys.exit(0 if found else 1)' "$@[-1]"
}
lab_no_writer "$LABEL"
'''
        values = (("python -S -B - /checkout", 0),
                  ("python -S -B -c from betangel_research.betfair.backtest import certificate", 0),
                  ("python -S -B -m betangel_research.betfair.backtest run-backtest", 2),
                  ("python -S -B -m betangel_research.betfair.readiness --contract /contract", 2),
                  ("python -m betangel_research.betfair.readiness", 2),
                  ("python -m betangel_research.betfair.readiness_suffix", 0))
        for index, (argv, expected) in enumerate(values):
            env = {**self.shell_environment(), "PROC_ARGV": argv, "LABEL": "step8-" + str(index) + "-writers.txt"}
            result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, expected, (argv, result.stdout, result.stderr))

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_concurrent_identity_child_does_not_count_as_cli_writer_and_all_postchecks_run(self):
        # A real pinned-style stdin identity child remains alive during both gates.
        # Process discovery is controlled because sandboxed pgrep may be denied;
        # its regex is evaluated against the actual launched child's argv.
        argv = [sys.executable, "-S", "-B", "-", str(ROOT)]
        child = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            child.stdin.write(lab.IDENTITY_CODE + "\nimport signal\nsys.stdout.flush()\nsignal.pause()\n")
            child.stdin.close()
            identity = child.stdout.readline()
            self.assertIn('"source_descriptor_sha256"', identity)
            self.assertIsNone(child.poll())
            for index, failure in enumerate(("none", "writer", "locks", "inputs", "input_error", "census_error")):
                forced_writer = failure == "writer"
                prefix = "step8-" + str(20 + index)
                state = self.root / prefix
                (state / "certifications").mkdir(parents=True)
                env = {**self.shell_environment(), "STEP8_PREFIX": prefix, "STEP6_PREFIX": "step6-01",
                       "L3_PREFIX": "l3-01", "C1": "b" * 40, "C1_TREE": "c" * 40,
                       "CONTRACT_SHA": SHA, "RUN_ID": "run-1", "LAB_STATE": str(state), "LAB_CONTRACT": "unused",
                       "LAB_DATA": "unused", "PROC_ARGV": " ".join(child.args),
                       "FORCE_WRITER": str(int(forced_writer)), "POST_FAILURE": failure}
                self.write("step1-01-identity.json", b"{}\n")
                source = '''set -C -u
source "$LAB_SHELL" || exit 2
lab_preflight() { print -r -- "$C1 tree $C1_TREE"; }
lab_identity() { print -r -- '{}'; }
failed_inputs() { print -r -- '{}'; return 2; }
lab_inputs() {
  if [[ "$POST_FAILURE" = inputs && "$1" = "$STEP8_PREFIX-after" ]]; then
    lab_capture "$1-files.json" print -r -- '{"changed":true}'
  elif [[ "$POST_FAILURE" = input_error && "$1" = "$STEP8_PREFIX-after" ]]; then
    lab_capture "$1-files.json" failed_inputs
  else lab_capture "$1-files.json" print -r -- '{}'; fi
}
functions[original_no_locks]=$functions[lab_no_locks]
lab_no_locks() { [[ "$POST_FAILURE" != locks ]] || return 2; original_no_locks; }
functions[original_census]=$functions[lab_census]
lab_census() {
  original_census "$@" || return 2
  [[ "$POST_FAILURE" != census_error || "$1" != "$LAB_STATE/contracts" ]] || return 2
}
lab_checkpoint() { print -r -- checkpoint_saved; }
lab_run_binding() { lab_capture "$STEP8_PREFIX-binding.json" print -r -- '{"binding":"checked"}'; }
functions[original_capture]=$functions[lab_capture]
lab_capture() {
  if [[ "$1" = "$STEP8_PREFIX-run.json" ]]; then
    original_capture "$1" print -r -- '{"status":"completed","engineering_acceptance":"passed"}'
  else
    original_capture "$@"
  fi
}
pgrep() {
  [[ $# == 3 && "$1" = -fl && "$2" = -- ]] || return 99
  if [[ "$FORCE_WRITER" = 1 && -f "$LAB_EVIDENCE/$STEP8_PREFIX-run.json" ]]; then
    print -r -- '123 python -m betangel_research.betfair.backtest run-backtest'; return 0
  fi
  "$LAB_PY" -I -S -B -c 'import os,re,sys;sys.exit(0 if re.search(sys.argv[1],os.environ["PROC_ARGV"]) else 1)' "$@[-1]"
}
lab_run_job
'''
                result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env,
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0 if failure == "none" else 2, result.stdout + result.stderr)
                self.assertEqual("checkpoint_saved" in result.stdout, failure == "none")
                for suffix in ("after-files.json", "input-compare.txt", "locks-census.jsonl", "locks-entries.json",
                               "no-locks.txt", "status.txt", "binding.json"):
                    exists = (self.root / (prefix + "-" + suffix)).is_file()
                    self.assertEqual(exists, not (failure == "census_error" and suffix == "locks-entries.json"), suffix)
                if failure in {"input_error", "census_error"}:
                    suffix = "after-files.json.exit" if failure == "input_error" else "locks-census.jsonl.exit"
                    self.assertEqual((self.root / (prefix + "-" + suffix)).read_text(), "2\n")
                self.assertIsNone(child.poll())
        finally:
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()

    def test_every_shell_capture_template_is_registered(self):
        # Read the producer, independently of SUFFIXES. Resolve bounded generator
        # parameters to every branch used by helpers; unknown interpolation fails.
        source = SHELL.read_text() + "\n" + (ROOT / "docs" / "backtest-plan.md").read_text()
        templates = set(re.findall(r'(?m)^\s*lab_capture\s+"([^"\n]+)"', source))
        templates.update(re.findall(r'if lab_capture\s+"([^"\n]+)"', source))
        templates.update(re.findall(r'lab_no_writer\s+"([^"\n]+)"', source))
        prefix_values = {
            "STEP1_PREFIX": "step1-01", "L1_PREFIX": "l1-01", "L2_PREFIX": "l2-01", "L3_PREFIX": "l3-01",
            "STEP5A_PREFIX": "step5a-01", "STEP5B_PREFIX": "step5b-01", "STEP5_FETCH_PREFIX": "step5-fetch-01",
            "STEP6_PREFIX": "step6-01", "STEP7_PREFIX": "step7-01", "STEP8_PREFIX": "step8-01",
            "STEP9_PREFIX": "step9-01", "STEP9_VERIFY_PREFIX": "step9-verify-01",
            "STEP9_RECHECK_PREFIX": "step9-recheck-01", "STEP9_LEDGER_PREFIX": "step9-ledger-01",
            "STEP9_RUNNER_PREFIX": "step9-runner-01", "RETURN_PREFIX": "step5b-return-01",
            "RESTORE_PREFIX": "restore-01",
        }
        tokens = set()
        for match in re.finditer(r'lab_notice "[^"\n]+" ([A-Z0-9_ ]+)', source):
            tokens.update(match[1].split())
        for match in re.finditer(r'for name in ([A-Z0-9_ ]+);', source):
            tokens.update(match[1].split())
        self.assertTrue({"C0", "C1", "CONTRACT_SHA"} <= tokens)
        resolved = set()
        for template in templates:
            if template == "$label":
                # lab_capture is the capture abstraction, publication's local
                # label is tested via its independently constructed bundle names.
                continue
            values = [template]
            for key, value in prefix_values.items():
                values = [v.replace("${" + key + "}", value).replace("$" + key, value) for v in values]
            if "${label}." in template:
                replacements = ("step9-01-runner-lock", "step9-verify-01-ledger-lock")
            elif "-files.json" in template or "${part" in template:
                replacements = ("l2-01-before", "l2-01-after", "l2-01-run-before")
            else:
                replacements = ("step9-01", "step9-recheck-01", "step9-ledger-01-recheck", "step9-runner-01-recheck")
            values = [v.replace("${label}", label).replace("$label", label)
                      for v in values for label in replacements]
            values = [v.replace("$1", "step9-01").replace("$prefix", "step9-01") for v in values]
            if "$location" in template:
                values = [v.replace("$location", location).replace("$name", name) for v in values
                          for location in ("parent", "data") for name in ("certifications", "contracts", "runs")]
            elif "$name" in template:
                values = [v.replace("$name", name) for v in values for name in tokens]
            values = [v.replace("${part//\\//_}", part) for v in values
                      for part in ("", "_manifests", "_index", "_index_" + SHA)]
            for value in values:
                self.assertNotIn("$", value, template)
                resolved.add(value)
        self.assertGreater(len(templates), 90)
        for name in resolved:
            with self.subTest(name=name):
                self.assertIsNotNone(lab.registered_name(name))
                self.assertIsNotNone(lab.registered_name(name + ".stderr"))
                self.assertIsNotNone(lab.registered_name(name + ".exit"))
        direct = set(re.findall(r'"\$(?:LAB|FREEZE)_EVIDENCE/([^"\n]+)"', source))
        for template in direct:
            if template in {"$label", "$label$suffix", "$label.exit", "$label.stderr", "$prefix"}:
                # Generic capture outputs are checked for every actual label above.
                # Bare $prefix is the detached wrapper's output stem, not a file.
                continue
            values = [template]
            mapping = {**prefix_values, "FREEZE_PREFIX": "freeze-c1-01",
                       "RECOVERY_CENSUS_PREFIX": "step9-01"}
            for key, value in mapping.items():
                values = [v.replace("${" + key + "}", value).replace("$" + key, value) for v in values]
            if template == "$label.zsh":
                values = ["session-step9-01.zsh"]
            else:
                values = [v.replace("$label", "step9-recheck-01") for v in values]
            prefix = "step8-01-job" if ".$" in template or ".block." in template or ".launch." in template else "l2-01"
            if "-job." in template:
                prefix = "step8-01"
            elif template.endswith(".pid"):
                prefix = "step8-01-job"
            values = [v.replace("$prefix", prefix) for v in values]
            values = [v.replace("$1", "step8-01-job" if ".block." in template else "step9-01") for v in values]
            values = [v.replace("$phase", phase) for v in values for phase in ("final", "review")]
            if template == "$STEP5A_PREFIX-$suffix.exit":
                suffixes = ("parent-compare.txt", "state-compare.txt")
            elif template == "$prefix-final-liveness.txt$suffix":
                suffixes = ("", ".stderr", ".exit")
            else:
                suffixes = ("launch.stdout", "launch.stderr", "block.stdout", "block.stderr", "block.exit", "pid")
            values = [v.replace("$suffix", suffix) for v in values for suffix in suffixes]
            for value in values:
                self.assertNotIn("$", value, template)
                self.assertIsNotNone(lab.registered_name(value), value)
        # Non-capture producers: checkpoint, pinned tool copies, detached wrapper,
        # operator notices, and publication bundles, including all fixed suffixes.
        extra = ["session-l2-01" + suffix + ".zsh" for suffix in ("", "-pre", "-review", "-job")]
        extra += ["step1-01-lab_runbook" + extension for extension in (".py", ".zsh")]
        extra += ["step8-01-job." + suffix for suffix in (
            "launch.stdout", "launch.stderr", "block.stdout", "block.stderr", "block.exit", "pid")]
        extra += ["step7-01-notice-and-consent.md", "l3-01-notice.md", "step9-01-final-publication.jsonl"]
        for name in extra:
            self.assertIsNotNone(lab.registered_name(name), name)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_zsh_syntax(self):
        result = subprocess.run(["/bin/zsh", "-n", str(SHELL)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_root_continuation_requires_both_successful_precheck_receipts(self):
        env = self.shell_environment()
        state = self.root / "lab-state"
        env.update(STEP5A_PREFIX="step5a-01", LAB_STATE=str(state))
        parent = self.root / "step5a-01-parent-compare.txt.exit"
        comparison = self.root / "step5a-01-state-compare.txt.exit"

        def run(operation):
            return subprocess.run(["/bin/zsh", "-f"], input='''set -C -u
source "$LAB_SHELL" || exit 2
"$OPERATION"
''', env={**env, "OPERATION": operation}, capture_output=True, text=True, check=False)

        for receipts in ((None, None), (b"0\n", None), (b"0\n", b"2\n"), (b"1\n", b"0\n")):
            for path, value in zip((parent, comparison), receipts):
                if path.exists():
                    path.unlink()
                if value is not None:
                    path.write_bytes(value)
            before = sorted(self.root.iterdir())
            for operation in ("lab_root_mkdir", "lab_root_finish"):
                result = run(operation)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("state:precheck_incomplete", result.stdout)
                self.assertEqual(before, sorted(self.root.iterdir()))
                self.assertFalse(state.exists())
        parent.write_bytes(b"0\n")
        comparison.unlink()
        comparison.symlink_to(parent)
        self.assertEqual(run("lab_root_mkdir").returncode, 2)
        self.assertFalse(state.exists())
        comparison.unlink()
        comparison.write_bytes(b"0\n")
        result = run("lab_root_mkdir")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(state.is_dir())
        result = run("lab_root_finish")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "session-step5a-01.zsh").is_file())
        self.assertEqual((self.root / "step5a-01-empty.txt").read_text().strip(), "present_and_empty")

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_publication_only_checkpoint_source_survives_deleted_checkout(self):
        checkpoint, _ = self.make_checkpoint()
        before = sorted(self.root.iterdir())
        normal = self.source_checkpoint(checkpoint, 0)
        self.assertEqual(normal.returncode, 2)
        self.assertIn("restore:cwd", normal.stdout)
        self.assertNotIn("publication_source_ready", normal.stdout)
        publication = self.source_checkpoint(checkpoint, 1)
        self.assertEqual(publication.returncode, 0, publication.stdout + publication.stderr)
        self.assertIn("publication_source_ready", publication.stdout)
        self.assertEqual(before, sorted(self.root.iterdir()))
        # Prove the fallback can actually project, and that it never enters the removed cwd.
        self.write("l2-01-check.txt", b"unchanged\n")
        result = subprocess.run(["/bin/zsh", "-f"], input='''set -C -u
LAB_PUBLICATION_ONLY=1
source "$CHECKPOINT" || exit 2
[[ "$(pwd -P)" = "$EXPECTED_CWD" ]] || exit 2
lab_publication l2-01 final
''', cwd=self.root, env={"PATH": "/usr/bin:/bin", "CHECKPOINT": str(checkpoint),
                           "EXPECTED_CWD": str(self.root)}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"publication_rc":0', result.stdout)
        self.assertIn('"code": "unchanged"', result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_publication_only_checkpoint_keeps_interpreter_and_blob_guards(self):
        checkpoint, executable = self.make_checkpoint()
        executable.unlink()
        before = sorted(self.root.iterdir())
        result = self.source_checkpoint(checkpoint, 1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("runbook:interpreter_unavailable", result.stdout)
        self.assertEqual(before, sorted(self.root.iterdir()))
        executable.symlink_to(sys.executable)
        tool = self.root / "step1-01-lab_runbook.py"
        tool.write_bytes(tool.read_bytes() + b"\n# changed pinned copy\n")
        before = sorted(self.root.iterdir())
        result = self.source_checkpoint(checkpoint, 1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("runbook:tool_hash_mismatch", result.stdout)
        self.assertNotIn("publication_source_ready", result.stdout)
        self.assertEqual(before, sorted(self.root.iterdir()))

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_finished_publication_does_not_burn_label_for_pending_or_bad_receipts(self):
        env = self.shell_environment()
        receipt = self.root / "step8-01-job.block.exit"
        source = '''set -C -u
source "$LAB_SHELL" || exit 2
lab_finished_publication step8-01
'''
        for raw in (None, b"", b"256\n", b"-1\n", b"2\n0\n", b"receipt_secret\n"):
            if raw is not None:
                receipt.write_bytes(raw)
            result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("publication:job_", result.stdout)
            self.assertNotIn("receipt_secret", result.stdout)
            self.assertFalse(any(path.name.startswith("step8-01-final-publication") for path in self.root.iterdir()))
        receipt.unlink()
        target = self.write("private-receipt", b"2\n")
        receipt.symlink_to(target)
        result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(any(path.name.startswith("step8-01-final-publication") for path in self.root.iterdir()))
        receipt.unlink()
        receipt.write_bytes(b"2\n")
        self.write("step8-01-run.json", {"status": "failed", "failure": "receipt_secret"})
        result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("job=step8-01-job block_exit=2", result.stdout)
        self.assertIn('"publication_rc":0', result.stdout)
        self.assertNotIn("receipt_secret", result.stdout)
        self.assertTrue((self.root / "step8-01-final-publication.jsonl").is_file())

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_missing_interpreter_refuses_before_creating_evidence(self):
        env = {"PATH": "/usr/bin:/bin", "LAB_EVIDENCE": str(self.root), "LAB_PY": str(self.root / "gone"),
               "LAB_SHELL": str(SHELL)}
        source = '''set -C -u
source "$LAB_SHELL" || exit 2
lab_capture l2-01-check.txt print -r -- should_never_run
'''
        result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("runbook:interpreter_unavailable", result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_uncertain_bundle_is_printed_on_rc2_and_can_be_reprinted(self):
        tool = self.root / "step1-01-lab_runbook.py"
        shell = self.root / "step1-01-lab_runbook.zsh"
        shutil.copyfile(TOOL, tool)
        shutil.copyfile(SHELL, shell)
        self.write("step9-runner-01-reconcile.json", {"status": "uncertain", "runner_lock": "wrong"})
        self.write("step9-runner-01-status.txt", b"uncertain\n")
        env = {"PATH": "/usr/bin:/bin", "LAB_EVIDENCE": str(self.root), "LAB_PY": sys.executable,
               "LAB_TOOL": str(tool), "LAB_SHELL": str(shell),
               "LAB_TOOL_SHA": hashlib.sha256(tool.read_bytes()).hexdigest(),
               "LAB_SHELL_SHA": hashlib.sha256(shell.read_bytes()).hexdigest(), "BASIC_SHA": SHA,
               "LAB_LOCK": "expected", "STEP1_PREFIX": "step1-01", "STEP9_PREFIX": "step9-01"}
        source = '''set -C -u
source "$LAB_SHELL" || exit 2
lab_publication step9-runner-01 final
[[ "$?" = 2 ]] || exit 3
bundle_sha="$(/usr/bin/shasum -a 256 "$LAB_EVIDENCE/step9-runner-01-final-publication.jsonl")"
lab_reprint step9-runner-01 final "${bundle_sha%% *}"
[[ "$?" = 2 ]] || exit 4
'''
        result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('"publication_rc":2'), 2)
        self.assertEqual(result.stdout.count('"runner_lock_path_matches": false'), 2)
        self.assertNotIn("wrong", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_zsh_capture_poll_and_publication_smoke(self):
        tool = self.root / "step1-01-lab_runbook.py"
        shell = self.root / "step1-01-lab_runbook.zsh"
        shutil.copyfile(TOOL, tool)
        shutil.copyfile(SHELL, shell)
        env = {"PATH": "/usr/bin:/bin", "LAB_EVIDENCE": str(self.root), "LAB_PY": sys.executable,
               "LAB_TOOL": str(tool), "LAB_SHELL": str(shell),
               "LAB_TOOL_SHA": hashlib.sha256(tool.read_bytes()).hexdigest(),
               "LAB_SHELL_SHA": hashlib.sha256(shell.read_bytes()).hexdigest(), "BASIC_SHA": SHA,
               "LAB_LOCK": "expected", "STEP1_PREFIX": "step1-01", "STEP9_PREFIX": "step9-01"}
        source = '''set -C -u
source "$LAB_SHELL" || exit 2
lab_capture l2-01-check.txt print -r -- unchanged || exit 2
lab_publication l2-01 final || exit 2
lab_poll step8-01-job
[[ "$?" = 3 ]] || exit 2
print -r -- 0 > "$LAB_EVIDENCE/step8-01-job.block.exit"
lab_poll step8-01-job || exit 2
lab_capture l2-01-check.txt print -r -- forbidden
[[ "$?" = 2 ]] || exit 2
'''
        result = subprocess.run(["/bin/zsh", "-f"], input=source, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"publication_rc":0', result.stdout)
        self.assertIn("job=step8-01-job pending", result.stdout)
        self.assertIn("job=step8-01-job block_exit=0", result.stdout)
        self.assertNotIn("forbidden", result.stdout)
        self.assertTrue(all(lab.registered_name(path.name) for path in self.root.iterdir()))


class WorkflowGateTests(ToolTest):
    """Drive the real orchestration; stub only external work, with observable call receipts."""

    shell_environment = RegistryAndShellTests.shell_environment
    run_shell = RegistryAndShellTests.run_shell

    RECOVERY_STUBS = '''lab_preflight() { print -r -- "$C1 tree $C1_TREE"; }
lab_identity_check() { return 0; }
lab_no_writer() { [[ "$FAULT" != writer ]]; }
lab_liveness() {
  if [[ "$FAULT" = liveness ]]; then print -r -- 'job_pgrep_exit=1 writer_pgrep_exit=1'; return 2; fi
  if [[ "$FAULT" = writer_live ]]; then print -r -- 'job_pgrep_exit=1 writer_pgrep_exit=0'
  else print -r -- 'job_pgrep_exit=1 writer_pgrep_exit=1'; fi
  print -r -- 'block_stdout=present'
}
lab_checkpoint() { print -r -- "checkpoint_saved=$1"; }
'''

    def recovery_fixture(self, suffix="01", verified=True):
        state = self.root / ("state-" + suffix)
        contract = state / "contracts" / SHA
        contract.mkdir(parents=True)
        runner = contract / "runner.lock"
        ledger = contract / "trial-ledger-v1.jsonl.lock"
        runner.write_text("run run-1 " + CODE)
        note = {"code_identity": CODE, "kind": "started", "trial_id": "run-1.ltp_favourite"}
        ledger.write_text(json.dumps(note, sort_keys=True))
        (contract / "trial-ledger-v1.jsonl").write_bytes(b"garbage-without-a-newline")
        env = {**self.shell_environment(), "STEP8_PREFIX": "step8-" + suffix, "STEP9_PREFIX": "step9-" + suffix,
               "STEP9_VERIFY_PREFIX": "step9-verify-" + suffix, "STEP9_RECHECK_PREFIX": "step9-recheck-" + suffix,
               "STEP9_RUNNER_PREFIX": "step9-runner-" + suffix, "LAB_STATE": str(state), "LAB_CHECKOUT": str(ROOT),
               "LAB_CONTRACT": "unused", "CONTRACT_SHA": SHA, "C1": "b" * 40, "C1_TREE": "c" * 40,
               "RUN_ID": "run-1", "LAB_LOCK": str(runner), "LAB_LEDGER_LOCK": str(ledger), "FAULT": "none",
               "RECOVERY_CENSUS_PREFIX": "step9-" + suffix + "-before"}
        baseline = '''lab_lock_census "$RECOVERY_CENSUS_PREFIX" || exit 2
lab_lock_note "$STEP9_PREFIX-runner-lock" "$LAB_LOCK" || exit 2
'''
        if verified:
            baseline += 'lab_lock_note "$STEP9_VERIFY_PREFIX-ledger-lock" "$LAB_LEDGER_LOCK" || exit 2\n'
            self.write("session-" + env["STEP9_VERIFY_PREFIX"] + ".zsh", b"# verified\n")
        result = self.run_shell(baseline, env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write("session-" + env["STEP9_PREFIX"] + ".zsh", b"# baseline\n")
        return env, runner, ledger, note

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_ledger_recheck_requires_reviewed_note_stat_and_verification(self):
        env, runner, ledger, note = self.recovery_fixture()
        original = ledger.stat()
        for index, mode in enumerate(("good", "unverified", "mismatch", "note_drift", "stat_drift")):
            changed = dict(note)
            if mode == "mismatch":
                changed["trial_id"] = "run-2.ltp_favourite"
            elif mode == "note_drift":
                changed["kind"] = "aborted"  # valid note, same length and timestamp, different reviewed contents
            ledger.write_text(json.dumps(changed, sort_keys=True))
            os.utime(ledger, ns=(original.st_atime_ns,
                                original.st_mtime_ns + (2_000_000_000 if mode == "stat_drift" else 0)))
            prefix = "step9-recheck-" + str(300 + index)
            selected = {**env, "PREFIX": prefix}
            if mode == "unverified":
                selected["STEP9_VERIFY_PREFIX"] = "step9-verify-999"
            result = self.run_shell(self.RECOVERY_STUBS + 'lab_recovery_recheck "$PREFIX"\n', selected)
            self.assertEqual(result.returncode, 0 if mode == "good" else 2, (mode, result.stdout, result.stderr))
            if mode == "unverified":
                self.assertFalse((self.root / (prefix + "-ledger-lock.txt")).exists())
            if mode == "mismatch":
                self.assertIn("recovery:ledger_lock_note_mismatch", result.stdout)
            if mode == "note_drift":
                self.assertEqual((self.root / (prefix + "-ledger-lock-check.txt.exit")).read_text(), "0\n")
                self.assertEqual((self.root / (prefix + "-ledger-note.txt.exit")).read_text(), "1\n")
            if mode == "stat_drift":
                self.assertEqual((self.root / (prefix + "-ledger-stat.txt.exit")).read_text(), "1\n")
            self.assertTrue(runner.exists())
            self.assertTrue(ledger.exists())

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_runner_remove_refuses_a_real_held_ledger_lock_before_rm(self):
        env, runner, ledger, _ = self.recovery_fixture()
        self.write("session-" + env["STEP9_RECHECK_PREFIX"] + ".zsh", b"# reviewed recheck\n")
        result = self.run_shell(self.RECOVERY_STUBS + "lab_runner_remove\n", env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(runner.exists())
        self.assertTrue(ledger.exists())
        self.assertFalse((self.root / (env["STEP9_RUNNER_PREFIX"] + "-rm-runner-lock.txt")).exists())
        self.assertNotIn("checkpoint_saved=" + env["STEP9_RUNNER_PREFIX"] + "\n", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_ledger_verify_child_and_shell_both_refuse_a_real_malformed_ledger(self):
        env, _, ledger, _ = self.recovery_fixture(verified=False)
        path = ledger.with_suffix("")
        child = subprocess.run([sys.executable, "-S", "-B", "-", str(path)], input=lab.LEDGER_CODE,
                               cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(child.returncode, 2, child.stdout + child.stderr)
        self.assertEqual(json.loads(child.stdout)["status"], "refused")
        result = self.run_shell('cd "$LAB_CHECKOUT" || exit 2\n' + self.RECOVERY_STUBS + "lab_ledger_verify\n", env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual((self.root / "step9-verify-01-ledger-verify.json.exit").read_text(), "2\n")
        self.assertNotIn("checkpoint_saved=step9-verify-01\n", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_recovery_begin_projection_lock_and_liveness_gates_have_codes(self):
        for index, (fault, code) in enumerate((("none", None), ("writer", "recovery:writer_present"),
                ("liveness", "recovery:liveness_refused"), ("writer_live", "recovery:job_or_writer_live"),
                ("unreadable_note", "recovery:lock_check_refused"),
                ("same_basename", "recovery:run_projection_refused"),
                ("missing_path", "recovery:run_projection_refused"),
                ("absent", "recovery:runner_lock_absent"), ("checkpoint", "recovery:wrong_checkpoint"))):
            suffix = str(320 + index)
            state = self.root / ("state-" + suffix)
            contract = state / "contracts" / SHA
            contract.mkdir(parents=True)
            runner = contract / "runner.lock"
            if fault != "absent":
                runner.write_bytes(b"\xff" if fault == "unreadable_note" else ("run run-1 " + CODE).encode())
            checkpoint = self.write("session-step8-" + suffix + "-job.zsh", b"# job\n")
            self.write("step8-" + suffix + "-state-check.txt.exit", b"0\n")
            value = {"status": "uncertain"}
            if fault != "missing_path":
                other = self.root / "other" / "runner.lock"
                value["runner_lock"] = str(other if fault == "same_basename" else runner)
            self.write("step8-" + suffix + "-run.json", value)
            env = {**self.shell_environment(), "STEP8_PREFIX": "step8-" + suffix, "STEP9_PREFIX": "step9-" + suffix,
                   "LAB_STATE": str(state), "CONTRACT_SHA": SHA, "C1": "b" * 40, "C1_TREE": "c" * 40,
                   "RUN_ID": "run-1", "RESTORE_FILE": "wrong.zsh" if fault == "checkpoint" else str(checkpoint),
                   "FAULT": fault}
            result = self.run_shell(self.RECOVERY_STUBS + "lab_recovery_begin\n", env)
            self.assertEqual(result.returncode, 0 if code is None else 2, (fault, result.stdout, result.stderr))
            self.assertEqual("checkpoint_saved=step9-" + suffix + "\n" in result.stdout, code is None)
            if code:
                self.assertIn(code, result.stdout)
            if fault == "absent":
                self.assertTrue((self.root / ("step9-" + suffix + "-before-census.jsonl")).exists())
                self.assertTrue((self.root / ("step9-" + suffix + "-before-entries.json")).exists())

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_recheck_writer_and_liveness_failures_are_distinct(self):
        env, _, _, _ = self.recovery_fixture()
        for index, (fault, code) in enumerate((("writer", "recovery:writer_present"),
                ("liveness", "recovery:liveness_refused"), ("writer_live", "recovery:job_or_writer_live"))):
            result = self.run_shell(self.RECOVERY_STUBS + 'lab_recovery_recheck "$PREFIX"\n',
                                    {**env, "FAULT": fault, "PREFIX": "step9-recheck-" + str(350 + index)})
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(code, result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_publication_shell_passes_bootstrap_and_recovery_run_and_review_scope(self):
        env = self.shell_environment()
        bootstrap = self.root / "betangel-runbook-bootstrap-step1-01"
        self.write(bootstrap.name + ".stdout", b"private bootstrap output")
        self.write(bootstrap.name + ".stderr", b"")
        self.write(bootstrap.name + ".exit", b"0\n")
        env.update(LAB_BOOTSTRAP=str(bootstrap), STEP8_PREFIX="step8-01")
        self.write("step8-01-run.json", fixtures()["run"][1])
        for prefix, expected in (("step1-01", bootstrap.name + ".exit"), ("step9-01", "step8-01-run.json")):
            result = self.run_shell('lab_publication "$PREFIX" final\n', {**env, "PREFIX": prefix})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(expected, result.stdout)
            self.assertNotIn("987654321", result.stdout)
        for prefix in ("step6-401", "step8-401"):
            result = self.run_shell('lab_publication "$PREFIX" review\n', {**env, "PREFIX": prefix})
            self.assertEqual(result.returncode, 2)
            self.assertIn("publication:review_prefix_refused", result.stdout)
            self.assertFalse((self.root / (prefix + "-review-publication.jsonl")).exists())

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_poll_empty_invalid_and_finished_nonzero_receipts(self):
        env = self.shell_environment()
        for index, (value, expected) in enumerate(((b"", 3), (b"256\n", 2), (b"3\n", 3), (b"4\n", 4))):
            prefix = "step8-" + str(410 + index)
            self.write(prefix + "-job.block.exit", value)
            result = self.run_shell('lab_poll "$PREFIX-job"\n', {**env, "PREFIX": prefix})
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            if value == b"":
                self.assertIn("receipt not complete", result.stdout)
            elif value in (b"3\n", b"4\n"):
                result = self.run_shell('lab_finished_publication "$PREFIX"\n', {**env, "PREFIX": prefix})
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertIn('"publication_rc":0', result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_final_gate_symlinks_and_observation_failures_do_not_burn_final_labels(self):
        base = self.shell_environment()
        for index, kind in enumerate(("bad_pid", "pgrep_error", "receipt_link", "checkpoint_link")):
            prefix = "step8-" + str(420 + index)
            checkpoint = self.write("session-" + prefix + "-job.zsh", b"# job\n")
            if kind == "bad_pid":
                self.write(prefix + "-job.pid", b"12a\n")
            elif kind == "receipt_link":
                (self.root / (prefix + "-job.block.exit")).symlink_to(self.write("target", b"0\n"))
            elif kind == "checkpoint_link":
                checkpoint.unlink()
                checkpoint.symlink_to(self.root / "absent")
            source = '''pgrep() {
  if (( $# == 3 )); then [[ "$1" = -d, && "$2" = -f ]] || return 99
  else [[ $# == 4 && "$1" = -d, && "$2" = -f && "$3" = -- ]] || return 99; fi
  [[ "$KIND" != pgrep_error ]] || return 3
  print -r -- 123; return 0
}
ps() { return 0; }
lab_publication "$PREFIX" final
'''
            result = self.run_shell(source, {**base, "PREFIX": prefix, "KIND": kind})
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            expected = ("publication:job_checkpoint_refused" if kind == "checkpoint_link" else
                        "publication:job_pending" if kind == "receipt_link" else
                        "publication:liveness_observation_refused")
            self.assertIn(expected, result.stdout)
            self.assertFalse(any(p.name.startswith(prefix + "-final-") for p in self.root.iterdir()))

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_real_liveness_each_refusal_and_full_process_query_argv(self):
        source = '''functions[real_tool]=$functions[lab_tool]
lab_tool() { [[ "$FAULT" != pattern ]] || return 2; real_tool "$@"; }
pgrep() {
  if (( $# == 3 )); then
    [[ "$1" = -d, && "$2" = -f ]] || return 99
    [[ "$FAULT" != job_query ]] || return 3
    [[ "$FAULT" != writer_ids && "$FAULT" != writer_ps ]] || return 1
  else
    [[ $# == 4 && "$1" = -d, && "$2" = -f && "$3" = -- ]] || return 99
    [[ "$FAULT" != writer_query ]] || return 2
    [[ "$FAULT" = writer_ids || "$FAULT" = writer_ps ]] || return 1
  fi
  if [[ "$FAULT" = job_ids || "$FAULT" = writer_ids ]]; then print -r -- malformed
  else print -r -- 123; fi
}
ps() {
  [[ $# == 4 && "$1" = -o && "$2" = pid=,ppid=,stat=,etime=,comm= && "$3" = -p && "$4" = 123 ]] || return 99
  [[ "$FAULT" != job_ps && "$FAULT" != writer_ps ]]
}
lab_liveness "$PREFIX"
'''
        cases = (("prefix", "prefix_refused"), ("pattern", "pattern_refused"), ("job_query", "pgrep_refused"),
                 ("writer_query", "pgrep_refused"), ("job_ids", "job_pids_refused"),
                 ("writer_ids", "writer_pids_refused"), ("job_ps", "ps_refused"), ("writer_ps", "ps_refused"),
                 ("stdout", "stdout_refused"))
        env = self.shell_environment()
        for index, (fault, code) in enumerate(cases):
            prefix = "step9-450-job" if fault == "prefix" else "step8-" + str(450 + index) + "-job"
            if fault == "stdout":
                (self.root / (prefix + ".block.stdout")).symlink_to(self.write("target", b"private"))
            result = self.run_shell(source, {**env, "PREFIX": prefix, "FAULT": fault})
            self.assertEqual(result.returncode, 2, (fault, result.stdout, result.stderr))
            self.assertIn("liveness:" + code, result.stdout)

    @unittest.skipUnless(all(Path(p).exists() for p in ("/bin/zsh", "/usr/bin/nohup", "/usr/bin/caffeinate")),
                         "macOS detach tools are not installed")
    def test_real_detach_hup_trap_reaches_children_and_survives_a_session_hangup(self):
        env = self.shell_environment()
        checkpoint = self.write("session-step8-470-job.zsh", b'''lab_probe_job() {
  "$LAB_PY" -I -S -B -c 'import signal,sys; sys.exit(0 if signal.getsignal(signal.SIGHUP)==signal.SIG_IGN else 5)' \
    || return 5
  print -r -- child_inherits_ignored_hup
  local n=0
  while [[ ! -f "$RELEASE" ]] && (( n < 200 )); do /bin/sleep 0.02; (( n++ )); done
  [[ -f "$RELEASE" ]] || return 6
  print -r -- job_survived_hup
  return 4
}
''')
        release = self.root / "release"
        launcher = subprocess.Popen(["/bin/zsh", "-f"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True, cwd=self.root,
                                    env={**env, "CHECKPOINT": str(checkpoint), "RELEASE": str(release)})
        source = '''set -C -u
source "$LAB_SHELL" || exit 2
lab_probe_job() { return 4; }
functions[/usr/bin/nohup]='
[[ $# -ge 2 && "$1" = /usr/bin/caffeinate && "$2" = -i ]] || return 98
command /usr/bin/nohup "$@"
'
lab_detach step8-470-job "$CHECKPOINT" lab_probe_job
'''
        out, err = launcher.communicate(source, timeout=10)
        self.assertEqual(launcher.returncode, 0, out + err)
        output = self.root / "step8-470-job.block.stdout"
        receipt = self.root / "step8-470-job.block.exit"
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if output.exists() and "child_inherits_ignored_hup" in output.read_text():
                    break
                if receipt.exists() and receipt.read_bytes():
                    break
                time.sleep(0.01)
            self.assertTrue(output.exists(), out + err)
            self.assertIn("child_inherits_ignored_hup", output.read_text())
            os.killpg(launcher.pid, signal.SIGHUP)
            release.write_bytes(b"")
            deadline = time.monotonic() + 5
            while (not receipt.exists() or not receipt.read_bytes()) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(receipt.exists(), out + err)
            self.assertEqual(receipt.read_text(), "4\n")
            self.assertEqual(output.read_text(), "child_inherits_ignored_hup\njob_survived_hup\n")
        finally:
            release.touch(exist_ok=True)
            try:
                os.killpg(launcher.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass  # Darwin killpg may report EPERM when only unreaped zombies remain.

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_identifier_token_rejects_duplicate_conflicting_and_identical_rows(self):
        env = {**self.shell_environment(), "C0": "b" * 40, "C0_TREE": "c" * 40, "L2_INDEX_SHA": SHA,
               "L2_SPACE_BUDGET_KIB": "1", "C1": "b" * 40, "C1_TREE": "c" * 40,
               "RUN_ID": "run-1", "CONTRACT_SHA": SHA, "CERT_KEY": SHA,
               "L3_PREFIX": "l3-01", "STEP6_PREFIX": "step6-01"}
        self.write("l3-01-inventory.json", {"input_sha256": SHA})
        self.write("step6-01-certificate-check.json", {"sha256": SHA})
        notice_keys = ("C0", "C0_TREE", "BASIC_SHA", "L2_INDEX_SHA", "L2_SPACE_BUDGET_KIB")
        consent_keys = ("RUN_ID", "C1", "C1_TREE", "CONTRACT_SHA", "CERT_KEY")
        for operation, keys in (("notice", notice_keys), ("consent", consent_keys)):
            for index, key in enumerate(keys):
                for duplicate in ("wrong", env[key]):
                    number = str(500 + index * 2 + (duplicate != "wrong"))
                    prefix = ("l2-" if operation == "notice" else "step7-") + number
                    rows = ["DATE=2026-10-17", "MESSAGE_REF=synthetic", "NOTICE_TEXT=sent", "CONSENT_TEXT=agreed",
                            "INPUT_SHA=" + SHA, "CERT_SHA=" + SHA, *[name + "=" + env[name] for name in keys],
                            key + "=" + duplicate]
                    suffix = "-notice.md" if operation == "notice" else "-notice-and-consent.md"
                    self.write(prefix + suffix, "\n".join(rows).encode())
                    source = ('lab_notice "$PREFIX" ' + " ".join(keys) if operation == "notice" else
                              'lab_checkpoint() { print -r -- forbidden_checkpoint; }\nlab_consent_finish')
                    result = self.run_shell(source + "\n", {**env, "PREFIX": prefix, "STEP7_PREFIX": prefix})
                    self.assertEqual(result.returncode, 2, (operation, key, result.stdout, result.stderr))
                    self.assertNotIn("forbidden_checkpoint", result.stdout)

    def test_matching_lock_check_bundle_is_successful_and_receipt_fields_stay_null(self):
        self.write("step9-01-runner-lock-check.txt", b"lock_note_matches\n")
        run = self.write("step8-01-run.json", {"status": "uncertain", "runner_lock": "/a/runner.lock"})
        rc, text = self.invoke("publish-attempt", self.root, "step9-01", SHA, "/a/runner.lock", "--recovery-run", run)
        self.assertEqual(rc, 0, text)
        self.assertIn('"code": "lock_note_matches"', text)
        for mode in ("run", "recovery"):
            rc, text = self.publish(mode, {"status": "uncertain", "runner_lock": "/b/runner.lock"},
                                    lock="/a/runner.lock")
            self.assertEqual((rc, json.loads(text)["runner_lock_path_matches"]), (2, False))
            rc, text = self.publish(mode, {"status": "uncertain"}, lock="/a/runner.lock")
            self.assertEqual((rc, json.loads(text)["runner_lock_path_matches"]), (2, None))

    def test_identity_requires_both_cwd_and_source_root_and_runtime_filters_are_closed(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = lab.runtime_check(ROOT, "import os\nos.getcwd=lambda: '/other-checkout'\n" + lab.IDENTITY_CODE,
                                   [str(ROOT)])
        self.assertEqual(rc, 2, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["code"], "identity:not_the_dedicated_checkout")
        for status in (3, 4, 127):
            def command(_):
                raise SystemExit(status)
            with mock.patch.dict(lab.COMMANDS, {"identity": (1, command)}):
                rc, text = self.invoke("identity", ROOT)
            self.assertEqual(rc, 2, text)
            self.assertEqual((rc, json.loads(text)["code"]), (2, "extractor:unexpected"))
        row = "job_pgrep_exit=1 writer_pgrep_exit=1\n"
        rc, text = self.publish("liveness", (row + row + "block_stdout=present\n").encode())
        self.assertEqual(rc, 2, text)
        for value in ("line 67108866:truncated_line", "entry:made_up_code", "ledger:made_up_code"):
            self.assertEqual(lab.ledger_code(value), {"code": "unclassified", "line": None})

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_lock_note_file_type_and_step1_refusal_codes(self):
        env = self.shell_environment()
        lock = self.write("runner.lock", b"run run-1 " + CODE.encode())
        source = '''functions[/usr/bin/stat]='print -r -- "Symbolic Link"'
lab_lock_note step9-01-runner-lock "$LOCK"
'''
        result = self.run_shell(source, {**env, "LOCK": str(lock)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.root / "step9-01-runner-lock.txt").exists())
        result = self.run_shell('lab_lock_note step9-02-runner-lock "$LOCK"\n',
                                {**env, "LOCK": str(self.root / "absent")})
        self.assertEqual(result.returncode, 2)
        self.assertIn("recovery:lock_not_regular", result.stdout)
        result = self.run_shell("unsetopt noclobber\nlab_init\n", env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("step1:settings_refused", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_bootstrap_identity_gate_failure_has_a_fixed_code_and_no_checkpoint(self):
        env = {**self.shell_environment(), "LAB_EVIDENCE": str(self.root / "fresh-evidence"),
               "LAB_BOOTSTRAP": str(self.root / "bootstrap"), "C0": "b" * 40, "C0_TREE": "c" * 40,
               "LAB_CHECKOUT": str(self.root), "LAB_DATA": "unused", "LAB_STATE": "unused"}
        source = '''lab_tool() { return 0; }
lab_census() { return 0; }
source() {
  builtin source "$@" || return 2
  lab_capture() { [[ "$1" != "$STEP1_PREFIX-identity-gate.txt" ]]; }
  lab_checkpoint() { print -r -- forbidden_checkpoint; }
}
lab_bootstrap
'''
        result = self.run_shell(source, env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("step1:identity_gate_refused", result.stdout)
        self.assertNotIn("forbidden_checkpoint", result.stdout)

    @unittest.skipUnless(all(Path(p).exists() for p in ("/bin/zsh", "/usr/bin/nohup", "/usr/bin/caffeinate")),
                         "macOS detach tools are not installed")
    def test_detach_rejects_preexisting_outputs_and_untrusted_checkpoint_locations(self):
        env = self.shell_environment()
        checkpoint = self.write("session-step8-480-job.zsh", b"lab_probe_job() { return 0; }\n")
        source = '''lab_probe_job() { return 0; }
lab_detach "$PREFIX" "$CHECKPOINT" lab_probe_job
'''
        for suffix in ("launch.stdout", "launch.stderr", "block.stdout", "block.stderr", "block.exit", "pid"):
            prefix = "step8-480-job"
            target = self.write(prefix + "." + suffix, b"keep-original")
            before = set(self.root.iterdir())
            result = self.run_shell(source, {**env, "PREFIX": prefix, "CHECKPOINT": str(checkpoint)})
            self.assertEqual(result.returncode, 2, (suffix, result.stdout, result.stderr))
            self.assertEqual(target.read_bytes(), b"keep-original")
            self.assertEqual(set(self.root.iterdir()), before)
            target.unlink()
        elsewhere = self.root / "evidence-subdir"
        elsewhere.mkdir()
        result = self.run_shell(source, {**env, "LAB_EVIDENCE": str(elsewhere), "PREFIX": "step8-480-job",
                                         "CHECKPOINT": str(checkpoint)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])

    @unittest.skipUnless(all(Path(p).exists() for p in ("/bin/zsh", "/usr/bin/nohup", "/usr/bin/caffeinate")),
                         "macOS detach tools are not installed")
    def test_detach_rechecks_checkpoint_type_and_function_in_the_child(self):
        env = self.shell_environment()
        for index, mode in enumerate(("symlink", "missing_function")):
            prefix = "step8-" + str(490 + index) + "-job"
            body = b"lab_probe_job() { print -r -- forbidden; }\n" if mode == "symlink" else b"# no fn\n"
            checkpoint = self.write("session-" + prefix + ".zsh", body)
            target = self.write("same-content", checkpoint.read_bytes())
            source = '''lab_probe_job() { return 0; }
functions[/usr/bin/nohup]='
if [[ "$MODE" = symlink ]]; then
  /bin/rm "$CHECKPOINT" || return 2
  /bin/ln -s "$TARGET" "$CHECKPOINT" || return 2
fi
command /usr/bin/nohup "$@"
'
lab_detach "$PREFIX" "$CHECKPOINT" lab_probe_job
'''
            result = self.run_shell(source, {**env, "PREFIX": prefix, "CHECKPOINT": str(checkpoint),
                                             "TARGET": str(target), "MODE": mode})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = self.root / (prefix + ".block.exit")
            deadline = time.monotonic() + 5
            while (not receipt.exists() or not receipt.read_bytes()) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(receipt.exists())
            self.assertEqual(receipt.read_text(), "2\n")
            self.assertNotIn("forbidden", (self.root / (prefix + ".block.stdout")).read_text())

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_runner_remove_keeps_reconcile_failure_terminal(self):
        env, runner, ledger, _ = self.recovery_fixture()
        ledger.unlink()
        self.write("session-step9-recheck-01.zsh", b"# reviewed\n")
        source = self.RECOVERY_STUBS + '''lab_recovery_recheck() { return 0; }
functions[real_capture]=$functions[lab_capture]
failed_reconcile() { print -r -- '{"status":"reconciled"}'; return 2; }
lab_capture() {
  if [[ "$1" = "$STEP9_RUNNER_PREFIX-reconcile.json" ]]; then real_capture "$1" failed_reconcile
  else real_capture "$@"; fi
}
lab_runner_remove
'''
        result = self.run_shell(source, env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(runner.exists())
        self.assertNotIn("checkpoint_saved=step9-runner-01\n", result.stdout)
        self.assertEqual((self.root / "step9-runner-01-reconcile.json.exit").read_text(), "2\n")

    def test_recovery_attempt_named_refusals_and_regular_file_guards(self):
        checkpoint = self.write("session-step8-01-job.zsh", b"# job\n")
        state = self.write("step8-01-state-check.txt.exit", b"0\n")
        run = self.write("step8-01-run.json", b"")
        self.assertEqual(self.invoke("recovery-attempt", self.root, "step8-01"), (0, "recovery_attempt_current\n"))
        rc, text = self.invoke("recovery-attempt", self.root, "step8-x")
        self.assertEqual((rc, json.loads(text)["code"]), (2, "recovery:attempt_prefix"))
        for path, code in ((checkpoint, "recovery:checkpoint_not_regular"),
                           (state, "recovery:state_check_not_regular"), (run, "recovery:no_run_json")):
            original = path.read_bytes()
            for replacement in ("missing", "directory", "symlink"):
                path.unlink()
                if replacement == "directory":
                    path.mkdir()
                elif replacement == "symlink":
                    path.symlink_to(self.write("target", original))
                rc, text = self.invoke("recovery-attempt", self.root, "step8-01")
                self.assertEqual(rc, 2, text)
                self.assertEqual((rc, json.loads(text)["code"]), (2, code), replacement)
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                elif path.is_symlink():
                    path.unlink()
                path.write_bytes(original)
        for value in (b"2\n", b"", b"00\n"):
            state.write_bytes(value)
            rc, text = self.invoke("recovery-attempt", self.root, "step8-01")
            self.assertEqual(rc, 2, text)
            self.assertEqual((rc, json.loads(text)["code"]), (2, "recovery:state_check_not_zero"))

    def test_publication_names_exit_tokens_liveness_and_symlink_directory_are_bound(self):
        path = self.write("step8-01-run.json", fixtures()["run"][1])
        self.assertEqual(self.invoke("publish", "run", path, "", path.name)[0], 0)
        rc, text = self.invoke("publish", "run", path, "", "step8-02-run.json")
        self.assertEqual(rc, 2, text)
        for name in ("step8-01-run.json.exit", "l2-01-index.json.exit", "restore-01-liveness.txt.exit"):
            self.assertEqual(lab.registered_name(name), "exit")
            rc, text = self.publish("exit", b"2\n", name)
            self.assertEqual((rc, json.loads(text)["exit"]), (0, 2))
        for raw in (b"-2", b"2 private suffix", b"x4", b"2222", b"00"):
            self.assertEqual(self.publish("exit", raw)[0], 2, raw)
        raw = b"job_pgrep_exit=1 writer_pgrep_exit=1\nblock_stdout=present\nmarket_1.987654321\n"
        rc, text = self.publish("liveness", raw)
        self.assertEqual(rc, 0, text)
        value = json.loads(text)
        self.assertEqual(set(value), {"job_pgrep_exit", "writer_pgrep_exit", "block_stdout", "raw_report", "stderr"})
        self.assertNotIn("1.987654321", text)
        self.assertEqual(value["stderr"]["lab_local_file"], "restore-01-liveness.txt.stderr")
        directory = self.root / "contract"
        directory.mkdir()
        (directory / "runner.lock").write_bytes(b"run run-1")
        alias = self.root / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        self.assertEqual(self.invoke("lock-entries", directory)[0], 0)
        rc, text = self.invoke("lock-entries", alias)
        self.assertEqual(rc, 2, text)

    def test_job_pattern_is_literal_and_requires_checkpoint_argument_boundary(self):
        checkpoint = str(self.root / "space [x].zsh")
        rc, pattern = self.invoke("job-pattern", checkpoint)
        self.assertEqual(rc, 0)
        self.assertIsNotNone(re.search(pattern.strip("\n"), "zsh runbook-job " + checkpoint + " hash fn output"))
        for changed in (checkpoint + ".old", checkpoint.replace("[x]", "x"), "other " + checkpoint):
            self.assertIsNone(re.search(pattern.strip("\n"), "zsh runbook-job " + changed + " hash fn output"))

    def test_run_receipt_omits_outcome_hashes_and_issuance_counts(self):
        for mode in ("run", "recovery"):
            _, payload = fixtures()[mode]
            rc, text = self.publish(mode, payload)
            self.assertEqual(rc, 0, text)
            receipt = json.loads(text)["receipt"]
            self.assertEqual(set(receipt), {"status", "failure_code", "ledger_head", "contract_sha256",
                                           "certificate_sha256", "acceptance_status", "trial_heads"})
            for secret in ("d" * 64, "e" * 64, "987654321", "results_sha256", "issuance_ledger", '"issued"'):
                self.assertNotIn(secret, text)
                self.assertNotIn(hashlib.sha256(secret.encode()).hexdigest(), text)
        run = self.write("step8-01-run.json", fixtures()["run"][1])
        rc, text = self.invoke("publish-attempt", self.root, "step9-01", SHA, "", "--recovery-run", run)
        self.assertEqual(rc, 0, text)
        for secret in ("d" * 64, "e" * 64, "987654321", "results_sha256", "issuance_ledger", '"issued"'):
            self.assertNotIn(secret, text)

    @unittest.skipUnless(Path("/usr/bin/pgrep").exists(), "native pgrep is not installed")
    def test_native_pgrep_finds_only_the_owned_sentinel_without_a_quiet_machine_assumption(self):
        marker = "runbook-owned-" + os.urandom(12).hex()
        child = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c",
                                  "import signal; signal.pause()", marker], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            result = subprocess.run(["/usr/bin/pgrep", "-f", marker], capture_output=True, text=True, check=False)
            if result.returncode not in (0, 1):
                self.skipTest("native pgrep unavailable in this sandbox (exit " + str(result.returncode) + ")")
            self.assertIsNone(child.poll())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(child.pid), result.stdout.splitlines())
        finally:
            child.terminate()
            child.wait(timeout=5)

    RUN_STUBS = '''lab_preflight() { [[ "$FAULT" != git ]] || return 2; print -r -- "$C1 tree $C1_TREE"; }
lab_identity() {
  if [[ "$FAULT" = identity ]]; then print -r -- '{}'; return 2; fi
  if [[ "$FAULT" = identity_compare ]]; then print -r -- '{"changed":true}'; else print -r -- '{}'; fi
}
lab_no_writer() {
  [[ "$FAULT" != after_writer || "$1" != "$STEP8_PREFIX-after-writers.txt" ]] || return 2
  [[ "$FAULT" != writer || "$1" != "$STEP8_PREFIX-writers.txt" ]]
}
lab_inputs() {
  lab_capture "$1-files.json" print -r -- '{}' || return 2
  [[ "$FAULT" != inputs || "$1" != "$STEP8_PREFIX-before" ]]
}
functions[real_census]=$functions[lab_census]
lab_census() { [[ "$FAULT" != census ]] || return 2; real_census "$@"; }
lab_checkpoint() { print -r -- "checkpoint_saved=$1"; }
lab_run_binding() { print -r -- "binding_rc=$1"; [[ "$FAULT" != binding ]]; }
runner_fixture() {
  print -r -- called > "$LAB_EVIDENCE/$STEP8_PREFIX-runner-called"
  printf '{"status":"%s","engineering_acceptance":"%s"}\\n' "$RUN_STATUS" "$ACCEPTANCE"
  return "$RUN_RC"
}
functions[real_capture]=$functions[lab_capture]
lab_capture() {
  if [[ "$1" = "$STEP8_PREFIX-run.json" ]]; then real_capture "$1" runner_fixture
  elif [[ "$1" = "$STEP8_PREFIX-status.txt" && "$FAULT" = classifier ]]; then
    real_capture "$@" || return 2
    return 2
  else real_capture "$@"; fi
}
lab_run_job
'''

    def run_job_case(self, index, fault="none", status="completed", rc=0):
        prefix = "step8-" + str(200 + index)
        state = self.root / prefix
        (state / "certifications").mkdir(parents=True)
        if fault == "state":
            (state / "unexpected").write_bytes(b"")
        self.write("step1-01-identity.json", b"{}\n")
        env = {**self.shell_environment(), "STEP8_PREFIX": prefix, "STEP6_PREFIX": "step6-01",
               "L3_PREFIX": "l3-01", "C1": "b" * 40, "C1_TREE": "c" * 40,
               "CONTRACT_SHA": "bad" if fault == "contract" else SHA,
               "RUN_ID": "bad/id" if fault == "run_id" else "run-1", "LAB_STATE": str(state),
               "LAB_CONTRACT": "unused", "LAB_DATA": "unused", "FAULT": fault, "RUN_STATUS": status,
               "RUN_RC": str(rc), "ACCEPTANCE": "failed" if rc == 4 else "passed"}
        return self.run_shell(self.RUN_STUBS, env), prefix

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_run_job_each_prerun_gate_prevents_the_runner_and_its_output(self):
        for index, fault in enumerate(("git", "contract", "run_id", "identity", "identity_compare", "writer",
                                       "inputs", "census", "state")):
            result, prefix = self.run_job_case(index, fault)
            self.assertEqual(result.returncode, 2, (fault, result.stdout, result.stderr))
            self.assertFalse((self.root / (prefix + "-runner-called")).exists(), fault)
            self.assertFalse((self.root / (prefix + "-run.json")).exists(), fault)
            self.assertNotIn("checkpoint_saved", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_run_job_status_binding_and_cli_exit_mapping(self):
        cases = (("none", "completed", 0, 0), ("none", "completed", 4, 4), ("none", "completed", 2, 2),
                 ("binding", "completed", 0, 2), ("binding", "completed", 4, 2),
                 ("none", "failed", 2, 2), ("none", "failed", 0, 2),
                 ("none", "refused", 2, 2), ("none", "refused", 0, 2),
                 ("none", "uncertain", 2, 2), ("none", "unknown", 0, 2), ("classifier", "completed", 0, 2),
                 ("after_writer", "completed", 0, 2))
        for index, (fault, status, cli_rc, expected) in enumerate(cases):
            result, prefix = self.run_job_case(index, fault, status, cli_rc)
            self.assertEqual(result.returncode, expected, (fault, status, cli_rc, result.stdout, result.stderr))
            self.assertTrue((self.root / (prefix + "-runner-called")).exists())
            self.assertEqual("checkpoint_saved=" + prefix + "\n" in result.stdout, expected == 0)
            self.assertEqual("binding_rc=" in result.stdout,
                             status == "completed" and cli_rc in (0, 4) and fault != "classifier")
            if status in {"uncertain", "unknown"}:
                self.assertIn("run:status_unresolved", result.stdout)
            if fault == "after_writer":
                self.assertIn("run:postcheck_refused", result.stdout)
                self.assertNotIn("run:status_unresolved", result.stdout)
            for suffix in ("after-files.json", "input-compare.txt", "locks-census.jsonl", "no-locks.txt", "status.txt"):
                self.assertTrue((self.root / (prefix + "-" + suffix)).exists(), suffix)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_l2_job_budget_and_result_gates_and_cert_job_empty_state_gate(self):
        source = '''lab_notice() { [[ "$FAULT" != notice ]]; }
lab_preflight() { [[ "$FAULT" != git ]]; }
lab_identity_check() { [[ "$FAULT" != identity ]]; }
lab_no_writer() { [[ "$FAULT" != writer ]]; }
lab_inputs() {
  if [[ "$FAULT" = compare ]]; then lab_capture "$1-files.json" print -r -- '{"changed":true}'
  else lab_capture "$1-files.json" print -r -- '{}'; fi
}
lab_checkpoint() { print -r -- "checkpoint_saved=$1"; }
functions[real_capture]=$functions[lab_capture]
fake_index() {
  print -r -- called > "$LAB_EVIDENCE/$L2_PREFIX-index-called"
  if [[ "$FAULT" = index ]]; then print -r -- '{"status":"refused","archives":[]}'
  else printf '{"status":"indexed","archives":[{"state":"unchanged","archive_sha256":"%s"}]}\\n' "$BASIC_SHA"
  fi
}
lab_capture() {
  if [[ "$1" = "$L2_PREFIX-index.json" ]]; then real_capture "$1" fake_index
  elif [[ "$1" = "$STEP6_PREFIX-certify.json" ]]; then
    real_capture "$1" print -r -- certifier_called
  else real_capture "$@"; fi
}
functions[real_tool]=$functions[lab_tool]
lab_tool() {
  if [[ "$1" = extract-digest ]]; then print -r -- "$BASIC_SHA"; else real_tool "$@"; fi
}
"$OPERATION"
'''
        base = self.shell_environment()
        data = self.root / "data"
        self.write("data/index/" + SHA + "/markets-v1.jsonl", b"{}\n")
        for index, fault in enumerate(("none", "space", "index", "notice", "git", "identity", "writer", "compare")):
            prefix = "l2-" + str(210 + index)
            self.write(prefix + "-before-files.json", b"{}\n")
            env = {**base, "L2_PREFIX": prefix, "STEP6_PREFIX": "step6-01", "C0": "b" * 40,
                   "C0_TREE": "c" * 40, "LAB_DATA": str(data), "FAULT": fault, "OPERATION": "lab_l2_job",
                   "L2_SPACE_BUDGET_KIB": "999999999999999" if fault == "space" else "1"}
            result = self.run_shell(source, env)
            self.assertEqual(result.returncode, 0 if fault == "none" else 2, (fault, result.stdout, result.stderr))
            self.assertEqual((self.root / (prefix + "-index-called")).exists(), fault in {"none", "index"})
            self.assertEqual("checkpoint_saved=" + prefix + "\n" in result.stdout, fault == "none")
        for index, fault in enumerate(("none", "occupied", "git", "identity", "writer")):
            occupied = fault == "occupied"
            prefix = "step6-" + str(220 + index)
            state = self.root / prefix
            state.mkdir()
            if occupied:
                (state / "certifications").mkdir()
            env = {**base, "STEP6_PREFIX": prefix, "L2_PREFIX": "l2-01", "C1": "b" * 40,
                   "C1_TREE": "c" * 40, "LAB_STATE": str(state), "ATTEMPT_ID": "attempt-1",
                   "FAULT": fault, "OPERATION": "lab_cert_job"}
            result = self.run_shell(source, env)
            self.assertEqual(result.returncode, 0 if fault == "none" else 2, result.stdout + result.stderr)
            self.assertEqual((self.root / (prefix + "-certify.json")).exists(), fault == "none")
            self.assertNotIn("checkpoint_saved", result.stdout)


class FollowupGateTests(ToolTest):
    shell_environment = RegistryAndShellTests.shell_environment
    run_shell = RegistryAndShellTests.run_shell
    recovery_fixture = WorkflowGateTests.recovery_fixture
    RECOVERY_STUBS = WorkflowGateTests.RECOVERY_STUBS

    def freeze_fence(self):
        doc = (ROOT / "docs" / "backtest-plan.md").read_text()
        fences = [fence for fence in re.findall(r"```sh\n(.*?)\n```", doc, re.S)
                  if 'FREEZE_CHECKOUT=' in fence and 'author-suite' in fence]
        self.assertEqual(len(fences), 1)
        return fences[0]

    def test_freeze_fence_keeps_captures_and_gates_around_the_suite(self):
        fence = self.freeze_fence()
        suite = fence.index('"$FREEZE_PY" -E -s -S -B -m unittest discover -s tests -v')
        receipt = fence.index('printf \'%s\\n\' "$suite_rc" > "$FREEZE_EVIDENCE/$FREEZE_PREFIX-suite.exit" || exit 2')
        publication = fence.index('"$FREEZE_TOOL" author-suite ')
        self.assertLess(suite, receipt)
        before_git = ('git rev-parse HEAD \'HEAD^{tree}\' > '
                      '"$FREEZE_EVIDENCE/$FREEZE_PREFIX-git.txt" || exit 2')
        self.assertEqual(fence.count(before_git), 1)
        self.assertLess(fence.index('"$(git rev-parse --verify HEAD)" = "$FREEZE_C0"'), fence.index(before_git))
        self.assertLess(fence.index(before_git), suite)
        for stage in ("before", "after"):
            captures = (
                'git --no-optional-locks -c core.fsmonitor=false -c core.untrackedCache=false \\\n'
                '  -c core.trustctime=true -c core.checkStat=default -c core.ignorecase=false \\\n'
                '  status --porcelain=v1 -z --untracked-files=all --ignored \\\n'
                '  > "$FREEZE_EVIDENCE/$FREEZE_PREFIX-status-' + stage + '.z" \\\n'
                '  2> "$FREEZE_EVIDENCE/$FREEZE_PREFIX-status-' + stage + '.z.stderr"',
                'git -c core.fsmonitor=false ls-files -v -z > "$FREEZE_EVIDENCE/$FREEZE_PREFIX-index-'
                + stage + '.z" \\\n  2> "$FREEZE_EVIDENCE/$FREEZE_PREFIX-index-' + stage + '.z.stderr"',
                '/usr/bin/find "$FREEZE_CHECKOUT" -name __pycache__ -print \\\n'
                '  > "$FREEZE_EVIDENCE/$FREEZE_PREFIX-cache-' + stage + '.txt" \\\n'
                '  2> "$FREEZE_EVIDENCE/$FREEZE_PREFIX-cache-' + stage + '.txt.stderr"',
            )
            state = ('"$FREEZE_PY" -I -S -B "$FREEZE_TOOL" author-state "$FREEZE_LABEL" \\\n'
                     '  "$FREEZE_EVIDENCE/$FREEZE_PREFIX-status-' + stage + '.z" '
                     '"$FREEZE_EVIDENCE/$FREEZE_PREFIX-cache-' + stage + '.txt" \\\n'
                     '  "$FREEZE_EVIDENCE/$FREEZE_PREFIX-index-' + stage + '.z" || exit 2')
            self.assertEqual(fence.count(state), 1)
            state_at = fence.index(state)
            for capture in captures:
                capture += (' || {\n    print -r -- \'{"status":"refused","code":"author:census_failed"}\'; '
                            'exit 2\n  }')
                self.assertEqual(fence.count(capture), 1)
                capture_at = fence.index(capture)
                self.assertLess(capture_at, state_at)
                if stage == "before":
                    self.assertLess(state_at, suite)
                else:
                    self.assertLess(receipt, capture_at)
                    self.assertLess(state_at, publication)
            for kind, extension in (("status", ".z"), ("index", ".z"), ("cache", ".txt")):
                target = '"$FREEZE_EVIDENCE/$FREEZE_PREFIX-' + kind + '-' + stage + extension + '"'
                self.assertEqual(len(re.findall(r'>&?[|!]?\s*' + re.escape(target), fence)), 1)
                self.assertEqual(fence.count(target), 2)
        self.assertNotRegex(fence, r'>&?[|!]')
        after_git = ('git rev-parse HEAD \'HEAD^{tree}\' > '
                     '"$FREEZE_EVIDENCE/$FREEZE_PREFIX-git-after.txt" || exit 2')
        self.assertEqual(fence.count(after_git), 1)
        self.assertLess(receipt, fence.index(after_git))
        self.assertLess(fence.index(after_git), publication)
        for suffix in ("-git.txt", "-git-after.txt"):
            self.assertEqual(fence.count('$FREEZE_EVIDENCE/$FREEZE_PREFIX' + suffix), 1)
        suite_gate = '(( suite_rc == 0 )) || exit 2'
        self.assertEqual(fence.count(suite_gate), 1)
        self.assertLess(publication, fence.index(suite_gate))
        self.assertLess(fence.index(suite_gate), fence.index('typeset -p FREEZE_PREFIX'))
        run_block = ('BETANGEL_BETFAIR_FORBID_NETWORK=1 PYTHONDONTWRITEBYTECODE=1 '
                     '"$FREEZE_PY" -E -s -S -B -m unittest discover -s tests -v \\\n'
                     '  > "$FREEZE_EVIDENCE/$FREEZE_PREFIX-suite.log" 2>&1\n'
                     'suite_rc=$?\n'
                     'printf \'%s\\n\' "$suite_rc" > "$FREEZE_EVIDENCE/$FREEZE_PREFIX-suite.exit" || exit 2\n')
        self.assertEqual(fence.count(run_block), 1)  # F3-MUT-1: no fabricated or overwritten suite rc.
        self.assertEqual(fence.count('suite_rc='), 1)
        finish = ('"$FREEZE_PY" -I -S -B "$FREEZE_TOOL" author-suite '
                  '"$FREEZE_EVIDENCE" "$FREEZE_PREFIX" "$FREEZE_LABEL" || exit 2\n'
                  + suite_gate + '\n'
                  'typeset -p FREEZE_PREFIX FREEZE_LABEL FREEZE_CHECKOUT FREEZE_PY FREEZE_EVIDENCE FREEZE_C0 \\\n'
                  '  > "$FREEZE_EVIDENCE/session-$FREEZE_PREFIX.zsh" || exit 2\n)')
        self.assertEqual(fence.count(finish), 1)  # Gate cannot be wrapped in if false.

    def test_freeze_fence_gates_c0_head_and_flip_transition_before_the_suite(self):
        fence = self.freeze_fence()
        suite = fence.index('"$FREEZE_PY" -E -s -S -B -m unittest discover -s tests -v')
        self.assertTrue(fence.startswith('(\nset -C -u\nexport GIT_NO_REPLACE_OBJECTS=1\nFREEZE_CHECKOUT='))
        self.assertEqual(fence.count('export GIT_NO_REPLACE_OBJECTS=1'), 1)
        location = ('cd "$FREEZE_CHECKOUT" || exit 2\n'
                    '[[ "$(pwd -P)" = "$FREEZE_CHECKOUT" ]] || exit 2\n'
                    '[[ "$(git rev-parse --show-toplevel)" = "$FREEZE_CHECKOUT" ]] || exit 2\n'
                    '[[ -z "${GIT_DIR-}${GIT_WORK_TREE-}${GIT_INDEX_FILE-}${GIT_COMMON_DIR-}\\\n'
                    '${GIT_OBJECT_DIRECTORY-}${GIT_ALTERNATE_OBJECT_DIRECTORIES-}" ]] || exit 2\n')
        head_gate = ('if [[ "$FREEZE_LABEL" != c1 ]]; then\n'
                     '  [[ "$(git rev-parse --verify HEAD)" = "$FREEZE_C0" ]] || {\n'
                     '    print -r -- \'{"status":"refused","code":"author:c0_head_mismatch"}\'; exit 2\n'
                     '  }\nfi')
        self.assertEqual(fence.count(head_gate), 1)
        adjacent_head = location + head_gate + '\nFREEZE_TOOL="$FREEZE_CHECKOUT/scripts/lab_runbook.py"'
        self.assertEqual(fence.count(adjacent_head), 1)
        head = ('(\nset -C -u\nexport GIT_NO_REPLACE_OBJECTS=1\n'
                "FREEZE_CHECKOUT='<absolute 為今次 label 新建嘅 clone>'\n"
                "FREEZE_PY='<absolute Python 3.15 executable>'\n"
                "FREEZE_EVIDENCE='<已存在、喺 checkout/state 以外嘅 absolute evidence directory>'\n"
                "FREEZE_C0='<reviewed C0 完整 40 hex>'\n"
                "FREEZE_LABEL='<c0-draft 或 c0-flip-only 或 c1>'\n"
                "FREEZE_PREFIX='<全新 freeze-c0-draft-NN／freeze-c0-flip-only-NN／freeze-c1-NN>'\n"
                '[[ "$FREEZE_CHECKOUT" = /* && "$FREEZE_PY" = /* && "$FREEZE_EVIDENCE" = /* ]] || exit 2\n'
                '[[ "$FREEZE_C0" =~ \'^[0-9a-f]{40}$\' ]] || exit 2\n'
                '[[ "$FREEZE_LABEL" = c0-draft || "$FREEZE_LABEL" = c0-flip-only '
                '|| "$FREEZE_LABEL" = c1 ]] || exit 2\n'
                '[[ "$FREEZE_PREFIX" =~ "^freeze-$FREEZE_LABEL-[0-9]+$" ]] || exit 2\n'
                + adjacent_head + '\n'
                '[[ "$(git hash-object --no-filters "$FREEZE_TOOL")" = \\\n'
                '   "$(git rev-parse "${FREEZE_C0}:scripts/lab_runbook.py")" ]] || exit 2\n'
                '"$FREEZE_PY" -I -S -B "$FREEZE_TOOL" author-interpreter '
                '> "$FREEZE_EVIDENCE/$FREEZE_PREFIX-interpreter.json" || exit 2\n')
        self.assertRegex(fence, r'\A' + re.escape(head))  # F3-MUT-3: no wrapper/pushd before the gates.
        self.assertEqual(fence.count('[[ "$(git rev-parse --show-toplevel)" = "$FREEZE_CHECKOUT" ]] || exit 2'), 1)
        self.assertLess(fence.index(head_gate), suite)
        flip_gate = ('if [[ "$FREEZE_LABEL" = c0-flip-only ]]; then\n'
                     '  git show HEAD:research/experiments/backtest-p0-bsp-v1.json \\\n'
                     '    > "$FREEZE_EVIDENCE/$FREEZE_PREFIX-contract-head.json" || exit 2\n'
                     '  "$FREEZE_PY" -I -S -B "$FREEZE_TOOL" freeze-diff '
                     '"$FREEZE_EVIDENCE/$FREEZE_PREFIX-contract-head.json" \\\n'
                     '    "$FREEZE_CHECKOUT/research/experiments/backtest-p0-bsp-v1.json" || exit 2\nfi')
        self.assertEqual(fence.count(flip_gate), 1)
        self.assertEqual(fence.count('"$FREEZE_EVIDENCE/$FREEZE_PREFIX-index-before.z" || exit 2\n'
                                     '"$FREEZE_PY" -I -S -B "$FREEZE_TOOL" author-bytes "$FREEZE_LABEL" || exit 2\n'
                                     + flip_gate + '\nif [[ "$FREEZE_LABEL" = c1 ]]; then'), 1)
        self.assertLess(fence.index(flip_gate), suite)

    def author_receipts(self, prefix, label):
        self.write(prefix + "-suite.log", b"Ran 1 test in 0.001s\nOK\n")
        self.write(prefix + "-suite.exit", b"0\n")
        self.write(prefix + "-git.txt", ("a" * 40 + "\n" + "b" * 40 + "\n").encode())
        self.write(prefix + "-diff.txt", b"research/experiments/backtest-p0-bsp-v1.json\n")
        self.author_states(prefix, label)

    def test_author_state_rejects_dirty_staged_untracked_ignored_and_cache_paths_for_every_label(self):
        contract = "research/experiments/backtest-p0-bsp-v1.json"
        for index, label in enumerate(("c0-draft", "c0-flip-only", "c1")):
            checkout = self.root / label
            checkout.mkdir()
            def git(*args):
                return subprocess.run(["git", "--no-optional-locks", "-C", str(checkout), *args],
                                      capture_output=True, check=True).stdout
            git("init", "-q")
            (checkout / contract).parent.mkdir(parents=True)
            (checkout / contract).write_bytes(b"draft\n")
            (checkout / "tracked.txt").write_bytes(b"tracked\n")
            (checkout / ".gitignore").write_bytes(b"ignored\n__pycache__/\n")
            git("add", ".")
            git("-c", "user.name=Runbook test", "-c", "user.email=runbook@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic baseline")
            for fault in ("none", "unstaged", "staged", "staged_contract", "untracked", "ignored", "cache",
                          "assume_unchanged", "skip_worktree", "assume_unchanged_first", "skip_worktree_first"):
                with self.subTest(label=label, fault=fault):
                    git("update-index", "--no-assume-unchanged", ".gitignore", "tracked.txt")
                    git("update-index", "--no-skip-worktree", ".gitignore", "tracked.txt")
                    git("restore", "--source=HEAD", "--staged", "--worktree", ".")
                    for filename in ("untracked.py", "ignored"):
                        (checkout / filename).unlink(missing_ok=True)
                    cache = checkout / "__pycache__"
                    if cache.exists():
                        cache.rmdir()
                    if label == "c0-flip-only":
                        (checkout / contract).write_bytes(b"frozen\n")
                    if fault in {"unstaged", "staged"}:
                        (checkout / "tracked.txt").write_bytes(b"unreviewed fix\n")
                        if fault == "staged":
                            git("add", "tracked.txt")
                    elif fault == "staged_contract":
                        (checkout / contract).write_bytes(b"frozen\n")
                        git("add", contract)
                    elif fault in {"untracked", "ignored"}:
                        (checkout / ("untracked.py" if fault == "untracked" else fault)).write_bytes(b"fix\n")
                    elif fault == "cache":
                        cache.mkdir()
                    elif fault.startswith(("assume_unchanged", "skip_worktree")):
                        flagged = ".gitignore" if fault.endswith("_first") else "tracked.txt"
                        flag = fault.removesuffix("_first").replace("_", "-")
                        git("update-index", "--" + flag, flagged)
                        (checkout / flagged).write_bytes(b"hidden unreviewed fix\n")
                    status = self.write("status.z", git("status", "--porcelain=v1", "-z",
                                                       "--untracked-files=all", "--ignored"))
                    caches = self.write("cache.txt", b"__pycache__\n" if cache.exists() else b"")
                    index_path = self.write("index.z", git("-c", "core.fsmonitor=false", "ls-files", "-v", "-z"))
                    for path in (status, caches, index_path):
                        self.write(path.name + ".stderr", b"")
                    rc, text = self.invoke("author-state", label, status, caches, index_path)
                    self.assertEqual(rc, 0 if fault == "none" else 2, text)
                    value = json.loads(text)
                    if fault == "none":
                        self.assertEqual(value, {"status": "accepted", "worktree_state":
                                                 "contract_modified" if label == "c0-flip-only" else "clean"})
                    else:
                        self.assertEqual(value["code"], "author:cache_present" if fault == "cache"
                                         else "author:worktree_state")
                    self.assertNotIn("unreviewed", text)
                    self.assertNotIn("untracked.py", text)

    def test_author_suite_revalidates_all_census_receipts_before_recording(self):
        for label in ("c0-draft", "c0-flip-only", "c1"):
            prefix = "freeze-" + label + "-31"
            self.author_receipts(prefix, label)
            rc, text = self.invoke("author-suite", self.root, prefix, label)
            self.assertEqual(rc, 0, text)
            self.assertEqual(json.loads(text)["worktree_state"],
                             "contract_modified" if label == "c0-flip-only" else "clean")
            for stage in ("before", "after"):
                for suffix, payload, code in (("status-" + stage + ".z", b"?? market_1.987654321\0",
                                              "author:worktree_state"),
                                             ("cache-" + stage + ".txt", b"__pycache__\n",
                                              "author:cache_present"),
                                             ("index-" + stage + ".z", b"h tracked.txt\0",
                                              "author:worktree_state"),
                                             *((kind + "-" + stage + extension + ".stderr", b"market_1.987654321\n",
                                                "author:census_stderr") for kind, extension in
                                               (("status", ".z"), ("cache", ".txt"), ("index", ".z")))):
                    self.author_receipts(prefix, label)
                    self.write(prefix + "-" + suffix, payload)
                    rc, text = self.invoke("author-suite", self.root, prefix, label)
                    self.assertEqual((rc, json.loads(text)), (2, {"status": "refused", "code": code}))
                    self.assertNotIn("diff_only_contract", text)
                    self.assertNotIn("987654321", text)

    def test_author_state_requires_ordinary_complete_index_rows_and_exact_flip_state(self):
        for label in ("c0-draft", "c0-flip-only", "c1"):
            prefix = "freeze-" + label + "-32"
            self.author_receipts(prefix, label)
            status = self.root / (prefix + "-status-before.z")
            cache = self.root / (prefix + "-cache-before.txt")
            index = self.root / (prefix + "-index-before.z")
            for data in (b"", b"H tracked.txt", b"h tracked.txt\0", b"S tracked.txt\0", b"s tracked.txt\0",
                         b"M tracked.txt\0", b"H first.txt\0h second.txt\0", b"H tracked.txt\0\0",
                         b"h first.txt\0H second.txt\0", b"S first.txt\0H second.txt\0",
                         b"H first.txt\0h middle.txt\0H last.txt\0",
                         b"H first.txt\0S middle.txt\0H last.txt\0", b"H first.txt\0H second.txt"):
                with self.subTest(label=label, index=data):
                    index.write_bytes(data)
                    rc, text = self.invoke("author-state", label, status, cache, index)
                    self.assertEqual((rc, json.loads(text)),
                                     (2, {"status": "refused", "code": "author:worktree_state"}))
            index.write_bytes(b"H tracked.txt\0")
            self.assertEqual(self.invoke("author-state", label, status, cache, index)[0], 0)
            if label == "c0-flip-only":
                status.write_bytes(b"")
                rc, text = self.invoke("author-state", label, status, cache, index)
                self.assertEqual((rc, json.loads(text)["code"]), (2, "author:worktree_state"))
            status.write_bytes(b"")
            rc, text = self.invoke("author-state", "unknown", status, cache, index)
            self.assertEqual((rc, json.loads(text)["code"]), (2, "extractor:shape"))
            with self.assertRaises(lab.ExtractorRefusal) as error:
                lab.author_state("unknown", status, cache, index)
            self.assertEqual(error.exception.args, ("extractor:shape",))

    def test_author_suite_refuses_head_or_tree_movement_after_the_suite(self):
        for label in ("c0-draft", "c0-flip-only", "c1"):
            prefix = "freeze-" + label + "-33"
            for after in (("c" * 40 + "\n" + "b" * 40 + "\n"), ("a" * 40 + "\n" + "c" * 40 + "\n")):
                self.author_receipts(prefix, label)
                self.write(prefix + "-git-after.txt", after.encode())
                rc, text = self.invoke("author-suite", self.root, prefix, label)
                self.assertEqual((rc, json.loads(text)),
                                 (2, {"status": "refused", "code": "author:head_changed"}))

    def test_author_census_stderr_never_reaches_the_projection(self):
        for label in ("c0-draft", "c0-flip-only", "c1"):
            prefix = "freeze-" + label + "-34"
            for suffix in ("status-before.z", "cache-before.txt", "index-before.z"):
                self.author_receipts(prefix, label)
                self.write(prefix + "-" + suffix + ".stderr", b"market_1.987654321\n")
                args = [self.root / (prefix + "-" + name)
                        for name in ("status-before.z", "cache-before.txt", "index-before.z")]
                rc, text = self.invoke("author-state", label, *args)
                self.assertEqual((rc, json.loads(text)),
                                 (2, {"status": "refused", "code": "author:census_stderr"}))
                self.assertNotIn("1.987654321", text)

    def test_author_suite_requires_every_census_stderr_and_after_head_receipt(self):
        for label in ("c0-draft", "c0-flip-only", "c1"):
            prefix = "freeze-" + label + "-35"
            suffixes = [kind + "-" + stage + extension + ".stderr" for stage in ("before", "after")
                        for kind, extension in (("status", ".z"), ("cache", ".txt"), ("index", ".z"))]
            for suffix in [*suffixes, "git-after.txt"]:
                with self.subTest(label=label, missing=suffix):
                    self.author_receipts(prefix, label)
                    (self.root / (prefix + "-" + suffix)).unlink()
                    rc, output = self.invoke("author-suite", self.root, prefix, label)
                    self.assertEqual(rc, 2, output)
                    self.assertEqual(json.loads(output), {"status": "refused", "code": "extractor:unexpected"})

    def test_notice_and_consent_reject_near_miss_keys_and_duplicate_keys(self):
        inventory = self.write("l3-01-inventory.json", {"input_sha256": SHA})
        certificate = self.write("step6-01-certificate-check.json", {"sha256": SHA})
        notice = "DATE=2026-10-17\nMESSAGE_REF=operator message 17\nC0=" + "b" * 40 + "\n"
        consent = (notice + "NOTICE_TEXT=Yu received the identifiers.\nCONSENT_TEXT=Proceed with this run.\n"
                   "INPUT_SHA=" + SHA + "\nCERT_SHA=" + SHA + "\n")
        for command, text, key, args in (("notice", notice, "C0", []),
                                         ("consent", consent, "INPUT_SHA", [inventory, certificate])):
            record = self.write("record.md", text.encode())
            self.assertEqual(self.invoke(command, record, *args)[0], 0)
            for altered in (" " + key, "\t" + key, key + " ", key.lower(), "#" + key, "\ufeff" + key,
                            "\u0421" + key[1:], key[:-1] + "\u0410", key[:1] + "a" + key[1:],
                            "_" + key, "9" + key):
                record.write_text(text + altered + "=conflicting\n")
                rc, out = self.invoke(command, record, *args)
                self.assertEqual(rc, 2, out)
                self.assertEqual(json.loads(out)["code"], "notice:record", altered)
            for row in (key + "＝conflicting", "MESSAGE_REF=other message", "EXTRA=one\nEXTRA=two"):
                record.write_text(text + row + "\n")
                rc, out = self.invoke(command, record, *args)
                self.assertEqual(rc, 2, out)
                self.assertEqual(json.loads(out)["code"], "notice:record", row)
            record.write_text(text + "\nEXTRA=free form text = and # are values\n")
            self.assertEqual(self.invoke(command, record, *args)[0], 0)

    def test_notice_and_consent_use_only_lf_record_boundaries(self):
        inventory = self.write("l3-01-inventory.json", {"input_sha256": SHA})
        certificate = self.write("step6-01-certificate-check.json", {"sha256": SHA})
        notice = "DATE=2026-10-17\nMESSAGE_REF=operator message 17\nC0=" + "b" * 40 + "\n"
        consent = (notice + "NOTICE_TEXT=Yu received the identifiers.\nCONSENT_TEXT=Proceed with this run.\n"
                   "INPUT_SHA=" + SHA + "\nCERT_SHA=" + SHA + "\n")
        for command, text, args in (("notice", notice, []), ("consent", consent, [inventory, certificate])):
            record = self.write("record.md", text.encode())
            self.assertEqual(self.invoke(command, record, *args)[0], 0)
            for separator in "\r\v\f\x1c\x1d\x1e\x85\u2028\u2029":
                for altered in (text.replace("\n", separator), text.replace("\n", separator + "\n"),
                                text + "EXTRA=value" + separator + "tail\n", text.rstrip("\n") + separator):
                    with self.subTest(command=command, separator=repr(separator)):
                        record.write_bytes(altered.encode())
                        rc, output = self.invoke(command, record, *args)
                        self.assertEqual((rc, json.loads(output)["code"]), (2, "notice:record"))
            record.write_bytes(text.encode() + b"EXTRA=value\xff\n")
            rc, output = self.invoke(command, record, *args)
            self.assertEqual(rc, 2, output)
            self.assertEqual(json.loads(output), {"status": "refused", "code": "extractor:unexpected"})

    def test_recovery_attempt_ordering_is_numeric_and_same_number_aliases_refuse(self):
        for index, (current, other, expected) in enumerate((("10", "9", 0), ("9", "10", 2),
                                                           ("01", "001", 2), ("001", "01", 2))):
            directory = self.root / str(index)
            directory.mkdir()
            prefix = "step8-" + current
            (directory / ("session-" + prefix + "-job.zsh")).write_bytes(b"# job\n")
            (directory / (prefix + "-state-check.txt.exit")).write_bytes(b"0\n")
            (directory / (prefix + "-run.json")).write_bytes(b"")
            (directory / ("session-step8-" + other + "-job.zsh")).write_bytes(b"# other\n")
            rc, text = self.invoke("recovery-attempt", directory, prefix)
            self.assertEqual(rc, expected, (current, other, text))
            if expected:
                self.assertEqual(json.loads(text)["code"], "recovery:newer_attempt")
        rc, text = self.invoke("recovery-attempt", self.root, "step8-01-extra")
        self.assertEqual((rc, json.loads(text)["code"]), (2, "recovery:attempt_prefix"))
        with mock.patch.object(lab.Path, "lstat", side_effect=PermissionError("market_1.987654321")):
            rc, text = self.invoke("recovery-attempt", self.root, "step8-01")
        self.assertEqual((rc, json.loads(text)["code"]), (2, "recovery:checkpoint_not_regular"))

    def test_step8_bundle_publishes_all_operator_status_and_writer_exit_signals(self):
        for index, status in enumerate(("uncertain", "unknown", "no_json")):
            prefix = "step8-" + str(800 + index)
            self.write(prefix + "-status.txt", (status + "\n").encode())
            self.write(prefix + "-status.txt.exit", b"0\n")
            self.write(prefix + "-after-writers.txt", b"market_1.987654321\n")
            self.write(prefix + "-after-writers.txt.exit", b"0\n")
            rc, text = self.invoke("publish-attempt", self.root, prefix, SHA, "")
            self.assertEqual(rc, 0, text)
            rows = [json.loads(row) for row in text.splitlines()]
            named = {row["raw_report"]["lab_local_file"]: row for row in rows if "raw_report" in row}
            for suffix in ("status.txt", "after-writers.txt", "after-writers.txt.exit"):
                self.assertIn(prefix + "-" + suffix, named)
            self.assertIn("code", named[prefix + "-status.txt"])
            self.assertEqual(named[prefix + "-status.txt"]["code"], status)
            self.assertEqual(named[prefix + "-after-writers.txt"]["classification"], "unclassified")
            self.assertEqual(named[prefix + "-after-writers.txt.exit"]["exit"], 0)
            self.assertNotIn("1.987654321", text)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_lock_tool_refusal_is_not_mislabelled_as_note_mismatch(self):
        env = self.shell_environment()
        result = self.run_shell('''lab_tool() { print -r -- '{"code":"runbook:tool_hash_mismatch"}'; return 2; }
lab_lock_match step9-01-runner-lock "$LAB_LOCK"
''', env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("recovery:lock_check_refused", result.stdout)
        self.assertNotIn('"code":"recovery:lock_note_mismatch"', result.stdout)
        self.assertEqual((self.root / "step9-01-runner-lock-check.txt.exit").read_text(), "2\n")

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_final_publication_refuses_capture_failure_even_after_a_both_gone_row(self):
        source = '''lab_liveness() {
  print -r -- 'job_pgrep_exit=1 writer_pgrep_exit=1'
  print -r -- 'block_stdout=present'
  if [[ -f "$LAB_EVIDENCE/observed" ]]; then return 2; fi
  print -r -- yes > "$LAB_EVIDENCE/observed"
}
lab_publication step8-810 final
'''
        self.write("session-step8-810-job.zsh", b"# job\n")
        result = self.run_shell(source)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("publication:liveness_refused", result.stdout)
        self.assertNotIn("job_pending", result.stdout)
        self.assertFalse((self.root / "step8-810-final-publication.jsonl").exists())
        self.assertEqual((self.root / "step8-810-final-liveness.txt.exit").read_text(), "2\n")

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_observation_failure_with_both_gone_row_never_reaches_the_final_capture(self):
        self.write("session-step8-811-job.zsh", b"# job\n")
        result = self.run_shell('''lab_liveness() {
  print -r -- 'job_pgrep_exit=1 writer_pgrep_exit=1'
  print -r -- 'block_stdout=present'
  return 2
}
lab_publication step8-811 final
''')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("publication:liveness_observation_refused", result.stdout)
        self.assertNotIn("job_pending", result.stdout)
        self.assertFalse((self.root / "step8-811-final-liveness.txt").exists())

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_liveness_queries_ps_for_both_distinct_job_and_writer_pids(self):
        result = self.run_shell('''pgrep() {
  if (( $# == 3 )); then [[ "$1" = -d, && "$2" = -f ]] || return 99; print -r -- 123
  else [[ $# == 4 && "$1" = -d, && "$2" = -f && "$3" = -- ]] || return 99; print -r -- 456; fi
}
ps() {
  [[ $# == 4 && "$1" = -o && "$2" = pid=,ppid=,stat=,etime=,comm= && "$3" = -p ]] || return 99
  print -r -- "queried_pid=$4"
}
lab_liveness step8-812-job
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("queried_pid=123\n", result.stdout)
        self.assertIn("queried_pid=456\n", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_removal_runs_fresh_recheck_before_either_lock_can_be_removed(self):
        for index, (operation, fault) in enumerate((operation, fault)
                for operation in ("ledger", "runner") for fault in ("writer", "liveness", "writer_live", "drift")):
            suffix = str(820 + index)
            env, runner, ledger, _ = self.recovery_fixture(suffix)
            env.update(STEP9_LEDGER_PREFIX="step9-ledger-" + suffix, FAULT=fault)
            self.write("session-" + env["STEP9_RECHECK_PREFIX"] + ".zsh", b"# reviewed\n")
            if operation == "runner":
                ledger.unlink()
                env["RECOVERY_CENSUS_PREFIX"] = "step9-ledger-" + suffix + "-after"
                baseline = self.run_shell('lab_lock_census "$RECOVERY_CENSUS_PREFIX"\n', env)
                self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
            if fault == "drift":
                (runner.parent / "trial-ledger-v1.jsonl").write_bytes(b"changed-length\n")
            prefix = env["STEP9_" + operation.upper() + "_PREFIX"]
            result = self.run_shell(self.RECOVERY_STUBS + "lab_" + operation + "_remove\n", env)
            self.assertEqual(result.returncode, 2, (operation, fault, result.stdout, result.stderr))
            if fault == "drift":
                self.assertEqual((self.root / (prefix + "-recheck-census-compare.txt.exit")).read_text(), "1\n")
            else:
                self.assertIn({"writer": "recovery:writer_present", "liveness": "recovery:liveness_refused",
                               "writer_live": "recovery:job_or_writer_live"}[fault], result.stdout)
            self.assertTrue(runner.exists())
            self.assertEqual(ledger.exists(), operation == "ledger")
            for ending in ("rm-ledger-lock.txt", "rm-runner-lock.txt", "reconcile.json"):
                self.assertFalse((self.root / (prefix + "-" + ending)).exists())
            self.assertNotIn("checkpoint_saved=" + prefix + "\n", result.stdout)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_ledger_verify_checks_other_run_note_before_reading_a_valid_ledger(self):
        from betangel_research.betfair.backtest.trial_ledger import TrialLedger
        env, _, lock, note = self.recovery_fixture(verified=False)
        ledger_path = lock.with_suffix("")
        ledger_path.unlink()
        lock.unlink()
        contract = json.loads((ROOT / "research/experiments/backtest-p0-bsp-v1.json").read_text())
        contract.update(status="frozen", frozen_on="2026-10-17")
        TrialLedger.open(lock.parent).append("started", trial_id="run-1.ltp_favourite", contract=contract,
            variant="ltp_favourite", input_sha256=SHA, code_identity=CODE,
            recorded_at="2026-10-17T00:00:00Z", summary={})
        note["trial_id"] = "run-2.ltp_favourite"
        lock.write_text(json.dumps(note))
        child = subprocess.run([sys.executable, "-S", "-B", "-", str(ledger_path)], input=lab.LEDGER_CODE,
                               cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertEqual(json.loads(child.stdout)["status"], "verified")
        result = self.run_shell('cd "$LAB_CHECKOUT" || exit 2\n' + self.RECOVERY_STUBS + "lab_ledger_verify\n", env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("recovery:ledger_lock_note_mismatch", result.stdout)
        self.assertFalse((self.root / "step9-verify-01-ledger-verify.json").exists())
        self.assertNotIn("checkpoint_saved=step9-verify-01\n", result.stdout)

    def native_pgrep_probe(self):
        marker = "runbooknative" + os.urandom(12).hex()
        child = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c",
                                  "import signal; signal.pause()", marker], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            result = subprocess.run(["/usr/bin/pgrep", "-d,", "-f", "--", marker],
                                    capture_output=True, text=True, check=False)
            if result.returncode not in (0, 1):
                self.skipTest("native pgrep unavailable in this sandbox (exit " + str(result.returncode) + ")")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(child.pid), result.stdout.strip().split(","))
        finally:
            child.terminate()
            child.wait(timeout=5)

    @unittest.skipUnless(all(Path(p).exists() for p in ("/bin/zsh", "/usr/bin/pgrep", "/usr/bin/nohup",
                                                     "/usr/bin/caffeinate")), "native macOS job tools unavailable")
    def test_native_pgrep_drives_real_detach_and_liveness_before_and_after_receipt(self):
        self.native_pgrep_probe()
        env = self.shell_environment()
        release = self.root / "release"
        env["RELEASE"] = str(release)
        checkpoint = self.write("session-step8-850-job.zsh", b'''lab_probe_job() {
  print -r -- ready
  local n=0
  while [[ ! -f "$RELEASE" ]] && (( n < 500 )); do /bin/sleep 0.02; (( n++ )); done
  [[ -f "$RELEASE" ]] || return 6
  return 4
}
''')
        result = self.run_shell('''lab_probe_job() { return 4; }
lab_detach step8-850-job "$CHECKPOINT" lab_probe_job
''', {**env, "CHECKPOINT": str(checkpoint)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.root / "step8-850-job.block.stdout"
        receipt = self.root / "step8-850-job.block.exit"
        try:
            deadline = time.monotonic() + 5
            while (not output.exists() or output.read_text() != "ready\n") and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(output.is_file())
            self.assertEqual(output.read_text(), "ready\n")
            def observe():
                # Another agent's CLI can end between pgrep and ps; retry only that exact transient.
                for attempt in range(4):
                    result = self.run_shell("lab_liveness step8-850-job\n", env)
                    if result.returncode != 2 or '"code":"liveness:ps_refused"' not in result.stdout:
                        return result
                    time.sleep(0.02)
                return result
            live = observe()
            self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
            self.assertRegex(live.stdout, r"(?m)^job_pgrep_exit=0 writer_pgrep_exit=[01]$")
            release.touch()
            deadline = time.monotonic() + 5
            while (not receipt.exists() or not receipt.read_bytes()) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(receipt.read_text(), "4\n")
            while time.monotonic() < deadline:
                live = observe()
                if live.returncode == 0 and "job_pgrep_exit=1 " in live.stdout:
                    break
                time.sleep(0.02)
            self.assertEqual(live.returncode, 0, live.stdout + live.stderr)
            self.assertRegex(live.stdout, r"(?m)^job_pgrep_exit=1 writer_pgrep_exit=[01]$")
        finally:
            release.touch(exist_ok=True)
            deadline = time.monotonic() + 5
            while not receipt.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            # Only our bounded job is released; no machine-wide signal or quiet-machine assumption.

    @unittest.skipUnless(Path("/usr/bin/pgrep").exists(), "native pgrep is not installed")
    def test_native_writer_regexes_find_owned_sentinels_with_production_flags(self):
        self.native_pgrep_probe()
        patterns = re.findall(r"pgrep (-fl --|-d, -f --) '(-m betangel_research[^']+)'", SHELL.read_text())
        self.assertEqual(len(patterns), 2)
        marker = "runbookwriter" + os.urandom(12).hex()
        for module in ("backtest", "readiness"):
            child = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c",
                                      "import signal; signal.pause()", "-m", marker + ".betfair." + module,
                                      "synthetic"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for flags, pattern in patterns:
                    result = subprocess.run(["/usr/bin/pgrep", *flags.split(),
                                             pattern.replace("betangel_research", marker)],
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, (flags, pattern, result.stderr))
                    pids = (result.stdout.strip().split(",") if flags.startswith("-d,") else
                            [row.split()[0] for row in result.stdout.splitlines()])
                    self.assertIn(str(child.pid), pids)
            finally:
                child.terminate()
                child.wait(timeout=5)


class PostP0FreezeTests(ToolTest):
    freeze_fence = FollowupGateTests.freeze_fence
    CONTRACT = "research/experiments/backtest-p0-bsp-v1.json"

    def git(self, checkout, *args):
        return subprocess.run(["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null",
                               "-C", str(checkout), *args], env=self.git_env, capture_output=True,
                              check=True).stdout

    def repository(self, name):
        self.git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_NO_REPLACE_OBJECTS": "1"}
        checkout = self.root / name
        checkout.mkdir()
        self.git(checkout, "init", "-q")
        (checkout / self.CONTRACT).parent.mkdir(parents=True)
        (checkout / self.CONTRACT).write_text('{"status":"draft","frozen_on":null}\n')
        (checkout / "tracked.py").write_text("VALUE=1\n")
        (checkout / "name with space\nand\ttab.txt").write_text("ordinary bytes\n")
        self.commit(checkout)
        return checkout

    def commit(self, checkout):
        self.git(checkout, "add", ".")
        self.git(checkout, "-c", "user.name=Runbook test", "-c", "user.email=runbook@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic freeze fixture")

    def bytes_gate(self, checkout, label):
        return subprocess.run([sys.executable, "-I", "-S", "-B", str(TOOL), "author-bytes", label],
                              cwd=checkout, env=self.git_env, capture_output=True, text=True, check=False)

    def clean_filter(self, checkout):
        (checkout / ".git/info/attributes").write_text("tracked.py filter=restore\n")
        self.git(checkout, "config", "filter.restore.clean", "sed s/VALUE=2/VALUE=1/")

    def test_author_bytes_refuses_filter_hidden_edits_for_each_label(self):
        # F3-FW-R1: status and diff are clean after the index learns the filtered bytes.
        for label in ("c0-draft", "c0-flip-only", "c1"):
            with self.subTest(label=label):
                checkout = self.repository(label)
                if label == "c0-flip-only":
                    (checkout / self.CONTRACT).write_text('{"status":"frozen","frozen_on":"2026-10-17"}\n')
                clean = self.bytes_gate(checkout, label)
                self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
                self.assertEqual(json.loads(clean.stdout), {"status": "accepted", "checked":
                                 2 if label == "c0-flip-only" else 3, "skipped": int(label == "c0-flip-only")})
                self.clean_filter(checkout)
                path = checkout / "tracked.py"
                before = path.stat()
                path.write_text("VALUE=2\n")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                expected = (b" M " + self.CONTRACT.encode() + b"\0" if label == "c0-flip-only" else b"")
                self.assertEqual(self.git(checkout, "status", "--porcelain=v1", "-z"), expected)
                self.assertEqual(self.git(checkout, "diff", "--name-only", "HEAD"),
                                 (self.CONTRACT + "\n").encode() if label == "c0-flip-only" else b"")
                result = self.bytes_gate(checkout, label)
                self.assertEqual((result.returncode, json.loads(result.stdout)),
                                 (2, {"status": "refused", "code": "author:tracked_bytes"}))
                self.assertEqual(result.stderr, "")

    def test_author_bytes_checks_all_paths_and_only_skips_the_flip_contract(self):
        checkout = self.repository("paths")
        for label in ("c0-draft", "c0-flip-only", "c1"):
            for name in ("name with space\nand\ttab.txt", self.CONTRACT, "tracked.py"):
                with self.subTest(label=label, name=name):
                    self.git(checkout, "restore", ".")
                    (checkout / name).write_text("market_1.987654321\n")
                    result = self.bytes_gate(checkout, label)
                    accepted = label == "c0-flip-only" and name == self.CONTRACT
                    self.assertEqual(result.returncode, 0 if accepted else 2, result.stdout)
                    if not accepted:
                        self.assertEqual(json.loads(result.stdout)["code"], "author:tracked_bytes")
                    self.assertNotIn("987654321", result.stdout + result.stderr)
        self.git(checkout, "restore", ".")
        target = checkout / "tracked.py"
        target.unlink()
        self.assertEqual(self.bytes_gate(checkout, "c1").returncode, 2)
        target.symlink_to(checkout / "name with space\nand\ttab.txt")
        self.assertEqual(self.bytes_gate(checkout, "c1").returncode, 2)
        self.assertEqual(self.bytes_gate(checkout, "unknown").returncode, 2)

    def test_freeze_fence_runs_byte_gates_immediately_after_both_state_checks(self):
        fence = self.freeze_fence()
        gate = '"$FREEZE_PY" -I -S -B "$FREEZE_TOOL" author-bytes "$FREEZE_LABEL" || exit 2\n'
        self.assertEqual(fence.count(gate), 2)
        for stage in ("before", "after"):
            self.assertIn('"$FREEZE_EVIDENCE/$FREEZE_PREFIX-index-' + stage + '.z" || exit 2\n' + gate, fence)
        flip_recheck = ('if [[ "$FREEZE_LABEL" = c0-flip-only ]]; then\n'
                        '  "$FREEZE_PY" -I -S -B "$FREEZE_TOOL" freeze-diff '
                        '"$FREEZE_EVIDENCE/$FREEZE_PREFIX-contract-head.json" \\\n'
                        '    "$FREEZE_CHECKOUT/research/experiments/backtest-p0-bsp-v1.json" || exit 2\nfi\n')
        self.assertEqual(fence.count(flip_recheck), 1)
        self.assertIn(gate + flip_recheck + '"$FREEZE_PY" -I -S -B "$FREEZE_TOOL" author-suite ', fence)

    def test_freeze_prose_names_byte_limits_and_non_h_refusal(self):
        doc = (ROOT / "docs/backtest-plan.md").read_text()
        section = doc.split("#### 5. Reviewed freeze", 1)[1].split("#### 5a.", 1)[0]
        self.assertTrue("Python 一律加 -B" in section)  # DOC-F3-1, old text: Python 只加 -B.
        self.assertFalse("Python 只加 -B" in section)
        for phrase in ("Suite process 用 -E -s -S -B：", "唔載入 site／.pth／sitecustomize",
                       "suite 後 author-suite 即報 author:c1_diff", "非 `H ` rows 報 author:worktree_state。",
                       "只喺 c0-flip-only 跳過 contract", "唔證明 mode、執行時載入嘅 bytes",
                       "偵測唔到兩次檢查之間改完再還原", "clean filter／attributes", "子程序",
                       "每次 clean-state check 後，author-bytes 要求每個 HEAD entry 都係 100644/100755 blob，"
                       "worktree path 係 regular file；",
                       "Symlink／gitlink entry、缺失或非 regular path、git／OS failure 或 bytes 唔相等，"
                       "都報 author:tracked_bytes；", "suite 前後都要通過 freeze-diff。"):
            self.assertEqual(section.count(phrase), 1, phrase)
        self.assertNotRegex(section, r'(?i)再試|重試|retry|rerun')

    def test_history_distinguishes_post_p0_gates_from_the_first_freeze(self):
        doc = (ROOT / "docs/backtest-plan.md").read_text()
        preamble = doc.split("### 10.1 Runbook:", 1)[1].split("**首次 run 記錄", 1)[0]
        self.assertIn("post-P0 加入 author-bytes、-E -s -S -B 同 author:c1_diff gate", preamble)
        self.assertIn("2026-10-17 freeze 跑 C0 e452f2e 嘅 fence，suite line 只有 -B。", preamble)
        self.assertEqual(doc.count("按當時（C0 e452f2e）§10.1 完成"), 1)

    def test_suite_flags_block_python_environment_and_site_startup_but_keep_checkout_imports(self):
        fence = self.freeze_fence()
        flags = shlex.split(re.search(r'"\$FREEZE_PY" ([-\w ]+) -m unittest', fence)[1])
        checkout = self.root / "discovery"
        (checkout / "tests").mkdir(parents=True)
        (checkout / "local_fixture.py").write_text("VALUE=17\n")
        (checkout / "tests/test_smoke.py").write_text(
            "import sys, unittest, local_fixture\n"
            "class Smoke(unittest.TestCase):\n"
            "    def test_startup(self):\n"
            "        self.assertEqual(local_fixture.VALUE, 17)\n"
            "        self.assertTrue(sys.flags.ignore_environment)\n"
            "        self.assertTrue(sys.flags.no_site)\n"
            "        self.assertTrue(sys.flags.no_user_site)\n"
            "        self.assertEqual(sys.flags.optimize, 0)\n"
            "        self.assertNotIn('sitecustomize', sys.modules)\n")
        poison = self.root / "poison"
        poison.mkdir()
        marker = self.root / "startup-ran"
        (poison / "sitecustomize.py").write_text("from pathlib import Path\nPath(" + repr(str(marker))
                                                + ").write_text('unexpected startup')\n")
        env = {**os.environ, "PYTHONPATH": str(poison), "PYTHONOPTIMIZE": "1", "PYTHONUSERBASE": str(poison)}
        for home in (None, str(self.root / "not-a-python-home")):
            if home is not None:
                env["PYTHONHOME"] = home
            result = subprocess.run([sys.executable, *flags, "-m", "unittest", "discover", "-s", "tests", "-v"],
                                    cwd=checkout, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Ran 1 test", result.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse(any(checkout.rglob("__pycache__")))

    def mini_fence(self, case, label):
        checkout = self.repository(case)
        (checkout / self.CONTRACT).write_text('{"status":"draft","frozen_on":null,"max_trials":4}\n')
        (checkout / "scripts").mkdir()
        shutil.copyfile(TOOL, checkout / "scripts/lab_runbook.py")
        (checkout / "tests").mkdir()
        (checkout / "tests/test_smoke.py").write_text(
            "from pathlib import Path\nimport json, os, subprocess, unittest\n"
            "class Smoke(unittest.TestCase):\n"
            "    def test_one(self):\n"
            "        if os.environ['FREEZE_CASE'] == 'after_filter':\n"
            "            path = Path('tracked.py')\n"
            "            before = path.stat()\n"
            "            path.write_text('VALUE=2\\n')\n"
            "            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))\n"
            "        if os.environ['FREEZE_CASE'] == 'head_move':\n"
            "            subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c',\n"
            "                'user.name=Runbook test', '-c', 'user.email=runbook@example.invalid',\n"
            "                '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'moved'], check=True)\n"
            "        if os.environ['FREEZE_CASE'] == 'contract_rewrite':\n"
            "            path = Path('research/experiments/backtest-p0-bsp-v1.json')\n"
            "            contract = json.loads(path.read_text())\n"
            "            contract['max_trials'] = 9\n"
            "            path.write_text(json.dumps(contract) + '\\n')\n"
            "        self.assertNotEqual(os.environ['FREEZE_CASE'], 'suite_failure')\n")
        self.commit(checkout)
        c0 = self.git(checkout, "rev-parse", "HEAD").decode().strip()
        if case == "extra_c1_path":
            (checkout / "tracked.py").write_text("VALUE=2\n")
            self.commit(checkout)  # MUT-C1BASE-1: HEAD^..HEAD alone would miss this code commit.
        if label != "c0-draft":
            (checkout / self.CONTRACT).write_text('{"status":"frozen","frozen_on":"2026-10-17","max_trials":4}\n')
            if label == "c1":
                self.commit(checkout)
        if case == "case_rename":
            self.git(checkout, "config", "core.ignorecase", "true")
            (checkout / "tests/test_smoke.py").rename(checkout / "tests/Test_smoke.py")
        if case in {"before_filter", "after_filter"}:
            self.clean_filter(checkout)
        if case == "before_filter":
            (checkout / "tracked.py").write_text("VALUE=2\n")
        evidence = self.root / (case + "-evidence")
        evidence.mkdir()
        wrapper = self.root / (case + "-python")
        # Air tests use their actual interpreter; only the lab's 3.15 prerequisite is stubbed.
        wrapper.write_text("#!/bin/zsh -f\nif [[ \"$5\" = author-interpreter ]]; then\n"
                           "  print -r -- '{}'; exit 0\nfi\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        wrapper.chmod(0o700)
        prefix = "freeze-" + label + "-81"
        values = {"FREEZE_CHECKOUT": checkout, "FREEZE_PY": wrapper, "FREEZE_EVIDENCE": evidence,
                  "FREEZE_C0": c0, "FREEZE_LABEL": label, "FREEZE_PREFIX": prefix}
        fence = self.freeze_fence()
        for key, value in values.items():
            fence = re.sub(r"(?m)^" + key + r"=.*$", lambda _: key + "=" + shlex.quote(str(value)), fence)
        result = subprocess.run(["/bin/zsh", "-f"], input=fence, cwd=self.root,
                                env={**self.git_env, "FREEZE_CASE": case}, capture_output=True,
                                text=True, check=False, timeout=30)
        return result, evidence, prefix

    @unittest.skipUnless(Path("/bin/zsh").exists(), "zsh is not installed")
    def test_literal_freeze_fence_refuses_hidden_bytes_c1_diff_and_failed_suite(self):
        cases = (("draft", "c0-draft", None), ("flip", "c0-flip-only", None), ("frozen", "c1", None),
                 ("before_filter", "c0-draft", "author:tracked_bytes"),
                 ("after_filter", "c0-draft", "author:tracked_bytes"),
                 ("extra_c1_path", "c1", "author:c1_diff"), ("suite_failure", "c0-draft", "FAILED"),
                 ("case_rename", "c0-draft", "author:worktree_state"),
                 ("head_move", "c0-draft", "author:head_changed"),
                 ("contract_rewrite", "c0-flip-only", "contract:settings_changed"))
        for case, label, code in cases:
            with self.subTest(case=case):
                result, evidence, prefix = self.mini_fence(case, label)
                self.assertEqual(result.returncode, 0 if code is None else 2, result.stdout + result.stderr)
                if code is not None:
                    self.assertIn(code, result.stdout)
                self.assertEqual((evidence / ("session-" + prefix + ".zsh")).exists(), code is None)
                self.assertEqual((evidence / (prefix + "-suite.exit")).exists(),
                                 case not in {"before_filter", "case_rename"})


if __name__ == "__main__":
    unittest.main()
