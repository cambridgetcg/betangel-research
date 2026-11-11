"""Registered measurement close-v1 (betangel_research.betfair.backtest.fills and resting).

Every archive here is synthetic and goes through the real ingest, index and replay, with the fixture helpers of
tests/test_betfair_backtest_replay.py and tests/test_backtest_lp.py (lp_market and its PRO ladder deltas). These
tests show the registration's checks, the view a rule file reads, the fill rules and the walk to the pull time on
these synthetic shapes only. The metric, the statistics, the selection and the labels are shown on order rows built
by hand. The pass runner (resting_study) is run through main() on a synthetic archive, with a copy of the
registration whose gates are lowered, and with a study ledger in a temporary folder. They say nothing about real
Betfair files or results.
"""

import bz2
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import inspect
import io
import json
import math
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from betangel_research.betfair.backtest import fills, resting_study
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, ingest_archive, safe_target
from betangel_research.betfair.backtest.contract import canonical_json
from betangel_research.betfair.backtest.fills import CENTRAL, MODELS, OPTIMISTIC, PESSIMISTIC
from betangel_research.betfair.backtest.lp import BACK, CROSS, FILL_KINDS, JOIN, LAY, THROUGH, VOLUME, Leg
from betangel_research.betfair.backtest.lp import spread_ticks
from betangel_research.betfair.backtest.lp_study import _walk as lp_walk
from betangel_research.betfair.backtest.market_index import index_archives
from betangel_research.betfair.backtest.metrics import HashStream
from betangel_research.betfair.backtest.movement import (MARKET_CODES, Moment, RegistrationError, active_ids,
                                                         cr1_mean, decision_time, exact, holm, nclv, replay_market,
                                                         sample_sd, select, student_t_quantile)
from betangel_research.betfair.backtest.movement import parse_registration as parse_movement
from betangel_research.betfair.backtest.replay import ReplayProblem, _member
from betangel_research.betfair.backtest.resting import (AS_WRITTEN, BLOCKED, BSP_ENDS, CLAIM_END, CLASSES,
                                                        COUNTS_ONLY, FILLED, FILLED_IS_ALL_PLACED, G, GIVEN_FIRST,
                                                        HARNESS_CODES, KILL_END, LATER, NET_CLOSED, NET_HELD,
                                                        NULL_BLOCKS, NULL_LABEL, NULL_UNDEFINED, PLUS_STEP,
                                                        REST_NOT_GIVEN, SCORED, TRD_COUNTS, UNDERPOWERED, VERDICTS,
                                                        Cell, Fill, OrderRow, blind_outcome, bound, cr1_test,
                                                        deciding_end, described, detectable, ends_apart, figures,
                                                        filled_part, gap, gate, gated, given_figures, given_together,
                                                        named_files, null_gate, one_sided_p, parse_registration,
                                                        population_codes, quantile_table, record_code, rounded, score,
                                                        scored, select_primaries, series, spread, verdicts, view_of,
                                                        walk, whole_hundredths, withheld, without_first_fills)
from betangel_research.betfair.backtest.resting_study import (Tally, decide_record, main, record_facts,
                                                              run_study, summary)
from betangel_research.betfair.backtest.rulefile import RuleView, RunnerView, decide
from betangel_research.betfair.backtest.study_ledger import LEDGER_FILE, PassRequest, StudyLedger, verify
from betangel_research.betfair.backtest.trial_ledger import UnlockError

import test_betfair_backtest_replay as fx
from test_backtest_lp import (BOOK, BSPS, FAV, M, OFF, R2, R3, R6, SIDS, before, book, ladder_move, lp_market,
                              markdown_columns, path, status_at, walk_items)
from test_backtest_lp import REG as LP_REG
from test_backtest_movement import walk_keys
from test_betfair_backtest_replay import definition, iso, mcm, md, ms, runner

REPO = Path(__file__).resolve().parent.parent
REGISTRATION = REPO / "research" / "studies" / "close-v1.json"
# The registration's sha256 as reviewed: any change to the file must change this line too (a new registration).
REGISTRATION_SHA256 = "0424f0e3ff16ab2c85f803c6dbf488aab8173ac9ebf00cb7a42652208a04afb2"
SPLIT_RAW = (REPO / "research" / "partitions-v1.json").read_bytes()
BACK_RULE, LAY_RULE = "close-v1-back", "close-v1-lay"
RULES_RAW = {f"{name}.json": (REPO / "research" / "rules" / f"{name}.json").read_bytes()
             for name in (BACK_RULE, LAY_RULE)}
PLAN = parse_registration(REGISTRATION.read_bytes(), SPLIT_RAW, RULES_RAW)
MOVEMENT_REG = parse_movement((REPO / "research" / "studies" / "movement-v1.json").read_bytes())
TIMES = ((-600, -120), (-300, -120), (-120, -10))
BACK_CELLS = tuple(Cell(BACK_RULE, at_s, pull_s) for at_s, pull_s in TIMES)
LAY_CELLS = tuple(Cell(LAY_RULE, at_s, pull_s) for at_s, pull_s in TIMES)
# The standard book's favourite is 2.5 to back (50 shown) and 2.52 to lay (75 shown): the LAY rests at 2.5 behind
# 0, 25 or 50, the BACK at 2.52 behind 0, 37.5 or 75.
UNFILLED = dict.fromkeys(MODELS)


def P(value):
    return exact(value)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    """The one byte form of a registration, a rule file or the split file."""
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def files(registration=None, rules=None, split=None):
    """(registration bytes, split bytes, {rule file name: bytes}) of the repository's files, changed: rules is
    {rule id: change(value)}, split and registration are change(value). The registration names the changed files'
    sha256 before its own change runs."""
    value, raws, split_raw = json.loads(REGISTRATION.read_bytes()), dict(RULES_RAW), SPLIT_RAW
    for name, change in (rules or {}).items():
        rule = json.loads(raws[f"{name}.json"])
        change(rule)
        raws[f"{name}.json"] = canonical(rule)
    if split is not None:
        parsed = json.loads(split_raw)
        split(parsed)
        split_raw = canonical(parsed)
    value["data"]["split"]["sha256"] = sha(split_raw)
    for item in value["data"]["rules"]:
        item["sha256"] = sha(raws[item["name"]])
    if registration is not None:
        registration(value)
    return canonical(value), split_raw, raws


def plan_of(**changes):
    return parse_registration(*files(**changes))


def member_stream(root, manifest, record):
    return bz2.open(safe_target(root, manifest["archive"]["sha256"], _member(manifest, record)["path"]), "rb")


def kinds(row):
    """{model: fill kind or None} of one OrderRow."""
    return {model: row.fills[model].kind for model in MODELS}


def null_time(cell, market_id=M, off=OFF):
    """The null's time u of one cell's order, in ms, by the registered seed and label."""
    t, end = ms(off) + cell.at_s * 1000, ms(off) + cell.pull_s * 1000
    label = NULL_LABEL.format(market_id=market_id, at_s=cell.at_s, rule_id=cell.rule_id)
    return t + 1 + HashStream(4, label).below(end - t)


class Recording:
    """A stream that keeps every line read from it."""

    def __init__(self, stream):
        self.stream, self.lines = stream, []

    def readline(self, size):
        self.lines.append(self.stream.readline(size))
        return self.lines[-1]


class RestingCase(fx.Workspace):
    def walked(self, messages, plan=PLAN, market_id=M, when=OFF, member=None):
        root, manifest, records = self.build([(member or path(market_id, when), messages)])
        with member_stream(root, manifest, records[market_id]) as stream:
            return walk(stream, records[market_id], plan, DEFAULT_LIMITS)

    def rows(self, messages, **kwargs):
        """{Cell: OrderRow or code} of one market's walk. Every order row is checked on the way: a fill behind a
        longer queue implies a fill behind a shorter one, no later than it."""
        outcomes = self.walked(messages, **kwargs).outcomes
        for row in outcomes.values():
            if isinstance(row, OrderRow):
                taken = [row.fills[model].seconds for model in MODELS]          # front of the queue first
                for ahead, behind in zip(taken, taken[1:]):
                    self.assertTrue(behind is None or (ahead is not None and ahead <= behind), taken)
        return outcomes

    def filled(self, messages, **kwargs):
        """{(side, at_s): {model: kind}} of every order with a fill under some model."""
        found = {}
        for cell, row in self.rows(messages, **kwargs).items():
            if isinstance(row, OrderRow) and any(kinds(row).values()):
                found[row.side, cell.at_s] = kinds(row)
        return found


# ---------------------------------------------------------------------------------------------------------------

