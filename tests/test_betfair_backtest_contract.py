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
from betangel_research.betfair.backtest.contract import (DRAFT_NULLABLE, ENGINEERING_CRITERIA, check_contract_file,
                                                         validate_contract)

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
TEXT_LEAVES = ("registration.note", "financial_rule.reason", "engineering_acceptance.criteria[0].definition",
               "engineering_acceptance.criteria[1].definition", "engineering_acceptance.criteria[2].definition",
               "cohort.market_rule", "cohort.runner_rule", "features[0].definition", "features[1].definition",
               "costs.note", "variants[0].description", "variants[1].description", "stopping_rule.rule")
REGISTERED_RULE = {"kind": "roi_threshold", "hypothesis": "Synthetic: a hypothesis for the test only.",
                   "decision_threshold": 0.0, "decision_rule": "Synthetic rule for the test only."}


def draft():
    return json.loads(DRAFT.read_text(encoding="utf-8"))


def frozen():
    """The engineering draft frozen: its operational nulls filled (synthetic values), nothing financial invented."""
    value = draft()
    value.update(status="frozen", frozen_on="2026-10-14")
    value["decision_timing"]["delay_s"] = 1
    value["stopping_rule"]["max_trials"] = 4
    return value


def registered():
    """frozen() as a registered hypothesis over the research split, with a synthetic roi_threshold rule."""
    value = frozen()
    value["registration"]["kind"] = "registered_hypothesis"
    value["population"]["evaluate_split"] = "research"
    value["financial_rule"] = dict(REGISTERED_RULE)
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
        # Only the operational choices are still open; the engineering check states its purpose in full.
        nulls = sorted(path for path in leaf_paths(value) if get_path(value, path) is None)
        self.assertEqual(nulls, ["decision_timing.delay_s", "frozen_on", "stopping_rule.max_trials"])
        self.assertEqual(sorted(value["financial_rule"]), ["kind", "reason"])
        self.assertEqual(value["financial_rule"]["kind"], "not_applicable")
        self.assertEqual([c["name"] for c in value["engineering_acceptance"]["criteria"]], list(ENGINEERING_CRITERIA))
        self.assertEqual(value["capabilities"], {
            "decision": {"required": ["ltp", "market_definition"], "on_missing": "unsupported_abstain"},
            "outcome": {"required": ["final_bsp", "results"], "on_missing": "unresolved_settlement"}})
        population = value["population"]
        self.assertEqual((population["tier"], population["sport"], population["countries"], population["market_types"]),
                         ("BASIC", "horse_racing", ["GB", "IE"], ["WIN"]))
        self.assertEqual(population["splits"], {
            "engineering": {"start_month": "2015-05", "end_month": "2016-04"},
            "research": {"start_month": "2023-01", "end_month": "2025-12"},
            "holdout": {"start_month": "2026-01", "end_month": "2026-08", "state": "sealed_not_downloaded"}})
        self.assertEqual(population["evaluate_split"], "engineering")
        self.assertEqual(value["fitting"], {"kind": "none"})
        self.assertEqual(set(value["settlement"].values()), {"unresolved"})
        self.assertEqual([(f["name"], f["as_of"], f["lookback_s"]) for f in value["features"]],
                         [("active_runner_count", "decision_time", 0), ("ltp_at_decision", "decision_time", 0)])
        self.assertEqual([v["uses_features"] for v in value["variants"]], [["ltp_at_decision"], []])

    def test_only_draft_nullable_fields_may_be_null_in_a_draft(self):
        paths = leaf_paths(draft())
        self.assertGreater(len(paths), 70)
        for path in paths:
            if path == "frozen_on" or path in DRAFT_NULLABLE:
                continue
            value = draft()
            set_path(value, path, None)
            with self.subTest(path=path):
                self.assertIn(f"{path}:null", problems(value))
        # A registered hypothesis's financial fields may wait in a draft; nothing operational beyond delay and budget.
        value = draft()
        value["registration"]["kind"] = "registered_hypothesis"
        value["population"]["evaluate_split"] = "research"
        value["financial_rule"] = dict.fromkeys(REGISTERED_RULE, None) | {"kind": "roi_threshold"}
        self.assertEqual(problems(value), [])
        # Exactly these: nothing else, such as offset_s or the stopping rule's text, may wait in a draft.
        self.assertEqual(DRAFT_NULLABLE, ("decision_timing.delay_s", "financial_rule.decision_rule",
                                          "financial_rule.decision_threshold", "financial_rule.hypothesis",
                                          "stopping_rule.max_trials"))

    def test_frozen_contract_needs_every_field_non_null(self):
        for base in (frozen, registered):
            value = base()
            found, _ = validate_contract(value)
            with self.subTest(base=base.__name__):
                self.assertEqual(found, [])
                self.assertEqual(contract_module.result_status(value, found), "valid_frozen")
        for path in DRAFT_NULLABLE:
            value = registered()
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
        self.assertIn("decision_timing.delay_s:null_in_frozen", problems(value))

    def test_frozen_text_must_not_be_a_placeholder(self):
        value = registered()
        value["financial_rule"]["hypothesis"] = "TBD"
        value["cohort"].update(market_rule="-", runner_rule="TBD", exclusions=[])
        value["features"][0]["definition"] = "x"
        value["stopping_rule"]["rule"] = "?"
        value["costs"]["note"] = "."
        value["registration"]["note"] = "n/a"
        value["variants"][0]["description"] = "TODO: describe"
        value["financial_rule"]["decision_rule"] = "Lorem ipsum dolor sit amet, placeholder."
        value["engineering_acceptance"]["criteria"][0]["definition"] = "tbc"
        self.assertEqual(problems(value), sorted([
            "financial_rule.hypothesis:placeholder_in_frozen", "cohort.market_rule:placeholder_in_frozen",
            "cohort.runner_rule:placeholder_in_frozen", "cohort.exclusions:empty_in_frozen",
            "features[0].definition:placeholder_in_frozen", "stopping_rule.rule:placeholder_in_frozen",
            "costs.note:placeholder_in_frozen", "registration.note:placeholder_in_frozen",
            "variants[0].description:placeholder_in_frozen", "financial_rule.decision_rule:placeholder_in_frozen",
            "engineering_acceptance.criteria[0].definition:placeholder_in_frozen"]))
        for base, path in ((registered, "financial_rule.hypothesis"), (frozen, "cohort.market_rule"),
                           (frozen, "cohort.runner_rule"), (frozen, "features[1].definition"),
                           (registered, "financial_rule.decision_rule"), (frozen, "variants[1].description"),
                           (frozen, "stopping_rule.rule"), (frozen, "financial_rule.reason"),
                           (frozen, "engineering_acceptance.criteria[2].definition")):
            value = base()
            set_path(value, path, "Too short a rule.")
            with self.subTest(path=path):
                self.assertEqual(problems(value), [f"{path}:too_short_in_frozen"])
        self.assertEqual(problems(edited(frozen, financial_rule__reason="n/a")),
                         ["financial_rule.reason:placeholder_in_frozen"])
        value = frozen()
        value["cohort"]["exclusions"] = ["TBD"]
        self.assertEqual(problems(value), ["cohort.exclusions[0]:placeholder_in_frozen"])
        # A draft may hold unfinished text; the freeze is what binds it.
        value = draft()
        value["cohort"].update(market_rule="TBD", exclusions=[])
        self.assertEqual(problems(value), [])
        for threshold, expected in ((-5.0, ["financial_rule.decision_threshold:out_of_range"]), (-1.0, []),
                                    (998.5, []), (999.0, ["financial_rule.decision_threshold:out_of_range"]),
                                    (0, ["financial_rule.decision_threshold:not_float"])):
            value = registered()
            value["financial_rule"]["decision_threshold"] = threshold
            with self.subTest(threshold=threshold):
                self.assertEqual(problems(value), expected)

    def test_engineering_check_has_an_honest_frozen_form(self):
        """Astra's R3 root reproduction: the committed draft with status frozen, frozen_on, delay_s=1 and
        max_trials=2 was refused for hypothesis, decision_rule and decision_threshold being null. An engineering
        check now states that no financial rule applies and fixes deterministic engineering criteria instead; the
        operational choices stay mandatory at freeze and no draft passes as frozen."""
        def freeze(delay_s, max_trials):
            value = draft()
            value.update(status="frozen", frozen_on="2026-10-14")
            value["decision_timing"]["delay_s"] = delay_s
            value["stopping_rule"]["max_trials"] = max_trials
            return value

        value = freeze(1, 2)
        found, _ = validate_contract(value)
        self.assertEqual((found, contract_module.result_status(value, found)), ([], "valid_frozen"))
        self.assertEqual(problems(freeze(None, None)), ["decision_timing.delay_s:null_in_frozen",
                                                        "stopping_rule.max_trials:null_in_frozen"])
        self.assertEqual(problems(freeze(1, None)), ["stopping_rule.max_trials:null_in_frozen"])
        self.assertEqual(problems(freeze(None, 2)), ["decision_timing.delay_s:null_in_frozen"])
        # Each kind carries its own typed financial rule; the other kind's is refused, not ignored.
        value = freeze(1, 2)
        value["financial_rule"] = dict(REGISTERED_RULE)
        self.assertEqual(problems(value), ["financial_rule.kind:not_applicable_required_for_engineering_check"])
        value = registered()
        value["financial_rule"] = draft()["financial_rule"]
        self.assertEqual(problems(value), ["financial_rule.kind:roi_threshold_required_for_registered_hypothesis"])
        value = freeze(1, 2)
        value["financial_rule"]["decision_threshold"] = 0.0
        self.assertEqual(problems(value), ["financial_rule.decision_threshold:unknown_key"])
        value = freeze(1, 2)
        value["financial_rule"] = {"kind": "skip", "reason": "Synthetic reason for the test only.", "extra": 1}
        self.assertEqual(problems(value), ["financial_rule.extra:unknown_key", "financial_rule.kind:not_allowed"])
        value = freeze(1, 2)
        del value["financial_rule"]["kind"]
        self.assertEqual(problems(value), ["financial_rule.kind:missing"])
        for bad, reason in ((None, "null"), ([], "not_an_object")):
            value = freeze(1, 2)
            value["financial_rule"] = bad
            with self.subTest(financial_rule=bad):
                self.assertEqual(problems(value), [f"financial_rule:{reason}"])
        value = registered()
        del value["financial_rule"]["decision_rule"]
        self.assertEqual(problems(value), ["financial_rule.decision_rule:missing"])
        # Every engineering criterion, once, by name; nothing outcome-based can be added as one.
        for name in ENGINEERING_CRITERIA:
            value = freeze(1, 2)
            criteria = value["engineering_acceptance"]["criteria"]
            criteria[:] = [c for c in criteria if c["name"] != name]
            with self.subTest(missing=name):
                self.assertEqual(problems(value), [f"engineering_acceptance.criteria:missing_{name}"])
        value = freeze(1, 2)
        value["engineering_acceptance"]["criteria"].append({"name": "roi_above_zero",
                                                            "definition": "Synthetic: observed ROI above zero."})
        self.assertEqual(problems(value), ["engineering_acceptance.criteria[3].name:not_allowed"])
        value = freeze(1, 2)
        value["engineering_acceptance"]["criteria"].reverse()
        self.assertEqual(problems(value), ["engineering_acceptance.criteria:not_sorted_by_name"])
        value = freeze(1, 2)
        value["engineering_acceptance"]["criteria"][1]["name"] = ENGINEERING_CRITERIA[0]
        self.assertEqual(problems(value), ["engineering_acceptance.criteria:missing_known_accounting_answers",
                                           "engineering_acceptance.criteria[1].name:duplicate"])
        value = freeze(1, 2)
        value["engineering_acceptance"]["criteria"] = []
        missing = [f"engineering_acceptance.criteria:missing_{name}" for name in ENGINEERING_CRITERIA]
        self.assertEqual(problems(value), ["engineering_acceptance.criteria:empty", *missing])
        # How the criteria combine is a token, not text: no wording can turn observed ROI into a pass condition,
        # in a draft or a frozen contract.
        self.assertEqual(contract_module.ENGINEERING_RULES, ("all_criteria_must_hold_outcome_metrics_not_judged",))
        self.assertEqual(draft()["engineering_acceptance"]["rule"], contract_module.ENGINEERING_RULES[0])
        for bad in ("The run passes only if the observed ROI is above zero percent.", "tbc", "", 7, ["x"]):
            for base in (draft, lambda: freeze(1, 2)):
                value = base()
                value["engineering_acceptance"]["rule"] = bad
                with self.subTest(rule=bad, status=value["status"]):
                    self.assertEqual(problems(value), ["engineering_acceptance.rule:not_allowed"])
        # The gate is unchanged: a draft is never valid_frozen, and the CLI's --require-frozen still exits 3 for it.
        self.assertEqual(contract_module.result_status(draft(), []), "valid_draft")

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
        value["capabilities"]["decision"]["required"] = ["ltp"]
        self.assertEqual(problems(value), ["capabilities.decision.required:missing_market_definition",
                                           "features[0].capability:not_required"])
        value = draft()
        value["capabilities"]["decision"]["required"] = ["ltp", "market_definition", "best_offers"]
        self.assertEqual(problems(value), ["capabilities.decision.required:not_sorted_unique"])
        value = draft()
        value["features"] = []
        for variant in value["variants"]:
            variant["uses_features"] = []
        self.assertEqual(problems(value), [])

    def test_missing_outcomes_never_become_abstentions(self):
        """R1: decision-time inputs and outcomes are separate classes. A missing decision-time input may abstain
        (unsupported); a missing final BSP or result must leave the earlier decision standing with an unresolved
        settlement, so abstaining on it, or listing an outcome as a decision input, is refused."""
        self.assertEqual(set(contract_module.DECISION_CAPABILITIES) & set(contract_module.OUTCOME_CAPABILITIES), set())
        self.assertEqual(sorted(contract_module.DECISION_CAPABILITIES + contract_module.OUTCOME_CAPABILITIES),
                         list(contract_module.CAPABILITIES))
        self.assertEqual(contract_module.OUTCOME_CAPABILITIES, ("final_bsp", "results"))
        self.assertEqual(contract_module.ON_MISSING, {"decision": ("unsupported_abstain",),
                                                      "outcome": ("unresolved_settlement",)})
        # The outcome policy and the settlement rule for a missing BSP or result say the same thing.
        self.assertEqual(contract_module.SETTLEMENT_RULES, ("unresolved",))
        self.assertIn("missing_bsp_or_result", contract_module.SETTLEMENT_CASES)
        cases = [
            (("outcome", "on_missing"), "unsupported_abstain",
             "capabilities.outcome.on_missing:would_change_an_earlier_decision"),
            (("decision", "on_missing"), "unresolved_settlement",
             "capabilities.decision.on_missing:not_for_decision_inputs"),
            (("decision", "on_missing"), "zero", "capabilities.decision.on_missing:not_allowed"),
            (("outcome", "on_missing"), "zero", "capabilities.outcome.on_missing:not_allowed"),
            (("decision", "required"), ["final_bsp", "ltp", "market_definition"],
             "capabilities.decision.required:final_bsp_is_an_outcome"),
            (("decision", "required"), ["ltp", "market_definition", "results"],
             "capabilities.decision.required:results_is_an_outcome"),
            (("outcome", "required"), ["final_bsp", "ltp", "results"],
             "capabilities.outcome.required:ltp_is_a_decision_input"),
            (("outcome", "required"), ["final_bsp"], "capabilities.outcome.required:missing_results"),
            (("outcome", "required"), ["results"], "capabilities.outcome.required:missing_final_bsp"),
            (("outcome", "required"), [], "capabilities.outcome.required:empty"),
            (("decision", "required"), [], "capabilities.decision.required:empty"),
            (("outcome", "required"), ["results", "final_bsp"], "capabilities.outcome.required:not_sorted_unique"),
        ]
        for (cls, key), bad, expected in cases:
            value = draft()
            value["capabilities"][cls][key] = bad
            with self.subTest(field=f"{cls}.{key}", value=bad):
                self.assertIn(expected, problems(value))
        # Every misclassification names itself; nothing else in the contract is blamed.
        value = draft()
        value["capabilities"]["outcome"]["on_missing"] = "unsupported_abstain"
        self.assertEqual(problems(value), ["capabilities.outcome.on_missing:would_change_an_earlier_decision"])
        # The old single-list shape (one on_missing policy for outcomes and inputs alike) is no longer v1.
        value = draft()
        value["capabilities"] = {"required": ["final_bsp", "ltp", "market_definition", "results"],
                                 "on_missing": "unsupported_abstain"}
        self.assertEqual(problems(value), ["capabilities.decision:missing", "capabilities.on_missing:unknown_key",
                                           "capabilities.outcome:missing", "capabilities.required:unknown_key"])
        # An outcome is never a feature, whichever class lists it.
        for capability in contract_module.OUTCOME_CAPABILITIES:
            value = draft()
            value["features"][1]["capability"] = capability
            with self.subTest(feature_capability=capability):
                self.assertEqual(problems(value), ["features[1].capability:outcome_not_a_feature"])
        # The draft's cohort exclusions make the same split in words.
        exclusions = " ".join(draft()["cohort"]["exclusions"])
        self.assertIn("capabilities.decision", exclusions)
        self.assertIn("A missing final BSP or result is no exclusion", exclusions)

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
        expected = {"costs.commission_rate": "not_float", "financial_rule.decision_threshold": "not_float",
                    "uncertainty.confidence_level": "not_float", "staking.stake_pence": "out_of_range",
                    "uncertainty.resamples": "out_of_range", "seeds.selection": "out_of_range",
                    "seeds.bootstrap": "out_of_range", "stopping_rule.max_trials": "out_of_range",
                    "decision_timing.delay_s": "out_of_range", "features[0].lookback_s": "out_of_range"}
        for path, reason in expected.items():
            for huge in (10 ** 400, -10 ** 400):
                value = registered()
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
        value = registered()
        value["population"]["evaluate_split"] = "engineering"
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

    def test_split_months_refuse_year_zero(self):
        """N2: year 0000 is not a calendar year; an otherwise valid frozen contract with it was valid_frozen."""
        for path in ("engineering.start_month", "engineering.end_month", "research.start_month",
                     "holdout.start_month"):
            value = frozen()
            set_path(value, f"population.splits.{path}", "0000-01")
            with self.subTest(path=path):
                self.assertIn(f"population.splits.{path}:malformed", problems(value))
        self.assertEqual(problems(edited(frozen, population__splits__engineering__start_month="0000-01")),
                         ["population.splits.engineering.start_month:malformed"])
        self.assertEqual(problems(edited(frozen, population__splits__engineering__start_month="0001-01")), [])

    def test_the_sealed_holdout_cannot_move(self):
        """R2: the holdout is 2026-01..2026-08 (Astra's technical decision of 2026-10-14, adopted by Claude);
        2026-09 is exposed and exploratory, so the superseded 2026-01..2026-09 holdout is refused."""
        def splits(engineering, research, holdout):
            value = registered()
            value["population"]["splits"] = {
                "engineering": {"start_month": engineering[0], "end_month": engineering[1]},
                "research": {"start_month": research[0], "end_month": research[1]},
                "holdout": {"start_month": holdout[0], "end_month": holdout[1], "state": "sealed_not_downloaded"}}
            return problems(value)

        self.assertEqual(contract_module.SEALED_HOLDOUT, ("2026-01", "2026-08"))
        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2025-12"), ("2026-01", "2026-08")), [])
        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2025-12"), ("2026-01", "2026-09")),
                         ["population.splits.holdout:exposed_not_holdout",
                          "population.splits.holdout:not_the_sealed_holdout"])
        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2025-12"), ("2026-01", "2026-07")),
                         ["population.splits.holdout:not_the_sealed_holdout"])
        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2026-09"), ("2026-10", "2026-12")),
                         ["population.splits.holdout:exposed_not_holdout",
                          "population.splits.holdout:not_the_sealed_holdout",
                          "population.splits.research:exposed_exploratory_only",
                          "population.splits.research:overlaps_sealed_holdout"])
        self.assertEqual(splits(("2026-01", "2026-08"), ("2015-05", "2016-04"), ("2026-10", "2027-06")),
                         ["population.splits.engineering:overlaps_sealed_holdout",
                          "population.splits.holdout:exposed_not_holdout",
                          "population.splits.holdout:not_the_sealed_holdout"])
        self.assertEqual(splits(("2015-05", "2015-12"), ("2016-05", "2016-12"), ("2017-01", "2017-06")),
                         ["population.splits.holdout:inspected_not_holdout",
                          "population.splits.holdout:not_the_sealed_holdout"])
        self.assertEqual(splits(("2015-05", "2016-04"), ("2023-01", "2026-01"), ("2026-01", "2026-08")),
                         sorted(["population.splits.holdout:not_after_other_splits",
                                 "population.splits.research:overlaps_sealed_holdout",
                                 "population.splits:research_overlaps_holdout"]))
        # Exposed data fits no split and the refusal names it, not only the ordering rule: 2026-09 (EXPOSED_MONTHS)
        # and 2026-10, the month of the not-prospective 2026-10-12 (NOT_PROSPECTIVE_DATES), in any split.
        for month in ("2026-09", "2026-10"):
            with self.subTest(research=month):
                self.assertEqual(splits(("2015-05", "2016-04"), (month, month), ("2026-01", "2026-08")),
                                 ["population.splits.holdout:not_after_other_splits",
                                  "population.splits.research:exposed_exploratory_only"])
            with self.subTest(engineering=month):
                self.assertEqual(splits((month, month), ("2023-01", "2025-12"), ("2026-01", "2026-08")),
                                 ["population.splits.engineering:exposed_exploratory_only",
                                  "population.splits.holdout:not_after_other_splits"])
            with self.subTest(holdout=month):
                self.assertIn("population.splits.holdout:exposed_not_holdout",
                              splits(("2015-05", "2016-04"), ("2023-01", "2025-12"), ("2026-01", month)))
        # Astra's root reproduction, inverted: the committed draft holds 2026-08, and 2026-09 is refused.
        value = draft()
        value["population"]["splits"]["holdout"]["end_month"] = "2026-09"
        self.assertEqual(problems(value), ["population.splits.holdout:exposed_not_holdout",
                                           "population.splits.holdout:not_the_sealed_holdout"])

    def test_exposures_are_recorded_with_their_evidence(self):
        """R2: the exposed September file and the 2026-10-12 BSP CSV are recorded with their hashes; deletion does
        not restore untouched status, the full inspection extent stays unknown, and the known minimum is kept."""
        exposed, dates = contract_module.EXPOSED_MONTHS, contract_module.NOT_PROSPECTIVE_DATES
        self.assertEqual(list(exposed), ["2026-09"])
        self.assertEqual(exposed["2026-09"]["sha256"],
                         "8e999e30a7fc17b2dd981cc8d223071b63d94797fbe08884d7ef67d0ec22ec92")
        self.assertEqual(list(dates), ["2026-10-12"])
        self.assertEqual(dates["2026-10-12"]["sha256"],
                         "e1bdc0816bac5ebac2a1fdefa4a46f661cfb65ab0fbbd080ab3dc3545c5e2244")
        for record in (*exposed.values(), *dates.values()):
            self.assertEqual((record["inspection_extent"], record["deleted_on"]), ("unknown", "2026-10-14"))
            self.assertRegex(record["sha256"], r"\A[0-9a-f]{64}\Z")
            # The full extent is unknown, but outcome rows are known to have been seen: the record keeps both.
            self.assertEqual(record["known_minimum"],
                             "header and first one to three rows viewed 2026-10-14 for the plan")
        sealed = contract_module.SEALED_HOLDOUT
        self.assertTrue(all(month > sealed[1] for month in exposed))
        self.assertTrue(all(day[:7] > sealed[1] for day in dates))
        self.assertTrue(all(not sealed[0] <= month <= sealed[1] for month in contract_module.INSPECTED_MONTHS))

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
