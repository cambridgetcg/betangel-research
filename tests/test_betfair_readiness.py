"""Index-only readiness on synthetic archives: runner parity, read boundaries and failure accounting.

Fixture construction may ingest, index and (in one parity test) run a frozen synthetic contract. The inventory
itself is surrounded by raw-read, write and evaluation traps. No real corpus or lab-state directory is accessed.
"""

import ast
import builtins
import bz2
from contextlib import ExitStack, redirect_stdout
import copy
from dataclasses import asdict
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import unittest
from unittest import mock

from betangel_research.betfair import readiness
from betangel_research.betfair.backtest import certificate, cohort, engineering, market_index, metrics, replay, runner
from betangel_research.betfair.backtest import accounting, trial_ledger
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, gate_snapshot
from betangel_research.betfair.backtest.contract import canonical_json, contract_sha256

import test_betfair_backtest_replay as fx
import test_betfair_backtest_runner as runner_fx

M = "1.100000001"
CODE = "synthetic operator label"

# None disables the process-wide hook; only the extra-archive inventory call enables it.
_INPUT_OPEN_AUDIT = None


def _record_input_opens(event, args):
    if event != "open" or _INPUT_OPEN_AUDIT is None:
        return
    path = args[0]
    if isinstance(path, (str, bytes, os.PathLike)):
        roots, opened = _INPUT_OPEN_AUDIT
        candidate = Path(os.fsdecode(path)).absolute()
        if any(parent in candidate.parents for parent in roots):
            opened.append(candidate)


sys.addaudithook(_record_input_opens)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ReadinessCase(fx.Workspace):
    def setUp(self):
        super().setUp()
        self.contract_value = json.loads(fx.DRAFT.read_bytes())
        self.contract_path = self.tmp / "draft.json"
        self.write_contract()
        self.data = self.manifest = self.records = self.archive = None

    def write_contract(self):
        self.contract_path.write_bytes(canonical_json(self.contract_value))

    def inputs(self, members=None):
        self.data, self.manifest, self.records = self.build(members or [(fx.path(M), fx.standard_market(M))])
        self.archive = self.manifest["archive"]["sha256"]
        self.head_path = self.data / "index" / self.archive / market_index.HEAD_FILE
        self.markets_path = self.head_path.with_name(market_index.MARKETS_FILE)
        self.index_sha = sha(self.head_path.read_bytes())

    def inventory(self, **changes):
        args = dict(contract_path=self.contract_path, data_root=self.data, expect_archive=self.archive,
                    expect_index_sha256=self.index_sha, code_identity=CODE)
        args.update(changes)
        return readiness.inventory(**args)

    def rewrite(self, records=None, data=None, head_edit=None, manifest_edit=None):
        head = json.loads(self.head_path.read_bytes())
        if manifest_edit is not None:
            manifest_path = self.data / "manifests" / f"{self.archive}.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest_edit(manifest)
            manifest_data = canonical_json(manifest) + b"\n"
            manifest_path.chmod(0o600)
            manifest_path.write_bytes(manifest_data)
            head["manifest_sha256"] = sha(manifest_data)
        if records is not None:
            data = b"".join(canonical_json(record) + b"\n" for record in records)
        if data is not None:
            self.markets_path.chmod(0o600)
            self.markets_path.write_bytes(data)
            head.update(markets_sha256=sha(data), markets=data.count(b"\n"))
        if head_edit is not None:
            head_edit(head)
        self.head_path.chmod(0o600)
        self.head_path.write_bytes(canonical_json(head) + b"\n")
        self.index_sha = sha(self.head_path.read_bytes())

    def assertRefused(self, result, code):
        self.assertEqual(result, {"kind": readiness.KIND, "version": readiness.VERSION,
                                  "status": "refused", "refusal": code})

    def assertAccounting(self, result):
        self.assertIn(result["status"], ("completed", "completed_with_problems"), result)
        counts = result["records"]
        self.assertEqual(counts["lines"], counts["classified"] + counts["with_problems"])
        self.assertEqual(counts["lines"], sum(item["markets"] for item in result["input"]["archives"]))
        self.assertEqual(counts["with_problems"], sum(result["record_problems"].values()))
        self.assertEqual(counts["classified"], sum(result["population"]["by_class"].values()))
        self.assertEqual(counts["classified"], sum(result["records_by_plan"].values()))

    def first_pass_refusal(self):
        archives = runner._inputs(self.data)[0]
        rules = cohort.CohortRules.from_contract(self.contract_value)
        try:
            count, eligible, _ = runner._first_pass(archives, rules)
            return "inputs:no_records" if not count else "inputs:no_eligible_records" if not eligible else None
        except runner.RunRefused as error:
            return str(error)

    def argv(self):
        return ["--contract", str(self.contract_path), "--data-root", str(self.data),
                "--expect-archive", self.archive, "--expect-index-sha256", self.index_sha, "--code-identity", CODE]


