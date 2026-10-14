"""Backtest experiment-contract tests: the draft P0 contract, synthetic edits of it, file loading and the CLI.

These show that the validator refuses what schema_version 1 refuses and that the sha256 depends only on the
JSON value. They do not show that a contract was written before outcomes were seen, or that its corpus exists.
"""

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair import receipts
from betangel_research.betfair.backtest import contract as contract_module
from betangel_research.betfair.backtest.__main__ import main
from betangel_research.betfair.backtest.contract import DRAFT_NULLABLE, check_contract_file, validate_contract

import test_betfair_support as support

REPO = Path(__file__).resolve().parent.parent
DRAFT = REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json"
RETROSPECTIVE = ("actual_off", "actual_off_time", "last_pre_off_update", "last_update_before_off", "market_close",
                 "market_closed", "in_play_transition", "turn_in_play", "inplay_start", "suspend_time", "result_time",
                 "settled_time", "bsp_reconciliation", "final_market_definition", "latest_scheduled_off",
                 "revised_scheduled_off", "winner_declared", "Actual_Off", "post_time", "reconciliation_time")
# Network and process modules; urllib.parse is pure parsing (and pathlib imports it on Python 3.10-3.12).
LOADED_MODULES_SCRIPT = """
import json
import sys
sys.path.insert(0, sys.argv[1])
from betangel_research.betfair.backtest.__main__ import main
code = main(["validate-contract", "--contract", sys.argv[2]])
network = ("socket", "ssl", "http", "subprocess", "asyncio", "multiprocessing")
sys.stderr.write(json.dumps([code, sorted(m for m in sys.modules if m.split(".")[0] == "betangel_research"),
                             sorted(m for m in sys.modules if m.split(".")[0] in network
                                    or m in ("urllib.request", "urllib.response"))]))
"""
TEXT_LEAVES = ("registration.note", "hypothesis", "cohort.market_rule", "cohort.runner_rule",
               "features[0].definition", "features[1].definition", "costs.note", "primary_metric.decision_rule",
               "variants[0].description", "variants[1].description", "stopping_rule.rule")


def draft():
    return json.loads(DRAFT.read_text(encoding="utf-8"))


def frozen():
    """The draft with every draft-nullable field filled and status frozen (synthetic values)."""
    value = draft()
    value.update(status="frozen", frozen_on="2026-10-14", hypothesis="Synthetic: the pipeline reproduces hand totals.")
    value["decision_timing"]["delay_s"] = 1
    value["primary_metric"].update(decision_threshold=0.0, decision_rule="Synthetic rule for the test only.")
    value["stopping_rule"]["max_trials"] = 4
    return value