class Registration(unittest.TestCase):
    def assertRefused(self, reason, **changes):
        with self.assertRaises(RegistrationError) as caught:
            plan_of(**changes)
        self.assertEqual(str(caught.exception), f"registration:{reason}")

    def test_the_repository_registration_parses_and_is_named_by_its_bytes(self):
        raw = REGISTRATION.read_bytes()
        self.assertEqual(PLAN.sha256, sha(raw))
        self.assertEqual(PLAN.sha256, REGISTRATION_SHA256)
        self.assertEqual(raw, canonical(json.loads(raw)))
        self.assertTrue(all(byte < 128 for byte in raw))
        self.assertEqual((PLAN.study, PLAN.lineage, PLAN.labels), ("close-v1", "close", ("PRO-D", "PRO-V")))
        self.assertEqual(PLAN.cells, BACK_CELLS + LAY_CELLS)
        self.assertEqual([rule.id for rule in PLAN.rules], [BACK_RULE, LAY_RULE])
        self.assertEqual(PLAN.split.sha256, sha(SPLIT_RAW))
        self.assertEqual((PLAN.tiers, PLAN.divisor, PLAN.after_the_hour_s, PLAN.within_s), (("PRO",), 2, 60, (1, 2)))
        self.assertEqual((PLAN.months[0], PLAN.months[-1], len(PLAN.months)), ("2015-05", "2016-04", 12))
        self.assertEqual(PLAN.commission, Fraction(2, 100))
        self.assertEqual((PLAN.models, PLAN.queue_fractions), (
            MODELS, {OPTIMISTIC: Fraction(0), CENTRAL: Fraction(1, 2), PESSIMISTIC: Fraction(1)}))
        self.assertEqual((PLAN.family_m, PLAN.alpha), (2, Fraction(2, 100)))
        self.assertEqual((PLAN.null_seed, PLAN.max_spread_ticks, PLAN.null_fraction, PLAN.null_alpha),
                         (4, 20, Fraction(1, 2), Fraction(5, 100)))
        self.assertEqual((PLAN.select_explore_model, PLAN.select_test_model), (CENTRAL, PESSIMISTIC))
        self.assertEqual((PLAN.min_values, PLAN.min_tested_values, PLAN.min_tested_race_dates, PLAN.min_clusters,
                          PLAN.half_year_min_fills), (100, 200, 15, 15, 100))
        self.assertEqual((PLAN.confidence_level, PLAN.kill_level, PLAN.lower_bound_level, PLAN.pilot_floor),
                         (Fraction(95, 100), Fraction(975, 1000), Fraction(98, 100), Fraction(-15, 1000)))
        self.assertEqual((PLAN.bsp_step, PLAN.bsp_whole_share, PLAN.power, PLAN.detectable_level),
                         (Fraction(1, 100), Fraction(99, 100), Fraction(8, 10), Fraction(99, 100)))
        self.assertEqual(PLAN.quantiles[0], Fraction(5, 100))
        self.assertEqual((PLAN.label_note, PLAN.exposure_note), (
            "PRO 2015-16: a kill gate and a prior, never confirmation",
            "The level of g was known from the whole year. Only the gap is new."))
        value = json.loads(raw)
        self.assertEqual(tuple(value["measurement"]["bsp_ends"]["names"]), BSP_ENDS)
        self.assertEqual(tuple(value["null"]["blocks"]), NULL_BLOCKS)
        self.assertEqual((value["selection"]["bsp_end"], value["selection"]["measure"]), (CLAIM_END, NET_HELD))

    def test_the_codes_are_the_harness_and_the_rule_files_in_evaluation_order(self):
        self.assertEqual(PLAN.codes, population_codes(PLAN.rules))
        self.assertEqual(PLAN.codes[:12], MARKET_CODES[:12])
        self.assertEqual(PLAN.codes[12:], (
            "partition_not_registered", "bsp_market_false", "ladder_malformed", "market_not_open",
            "too_few_active_runners", "no_priced_active_runner", "favourite_tie", "best_back_out_of_range",
            "depth_below_min", "spread_not_1_or_2_ticks", "no_reference_price", "price_off_ladder"))
        self.assertNotIn("issued", PLAN.codes)
        self.assertEqual(CLASSES[:5], ("outcome_unknown", "void_own_runner_removed", "post_decision_removal",
                                       "result_revised_after_close", "missing_bsp"))
        self.assertEqual(CLASSES[5:], ("stream_malformed_after_placement", SCORED))
        # The adapter's minimum field is 0, so movement's thirteenth code is the rule files' own row, never a gate.
        self.assertEqual(MARKET_CODES[12], "too_few_active_runners")
        self.assertNotIn("too_few_active_runners", HARNESS_CODES)

    def test_the_text_says_what_the_code_does(self):
        value = json.loads(REGISTRATION.read_bytes())
        self.assertEqual(value["measurement"]["commission_source"], "Yu, 2026-11-11: the account is on Basic 2%")
        self.assertEqual(value["exposure"]["printed_with_every_label"],
                         "The level of g was known from the whole year. Only the gap is new.")
        self.assertIn("Not fresh on PRO-V", value["exposure"]["not_fresh_on_test"])
        self.assertIn("Fresh on PRO-V", value["exposure"]["fresh_on_test"])
        self.assertIn("needs the stake only", value["fill_models"]["rule"])
        self.assertIn("never snapped to a tick", value["measurement"]["bsp_ends"]["rule"])
        self.assertIn("The null never kills", value["null"]["rule"])
        self.assertIn("a kill gate and a prior, never confirmation", value["verdicts"]["printed_with"])
        self.assertIn("certificate key", value["summary"])
        released = json.dumps(value["blind_pass"]["releases"] + value["registered_pass"]["releases"])
        self.assertEqual([name for name in ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "T5", "T7", "T8", "T9", "T10")
                          if f"{name}:" not in released], [])
        self.assertNotIn("T6", json.dumps(value))                                  # the table by price band is cut
        # Part and rest across the models, the half-years, the labels and the null, in the code's own words.
        part_and_rest = value["measurement"]["part_and_rest"]
        self.assertIn(f"in the order {', '.join(GIVEN_FIRST)}", part_and_rest)
        for phrase in (REST_NOT_GIVEN, "(1) Across the fill models", "(2) Across half-years", "(3) Across labels",
                       "(4) The null's main mean", "after the selection and the labels are decided", "Not covered"):
            self.assertIn(phrase, part_and_rest)
        self.assertIn("the main mean over the same orders is decided on as before and not printed",
                      value["null"]["rule"])
        self.assertIn("the SD is the pooled one over all scored orders", value["statistics"]["rule"])
        self.assertIn("the batch that itself turns the market in play is still judged", value["fill_models"]["rule"])
        for phrase in ("fills in the batch that turned the market in play", "began with no market status at all"):
            self.assertIn(phrase, value["diagnostics"]["rule"])
        self.assertIn("fills in the batch that turns the market in play", released)
        self.assertIn("a primary's own rows are headed as tested in T9 only", released)
        self.assertIn("no table by price band", value["registered_pass"]["rule"])
        # The grid's text names the rule files' own times; the values live in the rule files alone.
        for rule in PLAN.rules:
            self.assertEqual(rule.times, TIMES)
        for at_s, pull_s in TIMES:
            self.assertIn(f"off - {-at_s} s", value["grid"]["rule"])
            self.assertIn(f"off - {-pull_s} s", value["grid"]["rule"])

    def test_the_named_files_are_plain_names(self):
        raw = REGISTRATION.read_bytes()
        self.assertEqual(named_files(raw), ("partitions-v1.json", ("close-v1-back.json", "close-v1-lay.json")))
        for name in ("../rules/close-v1-back.json", "rules/close-v1-back.json", "close-v1-back", "/etc/x.json"):
            with self.subTest(name=name), self.assertRaises(RegistrationError):
                value = json.loads(raw)
                value["data"]["rules"][0]["name"] = name
                named_files(canonical(value))

    def test_a_sealed_unknown_or_budget_less_label_refuses(self):
        def label(key, name):
            return lambda value: value["data"]["partitions"].__setitem__(key, name)

        for key, name, reason in (("explore", "BASIC-D", "no_budget"), ("test", "BASIC-V", "no_budget"),
                                  ("test", "BASIC-T", "role_sealed"), ("test", "SEALED", "role_never"),
                                  ("explore", "SEALED", "role_never"), ("test", "PRO-X", "label_unknown"),
                                  ("explore", None, "label_unknown"), ("explore", "PRO-V", "role_not_explore"),
                                  ("test", "PRO-D", "role_not_holdout")):
            with self.subTest(key=key, name=name):
                self.assertRefused(f"data.partitions.{key}:{reason}", registration=label(key, name))

    def test_the_rule_files_and_the_split_must_be_the_named_bytes(self):
        raw, split_raw, rules = files()
        self.assertEqual(parse_registration(raw, split_raw, rules).sha256, REGISTRATION_SHA256)
        other_rule = canonical({**json.loads(rules["close-v1-lay.json"]), "claim": "Another claim."})
        other_split = canonical({**json.loads(split_raw), "max_looks": 3})
        cases = {
            "data.rules[1]:sha256_mismatch": (raw, split_raw, {**rules, "close-v1-lay.json": other_rule}),
            "data.rules[0]:not_given": (raw, split_raw, {"close-v1-lay.json": rules["close-v1-lay.json"]}),
            "data.rules:not_given": (raw, split_raw, None),
            "data.split:sha256_mismatch": (raw, other_split, rules),
            "data.split:sha256_mismatch ": (raw, None, rules),
            "file:not_canonical": (json.dumps(json.loads(raw)).encode(), split_raw, rules),
            "file:not_json": (raw[:-3], split_raw, rules),
            "file:not_bytes_or_too_large": (raw.decode(), split_raw, rules),
        }
        for reason, given in cases.items():
            with self.subTest(reason=reason), self.assertRaises(RegistrationError) as caught:
                parse_registration(*given)
            self.assertEqual(str(caught.exception), f"registration:{reason.strip()}")
        # A changed file with its new sha256 in the registration parses, under a new registration sha256.
        self.assertNotEqual(plan_of(rules={LAY_RULE: lambda rule: rule.update(claim="Another claim.")}).sha256,
                            REGISTRATION_SHA256)
        self.assertRefused("data.rules[0]:name_not_the_rule_id", rules={BACK_RULE: lambda rule: rule.update(id="x")})
        self.assertRefused("data.rules:name_used_twice",
                           registration=lambda value: value["data"]["rules"].append(value["data"]["rules"][0]))
        self.assertRefused("data.rules[1]:rule:status:not_frozen",
                           rules={LAY_RULE: lambda rule: rule.update(status="draft")})
        self.assertRefused("data.split:partition:max_looks:not_an_int_in_range",
                           split=lambda value: value.update(max_looks=0))

    def test_rule_files_of_two_lineages_refuse(self):
        self.assertRefused("data.rules:lineages_differ", rules={LAY_RULE: lambda rule: rule.update(lineage="other")})
        self.assertEqual(plan_of(rules={name: lambda rule: rule.update(lineage="other")
                                        for name in (BACK_RULE, LAY_RULE)}).lineage, "other")

    def test_a_when_code_may_not_be_a_harness_code_and_must_be_in_the_list(self):
        def code(text):
            return {BACK_RULE: lambda rule: rule["when"][2].update(code=text)}

        for text in ("issued", "price_off_ladder", "partition_not_registered", "bsp_market_false",
                     "turn_in_play_not_enabled", "persistence_not_enabled", *MARKET_CODES[:12]):
            with self.subTest(code=text):
                self.assertIn(text, HARNESS_CODES)
                self.assertRefused("data.rules[0]:when_code_reserved", rules=code(text))
        # decide's own codes are refused by the rule file's parser.
        self.assertRefused("data.rules[0]:rule:when[2]:code_reserved", rules=code("ladder_malformed"))
        self.assertRefused("data.rules[0]:when_code_not_in_population_codes", rules=code("price_too_short"))

        def listed(position):
            def change(value):
                value["population"]["codes"].insert(position, "price_too_short")
            return change

        # In the list, but not where the rule files' rows put it.
        self.assertRefused("population.codes:not_the_implemented_list", rules=code("price_too_short"),
                           registration=listed(0))
        place = PLAN.codes.index("best_back_out_of_range")
        plan = plan_of(rules=code("price_too_short"), registration=listed(place))
        self.assertEqual(plan.codes[place:place + 2], ("price_too_short", "best_back_out_of_range"))

    def test_a_fixed_price_must_be_on_the_ladder(self):
        def fixed(text):
            return {LAY_RULE: lambda rule: rule["intents"][0].update(price={"ref": "fixed", "value": text})}

        self.assertRefused("data.rules[1]:fixed_price_off_ladder", rules=fixed("2.51"))
        self.assertRefused("data.rules[1]:fixed_price_off_ladder", rules=fixed("3.01"))
        self.assertEqual(plan_of(rules=fixed("2.50")).rule(LAY_RULE).intents[0].fixed, Fraction(5, 2))

    def test_this_version_walks_one_lapse_order_per_cell_to_a_pull_time(self):
        def kept(rule):
            rule["times"] = [{"at_s": -600, "pull_s": None}]
            rule["intents"][0]["persist"] = "keep"

        cases = {
            "select_not_favourite": lambda rule: rule.update(select="each_active"),
            "not_one_intent": lambda rule: rule["intents"].append({**rule["intents"][0], "stake": "4.00"}),
            "persist_not_lapse": kept,
            "time_without_a_pull": lambda rule: rule["times"][2].update(pull_s=None),
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                self.assertRefused(f"data.rules[0]:{reason}", rules={BACK_RULE: change})

    def test_a_changed_rule_is_refused(self):
        def reorder(value):
            codes = value["population"]["codes"]
            codes[-1], codes[-2] = codes[-2], codes[-1]

        cases = {
            "population.codes:not_the_implemented_list": reorder,
            "top:keys_not_the_registered_set": lambda value: value.update(extra="x"),
            "id:not_supported": lambda value: value.update(id="inplay-v1"),
            "data.tiers:not_supported": lambda value: value["data"].update(tiers=["ADVANCED", "PRO"]),
            "data.traded_volume.divisor:not_the_basis": lambda value: value["data"]["traded_volume"].update(divisor=1),
            "data:keys_not_the_registered_set": lambda value: value["data"].pop("files"),
            "grid.cells:not_the_rule_files_times": lambda value: value["grid"].update(cells=5),
            "grid.trials:not_supported": lambda value: value["grid"].update(trials="pooled"),
            "fill_models.names:not_the_implemented_list":
                lambda value: value["fill_models"].update(names=["pessimistic", "central", "optimistic"]),
            "fill_models.kinds:not_the_implemented_list":
                lambda value: value["fill_models"].update(kinds=["volume_at_price", "quote_cross", "trade_through"]),
            "fill_models.queue_fraction:not_in_the_models_order":
                lambda value: value["fill_models"]["queue_fraction"].update(optimistic=[1, 1]),
            "fill_models.queue_fraction.central:not_an_int_in_range":
                lambda value: value["fill_models"]["queue_fraction"].update(central=[3, 2]),
            "fill_models.queue_fraction.central:not_a_numerator_and_denominator":
                lambda value: value["fill_models"]["queue_fraction"].update(central="0.5"),
            "measurement.classes:not_the_implemented_list":
                lambda value: value["measurement"]["classes"].remove("stream_malformed_after_placement"),
            "measurement.bsp_ends.step:not_supported":
                lambda value: value["measurement"]["bsp_ends"].update(step="0.001"),
            "measurement.bsp_ends.names:not_the_implemented_list":
                lambda value: value["measurement"]["bsp_ends"]["names"].reverse(),
            "measurement.commission_rate:not_a_decimal_rate":
                lambda value: value["measurement"].update(commission_rate=0.02),
            "family.m:not_one_primary_per_rule_file": lambda value: value["family"].update(m=6),
            "family.method:not_supported": lambda value: value["family"].update(method="bonferroni"),
            "exposure.cites:not_the_splits_prior_exposure": lambda value: value["exposure"]["cites"].remove("lp-v1"),
            "exposure.lines:keys_not_the_registered_set": lambda value: value["exposure"]["lines"].pop("p0"),
            "selection.explore_model:not_a_fill_model": lambda value: value["selection"].update(explore_model="mid"),
            "selection.measure:not_supported": lambda value: value["selection"].update(measure="net_closed"),
            "null.label:not_supported": lambda value: value["null"].update(label="close-v1/null/{market_id}"),
            "null.blocks:not_the_implemented_list": lambda value: value["null"]["blocks"].append("too_large"),
            "null.undefined_reasons:not_the_implemented_list":
                lambda value: value["null"]["undefined_reasons"].reverse(),
            "verdicts.names:not_the_implemented_list": lambda value: value["verdicts"]["names"].insert(0, "kill"),
            "verdicts.kill_level:not_above_one_half": lambda value: value["verdicts"].update(kill_level="0.025"),
            "statistics.quantiles:not_symmetric": lambda value: value["statistics"]["quantiles"].remove("0.95"),
            "statistics.min_tested_values:not_an_int_in_range":
                lambda value: value["statistics"].update(min_tested_values=50),
            "uncertainty.cluster:not_supported": lambda value: value["uncertainty"].update(cluster="market"),
            "diagnostics.trd_counts:not_the_implemented_list":
                lambda value: value["diagnostics"]["trd_counts"].pop(),
            "diagnostics.within_s:not_sorted_and_unique": lambda value: value["diagnostics"].update(within_s=[2, 1]),
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason):
                self.assertRefused(reason, registration=change)


# ---------------------------------------------------------------------------------------------------------------

class StandInView:
    """What Moment.active and view_of read of a replay.DecisionView: its runners and its definition fields."""

    def __init__(self, runners, status="OPEN", in_play=False):
        self.runners = tuple(SimpleNamespace(selection_id=sid, status=state, listed=listed)
                             for sid, state, listed in runners)
        self.fields = {"status": status, "inPlay": in_play}

    def field(self, name):
        return self.fields.get(name)


def moment_of(quotes, active=None, malformed=False, at_s=-300, **fields):
    """A Moment holding quotes {sid: (back, lay[, back size])}; active: the runners ACTIVE and listed (default every
    quoted runner). Another quoted runner is in the view as REMOVED."""
    rows = []
    for sid, (back, lay, *size) in sorted(quotes.items()):
        rows.append((sid, None if back is None else (exact(back), exact(size[0] if size else 50.0)),
                     None if lay is None else (exact(lay), exact(75.0))))
    active = sorted(quotes) if active is None else active
    view = StandInView([(sid, "ACTIVE" if sid in active else "REMOVED", True)
                        for sid in sorted(set(quotes) | set(active))], **fields)
    return Moment(offset_s=at_s, t=0, view=view, reason=None, quotes=tuple(rows), ladder_malformed=malformed)


class ViewOf(RestingCase):
    def test_the_view_holds_exactly_the_active_runners_with_exact_values(self):
        moment = moment_of(BOOK)
        view = view_of(moment, SIDS)
        self.assertIsInstance(view, RuleView)
        self.assertEqual((view.at_s, view.market_open, view.active_runners, view.ladder_malformed), (-300, 1, 6, False))
        self.assertEqual(view.runners[0], RunnerView(FAV, P(2.5), P(50.0), P(2.52), 1))
        self.assertEqual([item.selection_id for item in view.runners], list(SIDS))
        for item in view.runners:
            self.assertEqual({type(value) for value in item[1:4]}, {Fraction})
            self.assertEqual(item.spread_ticks, spread_ticks(item.best_back, item.best_lay))
        # A runner that is not ACTIVE keeps its quotes in the fold, but is not in the view, and does not count.
        moment = moment_of(BOOK, active=SIDS[1:])
        view = view_of(moment, SIDS[1:])
        self.assertEqual((view.active_runners, len(view.runners)), (5, 5))
        self.assertNotIn(FAV, [item.selection_id for item in view.runners])
        # An ACTIVE runner with no quote, or one side of one, has no value there.
        moment = moment_of({**BOOK, FAV: (None, None), R2: (4.0, None), R3: (None, 6.6)})
        view = view_of(moment, SIDS)
        self.assertEqual(view.runners[:3], (RunnerView(FAV, None, None, None, None),
                                            RunnerView(R2, P(4.0), P(50.0), None, None),
                                            RunnerView(R3, None, None, P(6.6), None)))
        self.assertEqual(view.active_runners, 6)
        unquoted = moment_of({FAV: (2.5, 2.52)}, active=[FAV, R2])
        self.assertEqual(view_of(unquoted, [R2, FAV]).runners[1], RunnerView(R2, None, None, None, None))

    def test_the_ids_must_be_the_moments_own_active_runners(self):
        moment = moment_of(BOOK, active=SIDS[1:])
        for given in (SIDS, SIDS[2:], (), SIDS[1:] + (99,), SIDS[1:] + (R2,)):
            with self.subTest(given=given), self.assertRaises(ValueError) as caught:
                view_of(moment, given)
            self.assertEqual(str(caught.exception), "view:active_runners_differ")
        self.assertEqual(view_of(moment, reversed(SIDS[1:])).active_runners, 5)
        # A runner the view lists as not listed is not active either.
        view = StandInView([(FAV, "ACTIVE", True), (R2, "ACTIVE", False)])
        unlisted = Moment(offset_s=-300, t=0, view=view, reason=None, quotes=(), ladder_malformed=False)
        self.assertEqual(view_of(unlisted, [FAV]).active_runners, 1)
        with self.assertRaises(ValueError):
            view_of(unlisted, [FAV, R2])
        without = Moment(offset_s=-300, t=0, view=None, reason="no_view", quotes=(), ladder_malformed=False)
        with self.assertRaises(ValueError) as caught:
            view_of(without, [])
        self.assertEqual(str(caught.exception), "view:no_view_at_time")

    def test_a_price_or_size_that_is_not_exact_is_refused(self):
        good = moment_of({FAV: (2.5, 2.52)})
        for position, value in ((1, (2.5, P(50.0))), (1, (P(2.5), 50)), (2, (2.52, P(75.0))),
                                (2, (P(2.52), "75"))):
            row = list(good.quotes[0])
            row[position] = value
            inexact = Moment(offset_s=-300, t=0, view=good.view, reason=None, quotes=(tuple(row),),
                             ladder_malformed=False)
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                view_of(inexact, [FAV])
            self.assertEqual(str(caught.exception), "view:not_exact")

    def test_market_open_spread_and_malformed(self):
        for fields, opened in (({}, 1), ({"status": "SUSPENDED"}, 0), ({"status": None}, 0), ({"in_play": True}, 0),
                               ({"in_play": None}, 1), ({"status": "CLOSED"}, 0)):
            with self.subTest(fields=fields):
                self.assertEqual(view_of(moment_of(BOOK, **fields), SIDS).market_open, opened)
        cases = {(2.5, 2.52): 1, (2.5, 2.54): 2, (2.5, 2.5): 0, (2.52, 2.5): -1, (2.51, 2.52): None,
                 (2.5, 2.53): None, (1.99, 2.02): 2, (2.5, 1000): 225}
        for quote, ticks in cases.items():
            with self.subTest(quote=quote):
                self.assertEqual(view_of(moment_of({FAV: quote}), [FAV]).runners[0].spread_ticks, ticks)
        self.assertTrue(view_of(moment_of(BOOK, malformed=True), SIDS).ladder_malformed)
        self.assertTrue(view_of(moment_of(BOOK), SIDS, malformed=True).ladder_malformed)
        self.assertEqual(view_of(moment_of(BOOK, at_s=-120), SIDS).at_s, -120)

    def test_the_view_of_a_replayed_moment(self):
        # R6 is removed at off - 400 s; its quotes stay in the ladder fold, and the favourite's book moves.
        removal = md(M, before(400), definition(OFF, [runner(s, "REMOVED" if s == R6 else "ACTIVE") for s in SIDS]))
        statuses = {s: "REMOVED" if s == R6 else "WINNER" if s == FAV else "LOSER" for s in SIDS}
        messages = lp_market(books={3600: BOOK, 200: book(FAV=(2.44, 2.48, 12.5))}, extra=[removal],
                             statuses=statuses)
        root, manifest, records = self.build([(path(M), messages)])
        record = records[M]
        with member_stream(root, manifest, record) as stream:
            replayed = replay_market(stream, record, ("atb", "atl"), (-600, -300, -120), ms(OFF), DEFAULT_LIMITS)
        expected = {-600: (6, RunnerView(FAV, P(2.5), P(50.0), P(2.52), 1)),
                    -300: (5, RunnerView(FAV, P(2.5), P(50.0), P(2.52), 1)),
                    -120: (5, RunnerView(FAV, P(2.44), P(12.5), P(2.48), 2))}
        for at_s, (count, favourite) in expected.items():
            moment = replayed.moments[at_s]
            active = active_ids(record, decision_time(record, at_s))
            view = view_of(moment, active)
            with self.subTest(at_s=at_s):
                self.assertEqual(active, moment.active())
                self.assertEqual((view.at_s, view.market_open, view.active_runners, len(view.runners)),
                                 (at_s, 1, count, count))
                self.assertEqual(view.runners[0], favourite)
                self.assertEqual({item.selection_id for item in view.runners}, set(moment.active()))
                self.assertIn(R6, [rid for rid, _, _ in moment.quotes])
                for item in view.runners:
                    back, lay = moment.quote(item.selection_id)
                    self.assertEqual((item.best_back, item.best_back_size, item.best_lay), (*back, lay[0]))
        with self.assertRaises(ValueError):
            view_of(replayed.moments[-300], SIDS)


class RuleMatchesMovementSelect(RestingCase):
    """The two rule files accept exactly the runner movement.select accepts (movement-v1's favourite gates), with
    the same favourite; where movement names several reasons the rule files have one row."""

    BACK_FILE, LAY_FILE = (PLAN.rule(name) for name in (BACK_RULE, LAY_RULE))

    def compare(self, moment, active, movement_code, rule_code):
        code, selection = select(MOVEMENT_REG, active, moment)
        self.assertEqual(code, movement_code)
        view = view_of(moment, active)
        for rule, side in ((self.BACK_FILE, BACK), (self.LAY_FILE, LAY)):
            decision = decide(view, rule)
            self.assertEqual(decision.code, rule_code)
            if selection is None:
                self.assertEqual(decision.intents, ())
                continue
            (intent,) = decision.intents
            self.assertEqual((intent.selection_id, intent.side), (selection.selection_id, side))
            self.assertEqual(intent.price, selection.lay[0] if side == BACK else selection.back[0])

    def test_the_same_runner_or_none_on_hand_built_books(self):
        cases = (
            ("standard", BOOK, None, None),
            ("two ticks", book(FAV=(2.5, 2.54)), None, None),
            ("the band's ends", book(FAV=(1.5, 1.51)), None, None),
            ("the band's top", book(FAV=(4.0, 4.1), R2=(4.1, 4.2)), None, None),
            ("exactly GBP 10", book(FAV=(2.5, 2.52, 10.0)), None, None),
            ("another runner is the favourite", book(FAV=(5.0, 5.1), R2=(3.0, 3.05)), None, None),
            ("a tie", book(R2=(2.5, 2.52)), "favourite_tie", "favourite_tie"),
            ("nobody priced", {sid: (None, lay) for sid, (_, lay) in BOOK.items()}, "no_priced_active_runner",
             "no_priced_active_runner"),
            ("too short", book(FAV=(1.49, 1.5)), "best_back_out_of_range", "best_back_out_of_range"),
            ("too long", book(FAV=(4.1, 4.2), R2=(4.2, 4.3)), "best_back_out_of_range", "best_back_out_of_range"),
            ("thin", book(FAV=(2.5, 2.52, 9.99)), "depth_below_min", "depth_below_min"),
            ("back off the ladder", book(FAV=(2.51, 2.52)), "best_back_off_ladder", "spread_not_1_or_2_ticks"),
            ("no lay", book(FAV=(2.5, None)), "best_lay_missing", "spread_not_1_or_2_ticks"),
            ("crossed", book(FAV=(2.5, 2.5)), "book_crossed", "spread_not_1_or_2_ticks"),
            ("lay off the ladder", book(FAV=(2.5, 2.53)), "best_lay_off_ladder", "spread_not_1_or_2_ticks"),
            ("three ticks", book(FAV=(2.5, 2.56)), "spread_too_wide", "spread_not_1_or_2_ticks"),
        )
        for name, quotes, movement_code, rule_code in cases:
            with self.subTest(case=name):
                self.compare(moment_of(quotes), SIDS, movement_code, rule_code)
        self.compare(moment_of(BOOK, malformed=True), SIDS, "ladder_malformed", "ladder_malformed")
        # The lowest best back belongs to a runner that is not ACTIVE: both pass over it.
        self.compare(moment_of(book(R6=(1.6, 1.61)), active=SIDS[:5]), SIDS[:5], None, None)
        selection = select(MOVEMENT_REG, SIDS[:5], moment_of(book(R6=(1.6, 1.61)), active=SIDS[:5]))[1]
        self.assertEqual(selection.selection_id, FAV)

    def test_the_same_runner_on_replayed_markets(self):
        fixtures = (lp_market(), lp_market(books={3600: BOOK, 400: book(FAV=(3.0, 3.05), R2=(2.9, 2.92)),
                                                  200: book(FAV=(2.9, 2.92), R2=(2.9, 2.96))}),
                    lp_market(books={3600: book(FAV=(2.5, 2.56)), 200: book(FAV=(2.5, 2.52, 4.0))}))
        expected = ({-600: FAV, -300: FAV, -120: FAV}, {-600: FAV, -300: R2, -120: None},
                    {-600: None, -300: None, -120: None})
        for index, (messages, chosen) in enumerate(zip(fixtures, expected)):
            root, manifest, records = self.build([(path(M), messages)])
            record = records[M]
            with member_stream(root, manifest, record) as stream:
                replayed = replay_market(stream, record, ("atb", "atl"), (-600, -300, -120), ms(OFF),
                                         DEFAULT_LIMITS)
            with member_stream(root, manifest, record) as stream:
                outcomes = walk(stream, record, PLAN, DEFAULT_LIMITS).outcomes
            for at_s, sid in chosen.items():
                active = active_ids(record, decision_time(record, at_s))
                code, selection = select(MOVEMENT_REG, active, replayed.moments[at_s])
                with self.subTest(fixture=index, at_s=at_s):
                    self.assertEqual(None if selection is None else selection.selection_id, sid)
                    self.compare(replayed.moments[at_s], active, code,
                                 code if code in (None, "favourite_tie", "depth_below_min")
                                 else "spread_not_1_or_2_ticks")
                    # The walk's own decision: an order at the selected runner's prices, or a code.
                    for cell in (BACK_CELLS + LAY_CELLS):
                        if cell.at_s != at_s:
                            continue
                        row = outcomes[cell]
                        if selection is None:
                            self.assertIsInstance(row, str)
                        else:
                            self.assertEqual(row.price, selection.lay[0] if row.side == BACK else selection.back[0])


# ---------------------------------------------------------------------------------------------------------------

def leg(side, price=2.5, queue=0, placed=1000, cancel=5000, base=None):
    return Leg(-300, PESSIMISTIC, side, FAV, P(price), Fraction(queue), placed, cancel, base or {})


class FillJudge(RestingCase):
    def judge(self, order, counted=(), at_price=0, shown=None, stake=2):
        return fills.judge(order, Fraction(stake), 2, {P(price) for price in counted}, Fraction(at_price),
                           None if shown is None else Fraction(shown))

    def test_each_kind(self):
        self.assertEqual(fills.MODELS, ("optimistic", "central", "pessimistic"))
        lay, back = leg(LAY), leg(BACK, 2.52)
        self.assertIsNone(self.judge(lay))
        # A counted rise below a LAY, or above a BACK, is a trade through it, whatever its size.
        self.assertEqual(self.judge(lay, counted=[2.48]), THROUGH)
        self.assertEqual(self.judge(back, counted=[2.54]), THROUGH)
        self.assertIsNone(self.judge(lay, counted=[2.52]))
        self.assertIsNone(self.judge(back, counted=[2.5]))
        self.assertEqual(self.judge(lay, shown=2), CROSS)
        self.assertEqual(self.judge(lay, counted=[2.5], at_price=4), VOLUME)
        self.assertEqual(self.judge(back, counted=[2.52], at_price=4), VOLUME)

    def test_the_precedence_is_lps(self):
        lay = leg(LAY)
        self.assertEqual(FILL_KINDS, (THROUGH, CROSS, VOLUME))
        self.assertEqual(self.judge(lay, counted=[2.48, 2.5], at_price=4, shown=2), THROUGH)
        self.assertEqual(self.judge(lay, counted=[2.5], at_price=4, shown=2), CROSS)
        self.assertEqual(self.judge(lay, counted=[2.5], at_price=4, shown=1.99), VOLUME)

    def test_two_pounds_needs_a_rise_of_4_at_the_front_and_twice_queue_plus_2_behind_a_queue(self):
        front = leg(LAY)
        self.assertIsNone(self.judge(front, counted=[2.5], at_price=3.98))
        self.assertEqual(self.judge(front, counted=[2.5], at_price=4), VOLUME)
        for queue, need in ((25, 54), (50, 104), (37.5, 79)):
            behind = leg(LAY, queue=queue)
            with self.subTest(queue=queue):
                self.assertIsNone(self.judge(behind, counted=[2.5], at_price=need - 0.02))
                self.assertEqual(self.judge(behind, counted=[2.5], at_price=need), VOLUME)
        # Only the rise since placement counts: the leg's base is the counted volume at t.
        based = leg(LAY, base={P(2.5): Fraction(10)})
        self.assertIsNone(self.judge(based, counted=[2.5], at_price=13.98))
        self.assertEqual(self.judge(based, counted=[2.5], at_price=14), VOLUME)
        # The volume must rise in this batch: an old total fills nothing in a batch that counts nothing there.
        self.assertIsNone(self.judge(front, counted=[], at_price=400))
        self.assertIsNone(self.judge(front, counted=[2.52], at_price=400))
        self.assertEqual(fills.queue_ahead(Fraction(1, 2), Fraction(75)), Fraction(75, 2))
        self.assertEqual(fills.queue_ahead(Fraction(0), Fraction(75)), 0)

    def test_a_cross_needs_the_stake_not_the_queue(self):
        behind = leg(LAY, queue=50)
        self.assertIsNone(self.judge(behind, shown=1.99))
        self.assertEqual(self.judge(behind, shown=2), CROSS)                    # lp-v1 would ask for 52
        self.assertIsNone(self.judge(behind, shown=None))
        self.assertIsNone(self.judge(behind, shown=0))
        self.assertEqual(self.judge(leg(LAY, queue=50), shown=4, stake=4), CROSS)
        self.assertIsNone(self.judge(leg(LAY, queue=50), shown=3.99, stake=4))

    def test_when_a_leg_is_judged(self):
        order = leg(LAY, placed=1000, cancel=5000)
        self.assertFalse(fills.judged(order, 999, True, False, "lapse"))
        self.assertFalse(fills.judged(order, 1000, True, False, "lapse"))       # the batch at placement
        self.assertTrue(fills.judged(order, 1001, True, False, "lapse"))
        self.assertTrue(fills.judged(order, 5000, True, False, "lapse"))        # the batch at the pull time
        self.assertFalse(fills.judged(order, 5001, True, False, "lapse"))
        self.assertFalse(fills.judged(order, 2000, False, False, "lapse"))      # a batch that is not open
        self.assertFalse(fills.judged(order, 2000, True, True, "lapse"))        # in play before the batch
        self.assertTrue(fills.judged(order, 2000, True, True, "keep"))
        order.fill_pt = 1500
        self.assertFalse(fills.judged(order, 2000, True, False, "lapse"))       # one fill
        self.assertEqual([fills.is_open(status) for status in ("OPEN", "SUSPENDED", "CLOSED", None)],
                         [True, False, False, False])

    def test_nothing_at_or_before_placement_fills_through_the_walk(self):
        # A trade through both orders' prices in the batch at t is in the view; the order placed then never sees it.
        for seconds, cells in ((600, []), (300, [-600]), (599.999, [-600])):
            with self.subTest(seconds=seconds):
                found = self.filled(lp_market(trades=[(seconds, FAV, [[2.48, 1.0], [2.54, 1.0]])]))
                self.assertEqual(sorted(found), sorted((side, at_s) for side in (BACK, LAY) for at_s in cells))
                self.assertEqual({kind for row in found.values() for kind in row.values()},
                                 {THROUGH} if cells else set())
        # Volume traded before t is in the base: 4 more is needed after it.
        found = self.filled(lp_market(trades=[(900, FAV, [[2.5, 10.0]]), (200, FAV, [[2.5, 13.98]])]))
        self.assertEqual(found, {})
        found = self.filled(lp_market(trades=[(900, FAV, [[2.5, 10.0]]), (200, FAV, [[2.5, 14.0]])]))
        self.assertEqual(found, {(LAY, at_s): {**UNFILLED, OPTIMISTIC: VOLUME} for at_s in (-600, -300)})

    def test_the_three_queues_through_the_walk(self):
        rows = self.rows(lp_market())
        for cell in LAY_CELLS:
            self.assertEqual((rows[cell].side, rows[cell].price), (LAY, P(2.5)))
            self.assertEqual([rows[cell].fills[model].queue for model in MODELS], [0, 25, 50])
        for cell in BACK_CELLS:
            self.assertEqual((rows[cell].side, rows[cell].price), (BACK, P(2.52)))
            self.assertEqual([rows[cell].fills[model].queue for model in MODELS], [0, Fraction(75, 2), 75])
        # GBP 27 of backer's stake at 2.5 (a rise of 54) passes the front and half the queue, not all of it.
        found = self.filled(lp_market(trades=[(200, FAV, [[2.5, 54.0]])]))
        self.assertEqual(found[LAY, -300], {OPTIMISTIC: VOLUME, CENTRAL: VOLUME, PESSIMISTIC: None})
        found = self.filled(lp_market(trades=[(200, FAV, [[2.5, 103.98]])]))
        self.assertEqual(found[LAY, -300], {OPTIMISTIC: VOLUME, CENTRAL: VOLUME, PESSIMISTIC: None})
        found = self.filled(lp_market(trades=[(200, FAV, [[2.5, 104.0]])]))
        self.assertEqual(found, {(LAY, at_s): dict.fromkeys(MODELS, VOLUME) for at_s in (-600, -300)})
        found = self.filled(lp_market(trades=[(200, FAV, [[2.52, 79.0]])]))
        self.assertEqual(found, {(BACK, at_s): {OPTIMISTIC: VOLUME, CENTRAL: VOLUME, PESSIMISTIC: None}
                                 for at_s in (-600, -300)})
        # Each model fills at its own batch: the front at the first GBP 2, the others later.
        rows = self.rows(lp_market(trades=[(250, FAV, [[2.5, 4.0]]), (200, FAV, [[2.5, 60.0]]),
                                           (150, FAV, [[2.5, 104.0]])]))
        self.assertEqual([rows[LAY_CELLS[1]].fills[model].seconds for model in MODELS], [50.0, 100.0, 150.0])
        # Volume and a trade through at the same prices on another runner fill nothing.
        self.assertEqual(self.filled(lp_market(trades=[(200, R2, [[2.5, 400.0], [2.48, 1.0]])])), {})


def suspended(seconds, status, rc, market_id=M):
    """One change at `seconds` before the off: a definition with the status and the favourite's rc entry."""
    change = {"id": market_id, "marketDefinition": definition(OFF, [runner(s) for s in SIDS], status=status),
              "rc": [{"id": FAV, **rc}]}
    return mcm(before(seconds), [change])


class OpenBatches(RestingCase):
    THROUGH_LAY = {"atb": [[2.48, 0]], "trd": [[2.48, 1.0]]}

    def test_the_batch_that_suspends_credits_its_trades(self):
        rows = self.rows(lp_market(extra=[suspended(200, "SUSPENDED", self.THROUGH_LAY)]))
        for cell, seconds in zip(LAY_CELLS[:2], (400.0, 100.0)):
            row = rows[cell]
            self.assertEqual(kinds(row), dict.fromkeys(MODELS, THROUGH))
            self.assertEqual([(fill.seconds, fill.transition, fill.ended) for fill in row.fills.values()],
                             [(seconds, True, None)] * 3)
            self.assertEqual(row.trd, dict(zip(TRD_COUNTS, (1, 0, 0))))
            self.assertEqual(row.klass, SCORED)                                 # a later suspension drops no order
        self.assertEqual(rows[LAY_CELLS[2]], "market_not_open")
        # The same trade in a batch that leaves the market OPEN is no transition fill.
        rows = self.rows(lp_market(extra=[suspended(200, "OPEN", self.THROUGH_LAY)]))
        self.assertEqual([rows[LAY_CELLS[0]].fills[model].transition for model in MODELS], [False] * 3)

    def test_the_batch_that_reopens_does_not(self):
        extra = [status_at(250, "SUSPENDED"), suspended(200, "OPEN", self.THROUGH_LAY)]
        rows = self.rows(lp_market(extra=extra))
        for cell in LAY_CELLS[:2]:
            self.assertEqual(kinds(rows[cell]), UNFILLED)
            self.assertEqual(rows[cell].trd, dict(zip(TRD_COUNTS, (0, 0, 1))))
            self.assertEqual({fill.ended for fill in rows[cell].fills.values()}, {"pull"})
        self.assertEqual(kinds(rows[LAY_CELLS[2]]), UNFILLED)                   # placed after it, market OPEN again
        # Nor does a trade published while the market stays suspended.
        extra = [status_at(250, "SUSPENDED"), suspended(200, "SUSPENDED", self.THROUGH_LAY), status_at(150)]
        self.assertEqual(self.filled(lp_market(extra=extra)), {})

    def test_a_rise_in_a_batch_that_is_not_open_is_never_counted_later(self):
        at_touch = {"atb": [[2.5, 50.0]]}
        closed = [status_at(250, "SUSPENDED"), suspended(200, "OPEN", {**at_touch, "trd": [[2.5, 4.0]]})]
        # Re-sending the same volume in an open batch is no rise, and 2 more is 2, not 6.
        later = [ladder_move(150, atb=[[2.5, 50.0]], trd=[[2.5, 4.0]], sid=FAV),
                 ladder_move(140, atb=[[2.5, 50.0]], trd=[[2.5, 6.0]], sid=FAV)]
        rows = self.rows(lp_market(extra=closed + later))
        self.assertEqual(kinds(rows[LAY_CELLS[1]]), UNFILLED)
        self.assertEqual(rows[LAY_CELLS[1]].trd, dict(zip(TRD_COUNTS, (1, 0, 1))))
        rows = self.rows(lp_market(extra=closed + later + [ladder_move(130, atb=[[2.5, 50.0]], trd=[[2.5, 8.0]],
                                                                       sid=FAV)]))
        self.assertEqual(kinds(rows[LAY_CELLS[1]]), {**UNFILLED, OPTIMISTIC: VOLUME})
        self.assertEqual(rows[LAY_CELLS[1]].fills[OPTIMISTIC].seconds, 170.0)
        # Had the market been open throughout, the first GBP 2 would have filled the front of the queue.
        opened = [ladder_move(200, atb=[[2.5, 50.0]], trd=[[2.5, 4.0]], sid=FAV)]
        self.assertEqual(self.rows(lp_market(extra=opened))[LAY_CELLS[1]].fills[OPTIMISTIC].seconds, 100.0)

    def test_a_cross_shown_in_a_batch_that_is_not_open_fills_at_the_next_open_batch_that_shows_it(self):
        crossing = {"atb": [[2.5, 0], [2.48, 50.0]], "atl": [[2.52, 0], [2.5, 10.0]]}
        extra = [status_at(250, "SUSPENDED"), suspended(200, "OPEN", crossing)]
        self.assertEqual(self.filled(lp_market(extra=extra)), {})               # nothing reads the ladder again
        touch = ladder_move(150, atb=[[2.46, 5.0]], sid=FAV)
        rows = self.rows(lp_market(extra=extra + [touch]))
        self.assertEqual(kinds(rows[LAY_CELLS[1]]), dict.fromkeys(MODELS, CROSS))
        self.assertEqual(rows[LAY_CELLS[1]].fills[PESSIMISTIC].seconds, 150.0)
        # A change to another runner's ladder does not read the favourite's.
        other = ladder_move(150, atb=[[4.0, 40.0]], sid=R2)
        self.assertEqual(self.filled(lp_market(extra=extra + [other])), {})

    def test_a_quote_cross_on_the_back_side(self):
        # The best back comes up to the BACK's price of 2.52 with GBP 2 shown: a cross under every model.
        def crossing(size):
            return ladder_move(200, atl=[[2.52, 0], [2.56, 75.0]], atb=[[2.52, size]], sid=FAV)

        rows = self.rows(lp_market(extra=[crossing(2.0)]))
        for cell, seconds in zip(BACK_CELLS[:2], (400.0, 100.0)):
            self.assertEqual(kinds(rows[cell]), dict.fromkeys(MODELS, CROSS))
            self.assertEqual({fill.seconds for fill in rows[cell].fills.values()}, {seconds})
            self.assertEqual(kinds(rows[LAY_CELLS[cell.at_s == -300]]), UNFILLED)     # nothing reached the LAY
        self.assertEqual(self.filled(lp_market(extra=[crossing(1.99)])), {})          # a fill is all or nothing
        # Through the BACK's price is a cross too: a best back of 2.54.
        through = ladder_move(200, atl=[[2.52, 0], [2.56, 75.0]], atb=[[2.54, 2.0]], sid=FAV)
        self.assertEqual(self.filled(lp_market(extra=[through]))[BACK, -300], dict.fromkeys(MODELS, CROSS))
        back = leg(BACK, 2.52, queue=75)
        self.assertEqual([fills.judge(back, Fraction(2), 2, set(), Fraction(0), shown)
                          for shown in (None, Fraction(199, 100), Fraction(2))], [None, None, CROSS])

    def test_a_batch_begun_with_no_status_is_not_open_and_is_counted(self):
        trade = ladder_move(200, atb=[[2.48, 0]], trd=[[2.48, 1.0]], sid=FAV)
        # A definition that carries no status at off - 250 s: from then on the market has no status at all. The
        # order stays scored, nothing can fill it, and it is marked.
        blank = status_at(250, status=fx.DROP)
        rows = self.rows(lp_market(extra=[blank, trade]))
        for cell in LAY_CELLS[:2] + BACK_CELLS[:2]:
            row = rows[cell]
            self.assertEqual((kinds(row), row.no_status, row.klass), (UNFILLED, True, SCORED))
            self.assertEqual({fill.ended for fill in row.fills.values()}, {"pull"})
        self.assertEqual(rows[LAY_CELLS[1]].trd, dict(zip(TRD_COUNTS, (0, 0, 1))))
        self.assertEqual(rows[LAY_CELLS[2]], "market_not_open")
        # An image with no definition does the same to the status. It also unlists every runner, so the order's
        # outcome cannot be assessed: it is marked and it is not scored.
        image = mcm(before(250), [{"id": M, "img": True, "rc": [
            {"id": sid, "atb": [[back, 50.0]], "atl": [[lay, 75.0]]} for sid, (back, lay) in BOOK.items()]}])
        rows = self.rows(lp_market(extra=[image, trade]))
        self.assertEqual((kinds(rows[LAY_CELLS[1]]), rows[LAY_CELLS[1]].no_status, rows[LAY_CELLS[1]].klass),
                         (UNFILLED, True, "outcome_unknown"))
        # The batch that takes the status away began OPEN: with nothing after it in the window, no order is marked,
        # and a trade it carries itself still fills.
        rows = self.rows(lp_market(extra=[status_at(120, status=fx.DROP)]))
        self.assertEqual([rows[cell].no_status for cell in LAY_CELLS[:2]], [False, False])
        rows = self.rows(lp_market(extra=[suspended(200, fx.DROP, self.THROUGH_LAY)]))
        self.assertEqual((kinds(rows[LAY_CELLS[1]]), rows[LAY_CELLS[1]].no_status),
                         (dict.fromkeys(MODELS, THROUGH), False))
        # A real suspension is not this: the status is known.
        rows = self.rows(lp_market(extra=[status_at(250, "SUSPENDED"), suspended(200, "SUSPENDED", self.THROUGH_LAY)]))
        self.assertEqual((rows[LAY_CELLS[1]].no_status, rows[LAY_CELLS[1]].trd),
                         (False, dict(zip(TRD_COUNTS, (0, 0, 1)))))
        self.assertEqual({row.no_status for row in self.rows(lp_market()).values()}, {False})


class OnePtOneBatch(RestingCase):
    """Messages that share a pt are one batch, and what was in force before its first message decides whether it
    is open and whether a lapse order is still judged. Every case here splits one pt over two or three messages."""

    TRADE = dict(atb=[[2.48, 0]], trd=[[2.48, 1.0]], sid=FAV)                   # a trade through the LAY at 2.5

    def test_the_suspension_then_the_trade_at_one_pt_is_an_open_batch(self):
        rows = self.rows(lp_market(extra=[status_at(200, "SUSPENDED"), ladder_move(200, **self.TRADE)]))
        for cell, seconds in zip(LAY_CELLS[:2], (400.0, 100.0)):
            self.assertEqual(kinds(rows[cell]), dict.fromkeys(MODELS, THROUGH))
            self.assertEqual([(fill.seconds, fill.transition, fill.turn) for fill in rows[cell].fills.values()],
                             [(seconds, True, False)] * 3)
            self.assertEqual(rows[cell].trd, dict(zip(TRD_COUNTS, (1, 0, 0))))
        self.assertEqual(kinds(rows[BACK_CELLS[1]]), UNFILLED)

    def test_the_reopening_then_the_trade_at_one_pt_is_not_an_open_batch(self):
        extra = [status_at(250, "SUSPENDED"), status_at(200), ladder_move(200, **self.TRADE)]
        rows = self.rows(lp_market(extra=extra))
        for cell in LAY_CELLS[:2]:
            self.assertEqual(kinds(rows[cell]), UNFILLED)
            self.assertEqual(rows[cell].trd, dict(zip(TRD_COUNTS, (0, 0, 1))))
        # One ms later the market was OPEN before the batch, and the same trade fills.
        extra = [status_at(250, "SUSPENDED"), status_at(200), ladder_move(199.999, **self.TRADE)]
        self.assertEqual(self.filled(lp_market(extra=extra))[LAY, -300], dict.fromkeys(MODELS, THROUGH))

    def test_a_rise_and_its_ladder_entry_in_two_messages_of_one_pt_count_together(self):
        rise, entry = ladder_move(200, trd=[[2.48, 1.0]], sid=FAV), ladder_move(200, atb=[[2.48, 0]], sid=FAV)
        for extra in ([rise, entry], [entry, rise]):
            found = self.filled(lp_market(extra=extra))
            self.assertEqual(found, {(LAY, at_s): dict.fromkeys(MODELS, THROUGH) for at_s in (-600, -300)})
        self.assertEqual(self.filled(lp_market(extra=[rise])), {})              # the rise alone is not a trade
        # The entry 1 ms later is another batch: the rise is not counted then, nor later.
        late = ladder_move(199.999, atb=[[2.48, 0]], sid=FAV)
        self.assertEqual(self.filled(lp_market(extra=[rise, late])), {})

    def test_the_in_play_turn_then_the_trade_at_one_pt_is_still_judged(self):
        rows = self.rows(lp_market(extra=[status_at(200, in_play=True), ladder_move(200, **self.TRADE)]))
        for cell in LAY_CELLS[:2]:
            self.assertEqual(kinds(rows[cell]), dict.fromkeys(MODELS, THROUGH))
            self.assertEqual([(fill.transition, fill.turn) for fill in rows[cell].fills.values()], [(False, True)] * 3)
        self.assertEqual({fill.ended for fill in rows[BACK_CELLS[1]].fills.values()}, {"lapse"})
        # The same trade 1 ms later: inPlay was true before its batch, so the lapse order is no longer judged.
        rows = self.rows(lp_market(extra=[status_at(200, in_play=True), ladder_move(199.999, **self.TRADE)]))
        for cell in LAY_CELLS[:2]:
            self.assertEqual(kinds(rows[cell]), UNFILLED)
            self.assertEqual({fill.ended for fill in rows[cell].fills.values()}, {"lapse"})
            self.assertEqual(rows[cell].trd, dict(zip(TRD_COUNTS, (1, 0, 0))))    # the market was OPEN: a trade

    def test_in_play_published_true_then_false_at_one_pt_leaves_the_order_resting(self):
        """What ended an unfilled order is read from the value in force after each batch, as the walk reads it."""
        both = [status_at(200, in_play=True), status_at(200)]
        rows = self.rows(lp_market(extra=both, trades=[(150, FAV, [[2.48, 1.0]])]))
        for cell in LAY_CELLS[:2]:
            self.assertEqual(kinds(rows[cell]), dict.fromkeys(MODELS, THROUGH))       # judged to the pull
            self.assertEqual({fill.ended for fill in rows[BACK_CELLS[1]].fills.values()}, {"pull"})
        # False then true at one pt leaves it true: the order lapsed.
        rows = self.rows(lp_market(extra=both[::-1], trades=[(150, FAV, [[2.48, 1.0]])]))
        self.assertEqual(kinds(rows[LAY_CELLS[1]]), UNFILLED)
        self.assertEqual({fill.ended for fill in rows[LAY_CELLS[1]].fills.values()}, {"lapse"})


def lp_like(rule):
    """A rule file changed to lp-v1's shape: any spread up to 20 ticks, every order pulled at off - 10 s."""
    rule["when"][-1]["value"] = 20
    for row in rule["times"]:
        row["pull_s"] = -10


class AgreesWithLp(RestingCase):
    """The pessimistic model is lp-v1's join rule but for three things. On markets that stay OPEN and out of play,
    where lp-v1 selects the favourite (a five-tick spread here), its join legs and this walk's pessimistic legs
    fill in the same batch by the same kind, unless a cross shows at least the stake but less than queue + stake.
    The other two differences: lp-v1's walk credits a fill in a batch that is not open, and after the market has
    turned in play; this walk does neither (lp-v1 classed such a market out afterwards instead)."""

    WIDE = {3600: book(FAV=(2.5, 2.6))}
    LP_PLAN = plan_of(rules={BACK_RULE: lp_like, LAY_RULE: lp_like})

    def both(self, messages, times=(-600, -300, -120)):
        """({(side, at_s): (kind, seconds)} of lp's join legs, the same of this walk's pessimistic legs), over
        those of the decision times at which lp selected the favourite."""
        root, manifest, records = self.build([(path(M), messages)])
        record = records[M]
        lp = lp_walk(root, manifest, record, LP_REG, [-600, -300, -120], DEFAULT_LIMITS)
        times = [at_s for at_s, (code, selection) in lp.selections.items()
                 if at_s in times and selection is not None and selection.selection_id == FAV]
        ours, theirs = {}, {}
        for item in lp.legs:
            if item.role == JOIN and item.offset_s in times:
                theirs[item.side, item.offset_s] = (item.fill_kind, None if item.fill_pt is None
                                                    else (item.fill_pt - item.placed) / 1000)
        with member_stream(root, manifest, record) as stream:
            outcomes = walk(stream, record, self.LP_PLAN, DEFAULT_LIMITS).outcomes
        for cell, row in outcomes.items():
            if cell.at_s in times:
                self.assertEqual(row.fills[PESSIMISTIC].queue, {LAY: 50, BACK: 75}[row.side])
                ours[row.side, cell.at_s] = (row.fills[PESSIMISTIC].kind, row.fills[PESSIMISTIC].seconds)
        return theirs, ours

    def test_the_same_batch_and_kind_as_lps_join_legs(self):
        cross = ladder_move(200, atb=[[2.5, 0], [2.48, 50.0]], atl=[[2.6, 0], [2.5, 52.0]], sid=FAV)
        fixtures = {
            "volume": dict(trades=[(200, FAV, [[2.5, 104.0]]), (100, FAV, [[2.6, 154.0]])]),
            "through": dict(trades=[(250, FAV, [[2.5, 103.98]]), (200, FAV, [[2.48, 0.5]]),
                                    (90, FAV, [[2.62, 0.02]])]),
            "a cross of queue + stake": dict(extra=[cross]),
            "a restatement between trades": dict(
                trades=[(900, FAV, [[2.5, 10.0]]), (400, FAV, [[2.5, 60.0]]), (200, FAV, [[2.5, 114.0]]),
                        (100, FAV, [[2.5, 124.0]])], restatements=[(300, FAV, [[2.5, 70.0]])]),
            "nothing": {},
        }
        seen = set()
        for name, changes in fixtures.items():
            theirs, ours = self.both(lp_market(books=self.WIDE, **changes))
            with self.subTest(fixture=name):
                self.assertEqual(ours, theirs)
                self.assertGreaterEqual(len(ours), 4)
            seen |= {kind for kind, _ in ours.values()}
        self.assertEqual(seen, {None, THROUGH, CROSS, VOLUME})
        theirs, _ = self.both(lp_market(books=self.WIDE, **fixtures["a restatement between trades"]))
        self.assertEqual(theirs[LAY, -600], (VOLUME, 500.0))

    def test_a_cross_below_queue_plus_stake_fills_here_and_not_in_lp(self):
        cross = ladder_move(200, atb=[[2.5, 0], [2.48, 50.0]], atl=[[2.6, 0], [2.5, 10.0]], sid=FAV)
        theirs, ours = self.both(lp_market(books=self.WIDE, extra=[cross]))
        for at_s, seconds in ((-600, 400.0), (-300, 100.0)):
            self.assertEqual(theirs[LAY, at_s], (None, None))                   # lp-v1: 10 shown, 52 needed
            self.assertEqual(ours[LAY, at_s], (CROSS, seconds))
            self.assertEqual(theirs[BACK, at_s], ours[BACK, at_s])

    def test_lp_fills_in_a_batch_that_is_not_open_and_after_an_in_play_turn_and_this_walk_does_not(self):
        through = {"atb": [[2.48, 0]], "trd": [[2.48, 1.0]]}
        fixtures = {
            "the batch that reopens the market": [status_at(250, "SUSPENDED"), suspended(200, "OPEN", through)],
            "a batch after the in-play turn": [status_at(200, in_play=True),
                                               ladder_move(150, sid=FAV, **through)],
        }
        for name, extra in fixtures.items():
            theirs, ours = self.both(lp_market(books=self.WIDE, extra=extra), times=(-600, -300))
            with self.subTest(fixture=name):
                for at_s, seconds in ((-600, 400.0 if "reopens" in name else 450.0),
                                      (-300, 100.0 if "reopens" in name else 150.0)):
                    self.assertEqual(theirs[LAY, at_s], (THROUGH, seconds))     # lp-v1's walk has neither check
                    self.assertEqual(ours[LAY, at_s], (None, None))
                    self.assertEqual(theirs[BACK, at_s], ours[BACK, at_s])


# ---------------------------------------------------------------------------------------------------------------

class Unread:
    """A stream that must not be read."""

    def readline(self, size):
        raise AssertionError("the member was read")


class PopulationCodes(RestingCase):
    def codes(self, messages, **kwargs):
        walked = self.walked(messages, **kwargs)
        self.assertEqual(set(walked.outcomes), set(PLAN.cells))
        return walked

    def test_the_standard_market_issues_one_order_per_cell(self):
        walked = self.codes(lp_market())
        self.assertTrue(walked.replayed)
        self.assertEqual(walked.flags_missing, dict.fromkeys(PLAN.cells, ()))
        for cell, row in walked.outcomes.items():
            self.assertIsInstance(row, OrderRow)
            self.assertEqual((row.label, row.half, row.race_date, row.klass, row.bsp),
                             ("PRO-V", "H1", "2015-06-01", SCORED, P(2.6)))
            self.assertEqual((row.mid, kinds(row)), (P(2.51), UNFILLED))
            self.assertEqual({fill.ended for fill in row.fills.values()}, {"pull"})
            self.assertEqual(row.trd, dict.fromkeys(TRD_COUNTS, 0))
        self.assertEqual(self.rows(lp_market(off=OFF + timedelta(days=1)),
                                   when=OFF + timedelta(days=1))[LAY_CELLS[0]].label, "PRO-D")
        winter = datetime(2015, 12, 1, 14, 0, tzinfo=timezone.utc)
        row = self.rows(lp_market(off=winter), when=winter)[LAY_CELLS[0]]
        self.assertEqual((row.label, row.half, row.race_date), ("PRO-V", "H2", "2015-12-01"))

    def test_a_row_holds_no_market_or_selection_id(self):
        self.assertEqual(OrderRow._fields, ("side", "price", "label", "half", "race_date", "klass", "bsp", "mid",
                                            "fills", "null_mid", "null_reason", "trd", "no_status"))
        self.assertEqual(Fill._fields, ("queue", "kind", "seconds", "within", "after_the_hour", "transition", "turn",
                                        "ended"))
        text = repr(sorted(self.rows(lp_market(trades=[(200, FAV, [[2.48, 1.0]])])).items()))
        for token in (M, M.split(".")[1], fx.EVENT, *map(str, SIDS)):
            self.assertNotIn(token, text)

    def test_the_codes_of_the_record_alone_never_open_the_member(self):
        late, edge = datetime(2016, 6, 1, 14, 0, tzinfo=timezone.utc), datetime(2016, 4, 30, 23, 30,
                                                                              tzinfo=timezone.utc)
        no_definition = [message for message in lp_market()
                         if not any("marketDefinition" in change for change in message["mc"])]
        revised = md(M, before(700), definition(OFF + timedelta(minutes=5), [runner(s) for s in SIDS]))
        cases = {
            "plan_not_registered": dict(messages=lp_market(), member=fx.path(M, when=OFF, plan="BASIC")),
            "no_view": dict(messages=no_definition),
            "month_not_registered": dict(messages=lp_market(off=late), when=late),
            # 23:30 UTC on 30 April is 1 May in London: a registered month, a race date with no label.
            "partition_not_registered": dict(messages=lp_market(off=edge), when=edge),
            "schedule_revised_before_decision": dict(messages=lp_market(extra=[revised])),
            "country_not_allowed": dict(messages=lp_market(countryCode="US")),
            "event_type_not_allowed": dict(messages=lp_market(eventTypeId="4339")),
            "market_type_not_allowed": dict(messages=lp_market(marketType="PLACE")),
            "betting_type_not_odds": dict(messages=lp_market(bettingType="ASIAN_HANDICAP_DOUBLE_LINE")),
            "number_of_winners_not_1": dict(messages=lp_market(numberOfWinners=2)),
            "bsp_market_false": dict(messages=lp_market(bspMarket=False, bsps={}, reconciled=False)),
        }
        for code, given in cases.items():
            with self.subTest(code=code):
                root, manifest, records = self.build([(given.get("member") or path(M, given.get("when", OFF)),
                                                       given["messages"])])
                walked = walk(Unread(), records[M], PLAN, DEFAULT_LIMITS)
                self.assertEqual(walked.outcomes, dict.fromkeys(PLAN.cells, code))
                self.assertEqual((walked.replayed, walked.flags_missing), (False, {}))
                self.assertIn(code, PLAN.codes[:14])
                self.assertEqual({record_code(PLAN, records[M], cell) for cell in PLAN.cells}, {(code, ())})

    def test_a_flag_gate_fails_on_an_exact_false_only(self):
        # No bspMarket in any definition, a null one, or a value that is not a boolean: the cell passes and is counted.
        for value in (fx.DROP, None, "false", 0):
            with self.subTest(value=value):
                walked = self.codes(lp_market(bspMarket=value))
                self.assertEqual(walked.flags_missing, dict.fromkeys(PLAN.cells, ("bsp_market",)))
                self.assertTrue(all(isinstance(row, OrderRow) for row in walked.outcomes.values()))
        # False only from off - 400 s: the value as of t decides, cell by cell.
        late = md(M, before(400), definition(OFF, [runner(s) for s in SIDS], bspMarket=False))
        walked = self.codes(lp_market(extra=[late]))
        self.assertEqual({cell.at_s: row if isinstance(row, str) else "issued"
                          for cell, row in walked.outcomes.items()},
                         {-600: "issued", -300: "bsp_market_false", -120: "bsp_market_false"})
        self.assertEqual(set(walked.flags_missing), {BACK_CELLS[0], LAY_CELLS[0]})
        # A rule file that does not gate the flag issues in a market without BSP; the order is then missing_bsp.
        ungated = plan_of(rules={LAY_RULE: lambda rule: rule["market"].update(bsp_market=None)})
        outcomes = self.rows(lp_market(bspMarket=False, bsps={}, reconciled=False), plan=ungated)
        self.assertEqual({cell.rule_id: row if isinstance(row, str) else row.klass
                          for cell, row in outcomes.items()},
                         {BACK_RULE: "bsp_market_false", LAY_RULE: "missing_bsp"})
        # The other two flags are gated the same way when a rule file asks for them.
        gated = plan_of(rules={LAY_RULE: lambda rule: rule["market"].update(turn_in_play_enabled=True)},
                        registration=lambda value: value["population"]["codes"].insert(14, "turn_in_play_not_enabled"))
        outcomes = self.rows(lp_market(turnInPlayEnabled=False), plan=gated)
        self.assertEqual({row if isinstance(row, str) else "issued" for cell, row in outcomes.items()
                          if cell.rule_id == LAY_RULE}, {"turn_in_play_not_enabled"})
        self.assertTrue(all(isinstance(outcomes[cell], OrderRow) for cell in BACK_CELLS))

    def test_the_codes_of_the_book(self):
        malformed = mcm(before(700), [{"id": M, "rc": [{"id": R3, "trd": [[6.2]]}]}])
        unattributed = mcm(before(700), [{"id": M, "rc": [{"id": "x", "atb": [[2.0, 5.0]]}]}])
        cases = {
            "ladder_malformed": [lp_market(extra=[malformed]), lp_market(extra=[unattributed])],
            "market_not_open": [lp_market(extra=[status_at(700, "SUSPENDED")]),
                                lp_market(extra=[status_at(700, in_play=True)])],
            "too_few_active_runners": [lp_market(runners=SIDS[:4])],
            "no_priced_active_runner": [lp_market(books={3600: {sid: (None, lay) for sid, (_, lay) in BOOK.items()}})],
            "favourite_tie": [lp_market(books={3600: book(R2=(2.5, 2.52))})],
            "best_back_out_of_range": [lp_market(books={3600: book(FAV=(1.4, 1.41))}),
                                       lp_market(books={3600: book(FAV=(4.1, 4.2), R2=(4.2, 4.3))})],
            "depth_below_min": [lp_market(books={3600: book(FAV=(2.5, 2.52, 9.99))})],
            "spread_not_1_or_2_ticks": [lp_market(books={3600: book(FAV=(2.5, 2.56))}),
                                        lp_market(books={3600: book(FAV=(2.5, None))}),
                                        lp_market(books={3600: book(FAV=(2.5, 2.53))})],
        }
        for code, markets in cases.items():
            for index, messages in enumerate(markets):
                with self.subTest(code=code, market=index):
                    walked = self.codes(messages)
                    self.assertEqual(walked.outcomes, dict.fromkeys(PLAN.cells, code))
                    self.assertTrue(walked.replayed)
                    self.assertIn(code, PLAN.codes[14:])

    def test_a_price_off_the_ladder_and_a_missing_reference_price(self):
        # Without its spread rows the BACK file can ask for a best lay that is missing or off the CLASSIC ladder.
        loose = plan_of(rules={BACK_RULE: lambda rule: rule.update(when=rule["when"][:5])})
        self.assertEqual(loose.codes, PLAN.codes)
        for quote, code in (((2.5, 2.53), "price_off_ladder"), ((2.5, None), "no_reference_price")):
            with self.subTest(quote=quote):
                outcomes = self.rows(lp_market(books={3600: book(FAV=quote)}), plan=loose)
                self.assertEqual({cell.rule_id: outcomes[cell] for cell in loose.cells},
                                 {BACK_RULE: code, LAY_RULE: "spread_not_1_or_2_ticks"})
        outcomes = self.rows(lp_market(books={3600: book(FAV=(2.5, 2.6))}), plan=loose)
        self.assertEqual(outcomes[BACK_CELLS[0]].price, P(2.6))

    def test_each_cell_decides_at_its_own_time(self):
        # The favourite's spread is three ticks until off - 400 s, and the market is suspended at off - 120 s.
        messages = lp_market(books={3600: book(FAV=(2.5, 2.56)), 400: book(FAV=(2.5, 2.54))},
                             extra=[status_at(130, "SUSPENDED"), status_at(110)])
        outcomes = self.rows(messages)
        self.assertEqual({cell.at_s: row if isinstance(row, str) else row.price for cell, row in outcomes.items()
                          if cell.rule_id == BACK_RULE},
                         {-600: "spread_not_1_or_2_ticks", -300: P(2.54), -120: "market_not_open"})


class WalkBeforeOff(RestingCase):
    def test_a_restatement_with_no_ladder_entry_fills_nothing(self):
        # A 1p rise at a stale price below the LAY and above the BACK, with no ladder entry in its batch.
        stale = lp_market(trades=[(1800, FAV, [[2.4, 500.0], [2.7, 300.0]])],
                          restatements=[(200, FAV, [[2.4, 500.01], [2.7, 300.01]])])
        rows = self.rows(stale)
        self.assertEqual(self.filled(stale), {})
        self.assertEqual(rows[LAY_CELLS[1]].trd, dict.fromkeys(TRD_COUNTS, 0))
        # The same rises with their levels changing in the batch are trades through both orders.
        traded = lp_market(trades=[(1800, FAV, [[2.4, 500.0], [2.7, 300.0]]),
                                   (200, FAV, [[2.4, 500.01], [2.7, 300.01]])])
        self.assertEqual(self.filled(traded), {(side, at_s): dict.fromkeys(MODELS, THROUGH)
                                               for side in (BACK, LAY) for at_s in (-600, -300)})
        # At the touch a restatement is counted apart: it is the check of the counting rule.
        touch = lp_market(trades=[(1800, FAV, [[2.5, 100.0], [2.52, 50.0]])],
                          restatements=[(200, FAV, [[2.5, 100.01]]), (180, FAV, [[2.52, 50.01]])])
        rows = self.rows(touch)
        self.assertEqual(self.filled(touch), {})
        self.assertEqual([rows[cell].trd[TRD_COUNTS[1]] for cell in LAY_CELLS], [2, 2, 0])
        # A lower value resets the last published value: the 2p after it is a real, small trade through.
        after = lp_market(trades=[(1800, FAV, [[2.48, 100.0]]), (200, FAV, [[2.48, 99.98]])],
                          restatements=[(400, FAV, [[2.48, 99.9]])])
        self.assertEqual(self.filled(after)[LAY, -300], dict.fromkeys(MODELS, THROUGH))

    def test_the_after_the_hour_seconds(self):
        # Off 14:05 on a Monday: 14:00:20 is in the first 60 s after the hour, 14:03:20 is not.
        off = OFF + timedelta(minutes=5)
        rows = self.rows(lp_market(off=off, trades=[(280, FAV, [[2.48, 1.0]])],
                                   restatements=[(290, FAV, [[2.5, 0.01]])]), when=off)
        self.assertEqual([rows[cell].fills[PESSIMISTIC].after_the_hour for cell in LAY_CELLS[:2]], [True, True])
        self.assertEqual(rows[LAY_CELLS[0]].trd[TRD_COUNTS[1]], 0)              # a restatement after the hour
        rows = self.rows(lp_market(off=off, trades=[(100, FAV, [[2.48, 1.0]])]), when=off)
        self.assertEqual(rows[LAY_CELLS[2]].fills[PESSIMISTIC].after_the_hour, False)

    def test_seconds_to_fill_and_the_first_two_seconds(self):
        for seconds, within in ((1, (True, True)), (1.001, (False, True)), (2, (False, True)),
                                (2.001, (False, False))):
            with self.subTest(seconds=seconds):
                rows = self.rows(lp_market(trades=[(300 - seconds, FAV, [[2.48, 1.0]])]))
                fill = rows[LAY_CELLS[1]].fills[OPTIMISTIC]
                self.assertEqual((fill.seconds, fill.within, fill.kind), (seconds, within, THROUGH))
                self.assertEqual(rows[LAY_CELLS[0]].fills[OPTIMISTIC].within, (False, False))
                self.assertEqual(rows[BACK_CELLS[1]].fills[OPTIMISTIC].within, (False, False))     # not filled

    def test_the_pull_time(self):
        through = [[2.48, 1.0], [2.54, 1.0]]
        expected = {
            120: {-600: THROUGH, -300: THROUGH, -120: None},        # at the first pull time; at t of the last cell
            119.999: {-600: None, -300: None, -120: THROUGH},       # 1 ms after the pull
            10: {-600: None, -300: None, -120: THROUGH},            # at the last pull time
            9.999: dict.fromkeys((-600, -300, -120)),
        }
        for seconds, found in expected.items():
            with self.subTest(seconds=seconds):
                rows = self.rows(lp_market(trades=[(seconds, FAV, through)]))
                for cell in PLAN.cells:
                    fill = rows[cell].fills[CENTRAL]
                    self.assertEqual((fill.kind, fill.ended), (found[cell.at_s], None if found[cell.at_s] else "pull"))
        rows = self.rows(lp_market(trades=[(119.999, FAV, through)]))
        self.assertEqual(rows[LAY_CELLS[2]].fills[CENTRAL].seconds, 0.001)

    def test_nothing_after_the_last_pull_time_is_parsed(self):
        late = ladder_move(5, atb=[[2.44, 5.0]], sid=FAV)
        root, manifest, records = self.build([(path(M), lp_market(extra=[late]))])
        with member_stream(root, manifest, records[M]) as stream:
            recording = Recording(stream)
            walked = walk(recording, records[M], PLAN, DEFAULT_LIMITS)
        self.assertTrue(walked.replayed)
        self.assertEqual(json.loads(recording.lines[-1])["pt"], before(5))      # the first message after the pull
        self.assertNotIn(b"CLOSED", b"".join(recording.lines))
        self.assertNotIn(b"bspReconciled\": true", b"".join(recording.lines))
        # Cells that all end at off - 120 s stop the read there.
        revised = md(M, before(200), definition(OFF + timedelta(minutes=5), [runner(s) for s in SIDS]))
        messages = lp_market(extra=[revised, ladder_move(100, atb=[[2.44, 5.0]], sid=FAV), late])
        root, manifest, records = self.build([(path(M), messages)])
        with member_stream(root, manifest, records[M]) as stream:
            recording = Recording(stream)
            walked = walk(recording, records[M], PLAN, DEFAULT_LIMITS)
        self.assertEqual({cell.at_s for cell, row in walked.outcomes.items()
                          if row == "schedule_revised_before_decision"}, {-120})
        self.assertEqual(json.loads(recording.lines[-1])["pt"], before(100))

    def test_a_stream_that_is_not_what_the_index_read_refuses(self):
        def built(messages):
            root, manifest, records = self.build([(path(M), messages)])
            return records[M], lambda: member_stream(root, manifest, records[M])

        whole, _ = built(lp_market())
        tailed, _ = built(lp_market(bad_tail=True))                 # read to the off, not to the end
        _, other = built(lp_market(extra=[status_at(700, "SUSPENDED")]))
        _, short = built([message for message in lp_market() if message["pt"] <= before(400)] + [b"not json\n"])
        for record, opened, reason in ((whole, other, "index_mismatch"),
                                       (whole, short, "unreadable_where_the_index_read"),
                                       (tailed, short, "unreadable_before_view_ends")):
            with self.subTest(reason=reason), opened() as stream, self.assertRaises(ReplayProblem) as caught:
                walk(stream, record, PLAN, DEFAULT_LIMITS)
            self.assertEqual(str(caught.exception), f"replay:{M}:{reason}")

    def test_a_lapse_order_ends_at_an_early_off(self):
        through = {"atb": [[2.48, 0]], "trd": [[2.48, 1.0]]}
        early = lp_market(extra=[status_at(200, in_play=True)], trades=[(150, FAV, [[2.48, 1.0]])])
        rows = self.rows(early)
        for cell in LAY_CELLS[:2] + BACK_CELLS[:2]:
            self.assertEqual(kinds(rows[cell]), UNFILLED)
            self.assertEqual({fill.ended for fill in rows[cell].fills.values()}, {"lapse"})
            self.assertEqual(rows[cell].klass, SCORED)                          # an early off drops no order
        self.assertEqual(rows[LAY_CELLS[2]], "market_not_open")
        # A trade in the batch that turns the market in play is still judged.
        turn = mcm(before(200), [{"id": M, "rc": [{"id": FAV, **through}],
                                  "marketDefinition": definition(OFF, [runner(s) for s in SIDS], in_play=True)}])
        rows = self.rows(lp_market(extra=[turn]))
        self.assertEqual(kinds(rows[LAY_CELLS[1]]), dict.fromkeys(MODELS, THROUGH))
        self.assertEqual({fill.ended for fill in rows[BACK_CELLS[1]].fills.values()}, {"lapse"})
        # Such a fill is marked: the market stayed OPEN, so it is no transition fill, but the batch turned it in play.
        self.assertEqual([(fill.transition, fill.turn) for fill in rows[LAY_CELLS[1]].fills.values()],
                         [(False, True)] * 3)
        # A quote that crosses only in the turn batch (the first in-running ladder) fills too, and is marked.
        crossing = mcm(before(200), [{"id": M, "rc": [{"id": FAV, "atl": [[2.46, 5.0]]}],
                                      "marketDefinition": definition(OFF, [runner(s) for s in SIDS], in_play=True)}])
        rows = self.rows(lp_market(extra=[crossing]))
        self.assertEqual([(fill.kind, fill.turn) for fill in rows[LAY_CELLS[1]].fills.values()], [(CROSS, True)] * 3)
        # A fill in a batch before the turn is not marked.
        rows = self.rows(lp_market(extra=[status_at(150, in_play=True)], trades=[(200, FAV, [[2.48, 1.0]])]))
        self.assertEqual([(fill.kind, fill.turn) for fill in rows[LAY_CELLS[1]].fills.values()], [(THROUGH, False)] * 3)
        # The lapse is for good: inPlay published false again does not bring the order back.
        back_again = lp_market(extra=[status_at(200, in_play=True), status_at(190)],
                               trades=[(150, FAV, [[2.48, 1.0]])])
        rows = self.rows(back_again)
        self.assertEqual(kinds(rows[LAY_CELLS[1]]), UNFILLED)
        self.assertEqual({fill.ended for fill in rows[LAY_CELLS[1]].fills.values()}, {"lapse"})
        self.assertEqual(kinds(rows[LAY_CELLS[2]]), UNFILLED)                   # placed at off - 120 s, not in play
        self.assertEqual({fill.ended for fill in rows[LAY_CELLS[2]].fills.values()}, {"pull"})
        # In play only from the pull time on: the pull ended the order.
        rows = self.rows(lp_market(extra=[status_at(120, in_play=True)]))
        self.assertEqual({fill.ended for fill in rows[LAY_CELLS[1]].fills.values()}, {"pull"})

    def test_the_mid_at_t_and_the_null(self):
        cell = LAY_CELLS[0]
        u = null_time(cell)
        self.assertTrue(ms(OFF) - 600_000 < u <= ms(OFF) - 120_000)
        self.assertEqual(len({null_time(item) for item in PLAN.cells}), 6)
        at_u = (ms(OFF) - u) / 1000                                             # u, in seconds before the off
        # The quote at u holds the batch at u and nothing later.
        rows = self.rows(lp_market(books={3600: BOOK, at_u: book(FAV=(2.54, 2.56)),
                                          at_u - 0.001: book(FAV=(2.6, 2.62))}))
        self.assertEqual((rows[cell].mid, rows[cell].null_mid, rows[cell].null_reason), (P(2.51), P(2.55), None))
        self.assertEqual(self.rows(lp_market())[cell].null_mid, P(2.51))
        undefined = {
            "market_not_open": [status_at(at_u, "SUSPENDED"), status_at(at_u - 0.001)],
            "in_play": [status_at(at_u, in_play=True), status_at(at_u - 0.001)],
            "not_two_sided": [ladder_move(at_u, atl=[[2.52, 0]], sid=FAV)],
            "spread_above_max": [ladder_move(at_u, atl=[[2.52, 0], [2.92, 5.0]], sid=FAV)],
        }
        for reason, extra in undefined.items():
            with self.subTest(reason=reason):
                row = self.rows(lp_market(extra=extra))[cell]
                self.assertEqual((row.null_mid, row.null_reason), (None, reason))
                self.assertIn(reason, NULL_UNDEFINED)
        # 20 ticks apart is still a market; 21 is not.
        row = self.rows(lp_market(extra=[ladder_move(at_u, atl=[[2.52, 0], [2.9, 5.0]], sid=FAV)]))[cell]
        self.assertEqual((row.null_mid, row.null_reason), (P(2.7), None))
        # The member stops being readable at u: the order was placed, the null could not be read.
        cut = [message for message in lp_market(extra=[ladder_move(at_u, atb=[[4.0, 40.0]], sid=R2)])
               if message["pt"] <= u]
        outcomes = self.rows(cut + [b"not json\n"])
        self.assertEqual((outcomes[cell].klass, outcomes[cell].null_reason, outcomes[cell].bsp),
                         ("outcome_unknown", "member_unreadable", None))
        self.assertEqual(outcomes[LAY_CELLS[2]], "member_unreadable_before_decision")


class CloseClasses(RestingCase):
    def classes(self, messages, **kwargs):
        """{at_s: class or code} of the LAY file's cells, after checking the BACK file's agree."""
        outcomes = self.rows(messages, **kwargs)
        found = {cell.at_s: row if isinstance(row, str) else row.klass for cell, row in outcomes.items()}
        for cell, row in outcomes.items():
            self.assertEqual(row if isinstance(row, str) else row.klass, found[cell.at_s])
            if isinstance(row, OrderRow):
                self.assertEqual(row.bsp is not None, row.klass == SCORED)
        return found

    def test_each_class(self):
        def removal(seconds, sid):
            return md(M, before(seconds), definition(OFF, [runner(s, "REMOVED" if s == sid else "ACTIVE")
                                                           for s in SIDS]))

        def final(removed):
            return {s: "REMOVED" if s == removed else "WINNER" if s == R2 else "LOSER" for s in SIDS}

        winners = [runner(sid, "WINNER" if sid == FAV else "LOSER", 2.44 if sid == FAV else BSPS[sid]) for sid in SIDS]
        settled = OFF + timedelta(minutes=8)
        revised = md(M, OFF + timedelta(minutes=30), definition(OFF, winners, status="CLOSED", in_play=True,
                                                                bspReconciled=True, settledTime=iso(settled)))
        malformed = mcm(before(200), [{"id": M, "rc": [{"id": R3, "trd": [[6.2]]}]}])
        unattributed = mcm(before(200), [{"id": M, "rc": [{"id": "x", "atb": [[2.0, 5.0]]}]}])
        cases = {
            "scored": ({}, dict.fromkeys((-600, -300, -120), SCORED)),
            "unreadable after the off": ({"bad_tail": True}, dict.fromkeys((-600, -300, -120), "outcome_unknown")),
            "our runner removed": ({"extra": [removal(200, FAV)], "statuses": final(FAV)},
                                   {-600: "void_own_runner_removed", -300: "void_own_runner_removed",
                                    -120: SCORED}),                         # R2 is the favourite at off - 120 s
            "another runner removed": ({"extra": [removal(200, R6)], "statuses": final(R6)},
                                       {-600: "post_decision_removal", -300: "post_decision_removal", -120: SCORED}),
            "result revised": ({"tail": [revised]}, dict.fromkeys((-600, -300, -120), "result_revised_after_close")),
            "not reconciled": ({"reconciled": False}, dict.fromkeys((-600, -300, -120), "missing_bsp")),
            "no BSP for the runner": ({"bsps": {**BSPS, FAV: None}}, dict.fromkeys((-600, -300, -120), "missing_bsp")),
            "a trd entry unreadable after t": ({"extra": [malformed]},
                                               {-600: "stream_malformed_after_placement",
                                                -300: "stream_malformed_after_placement", -120: "ladder_malformed"}),
            "an rc entry with no runner after t": ({"extra": [unattributed]},
                                                   {-600: "stream_malformed_after_placement",
                                                    -300: "stream_malformed_after_placement",
                                                    -120: "ladder_malformed"}),
        }
        for name, (changes, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.classes(lp_market(**changes)), expected)
        self.assertEqual(set(CLASSES), {"outcome_unknown", "void_own_runner_removed", "post_decision_removal",
                                        "result_revised_after_close", "missing_bsp",
                                        "stream_malformed_after_placement", SCORED})

    def test_a_malformed_entry_classes_only_the_orders_still_resting(self):
        # Unreadable at off - 60 s: after the pull of the first two cells, inside the window of the last.
        malformed = mcm(before(60), [{"id": M, "rc": [{"id": R3, "atb": [[6.0]]}]}])
        self.assertEqual(self.classes(lp_market(extra=[malformed])),
                         {-600: SCORED, -300: SCORED, -120: "stream_malformed_after_placement"})
        at_pull = mcm(before(120), [{"id": M, "rc": [{"id": R3, "atb": [[6.0]]}]}])
        self.assertEqual(self.classes(lp_market(extra=[at_pull])),
                         {-600: "stream_malformed_after_placement", -300: "stream_malformed_after_placement",
                          -120: "ladder_malformed"})
        # The fills of an order that is not scored are still reported, for the counts by class.
        rows = self.rows(lp_market(extra=[malformed], trades=[(100, FAV, [[2.48, 1.0]])]))
        self.assertEqual((rows[LAY_CELLS[2]].klass, kinds(rows[LAY_CELLS[2]])),
                         ("stream_malformed_after_placement", dict.fromkeys(MODELS, THROUGH)))

    def test_a_later_suspension_an_early_off_or_a_revised_schedule_keeps_the_order(self):
        revised = md(M, before(200), definition(OFF + timedelta(minutes=5), [runner(s) for s in SIDS]))
        cases = {
            "suspension": ([status_at(250, "SUSPENDED"), status_at(240)], dict.fromkeys((-600, -300, -120), SCORED)),
            "still suspended at the pull": ([status_at(250, "SUSPENDED")],
                                            {-600: SCORED, -300: SCORED, -120: "market_not_open"}),
            "early off": ([status_at(200, in_play=True)], {-600: SCORED, -300: SCORED, -120: "market_not_open"}),
            "schedule revised": ([revised], {-600: SCORED, -300: SCORED,
                                             -120: "schedule_revised_before_decision"}),
        }
        for name, (extra, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.classes(lp_market(extra=extra)), expected)

    def test_a_removal_classes_and_never_re_prices(self):
        removal = md(M, before(200), definition(OFF, [runner(s, "REMOVED" if s == R6 else "ACTIVE") for s in SIDS]))
        statuses = {s: "REMOVED" if s == R6 else "WINNER" if s == FAV else "LOSER" for s in SIDS}
        rows = self.rows(lp_market(extra=[removal], statuses=statuses, trades=[(250, FAV, [[2.48, 1.0]])]))
        row = rows[LAY_CELLS[1]]
        self.assertEqual((row.klass, row.price, row.bsp), ("post_decision_removal", P(2.5), None))
        self.assertEqual(kinds(row), dict.fromkeys(MODELS, THROUGH))            # counted, never scored
        self.assertEqual((rows[LAY_CELLS[2]].klass, rows[LAY_CELLS[2]].price, rows[LAY_CELLS[2]].bsp),
                         (SCORED, P(2.5), P(2.6)))

    def test_the_bsp_is_the_value_as_written(self):
        # 2.57 is not a CLASSIC price (the ladder steps 0.02 there): the row keeps it as the file wrote it.
        rows = self.rows(lp_market(bsps={**BSPS, FAV: 2.57}))
        self.assertEqual({row.bsp for row in rows.values()}, {Fraction(257, 100)})
        rows = self.rows(lp_market(bsps={**BSPS, FAV: 2.5733}))
        self.assertEqual({row.bsp for row in rows.values()}, {Fraction(25733, 10000)})


# ---------------------------------------------------------------------------------------------------------------
# Order rows built by hand, for the metric, the statistics, the selection and the labels

D, V = PLAN.labels
D_DAYS = [(date(2015, 5, 2) + timedelta(days=index)).isoformat() for index in range(16)]
V_DAYS = [(date(2015, 7, 1) + timedelta(days=index)).isoformat() for index in range(8)] + \
         [(date(2016, 1, 5) + timedelta(days=index)).isoformat() for index in range(8)]     # H1 then H2
NONE = ()


def order(bsp, filled=MODELS, side=LAY, price=2.5, day=V_DAYS[0], label=V, half="H1", null=None, mid=None,
          first_second=False):
    """One scored OrderRow: filled names the models it filled under; null and mid are prices or None."""
    found = {model: Fill(Fraction(0), VOLUME, 0.5 if first_second else 5.0, (first_second, True), False, False,
                         False, None)
             if model in filled else Fill(Fraction(0), None, None, (False, False), False, False, False, "pull")
             for model in MODELS}
    return OrderRow(side, P(price), label, half, day, SCORED, P(bsp), None if mid is None else P(mid), found,
                    None if null is None else P(null), None if null is not None else "not_two_sided",
                    dict.fromkeys(TRD_COUNTS, 0), False)


def block(count, days, bsps, label=V, nulls=(None,), **kwargs):
    """count rows over the days in turn. A day's rows share one BSP and one null mid (bsps and nulls in turn by
    day), so the spread lies between race dates. The first half of V_DAYS is H1, the second H2."""
    rows = []
    for index in range(count):
        place = index % len(days)
        half = "H2" if label == V and days[place] in V_DAYS[8:] else "H1"
        rows.append(order(bsps[place % len(bsps)], day=days[place], label=label, half=half,
                          null=nulls[place % len(nulls)], **kwargs))
    return rows


def mean_of(values):
    return math.fsum(x for _, x in values) / len(values)


class CloseMetric(RestingCase):
    def test_g_and_both_nets_are_exact_for_both_sides(self):
        c = Fraction(2, 100)
        # A BACK at 2.52 against BSP 2.4: g = 2.52 / 2.4 - 1 = 1/20; closed keeps 98% of a gain; held pays
        # c x (P - 1) / BSP = 0.02 x 1.52 / 2.4.
        back = {what: score(PLAN, BACK, P(2.52), P(2.4), what) for what in (G, NET_CLOSED, NET_HELD)}
        self.assertEqual(back, {G: Fraction(1, 20), NET_CLOSED: Fraction(49, 1000), NET_HELD: Fraction(14, 375)})
        self.assertEqual(back[NET_HELD], nclv(P(2.52), P(2.4), c))                     # contract p1's NCLV
        # A LAY at 2.5 against BSP 2.6: g = 1 - 2.5 / 2.6 = 1/26; held pays c x (1 - 1 / BSP).
        lay = {what: score(PLAN, LAY, P(2.5), P(2.6), what) for what in (G, NET_CLOSED, NET_HELD)}
        self.assertEqual(lay, {G: Fraction(1, 26), NET_CLOSED: Fraction(49, 1300), NET_HELD: Fraction(17, 650)})
        self.assertEqual(lay[NET_HELD], (1 - c) - (P(2.5) - c) / P(2.6))               # lp-study's form
        # A loss pays no commission when closed, and still pays it when held.
        self.assertEqual([score(PLAN, LAY, P(2.5), P(2.4), what) for what in (G, NET_CLOSED, NET_HELD)],
                         [Fraction(-1, 24), Fraction(-1, 24), Fraction(-4, 75)])
        self.assertEqual([score(PLAN, BACK, P(2.4), P(2.5), what) for what in (G, NET_CLOSED)],
                         [Fraction(-1, 25), Fraction(-1, 25)])
        # A maker LAY at a price is minus the taker BACK there, before commission.
        self.assertEqual(score(PLAN, LAY, P(3.0), P(3.05)), -score(PLAN, BACK, P(3.0), P(3.05)))
        self.assertEqual(score(PLAN, LAY, P(2.5), P(2.5)), 0)
        self.assertIs(type(score(PLAN, LAY, P(2.5), P(2.5), NET_HELD)), Fraction)
        with self.assertRaises(ValueError):
            score(PLAN, LAY, P(2.5), P(2.6), "net")

    def test_net_closed_is_the_kinder_net(self):
        for side in (BACK, LAY):
            for price in (1.5, 2.0, 2.52, 3.95, 4.0):
                for bsp in (1.2, 1.5, 2.0, 2.57, 3.0, 4.0, 7.4):
                    held, closed, g = (score(PLAN, side, P(price), P(bsp), what) for what in (NET_HELD, NET_CLOSED, G))
                    self.assertLessEqual(held, closed, (side, price, bsp))
                    self.assertLessEqual(closed, g)

    def test_both_bsp_ends_and_which_one_decides(self):
        lay, back = order(2.6), order(2.4, side=BACK, price=2.52)
        for row, written, stepped in ((lay, 1 - P(2.5) / P(2.6), 1 - P(2.5) / P(2.61)),
                                      (back, P(2.52) / P(2.4) - 1, P(2.52) / P(2.41) - 1)):
            self.assertEqual(series(PLAN, [row], G, AS_WRITTEN), [(row.race_date, float(written))])
            self.assertEqual(series(PLAN, [row], G, PLUS_STEP), [(row.race_date, float(stepped))])
        self.assertEqual(series(PLAN, [lay], NET_HELD, PLUS_STEP)[0][1],
                         float(score(PLAN, LAY, P(2.5), P(2.61), NET_HELD)))
        with self.assertRaises(KeyError):
            series(PLAN, [lay], G, "bsp_rounded")
        # The kill end favours the order: a LAY gains from a higher BSP, a BACK from a lower one.
        self.assertEqual([deciding_end(LAY, KILL_END, True), deciding_end(LAY, CLAIM_END, True)],
                         [PLUS_STEP, AS_WRITTEN])
        self.assertEqual([deciding_end(BACK, KILL_END, True), deciding_end(BACK, CLAIM_END, True)],
                         [AS_WRITTEN, PLUS_STEP])
        for row in (lay, back):
            kill, claim = (series(PLAN, [row], G, deciding_end(row.side, purpose, True))[0][1]
                           for purpose in (KILL_END, CLAIM_END))
            self.assertGreater(kill, claim)
        # Without the switch the as-written figure decides alone.
        self.assertEqual({deciding_end(side, purpose, False) for side in (BACK, LAY)
                          for purpose in (KILL_END, CLAIM_END)}, {AS_WRITTEN})
        self.assertEqual(BSP_ENDS, (AS_WRITTEN, PLUS_STEP))

    def test_the_99_percent_switch(self):
        rows = [order(2.6)] * 99 + [order(2.5733)]
        self.assertEqual(whole_hundredths(PLAN, rows), (99, 100))
        self.assertTrue(ends_apart(PLAN, 99, 100))
        self.assertFalse(ends_apart(PLAN, 98, 100))
        self.assertTrue(ends_apart(PLAN, 990, 1000))
        self.assertFalse(ends_apart(PLAN, 989, 1000))
        self.assertFalse(ends_apart(PLAN, 0, 0))                                # no scored order: nothing to switch
        # 2.57 is a whole hundredth though not a ladder price; only scored rows are counted.
        unscored = order(2.6)._replace(klass="missing_bsp", bsp=None)
        self.assertEqual(whole_hundredths(PLAN, [order(2.57), order(2.575), order(3), unscored]), (2, 3))
        self.assertEqual(scored([order(2.6), unscored, order(2.6, label=D), order(2.6, half="H2")], V, "H1"),
                         [order(2.6)])

    def test_the_bsp_is_never_snapped_through_the_walk(self):
        # 2.57 is not a CLASSIC price: g is taken against 2.57 and 2.58, not 2.56 or 2.58 and 2.6.
        rows = self.rows(lp_market(bsps={**BSPS, FAV: 2.57}))
        lay, back = rows[LAY_CELLS[0]], rows[BACK_CELLS[0]]
        self.assertEqual((lay.price, back.price, lay.mid), (P(2.5), P(2.52), P(2.51)))
        self.assertEqual(series(PLAN, [lay], G, AS_WRITTEN)[0][1], float(1 - Fraction(250, 257)))
        self.assertEqual(series(PLAN, [lay], G, PLUS_STEP)[0][1], float(1 - Fraction(250, 258)))
        self.assertEqual(series(PLAN, [back], NET_HELD, PLUS_STEP)[0][1],
                         float(nclv(P(2.52), P(2.58), PLAN.commission)))
        # The mid at t and the mid at u are valued as the order is; a row without one is left out.
        self.assertEqual(series(PLAN, [lay, back], G, AS_WRITTEN, "mid"), [
            (lay.race_date, float(1 - Fraction(251, 257))), (back.race_date, float(Fraction(251, 257) - 1))])
        self.assertEqual(series(PLAN, [lay], G, AS_WRITTEN, "null_mid"), series(PLAN, [lay], G, AS_WRITTEN, "mid"))
        self.assertEqual(series(PLAN, [order(2.6)], G, AS_WRITTEN, "mid"), [])


class PartAndRest(unittest.TestCase):
    def group(self, unfilled, filled=200):
        """filled rows that filled under every model (BSP 2.7 or 2.8 by day) and unfilled rows (BSP 2.4)."""
        return block(filled, V_DAYS, [2.7, 2.8]) + block(unfilled, V_DAYS, [2.4], filled=NONE)

    def test_1_and_99_unfilled_orders_give_filled_is_all_placed_and_100_does_not(self):
        for unfilled, flag in ((0, True), (1, True), (99, True), (100, False), (101, False)):
            with self.subTest(unfilled=unfilled):
                rows = self.group(unfilled)
                part = filled_part(PLAN, rows, PESSIMISTIC)
                self.assertEqual((part.all_placed, part.fills, part.fill_dates), (flag, 200, 16))
                self.assertEqual(len(part.rows), 200 + unfilled if flag else 200)
                found = figures(PLAN, rows, AS_WRITTEN)
                model = found["models"][PESSIMISTIC]
                self.assertEqual((model[FILLED_IS_ALL_PLACED], model["fills"], model["fill_race_dates"]),
                                 (flag, 200, 16))
                self.assertEqual(found["placed"]["bets"], 200 + unfilled)
                same = model["filled"][G]["mean"] == found["placed"]["mean"]
                self.assertEqual(same, flag or unfilled == 0)
                blind = blind_outcome(PLAN, rows, PESSIMISTIC, AS_WRITTEN)
                self.assertEqual((blind[FILLED_IS_ALL_PLACED], blind["values"]), (flag, len(part.rows)))
                self.assertEqual(blind["sd"], sample_sd([x for _, x in series(PLAN, part.rows, G, AS_WRITTEN)]))
                self.assertEqual(blind["detectable"][FILLED_IS_ALL_PLACED], flag)

    def test_under_the_flag_every_filled_figure_is_the_all_placed_figure(self):
        rows = self.group(99)
        model = figures(PLAN, rows, AS_WRITTEN)["models"][PESSIMISTIC]
        for what in (G, NET_CLOSED, NET_HELD):
            self.assertEqual(model["filled"][what], described(PLAN, series(PLAN, rows, what, AS_WRITTEN)))
        # Every order is then valued as filled, and no gap is given: with them, 99 orders' mean could be worked out.
        self.assertEqual(model["value_per_placed"], model["filled"][NET_HELD])
        self.assertEqual((model["gap"]["estimate"], model["gap"]["reason"], model["gap"]["bets"]),
                         (None, FILLED_IS_ALL_PLACED, [200, 99]))
        self.assertEqual(model["filled"][G]["bets"], 299)

    def test_with_100_unfilled_the_four_figures_are_the_registered_ones(self):
        rows = self.group(100)
        filled, unfilled = rows[:200], rows[200:]
        found = figures(PLAN, rows, AS_WRITTEN)
        model = found["models"][CENTRAL]
        g, held = series(PLAN, filled, G, AS_WRITTEN), series(PLAN, filled, NET_HELD, AS_WRITTEN)
        self.assertEqual(model["filled"][G]["mean"], mean_of(g))                                     # (1)
        self.assertEqual(model["filled"][NET_HELD]["mean"], mean_of(held))
        self.assertEqual(model["filled"][NET_CLOSED]["mean"], mean_of(series(PLAN, filled, NET_CLOSED, AS_WRITTEN)))
        self.assertEqual(found["placed"]["mean"], mean_of(series(PLAN, rows, G, AS_WRITTEN)))           # (2)
        self.assertAlmostEqual(model["value_per_placed"]["mean"], mean_of(held) * 200 / 300, places=15)  # (3)
        self.assertEqual(model["value_per_placed"]["bets"], 300)
        gap_ = model["gap"]                                                                            # (4)
        self.assertAlmostEqual(gap_["estimate"], mean_of(g) - mean_of(series(PLAN, unfilled, G, AS_WRITTEN)), places=15)
        self.assertEqual((gap_["bets"], gap_["race_dates"], gap_["df"], gap_["reason"]),
                         ([200, 100], [16, 16], 15, None))
        self.assertLess(gap_["ci_low"], gap_["estimate"])
        self.assertAlmostEqual(gap_["ci_high"] - gap_["estimate"], student_t_quantile(0.975, 15) * gap_["se"],
                               places=15)
        self.assertNotIn("p_two_sided", gap_)                                   # descriptive: it tests nothing
        self.assertEqual(gap_, gap(PLAN, rows, CENTRAL, AS_WRITTEN))
        self.assertEqual(found["mid"]["bets"], 0)
        # An interval needs 15 race dates; the estimate does not.
        few = gap(PLAN, block(200, V_DAYS[:14], [2.7, 2.8]) + block(100, V_DAYS[:14], [2.4], filled=NONE), CENTRAL,
                  AS_WRITTEN)
        self.assertEqual((few["reason"], few["se"], few["ci_low"]), ("too_few_clusters", None, None))
        self.assertIsNotNone(few["estimate"])

    def test_a_filled_part_under_100_gives_counts_only(self):
        rows = self.group(200, filled=99)
        model = figures(PLAN, rows, AS_WRITTEN)["models"][OPTIMISTIC]
        self.assertFalse(model[FILLED_IS_ALL_PLACED])
        for item in (*model["filled"].values(), model["value_per_placed"]):
            self.assertEqual((item["mean"], item["sd"], item["ci_low"], item["reason"]),
                             (None, None, None, COUNTS_ONLY))
        self.assertEqual((model["filled"][G]["bets"], model["value_per_placed"]["bets"], model["fills"]), (99, 299, 99))
        self.assertEqual((model["gap"]["estimate"], model["gap"]["reason"]), (None, COUNTS_ONLY))
        blind = blind_outcome(PLAN, rows, OPTIMISTIC, AS_WRITTEN)
        self.assertEqual((blind["values"], blind["sd"], blind["ranges"], blind["detectable"]["effect"]),
                         (99, None, None, None))

    def test_the_models_are_judged_apart(self):
        rows = block(150, V_DAYS, [2.7]) + block(60, V_DAYS, [2.6], filled=(OPTIMISTIC, CENTRAL)) + \
            block(60, V_DAYS, [2.5], filled=(OPTIMISTIC,)) + block(60, V_DAYS, [2.4], filled=NONE)
        parts = {model: filled_part(PLAN, rows, model) for model in MODELS}
        self.assertEqual({model: (part.fills, part.all_placed, len(part.rows)) for model, part in parts.items()},
                         {OPTIMISTIC: (270, True, 330), CENTRAL: (210, False, 210), PESSIMISTIC: (150, False, 150)})

    def test_the_fills_after_the_first_second(self):
        def group(first, unfilled=100):
            return block(first, V_DAYS, [2.7], first_second=True) + block(150, V_DAYS, [2.8]) + \
                block(unfilled, V_DAYS, [2.4], filled=NONE)

        rows, stands = without_first_fills(PLAN, group(100), CENTRAL)
        self.assertEqual((len(rows), stands, {row.bsp for row in rows}), (150, False, {P(2.8)}))
        # 99 first-second fills: the filled figure stands, or the two means would give those 99 orders' mean.
        rows, stands = without_first_fills(PLAN, group(99), CENTRAL)
        self.assertEqual((len(rows), stands), (249, True))
        # Under filled_is_all_placed the group is every scored order: its rest is the unfilled and the first fills.
        rows, stands = without_first_fills(PLAN, group(60, unfilled=40), CENTRAL)
        self.assertEqual((len(rows), stands), (150, False))
        rows, stands = without_first_fills(PLAN, group(60, unfilled=39), CENTRAL)
        self.assertEqual((len(rows), stands), (249, True))


class PartAndRestAcrossModels(unittest.TestCase):
    """The models' fills lie inside one another: every pessimistic fill is a central fill, and every central fill
    an optimistic one. Two filled means over groups that differ by a few orders would give those orders' mean."""

    def group(self, front_only, half_only=0, behind=499, unfilled=300):
        """behind rows filled under every model, half_only more under the optimistic and central models,
        front_only more under the optimistic model alone, and unfilled rows."""
        return block(behind, V_DAYS, [2.7, 2.8]) + block(half_only, V_DAYS, [2.5], filled=(OPTIMISTIC, CENTRAL)) + \
            block(front_only, V_DAYS, [2.3], filled=(OPTIMISTIC,)) + block(unfilled, V_DAYS, [2.4], filled=NONE)

    def given(self, rows):
        return [model for kind, model in given_figures(PLAN, rows) if kind == FILLED]

    def test_classes_of_orders_under_100_stop_a_group(self):
        def names(*groups):
            return given_together(PLAN, 400, [(name, set(range(*span))) for name, span in groups])

        self.assertEqual(GIVEN_FIRST, (PESSIMISTIC, OPTIMISTIC, CENTRAL))
        # b holds a and 50 more: with a given, the 50 could be worked out. c holds a and 100 more.
        self.assertEqual(names(("a", (0, 100)), ("b", (0, 150)), ("c", (0, 200))), ["a", "c"])
        self.assertEqual(names(("b", (0, 150)), ("a", (0, 100)), ("c", (0, 200))), ["b"])       # the first is kept
        self.assertEqual(names(("a", (0, 100)), ("same", (0, 100)), ("all", (0, 400))), ["a", "same", "all"])
        self.assertEqual(names(("small", (0, 99)), ("a", (0, 100))), ["a"])
        self.assertEqual(names(("rest of 99", (0, 301)), ("a", (0, 300))), ["a"])
        # Two groups that leave 100 rows each, and cut a class of 50 between them.
        self.assertEqual(names(("a", (0, 200)), ("crossing", (150, 300))), ["a"])
        self.assertEqual(given_together(PLAN, 99, [("a", set(range(99)))]), [])
        self.assertEqual(given_together(PLAN, 0, [("a", set())]), [])

    def test_500_and_499_fills_do_not_both_give_a_mean(self):
        rows = self.group(1)
        self.assertEqual({model: filled_part(PLAN, rows, model).fills for model in MODELS},
                         {OPTIMISTIC: 500, CENTRAL: 499, PESSIMISTIC: 499})
        self.assertEqual(self.given(rows), [PESSIMISTIC, CENTRAL])
        for end in BSP_ENDS:
            found = figures(PLAN, rows, end)["models"]
            for model in (PESSIMISTIC, CENTRAL):                # one and the same group of 499 orders
                self.assertEqual((found[model]["filled"][G]["bets"], found[model]["filled"][G]["reason"]), (499, None))
                self.assertIsNotNone(found[model]["value_per_placed"]["mean"])
                self.assertIsNotNone(found[model]["gap"]["estimate"])
            self.assertEqual(found[PESSIMISTIC]["filled"], found[CENTRAL]["filled"])
            front = found[OPTIMISTIC]
            self.assertEqual((front["fills"], front["fill_race_dates"], front[FILLED_IS_ALL_PLACED]), (500, 16, False))
            for item in (*front["filled"].values(), front["value_per_placed"]):
                self.assertEqual((item["mean"], item["sd"], item["se"], item["ci_low"], item["ci_high"],
                                  item["reason"]), (None, None, None, None, None, REST_NOT_GIVEN))
            self.assertEqual(front["filled"][NET_HELD]["bets"], 500)             # the counts stay
            self.assertEqual((front["gap"]["estimate"], front["gap"]["ci_low"], front["gap"]["reason"],
                              front["gap"]["bets"]), (None, None, REST_NOT_GIVEN, [500, 300]))
        # The figure withheld is the one whose group could be told apart; what it would have been is known here.
        full = described(PLAN, series(PLAN, rows[:500], G, AS_WRITTEN))
        self.assertEqual(withheld(full), dict(full, mean=None, se=None, df=None, t_critical=None, ci_low=None,
                                              ci_high=None, sd=None, reason=REST_NOT_GIVEN))
        self.assertEqual(withheld(described(PLAN, [])), described(PLAN, []))    # counts only keeps its own reason

    def test_the_models_are_given_in_the_order_a_claim_a_kill_then_the_one_between(self):
        cases = {
            (0, 0): [PESSIMISTIC, OPTIMISTIC, CENTRAL],         # three times the same group
            (99, 0): [PESSIMISTIC, CENTRAL],
            (100, 0): [PESSIMISTIC, OPTIMISTIC, CENTRAL],
            (0, 1): [PESSIMISTIC],                              # central and optimistic each hold one order more
            (0, 100): [PESSIMISTIC, OPTIMISTIC, CENTRAL],
            (60, 60): [PESSIMISTIC, OPTIMISTIC],                # 120 more at the front; central lies 60 from both
            (100, 100): [PESSIMISTIC, OPTIMISTIC, CENTRAL],
            (100, 99): [PESSIMISTIC, OPTIMISTIC],
        }
        for (front_only, half_only), given in cases.items():
            with self.subTest(front_only=front_only, half_only=half_only):
                self.assertEqual(self.given(self.group(front_only, half_only)), given)
        # A model with under 100 fills gives counts only, and stops no other model.
        rows = self.group(60, behind=50)
        self.assertEqual(self.given(rows), [OPTIMISTIC])
        found = figures(PLAN, rows, AS_WRITTEN)["models"]
        self.assertEqual([found[model]["filled"][G]["reason"] for model in MODELS], [None, COUNTS_ONLY, COUNTS_ONLY])
        # Under the flag a model's group is every scored order: 40 unfilled here, under every model.
        rows = self.group(1, unfilled=40)
        self.assertEqual(self.given(rows), [PESSIMISTIC, OPTIMISTIC, CENTRAL])
        self.assertEqual({figures(PLAN, rows, AS_WRITTEN)["models"][model]["filled"][G]["bets"] for model in MODELS},
                         {540})
        # 150 unfilled under the pessimistic model, 99 of them filled at the front: the front is flagged, and
        # its all-placed mean with the pessimistic mean gives nothing new (the placed mean is given anyway).
        rows = self.group(99, unfilled=51)
        self.assertEqual({model: filled_part(PLAN, rows, model).all_placed for model in MODELS},
                         {OPTIMISTIC: True, CENTRAL: False, PESSIMISTIC: False})
        self.assertEqual(self.given(rows), [PESSIMISTIC, OPTIMISTIC, CENTRAL])

    def test_the_figure_without_the_first_fills_follows_the_same_rule(self):
        def later(rows):
            return [model for kind, model in given_figures(PLAN, rows) if kind == LATER]

        def group(first_behind, first_front):
            """150 later fills and first_behind first-second fills under every model, and first_front orders
            that fill in the first second at the front of the queue and later behind it."""
            both = block(first_front, V_DAYS, [2.6])
            mixed = [row._replace(fills={**row.fills, OPTIMISTIC: row.fills[OPTIMISTIC]._replace(
                seconds=0.5, within=(True, True))}) for row in both]
            return block(150, V_DAYS, [2.8]) + block(first_behind, V_DAYS, [2.7], first_second=True) + mixed + \
                block(100, V_DAYS, [2.4], filled=NONE)

        self.assertEqual(later(group(100, 0)), [PESSIMISTIC, OPTIMISTIC, CENTRAL])
        # 30 orders are first-second fills at the front only: the pessimistic and central later groups hold them,
        # the optimistic one does not, so only the first of the three is given.
        rows = group(100, 30)
        self.assertEqual(given_figures(PLAN, rows)[:3], [(FILLED, model) for model in GIVEN_FIRST])
        self.assertEqual(later(rows), [PESSIMISTIC, CENTRAL])
        self.assertEqual([len(without_first_fills(PLAN, rows, model)[0]) for model in MODELS], [150, 180, 180])
        # Where the filled figure stands, the later figure is that figure: given when it is.
        rows = group(99, 0)
        self.assertEqual([without_first_fills(PLAN, rows, model)[1] for model in MODELS], [True] * 3)
        self.assertEqual(later(rows), [PESSIMISTIC, OPTIMISTIC, CENTRAL])


class Statistics(unittest.TestCase):
    def test_the_one_sided_p_against_hand_values(self):
        # Student's t with 1 degree of freedom is the Cauchy law, cdf 1/2 + atan(t) / pi; with 2 degrees it is
        # 1/2 + t / (2 sqrt(2 + t^2)).
        self.assertAlmostEqual(one_sided_p(1.0, 1.0, 1), 0.25, places=12)
        self.assertAlmostEqual(one_sided_p(-1.0, 1.0, 1), 0.75, places=12)
        self.assertAlmostEqual(one_sided_p(0.3, 0.1, 1), 0.5 - math.atan(3.0) / math.pi, places=12)
        self.assertAlmostEqual(one_sided_p(math.sqrt(2), 1.0, 2), 0.5 - math.sqrt(2) / 4, places=12)
        self.assertAlmostEqual(one_sided_p(0.0, 1.0, 7), 0.5, places=12)
        # t(0.975; 14) = 2.1448 and t(0.98; 14) = 2.2638 (tables): one_sided_p undoes them.
        self.assertAlmostEqual(one_sided_p(2.1448, 1.0, 14), 0.025, places=5)
        self.assertAlmostEqual(one_sided_p(2.2638, 1.0, 14), 0.02, places=5)

    def test_the_test_is_cr1_by_race_date(self):
        # 15 race dates of 20 values; a date's values are equal: 0.05 on 9 dates and -0.02 on 6.
        values = [(day, 0.05 if index < 9 else -0.02) for index, day in enumerate(V_DAYS[:15]) for _ in range(20)]
        mean = (9 * 0.05 - 6 * 0.02) / 15
        # SE^2 = G / (G - 1) x sum over dates of (20 x (x - mean))^2 / N^2.
        se = math.sqrt(15 / 14 * (9 * (20 * (0.05 - mean)) ** 2 + 6 * (20 * (-0.02 - mean)) ** 2)) / 300
        found = cr1_test(PLAN, values)
        self.assertEqual((found["bets"], found["race_dates"], found["df"], found["reason"]), (300, 15, 14, None))
        self.assertAlmostEqual(found["mean"], mean, places=15)
        self.assertAlmostEqual(found["se"], se, places=15)
        self.assertAlmostEqual(found["t"], mean / se, places=12)
        self.assertAlmostEqual(found["p_one_sided"], one_sided_p(mean, se, 14), places=12)
        self.assertNotIn("p_two_sided", found)
        self.assertLess(found["p_one_sided"], 0.05)
        both = cr1_test(PLAN, values, two_sided=True)
        self.assertAlmostEqual(both["p_two_sided"], 2 * found["p_one_sided"], places=12)
        self.assertNotIn("p_one_sided", both)
        # The clusters are the dates, not the orders: the same 300 values, one date each, give a far smaller SE.
        alone = cr1_test(PLAN, [(str(index), x) for index, (_, x) in enumerate(values)])
        self.assertEqual(alone["df"], 299)
        self.assertLess(alone["se"], se / 4)
        # A mean below 0 has a one-sided p above one half; it is not a test of "differs from 0".
        self.assertGreater(cr1_test(PLAN, [(day, -x) for day, x in values])["p_one_sided"], 0.95)

    def test_the_bounds(self):
        values = [(day, 0.05 if index < 9 else -0.02) for index, day in enumerate(V_DAYS[:15]) for _ in range(20)]
        found = gated(PLAN, values)
        self.assertAlmostEqual(student_t_quantile(0.975, 14), 2.1448, places=4)
        self.assertAlmostEqual(student_t_quantile(0.98, 14), 2.2638, places=4)
        self.assertAlmostEqual(bound(found, PLAN.kill_level, 1), found["mean"] + 2.1448 * found["se"], places=6)
        self.assertAlmostEqual(bound(found, PLAN.lower_bound_level, -1), found["mean"] - 2.2638 * found["se"], places=6)
        self.assertEqual(bound(found, PLAN.kill_level, 1), found["ci_high"])     # the 95% interval's upper end
        # No SE, no bound: equal values, or a failed gate.
        flat = gated(PLAN, [(day, 0.01) for day in V_DAYS for _ in range(20)])
        self.assertEqual((flat["reason"], bound(flat, PLAN.kill_level, 1)), ("se_undefined", None))
        self.assertIsNone(bound(gated(PLAN, values, False), PLAN.kill_level, 1))

    def test_holm_with_m_of_2(self):
        def rejected(**pvalues):
            return {name: answer[1] for name, answer in holm(pvalues, PLAN.alpha, PLAN.family_m).items()}

        self.assertEqual((PLAN.alpha, PLAN.family_m), (Fraction(2, 100), 2))
        # A rule file with no primary has no p and still counts in m: the other needs 0.02 / 2.
        self.assertEqual(rejected(back=0.015, lay=None), {"back": False, "lay": False})
        self.assertEqual(rejected(back=0.01, lay=None), {"back": True, "lay": False})
        # The smaller p needs 0.01; once it is rejected the other needs 0.02.
        self.assertEqual(rejected(back=0.015, lay=0.009), {"back": True, "lay": True})
        self.assertEqual(rejected(back=0.021, lay=0.009), {"back": False, "lay": True})
        self.assertEqual(rejected(back=0.015, lay=0.011), {"back": False, "lay": False})
        self.assertEqual(holm({"back": 0.015, "lay": 0.009}, PLAN.alpha, 2),
                         {"back": (0.018, True), "lay": (0.018, True)})

    def test_the_size_gates(self):
        self.assertEqual([gate(PLAN, 200, 15), gate(PLAN, 199, 15), gate(PLAN, 200, 14), gate(PLAN, 5000, 14)],
                         [True, False, False, False])

        def values(count, days):
            return [(days[index % len(days)], 0.01 * (index % 7)) for index in range(count)]

        # 100 values give a mean and an SD; 99 give counts only.
        self.assertEqual(described(PLAN, values(99, V_DAYS)),
                         dict(bets=99, race_dates=16, mean=None, se=None, df=None, t_critical=None, ci_low=None,
                              ci_high=None, sd=None, reason=COUNTS_ONLY))
        enough = described(PLAN, values(100, V_DAYS))
        self.assertEqual(enough["sd"], sample_sd([x for _, x in values(100, V_DAYS)]))
        self.assertEqual({key: value for key, value in enough.items() if key != "sd"},
                         cr1_mean(values(100, V_DAYS), PLAN.confidence_level, PLAN.min_clusters))
        self.assertEqual(described(PLAN, values(100, V_DAYS), False)["reason"], COUNTS_ONLY)
        # An interval needs 15 race dates.
        few = described(PLAN, values(100, V_DAYS[:14]))
        self.assertEqual((few["reason"], few["ci_low"], few["mean"] is None), ("too_few_clusters", None, False))
        # A test needs 200 values on 15 race dates, read on the model the rule reads (passes) or on the values.
        for count, days, tested in ((199, V_DAYS, False), (200, V_DAYS[:14], False), (200, V_DAYS[:15], True)):
            found = cr1_test(PLAN, values(count, days))
            self.assertEqual((found["reason"] == UNDERPOWERED, found["p_one_sided"] is None, found["mean"] is None),
                             (not tested,) * 3)
        self.assertEqual(cr1_test(PLAN, values(300, V_DAYS), False)["reason"], UNDERPOWERED)
        self.assertIsNotNone(cr1_test(PLAN, values(150, V_DAYS), True)["p_one_sided"])
        self.assertEqual(cr1_test(PLAN, [])["reason"], UNDERPOWERED)

    def test_quantiles_are_nearest_rank_and_the_blind_spread_has_no_location(self):
        self.assertEqual(quantile_table(PLAN, range(1, 101)),
                         {"0.05": 5.0, "0.1": 10.0, "0.25": 25.0, "0.5": 50.0, "0.75": 75.0, "0.9": 90.0, "0.95": 95.0})
        self.assertEqual(quantile_table(PLAN, [Fraction(index, 3) for index in range(1, 201)])["0.5"], 33.3333)
        self.assertIsNone(quantile_table(PLAN, range(99)))
        values = [(V_DAYS[index % 16], index / 1000) for index in range(1, 101)]
        found = spread(PLAN, values)
        self.assertEqual(found, {"values": 100, "race_dates": 16, "sd": sample_sd([x for _, x in values]),
                                 "ranges": {"0.95-0.05": 0.09, "0.9-0.1": 0.08, "0.75-0.25": 0.05}})
        self.assertEqual(list(found), ["values", "race_dates", "sd", "ranges"])
        # A shift or a change of sign leaves it as it is: it holds no location and no direction.
        for change in (lambda x: x + 0.5, lambda x: -x):
            moved = spread(PLAN, [(day, change(x)) for day, x in values])
            self.assertEqual(moved["ranges"], found["ranges"])
            self.assertAlmostEqual(moved["sd"], found["sd"], places=12)
        self.assertEqual(spread(PLAN, values[:99]), {"values": 99, "race_dates": 16, "sd": None, "ranges": None})

    def test_rounding(self):
        value = {"mean": 0.123456789, "n": 3, "flag": True, "none": None, "cell": LAY_CELLS[0],
                 "list": [0.00000049, (1.5, "x")], "exact": Fraction(1, 3)}
        self.assertEqual(rounded(value), {"mean": 0.123457, "n": 3, "flag": True, "none": None, "cell": LAY_CELLS[0],
                                          "list": [0.0, [1.5, "x"]], "exact": Fraction(1, 3)})
        self.assertIs(rounded(value)["flag"], True)
        self.assertEqual(rounded(0.123456789, 4), 0.1235)

    def test_the_detectable_effect(self):
        # (t(0.99; 14) + t(0.8; 14)) x SD / sqrt(N) = (2.6245 + 0.8681) x 0.1 / 20.
        self.assertAlmostEqual(detectable(PLAN, 0.1, 400, 15), 3.4926 * 0.1 / 20, places=5)
        self.assertEqual([detectable(PLAN, None, 400, 15), detectable(PLAN, 0.1, 0, 15), detectable(PLAN, 0.1, 400, 1)],
                         [None, None, None])
        # The pooled SD with the test label's own count and race dates.
        rows = block(300, D_DAYS, [2.7, 2.8], label=D) + block(200, V_DAYS[:15], [2.6, 2.9]) + \
            block(100, D_DAYS, [2.4], label=D, filled=NONE) + block(100, V_DAYS, [2.4], filled=NONE)
        blind = blind_outcome(PLAN, rows, CENTRAL, AS_WRITTEN)
        sd = sample_sd([x for _, x in series(PLAN, [row for row in rows if row.fills[CENTRAL].kind], G, AS_WRITTEN)])
        self.assertEqual((blind["values"], blind["race_dates"], blind["sd"]), (500, 31, sd))
        self.assertEqual(blind["detectable"], {"values": 200, "race_dates": 15, FILLED_IS_ALL_PLACED: False,
                                               "effect": detectable(PLAN, sd, 200, 15)})
        self.assertEqual(set(blind), {"values", "race_dates", "sd", "ranges", FILLED_IS_ALL_PLACED, "detectable"})
        # 160 unfilled orders on the explore label and 60 on the test label: the pooled SD is over the fills, but
        # the test label's figure will be its all-placed one. The effect is then worked out from the SD over every
        # scored order, which the blind pass gives too, and never from the fills' SD with the all-placed count.
        rows = block(300, D_DAYS, [2.7, 2.8], label=D) + block(200, V_DAYS[:15], [2.6, 2.9]) + \
            block(160, D_DAYS, [2.4], label=D, filled=NONE) + block(60, V_DAYS, [2.4], filled=NONE)
        blind = blind_outcome(PLAN, rows, CENTRAL, AS_WRITTEN)
        everything = sample_sd([x for _, x in series(PLAN, rows, G, AS_WRITTEN)])
        self.assertEqual((blind["values"], blind["sd"], blind[FILLED_IS_ALL_PLACED]), (500, sd, False))
        self.assertNotAlmostEqual(everything, sd, places=3)
        self.assertEqual(blind["detectable"], {"values": 260, "race_dates": 16, FILLED_IS_ALL_PLACED: True,
                                               "effect": detectable(PLAN, everything, 260, 16)})
        self.assertEqual(everything, spread(PLAN, series(PLAN, rows, G, AS_WRITTEN))["sd"])


class SelectionIsBlindToHoldout(unittest.TestCase):
    COUNTS = {cell: (240, 16) for cell in PLAN.cells}

    def explore(self, bsps, side=LAY, price=2.5, fills_=240, unfilled=100):
        """One cell's PRO-D rows: fills under every model at the BSPs in turn by day, and unfilled orders."""
        return block(fills_, D_DAYS, bsps, label=D, side=side, price=price) + \
            block(unfilled, D_DAYS, [2.5], label=D, side=side, price=price, filled=NONE)

    def standard(self):
        """LAY at 2.5: the higher the BSP the better, so the middle time is best. BACK at 2.52: the lower the better,
        so the first time is best."""
        rows = {cell: self.explore(bsps) for cell, bsps in zip(LAY_CELLS, ([2.6, 2.7], [2.7, 2.8], [2.55, 2.6]))}
        rows.update({cell: self.explore(bsps, BACK, 2.52)
                     for cell, bsps in zip(BACK_CELLS, ([2.4, 2.45], [2.45, 2.5], [2.5, 2.55]))})
        return rows

    def passed(self, bsps):
        """Every cell's rows of both labels; bsps: the test label's BSPs, in turn by day."""
        return {cell: rows + block(240, V_DAYS, bsps, side=rows[0].side, price=float(rows[0].price), nulls=bsps)
                + block(200, V_DAYS, [2.5], side=rows[0].side, price=float(rows[0].price), filled=NONE)
                for cell, rows in self.standard().items()}

    def test_the_highest_explore_mean_of_net_held_is_chosen(self):
        found = select_primaries(PLAN, self.standard(), self.COUNTS, True)
        self.assertEqual(found.eligible, PLAN.cells)
        self.assertEqual(found.primaries, {BACK_RULE: BACK_CELLS[0], LAY_RULE: LAY_CELLS[1]})
        self.assertEqual(list(inspect.signature(select_primaries).parameters),
                         ["plan", "explore", "test_counts", "apart"])

    def test_the_gates_read_the_explore_models_fills_and_the_test_labels_counts(self):
        rows = self.standard()
        cases = {
            "199 central fills on the explore label": ({**rows, LAY_CELLS[1]: self.explore([2.7, 2.8], fills_=199)},
                                                       self.COUNTS),
            "the fills are the front model's only": (
                {**rows, LAY_CELLS[1]: block(240, D_DAYS, [2.7, 2.8], label=D, filled=(OPTIMISTIC,))
                 + block(100, D_DAYS, [2.5], label=D, filled=NONE)}, self.COUNTS),
            "199 pessimistic fills on the test label": (rows, {**self.COUNTS, LAY_CELLS[1]: (199, 16)}),
            "14 race dates on the test label": (rows, {**self.COUNTS, LAY_CELLS[1]: (240, 14)}),
        }
        for name, (explore, counts) in cases.items():
            with self.subTest(case=name):
                found = select_primaries(PLAN, explore, counts, True)
                self.assertNotIn(LAY_CELLS[1], found.eligible)
                self.assertEqual(found.primaries, {BACK_RULE: BACK_CELLS[0], LAY_RULE: LAY_CELLS[0]})
        # No eligible cell: the rule file has no primary, and is still one of the family's m.
        none = select_primaries(PLAN, rows, {**self.COUNTS, **dict.fromkeys(LAY_CELLS, (0, 0))}, True)
        self.assertEqual((none.eligible, none.primaries), (BACK_CELLS, {BACK_RULE: BACK_CELLS[0], LAY_RULE: None}))
        self.assertEqual(len(none.primaries), PLAN.family_m)
        # Under filled_is_all_placed the gate still counts the fills, and the mean is over every placed order.
        flagged = {**rows, LAY_CELLS[2]: self.explore([2.9], unfilled=99)}
        self.assertEqual(select_primaries(PLAN, flagged, self.COUNTS, True).primaries[LAY_RULE], LAY_CELLS[2])
        flagged[LAY_CELLS[2]] = self.explore([2.9], fills_=199, unfilled=99)
        self.assertEqual(select_primaries(PLAN, flagged, self.COUNTS, True).primaries[LAY_RULE], LAY_CELLS[1])

    def test_a_tie_goes_to_the_later_time(self):
        rows = self.standard()
        for first, second in ((0, 1), (1, 2), (0, 2)):
            tied = {**rows, LAY_CELLS[first]: self.explore([2.9]), LAY_CELLS[second]: self.explore([2.9])}
            self.assertEqual(select_primaries(PLAN, tied, self.COUNTS, True).primaries[LAY_RULE], LAY_CELLS[second])

    def test_the_mean_is_read_at_the_claim_end(self):
        # A BACK at 1.6 against BSP 1.5 beats one at 4.3 against 4.0 as written (0.0587 to 0.0585) and loses to it
        # at BSP + 0.01 (0.0517 to 0.0559): a short price moves more for one hundredth.
        rows = {**self.standard(), BACK_CELLS[0]: self.explore([1.5], BACK, 1.6),
                BACK_CELLS[2]: self.explore([4], BACK, 4.3)}
        self.assertEqual(select_primaries(PLAN, rows, self.COUNTS, True).primaries[BACK_RULE], BACK_CELLS[2])
        self.assertEqual(select_primaries(PLAN, rows, self.COUNTS, False).primaries[BACK_RULE], BACK_CELLS[0])

    def test_a_pass_hands_the_99_percent_switch_to_the_selection_and_the_labels(self):
        """The rows of test_the_mean_is_read_at_the_claim_end, through summary(): with every scored BSP a whole
        hundredth the two ends decide apart; with 2.5% of them off the grid the as-written figure decides alone."""
        def ends(value):
            return {rule: (item["kill"]["bsp_end"], item["test"]["bsp_end"])
                    for rule, item in value["primaries"].items()}

        rows = self.passed([2.8, 2.9])
        for cell, bsp, price in ((BACK_CELLS[0], 1.5, 1.6), (BACK_CELLS[2], 4, 4.3)):
            rows[cell] = self.explore([bsp], BACK, price) + [row for row in rows[cell] if row.label == V]
        # Half a hundredth more on every 20th row of the LAY cells: 117 of 4,680 scored BSPs.
        off_grid = {cell: [row._replace(bsp=row.bsp + Fraction(1, 200)) if cell.rule_id == LAY_RULE and place % 20 == 0
                           else row for place, row in enumerate(found)] for cell, found in rows.items()}
        on, off = (summary(PLAN, tally_of(found), "registered", "0" * 64) for found in (rows, off_grid))
        self.assertEqual([value["bsp_precision"] for value in (on, off)], [
            {"scored_bsps_whole_hundredths": 4680, "scored_bsps_not_whole_hundredths": 0,
             "bsp_ends_decide_apart": True},
            {"scored_bsps_whole_hundredths": 4563, "scored_bsps_not_whole_hundredths": 117,
             "bsp_ends_decide_apart": False}])
        self.assertEqual([value["selection"]["primaries"] for value in (on, off)], [
            {BACK_RULE: "close-v1-back@-120", LAY_RULE: "close-v1-lay@-300"},
            {BACK_RULE: "close-v1-back@-600", LAY_RULE: "close-v1-lay@-300"}])
        self.assertEqual(ends(on), {BACK_RULE: (AS_WRITTEN, PLUS_STEP), LAY_RULE: (PLUS_STEP, AS_WRITTEN)})
        self.assertEqual(ends(off), {BACK_RULE: (AS_WRITTEN, AS_WRITTEN), LAY_RULE: (AS_WRITTEN, AS_WRITTEN)})
        # Both ends are printed either way.
        for value in (on, off):
            self.assertEqual({tuple(item[block]["both"]) for item in value["primaries"].values()
                              for block in ("kill", "test")}, {BSP_ENDS})

    def test_changing_every_test_label_value_leaves_the_chosen_cells_unchanged(self):
        def inputs(rows):
            """What the selection is given, from every row of a pass: the explore label's rows, two counts per cell
            from the test label, and the switch (a count over both labels)."""
            everything = [row for cell in PLAN.cells for row in rows[cell]]
            explore = {cell: [row for row in rows[cell] if row.label == D] for cell in PLAN.cells}
            counts = {}
            for cell in PLAN.cells:
                part = filled_part(PLAN, scored(rows[cell], V), PESSIMISTIC)
                counts[cell] = (part.fills, part.fill_dates)
            return explore, counts, ends_apart(PLAN, *whole_hundredths(PLAN, everything))

        first, second = self.passed([2.8, 2.9]), self.passed([2.2, 2.3])       # good for a LAY, then bad for it
        given = [inputs(first), inputs(second)]
        self.assertEqual(given[0], given[1])
        self.assertEqual(given[0][1], self.COUNTS)
        chosen = [select_primaries(PLAN, *item) for item in given]
        self.assertEqual(chosen[0], chosen[1])
        self.assertEqual(chosen[0].primaries, {BACK_RULE: BACK_CELLS[0], LAY_RULE: LAY_CELLS[1]})
        # The values did change: the same primaries get other labels.
        labels = [{rule: item["label"] for rule, item in verdicts(PLAN, chosen[0].primaries, rows, given[0][2]).items()}
                  for rows in (first, second)]
        self.assertEqual(labels[0][LAY_RULE], "edge_candidate")
        self.assertNotEqual(labels[0], labels[1])
        # A row of the test label is refused, whatever it holds.
        with self.assertRaises(ValueError) as caught:
            select_primaries(PLAN, first, self.COUNTS, True)
        self.assertEqual(str(caught.exception), "select:not_the_explore_label")

    def test_in_the_registered_tables_the_explore_label_and_the_chosen_cells_do_not_move(self):
        """The same change through the runner's tables: every test-label value changes and no count does. What the
        registered summary gives of the explore label alone, and its selection, are the same bytes."""
        def of_label(value, label):
            cells = {cell: {name: tables[name][label] for name in ("classes", "fills", "fills_not_scored")}
                     for cell, tables in value["cells"].items()}
            return canonical_json({
                "cells": cells,
                "figures": {cell: by_label[label] for cell, by_label in value["figures"]["by_cell"].items()},
                "half_years": {cell: by_label.get(label) for cell, by_label in value["half_years"]["by_cell"].items()},
                "sensitivity": {cell: by_label[label]
                                for cell, by_label in value["sensitivity"]["without_first_fills"].items()}})

        first, second = (summary(PLAN, tally_of(self.passed(bsps)), "registered", "0" * 64)
                         for bsps in ([2.8, 2.9], [2.2, 2.3]))
        self.assertEqual(of_label(first, D), of_label(second, D))
        self.assertEqual(canonical_json(first["selection"]), canonical_json(second["selection"]))
        self.assertEqual(first["selection"], {"eligible": [f"{cell.rule_id}@{cell.at_s}" for cell in PLAN.cells],
                                              "primaries": {BACK_RULE: "close-v1-back@-600",
                                                            LAY_RULE: "close-v1-lay@-300"}})
        # The test label's counts are the same and its values are not; the labels follow the values.
        counts = [{cell: tables["fills"][V] for cell, tables in value["cells"].items()} for value in (first, second)]
        self.assertEqual(counts[0], counts[1])
        self.assertNotEqual(of_label(first, V), of_label(second, V))
        self.assertEqual([value["primaries"][LAY_RULE]["label"] for value in (first, second)],
                         ["edge_candidate", "unresolved"])
        held = first["figures"]["by_cell"]["close-v1-lay@-300"][D][AS_WRITTEN]["models"][CENTRAL]["filled"][NET_HELD]
        self.assertEqual((held["bets"], held["race_dates"], held["reason"]), (240, 16, None))
        self.assertGreater(held["mean"], 0.05)


class Verdicts(unittest.TestCase):
    CELL = LAY_CELLS[1]
    KILL, UNDER, EDGE, PILOT, UNRESOLVED = VERDICTS

    def verdict(self, rows, apart=True):
        found = verdicts(PLAN, {BACK_RULE: None, LAY_RULE: self.CELL}, {self.CELL: rows}, apart)
        self.assertEqual(list(found), [BACK_RULE, LAY_RULE])
        self.assertIsNone(found[BACK_RULE])
        return found[LAY_RULE]

    def rows(self, bsps=(2.7, 2.8), fills_=240, days=V_DAYS, nulls="bsp", explore=(2.7, 2.8), **kwargs):
        """A LAY primary at 2.5: on the test label, fills under every model at the BSPs in turn by day and 200
        unfilled orders; on the explore label 240 fills and 100 unfilled. nulls "bsp": the mid at u is the BSP, a
        null value of exactly 0."""
        nulls = list(bsps) if nulls == "bsp" else nulls
        return block(fills_, days, list(bsps), nulls=nulls, **kwargs) + block(200, V_DAYS, [2.5], filled=NONE) + \
            block(240, D_DAYS, list(explore), label=D) + block(100, D_DAYS, [2.5], label=D, filled=NONE)

    def test_edge_candidate_and_what_is_printed_with_every_label(self):
        found = self.verdict(self.rows())
        self.assertEqual((found["label"], found["blocked"], found["cell"]), (self.EDGE, None, self.CELL))
        self.assertEqual(found["checks"], {"rejected_under_holm": True, "mean_above_0_under_every_model": True,
                                           "mean_above_0_in_each_half_year": True})
        self.assertEqual(found["printed_with"], ("PRO 2015-16: a kill gate and a prior, never confirmation",
                                                 "The level of g was known from the whole year. Only the gap is new."))
        self.assertEqual(found["pessimistic_fill_rate"], 240 / 440)
        test, kill = found["test"], found["kill"]
        self.assertEqual((test["bets"], test["race_dates"], test["bsp_end"], test["reason"]),
                         (240, 16, AS_WRITTEN, None))
        self.assertLessEqual(test["p_one_sided"], 0.01)                          # m = 2: one primary needs 0.02 / 2
        self.assertEqual((test["holm_rejected"], test["holm_p_adjusted"]), (True, 2 * test["p_one_sided"]))
        self.assertEqual(test["lower_bound"], bound(test, PLAN.lower_bound_level, -1))
        self.assertGreater(test["lower_bound"], 0)
        # The kill is read on both labels pooled, on net_closed at the kill end, under the front model.
        self.assertEqual((kill["bets"], kill["fills"], kill["race_dates"], kill["bsp_end"]), (480, 480, 32, PLUS_STEP))
        self.assertGreater(kill["bound"], 0)
        self.assertNotIn("p_one_sided", kill)
        # Both nets at both BSP ends, the flags and the fills stand beside the label.
        for item in (test, kill):
            self.assertEqual({end: set(nets) for end, nets in item["both"].items()},
                             {AS_WRITTEN: {NET_CLOSED, NET_HELD}, PLUS_STEP: {NET_CLOSED, NET_HELD}})
        self.assertEqual(test["both"][AS_WRITTEN][NET_HELD], test["mean"])
        self.assertEqual(kill["both"][PLUS_STEP][NET_CLOSED], kill["mean"])
        self.assertGreater(test["both"][PLUS_STEP][NET_HELD], test["mean"])      # a LAY gains from a higher BSP
        self.assertEqual((kill[FILLED_IS_ALL_PLACED], test[FILLED_IS_ALL_PLACED], test["fills"]), (False, False, 240))
        self.assertEqual(found["models"], {model: {"fills": 240, FILLED_IS_ALL_PLACED: False, "mean": test["mean"]}
                                           for model in MODELS})
        self.assertEqual({half: (item["fills"], item["mean"] > 0) for half, item in found["half_years"].items()},
                         {"H1": (120, True), "H2": (120, True)})
        self.assertEqual((found["null"]["blocks"], found["null"]["why"], found["null"]["undefined"]), (False, None, 0))
        self.assertEqual(found["null"]["null"]["mean"], 0.0)

    def test_kill_optimistic_is_pooled_and_reads_the_optimistic_gate(self):
        # 120 front-model fills a label, clearly below BSP; 100 unfilled orders far above it.
        def lost(label, days):
            return block(120, days, [2.3, 2.4], label=label, filled=(OPTIMISTIC,))

        unfilled = block(100, V_DAYS, [3.5], filled=NONE)
        found = self.verdict(lost(D, D_DAYS[:8]) + lost(V, V_DAYS[:8]) + unfilled)
        self.assertEqual((found["label"], found["null"], found["blocked"]), (self.KILL, None, None))
        self.assertEqual((found["kill"]["fills"], found["kill"]["bets"], found["kill"]["race_dates"]), (240, 240, 16))
        self.assertLess(found["kill"]["bound"], 0)
        self.assertEqual(found["kill"]["bound"], bound(found["kill"], PLAN.kill_level, 1))
        # It comes first: the pessimistic model has no fill on the test label, which alone is underpowered.
        self.assertEqual((found["test"]["reason"], found["models"][PESSIMISTIC]["fills"]), (UNDERPOWERED, 0))
        # Neither label passes the gate alone.
        for rows in (lost(D, D_DAYS[:8]) + unfilled, lost(V, V_DAYS[:8]) + unfilled,
                     lost(V, V_DAYS[:8]) * 2 + unfilled):                        # 240 fills on 8 race dates
            alone = self.verdict(rows)
            self.assertEqual((alone["label"], alone["kill"]["reason"], alone["kill"]["bound"], alone["kill"]["mean"]),
                             (self.UNDER, UNDERPOWERED, None, None))
        # The gate is the front model's: 190 of its fills do not kill, whatever 300 pessimistic fills say.
        behind = block(300, V_DAYS, [2.2, 2.3], filled=(PESSIMISTIC,))
        found = self.verdict(block(190, D_DAYS, [2.3, 2.4], label=D, filled=(OPTIMISTIC,)) + behind + unfilled)
        self.assertEqual((found["label"], found["kill"]["fills"], found["kill"]["bound"]), (self.UNRESOLVED, 190, None))
        self.assertLess(found["test"]["mean"], -0.05)

    def test_the_kill_reads_net_closed_at_the_kill_end(self):
        # A LAY at 2.5 against BSP 2.49 or 2.5 as written loses a little; against 2.5 or 2.51 it does not.
        rows = self.rows(bsps=(2.49, 2.49, 2.49, 2.5), explore=(2.49, 2.49, 2.49, 2.5))
        apart, alone = self.verdict(rows, True), self.verdict(rows, False)
        self.assertEqual((alone["label"], alone["kill"]["bsp_end"]), (self.KILL, AS_WRITTEN))
        self.assertLess(alone["kill"]["bound"], 0)
        self.assertEqual((apart["label"], apart["kill"]["bsp_end"]), (self.UNRESOLVED, PLUS_STEP))
        self.assertGreater(apart["kill"]["mean"], 0)
        # Held, the same orders lose more than the commission-free closed net: the kill does not read that.
        self.assertLess(alone["kill"]["both"][AS_WRITTEN][NET_HELD], alone["kill"]["both"][AS_WRITTEN][NET_CLOSED])

    def test_a_low_mean_alone_never_prints_a_kill(self):
        # Below 0 on average, on 9 race dates of 16 far below and on 7 far above: the bound is above 0.
        swing = [2.0] * 9 + [3.5] * 7
        found = self.verdict(self.rows(bsps=swing, explore=swing))
        self.assertLess(found["kill"]["mean"], 0)
        self.assertGreater(found["kill"]["bound"], 0)
        self.assertEqual(found["label"], self.UNRESOLVED)
        # Far below 0 with too few fills: no kill either.
        few = self.verdict(block(199, V_DAYS, [2.0]) + block(100, V_DAYS, [2.5], filled=NONE))
        self.assertEqual((few["label"], few["kill"]["bound"]), (self.UNDER, None))
        # Equal values have no SE: no bound, no kill.
        flat = self.verdict(self.rows(bsps=[2.3], explore=[2.3]))
        self.assertEqual((flat["label"], flat["kill"]["reason"], flat["kill"]["bound"]),
                         (self.UNRESOLVED, "se_undefined", None))

    def test_underpowered_reads_the_pessimistic_gate_on_the_test_label(self):
        for name, rows in (("199 fills", self.rows(fills_=199)), ("14 race dates", self.rows(days=V_DAYS[:14])),
                           ("fills of the other models only", self.rows(filled=(OPTIMISTIC, CENTRAL)))):
            with self.subTest(case=name):
                found = self.verdict(rows)
                self.assertEqual((found["label"], found["test"]["reason"], found["test"]["p_one_sided"],
                                  found["test"]["lower_bound"], found["null"]), (self.UNDER, UNDERPOWERED, None, None,
                                                                                None))
                self.assertFalse(found["checks"]["rejected_under_holm"])
        # The explore label's fills do not help: 240 there, 199 on the test label.
        found = self.verdict(self.rows(fills_=199))
        self.assertEqual((found["kill"]["fills"], found["test"]["fills"], found["test"]["bets"]), (439, 199, 199))
        # Under filled_is_all_placed the gate still counts the fills: 199 of them among 298 placed orders.
        found = self.verdict(block(199, V_DAYS, [2.7, 2.8]) + block(99, V_DAYS, [2.5], filled=NONE))
        self.assertEqual((found["label"], found["test"]["fills"], found["test"]["bets"],
                          found["test"][FILLED_IS_ALL_PLACED]), (self.UNDER, 199, 298, True))
        found = self.verdict(block(200, V_DAYS, [2.7, 2.8]) + block(99, V_DAYS, [2.5], filled=NONE))
        self.assertEqual((found["test"]["fills"], found["test"]["bets"], found["test"][FILLED_IS_ALL_PLACED],
                          found["test"]["reason"]), (200, 299, True, None))
        self.assertEqual(found["test"]["mean"], found["models"][OPTIMISTIC]["mean"])   # every placed order

    def test_each_claim_condition_is_needed(self):
        # One model's mean at or below 0: 300 more front-model fills, far below BSP, on two race dates.
        worse = self.rows() + block(300, V_DAYS[:2], [2.0], filled=(OPTIMISTIC,))
        found = self.verdict(worse)
        self.assertEqual(found["checks"], {"rejected_under_holm": True, "mean_above_0_under_every_model": False,
                                           "mean_above_0_in_each_half_year": True})
        self.assertLess(found["models"][OPTIMISTIC]["mean"], 0)
        self.assertGreater(found["kill"]["bound"], 0)
        self.assertEqual((found["label"], found["null"], found["blocked"]), (self.PILOT, None, None))
        # One half-year at or below 0: H2 a hair below BSP after commission, H1 far above.
        found = self.verdict(self.rows(bsps=[2.8] * 8 + [2.53] * 8))
        self.assertEqual(found["checks"], {"rejected_under_holm": True, "mean_above_0_under_every_model": True,
                                           "mean_above_0_in_each_half_year": False})
        self.assertLess(found["half_years"]["H2"]["mean"], 0)
        self.assertEqual((found["label"], found["null"]), (self.PILOT, None))
        # One half-year with 99 fills.
        found = self.verdict(block(141, V_DAYS[:8], [2.7, 2.8], nulls=[2.7, 2.8])
                             + self.rows(fills_=99, days=V_DAYS[8:]))
        self.assertEqual({half: item["fills"] for half, item in found["half_years"].items()}, {"H1": 141, "H2": 99})
        self.assertTrue(found["half_years"]["H2"]["mean"] > 0)
        self.assertEqual((found["checks"]["mean_above_0_in_each_half_year"], found["label"]), (False, self.PILOT))
        # Not rejected: above 0 on average, but not by enough for its spread between race dates.
        found = self.verdict(self.rows(bsps=[2.4] * 4 + [2.9] * 4, explore=[2.8]))
        self.assertGreater(found["test"]["mean"], 0)
        self.assertGreater(found["test"]["p_one_sided"], 0.01)
        self.assertEqual((found["checks"]["rejected_under_holm"], found["null"]), (False, None))
        self.assertIn(found["label"], (self.PILOT, self.UNRESOLVED))

    def test_a_blocked_or_uncomputable_null_blocks_the_claim_and_never_kills(self):
        # The mid at u is the order's own price: the null is as large as the result, and clearly not 0.
        found = self.verdict(self.rows(nulls=[2.5]))
        self.assertEqual((found["label"], found["blocked"]), (self.PILOT, BLOCKED))
        self.assertEqual((found["null"]["blocks"], found["null"]["why"]), (True, NULL_BLOCKS[1]))
        self.assertEqual(found["null"]["null"]["mean"], found["null"]["main_same_orders"]["mean"])
        self.assertLess(found["null"]["null"]["p_two_sided"], 0.05)
        self.assertNotIn("p_two_sided", found["null"]["main_same_orders"])
        self.assertTrue(all(found["checks"].values()))
        # No null value at all, or too few of them: it cannot be computed, and that blocks.
        for nulls, defined in (([None], 0), ([2.7, None, None, None], 60)):
            found = self.verdict(self.rows(nulls=nulls))
            self.assertEqual((found["label"], found["blocked"], found["null"]["why"], found["null"]["undefined"]),
                             (self.PILOT, BLOCKED, NULL_BLOCKS[0], 240 - defined))
            self.assertEqual(found["null"]["null"]["bets"], defined)
        # Large but not clearly away from 0 (far above BSP on 10 race dates, far below on 6): it does not block.
        found = self.verdict(self.rows(nulls=[2.0] * 5 + [3.4] * 3))
        null = found["null"]
        self.assertGreater(abs(null["null"]["mean"]), 0.5 * abs(null["main_same_orders"]["mean"]))
        self.assertGreater(null["null"]["p_two_sided"], 0.05)
        self.assertEqual((found["label"], null["blocks"]), (self.EDGE, False))
        # null_gate alone: clearly away from 0 but under half the result does not block; over half of it does.
        rows = block(240, V_DAYS, [2.7, 2.8], nulls=[2.6, 2.7])
        small = null_gate(PLAN, rows, AS_WRITTEN)
        self.assertLess(small["null"]["p_two_sided"], 0.05)
        self.assertEqual((small["blocks"], small["null"]["mean"] < 0.5 * small["main_same_orders"]["mean"]),
                         (False, True))
        self.assertEqual(null_gate(PLAN, block(240, V_DAYS, [2.7, 2.8], nulls=[2.55, 2.65]), AS_WRITTEN)["why"],
                         NULL_BLOCKS[1])
        # A null that would block is not read for a result that is no claim, and changes nothing there.
        for bsps in ((2.5, 2.52), (2.45, 2.46)):
            with_null, without = (self.verdict(self.rows(bsps=bsps, nulls=nulls)) for nulls in ([2.5], [None]))
            self.assertEqual((with_null["null"], with_null["blocked"]), (None, None))
            self.assertEqual(with_null, without)

    def test_the_main_mean_over_the_same_orders_is_withheld_when_a_few_orders_have_no_null_value(self):
        def without(rows, count):
            """rows with the first count of the test label's fills left without a null value."""
            return [row._replace(null_mid=None, null_reason="market_not_open") for row in rows[:count]] + rows[count:]

        whole = self.verdict(self.rows(fills_=340))
        claim = figures(PLAN, scored(self.rows(fills_=340), V), AS_WRITTEN)["models"][PESSIMISTIC]["filled"][G]
        self.assertEqual((whole["null"]["main_same_orders"]["mean"], claim["bets"]), (claim["mean"], 340))
        self.assertIs(whole["null"]["at_least_the_fraction"], False)
        # 1 or 99 of the 340 have no null value: the mean over the other 339 or 241, beside the claim's mean over
        # all 340, would give the mean g of those few. The gate is decided as before; the mean is not printed.
        for count in (1, 99):
            found = self.verdict(without(self.rows(fills_=340), count))
            null = found["null"]
            self.assertEqual((found["label"], null["blocks"], null["why"], null["undefined"]),
                             (self.EDGE, False, None, count))
            main = null["main_same_orders"]
            self.assertEqual((main["bets"], main["race_dates"], main["mean"], main["se"], main["ci_low"],
                              main["reason"]), (340 - count, 16, None, None, None, REST_NOT_GIVEN))
            self.assertEqual((null["null"]["bets"], null["null"]["mean"], null["at_least_the_fraction"]),
                             (340 - count, 0.0, False))
            # A null that blocks still blocks, and says that it reached the fraction.
            blocked = self.verdict(without(self.rows(fills_=340, nulls=[2.5]), count))["null"]
            self.assertEqual((blocked["blocks"], blocked["why"], blocked["at_least_the_fraction"],
                              blocked["main_same_orders"]["mean"]), (True, NULL_BLOCKS[1], True, None))
            self.assertIsNotNone(blocked["null"]["mean"])
        # With none or 100 undefined, no small group can be worked out, and the mean is given.
        for count in (0, 100):
            null = null_gate(PLAN, without(block(340, V_DAYS, [2.7, 2.8], nulls=[2.7, 2.8]), count), AS_WRITTEN)
            self.assertEqual((null["undefined"], null["main_same_orders"]["bets"],
                              null["main_same_orders"]["reason"]), (count, 340 - count, None))
            self.assertIsNotNone(null["main_same_orders"]["mean"])
        # Too few null values to compute it: no fraction, and it keeps its own reason.
        null = null_gate(PLAN, without(block(240, V_DAYS, [2.7, 2.8], nulls=[2.7, 2.8]), 60), AS_WRITTEN)
        self.assertEqual((null["why"], null["at_least_the_fraction"], null["main_same_orders"]["reason"]),
                         (NULL_BLOCKS[0], None, UNDERPOWERED))

    def test_measurement_pilot_candidate_and_unresolved(self):
        # At BSP or a hair above it: a small loss after commission, with a lower bound above -1.5 points.
        found = self.verdict(self.rows(bsps=(2.5, 2.52)))
        self.assertLess(found["test"]["mean"], 0)
        self.assertGreater(found["test"]["lower_bound"], -0.015)
        self.assertEqual((found["label"], found["checks"]["rejected_under_holm"]), (self.PILOT, False))
        self.assertGreater(found["kill"]["bound"], 0)
        # Two points below BSP: the lower bound is below the floor.
        found = self.verdict(self.rows(bsps=(2.45, 2.46)))
        self.assertLess(found["test"]["lower_bound"], -0.015)
        self.assertEqual(found["label"], self.UNRESOLVED)
        self.assertGreater(found["kill"]["bound"], 0)                            # the explore label's fills are good
        # The floor is the registered one.
        self.assertEqual(PLAN.pilot_floor, Fraction(-15, 1000))

    def test_the_first_label_that_applies_wins(self):
        # Good on the test label, and killed: 2,400 front-model fills far below BSP on the explore label.
        good = self.rows()[:440]
        killed = good + block(2400, D_DAYS, [2.3, 2.4], label=D, filled=(OPTIMISTIC,))
        self.assertEqual(self.verdict(good + block(240, D_DAYS, [2.7, 2.8], label=D))["label"], self.EDGE)
        found = self.verdict(killed)
        self.assertTrue(all(found["checks"].values()))
        self.assertEqual((found["label"], found["null"]), (self.KILL, None))
        # A result fit for a pilot label on 199 fills is underpowered.
        self.assertEqual(self.verdict(self.rows(bsps=(2.5, 2.52), fills_=199))["label"], self.UNDER)
        self.assertEqual(VERDICTS, ("kill_optimistic", "underpowered", "edge_candidate", "measurement_pilot_candidate",
                                    "unresolved"))

    def test_holm_counts_a_rule_file_with_no_primary(self):
        # A BACK at 2.52 against BSP 2.37 or 2.52 by day: a one-sided p between 0.01 and 0.02.
        back = block(240, V_DAYS, [2.37, 2.52], side=BACK, price=2.52, nulls=[2.37, 2.52]) + \
            block(200, V_DAYS, [2.52], side=BACK, price=2.52, filled=NONE)
        cells = {BACK_RULE: BACK_CELLS[0], LAY_RULE: self.CELL}
        rows = {BACK_CELLS[0]: back, self.CELL: self.rows()}
        alone = verdicts(PLAN, {**cells, LAY_RULE: None}, rows, False)[BACK_RULE]
        self.assertTrue(0.01 < alone["test"]["p_one_sided"] < 0.02)
        self.assertEqual((alone["test"]["holm_rejected"], alone["label"]), (False, self.PILOT))
        self.assertEqual(alone["test"]["holm_p_adjusted"], 2 * alone["test"]["p_one_sided"])
        # With the LAY primary rejected first at 0.01, the BACK is tested at 0.02.
        both = verdicts(PLAN, cells, rows, False)
        self.assertEqual(both[BACK_RULE]["test"]["p_one_sided"], alone["test"]["p_one_sided"])
        self.assertEqual((both[BACK_RULE]["test"]["holm_rejected"], both[BACK_RULE]["label"]), (True, self.EDGE))
        self.assertEqual(both[LAY_RULE]["label"], self.EDGE)
        # A BACK's claim reads BSP + 0.01 when the ends decide apart, its kill the BSP as written.
        apart = verdicts(PLAN, cells, rows, True)[BACK_RULE]
        self.assertEqual((apart["test"]["bsp_end"], apart["kill"]["bsp_end"]), (PLUS_STEP, AS_WRITTEN))
        self.assertLess(apart["test"]["mean"], alone["test"]["mean"])
        self.assertEqual(verdicts(PLAN, dict.fromkeys(cells), rows, True), dict.fromkeys(cells))


# ---------------------------------------------------------------------------------------------------------------
# The pass runner: one synthetic archive, a registration with lowered gates, and a ledger in a temporary folder

COMMIT = "0123456789abcdef0123456789abcdef01234567"
WINTER = datetime(2015, 12, 1, 14, 0, tzinfo=timezone.utc)
OUTPUTS = ("summary.json", "summary.md", "receipt.json")
STUDY = "close-v1"


def clock():
    return "2026-11-11T00:00:00.000000Z"


def offs_of(label, start, count):
    """The first `count` 14:00 UTC offs from start whose race date has the partition label."""
    found, off = [], start
    while len(found) < count:
        if PLAN.split.label(off.date().isoformat()) == label:
            found.append(off)
        off += timedelta(days=1)
    return found


EDGE = datetime(2016, 4, 30, 23, 30, tzinfo=timezone.utc)                    # 1 May in London: no label


def study_offs():
    """The eight offs of the study archive's dated markets: two race dates for each partition label in each
    half-year, the explore label's four first."""
    return offs_of(D, OFF, 2) + offs_of(D, WINTER, 2) + offs_of(V, OFF, 2) + offs_of(V, WINTER, 2)


def study_members():
    """[(member path, messages)] and the market ids. Eight race dates, two for each partition label in each
    half-year, with three markets each: one with a trade through both orders' prices at off - 200 s and at
    off - 60 s (every model fills, on both sides, at every time), one with GBP 27 of backer's stake at 2.5 at
    off - 200 s (the LAY's front and central models fill at the first two times) and one with no trade. The
    favourite's BSP is 2.6 or 2.7 by date on the explore label and 2.7 or 2.8 on the test label, and 0.05 more in
    the second market. Then five single markets: four runners; a runner removed at off - 200 s after a trade
    through; a BASIC file; a race date with no label; and a file with no definition."""
    members = []
    dated = [(off, (2.6 if index < 4 else 2.7) + index % 2 / 10) for index, off in enumerate(study_offs())]
    through = [(200, FAV, [[2.48, 1.0], [2.54, 1.0]]), (60, FAV, [[2.46, 1.0], [2.56, 1.0]])]
    for day, (off, bsp) in enumerate(dated):
        for kind, (trades, more) in enumerate(((through, 0), ([(200, FAV, [[2.5, 54.0]])], 0.05), ((), 0))):
            market_id = f"1.1810{day}{kind}001"
            members.append((path(market_id, off), lp_market(market_id, off, trades=trades,
                                                            bsps={**BSPS, FAV: round(bsp + more, 2)})))
    off = dated[4][0]                                                        # the first race date of the test label
    removal = md("1.181099001", ms(off) - 200_000,
                 definition(off, [runner(s, "REMOVED" if s == R6 else "ACTIVE") for s in SIDS]))
    statuses = {s: "REMOVED" if s == R6 else "WINNER" if s == FAV else "LOSER" for s in SIDS}
    edge = EDGE
    members += [
        (path("1.181098001", off), lp_market("1.181098001", off, runners=SIDS[:4])),
        (path("1.181099001", off), lp_market("1.181099001", off, extra=[removal], statuses=statuses,
                                             trades=[(250, FAV, [[2.48, 1.0]])])),
        (fx.path("1.181097001", when=off, plan="BASIC"), lp_market("1.181097001", off)),
        (path("1.181096001", edge), lp_market("1.181096001", edge)),
        (path("1.181095001", off), [message for message in lp_market("1.181095001", off)
                                    if not any("marketDefinition" in change for change in message["mc"])]),
    ]
    return members, [name.rsplit("/", 1)[1][:-4] for name, _ in members]


def build_root(folder, members, name="data"):
    """A data root under folder holding one ingested and indexed archive of members."""
    root = folder / name
    root.mkdir()
    tar = fx.make_tar(folder / f"{name}.tar", [(member, bz2.compress(fx.lines(messages)))
                                               for member, messages in members])
    if ingest_archive(tar, root)["status"] != "ingested" or index_archives(root)["status"] != "indexed":
        raise AssertionError("the synthetic archive was not ingested and indexed")
    return root


def lowered(value):
    """Gates low enough for the synthetic archive: 2 values, 2 race dates, 2 fills a half-year."""
    value["statistics"].update(min_values=2, min_tested_values=2, min_tested_race_dates=2)
    value["uncertainty"]["min_clusters"] = 2
    value["verdicts"]["half_year_min_fills"] = 2


def write_registration(folder, **changes):
    """(path, sha256) of a registration written with its split file and rule files into folder, in the shape the
    runner reads: studies/close-v1.json, the split file beside studies/, the rule files in rules/."""
    raw, split_raw, rules = files(**changes)
    for name in ("studies", "rules"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    (folder / "studies" / "close-v1.json").write_bytes(raw)
    (folder / "partitions-v1.json").write_bytes(split_raw)
    for name, data in rules.items():
        (folder / "rules" / name).write_bytes(data)
    return folder / "studies" / "close-v1.json", sha(raw)


def cli(*arguments):
    """(exit code, stdout) of the runner's main()."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(list(arguments))
    return code, out.getvalue()


def outputs_of(out):
    return {name: (out / name).read_bytes() for name in OUTPUTS}


_SHARED = {}


def shared():
    """One blind and one registered pass through main() over the study archive, run once for this module: a
    namespace of the folder, the data root, the registration and its sha256, the ledger directory, each pass's
    (exit code, stdout) and output bytes, and the market ids."""
    if not _SHARED:
        folder = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(folder.cleanup)
        base = Path(folder.name).resolve()
        members, ids = study_members()
        root = build_root(base, members)
        registration, digest = write_registration(base / "research", registration=lowered)
        ledger = base / "ledger"
        ledger.mkdir(mode=0o700)
        common = ["--study", STUDY, "--registration-sha256", digest, "--registration", str(registration),
                  "--archive-root", str(root), "--code-commit", COMMIT, "--ledger", str(ledger)]
        blind = cli(*common, "--pass", "blind", "--out", str(base / "blind"), "--ledger-head", "none")
        head = json.loads(blind[1]).get("ledger_head", "none")
        registered = cli(*common, "--pass", "registered", "--out", str(base / "registered"), "--ledger-head", head,
                         "--blind-receipt", str(base / "blind" / "receipt.json"))
        _SHARED["value"] = SimpleNamespace(base=base, root=root, registration=registration, sha=digest, ledger=ledger,
                                           ids=ids, blind=blind, registered=registered)
    return _SHARED["value"]


def one_market_root(folder):
    """A data root with the standard market alone: one race date of the test label, no fill."""
    return build_root(folder, [(path(M), lp_market())], name="one")


def raw_members(root):
    return sorted((root / "raw").rglob("*.bz2"))


def entry_in(directory, head="none", **changes):
    """Append one entry to the study ledger in directory by hand; the new head's text."""
    request = PassRequest(**{**dict(
        study=STUDY, lineage="close", kind="look", partitions=PLAN.labels, partitions_sha256=PLAN.split.sha256,
        registration_sha256="1" * 64, input_sha256="2" * 64, source_descriptor_sha256="3" * 64,
        code_commit=COMMIT), **changes})
    seq, digest = StudyLedger.open(directory, head).record(request, SPLIT_RAW, "4" * 64, "completed", clock)
    return f"{seq}:{digest}"


def tally_of(rows):
    """A Tally holding hand-built rows {Cell: [OrderRow]}, each under its own partition label."""
    tally = Tally(PLAN)
    for cell, found in rows.items():
        for row in found:
            tally.rows[cell][row.label].append(row)
    return tally


def walk_floats(value, trail=()):
    """(the keys leading to it, the float) of every float in a JSON value."""
    if isinstance(value, float):
        yield trail, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from walk_floats(item, trail + (key,))
    elif isinstance(value, list):
        for item in value:
            yield from walk_floats(item, trail)


# Keys that hold a statistic of an outcome, a test or a label: the registered pass's, under its own tables only.
REGISTERED_ONLY = {"mean", "median", "estimate", "se", "df", "t", "t_critical", "ci_low", "ci_high", "p_one_sided",
                   "p_two_sided", "holm_p_adjusted", "holm_rejected", "bound", "lower_bound", "label", "blocked",
                   "test", "kill", "null", "gap", "checks", "quantiles", "figures", "half_years", "selection",
                   "primaries", "sensitivity", "eligible"}
TESTED = {"t", "p_one_sided", "p_two_sided", "holm_p_adjusted", "holm_rejected", "label", "blocked", "bound",
          "lower_bound", "checks"}


class Run(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name).resolve()
        self.ledger = self.tmp / "ledger"
        self.ledger.mkdir(mode=0o700)
        self.given = shared()

    def run_pass(self, name, out, head="none", **kwargs):
        arguments = dict(registration_path=self.given.registration, clock=clock)
        arguments.update(kwargs)
        return run_study(arguments.pop("study", STUDY), name, arguments.pop("sha", self.given.sha),
                         arguments.pop("root", self.given.root), out, arguments.pop("commit", COMMIT),
                         arguments.pop("ledger", self.ledger), head, **arguments)

    def assertRefused(self, result, problem, out=None):
        self.assertEqual(result, {"status": "refused", "problems": [problem]})
        self.assertFalse((self.ledger / LEDGER_FILE).exists())
        if out is not None:
            self.assertFalse(out.exists())

    def test_blind_then_registered(self):
        given = self.given
        self.assertEqual((given.blind[0], given.registered[0]), (0, 0), (given.blind[1], given.registered[1]))
        blind, registered = json.loads(given.blind[1]), json.loads(given.registered[1])
        files = {name: outputs_of(given.base / name) for name in ("blind", "registered")}
        cells = [f"{cell.rule_id}@{cell.at_s}" for cell in PLAN.cells]
        self.assertEqual((blind["status"], blind["study"], blind["pass"], blind["records"], blind["replayed_markets"]),
                         ("completed", STUDY, "blind", 29, 26))
        self.assertEqual(blind["issued"], dict.fromkeys(cells, 25))
        self.assertEqual(set(blind["scored"].values()), {24, 25})

        # The ledger: one look, then the one registered entry, each written with its pass's tables hash.
        look, test = verify(given.ledger / LEDGER_FILE)
        for entry, result, name in ((look, blind, "blind"), (test, registered, "registered")):
            self.assertEqual(result["ledger_head"], f"{entry['seq']}:{entry['entry_sha256']}")
            self.assertEqual(result["summary_sha256"], sha(files[name]["summary.json"]))
            self.assertEqual((entry["study"], entry["lineage"], entry["partitions"], entry["partitions_sha256"],
                              entry["registration_sha256"], entry["input_sha256"], entry["code_commit"],
                              entry["tables_sha256"], entry["result"]),
                             (STUDY, "close", ["PRO-D", "PRO-V"], PLAN.split.sha256, given.sha, result["input_sha256"],
                              COMMIT, result["summary_sha256"], "completed"))
            self.assertFalse(result["ledger_repeat"])
        self.assertEqual([(entry["seq"], entry["kind"], entry["m"], entry["alpha"]) for entry in (look, test)],
                         [(0, "look", 0, "0"), (1, "registered", 2, "0.02")])
        self.assertEqual((blind["ledger_counts"], registered["ledger_counts"]),
                         ({"looks": 1, "registered": 0}, {"looks": 1, "registered": 1}))

        # The receipts: lp-v1's keys and the lane's.
        receipts = {name: json.loads(files[name]["receipt.json"]) for name in files}
        for name, receipt in receipts.items():
            result = blind if name == "blind" else registered
            self.assertEqual(set(receipt), {
                "kind", "schema_version", "study", "pass", "status", "registration_sha256", "partitions_sha256",
                "input_sha256", "input", "code_commit", "source_descriptor_sha256", "interpreter",
                "blind_receipt_sha256", "ledger_head", "ledger_entry", "ledger_repeat", "ledger_counts",
                "bytes_walked", "bytes_drained", "counts", "outputs", "started_at", "recorded_at"})
            self.assertEqual((receipt["study"], receipt["pass"], receipt["status"], receipt["registration_sha256"],
                              receipt["partitions_sha256"], receipt["code_commit"], receipt["ledger_head"],
                              receipt["ledger_entry"], receipt["ledger_counts"]),
                             (STUDY, name, "completed", given.sha, PLAN.split.sha256, COMMIT, result["ledger_head"],
                              result["ledger_head"], result["ledger_counts"]))
            self.assertIs(result["ledger_lock_left"], False)
            self.assertEqual(receipt["outputs"]["summary.json"],
                             {"sha256": sha(files[name]["summary.json"]), "bytes": len(files[name]["summary.json"])})
            for key in ("started_at", "recorded_at"):
                self.assertRegex(receipt[key], r"^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")
            self.assertEqual(result["receipt_sha256"], sha(files[name]["receipt.json"]))
        self.assertIsNone(receipts["blind"]["blind_receipt_sha256"])
        self.assertEqual(receipts["registered"]["blind_receipt_sha256"], sha(files["blind"]["receipt.json"]))
        # Every member of a record that reached the book was read or drained whole; the BASIC file, the file with
        # no definition and the one whose race date has no label were never opened.
        opened = sum(member.stat().st_size for member in raw_members(given.root)
                     if member.name[:-4] not in ("1.181097001", "1.181096001", "1.181095001"))
        self.assertEqual(len(raw_members(given.root)), 29)
        for receipt in receipts.values():
            self.assertGreater(receipt["bytes_walked"], 0)
            self.assertEqual(receipt["bytes_walked"] + receipt["bytes_drained"], opened)
        for name in files:
            out = given.base / name
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
            self.assertEqual({stat.S_IMODE((out / item).stat().st_mode) for item in OUTPUTS}, {0o600})

        # The blind tables.
        value = json.loads(files["blind"]["summary.json"])
        self.assertEqual(sorted(value["cells"]), sorted(cells))
        self.assertEqual((value["partitions"], value["partitions_sha256"]),
                         ({"explore_label": "PRO-D", "test_label": "PRO-V"}, PLAN.split.sha256))
        coverage = value["coverage"]
        self.assertEqual((coverage["records_without_an_anchor"], coverage["records_without_a_registered_label"]),
                         (1, 1))
        self.assertEqual(set(coverage["by_month"]), set(PLAN.months) | {"outside_registered_months", "no_anchor"})
        self.assertEqual(sum(count for row in coverage["by_month"].values() for count in row["records"].values()), 28)
        self.assertEqual(coverage["by_month"]["2015-06"], {
            "records": {"PRO-D": 6, "PRO-V": 8, "no_registered_label": 0}, "orders": {"PRO-D": 36, "PRO-V": 42}})
        for cell, tables in value["cells"].items():
            self.assertEqual(sum(tables["codes"].values()), 29, cell)
            self.assertEqual({code: count for code, count in tables["codes"].items() if count},
                             {"plan_not_registered": 1, "no_view": 1, "partition_not_registered": 1,
                              "too_few_active_runners": 1, "issued": 25})
            self.assertEqual(sum(count for classes in tables["classes"].values() for count in classes.values()), 25)
            self.assertEqual(tables["flags_missing_at_t"], {"bsp_market": 0})
        tables = value["cells"]["close-v1-lay@-300"]
        self.assertEqual((tables["rule"], tables["side"], tables["at_s"], tables["pull_s"]),
                         (LAY_RULE, LAY, -300, -120))
        self.assertEqual(tables["classes"]["PRO-V"]["post_decision_removal"], 1)
        self.assertEqual(tables["fills"]["PRO-V"][CENTRAL], {
            "scored": 12, "fills": 8, "fill_rate": round(8 / 12, 6),
            "by_kind": {THROUGH: 4, CROSS: 0, VOLUME: 4}, "within_s_of_t": {"1": 0, "2": 0},
            "after_the_hour": dict.fromkeys(FILL_KINDS, 0), "left_the_market_not_open": 0,
            "turned_the_market_in_play": 0, "ended_by": {"pull": 4, "lapse": 0}})
        self.assertEqual(tables["scored_orders_with_a_batch_begun_with_no_status"], 0)
        self.assertEqual((tables["fills"]["PRO-V"][PESSIMISTIC]["fills"],
                          tables["fills"]["PRO-D"][OPTIMISTIC]["fills"]), (4, 8))
        self.assertEqual(tables["fills_not_scored"]["PRO-V"]["post_decision_removal"], dict.fromkeys(MODELS, 1))
        self.assertEqual(set(tables["fills_not_scored"]["PRO-D"]), set(CLASSES) - {SCORED})
        self.assertEqual(tables["queue_at_t"]["queue_quantiles"]["0.5"], 50.0)
        self.assertEqual((tables["seconds_to_fill"][CENTRAL]["fills"],
                          tables["seconds_to_fill"][CENTRAL]["seconds_quantiles"]["0.5"]), (16, 100.0))
        self.assertEqual((tables["trd_changes"], tables["null_values"]["defined"]),
                         (dict(zip(TRD_COUNTS, (25, 0, 0))), 24))
        self.assertEqual(value["bsp_precision"], {"scored_bsps_whole_hundredths": 146,
                                                  "scored_bsps_not_whole_hundredths": 0, "bsp_ends_decide_apart": True})
        self.assertEqual(tables["spread_of_g"]["filled"][PESSIMISTIC]["detectable"]["values"], 4)

        # The registered tables: the selection, then one label per primary, each from hand-checkable values.
        value = json.loads(files["registered"]["summary.json"])
        self.assertEqual(value["selection"], {"eligible": cells, "primaries": {BACK_RULE: "close-v1-back@-120",
                                                                              LAY_RULE: "close-v1-lay@-300"}})
        self.assertEqual(cells[0], "close-v1-back@-600")                         # eligible keeps the plan's order
        back, lay = value["primaries"][BACK_RULE], value["primaries"][LAY_RULE]
        self.assertEqual((back["label"], back["blocked"], back["cell"]),
                         ("kill_optimistic", None, "close-v1-back@-120"))
        self.assertEqual((lay["label"], lay["blocked"], lay["cell"]),
                         ("measurement_pilot_candidate", BLOCKED, "close-v1-lay@-300"))
        for item in (back, lay):
            self.assertEqual(item["printed_with"], [PLAN.label_note, PLAN.exposure_note])
            self.assertTrue(item["kill"]["figures_given"])
        # Every scored BSP here is a whole hundredth, so the ends decide apart: a BACK's claim and a LAY's kill
        # read BSP + 0.01, a BACK's kill and a LAY's claim the BSP as written.
        self.assertEqual([(item["kill"]["bsp_end"], item["test"]["bsp_end"]) for item in (back, lay)],
                         [(AS_WRITTEN, PLUS_STEP), (PLUS_STEP, AS_WRITTEN)])
        # The BACK at 2.52 against BSPs of 2.6 to 2.8: below 0 at every fill, so the bound is too.
        closed = [2.52 / bsp - 1 for bsp in (2.6, 2.7, 2.6, 2.7, 2.7, 2.8, 2.7, 2.8)]
        self.assertEqual((back["kill"]["bets"], back["kill"]["fills"]), (8, 8))
        self.assertAlmostEqual(back["kill"]["mean"], sum(closed) / 8, places=6)
        self.assertLess(back["kill"]["bound"], 0)
        self.assertEqual(back["kill"]["orders_by_label"], {"PRO-D": 4, "PRO-V": 4})
        # The LAY at 2.5, held: (1 - 2.5 / BSP) - 0.02 x (1 - 1 / BSP) over the test label's four full fills.
        held = [(1 - 2.5 / bsp) - 0.02 * (1 - 1 / bsp) for bsp in (2.7, 2.8, 2.7, 2.8)]
        self.assertEqual((lay["test"]["bets"], lay["test"]["fills"], lay["test"]["race_dates"]), (4, 4, 4))
        self.assertAlmostEqual(lay["test"]["mean"], sum(held) / 4, places=6)
        self.assertTrue(lay["test"]["holm_rejected"])
        self.assertAlmostEqual(lay["test"]["holm_p_adjusted"], 2 * lay["test"]["p_one_sided"], places=5)
        self.assertEqual(lay["checks"], {"rejected_under_holm": True, "mean_above_0_under_every_model": True,
                                         "mean_above_0_in_each_half_year": True})
        # The book never moves in these markets, so the mid at a random time scores as the fill does: it blocks.
        self.assertEqual((lay["null"]["blocks"], lay["null"]["why"], lay["null"]["null"]["bets"]),
                         (True, NULL_BLOCKS[1], 4))
        self.assertAlmostEqual(lay["null"]["null"]["mean"], sum(1 - 2.51 / bsp for bsp in (2.7, 2.8)) / 2, places=6)
        self.assertGreater(lay["test"]["lower_bound"], -0.015)
        self.assertEqual((lay["gap"]["bets"], lay["pessimistic_fill_rate"]), ([4, 8], round(4 / 12, 6)))
        self.assertEqual({half: item["fills"] for half, item in lay["half_years"].items()}, {"H1": 2, "H2": 2})
        # T5: every cell and label at both BSP ends; a figure checked by hand.
        figures_ = value["figures"]
        self.assertEqual((figures_["cells"], figures_["sqrt_2_ln_c"], figures_["headings"]),
                         (6, round(math.sqrt(2 * math.log(6)), 6),
                          {"PRO-D": "exploratory", "PRO-V": "not tested, unadjusted",
                           "primary": "primary: tested in T9 only; this row is descriptive"}))
        self.assertEqual(value["half_years"]["headings"], figures_["headings"])
        self.assertEqual(value["sensitivity"]["headings"], figures_["headings"])
        self.assertEqual(sorted(figures_["by_cell"]), sorted(cells))
        placed = [1 - 2.5 / bsp for bsp in (2.6, 2.65, 2.6, 2.7, 2.75, 2.7)]
        group = figures_["by_cell"]["close-v1-lay@-300"]["PRO-D"]
        self.assertEqual(set(group), set(BSP_ENDS))
        self.assertEqual((group[AS_WRITTEN]["placed"]["bets"], group[AS_WRITTEN]["placed"]["race_dates"]), (12, 4))
        self.assertAlmostEqual(group[AS_WRITTEN]["placed"]["mean"], sum(placed) / 6, places=6)
        self.assertGreater(group[PLUS_STEP]["placed"]["mean"], group[AS_WRITTEN]["placed"]["mean"])
        self.assertEqual(set(group[AS_WRITTEN]["models"][CENTRAL]),
                         {"fills", "fill_race_dates", FILLED_IS_ALL_PLACED, "filled", "value_per_placed", "gap"})
        # T7: every cell on the explore label, the primaries alone on the test label.
        halves = value["half_years"]
        self.assertEqual(halves["cells"], 12)
        self.assertEqual({cell: sorted(by_label) for cell, by_label in halves["by_cell"].items()},
                         {cell: ["PRO-D", "PRO-V"] if cell in ("close-v1-back@-120", "close-v1-lay@-300")
                          else ["PRO-D"] for cell in cells})
        self.assertEqual(sorted(halves["by_cell"]["close-v1-lay@-300"]["PRO-V"]), ["H1", "H2"])
        self.assertEqual(halves["by_cell"]["close-v1-lay@-300"]["PRO-V"]["H1"][AS_WRITTEN]["placed"]["bets"], 6)
        # T10: no fill came in the first second, so the filled figure stands.
        later = value["sensitivity"]["without_first_fills"]["close-v1-lay@-300"]["PRO-V"][PESSIMISTIC]
        self.assertEqual((later["filled_figure_stands"], later["ends"][AS_WRITTEN][NET_HELD]["mean"]),
                         (True, lay["test"]["mean"]))
        self.assertEqual(value["sensitivity"]["bsp_book"]["PRO-V"]["markets"], 13)
        # A p-value, a test or a label is under a primary and nowhere else.
        rest = {key: item for key, item in value.items() if key != "primaries"}
        self.assertEqual(TESTED & set(walk_keys(rest)), set())
        self.assertLessEqual(TESTED, set(walk_keys(value["primaries"])))
        text = files["registered"]["summary.md"].decode()
        for phrase in (PLAN.label_note, PLAN.exposure_note, "label kill_optimistic",
                       "label measurement_pilot_candidate", "blocked: claim_blocked_by_placebo",
                       "PRO-V: not tested, unadjusted", "PRO-D: exploratory", "c = 6; sqrt(2 ln c) = 1.893018",
                       "c = 12; sqrt(2 ln c) =", "under independence; clustering makes it larger",
                       "both nets at both BSP ends", "points of stake"):
            self.assertIn(phrase, text)
        self.assertEqual(text.count("| p one-sided |"), 2)                       # the two primaries' tests
        # A primary's own rows on the test label are not headed "not tested": T9 tests that cell.
        tested = "primary: tested in T9 only; this row is descriptive"
        for cell in cells:
            chosen = cell in ("close-v1-back@-120", "close-v1-lay@-300")
            self.assertIn(f"### {cell}, PRO-V: {tested if chosen else 'not tested, unadjusted'}\n", text)
            self.assertNotIn(f"### {cell}, PRO-V: {'not tested, unadjusted' if chosen else tested}", text)
            self.assertIn(f"### {cell}, PRO-D: exploratory\n", text)
            self.assertIn(f"| {cell} | PRO-V | {tested if chosen else 'not tested, unadjusted'} |", text)
        self.assertEqual(text.count(f"PRO-V: {tested}, H"), 4)                   # T7: two primaries, two half-years
        self.assertNotIn(f"PRO-D: {tested}", text)

    def test_a_repeat_appends_nothing_and_gives_the_same_bytes(self):
        given = self.given
        head = json.loads(given.registered[1])["ledger_head"]
        ledger_bytes = (given.ledger / LEDGER_FILE).read_bytes()
        again = {"blind": self.run_pass("blind", self.tmp / "blind", head, ledger=given.ledger),
                 "registered": self.run_pass("registered", self.tmp / "registered", head, ledger=given.ledger,
                                             blind_receipt=given.base / "blind" / "receipt.json")}
        for name, result in again.items():
            first = json.loads(getattr(given, name)[1])
            self.assertEqual((result["status"], result["ledger_repeat"], result["ledger_head"],
                              result["ledger_counts"]),
                             ("completed", True, head, {"looks": 1, "registered": 1}))
            self.assertEqual(result["summary_sha256"], first["summary_sha256"])
            for item in ("summary.json", "summary.md"):
                self.assertEqual(outputs_of(self.tmp / name)[item], outputs_of(given.base / name)[item])
            self.assertTrue(json.loads(outputs_of(self.tmp / name)["receipt.json"])["ledger_repeat"])
        self.assertEqual((given.ledger / LEDGER_FILE).read_bytes(), ledger_bytes)
        # A repeat whose tables differ from the stored hash fails after the read and writes nothing.
        real = resting_study.summary
        with mock.patch.object(resting_study, "summary", lambda *given_: dict(real(*given_), records=0)):
            result = self.run_pass("blind", self.tmp / "differs", head, ledger=given.ledger)
        self.assertEqual(result, {"status": "failed", "failure": "ledger:repeat_differs"})
        self.assertFalse((self.tmp / "differs").exists())
        self.assertEqual((given.ledger / LEDGER_FILE).read_bytes(), ledger_bytes)
        # The operator's head is required: the right ledger with another head refuses before the read.
        for stale in ("none", f"0:{verify(given.ledger / LEDGER_FILE)[0]['entry_sha256']}"):
            self.assertEqual(self.run_pass("blind", self.tmp / "stale", stale, ledger=given.ledger),
                             {"status": "refused", "problems": ["ledger:head_not_expected"]})

    def test_the_registration_the_rule_files_and_the_split_are_refused_unless_they_are_the_named_bytes(self):
        out = self.tmp / "out"
        # Another study is refused before anything is read: none of these paths exists.
        nowhere = dict(registration_path=self.tmp / "none.json", root=self.tmp / "none", ledger=self.tmp / "none")
        for study in ("inplay-v1", "lp-v1", "", None):
            self.assertRefused(self.run_pass("blind", out, study=study, **nowhere), "study:not_supported", out)
        code, printed = cli("--study", "inplay-v1", "--pass", "blind", "--registration-sha256", "0" * 64,
                            "--archive-root", str(self.tmp / "none"), "--out", str(out), "--code-commit", COMMIT,
                            "--ledger", str(self.tmp / "none"), "--ledger-head", "none")
        self.assertEqual((code, json.loads(printed)), (2, {"status": "refused", "problems": ["study:not_supported"]}))
        self.assertRefused(self.run_pass("explore", out), "pass:not_supported", out)
        for digest, problem in (("0" * 64, "registration:sha256_mismatch"), ("abc", "registration_sha256:malformed")):
            for name in resting_study.PASSES:
                self.assertRefused(self.run_pass(name, out, sha=digest), problem, out)
        self.assertRefused(self.run_pass("blind", out, commit="abc"), "code_commit:malformed", out)
        # The registration must lie in a studies folder; its split file and rule files are read beside it.
        loose = self.tmp / "close-v1.json"
        loose.write_bytes(self.given.registration.read_bytes())
        self.assertRefused(self.run_pass("blind", out, registration_path=loose),
                           "registration:not_in_a_studies_folder", out)
        changed = json.loads(RULES_RAW[f"{LAY_RULE}.json"])
        changed["claim"] += " Changed."
        cases = {
            "registration:data.rules[1]:sha256_mismatch": lambda folder: (
                folder / "rules" / f"{LAY_RULE}.json").write_bytes(canonical(changed)),
            "registration:data.split:sha256_mismatch": lambda folder: (
                folder / "partitions-v1.json").write_bytes(SPLIT_RAW + b"\n"),
            "rule_file:missing_path": lambda folder: (folder / "rules" / f"{BACK_RULE}.json").unlink(),
            "split:not_a_regular_file": lambda folder: ((folder / "partitions-v1.json").unlink(),
                                                        (folder / "partitions-v1.json").mkdir()),
        }
        for index, (problem, change) in enumerate(cases.items()):
            with self.subTest(problem=problem):
                folder = self.tmp / f"research{index}"
                registration, digest = write_registration(folder, registration=lowered)
                change(folder)
                self.assertRefused(self.run_pass("blind", out, registration_path=registration, sha=digest), problem,
                                   out)
        # lp-v1's registration, hash and all, is not this study's; nor is one whose id is another study's.
        other = REPO / "research" / "studies" / "lp-v1.json"
        result = self.run_pass("blind", out, registration_path=other, sha=sha(other.read_bytes()))
        self.assertEqual((result["status"], result["problems"][0].split(":")[0]), ("refused", "registration"))
        registration, digest = write_registration(self.tmp / "renamed",
                                                  registration=lambda value: value.update(id="inplay-v1"))
        self.assertRefused(self.run_pass("blind", out, registration_path=registration, sha=digest),
                           "registration:id:not_supported", out)

    def test_the_out_directory_is_refused_in_a_worktree_a_data_tree_the_root_or_the_ledger(self):
        worktree = self.tmp / "clone"
        (worktree / ".git").mkdir(parents=True)
        data_tree = self.tmp / "Betangel-Data"
        data_tree.mkdir()
        existing = self.tmp / "existing"
        existing.mkdir()
        (self.tmp / "again.partial").mkdir()
        (self.ledger / "kept").mkdir()
        for out, problem in ((worktree / "out", "out:inside_git_worktree"), (REPO / "out", "out:inside_repository"),
                             (data_tree / "out", "out:under_a_data_tree"),
                             (self.tmp / "soccer-data" / "out", "out:under_a_data_tree"),
                             (existing, "out:already_exists"), (self.given.root / "out", "roots:not_distinct"),
                             (self.tmp / "missing" / "out", "out:unresolvable"),
                             (self.tmp / "again", "out:partial_present"),
                             (self.ledger / "out", "out:not_apart_from_the_ledger"),
                             (self.ledger / "kept" / "out", "out:not_apart_from_the_ledger")):
            with self.subTest(out=str(out)):
                self.assertRefused(self.run_pass("blind", out), problem)
                self.assertFalse(out.exists() and out != existing)
        self.assertEqual(sorted(item.name for item in self.ledger.iterdir()), ["kept"])
        # A ledger directory inside --out means --out exists, which refuses first.
        holder = self.tmp / "holder"
        (holder / "ledger").mkdir(parents=True)
        self.assertEqual(self.run_pass("blind", holder, ledger=holder / "ledger"),
                         {"status": "refused", "problems": ["out:already_exists"]})
        self.assertEqual(list((holder / "ledger").iterdir()), [])

    def test_the_out_directory_is_refused_under_another_spelling_of_the_ledger_directory(self):
        (self.ledger / "kept").mkdir()
        (self.tmp / "elsewhere").mkdir()
        # A file system that ignores letter case gives the ledger directory other spellings; the path text differs
        # and the directory is the same. Where case is told apart, that folder does not exist.
        same = (self.tmp / "LEDGER").exists()
        for out in (self.tmp / "LEDGER" / "out", self.tmp / "Ledger" / "KEPT" / "out"):
            with self.subTest(out=str(out)):
                self.assertRefused(self.run_pass("blind", out),
                                   "out:not_apart_from_the_ledger" if same else "out:unresolvable")
        self.assertEqual(sorted(item.name for item in self.ledger.iterdir()), ["kept"])
        # The same on any file system, with the identity check answered by hand: the folder above --out is the
        # ledger directory under another name, or cannot be inspected.
        real = Path.samefile

        def same_as_the_ledger(path, other):
            return (other == self.ledger and path.name == "elsewhere") or real(path, other)

        def unreadable(path, other):
            if other == self.ledger:
                raise PermissionError(13, "no")
            return real(path, other)

        for answer in (same_as_the_ledger, unreadable):
            with self.subTest(answer=answer.__name__), mock.patch.object(Path, "samefile", answer):
                self.assertRefused(self.run_pass("blind", self.tmp / "elsewhere" / "out"),
                                   "out:not_apart_from_the_ledger")
        self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])
        # A folder beside the ledger directory is apart from it.
        with mock.patch.object(resting_study, "_tally", side_effect=ReplayProblem("replay:x:stopped")):
            self.assertEqual(self.run_pass("blind", self.tmp / "elsewhere" / "out"),
                             {"status": "failed", "failure": "replay:stopped"})

    def test_the_ledger_is_checked_before_the_read(self):
        out = self.tmp / "out"

        def folder(name, *inside):
            (self.tmp / name).mkdir(parents=True)
            for item in inside:
                (self.tmp / name / item).write_bytes(b"")
            return self.tmp / name

        (self.tmp / "clone" / ".git").mkdir(parents=True)
        cases = [(dict(ledger=self.tmp / "missing"), "ledger:missing_path"),
                 (dict(ledger=None), "ledger:missing_path"),
                 (dict(ledger=folder("clone/ledger")), "ledger:inside_git_worktree"),
                 (dict(ledger=folder("betangel-logs/ledger")), "ledger:under_a_data_tree"),
                 (dict(ledger=self.given.root), "ledger:roots_not_distinct"),
                 (dict(ledger=folder("trial", "trial-ledger-v1.jsonl")), "ledger:trial_ledger_present"),
                 (dict(ledger=folder("locked", "trial-ledger-v1.jsonl.lock")), "ledger:locked"),
                 (dict(head="0"), "ledger:head_malformed"), (dict(head="none:"), "ledger:head_malformed"),
                 (dict(head="0:" + "0" * 64), "ledger:head_not_expected")]
        for given, problem in cases:
            with self.subTest(problem=problem):
                self.assertRefused(self.run_pass("blind", out, **given), problem, out)
        # The budgets: two looks of this study are spent, so a third blind pass refuses and reads nothing.
        head = entry_in(self.ledger, entry_in(self.ledger), registration_sha256="5" * 64)
        before = (self.ledger / LEDGER_FILE).read_bytes()
        with mock.patch.object(resting_study, "_tally", side_effect=AssertionError("a record was read")):
            self.assertEqual(self.run_pass("blind", out, head),
                             {"status": "refused", "problems": ["ledger:looks_spent"]})
            self.assertEqual(self.run_pass("blind", out, "none"),
                             {"status": "refused", "problems": ["ledger:head_not_expected"]})
        self.assertEqual((self.ledger / LEDGER_FILE).read_bytes(), before)
        self.assertFalse(out.exists())

    def test_an_entry_whose_lock_file_stays_still_gives_its_outputs(self):
        """The entry is written and confirmed and only the lock file cannot be removed: the read is not thrown
        away. The outputs are written, the result says so, and every later pass refuses until a person has removed
        the lock file."""
        root, out, lock = one_market_root(self.tmp), self.tmp / "out", self.ledger / "trial-ledger-v1.jsonl.lock"
        remove = Path.unlink

        def unlink(path, *arguments, **named):
            if path == lock:
                raise PermissionError(13, "the lock file stays")
            return remove(path, *arguments, **named)

        with mock.patch.object(Path, "unlink", unlink):
            result = self.run_pass("blind", out, root=root)
        entry = verify(self.ledger / LEDGER_FILE)[0]
        self.assertEqual((result["status"], result["ledger_lock_left"], result["ledger_head"], result["ledger_repeat"]),
                         ("completed", True, f"0:{entry['entry_sha256']}", False))
        self.assertEqual(sorted(item.name for item in self.ledger.iterdir()), sorted([LEDGER_FILE, lock.name]))
        receipt = json.loads(outputs_of(out)["receipt.json"])
        self.assertEqual((receipt["ledger_head"], receipt["ledger_entry"]), (result["ledger_head"],) * 2)
        self.assertEqual(receipt["outputs"]["summary.json"]["sha256"], entry["tables_sha256"])
        self.assertEqual(self.run_pass("blind", self.tmp / "next", result["ledger_head"], root=root),
                         {"status": "refused", "problems": ["ledger:locked"]})
        lock.unlink()
        again = self.run_pass("blind", self.tmp / "next", result["ledger_head"], root=root)
        self.assertEqual((again["status"], again["ledger_repeat"], again["ledger_lock_left"]),
                         ("completed", True, False))
        # Had the outputs failed too, the result would name both the head and the lock.
        with mock.patch.object(StudyLedger, "record", side_effect=UnlockError((0, entry["entry_sha256"]))), \
                mock.patch.object(resting_study, "_write", side_effect=resting_study.StudyRefused("out:unwritable")):
            self.assertEqual(self.run_pass("blind", self.tmp / "both", result["ledger_head"], root=root),
                             {"status": "failed", "failure": "out:unwritable", "ledger_head": result["ledger_head"],
                              "ledger_lock_left": True})

    def test_the_month_gate_the_read_limits_and_a_moved_head_stop_a_pass(self):
        root = one_market_root(self.tmp)
        # The month gate runs on every record before anything of it is used: no member is opened.
        with mock.patch.object(resting_study, "month_gate", return_value="month_sealed_holdout") as gate_, \
                mock.patch.object(resting_study, "_walk", side_effect=AssertionError("a member was opened")), \
                mock.patch.object(resting_study, "record_facts", side_effect=AssertionError("a record was used")):
            result = self.run_pass("blind", self.tmp / "sealed", root=root)
        self.assertEqual(gate_.call_args_list, [mock.call("2015-06")])
        self.assertEqual((result["status"], set(result)), ("failed", {"status", "failure"}))
        self.assertRegex(result["failure"], r"^input:[0-9a-f]{64}:stale_gate$")
        # An archive ingested under other read limits than this run's is refused before the read.
        with mock.patch.object(resting_study, "_tally", side_effect=AssertionError("a record was read")):
            result = self.run_pass("blind", self.tmp / "limits", root=root,
                                   limits=replace(DEFAULT_LIMITS, max_line_bytes=DEFAULT_LIMITS.max_line_bytes - 1))
        self.assertEqual(result["status"], "refused", result)
        self.assertRegex(result["problems"][0], r"^input:[0-9a-f]{64}:limits_not_the_runs$")
        self.assertEqual((list(self.ledger.iterdir()), (self.tmp / "sealed").exists(), (self.tmp / "limits").exists()),
                         ([], False, False))
        # Another pass moved the ledger's head during this pass's read: it fails at the end and writes nothing.
        read = resting_study._tally

        def moved(*given):
            tally = read(*given)
            entry_in(self.ledger)
            return tally

        with mock.patch.object(resting_study, "_tally", moved):
            result = self.run_pass("blind", self.tmp / "moved", root=root)
        self.assertEqual(result, {"status": "failed", "failure": "ledger:head_moved"})
        self.assertFalse((self.tmp / "moved").exists())
        self.assertEqual([entry["registration_sha256"] for entry in verify(self.ledger / LEDGER_FILE)], ["1" * 64])
        self.assertEqual(sorted(item.name for item in self.ledger.iterdir()), [LEDGER_FILE])

    def test_a_repeats_receipt_still_leads_the_registered_pass_to_the_look(self):
        """A blind pass whose outputs were lost is run again after another entry was appended. Its receipt names
        the head then, and beside it the look itself: the registered pass anchors on the look."""
        root, ledger, blind = self.small()
        other = entry_in(ledger, blind["ledger_head"], kind="registered", lineage="other", study="other-v1", m=1,
                         alpha="0.02")
        again = self.run_pass("blind", self.tmp / "again", other, root=root, ledger=ledger)
        receipt = json.loads((self.tmp / "again" / "receipt.json").read_bytes())
        self.assertEqual((again["status"], again["ledger_repeat"], again["ledger_head"]), ("completed", True, other))
        self.assertEqual((receipt["ledger_repeat"], receipt["ledger_head"], receipt["ledger_entry"]),
                         (True, other, blind["ledger_head"]))
        first = json.loads((self.tmp / "small-blind" / "receipt.json").read_bytes())
        self.assertEqual((first["ledger_head"], first["ledger_entry"]), (blind["ledger_head"],) * 2)
        done = self.run_pass("registered", self.tmp / "registered", other, root=root, ledger=ledger,
                             blind_receipt=self.tmp / "again" / "receipt.json")
        self.assertEqual((done["status"], done["ledger_repeat"], done["ledger_counts"]),
                         ("completed", False, {"looks": 1, "registered": 1}))
        self.assertEqual([entry["kind"] for entry in verify(ledger / LEDGER_FILE)],
                         ["look", "registered", "registered"])
        # The registered pass's receipt names its own entry too, the new head.
        receipt = json.loads((self.tmp / "registered" / "receipt.json").read_bytes())
        self.assertEqual((receipt["ledger_entry"], receipt["ledger_head"]), (done["ledger_head"],) * 2)

    def small(self):
        """(a data root with one market, a ledger directory holding its blind pass's look, that pass's result)."""
        root, ledger = one_market_root(self.tmp), self.tmp / "small-ledger"
        ledger.mkdir(mode=0o700)
        result = self.run_pass("blind", self.tmp / "small-blind", root=root, ledger=ledger)
        self.assertEqual((result["status"], result["ledger_counts"]), ("completed", {"looks": 1, "registered": 0}))
        return root, ledger, result

    def test_the_blind_receipt_must_be_this_passes_look_in_this_ledger(self):
        root, ledger, blind = self.small()
        out, head = self.tmp / "registered", blind["ledger_head"]
        receipt = json.loads((self.tmp / "small-blind" / "receipt.json").read_bytes())
        forged = self.tmp / "forged.json"

        def registered(**given):
            arguments = dict(root=root, ledger=ledger, blind_receipt=forged)
            arguments.update(given)
            return self.run_pass("registered", out, arguments.pop("head", head), **arguments)

        self.assertEqual(self.run_pass("registered", out, head, root=root, ledger=ledger),
                         {"status": "refused", "problems": ["blind_receipt:missing"]})
        self.assertEqual(self.run_pass("blind", out, head, root=root, ledger=ledger, blind_receipt=forged),
                         {"status": "refused", "problems": ["blind_receipt:not_for_the_blind_pass"]})
        other = entry_in(ledger, head, kind="registered", lineage="other", study="other-v1", m=1, alpha="0.02")
        no_head = {key: item for key, item in receipt.items() if key != "ledger_entry"}
        for change, problem in (({"registration_sha256": "1" * 64}, "blind_receipt:another_registration"),
                                ({"partitions_sha256": "1" * 64}, "blind_receipt:another_split"),
                                ({"input_sha256": "2" * 64}, "blind_receipt:another_input"),
                                ({"code_commit": "f" * 40}, "blind_receipt:another_code"),
                                ({"source_descriptor_sha256": "3" * 64}, "blind_receipt:another_code"),
                                ({"study": "lp-v1"}, "blind_receipt:not_a_completed_blind_receipt"),
                                ({"pass": "registered"}, "blind_receipt:not_a_completed_blind_receipt"),
                                ({"status": "failed"}, "blind_receipt:not_a_completed_blind_receipt"),
                                ({"ledger_entry": "none"}, "blind_receipt:ledger_entry_malformed"),
                                ({"ledger_entry": "0:zz"}, "blind_receipt:ledger_entry_malformed"),
                                ({"ledger_entry": 0}, "blind_receipt:ledger_entry_malformed"),
                                # An entry the ledger does not hold, and an entry that is not this pass's look.
                                ({"ledger_entry": "0:" + "f" * 64}, "ledger:anchor_mismatch"),
                                ({"ledger_entry": "7:" + "f" * 64}, "ledger:truncated_before_anchor"),
                                ({"ledger_entry": other}, "blind_receipt:not_this_ledgers_look"),
                                # The head after the pass is not what the registered pass reads.
                                ({"ledger_head": other}, None)):
            with self.subTest(change=change):
                forged.write_bytes(canonical_json({**receipt, **change}) + b"\n")
                if problem is None:
                    with mock.patch.object(resting_study, "_tally", side_effect=ReplayProblem("replay:x:stopped")):
                        self.assertEqual(registered(head=other), {"status": "failed", "failure": "replay:stopped"})
                    continue
                self.assertEqual(registered(head=other), {"status": "refused", "problems": [problem]})
        forged.write_bytes(canonical_json(no_head) + b"\n")
        self.assertEqual(registered(head=other),
                         {"status": "refused", "problems": ["blind_receipt:ledger_entry_malformed"]})
        forged.write_bytes(json.dumps(receipt, indent=2).encode())
        self.assertEqual(registered(head=other), {"status": "refused", "problems": ["blind_receipt:not_canonical"]})
        forged.write_bytes(b"{")
        self.assertEqual(registered(head=other), {"status": "refused", "problems": ["blind_receipt:not_json"]})
        # A look of this study on another input is not this pass's look, though the chain holds it.
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        look = entry_in(elsewhere, registration_sha256=self.given.sha, input_sha256="2" * 64,
                        source_descriptor_sha256=receipt["source_descriptor_sha256"])
        forged.write_bytes(canonical_json({**receipt, "ledger_entry": look}) + b"\n")
        self.assertEqual(registered(ledger=elsewhere, head=look),
                         {"status": "refused", "problems": ["blind_receipt:not_this_ledgers_look"]})
        # The true receipt against a ledger that does not hold its look.
        forged.write_bytes(canonical_json(receipt) + b"\n")
        self.assertRefused(registered(ledger=self.ledger, head="none"), "ledger:truncated_before_anchor", out)
        # The true receipt and ledger: the pass runs. Then the lineage is tested: a second test refuses.
        done = registered(head=other)
        self.assertEqual((done["status"], done["ledger_counts"]), ("completed", {"looks": 1, "registered": 1}))
        changed, digest = write_registration(self.tmp / "changed", registration=lambda value: (
            lowered(value), value.update(summary=value["summary"] + " Changed.")))
        again = self.run_pass("blind", self.tmp / "blind2", done["ledger_head"], root=root, ledger=ledger,
                              registration_path=changed, sha=digest)
        self.assertEqual((again["status"], again["ledger_counts"]), ("completed", {"looks": 2, "registered": 1}))
        with mock.patch.object(resting_study, "_tally", side_effect=AssertionError("a record was read")):
            result = self.run_pass("registered", self.tmp / "registered2", again["ledger_head"], root=root,
                                   ledger=ledger, registration_path=changed, sha=digest,
                                   blind_receipt=self.tmp / "blind2" / "receipt.json")
        self.assertEqual(result, {"status": "refused", "problems": ["ledger:lineage_already_tested"]})
        self.assertEqual([entry["kind"] for entry in verify(ledger / LEDGER_FILE)],
                         ["look", "registered", "registered", "look"])

    def test_the_real_gates_give_counts_only(self):
        """The registration as it is, read from the repository: about 12 scored orders a cell and label, far below
        100 values. Neither pass gives a statistic of an outcome, no cell is eligible, and nothing is tested."""
        blind = self.run_pass("blind", self.tmp / "blind", registration_path=None, sha=REGISTRATION_SHA256)
        self.assertEqual(blind["status"], "completed", blind)
        registered = self.run_pass("registered", self.tmp / "registered", blind["ledger_head"], registration_path=None,
                                   sha=REGISTRATION_SHA256, blind_receipt=self.tmp / "blind" / "receipt.json")
        self.assertEqual(registered["status"], "completed", registered)
        statistics = REGISTERED_ONLY | {"sd", "ranges", "effect", "queue_quantiles", "seconds_quantiles"}
        columns = {"SD", "mean", "CI low", "CI high", "gap", "kill bound", "lower bound", "p one-sided", "Holm p",
                   "detectable effect", "0.5"}
        for name in ("blind", "registered"):
            files = outputs_of(self.tmp / name)
            value = json.loads(files["summary.json"])
            found = [(key, item) for key, item in walk_items(value) if key in statistics and item is not None
                     and not isinstance(item, (dict, list))]
            with self.subTest(out=name):
                self.assertEqual(found, [])
                self.assertEqual({trail[-1] for trail, _ in walk_floats(value)},
                                 {"fill_rate"} | ({"sqrt_2_ln_c"} if name == "registered" else set()))
                shown = {column.split(" (")[0]: cells for column, cells in
                         markdown_columns(files["summary.md"].decode()).items()}
                self.assertEqual({column: cells for column, cells in shown.items()
                                  if column in columns and set(cells) != {"-"}}, {})
        value = json.loads(outputs_of(self.tmp / "registered")["summary.json"])
        self.assertEqual(value["selection"], {"eligible": [], "primaries": {BACK_RULE: None, LAY_RULE: None}})
        self.assertEqual(value["primaries"], {BACK_RULE: None, LAY_RULE: None})
        self.assertIn("close-v1-lay: no primary cell", outputs_of(self.tmp / "registered")["summary.md"].decode())
        # The family still counts both rule files, and the entry spends the registered alpha.
        entry = verify(self.ledger / LEDGER_FILE)[1]
        self.assertEqual((entry["kind"], entry["m"], entry["alpha"]), ("registered", 2, "0.02"))

    def test_a_market_twice_in_the_registered_tier_refuses(self):
        root = self.tmp / "twice"
        root.mkdir()
        for index, event in enumerate((fx.EVENT, "27000002")):
            member = fx.path(M, when=OFF, plan="PRO", event=event)
            tar = fx.make_tar(self.tmp / f"twice{index}.tar", [(member, bz2.compress(fx.lines(lp_market())))])
            self.assertEqual(ingest_archive(tar, root)["status"], "ingested")
        self.assertEqual(index_archives(root)["status"], "indexed")
        result = self.run_pass("blind", self.tmp / "twice-out", root=root)
        self.assertEqual(result["status"], "failed", result)
        self.assertRegex(result["failure"], r"^input:[0-9a-f]{64}:duplicate_market$")
        self.assertFalse((self.tmp / "twice-out").exists())
        self.assertFalse((self.ledger / LEDGER_FILE).exists())

    def test_only_the_record_reads_report_a_malformed_record_and_an_internal_error_prints_a_class_name(self):
        root, out = one_market_root(self.tmp), self.tmp / "out"
        arguments = ["--study", STUDY, "--pass", "blind", "--registration-sha256", self.given.sha, "--registration",
                     str(self.given.registration), "--archive-root", str(root), "--out", str(out), "--code-commit",
                     COMMIT, "--ledger", str(self.ledger), "--ledger-head", "none"]
        with mock.patch.object(resting_study, "record_facts", side_effect=KeyError(M)):
            code, printed = cli(*arguments)
        self.assertEqual(code, 2)
        self.assertRegex(json.loads(printed)["failure"], r"^input:[0-9a-f]{64}:malformed_record$")
        # The same error anywhere else is a fault of the code: one class name, no message, no traceback.
        for name in ("decide_record", "summary", "markdown"):
            with self.subTest(patched=name):
                with mock.patch.object(resting_study, name, side_effect=KeyError(M)):
                    code, printed = cli(*arguments)
                self.assertEqual((code, printed), (2, '{"failure": "internal:KeyError", "status": "failed"}\n'))
                self.assertFalse(out.exists())
                self.assertEqual(list(self.ledger.iterdir()), [])

    def test_an_entry_stands_when_an_output_cannot_be_written_and_the_next_run_is_a_repeat(self):
        root, out = one_market_root(self.tmp), self.tmp / "out"
        with mock.patch.object(resting_study, "_write", side_effect=resting_study.StudyRefused("out:unwritable")):
            failed = self.run_pass("blind", out, root=root)
        entry = verify(self.ledger / LEDGER_FILE)[0]
        self.assertEqual(failed, {"status": "failed", "failure": "out:unwritable",
                                  "ledger_head": f"0:{entry['entry_sha256']}", "ledger_lock_left": False})
        self.assertFalse(out.exists())
        again = self.run_pass("blind", out, failed["ledger_head"], root=root)
        self.assertEqual((again["status"], again["ledger_repeat"], again["ledger_head"], again["summary_sha256"]),
                         ("completed", True, failed["ledger_head"], entry["tables_sha256"]))
        self.assertEqual(len(verify(self.ledger / LEDGER_FILE)), 1)
        # A fault of the code after the append: the class name only, beside the head that stands.
        changed, digest = write_registration(self.tmp / "changed", registration=lambda value: (
            lowered(value), value.update(summary=value["summary"] + " Changed.")))
        with mock.patch.object(resting_study, "_write", side_effect=KeyError(M)):
            code, printed = cli("--study", STUDY, "--pass", "blind", "--registration-sha256", digest,
                                "--registration", str(changed), "--archive-root", str(root), "--out",
                                str(self.tmp / "fault"), "--code-commit", COMMIT, "--ledger", str(self.ledger),
                                "--ledger-head", failed["ledger_head"])
        second = verify(self.ledger / LEDGER_FILE)[1]
        self.assertEqual((code, json.loads(printed)), (2, {
            "status": "failed", "failure": "internal:KeyError", "ledger_head": f"1:{second['entry_sha256']}",
            "ledger_lock_left": False}))
        self.assertNotIn(M, printed)
        self.assertFalse((self.tmp / "fault").exists())
        again = self.run_pass("blind", self.tmp / "fault", f"1:{second['entry_sha256']}", root=root,
                              registration_path=changed, sha=digest)
        self.assertEqual((again["status"], again["ledger_repeat"], len(verify(self.ledger / LEDGER_FILE))),
                         ("completed", True, 2))
        # A ledger directory that cannot be written is found only at the append, after the read: nothing is written.
        closed = self.tmp / "closed"
        closed.mkdir(mode=0o500)
        self.addCleanup(closed.chmod, 0o700)
        self.assertEqual(self.run_pass("blind", self.tmp / "unwritten", root=root, ledger=closed),
                         {"status": "failed", "failure": "ledger:lock_failed"})
        self.assertEqual((list(closed.iterdir()), (self.tmp / "unwritten").exists()), ([], False))
        # A pass that fails during the read has appended nothing.
        raw_members(root)[0].chmod(0o644)
        raw_members(root)[0].write_bytes(raw_members(root)[0].read_bytes() + b"\0")
        fresh = self.tmp / "fresh"
        fresh.mkdir(mode=0o700)
        self.assertEqual(self.run_pass("blind", self.tmp / "torn", root=root, ledger=fresh),
                         {"status": "failed", "failure": "replay:member_hash_mismatch"})
        self.assertEqual(list(fresh.iterdir()), [])