class ParityTests(ReadinessCase):
    def population_inputs(self):
        three = [fx.runner(1), fx.runner(2), fx.runner(3)]
        later = fx.OFF.replace(year=2023, month=1)
        members = [(fx.path(M), fx.standard_market(M))]
        for mid, minutes, fields in (("1.100000002", 1, {}), ("1.100000003", -4, {}),
                                      ("1.100000004", -60, {"eventTypeId": "4339"}),
                                      ("1.100000005", -60, {"countryCode": "US"}),
                                      ("1.100000006", -60, {"marketType": "PLACE"}),
                                      ("1.100000007", -60, {"countryCode": fx.DROP})):
            members.append((fx.path(mid), [fx.md(mid, fx.OFF + timedelta(minutes=minutes),
                                                fx.definition(fx.OFF, three, **fields))]))
        members += [(fx.path("1.100000008", when=later), fx.standard_market("1.100000008", off=later)),
                    (fx.path("1.100000009", plan="ADVANCED"), fx.standard_market("1.100000009"))]
        self.inputs(members)

    def test_counts_and_input_binding_equal_a_synthetic_runs_results(self):
        self.population_inputs()
        result = self.inventory()
        self.assertEqual(result["status"], "completed", result)
        self.assertIsNone(result["input_preflight_would_refuse"])
        self.assertAccounting(result)
        self.assertEqual(result["population"]["by_class"], {"eligible": 1, "excluded": 5, "no_view": 1,
                                                           "anchor_not_known_by_decision": 1, "unclassifiable": 1})
        self.assertEqual(result["prefix_candidates"], 7)
        frozen = copy.deepcopy(self.contract_value)
        frozen.update(status="frozen", frozen_on="2026-10-16")
        frozen_path, lab = self.tmp / "frozen.json", self.tmp / "synthetic-lab"
        frozen_path.write_bytes(canonical_json(frozen))
        lab.mkdir()
        certificate.store_certificate(lab, runner_fx.CERTIFICATE)
        completed = runner.run_backtest(frozen_path, self.data, lab, "parity", CODE, clock=runner_fx.clock)
        self.assertEqual(completed["status"], "completed", completed)
        output = json.loads((lab / "runs" / contract_sha256(frozen) / "parity" / "results.json").read_bytes())
        for field in ("input", "input_sha256"):
            self.assertEqual(result[field], output[field])
        for field in ("by_class", "by_reason", "source_refusals"):
            actual = result["source_refusals"] if field == "source_refusals" else result["population"][field]
            self.assertEqual(actual, output["population"][field])
        self.assertNotEqual(result["contract"]["contract_sha256"], contract_sha256(frozen))

    def test_names_sources_tool_interpreter_gate_and_candidate_settings(self):
        self.inputs()
        result = self.inventory()
        naming = result["naming"]
        self.assertEqual(naming["code_identity"], CODE)
        self.assertEqual(naming["source_descriptor_sha256"], certificate.current_key()[2])
        self.assertEqual(naming["readiness_source_sha256"], sha(Path(readiness.__file__).read_bytes()))
        self.assertEqual(naming["interpreter"], certificate.interpreter_descriptor())
        self.assertEqual(naming["gate_snapshot_sha256"], sha(canonical_json(gate_snapshot())))
        self.assertEqual(result["contract"]["candidate_settings"]["split_months"], ["2015-05", "2016-04"])
        self.assertEqual(result["limits"]["read"], asdict(DEFAULT_LIMITS))
        self.assertEqual(result["raw_prefix_assessability"], "unknown")
        self.assertEqual(result["not_assessed"], [
            "frozen_contract", "engine_certificate", "known_accounting_answers", "raw_prefix_assessability",
            "stopped_views", "market_conditions_2_to_6", "replay", "issuance", "draws", "settlement", "metrics",
            "prefix_evaluation", "runner_lock", "history", "trial_budget", "stop_evidence"])
        self.assertNotIn("run_would_refuse", result)
        other = self.inventory(code_identity="unverified label")
        other["naming"]["code_identity"] = CODE
        self.assertEqual(other, result)

    def test_source_refusals_match_runner_without_combining_levels(self):
        self.inputs()
        self.rewrite(head_edit=lambda h: h.update(
            files=[{"status": "unreadable", "reason": "malformed_message"}, {"status": "indexed", "reason": None}],
            refused_markets=[{"market_id": M, "reason": "month_sealed_holdout"}] * 2),
            manifest_edit=lambda m: m.update(refused=[{"path": "refused", "reason": "no_parseable_month"}]))
        result = self.inventory()
        expected = runner._source_refusals(runner._inputs(self.data)[0])
        self.assertEqual(result["source_refusals"], expected)
        self.assertEqual(expected["market_rows_by_reason"], {"month_sealed_holdout": 2})
        self.assertEqual(expected["distinct_markets_by_reason"], {"month_sealed_holdout": 1})

    def test_no_records_and_no_eligible_records_match_first_pass_gate(self):
        self.inputs()
        excluded = copy.deepcopy(self.records[M])
        excluded["decision_time"]["definition"]["countryCode"] = [[fx.ms(fx.OFF - timedelta(hours=1)), "US"]]
        for records in ([], [excluded]):
            with self.subTest(records=len(records)):
                self.rewrite(records)
                result = self.inventory()
                self.assertAccounting(result)
                self.assertEqual(result["status"], "completed_with_problems")
                self.assertEqual(result["input_preflight_would_refuse"], self.first_pass_refusal())

    def test_same_market_id_in_different_plans_is_not_a_duplicate(self):
        self.inputs([(fx.path(M, plan=plan), fx.standard_market(M)) for plan in ("BASIC", "ADVANCED")])
        result = self.inventory()
        self.assertAccounting(result)
        self.assertEqual(result["status"], "completed", result)
        self.assertIsNone(result["input_preflight_would_refuse"])
        self.assertIsNone(self.first_pass_refusal())
        self.assertEqual(result["records"], {"lines": 2, "classified": 2, "with_problems": 0})
        self.assertEqual(result["records_by_plan"], {"BASIC": 1, "ADVANCED": 1})
        self.assertEqual(result["record_problems"]["duplicate_market"], 0)
        self.assertEqual(result["population"], {"by_class": {"eligible": 1, "excluded": 1},
                                               "by_reason": {"excluded": {"plan_not_contract_tier": 1}}})

    def test_source_refusal_labels_are_bounded_and_sanitised(self):
        self.inputs()
        self.rewrite(head_edit=lambda h: h.update(
            files=[{"status": "Weird Status CANARY", "reason": "CANARY file reason"},
                   {"status": "unreadable", "reason": "x" * 81}, {"status": "indexed", "reason": "CANARY"}],
            refused_markets=[{"market_id": "CANARY market id", "reason": "CANARY market reason"}]),
            manifest_edit=lambda m: m.update(refused=[{"path": "refused", "reason": "CANARY member reason"}]))
        result = self.inventory()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["source_refusals"], {
            "members_refused_at_ingest_by_reason": {"other": 1},
            "index_files_not_indexed_by_status_reason": {"other:other": 1, "unreadable:other": 1},
            "market_rows_by_reason": {"other": 1}, "distinct_markets_by_reason": {"other": 1}})
        self.assertNotIn("CANARY", canonical_json(result).decode())

    def test_limits_problem_precedes_record_diagnostics_but_inventory_consumes_them(self):
        self.inputs()
        self.rewrite([None, self.records[M]], manifest_edit=lambda m: m["limits"].update(max_members=1))
        result = self.inventory()
        self.assertAccounting(result)
        self.assertFalse(result["limits"]["manifest_limits_are_the_runs"])
        self.assertEqual(result["input_preflight_would_refuse"], f"input:{self.archive}:limits_not_the_runs")
        self.assertEqual(result["records"], {"lines": 2, "classified": 1, "with_problems": 1})