def reorder(value):
    """The same JSON value with every object's keys in reverse order."""
    if isinstance(value, dict):
        return {key: reorder(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reorder(item) for item in value]
    return value


def object_paths(value, path=""):
    """(dotted path, object) for every object reachable through objects and lists, the root included."""
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from object_paths(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from object_paths(child, f"{path}[{index}]")


def leaf_paths(value):
    """Every key path below an object, list items included (features[0].name), excluding the root."""
    return [f"{path}.{key}" if path else key for path, obj in object_paths(value) for key in obj]


def _parts(path):
    return [int(part) if part.isdigit() else part for part in re.split(r"\.|\[|\]\.?", path) if part != ""]


def get_path(value, path):
    for part in _parts(path):
        value = value[part]
    return value


def set_path(value, path, new):
    *parents, last = _parts(path)
    for part in parents:
        value = value[part]
    value[last] = new


def del_path(value, path):
    *parents, last = _parts(path)
    for part in parents:
        value = value[part]
    del value[last]


def problems(value):
    return validate_contract(value)[0]


def edited(base, **paths):
    """base() with each dotted path (double underscore for a dot, as keyword names cannot hold one) replaced."""
    value = base()
    for path, new in paths.items():
        set_path(value, path.replace("__", "."), new)
    return value


class ContractTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_repo_draft_is_a_valid_engineering_draft(self):
        value = draft()
        found, digest = validate_contract(value)
        self.assertEqual(found, [])
        self.assertEqual(contract_module.result_status(value, found), "valid_draft")
        self.assertRegex(digest, r"\A[0-9a-f]{64}\Z")
        self.assertEqual((value["status"], value["registration"]["kind"]), ("draft", "engineering_check"))
        self.assertIn("not a registered hypothesis", value["registration"]["note"])
        nulls = sorted(path for path in ("frozen_on", *DRAFT_NULLABLE) if get_path(value, path) is None)
        self.assertEqual(nulls, sorted(("frozen_on", *DRAFT_NULLABLE)))
        population = value["population"]
        self.assertEqual((population["tier"], population["sport"], population["countries"], population["market_types"]),
                         ("BASIC", "horse_racing", ["GB", "IE"], ["WIN"]))
        self.assertEqual(population["splits"], {
            "engineering": {"start_month": "2015-05", "end_month": "2016-04"},
            "research": {"start_month": "2023-01", "end_month": "2025-12"},
            "holdout": {"start_month": "2026-01", "end_month": "2026-09", "state": "sealed_not_downloaded"}})
        self.assertEqual(population["evaluate_split"], "engineering")
        self.assertEqual(value["fitting"], {"kind": "none"})
        self.assertEqual(set(value["settlement"].values()), {"unresolved"})
        self.assertEqual([(f["name"], f["as_of"], f["lookback_s"]) for f in value["features"]],
                         [("active_runner_count", "decision_time", 0), ("ltp_at_decision", "decision_time", 0)])
        self.assertEqual([v["uses_features"] for v in value["variants"]], [["ltp_at_decision"], []])

    def test_only_draft_nullable_fields_may_be_null_in_a_draft(self):
        paths = leaf_paths(draft())
        self.assertGreater(len(paths), 60)
        for path in paths:
            if path == "frozen_on" or path in DRAFT_NULLABLE:
                continue
            value = draft()
            set_path(value, path, None)
            with self.subTest(path=path):
                self.assertIn(f"{path}:null", problems(value))

    def test_frozen_contract_needs_every_field_non_null(self):
        value = frozen()
        found, _ = validate_contract(value)
        self.assertEqual(found, [])
        self.assertEqual(contract_module.result_status(value, found), "valid_frozen")
        for path in DRAFT_NULLABLE:
            value = frozen()
            set_path(value, path, None)
            with self.subTest(path=path):
                self.assertEqual(problems(value), [f"{path}:null_in_frozen"])
        value = frozen()
        value["frozen_on"] = None
        self.assertEqual(problems(value), ["frozen_on:null_in_frozen"])
        for bad in ("2026-02-30", "2026-10-14T00:00:00Z", "20261014", 20261014):
            value = frozen()
            value["frozen_on"] = bad
            with self.subTest(frozen_on=bad):
                self.assertEqual(problems(value), ["frozen_on:malformed"])
        # Earlier than schema v1 is refused; a future date cannot be checked without a clock.
        for bad in ("2026-10-13", "0001-01-01"):
            with self.subTest(frozen_on=bad):
                self.assertEqual(problems(edited(frozen, frozen_on=bad)), ["frozen_on:before_schema_v1"])
        self.assertEqual(problems(edited(frozen, frozen_on="9999-12-31")), [])
        value = draft()
        value["frozen_on"] = "2026-10-14"
        self.assertEqual(problems(value), ["frozen_on:not_null_in_draft"])
        # An unknown status is judged as frozen (strictest), and is itself a problem.
        value = draft()
        value["status"] = "registered"
        self.assertIn("status:not_allowed", problems(value))
        self.assertIn("hypothesis:null_in_frozen", problems(value))

    def test_frozen_text_must_not_be_a_placeholder(self):
        value = frozen()
        value.update(hypothesis="TBD")
        value["cohort"].update(market_rule="-", runner_rule="TBD", exclusions=[])
        value["features"][0]["definition"] = "x"
        value["stopping_rule"]["rule"] = "?"
        value["costs"]["note"] = "."
        value["registration"]["note"] = "n/a"
        value["variants"][0]["description"] = "TODO: describe"
        value["primary_metric"]["decision_rule"] = "Lorem ipsum dolor sit amet, placeholder."
        self.assertEqual(problems(value), sorted([
            "hypothesis:placeholder_in_frozen", "cohort.market_rule:placeholder_in_frozen",
            "cohort.runner_rule:placeholder_in_frozen", "cohort.exclusions:empty_in_frozen",
            "features[0].definition:placeholder_in_frozen", "stopping_rule.rule:placeholder_in_frozen",
            "costs.note:placeholder_in_frozen", "registration.note:placeholder_in_frozen",
            "variants[0].description:placeholder_in_frozen", "primary_metric.decision_rule:placeholder_in_frozen"]))
        for path in ("hypothesis", "cohort.market_rule", "cohort.runner_rule", "features[1].definition",
                     "primary_metric.decision_rule", "variants[1].description", "stopping_rule.rule"):
            value = frozen()
            set_path(value, path, "Too short a rule.")
            with self.subTest(path=path):
                self.assertEqual(problems(value), [f"{path}:too_short_in_frozen"])
        value = frozen()
        value["cohort"]["exclusions"] = ["TBD"]
        self.assertEqual(problems(value), ["cohort.exclusions[0]:placeholder_in_frozen"])
        # A draft may hold unfinished text; the freeze is what binds it.
        value = draft()
        value["cohort"].update(market_rule="TBD", exclusions=[])
        self.assertEqual(problems(value), [])
        for threshold, expected in ((-5.0, ["primary_metric.decision_threshold:out_of_range"]), (-1.0, []),
                                    (998.5, []), (999.0, ["primary_metric.decision_threshold:out_of_range"])):
            value = frozen()
            value["primary_metric"]["decision_threshold"] = threshold
            with self.subTest(threshold=threshold):
                self.assertEqual(problems(value), expected)

    def test_unknown_and_missing_keys_refused_at_every_level(self):
        for path, _ in list(object_paths(draft())):
            value = draft()
            target = get_path(value, path) if path else value
            target["extra_key"] = 1
            with self.subTest(path=path or "<root>"):
                self.assertEqual(problems(value), [f"{path}.extra_key:unknown_key" if path
                                                   else "extra_key:unknown_key"])
        for key in contract_module.TOP_LEVEL_KEYS:
            value = draft()
            del value[key]
            with self.subTest(missing=key):
                self.assertIn(f"{key}:missing", problems(value))
        nested = [path for path in leaf_paths(draft()) if "." in path or "[" in path]
        self.assertGreater(len(nested), 50)
        for path in nested:
            value = draft()
            del_path(value, path)
            with self.subTest(missing=path):
                self.assertIn(f"{path}:missing", problems(value))
        value = draft()
        value["staking"]["<script>alert(1)</script>"] = 1
        self.assertEqual(problems(value), ["staking.<malformed_key>:unknown_key"])
        self.assertEqual(problems([draft()]), ["contract:not_an_object"])

    def test_text_fields_and_names_are_checked(self):
        for path in TEXT_LEAVES:
            for bad in (7, "", "   ", ["text"]):
                value = draft()
                set_path(value, path, bad)
                with self.subTest(path=path, value=bad):
                    self.assertEqual(problems(value), [f"{path}:not_text"])
        cases = [
            ("cohort.exclusions", "x", "cohort.exclusions:not_a_list"),
            ("cohort.exclusions", [""], "cohort.exclusions[0]:not_text"),
            ("cohort.exclusions", ["ok", 3], "cohort.exclusions[1]:not_text"),
            ("variants[0].role", "oracle", "variants[0].role:not_allowed"),
            ("variants[1].id", "X", "variants[1].id:malformed"),
            ("features[1].name", "Bad Name", "features[1].name:malformed"),
            ("staking.market_impact", "modelled", "staking.market_impact:not_allowed"),
            ("costs.basis", "gross_winnings", "costs.basis:not_allowed"),
            ("costs.rate_source", "guess", "costs.rate_source:not_allowed"),
            ("status", "registered", "status:not_allowed"),
            ("seeds.selection", "1", "seeds.selection:not_integer"),
        ]
        for path, bad, expected in cases:
            value = draft()
            set_path(value, path, bad)
            with self.subTest(path=path, value=bad):
                self.assertIn(expected, problems(value))

    def test_decision_timing_rejects_retrospective_anchors(self):
        for anchor in RETROSPECTIVE:
            value = draft()
            value["decision_timing"]["anchor"] = anchor
            with self.subTest(anchor=anchor):
                self.assertEqual(problems(value), ["decision_timing.anchor:retrospective"])
        for marker in contract_module.RETROSPECTIVE_MARKERS:
            value = draft()
            value["decision_timing"]["anchor"] = f"x_{marker}_y"
            with self.subTest(marker=marker):
                self.assertEqual(problems(value), ["decision_timing.anchor:retrospective"])
        for anchor in ("scheduled_off", "first_published_scheduled_off ", "market_time", "in-play-start", 0):
            value = draft()
            value["decision_timing"]["anchor"] = anchor
            with self.subTest(anchor=anchor):
                self.assertEqual(problems(value), ["decision_timing.anchor:not_allowed"])
        for offset, reason in ((0, "not_negative"), (60, "not_negative"), (-86401, "out_of_range"),
                               (-300.0, "not_integer"), (True, "not_integer"), (10 ** 400, "not_negative")):
            value = draft()
            value["decision_timing"]["offset_s"] = offset
            with self.subTest(offset=offset):
                self.assertEqual(problems(value), [f"decision_timing.offset_s:{reason}"])
        for offset in (-1, -86400):
            value = draft()
            value["decision_timing"]["offset_s"] = offset
            self.assertEqual(problems(value), [])
        value = draft()
        value["decision_timing"]["view"] = "full_market_history"
        self.assertEqual(problems(value), ["decision_timing.view:not_allowed"])

    def test_delay_equal_pt_order_and_image_handling(self):
        for delay, expected in ((0, []), (299, []), (300, ["decision_timing.delay_s:not_before_scheduled_off"]),
                                (-1, ["decision_timing.delay_s:out_of_range"]), (3600, ["decision_timing.delay_s:"
                                                                                       "not_before_scheduled_off"]),
                                (3601, ["decision_timing.delay_s:out_of_range"]),
                                (1.0, ["decision_timing.delay_s:not_integer"])):
            value = frozen()
            value["decision_timing"]["delay_s"] = delay
            with self.subTest(delay=delay):
                self.assertEqual(problems(value), expected)
        value = frozen()
        value["decision_timing"].update(offset_s=-3601, delay_s=3600)
        self.assertEqual(problems(value), [])
        for key, bad in (("equal_pt_order", "timer_before_equal_pt_batch"), ("image_handling", "merge_img")):
            value = draft()
            value["decision_timing"][key] = bad
            with self.subTest(key=key):
                self.assertEqual(problems(value), [f"decision_timing.{key}:not_allowed"])

    def test_capabilities_and_features_keep_outcomes_out_of_strategy_inputs(self):
        def feature(name, capability, **extra):
            return {"name": name, "capability": capability, "as_of": "decision_time", "lookback_s": 0,
                    "definition": "synthetic feature definition", **extra}

        for capability in ("final_bsp", "results"):
            value = draft()
            value["features"].append(feature("outcome_leak", capability))
            with self.subTest(capability=capability):
                self.assertEqual(problems(value), ["features[2].capability:outcome_not_a_feature"])
        value = draft()
        value["features"].append(feature("spread_depth", "best_offers"))
        self.assertEqual(problems(value), ["features[2].capability:not_required"])
        value = draft()
        value["features"].append(dict(value["features"][0]))
        self.assertEqual(problems(value), ["features[2].name:duplicate"])
        value = draft()
        value["features"].reverse()
        self.assertEqual(problems(value), ["features:not_sorted_by_name"])
        for key, bad, reason in (("as_of", "actual_off", "not_allowed"), ("as_of", "final", "not_allowed"),
                                 ("lookback_s", -1, "out_of_range"), ("lookback_s", 86401, "out_of_range"),
                                 ("lookback_s", 60.0, "not_integer")):
            value = draft()
            value["features"][1][key] = bad
            with self.subTest(key=key, value=bad):
                self.assertEqual(problems(value), [f"features[1].{key}:{reason}"])
        value = draft()
        value["features"][1]["lookback_s"] = 86400
        self.assertEqual(problems(value), [])
        value = draft()
        value["capabilities"]["required"] = ["ltp", "market_definition"]
        self.assertEqual(problems(value), ["capabilities.required:missing_final_bsp",
                                           "capabilities.required:missing_results"])
        value = draft()
        value["capabilities"]["required"] = ["final_bsp", "ltp", "results"]
        self.assertEqual(problems(value), ["capabilities.required:missing_market_definition",
                                           "features[0].capability:not_required"])
        value = draft()
        value["capabilities"]["required"] = ["market_definition", "ltp", "final_bsp", "results"]
        self.assertEqual(problems(value), ["capabilities.required:not_sorted_unique"])
        value = draft()
        value["capabilities"]["on_missing"] = "zero"
        self.assertEqual(problems(value), ["capabilities.on_missing:not_allowed"])
        value = draft()
        value["features"] = []
        for variant in value["variants"]:
            variant["uses_features"] = []
        self.assertEqual(problems(value), [])

    def test_variants_see_only_declared_features_and_nothing_is_fitted(self):
        for uses, reason in ((["final_bsp"], "undeclared_feature"), (["winner_known"], "undeclared_feature"),
                             (["Bad"], "not_allowed"), ("ltp_at_decision", "not_a_list"),
                             (["ltp_at_decision", "active_runner_count"], "not_sorted_unique")):
            value = draft()
            value["variants"][0]["uses_features"] = uses
            with self.subTest(uses=uses):
                self.assertEqual(problems(value), [f"variants[0].uses_features:{reason}"])
        value = draft()
        value["variants"].reverse()
        self.assertEqual(problems(value), ["variants:not_sorted_by_id"])
        for kind, reason in (("walk_forward", "deferred"), ("in_sample", "not_allowed")):
            value = draft()
            value["fitting"]["kind"] = kind
            with self.subTest(kind=kind):
                self.assertEqual(problems(value), [f"fitting.kind:{reason}"])

    def test_settlement_cases_are_unresolved_in_v1(self):
        for case in contract_module.SETTLEMENT_CASES:
            value = draft()
            value["settlement"][case] = "settle_at_bsp"
            with self.subTest(case=case):
                self.assertEqual(problems(value), [f"settlement.{case}:not_allowed"])

    def test_p0_staking_costs_and_metrics(self):
        cases = [
            (("staking", "side"), "LAY", "staking.side:deferred"),
            (("staking", "price"), "LIMIT_ON_CLOSE", "staking.price:deferred"),
            (("staking", "price"), "LIMIT", "staking.price:deferred"),
            (("staking", "acceptance"), "conditional", "staking.acceptance:not_allowed"),
            (("staking", "stake_pence"), 0, "staking.stake_pence:out_of_range"),
            (("staking", "stake_pence"), 100001, "staking.stake_pence:out_of_range"),
            (("staking", "stake_pence"), 1.5, "staking.stake_pence:not_integer"),
            (("costs", "rate_source"), "market_base_rate", "costs.rate_source:not_effective_charge"),
            (("costs", "commission_rate"), 1.0, "costs.commission_rate:out_of_range"),
            (("costs", "commission_rate"), -0.01, "costs.commission_rate:out_of_range"),
            (("costs", "commission_rate"), 1, "costs.commission_rate:not_float"),
            (("costs", "commission_rate"), 0, "costs.commission_rate:not_float"),
            (("costs", "commission_rate"), -0.0, "costs.commission_rate:negative_zero"),
            (("costs", "commission_rate"), False, "costs.commission_rate:not_float"),
            (("costs", "rounding"), "none", "costs.rounding:not_allowed"),
            (("primary_metric", "name"), "sharpe", "primary_metric.name:not_allowed"),
            (("primary_metric", "definition"), "mean_net_per_bet", "primary_metric.definition:not_allowed"),
            (("primary_metric", "definition"), "sum_net_over_sum_stakes", "primary_metric.definition:not_allowed"),
            (("uncertainty", "block"), "market", "uncertainty.block:shorter_than_race_day"),
            (("uncertainty", "block"), "bet", "uncertainty.block:shorter_than_race_day"),
            (("uncertainty", "net_by"), "bet", "uncertainty.net_by:not_allowed"),
            (("uncertainty", "method"), "iid_bootstrap", "uncertainty.method:not_allowed"),
            (("uncertainty", "confidence_level"), 1, "uncertainty.confidence_level:not_float"),
            (("uncertainty", "confidence_level"), 1.0, "uncertainty.confidence_level:out_of_range"),
            (("uncertainty", "confidence_level"), 0.0, "uncertainty.confidence_level:out_of_range"),
            (("uncertainty", "resamples"), 99, "uncertainty.resamples:out_of_range"),
            (("uncertainty", "resamples"), 1000001, "uncertainty.resamples:out_of_range"),
            (("seeds", "selection"), -1, "seeds.selection:out_of_range"),
            (("seeds", "bootstrap"), 2 ** 63, "seeds.bootstrap:out_of_range"),
        ]
        for (section, key), bad, expected in cases:
            value = draft()
            value[section][key] = bad
            with self.subTest(field=f"{section}.{key}", value=bad):
                self.assertEqual(problems(value), [expected])
        for section, key, good in (("staking", "stake_pence", 100000), ("staking", "stake_pence", 1),
                                   ("costs", "commission_rate", 0.0), ("uncertainty", "confidence_level", 0.5),
                                   ("uncertainty", "resamples", 100), ("uncertainty", "resamples", 1000000),
                                   ("seeds", "selection", 0), ("seeds", "bootstrap", 2 ** 63 - 1)):
            value = draft()
            value[section][key] = good
            with self.subTest(field=f"{section}.{key}", good=good):
                self.assertEqual(problems(value), [])
        metrics = draft()["secondary_metrics"]
        value = draft()
        value["secondary_metrics"] = sorted(metrics + ["clv_vs_bsp"])
        self.assertEqual(problems(value), ["secondary_metrics:clv_vs_bsp_zero_by_construction_for_bsp_entry"])
        for deferred in ("brier_score", "log_loss"):
            value = draft()
            value["secondary_metrics"] = sorted(metrics + [deferred])
            with self.subTest(metric=deferred):
                self.assertEqual(problems(value), [f"secondary_metrics:{deferred}_deferred_no_forecast_output"])
        value = draft()
        value["secondary_metrics"] = list(reversed(metrics))
        self.assertEqual(problems(value), ["secondary_metrics:not_sorted_unique"])
        for needed in contract_module.REQUIRED_SECONDARY:
            value = draft()
            value["secondary_metrics"] = [m for m in metrics if m != needed]
            with self.subTest(needed=needed):
                self.assertEqual(problems(value), [f"secondary_metrics:missing_{needed}"])
        for max_trials, expected in ((1, ["stopping_rule.max_trials:fewer_than_variants"]),
                                     (0, ["stopping_rule.max_trials:out_of_range"]),
                                     (10001, ["stopping_rule.max_trials:out_of_range"]), (2, []), (10000, [])):
            value = frozen()
            value["stopping_rule"]["max_trials"] = max_trials
            with self.subTest(max_trials=max_trials):
                self.assertEqual(problems(value), expected)
        value = draft()
        value["variants"] = []
        self.assertEqual(problems(value), ["variants:empty"])
        value = draft()
        value["variants"][1]["id"] = value["variants"][0]["id"]
        self.assertEqual(problems(value), ["variants[1].id:duplicate"])

    def test_huge_integers_never_crash_the_validator(self):
        expected = {"costs.commission_rate": "not_float", "primary_metric.decision_threshold": "not_float",
                    "uncertainty.confidence_level": "not_float", "staking.stake_pence": "out_of_range",
                    "uncertainty.resamples": "out_of_range", "seeds.selection": "out_of_range",
                    "seeds.bootstrap": "out_of_range", "stopping_rule.max_trials": "out_of_range",
                    "decision_timing.delay_s": "out_of_range", "features[0].lookback_s": "out_of_range"}
        for path, reason in expected.items():
            for huge in (10 ** 400, -10 ** 400):
                value = frozen()
                set_path(value, path, huge)
                with self.subTest(path=path, huge=huge > 0):
                    self.assertIn(f"{path}:{reason}", problems(value))

    def test_population_scope_splits_and_registration(self):
        cases = [
            (("sport",), "greyhound_racing", "population.sport:not_in_schema_v1"),
            (("sport",), "soccer", "population.sport:not_in_schema_v1"),
            (("sport",), "HORSE_RACING", "population.sport:not_allowed"),
            (("provider",), "betfair_bsp_csv", "population.provider:not_allowed"),
            (("tier",), "basic", "population.tier:not_allowed"),
            (("countries",), ["IE", "GB"], "population.countries:not_sorted_unique"),
            (("countries",), ["GB", "GB"], "population.countries:not_sorted_unique"),
            (("countries",), ["FR", "GB"], "population.countries:not_allowed"),
            (("countries",), [], "population.countries:empty"),
            (("market_types",), ["PLACE"], "population.market_types:not_allowed"),
            (("evaluate_split",), "holdout", "population.evaluate_split:sealed"),
            (("evaluate_split",), "research", "population.evaluate_split:not_engineering_for_engineering_check"),
            (("splits", "holdout", "state"), "downloaded", "population.splits.holdout.state:not_allowed"),
            (("splits", "research", "start_month"), "2016-01",
             "population.splits:engineering_overlaps_research"),
            (("splits", "research", "start_month"), "2016-04",
             "population.splits:engineering_overlaps_research"),
            (("splits", "holdout", "start_month"), "2025-06", "population.splits:research_overlaps_holdout"),
            (("splits", "engineering", "start_month"), "2016-05", "population.splits.engineering:start_after_end"),
            (("splits", "engineering", "end_month"), "2016-13", "population.splits.engineering.end_month:malformed"),
            (("splits", "engineering", "end_month"), "2016-4", "population.splits.engineering.end_month:malformed"),
        ]
        for path, bad, expected in cases:
            value = draft()
            target = value["population"]
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = bad
            with self.subTest(path=".".join(path), value=bad):
                self.assertIn(expected, problems(value))
        value = draft()
        value["population"]["splits"]["engineering"] = {"start_month": "2015-05", "end_month": "2015-05"}
        self.assertEqual(problems(value), [])
        value = draft()
        value["population"]["splits"]["holdout"].update(start_month="2014-01", end_month="2014-12")
        self.assertEqual(problems(value), ["population.splits.holdout:not_after_other_splits",
                                           "population.splits.holdout:not_the_sealed_holdout"])
        value = frozen()
        value["registration"]["kind"] = "registered_hypothesis"
        self.assertEqual(problems(value), ["population.evaluate_split:not_research_for_registered_hypothesis"])
        value["population"]["evaluate_split"] = "research"
        self.assertEqual(problems(value), [])
        for bad in ("Backtest", "bx", "a" * 65, 7):
            value = draft()
            value["experiment_id"] = bad
            with self.subTest(experiment_id=bad):
                self.assertEqual(problems(value), ["experiment_id:malformed"])
        for bad in (2, True, "1"):
            value = draft()
            value["schema_version"] = bad
            with self.subTest(schema_version=bad):
                self.assertEqual(problems(value), ["schema_version:unsupported"])

    def test_the_sealed_holdout_cannot_move(self):
        def splits(engineering, research, holdout):
            value = frozen()
            value["registration"]["kind"] = "registered_hypothesis"
            value["population"]["evaluate_split"] = "research"
            value["population"]["splits"] = {
                "engineering": {"start_month": engineering[0], "end_month": engineering[1]},
                "research": {"start_month": research[0], "end_month": research[1]},
                "holdout": {"start_month": holdout[0], "end_month": holdout[1], "state": "sealed_not_downloaded"}}
            return problems(value)

        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2025-12"), ("2026-01", "2026-09")), [])
        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2026-09"), ("2026-10", "2026-12")),
                         ["population.splits.holdout:not_the_sealed_holdout",
                          "population.splits.research:overlaps_sealed_holdout"])
        self.assertEqual(splits(("2026-01", "2026-09"), ("2015-05", "2016-04"), ("2026-10", "2027-06")),
                         ["population.splits.engineering:overlaps_sealed_holdout",
                          "population.splits.holdout:not_the_sealed_holdout"])
        self.assertEqual(splits(("2015-05", "2015-12"), ("2016-05", "2016-12"), ("2017-01", "2017-06")),
                         ["population.splits.holdout:inspected_not_holdout",
                          "population.splits.holdout:not_the_sealed_holdout"])
        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2025-12"), ("2026-01", "2026-08")),
                         ["population.splits.holdout:not_the_sealed_holdout"])
        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2026-01"), ("2026-01", "2026-09")),
                         sorted(["population.splits.holdout:not_after_other_splits",
                                 "population.splits.research:overlaps_sealed_holdout",
                                 "population.splits:research_overlaps_holdout"]))

    def test_hash_is_canonical_and_stable_under_key_order(self):
        value = draft()
        _, digest = validate_contract(value)
        self.assertEqual(validate_contract(reorder(value))[1], digest)
        self.assertNotEqual(list(reorder(value)), list(value))
        expected = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                                  .encode("utf-8")).hexdigest()
        self.assertEqual(digest, expected)
        for sample in (value, {"b": "é", "a": [1, 2.5, None, True]}):
            self.assertEqual(contract_module.canonical_json(sample), receipts.canonical_json(sample))
        changed = draft()
        changed["seeds"]["selection"] += 1
        self.assertNotEqual(validate_contract(changed)[1], digest)
        with tempfile.TemporaryDirectory() as tmp:
            compact = Path(tmp) / "compact.json"
            spaced = Path(tmp) / "spaced.json"
            compact.write_text(json.dumps(reorder(value), separators=(",", ":")), encoding="utf-8")
            spaced.write_text(json.dumps(value, indent=4, ensure_ascii=True), encoding="utf-8")
            self.assertEqual(check_contract_file(compact)["contract_sha256"], digest)
            self.assertEqual(check_contract_file(spaced)["contract_sha256"], digest)
            self.assertEqual(check_contract_file(DRAFT)["contract_sha256"], digest)
        self.assertEqual(validate_contract({"x": float("nan")}), (["contract:not_canonical_json"], None))
        self.assertEqual(validate_contract({"x": {1, 2}}), (["contract:not_canonical_json"], None))
        # One meaning, one valid spelling: 0 and -0.0 are refused, reordered features and variants are refused.
        valid = [validate_contract(edited(draft, costs__commission_rate=rate)) for rate in (0, 0.0, -0.0)]
        self.assertEqual([found for found, _ in valid], [["costs.commission_rate:not_float"], [],
                                                         ["costs.commission_rate:negative_zero"]])

    def test_file_loading_is_strict(self):
        cases = {
            b'{"a": 1, "a": 2}': "contract:duplicate_key",
            b'{"a": NaN}': "contract:non_finite_number",
            b'{"a": -Infinity}': "contract:non_finite_number",
            b'{"a": 1e400}': "contract:non_finite_number",
            b'{"a": 1' + b'0' * 400 + b'}': "contract:number_too_large",
            b'{"a": -1' + b'0' * 5000 + b'}': "contract:number_too_large",
            b'{"a": 9223372036854775808}': "contract:number_too_large",
            b'{"a": -9223372036854775809}': "contract:number_too_large",
            b'{"a": 1,}': "contract:not_json",
            b'\xef\xbb\xbf{}': "contract:not_json",
            b'{"a": "\xff"}': "contract:not_utf8",
            b' ' * (contract_module.MAX_CONTRACT_BYTES + 1): "contract:too_large",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for index, (data, expected) in enumerate(cases.items()):
                path = Path(tmp) / f"c{index}.json"
                path.write_bytes(data)
                with self.subTest(case=data[:24]):
                    self.assertEqual(check_contract_file(path),
                                     {"status": "invalid", "contract_sha256": None, "problems": [expected]})
            for edge in (b'{"a": 9223372036854775807}', b'{"a": -9223372036854775808}'):
                path = Path(tmp) / "edge.json"
                path.write_bytes(edge)
                with self.subTest(edge=edge):
                    self.assertNotIn("contract:number_too_large", check_contract_file(path)["problems"])
            self.assertEqual(check_contract_file(Path(tmp) / "missing.json")["problems"], ["contract:unreadable"])
            self.assertEqual(check_contract_file(Path(tmp))["problems"], ["contract:not_a_regular_file"])
            fifo = Path(tmp) / "fifo.json"
            os.mkfifo(fifo)
            self.assertEqual(check_contract_file(fifo)["problems"], ["contract:not_a_regular_file"])
            path = Path(tmp) / "list.json"
            path.write_text("[]", encoding="utf-8")
            result = check_contract_file(path)
            self.assertEqual((result["status"], result["problems"]), ("invalid", ["contract:not_an_object"]))
            self.assertRegex(result["contract_sha256"], r"\A[0-9a-f]{64}\Z")


class ContractCliTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def run_main(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, json.loads(out.getvalue())

    def test_exit_codes_and_output(self):
        code, output = self.run_main(["validate-contract", "--contract", str(DRAFT)])
        self.assertEqual(code, 0)
        self.assertEqual(output, {"status": "valid_draft", "contract_sha256": validate_contract(draft())[1],
                                  "problems": []})
        code, output = self.run_main(["validate-contract", "--contract", str(DRAFT), "--require-frozen"])
        self.assertEqual((code, output["status"]), (3, "valid_draft"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frozen.json"
            path.write_text(json.dumps(frozen()), encoding="utf-8")
            code, output = self.run_main(["validate-contract", "--contract", str(path)])
            self.assertEqual((code, output["status"], output["problems"]), (0, "valid_frozen", []))
            code, output = self.run_main(["validate-contract", "--contract", str(path), "--require-frozen"])
            self.assertEqual((code, output["status"]), (0, "valid_frozen"))
            bad = draft()
            bad["decision_timing"]["anchor"] = "actual_off"
            path.write_text(json.dumps(bad), encoding="utf-8")
            for extra in ([], ["--require-frozen"]):
                code, output = self.run_main(["validate-contract", "--contract", str(path), *extra])
                self.assertEqual((code, output["status"], output["problems"]),
                                 (2, "invalid", ["decision_timing.anchor:retrospective"]))
                self.assertIsNotNone(output["contract_sha256"])
            code, output = self.run_main(["validate-contract", "--contract", str(Path(tmp) / "missing.json")])
            self.assertEqual((code, output), (2, {"status": "invalid", "contract_sha256": None,
                                                  "problems": ["contract:unreadable"]}))
            huge = Path(tmp) / "huge.json"
            text = DRAFT.read_text(encoding="utf-8")
            huge.write_text(text.replace('"commission_rate": 0.05', '"commission_rate": 1' + "0" * 400),
                            encoding="utf-8")
            code, output = self.run_main(["validate-contract", "--contract", str(huge)])
            self.assertEqual((code, output), (2, {"status": "invalid", "contract_sha256": None,
                                                  "problems": ["contract:number_too_large"]}))
        for argv in ([], ["validate-contract"], ["frobnicate"]):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_module_entry_point_in_a_fresh_interpreter(self):
        env = {**os.environ, "BETANGEL_BETFAIR_FORBID_NETWORK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-B", "-m", "betangel_research.betfair.backtest", "validate-contract",
                                 "--contract", str(DRAFT)], cwd=REPO, env=env, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout)["status"], "valid_draft")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text('{"schema_version": 1}', encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "-m", "betangel_research.betfair.backtest",
                                     "validate-contract", "--contract", str(path)], cwd=REPO, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"], "invalid")
            path.write_text('{"commission_rate": 1' + "0" * 400 + "}", encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "-m", "betangel_research.betfair.backtest",
                                     "validate-contract", "--contract", str(path)], cwd=REPO, env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual((result.returncode, result.stderr), (2, ""))
            self.assertEqual(json.loads(result.stdout)["problems"], ["contract:number_too_large"])

    def test_cli_loads_only_backtest_modules(self):
        """Runtime companion to the static boundary test: in an isolated interpreter (-I -S, no site), running the
        validator loads no betangel_research module beyond the backtest package and its parents, and no
        network or process module. This is one run on this interpreter, not a proof for every path."""
        env = {"BETANGEL_BETFAIR_FORBID_NETWORK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", LOADED_MODULES_SCRIPT, str(REPO), str(DRAFT)],
                                cwd=tempfile.gettempdir(), env=env, capture_output=True, text=True, timeout=60)
        code, loaded, network = json.loads(result.stderr)
        self.assertEqual(code, 0)
        self.assertEqual(loaded, ["betangel_research", "betangel_research.betfair",
                                  "betangel_research.betfair.backtest", "betangel_research.betfair.backtest.__main__",
                                  "betangel_research.betfair.backtest.contract"])
        self.assertEqual(network, [])


if __name__ == "__main__":
    unittest.main()