class RecordsAndTallies(RestingCase):
    def test_a_record_with_a_code_at_every_cell_is_never_opened(self):
        edge = datetime(2016, 4, 30, 23, 30, tzinfo=timezone.utc)       # 1 May in London: no label
        cases = {"partition_not_registered": (path(M, edge), lp_market(off=edge), "2016-04", "no_registered_label"),
                 "too_few_active_runners": (path(M), lp_market(runners=SIDS[:4]), "2015-06", "PRO-V"),
                 "plan_not_registered": (fx.path(M, when=OFF, plan="BASIC"), lp_market(), None, None)}
        for code, (member, messages, month, column) in cases.items():
            with self.subTest(code=code):
                _, _, records = self.build([(member, messages)])
                tally = Tally(PLAN)
                facts = record_facts(PLAN, records[M])
                if code == "too_few_active_runners":                         # this one needs the book: it is opened
                    with self.assertRaises(TypeError):
                        decide_record(PLAN, records[M], facts, None, None, tally)
                    continue
                decide_record(PLAN, records[M], facts, None, None, tally)     # no root and no manifest to read from
                self.assertEqual((tally.records, tally.replayed, tally.bytes_walked), (1, 0, 0))
                self.assertEqual({cell: {name for name, count in codes.items() if count}
                                  for cell, codes in tally.codes.items()}, dict.fromkeys(PLAN.cells, {code}))
                self.assertEqual(sum(count for row in tally.months.values() for count in row["records"].values()),
                                 0 if month is None else 1)
                if month is not None:
                    self.assertEqual(tally.months[month]["records"][column], 1)

    def test_rows_are_kept_by_cell_and_partition_label(self):
        tally = Tally(PLAN)
        for off in (OFF, OFF + timedelta(days=1), WINTER):                 # PRO-V, PRO-D, PRO-V
            root, manifest, records = self.build([(path(M, off), lp_market(off=off))])
            decide_record(PLAN, records[M], record_facts(PLAN, records[M]), root, manifest, tally)
        self.assertEqual((tally.records, tally.replayed), (3, 3))
        self.assertGreater(tally.bytes_walked, 0)
        for cell in PLAN.cells:
            self.assertEqual({label: [(row.label, row.half) for row in rows]
                              for label, rows in tally.rows[cell].items()},
                             {"PRO-D": [("PRO-D", "H1")], "PRO-V": [("PRO-V", "H1"), ("PRO-V", "H2")]})
        self.assertEqual({label: len(values) for label, values in tally.books.items()}, {"PRO-D": 1, "PRO-V": 2})
        self.assertAlmostEqual(tally.books["PRO-D"][0], sum(1 / bsp for bsp in BSPS.values()))

    def test_fills_in_the_turn_batch_and_orders_with_no_status_are_counted(self):
        through = {"atb": [[2.48, 0]], "trd": [[2.48, 1.0]]}
        turn = mcm(before(200), [{"id": M, "rc": [{"id": FAV, **through}],
                                  "marketDefinition": definition(OFF, [runner(s) for s in SIDS], in_play=True)}])
        blank = [status_at(250, status=fx.DROP), ladder_move(200, sid=FAV, **through)]
        tally = Tally(PLAN)
        for index, extra in enumerate(([turn], blank, [suspended(200, "SUSPENDED", through)], [])):
            market_id = f"1.18000000{index + 2}"
            messages = [json.loads(json.dumps(message).replace(M, market_id))
                        for message in lp_market(extra=extra)]
            root, manifest, records = self.build([(path(market_id), messages)])
            decide_record(PLAN, records[market_id], record_facts(PLAN, records[market_id]), root, manifest, tally)
        value = summary(PLAN, tally, "blind", "0" * 64)
        tables = value["cells"]["close-v1-lay@-300"]
        for model in MODELS:
            counts = tables["fills"]["PRO-V"][model]
            self.assertEqual((counts["scored"], counts["fills"], counts["left_the_market_not_open"],
                              counts["turned_the_market_in_play"], counts["ended_by"]),
                             (4, 2, 1, 1, {"pull": 2, "lapse": 0}))
        self.assertEqual(tables["fills"]["PRO-V"][OPTIMISTIC]["by_kind"], {THROUGH: 2, CROSS: 0, VOLUME: 0})
        self.assertEqual(tables["scored_orders_with_a_batch_begun_with_no_status"], 1)
        self.assertEqual(tables["trd_changes"], dict(zip(TRD_COUNTS, (2, 0, 1))))
        # The BACK placed at the same time was ended by the lapse in the first market, and marked in the second.
        back = value["cells"]["close-v1-back@-300"]
        self.assertEqual((back["fills"]["PRO-V"][CENTRAL]["ended_by"],
                          back["scored_orders_with_a_batch_begun_with_no_status"]), ({"pull": 3, "lapse": 1}, 1))
        shown = markdown_columns(resting_study.markdown(PLAN, value))
        self.assertEqual((sum(map(int, shown["turned the market in play"])),
                          sum(map(int, shown["left the market not OPEN"])),
                          sum(map(int, shown["scored orders with a batch begun with no status"]))), (6, 6, 4))