class RecordProblemTests(ReadinessCase):
    def test_market_id_sanitiser_is_exactly_the_index_grammar(self):
        kept = ["1.1", "2.100000001", "12.5", "123.123456789012345", "1.1234567890", "0.0", "000.000"]
        invalid = [1.5, 12, None, True, [], {}, "1.5\n", " 1.5", "1.5 ", "1.1234567890123456", "1234.5",
                   "\u0661.\u0665", "1.", ".5", "1.5.6", "CANARY"]
        for value, expected in [(value, value) for value in kept] + [(value, "invalid") for value in invalid]:
            with self.subTest(value=value):
                self.assertEqual(readiness._market_id({"market_id": value}), expected)

    def test_float_and_whitespace_market_ids_are_not_echoed_in_diagnostics(self):
        self.inputs()
        for value in (1.5, "1.5\n", " 1.5", "1.5 "):
            with self.subTest(value=value):
                bad = copy.deepcopy(self.records[M])
                bad["market_id"] = value
                bad["checks"]["path_month"] = "2026-01"
                self.rewrite([bad, self.records[M]])
                result = self.inventory()
                self.assertAccounting(result)
                stage = "malformed_record" if isinstance(value, float) else "stale_gate"
                suffix = "" if stage == "malformed_record" else ":invalid"
                self.assertEqual(result["record_problems_listed"], [["BASIC", "invalid", stage]])
                self.assertEqual(result["input_preflight_would_refuse"], f"input:{self.archive}:{stage}{suffix}")

    def test_non_one_market_id_diagnostic_codes_match_runner(self):
        mid = "2.100000001"
        self.inputs([(fx.path(mid), fx.standard_market(mid))])
        original = self.records[mid]
        for stage in ("stale_gate", "duplicate_market", "member_not_the_manifests"):
            broken = copy.deepcopy(original)
            if stage == "stale_gate":
                broken["checks"]["path_month"] = "2026-01"
            elif stage == "member_not_the_manifests":
                broken["provenance"]["member_sha256"] = "0" * 64
            records = [original, broken] if stage == "duplicate_market" else [broken]
            with self.subTest(stage=stage):
                self.rewrite(records)
                result = self.inventory()
                self.assertAccounting(result)
                expected = (f"cohort:{mid}:duplicate_market" if stage == "duplicate_market"
                            else f"input:{self.archive}:{stage}:{mid}")
                self.assertEqual(result["input_preflight_would_refuse"], expected)
                self.assertEqual(result["input_preflight_would_refuse"], self.first_pass_refusal())
                self.assertEqual(result["record_problems_listed"], [["BASIC", mid, stage]])

    def test_faulty_first_occurrence_then_same_identity_preserves_runner_seen_timing(self):
        self.inputs()
        original = self.records[M]
        for fault, stage, second_stage in (("shape", "malformed_record", None), ("gate", "stale_gate", None),
                                           ("classify", "malformed_record", "duplicate_market"),
                                           ("member", "member_not_the_manifests", "duplicate_market")):
            broken = copy.deepcopy(original)
            if fault == "shape":
                broken["decision_time"] = []
            elif fault == "gate":
                broken["checks"]["path_month"] = "2026-01"
            elif fault == "classify":
                del broken["decision_time"]["first_published_market_time"]
            else:
                broken["provenance"]["member_sha256"] = "0" * 64
            with self.subTest(fault=fault):
                self.rewrite([broken, original])
                result = self.inventory()
                self.assertAccounting(result)
                self.assertEqual(result["input_preflight_would_refuse"], self.first_pass_refusal())
                self.assertEqual(result["record_problems_listed"], [["BASIC", M, stage]] +
                                 ([["BASIC", M, second_stage]] if second_stage else []))
                self.assertEqual(result["records"]["classified"], 0 if second_stage else 1)
                self.assertEqual(result["prefix_candidates"], 0 if second_stage else 1)

    def test_each_records_first_problem_wins_in_runner_order(self):
        self.inputs()
        original = self.records[M]
        shape, gate, duplicate = (copy.deepcopy(original) for _ in range(3))
        shape["provenance"] = None
        shape["checks"]["path_month"] = "2026-01"
        gate["checks"]["path_month"] = "2026-01"
        del gate["decision_time"]["first_published_market_time"]
        del duplicate["decision_time"]["first_published_market_time"]
        self.rewrite([original, shape, gate, duplicate])
        result = self.inventory()
        self.assertAccounting(result)
        self.assertEqual(result["record_problems"], {"malformed_record": 1, "stale_gate": 1,
                                                    "duplicate_market": 1, "member_not_the_manifests": 0})
        self.assertEqual(result["records"]["classified"], 1)
        self.assertEqual(result["input_preflight_would_refuse"], self.first_pass_refusal())

    def test_non_object_provenance_and_other_malformed_records_are_counted_once(self):
        self.inputs()
        records = [None, [], "CANARY", 12, dict(self.records[M], provenance=None),
                   dict(self.records[M], provenance=[]), dict(self.records[M], provenance="CANARY")]
        self.rewrite(records + [self.records[M]])
        result = self.inventory()
        self.assertAccounting(result)
        self.assertEqual(result["record_problems"]["malformed_record"], len(records))
        self.assertEqual(result["population"]["by_class"], {"eligible": 1})
        self.assertNotIn("CANARY", canonical_json(result).decode())

    def test_problem_identifiers_are_sanitised_and_list_is_bounded(self):
        self.inputs()
        bad = copy.deepcopy(self.records[M])
        bad.update(market_id="1.123\nCANARY" + "9" * 300)
        bad["provenance"]["plan"] = {"CANARY": "dump"}
        self.rewrite([bad] * 105 + [self.records[M]])
        result = self.inventory()
        self.assertAccounting(result)
        self.assertEqual(result["record_problems"]["malformed_record"], 105)
        self.assertEqual(result["record_problems_listed"], [["invalid", "invalid", "malformed_record"]] * 100)
        self.assertNotIn("CANARY", canonical_json(result).decode())

    def test_first_refusal_does_not_echo_unsafe_market_id(self):
        self.inputs()
        bad = copy.deepcopy(self.records[M])
        bad["market_id"] = "CANARY\nobject"
        bad["checks"]["path_month"] = "2026-01"
        self.rewrite([bad])
        result = self.inventory()
        self.assertEqual(result["input_preflight_would_refuse"], f"input:{self.archive}:stale_gate:invalid")
        self.assertNotIn("CANARY", canonical_json(result).decode())

    def test_unknown_string_plan_is_sanitised_in_diagnostics_and_clean_counts(self):
        self.inputs()
        for faulty in (True, False):
            record = copy.deepcopy(self.records[M])
            record["provenance"]["plan"] = "CANARY unknown plan"
            if faulty:
                record["checks"]["path_month"] = "2026-01"
            else:
                record["decision_time"]["first_published_market_time"] = None
            with self.subTest(faulty=faulty):
                self.rewrite([record])
                result = self.inventory()
                self.assertAccounting(result)
                self.assertEqual(result["records_by_plan"], {} if faulty else {"invalid": 1})
                self.assertEqual(result["record_problems_listed"], [["invalid", M, "stale_gate"]] if faulty else [])
                self.assertEqual(result["population"]["by_class"], {} if faulty else {"no_view": 1})
                self.assertNotIn("CANARY", canonical_json(result).decode())

    def test_no_view_and_unknown_anchor_do_not_require_member_match(self):
        self.inputs()
        no_view, unknown = (copy.deepcopy(self.records[M]) for _ in range(2))
        no_view["decision_time"]["first_published_market_time"] = None
        unknown["market_id"] = "1.100000002"
        unknown["decision_time"]["first_published_market_time"]["pt"] = fx.ms(fx.OFF)
        for record in (no_view, unknown):
            record["provenance"]["member_sha256"] = "0" * 64
        self.rewrite([no_view, unknown])
        with mock.patch.object(readiness, "_member", side_effect=AssertionError("member checked")):
            result = self.inventory()
        self.assertAccounting(result)
        self.assertEqual(result["records"]["classified"], 2)
        self.assertEqual(result["prefix_candidates"], 0)
        self.assertEqual(result["input_preflight_would_refuse"], "inputs:no_eligible_records")


