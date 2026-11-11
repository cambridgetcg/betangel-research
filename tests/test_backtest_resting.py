"""Registered measurement close-v1 (betangel_research.betfair.backtest.fills and resting).

Every archive here is synthetic and goes through the real ingest, index and replay, with the fixture helpers of
tests/test_betfair_backtest_replay.py and tests/test_backtest_lp.py (lp_market and its PRO ladder deltas). These
tests show the registration's checks, the view a rule file reads, the fill rules and the walk to the pull time on
these synthetic shapes only. They say nothing about real Betfair files or results.
"""

import bz2
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from betangel_research.betfair.backtest import fills
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, safe_target
from betangel_research.betfair.backtest.fills import CENTRAL, MODELS, OPTIMISTIC, PESSIMISTIC
from betangel_research.betfair.backtest.lp import BACK, CROSS, FILL_KINDS, JOIN, LAY, THROUGH, VOLUME, Leg
from betangel_research.betfair.backtest.lp import spread_ticks
from betangel_research.betfair.backtest.lp_study import _walk as lp_walk
from betangel_research.betfair.backtest.metrics import HashStream
from betangel_research.betfair.backtest.movement import (MARKET_CODES, Moment, RegistrationError, active_ids,
                                                         decision_time, exact, replay_market, select)
from betangel_research.betfair.backtest.movement import parse_registration as parse_movement
from betangel_research.betfair.backtest.replay import ReplayProblem, _member
from betangel_research.betfair.backtest.resting import (CLASSES, HARNESS_CODES, NULL_LABEL, NULL_UNDEFINED, SCORED,
                                                        TRD_COUNTS, Cell, OrderRow, named_files, parse_registration,
                                                        population_codes, record_code, view_of, walk)
from betangel_research.betfair.backtest.rulefile import RuleView, RunnerView, decide

import test_betfair_backtest_replay as fx
from test_backtest_lp import (BOOK, BSPS, FAV, M, OFF, R2, R3, R6, SIDS, before, book, ladder_move, lp_market, path,
                              status_at)
from test_backtest_lp import REG as LP_REG
from test_betfair_backtest_replay import definition, iso, mcm, md, ms, runner

REPO = Path(__file__).resolve().parent.parent
REGISTRATION = REPO / "research" / "studies" / "close-v1.json"
# The registration's sha256 as reviewed: any change to the file must change this line too (a new registration).
REGISTRATION_SHA256 = "32dd394bbf47cc666d95d17771a68f0469a35d921485d5845494eb4d24bc3e53"
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
        """{Cell: OrderRow or code} of one market's walk."""
        return self.walked(messages, **kwargs).outcomes

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
            "measurement.commission_rate:not_a_decimal_rate":
                lambda value: value["measurement"].update(commission_rate=0.02),
            "family.m:not_one_primary_per_rule_file": lambda value: value["family"].update(m=6),
            "family.method:not_supported": lambda value: value["family"].update(method="bonferroni"),
            "exposure.cites:not_the_splits_prior_exposure": lambda value: value["exposure"]["cites"].remove("lp-v1"),
            "exposure.lines:keys_not_the_registered_set": lambda value: value["exposure"]["lines"].pop("p0"),
            "selection.explore_model:not_a_fill_model": lambda value: value["selection"].update(explore_model="mid"),
            "selection.measure:not_supported": lambda value: value["selection"].update(measure="net_closed"),
            "null.label:not_supported": lambda value: value["null"].update(label="close-v1/null/{market_id}"),
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


def lp_like(rule):
    """A rule file changed to lp-v1's shape: any spread up to 20 ticks, every order pulled at off - 10 s."""
    rule["when"][-1]["value"] = 20
    for row in rule["times"]:
        row["pull_s"] = -10


class AgreesWithLp(RestingCase):
    """The pessimistic model is lp-v1's join rule but for the cross test. On markets where lp-v1 selects the
    favourite (a five-tick spread here), its join legs and this walk's pessimistic legs fill in the same batch by
    the same kind, unless a cross shows at least the stake but less than queue + stake."""

    WIDE = {3600: book(FAV=(2.5, 2.6))}
    LP_PLAN = plan_of(rules={BACK_RULE: lp_like, LAY_RULE: lp_like})

    def both(self, messages):
        """({(side, at_s): (kind, seconds)} of lp's join legs, the same of this walk's pessimistic legs), over
        the decision times at which lp selected the favourite."""
        root, manifest, records = self.build([(path(M), messages)])
        record = records[M]
        lp = lp_walk(root, manifest, record, LP_REG, [-600, -300, -120], DEFAULT_LIMITS)
        times = [at_s for at_s, (code, selection) in lp.selections.items()
                 if selection is not None and selection.selection_id == FAV]
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

    def test_the_one_difference_is_a_cross_below_queue_plus_stake(self):
        cross = ladder_move(200, atb=[[2.5, 0], [2.48, 50.0]], atl=[[2.6, 0], [2.5, 10.0]], sid=FAV)
        theirs, ours = self.both(lp_market(books=self.WIDE, extra=[cross]))
        for at_s, seconds in ((-600, 400.0), (-300, 100.0)):
            self.assertEqual(theirs[LAY, at_s], (None, None))                   # lp-v1: 10 shown, 52 needed
            self.assertEqual(ours[LAY, at_s], (CROSS, seconds))
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
                                            "fills", "null_mid", "null_reason", "trd"))
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


if __name__ == "__main__":
    unittest.main()