class BlindReleases(unittest.TestCase):
    def test_the_blind_pass_gives_no_location_of_an_outcome(self):
        given = shared()
        files = outputs_of(given.base / "blind")
        value = json.loads(files["summary.json"])
        keys = set(walk_keys(value))
        self.assertEqual(set(value), {"kind", "schema_version", "study", "pass", "registration_sha256",
                                      "partitions_sha256", "input_sha256", "partitions", "records",
                                      "replayed_markets", "coverage", "cells", "bsp_precision", "withheld"})
        self.assertEqual(value["withheld"], resting_study.WITHHELD)
        self.assertEqual(keys & REGISTERED_ONLY, set())
        self.assertEqual([key for key in keys if "mean" in key.lower() or "median" in key.lower()], [])
        self.assertEqual({key for key in keys if "quantile" in key}, {"queue_quantiles", "seconds_quantiles"})
        self.assertEqual({key for key in keys if "net" in key}, set())             # no SD of a net: gross g only
        # Every float is a fill rate, an SD or a range of gross g, the detectable effect, or a quantile of a
        # queue size or of seconds: none is a location of an outcome.
        allowed = {("fill_rate",), ("spread_of_g", "all_scored", "sd"), ("spread_of_g", "filled", "sd"),
                   ("spread_of_g", "filled", "ranges"), ("spread_of_g", "filled", "detectable", "effect"),
                   ("queue_at_t", "queue_quantiles"), ("seconds_to_fill", "seconds_quantiles")}
        names = set(MODELS) | set(PLAN.labels) | set(value["cells"]) | {"cells", "fills"}
        places = {tuple(key for key in trail if key not in names and not key[0].isdigit())
                  for trail, _ in walk_floats(value)}
        self.assertEqual(places, allowed)
        ranges = [item for key, item in walk_items(value) if key == "ranges"]
        self.assertEqual({frozenset(item) for item in ranges}, {frozenset(("0.95-0.05", "0.9-0.1", "0.75-0.25"))})
        for tables in value["cells"].values():
            self.assertEqual(set(tables["spread_of_g"]), {"bsp_end", "all_scored", "filled"})
            self.assertEqual(set(tables["spread_of_g"]["all_scored"]), {"values", "race_dates", "sd"})
            for item in tables["spread_of_g"]["filled"].values():
                self.assertEqual(set(item), {"values", "race_dates", "sd", "ranges", FILLED_IS_ALL_PLACED,
                                             "detectable"})
                self.assertEqual(set(item["detectable"]), {"values", "race_dates", FILLED_IS_ALL_PLACED, "effect"})
        text = files["summary.md"].decode().lower()
        for word in ("mean", "median", "p one-sided", "holm", "ci low", "net_held", "net_closed", "## t",
                     "primar", "exploratory", "not tested", "sqrt", PLAN.label_note.lower(),
                     PLAN.exposure_note.lower(), *VERDICTS, BLOCKED):
            self.assertNotIn(word, text)
        self.assertIn("withheld in this pass", text)
        self.assertIn("under independence; clustering makes it larger", text)
        # The registered pass holds the same tables and its own.
        registered = json.loads(outputs_of(given.base / "registered")["summary.json"])
        self.assertEqual(set(registered) - set(value), {"figures", "half_years", "selection", "primaries",
                                                        "sensitivity"})
        self.assertEqual({key: item for key, item in registered.items() if key in value and key != "pass"},
                         {key: item for key, item in value.items() if key not in ("pass", "withheld")})

    def test_a_change_of_sign_of_every_g_moves_nothing_the_blind_pass_gives(self):
        """Every LAY's BSP is replaced by the one that turns its g into minus g. The counts, the SDs, the ranges
        and the detectable effects stay as they are; only the count of whole-hundredth BSPs can tell."""
        def mirrored(row):
            return row._replace(bsp=row.price / (2 - row.price / row.bsp))

        rows = {}
        for index, cell in enumerate(LAY_CELLS):
            bsps = [2.3 + (step * (index + 1)) % 61 / 100 for step in range(61)]
            rows[cell] = block(400, D_DAYS, bsps, label=D) + block(401, V_DAYS, bsps[::-1]) + \
                block(150, D_DAYS, bsps, label=D, filled=NONE) + block(151, V_DAYS, bsps, filled=NONE)
        first = summary(PLAN, tally_of(rows), "blind", "0" * 64)
        second = summary(PLAN, tally_of({cell: [mirrored(row) for row in found] for cell, found in rows.items()}),
                         "blind", "0" * 64)
        spread_ = first["cells"]["close-v1-lay@-300"]["spread_of_g"]
        self.assertEqual((spread_["filled"][CENTRAL]["values"], spread_["all_scored"]["values"]), (801, 1102))
        self.assertGreater(spread_["filled"][CENTRAL]["sd"], 0.01)
        self.assertGreater(spread_["filled"][CENTRAL]["detectable"]["effect"], 0)
        self.assertNotEqual(first["bsp_precision"], second["bsp_precision"])
        self.assertEqual({**first, "bsp_precision": None}, {**second, "bsp_precision": None})
        self.assertEqual(resting_study.markdown(PLAN, {**first, "bsp_precision": second["bsp_precision"]}),
                         resting_study.markdown(PLAN, second))
        # The values did change sign.
        g = [series(PLAN, scored(found[cell]), G, AS_WRITTEN) for found in (rows, {cell: [mirrored(row) for row in
                                                                                   rows[cell]] for cell in rows})
             for cell in LAY_CELLS[:1]]
        self.assertEqual([value for _, value in g[0]], [-value for _, value in g[1]])