class FileFailureTests(ReadinessCase):
    def test_extra_archive_refused_before_head_or_markets_open(self):
        global _INPUT_OPEN_AUDIT
        self.inputs()
        extra_sha = "a" * 64
        (self.data / "manifests" / f"{extra_sha}.json").write_text("PRO CANARY")
        extra_index = self.data / "index" / extra_sha
        extra_index.mkdir()
        for name in (market_index.HEAD_FILE, market_index.MARKETS_FILE):
            (extra_index / name).write_text("PRO INDEX CANARY")
        opened, audited = [], []
        input_roots = (self.data / "index", self.data / "manifests")

        def guarded(open_fn):
            def open_without_inputs(path, *args, **kwargs):
                if not isinstance(path, int):
                    candidate = Path(os.fsdecode(path)).absolute()
                    if any(parent in candidate.parents for parent in input_roots):
                        opened.append(candidate)
                        raise AssertionError("opened archive input before archive-set check")
                return open_fn(path, *args, **kwargs)
            return open_without_inputs

        with ExitStack() as stack:
            for module, name in ((builtins, "open"), (io, "open"), (os, "open"), (Path, "open")):
                stack.enter_context(mock.patch.object(module, name, guarded(getattr(module, name))))
            stack.enter_context(mock.patch.object(runner, "_inputs", side_effect=AssertionError("opened input")))
            stack.enter_context(mock.patch.object(runner, "_regular_file", side_effect=AssertionError("inspected")))
            # Unlike attribute patches, the audit hook sees path-based opens through import-time aliases too.
            _INPUT_OPEN_AUDIT = (input_roots, audited)
            try:
                result = self.inventory()
            finally:
                _INPUT_OPEN_AUDIT = None
        self.assertRefused(result, "inputs:not_the_expected_archives")
        self.assertEqual(opened, [])
        self.assertEqual(audited, [])

    def test_wrong_or_absent_archive_refuses(self):
        self.inputs()
        self.assertRefused(self.inventory(expect_archive="a" * 64), "inputs:not_the_expected_archives")
        shutil.rmtree(self.data / "manifests")
        self.assertRefused(self.inventory(), "inputs:not_the_expected_archives")

    def test_expected_index_hash_checked_before_record_iteration(self):
        self.inputs()
        with mock.patch.object(runner, "_records", side_effect=AssertionError("iterated")):
            self.assertRefused(self.inventory(expect_index_sha256="0" * 64), "inputs:index_not_the_verified")

    def test_missing_head_and_markets_files_refuse(self):
        self.inputs()
        head = self.head_path.read_bytes()
        self.head_path.unlink()
        self.assertRefused(self.inventory(), f"input:{self.archive}:not_indexed")
        self.head_path.write_bytes(head)
        self.markets_path.unlink()
        self.assertRefused(self.inventory(), f"input:{self.archive}:missing_path")

    def test_nonregular_inputs_refuse_without_opening(self):
        self.inputs()
        paths = ((self.head_path, "head:unreadable"), (self.markets_path, "markets_unreadable"),
                 (self.data / "manifests" / f"{self.archive}.json", f"manifest:{self.archive}:unreadable"))
        for path, runner_code in paths:
            original = path.read_bytes()
            for kind in ("directory", "fifo", "symlink"):
                with self.subTest(path=path.name, kind=kind):
                    path.unlink()
                    if kind == "directory":
                        path.mkdir()
                    elif kind == "fifo":
                        os.mkfifo(path)
                    else:
                        path.symlink_to(self.contract_path)
                    with mock.patch.object(runner, "_inputs", side_effect=AssertionError("opened inputs")):
                        code = "unsafe_target" if kind == "symlink" else "not_a_regular_file"
                        self.assertRefused(self.inventory(), f"input:{self.archive}:{code}")
                    if kind == "directory":
                        # Even one non-regular file gets a different runner diagnostic. Never give it a FIFO.
                        with self.assertRaises(runner.RunRefused) as caught:
                            runner._inputs(self.data)
                        self.assertEqual(str(caught.exception), f"input:{self.archive}:{runner_code}")
                    path.rmdir() if kind == "directory" else path.unlink()
                    path.write_bytes(original)

    def test_hash_and_line_count_mismatches_refuse_without_counts(self):
        self.inputs()
        for field, value in (("markets_sha256", "0" * 64), ("markets", 2)):
            with self.subTest(field=field):
                self.rewrite([self.records[M]], head_edit=lambda h: h.update({field: value}))
                self.assertRefused(self.inventory(), f"input:{self.archive}:markets_mismatch")

    def test_unterminated_last_record_refuses_inventory_count_invariant_only(self):
        self.inputs()
        self.rewrite(data=canonical_json(self.records[M]))
        archives = runner._inputs(self.data)[0]
        count, eligible, _ = runner._first_pass(archives, cohort.CohortRules.from_contract(self.contract_value))
        self.assertEqual((count, eligible), (1, 1))
        self.assertEqual(archives[0]["head"]["markets"], 0)
        self.assertRefused(self.inventory(), f"input:{self.archive}:records_count_mismatch")

    def test_late_parse_errors_discard_earlier_record_counts(self):
        self.inputs()
        good = canonical_json(self.records[M]) + b"\n"
        for tail in (b"{not-json}\n", b'{"x":NaN}\n', b"\xff\n"):
            with self.subTest(tail=tail):
                self.rewrite(data=good + b"null\n" + tail)
                self.assertRefused(self.inventory(), f"input:{self.archive}:markets_unreadable")

    def test_duplicate_json_keys_follow_the_runners_last_value_parser(self):
        self.inputs()
        data = b'{"market_id":"CANARY",' + canonical_json(self.records[M])[1:] + b"\n"
        self.rewrite(data=data)
        result = self.inventory()
        self.assertAccounting(result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["input_preflight_would_refuse"], self.first_pass_refusal())
        self.assertNotIn("CANARY", canonical_json(result).decode())

    def test_oversize_record_refuses_without_partial_counts(self):
        self.inputs()
        good = canonical_json(self.records[M]) + b"\n"
        self.rewrite(data=good + b" " * (len(good) + 2) + b"\n")
        with mock.patch.object(runner, "MAX_RECORD_BYTES", len(good)):
            self.assertRefused(self.inventory(), f"input:{self.archive}:record_too_large")

    def test_changed_stream_after_input_binding_discards_diagnostics(self):
        self.inputs()
        original_records = runner._records

        def changed(archive):
            self.markets_path.chmod(0o600)
            self.markets_path.write_bytes(canonical_json(self.records[M]) + b"\nnull\n")
            yield from original_records(archive)

        with mock.patch.object(runner, "_records", changed):
            self.assertRefused(self.inventory(), f"input:{self.archive}:markets_changed")

    def test_bad_head_and_manifest_refuse_with_known_codes(self):
        self.inputs()
        original = self.head_path.read_bytes()
        for data, code in ((b"{", "head_unreadable"), (b"{}", "head_not_canonical"),
                            (b"{}\n", "head_mismatch")):
            with self.subTest(code=code):
                self.head_path.chmod(0o600)
                self.head_path.write_bytes(data)
                self.assertRefused(self.inventory(), f"input:{self.archive}:{code}")
        self.head_path.write_bytes(original)
        manifest = self.data / "manifests" / f"{self.archive}.json"
        manifest.chmod(0o600)
        manifest.write_bytes(b"{}\n")
        self.assertRefused(self.inventory(), f"input:{self.archive}:manifest:{self.archive}:malformed")

    def test_unexpected_exception_is_type_only_and_known_code_translation_is_closed(self):
        self.inputs()
        for error, code in ((RuntimeError("CANARY secret"), "internal:RuntimeError"),
                            (runner.RunRefused("CANARY secret"), "inputs:unrecognised_problem")):
            with self.subTest(code=code), mock.patch.object(runner, "_inputs", side_effect=error):
                self.assertRefused(self.inventory(), code)

    def test_certificate_source_problem_does_not_echo_paths_or_exception_text(self):
        self.inputs()
        error = certificate.CertificateProblem("source:betangel_research/CANARY.py:symlink")
        with mock.patch.object(certificate, "current_key", side_effect=error):
            self.assertRefused(self.inventory(), "source:unavailable")