class PartAndRestAcrossASplit(unittest.TestCase):
    """A half-year is a part of its label and a label a part of the two pooled: a figure of one is given only when
    the other's is, so that a small part's figure cannot be worked out from two that are given."""
    CELL = LAY_CELLS[1]

    def test_a_figure_is_given_for_every_part_or_for_none(self):
        figure = dict(bets=150, race_dates=16, mean=0.01, se=0.001, df=15, t_critical=2.1, ci_low=0.0, ci_high=0.02,
                      reason=None, sd=0.05)
        small = dict(figure, bets=50, mean=None, se=None, df=None, t_critical=None, ci_low=None, ci_high=None,
                     sd=None, reason=COUNTS_ONLY)
        withheld = dict(small, bets=150, reason="rest_not_given")
        together = resting_study._together
        self.assertEqual(together({"H1": figure, "H2": figure}), {"H1": figure, "H2": figure})
        self.assertEqual(together({"H1": figure, "H2": small}), {"H1": withheld, "H2": small})
        # Each figure of a tree is judged on its own; counts and flags are kept as they are.
        trees = {"H1": {"placed": figure, "models": {"central": {"fills": 150, "filled": {"g": figure}}}},
                 "H2": {"placed": figure, "models": {"central": {"fills": 50, "filled": {"g": small}}}}}
        self.assertEqual(together(trees), {
            "H1": {"placed": figure, "models": {"central": {"fills": 150, "filled": {"g": withheld}}}},
            "H2": {"placed": figure, "models": {"central": {"fills": 50, "filled": {"g": small}}}}})
        gap_ = dict(estimate=0.01, se=0.001, df=15, bets=[150, 150], race_dates=16, reason=None, ci_low=0.0,
                    ci_high=0.02)
        none = dict(gap_, estimate=None, se=None, df=None, ci_low=None, ci_high=None, reason=FILLED_IS_ALL_PLACED)
        self.assertEqual(together({"H1": gap_, "H2": none})["H1"], dict(none, reason="rest_not_given"))
        self.assertEqual(together({"H1": {"fills": 150, "mean": 0.01}, "H2": {"fills": 50, "mean": None}}),
                         {"H1": {"fills": 150, "mean": None}, "H2": {"fills": 50, "mean": None}})

    def registered(self, second, unfilled=150):
        """A registered summary whose LAY primary has, on the test label: 240 fills and 200 unfilled orders in H1,
        and in H2 `second` fills and `unfilled` unfilled orders."""
        rows = block(240, V_DAYS[:8], [2.7, 2.8]) + block(200, V_DAYS[:8], [2.5], filled=NONE) + \
            block(second, V_DAYS[8:], [2.7, 2.8]) + block(unfilled, V_DAYS[8:], [2.5], filled=NONE) + \
            block(240, D_DAYS, [2.7, 2.8], label=D) + block(100, D_DAYS, [2.5], label=D, filled=NONE)
        value = summary(PLAN, tally_of({self.CELL: rows}), "registered", "0" * 64)
        self.assertEqual(value["selection"]["primaries"][LAY_RULE], "close-v1-lay@-300")
        return value

    def halves(self, second, unfilled=150):
        """(T7 for the primary on the test label, its T9 block) of registered()."""
        value = self.registered(second, unfilled)
        return value["half_years"]["by_cell"]["close-v1-lay@-300"]["PRO-V"], value["primaries"][LAY_RULE]

    def test_half_years_whose_filled_figures_are_over_different_kinds_of_group_give_none(self):
        """H1: 240 fills and 200 unfilled, so its filled mean is over the 240. H2: 150 fills and 50 unfilled, so
        its filled mean is over all 200 (the flag). The label's own filled mean is over its 390 fills. The three
        together would give the mean of H2's 50 unfilled orders; so neither half-year's filled figure is given."""
        value = self.registered(150, unfilled=50)
        both, verdict = (value["half_years"]["by_cell"]["close-v1-lay@-300"]["PRO-V"], value["primaries"][LAY_RULE])
        whole = value["figures"]["by_cell"]["close-v1-lay@-300"]["PRO-V"]
        for end in BSP_ENDS:
            for model in MODELS:
                self.assertEqual((whole[end]["models"][model][FILLED_IS_ALL_PLACED],
                                  whole[end]["models"][model]["filled"][NET_HELD]["bets"]), (False, 390))
                self.assertIsNotNone(whole[end]["models"][model]["filled"][NET_HELD]["mean"])
                for half, fills_, flag, bets in (("H1", 240, False, 240), ("H2", 150, True, 200)):
                    item = both[half][end]["models"][model]
                    self.assertEqual((item["fills"], item[FILLED_IS_ALL_PLACED]), (fills_, flag))
                    for figure in (*item["filled"].values(), item["value_per_placed"]):
                        self.assertEqual((figure["mean"], figure["sd"], figure["ci_low"], figure["reason"]),
                                         (None, None, None, "rest_not_given"))
                    self.assertEqual(item["filled"][G]["bets"], bets)
                    self.assertIsNone(item["gap"]["estimate"])
            for half in ("H1", "H2"):                                           # what has no fill model is given
                self.assertIsNotNone(both[half][end]["placed"]["mean"])
        self.assertEqual(verdict["half_years"], {"H1": {"fills": 240, FILLED_IS_ALL_PLACED: False, "mean": None},
                                                 "H2": {"fills": 150, FILLED_IS_ALL_PLACED: True, "mean": None}})
        # The label was decided on the full values before anything was withheld.
        self.assertTrue(verdict["checks"]["mean_above_0_in_each_half_year"])
        # With 100 unfilled in H2 both half-years are over their fills, and both are given.
        both, verdict = self.halves(150, unfilled=100)
        for half, fills_ in (("H1", 240), ("H2", 150)):
            model = both[half][AS_WRITTEN]["models"][PESSIMISTIC]
            self.assertEqual((model[FILLED_IS_ALL_PLACED], model["filled"][NET_HELD]["bets"]), (False, fills_))
            self.assertIsNotNone(model["filled"][NET_HELD]["mean"])
            self.assertIsNotNone(verdict["half_years"][half]["mean"])
        together = resting_study._together
        figure = dict(bets=150, race_dates=16, mean=0.01, se=0.001, df=15, t_critical=2.1, ci_low=0.0, ci_high=0.02,
                      reason=None, sd=0.05)
        trees = {half: {"fills": 150, FILLED_IS_ALL_PLACED: flag, "filled": {"g": figure}, "gap": dict(
            estimate=0.01, se=0.001, df=15, bets=[150, 150], race_dates=16, reason=None, ci_low=0.0, ci_high=0.02)}
            for half, flag in (("H1", False), ("H2", True))}
        self.assertEqual({(item["filled"]["g"]["mean"], item["filled"]["g"]["reason"], item["gap"]["estimate"],
                           item["gap"]["reason"], item["fills"]) for item in together(trees).values()},
                         {(None, "rest_not_given", None, "rest_not_given", 150)})
        self.assertEqual(together({half: dict(tree, **{FILLED_IS_ALL_PLACED: True}) for half, tree in trees.items()}),
                         {half: dict(tree, **{FILLED_IS_ALL_PLACED: True}) for half, tree in trees.items()})

    def test_a_half_year_is_given_only_when_the_other_is(self):
        both, verdict = self.halves(100)
        for half, fills_ in (("H1", 240), ("H2", 100)):
            model = both[half][AS_WRITTEN]["models"][PESSIMISTIC]
            self.assertEqual((model["fills"], model["filled"][NET_HELD]["reason"]), (fills_, "too_few_clusters"))
            self.assertIsNotNone(model["filled"][NET_HELD]["mean"])
            self.assertIsNotNone(verdict["half_years"][half]["mean"])
        # 99 fills in the second half-year: its figures are counts only, and so are the first half-year's.
        both, verdict = self.halves(99)
        for half, fills_, reason in (("H1", 240, "rest_not_given"), ("H2", 99, COUNTS_ONLY)):
            for end in BSP_ENDS:
                model = both[half][end]["models"][PESSIMISTIC]
                self.assertEqual((model["fills"], model["filled"][NET_HELD]["bets"], model["filled"][NET_HELD]["mean"],
                                  model["filled"][NET_HELD]["sd"], model["filled"][NET_HELD]["reason"]),
                                 (fills_, fills_, None, None, reason))
            self.assertEqual(verdict["half_years"][half], {"fills": fills_, FILLED_IS_ALL_PLACED: False, "mean": None})
        self.assertIsNotNone(both["H1"][AS_WRITTEN]["placed"]["mean"])            # 249 orders in H2: still given
        self.assertFalse(verdict["checks"]["mean_above_0_in_each_half_year"])

    def test_a_kills_pooled_figures_need_100_of_their_orders_on_each_label(self):
        def killed(explore, test):
            rows = block(explore, D_DAYS[:8], [2.3, 2.4], label=D, filled=(OPTIMISTIC,)) + \
                block(test, V_DAYS[:8], [2.3, 2.4], filled=(OPTIMISTIC,)) + block(100, V_DAYS, [3.5], filled=NONE)
            found = verdicts(PLAN, {BACK_RULE: None, LAY_RULE: self.CELL}, {self.CELL: rows}, True)[LAY_RULE]
            return found, resting_study._kill(PLAN, found["kill"], rows)

        found, printed = killed(120, 120)
        self.assertEqual(found["label"], "kill_optimistic")
        self.assertEqual(printed, dict(found["kill"], orders_by_label={"PRO-D": 120, "PRO-V": 120}, figures_given=True))
        # 141 and 99: the kill is decided as before, and its figures are withheld; the explore label's own mean,
        # which T5 gives, would otherwise leave the test label's 99 orders' mean to be worked out.
        found, printed = killed(141, 99)
        self.assertEqual((found["label"], found["kill"]["fills"]), ("kill_optimistic", 240))
        self.assertLess(found["kill"]["bound"], 0)
        self.assertEqual((printed["figures_given"], printed["orders_by_label"], printed["fills"], printed["bets"],
                          printed["race_dates"], printed["bsp_end"]),
                         (False, {"PRO-D": 141, "PRO-V": 99}, 240, 240, 16, PLUS_STEP))
        self.assertEqual({key for key, item in printed.items() if item is None},
                         {"mean", "se", "df", "t_critical", "ci_low", "ci_high", "bound", "reason"})
        self.assertEqual(printed["both"], {end: {NET_CLOSED: None, NET_HELD: None} for end in BSP_ENDS})

    def test_a_kills_pooled_figures_need_each_labels_own_figure_over_the_same_orders(self):
        def killed(explore, test, front_only=0):
            """A LAY primary with (fills, unfilled) per label under the optimistic model; front_only of the test
            label's fills are fills under that model alone, the others fill under every model."""
            rows = block(explore[0], D_DAYS, [2.3, 2.4], label=D, filled=(OPTIMISTIC,)) + \
                block(explore[1], D_DAYS, [3.5], label=D, filled=NONE) + \
                block(test[0] - front_only, V_DAYS, [2.3, 2.4]) + \
                block(front_only, V_DAYS, [2.3, 2.4], filled=(OPTIMISTIC,)) + block(test[1], V_DAYS, [3.5], filled=NONE)
            found = verdicts(PLAN, {BACK_RULE: None, LAY_RULE: self.CELL}, {self.CELL: rows}, True)[LAY_RULE]
            return found, resting_study._kill(PLAN, found["kill"], rows), rows

        # 450 fills and 150 unfilled on the explore label, 299 and 1 on the test label. The pooled figures are
        # over the 749 fills. T5 gives the explore label's mean over its 450 fills and the test label's over all
        # its 300 orders (the flag): the three would give the one unfilled order's net. 60 unfilled is the same.
        for test in ((299, 1), (240, 60)):
            found, printed, rows = killed((450, 150), test)
            with self.subTest(test=test):
                self.assertEqual((found["label"], found["kill"]["fills"], found["kill"][FILLED_IS_ALL_PLACED]),
                                 ("kill_optimistic", 450 + test[0], False))
                self.assertIsNotNone(found["kill"]["mean"])                     # decided on the full values
                self.assertEqual((printed["figures_given"], printed["orders_by_label"], printed["mean"],
                                  printed["bound"]), (False, {"PRO-D": 450, "PRO-V": test[0]}, None, None))
                self.assertEqual(printed["both"], {end: {NET_CLOSED: None, NET_HELD: None} for end in BSP_ENDS})
                front = figures(PLAN, scored(rows, V), AS_WRITTEN)["models"][OPTIMISTIC]
                self.assertEqual((front[FILLED_IS_ALL_PLACED], front["filled"][NET_CLOSED]["bets"]), (True, sum(test)))
                self.assertIsNotNone(front["filled"][NET_CLOSED]["mean"])
        # 100 unfilled on each label: every figure is over fills, and the pooled ones are given.
        found, printed, _ = killed((450, 150), (200, 100))
        self.assertEqual((printed["figures_given"], printed["mean"]), (True, found["kill"]["mean"]))
        # Fewer than 100 unfilled in all: every figure is over every order, on each label and pooled.
        found, printed, _ = killed((450, 30), (299, 1))
        self.assertEqual((found["kill"][FILLED_IS_ALL_PLACED], printed["figures_given"],
                          printed["orders_by_label"]), (True, True, {"PRO-D": 480, "PRO-V": 300}))
        # The test label's own optimistic figure is withheld beside its pessimistic one (50 fills between them):
        # the pooled figures less the explore label's would give it back, so they are withheld too.
        found, printed, rows = killed((450, 150), (300, 100), front_only=50)
        self.assertNotIn((FILLED, OPTIMISTIC), given_figures(PLAN, scored(rows, V)))
        self.assertEqual((found["label"], printed["figures_given"], printed["orders_by_label"]),
                         ("kill_optimistic", False, {"PRO-D": 450, "PRO-V": 300}))
        found, printed, rows = killed((450, 150), (300, 100), front_only=100)
        self.assertIn((FILLED, OPTIMISTIC), given_figures(PLAN, scored(rows, V)))
        self.assertTrue(printed["figures_given"])


class NoIds(unittest.TestCase):
    def test_no_output_holds_a_market_or_selection_id(self):
        given = shared()
        ids = given.ids + [market_id.split(".")[1] for market_id in given.ids] + [str(sid) for sid in SIDS] + \
            [fx.EVENT]
        # Nor a market's date or time: each race date, and each off as ms and as ISO text, to the minute.
        offs = study_offs() + [EDGE, OFF]
        ids += sorted({off.date().isoformat() for off in offs} | {"2016-05-01"}) + \
            [str(ms(off))[:-3] for off in offs] + [iso(off)[:16] for off in offs]
        self.assertLessEqual({"2015-06-01", "2016-04-30", "2015-06-01T14:00", "1433167200"}, set(ids))
        self.assertEqual((len(given.ids), given.blind[0], given.registered[0]), (29, 0, 0))
        for name in ("blind", "registered"):
            for item, data in outputs_of(given.base / name).items():
                with self.subTest(out=name, file=item):
                    self.assertEqual([token for token in ids if token in data.decode()], [])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        tmp = Path(folder.name).resolve()
        root, ledger = one_market_root(tmp), tmp / "ledger"
        ledger.mkdir(mode=0o700)
        raw_members(root)[0].chmod(0o644)
        raw_members(root)[0].write_bytes(raw_members(root)[0].read_bytes() + b"\0")
        failed = cli("--study", STUDY, "--pass", "blind", "--registration-sha256", given.sha, "--registration",
                     str(given.registration), "--archive-root", str(root), "--out", str(tmp / "failed"),
                     "--code-commit", COMMIT, "--ledger", str(ledger), "--ledger-head", "none")
        self.assertEqual((failed[0], json.loads(failed[1])),
                         (2, {"status": "failed", "failure": "replay:member_hash_mismatch"}))
        self.assertEqual((list(ledger.iterdir()), (tmp / "failed").exists()), ([], False))
        printed = {"blind": given.blind[1], "registered": given.registered[1], "failed replay": failed[1],
                   "ledger": (given.ledger / LEDGER_FILE).read_text()}
        for name, text in printed.items():
            with self.subTest(printed=name):
                self.assertEqual([token for token in ids + [M, M.split(".")[1]] if token in text], [])