class ContractTests(ReadinessCase):
    def test_invalid_frozen_unsupported_and_nonzero_delay_contracts_refuse(self):
        self.inputs()
        original = copy.deepcopy(self.contract_value)
        cases = ((lambda c: c.update(status="frozen", frozen_on="2026-10-16"), "contract:not_a_draft"),
                 (lambda c: c.update(status="bogus"), "contract:not_valid_draft"),
                 (lambda c: c["decision_timing"].update(delay_s=1), "contract:not_supported:delay_s_other_than_0"),
                 (lambda c: c["decision_timing"].update(delay_s=None),
                  "contract:not_supported:decision_timing:delay_s_null"),
                 (lambda c: c["population"].update(countries=["GB"]),
                  "contract:not_supported:not_the_certified_profile"),
                 (lambda c: c["costs"].update(commission_rate=0.1234567890123),
                  "contract:not_supported:commission_rate_too_precise"))
        for edit, code in cases:
            with self.subTest(code=code):
                self.contract_value = copy.deepcopy(original)
                edit(self.contract_value)
                self.write_contract()
                with mock.patch.object(readiness, "_inputs", side_effect=AssertionError("read inputs")):
                    self.assertRefused(self.inventory(), code)

    def test_bad_contract_bytes_and_identifiers_are_bounded(self):
        self.inputs()
        self.contract_path.write_bytes(b"{CANARY}")
        self.assertRefused(self.inventory(), "contract:not_json")
        self.write_contract()
        for option, value, code in (("expect_archive", "bad", "expect_archive:malformed_sha256"),
                                    ("expect_index_sha256", "bad", "expect_index_sha256:malformed_sha256"),
                                    ("code_identity", "line\nbreak", "code_identity:malformed")):
            with self.subTest(option=option):
                self.assertRefused(self.inventory(**{option: value}), code)

    def test_path_guard_denies_live_paths_before_any_read(self):
        self.inputs()
        denied = self.tmp / "Library" / "Application Support" / "betangel-research" / "betfair" / "canary"
        for option, where in (("contract_path", "contract"), ("data_root", "data_root")):
            with self.subTest(option=option), mock.patch.object(readiness, "load_contract",
                                                               side_effect=AssertionError("read contract")):
                self.assertRefused(self.inventory(**{option: denied}), f"{where}:denied_live_state_tree")