class RestingBoundaries(unittest.TestCase):
    MODULES = ("fills", "resting", "resting_study")

    def test_the_study_stays_inside_the_backtest_closure(self):
        from test_betfair_backtest_boundaries import (BACKTEST, BETFAIR, FORBIDDEN_BETFAIR, REAL, TOP,
                                                      backtest_violations)
        closure = REAL.closure([f"{BACKTEST}.{name}" for name in self.MODULES])
        self.assertEqual({module for module in closure if not module.startswith(BACKTEST)},
                         {TOP, BETFAIR, f"{TOP}.temporal"})
        self.assertEqual(backtest_violations(REAL, betfair=BETFAIR, forbidden=FORBIDDEN_BETFAIR), [])
        # The certified CLI does not know the runner: it is a module of its own.
        self.assertNotIn(f"{BACKTEST}.resting_study", REAL.closure([f"{BACKTEST}.__main__"]))

    def test_importing_the_study_loads_no_other_betfair_module(self):
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 + "".join(f"import betangel_research.betfair.backtest.{name}; " for name in self.MODULES)
                 + "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('betangel_research'))))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(REPO)], capture_output=True, text=True,
                                check=True)
        loaded = set(result.stdout.split())
        self.assertEqual({m for m in loaded if not m.startswith("betangel_research.betfair.backtest")},
                         {"betangel_research", "betangel_research.betfair", "betangel_research.temporal"})
        self.assertLessEqual({f"betangel_research.betfair.backtest.{name}" for name in self.MODULES}, loaded)

    def test_the_runner_takes_one_study_and_every_argument_the_ledger_needs(self):
        self.assertEqual(resting_study.STUDIES, (STUDY,))
        parser = resting_study.build_parser()
        required = {action.dest for action in parser._actions if action.required}
        self.assertEqual(required, {"study", "pass_name", "registration_sha256", "archive_root", "out",
                                    "code_commit", "ledger", "ledger_head"})
        self.assertEqual({action.dest for action in parser._actions if not action.required} - {"help"},
                         {"blind_receipt", "registration"})


if __name__ == "__main__":
    unittest.main()