class NoninterferenceTests(ReadinessCase):
    def test_non_gate_outcome_canaries_change_hashes_but_no_counts_codes_or_values(self):
        self.inputs()
        before = self.inventory()
        changed = copy.deepcopy(self.records[M])
        canary = "OUTCOME_CANARY_8f90"
        outcome = changed["evaluator_only_outcome"]
        for field in list(outcome):
            if field != "market_time_months":
                outcome[field] = {canary: [987654321.125, canary]}
        changed["coverage"] = {canary: 987654321.125}
        changed["provenance"]["other_sources"] = [{"agrees": canary, "agrees_decision_time": canary}]
        changed["decision_time"]["view_ends"] = {"pt": fx.ms(fx.OFF - timedelta(hours=4)), "reason": canary}
        changed["decision_time"]["definition"]["status"] = [[0, canary]]
        changed["checks"]["month_mismatch"] = canary
        self.rewrite([changed])
        after = self.inventory()
        self.assertNotEqual(before["input_sha256"], after["input_sha256"])
        for field in ("index_sha256", "markets_sha256"):
            self.assertNotEqual(before["input"]["archives"][0][field], after["input"]["archives"][0][field])
        self.assertEqual({k: v for k, v in before.items() if k not in ("input", "input_sha256")},
                         {k: v for k, v in after.items() if k not in ("input", "input_sha256")})
        encoded = canonical_json(after).decode()
        self.assertNotIn(canary, encoded)
        self.assertNotIn("987654321", encoded)

        def no_floats(value):
            self.assertNotIsInstance(value, float)
            for item in value.values() if isinstance(value, dict) else value if isinstance(value, list) else []:
                no_floats(item)

        no_floats(after)

    def test_semantic_outcome_canaries_leave_classification_and_codes_unchanged(self):
        self.inputs()
        before = self.inventory()
        original = self.records[M]
        flipped = copy.deepcopy(original["evaluator_only_outcome"]["final_runners"])
        for item in flipped:
            item["status"] = "LOSER" if item["status"] == "WINNER" else "WINNER"
            item["bsp"] = None
        cases = ({"dead_heat": True}, {"final_status": "VOID"}, {"final_runners": flipped},
                 {"results_revised_after_close": True}, {"missing_final_bsp_runner_ids": [987654321]},
                 {"winners_observed": 0})
        for edit in cases:
            with self.subTest(fields=list(edit)):
                changed = copy.deepcopy(original)
                changed["evaluator_only_outcome"].update(edit)
                self.rewrite([changed])
                after = self.inventory()
                self.assertNotEqual(before["input_sha256"], after["input_sha256"])
                for field in ("index_sha256", "markets_sha256"):
                    self.assertNotEqual(before["input"]["archives"][0][field], after["input"]["archives"][0][field])
                self.assertEqual({k: v for k, v in before.items() if k not in ("input", "input_sha256")},
                                 {k: v for k, v in after.items() if k not in ("input", "input_sha256")})
                self.assertNotIn("987654321", canonical_json(after).decode())

    def test_gate_and_provenance_mutations_are_separate_noninvariant_controls(self):
        self.inputs()
        original = self.records[M]
        for field, code in (("gate", "stale_gate"), ("provenance", "member_not_the_manifests")):
            changed = copy.deepcopy(original)
            if field == "gate":
                changed["evaluator_only_outcome"]["market_time_months"].append("2026-01")
            else:
                changed["provenance"]["event_id_from_path"] = "another"
            with self.subTest(field=field):
                self.rewrite([changed])
                result = self.inventory()
                self.assertEqual(result["record_problems"][code], 1)
                self.assertEqual(result["records"]["classified"], 0)
                self.assertEqual(result["input_preflight_would_refuse"], self.first_pass_refusal())

    def test_tracking_dict_pins_every_record_field_access(self):
        self.inputs()
        accesses = set()

        class Tracked(dict):
            def __init__(self, value, prefix=()):
                super().__init__((key, Tracked(item, (*prefix, key)) if isinstance(item, dict) else item)
                                 for key, item in value.items())
                self.prefix = prefix

            def __getitem__(self, key):
                accesses.add((*self.prefix, key))
                return super().__getitem__(key)

            def get(self, key, default=None):
                accesses.add((*self.prefix, key))
                return super().get(key, default)

        def bulk_read(name):
            def tracked(value, *args, **kwargs):
                accesses.add((*value.prefix, f"<{name}>"))
                return getattr(dict, name)(value, *args, **kwargs)
            return tracked

        # Overriding __iter__ also disables dict(subclass)'s direct-copy shortcut. JSON may use items or len.
        for name in ("values", "items", "keys", "__iter__", "__contains__", "__len__", "copy", "__repr__",
                     "__eq__", "__ne__", "__reversed__", "__or__", "__ror__", "__reduce_ex__",
                     "setdefault", "pop", "popitem", "update"):
            setattr(Tracked, name, bulk_read(name))

        allowed = {("market_id",), ("provenance",), ("decision_time",), ("evaluator_only_outcome",), ("checks",),
                   ("checks", "path_month"), ("evaluator_only_outcome", "market_time_months"),
                   ("decision_time", "first_published_market_time"), ("decision_time", "definition"),
                   ("decision_time", "first_published_market_time", "value"),
                   ("decision_time", "first_published_market_time", "pt")}
        allowed |= {("provenance", key) for key in ("archive_sha256", "plan", "member_path", "member_sha256",
                                                    "path_month", "path_date", "event_id_from_path", "file_type",
                                                    "file_market_id")}
        allowed |= {("decision_time", "definition", key) for key in ("eventTypeId", "countryCode", "marketType")}
        probes = {"values": lambda d: list(d.values()), "items": lambda d: list(d.items()),
                  "keys": lambda d: list(d.keys()), "iteration": lambda d: list(iter(d)),
                  "in": lambda d: "market_id" in d, "len": len, "copy": lambda d: d.copy(), "repr": repr,
                  "dict": dict, "json": json.dumps, "canonical_json": canonical_json,
                  "eq": lambda d: d == {}, "ne": lambda d: d != {}, "reversed": lambda d: list(reversed(d)),
                  "or": lambda d: d | {}, "ror": lambda d: {} | d, "reduce_ex": lambda d: d.__reduce_ex__(4),
                  "setdefault": lambda d: d.setdefault("market_id", None), "pop": lambda d: d.pop("market_id"),
                  "popitem": lambda d: d.popitem(), "update": lambda d: d.update({})}
        for name, probe in probes.items():
            with self.subTest(bulk_read=name):
                accesses.clear()
                probe(Tracked(self.records[M]))
                self.assertTrue(any(path[-1].startswith("<") for path in accesses), accesses)
        accesses.clear()
        record = Tracked(self.records[M])
        with mock.patch.object(runner, "_records", return_value=iter([record])):
            result = self.inventory()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(accesses, allowed)

    def test_inventory_needs_no_raw_data_and_invokes_no_evaluation_or_writes(self):
        self.inputs()
        before = self.inventory()
        shutil.rmtree(self.data / "raw")
        for tar in self.tmp.glob("*.tar"):
            tar.unlink()
        snapshots = {str(p): p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()}
        targets = ((replay, "replay_view"), (runner, "replay_view"), (cohort, "decide_market"),
                   (runner, "decide_market"), (engineering, "check_prefix"), (runner, "check_prefix"),
                   (accounting, "settle"), (runner, "settle"), (metrics, "HashStream"),
                   (metrics, "block_bootstrap"), (runner, "block_bootstrap"), (trial_ledger, "TrialLedger"),
                   (runner, "TrialLedger"), (runner, "_Lock"), (certificate, "load_certificate"),
                   (runner, "load_certificate"), (bz2, "BZ2File"), (bz2, "open"), (tarfile, "open"),
                   (runner, "known_accounting_answers"), (runner, "_History"), (runner, "_stop_evidence"))
        real_open, real_path_open = builtins.open, Path.open

        def read_only(open_fn):
            def guarded(path, mode="r", *args, **kwargs):
                self.assertNotRegex(mode, r"[wax+]")
                self.assertNotIn("raw", Path(path).parts)
                self.assertNotEqual(Path(path).suffix, ".tar")
                return open_fn(path, mode, *args, **kwargs)
            return guarded

        with ExitStack() as stack:
            for module, name in targets:
                stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
            for name in ("mkdir", "unlink", "rename", "replace", "write_bytes", "write_text", "touch", "chmod"):
                stack.enter_context(mock.patch.object(Path, name, side_effect=AssertionError(name)))
            stack.enter_context(mock.patch("builtins.open", read_only(real_open)))
            stack.enter_context(mock.patch.object(Path, "open", read_only(real_path_open)))
            self.assertEqual(self.inventory(), before)
        self.assertEqual({str(p): p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()}, snapshots)

    def test_read_only_files_and_directories_are_unchanged(self):
        self.inputs()
        expected = self.inventory()
        paths = [self.tmp, *self.tmp.rglob("*")]
        modes = {p: p.stat().st_mode & 0o777 for p in paths}
        files = {p: p.read_bytes() for p in paths if p.is_file()}
        try:
            for p in paths:
                p.chmod(0o500 if p.is_dir() else 0o400)
            self.assertEqual(self.inventory(), expected)
            self.assertEqual({p: p.read_bytes() for p in files}, files)
            self.assertEqual(set(self.tmp.rglob("*")), set(paths) - {self.tmp})
        finally:
            for p, mode in modes.items():
                p.chmod(mode)


class CliTests(ReadinessCase):
    def test_pinned_options_canonical_output_and_three_exit_codes(self):
        self.inputs()
        for records, status, code in (([self.records[M]], "completed", 0),
                                       ([None, self.records[M]], "completed_with_problems", 3)):
            self.rewrite(records)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(readiness.main(self.argv()), code)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], status)
            self.assertEqual(output.getvalue().encode(), canonical_json(result) + b"\n")
        self.index_sha = "0" * 64
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(readiness.main(self.argv()), 2)
        self.assertRefused(json.loads(output.getvalue()), "inputs:index_not_the_verified")
        tree = ast.parse(Path(readiness.__file__).read_bytes())
        options = {node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"}
        self.assertEqual(options, {"--contract", "--data-root", "--expect-archive", "--expect-index-sha256",
                                   "--code-identity"})

    def test_own_python_m_entry_point(self):
        self.inputs()
        result = subprocess.run([sys.executable, "-B", "-m", "betangel_research.betfair.readiness", *self.argv()],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(result.stdout), self.inventory())


if __name__ == "__main__":
    unittest.main()
