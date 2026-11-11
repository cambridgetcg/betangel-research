"""Registered measurement close-v1: resting orders on the favourite before the off, on historical files (pure).

It asks whether a resting GBP 2 order on the favourite, placed minutes before the off and pulled before it, gets a
price that beats BSP once only the orders that would fill are counted (docs/research-loop.md). An "order" here is a
simulated intent inside the backtest package; nothing here can reach an exchange. It is not a backtest contract and
changes no existing source file, but as modules of this package fills.py and this file are in the engine
certificate's source descriptor, so adding them changed the certificate key again: any later P0 run-backtest needs
certify-engine at these sources. Nothing here opens a file, reads a clock or uses the random module.

Three files decide everything, and this module reads its values from them: the registration
(research/studies/close-v1.json), the rule files it names (research/rules/, parsed by rulefile.parse_rule) and the
split file it names (research/partitions-v1.json, parsed by partition.parse_split).

Registration (parse_registration): the exact keys of the registration at every level, every value of its type and
domain, the file in its one byte form (sorted keys, indent 2, ASCII, one final newline), and every list of codes,
classes, models, fill kinds, reasons and labels equal to this module's own, in order. It is given the raw bytes
of the split file and of each rule file, checks each sha256 against the one the registration names, and refuses:
rule files whose lineages differ; a partition label the split refuses (unknown, sealed, never, no budget) or of
the wrong role; a rule file whose when row uses one of this module's own codes (HARNESS_CODES) or a code the
registration's population.codes does not hold; a fixed price off the CLASSIC ladder. This version walks one order
per cell to a pull time before the off, so it also refuses a rule file that does not select the favourite, has
more or fewer than one intent, keeps its order, or has a time with no pull time. The answer is one frozen Plan.
A cell is one rule file at one of its times (Cell).

Population (record_code, then the walk): per cell, the first code that applies, in population_codes' order.
From the index record alone: movement.market_code's first twelve codes, through an adapter that holds the rule
file's market block and a minimum field of 0 (the field size is the rule file's own row); partition_not_registered
when the race date's label is not one of the two the registration names; then a flag code (bsp_market_false) when
the rule file gates that flag and its value as of t is exactly false. A gated flag with no value at t passes and
is reported (flags_missing). A record whose cells all have a code is never opened. In the walk: a view problem
(as lp.walk_market, never met for a cell that passed the record's gates); the code rulefile.decide gives for the
view at t (ladder_malformed first: a ladder entry, a trd entry or an rc runner id published at or before t could
not be read); price_off_ladder when the order's price is not a CLASSIC price. A cell with no code holds one order.

The view (view_of): the rulefile.RuleView of a movement.Moment. It holds one RunnerView per runner ACTIVE and
listed at t, and refuses (ValueError) unless the ids it is given are exactly the Moment's own active runners and
every price and size is an exact Fraction. So active_runners equals len(runners), which decide trusts.
market_open is 1 when the status as of t is OPEN and inPlay as of t is not true. spread_ticks is lp.spread_ticks.

The walk (walk): one pass over the decompressed primary member, following lp.walk_market's loop: the index's
reader and fold (market_index._messages, _checked_changes, _Market), replay.PriceFold, movement.LadderFold and
lp.TradedFold, messages sharing a pt as one batch, and a heap of timed events fired after the last batch
published at or before their time. It stops at the first message published after the last pull time of the cells
it walks; nothing later is parsed. A decide event captures movement's Moment at t (which refuses, ReplayProblem,
unless the fold equals the index at t), builds the view, asks rulefile.decide and places the order as one lp.Leg
per fill model: its queue is the model's fraction of the size shown at t at its price on its own side (a LAY
rests on atb, a BACK on atl), its base a copy of the runner's counted volume at t. Each batch after t up to and
including the pull time is judged by fills.py's rules. A lapse order is no longer judged once inPlay was true
before a batch; it stays ended even if inPlay is later published false. A null event captures the runner's quote
and the market's status and inPlay at a time u drawn at placement from metrics.HashStream with the registered
seed and label, uniform over the whole milliseconds of (t, pull time].

Per cell the walk returns a code or one OrderRow. A row holds no market id and no selection id: the side and
price; per model a Fill (the queue at t, the fill kind or None, seconds from t to the fill, whether it came within
each of the registered seconds of t, in the after-the-hour seconds, in a batch that left the market not OPEN, and
for an unfilled order whether the pull or the lapse ended it); the mid at t; the mid at u or the reason it has
none (NULL_UNDEFINED); three counts of the runner's trd rises over (t, pull time] (TRD_COUNTS); the class; the BSP
as written when the class is scored; the partition label, the half-year and the race date (the cluster key).

Classes (CLASSES), from facts that do not depend on a fill, the first that applies: movement.measure's five
(outcome_unknown, void_own_runner_removed, post_decision_removal, result_revised_after_close, missing_bsp); then
stream_malformed_after_placement when a ladder entry, a trd entry or an rc runner id published after t and up to
the pull time could not be read; else scored. A suspension, an early off or a revised schedule after t drops no
order. A removal classes the order and never re-prices it.

What this does not do: it reads nothing after the last pull time, models no latency, partial fill, re-pricing
or closing order, and scores nothing: g, the nets, the statistics, the selection and the labels are computed from
the rows elsewhere. Standard library only; it imports fills, rulefile, partition, lp, movement, market_index,
replay and metrics and, through them, the modules they import. Nothing here is evidence of profitability.
"""

from collections import namedtuple
from dataclasses import dataclass
from fractions import Fraction
import heapq
import itertools
import re

from . import fills, rulefile
from .lp import (FILL_KINDS, LAY, TIERS, TRADED_BASIS, TRADED_DIVISOR, TRADED_KEY, TradedFold, _Batch, _rules,
                 _seconds, Leg, after_the_hour, facing, mid, spread_ticks, top, wide)
from .market_index import _as_of, _checked_changes, _last, _Market, _messages, _Unreadable, loads_strict
from .metrics import HashStream
from .movement import (ANCHOR, CLUSTER, DATE_RE, ELIGIBLE, ISSUED, LADDER_H, LADDER_NAMES, LADDERS, MARKET_CODES,
                       MAX_REGISTRATION_BYTES, MONTH_RE, QUANTILE_METHOD, SCHEMA, UNCERTAINTY_METHOD, LadderFold,
                       _codes, _fail, _int, _moment, _object, _rate, _text, _texts, active_ids, decision_time,
                       market_code, measure, race_date, registration_sha256, tick_index)
from .partition import EXPLORE, HOLDOUT, PartitionError, file_bytes, parse_split
from .replay import PriceFold, ReplayProblem
from .rulefile import FAVOURITE, LAPSE, RuleError, RuleView, RunnerView, parse_rule

STUDY_ID = "close-v1"
TRIALS = "independent"
PARTITION_NOT_REGISTERED, PRICE_OFF_LADDER = "partition_not_registered", "price_off_ladder"
# The codes one index record can give a cell before its file is opened: movement.market_code's first twelve (its
# thirteenth, too_few_active_runners, is never given here: the adapter's minimum field is 0), then the split.
RECORD_CODES = MARKET_CODES[:12] + (PARTITION_NOT_REGISTERED,)
# (rule file flag, definition field, code when the field's value as of t is exactly false), in rulefile.FLAGS order.
FLAG_GATES = (("bsp_market", "bspMarket", "bsp_market_false"),
              ("turn_in_play_enabled", "turnInPlayEnabled", "turn_in_play_not_enabled"),
              ("persistence_enabled", "persistenceEnabled", "persistence_not_enabled"))
# This module's own codes: a when row of a rule file may use none of them (decide's own are refused by rulefile).
HARNESS_CODES = RECORD_CODES + tuple(code for _, _, code in FLAG_GATES) + (rulefile.LADDER_MALFORMED,
                                                                          PRICE_OFF_LADDER, ISSUED)
SELECTOR_CODES = {FAVOURITE: (rulefile.NO_PRICED_ACTIVE_RUNNER, rulefile.FAVOURITE_TIE)}
SCORED, MALFORMED_AFTER = "scored", "stream_malformed_after_placement"
CLASSES = ("outcome_unknown", "void_own_runner_removed", "post_decision_removal", "result_revised_after_close",
           "missing_bsp", MALFORMED_AFTER, SCORED)
PULLED, LAPSED = ENDS = ("pull", "lapse")
NULL_LABEL = "close-v1/null/{market_id}/{at_s}/{rule_id}"
# Why a null value is undefined, the first that applies (lp-v1's reasons for a start).
NULL_UNDEFINED = ("member_unreadable", "market_not_open", "in_play", "not_two_sided", "spread_above_max")
# Per order, over (t, pull time] and its runner: rises counted as trades; rises not counted, in an open batch
# outside the after-the-hour seconds, at a price that was the best back or best lay just before the batch (the
# check of the counting rule, as lp-v1's); rises in a batch that was not open.
TRD_COUNTS = ("rises_counted", "rises_not_counted_at_touch_outside_the_hour", "rises_in_a_batch_not_open")
VERDICTS = ("kill_optimistic", "underpowered", "edge_candidate", "measurement_pilot_candidate", "unresolved")
BLOCKED, FILLED_IS_ALL_PLACED = "claim_blocked_by_placebo", "filled_is_all_placed"
HOLM = "holm"
SELECT_MEASURE, SELECT_END, SELECT_TIE = "net_held", "claim_end", "later_time"
BSP_STEP = Fraction(1, 100)
MAX_SECONDS = 3600
FILE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\.json")
TOP_KEYS = ("blind_pass", "data", "decided_by", "diagnostics", "doc", "exposure", "family", "fill_models", "grid",
            "id", "measurement", "not_measured", "null", "population", "registered_on", "registered_pass", "schema",
            "seeds", "selection", "statistics", "status", "summary", "uncertainty", "verdicts")
DATA_KEYS = ("duplicates", "end_month", "files", "ladders", "month", "partitions", "provider", "rules", "source",
             "split", "start_month", "tiers", "traded_volume")
DECIDE, NULL = "decide", "null"

Cell = namedtuple("Cell", ("rule_id", "at_s", "pull_s"))
# One model's answer for one order (module docstring). within: one bool per Plan.within_s.
Fill = namedtuple("Fill", ("queue", "kind", "seconds", "within", "after_the_hour", "transition", "ended"))
OrderRow = namedtuple("OrderRow", ("side", "price", "label", "half", "race_date", "klass", "bsp", "mid", "fills",
                                   "null_mid", "null_reason", "trd"))
_Gate = namedtuple("_Gate", ("tiers", "start_month", "end_month", "countries", "event_type_id", "market_type",
                             "betting_type", "number_of_winners", "min_active_runners"))


# ---------------------------------------------------------------------------------------------------------------
# Registration

@dataclass(frozen=True)
class Plan:
    """What one registration, its rule files and its split file say (parse_registration). sha256 is the
    registration's; split is the partition.Split and rules the rulefile.Rules, in the registration's order; cells
    is every Cell, rule by rule, each rule's times in order. queue_fractions is {model: exact fraction}. The levels
    and thresholds are exact Fractions; pilot_floor is below 0. label_note and exposure_note are the two sentences
    the registration says are printed with every label."""

    sha256: str
    study: str
    lineage: str
    split: object
    rules: tuple
    cells: tuple
    explore_label: str
    test_label: str
    tiers: tuple
    ladders: dict
    start_month: str
    end_month: str
    divisor: int
    after_the_hour_s: int
    within_s: tuple
    codes: tuple
    commission: Fraction
    models: tuple
    queue_fractions: dict
    family_m: int
    alpha: Fraction
    null_seed: int
    max_spread_ticks: int
    null_fraction: Fraction
    null_alpha: Fraction
    select_explore_model: str
    select_test_model: str
    min_values: int
    min_tested_values: int
    min_tested_race_dates: int
    min_clusters: int
    confidence_level: Fraction
    kill_level: Fraction
    lower_bound_level: Fraction
    pilot_floor: Fraction
    half_year_min_fills: int
    bsp_step: Fraction
    bsp_whole_share: Fraction
    quantiles: tuple
    power: Fraction
    detectable_level: Fraction
    label_note: str
    exposure_note: str

    @property
    def labels(self):
        """The two partition labels a pass reads: (explore, test)."""
        return self.explore_label, self.test_label

    @property
    def months(self):
        """Every registered month from start_month to end_month, as "YYYY-MM" text."""
        year, month = map(int, self.start_month.split("-"))
        out = []
        while f"{year:04d}-{month:02d}" <= self.end_month:
            out.append(f"{year:04d}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return tuple(out)

    def rule(self, rule_id):
        """The rulefile.Rule of a cell's rule_id."""
        return next(rule for rule in self.rules if rule.id == rule_id)


def population_codes(rules):
    """The codes a cell of these rule files can get, in evaluation order (module docstring): the record's, the
    flag codes of the flags some rule file gates, ladder_malformed, then per rule file its market rows' codes, its
    selector's and its runner rows', then no_reference_price and price_off_ladder. issued is not in it."""
    flags = tuple(code for name, _, code in FLAG_GATES if any(getattr(rule.market, name) is True for rule in rules))
    decided = []
    for rule in rules:
        market = [row.code for row in rule.when if row.feature in rulefile.MARKET_FEATURES]
        runner = [row.code for row in rule.when if row.feature in rulefile.RUNNER_FEATURES]
        for code in market + list(SELECTOR_CODES.get(rule.select, ())) + runner:
            if code not in decided:
                decided.append(code)
    return RECORD_CODES + flags + (rulefile.LADDER_MALFORMED,) + tuple(decided) + (rulefile.NO_REFERENCE_PRICE,
                                                                                    PRICE_OFF_LADDER)


def _loaded(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_REGISTRATION_BYTES:
        _fail("file", "not_bytes_or_too_large")
    try:
        value = loads_strict(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        _fail("file", "not_json")
    return _object(value, "top", TOP_KEYS)


def _named(value, place):
    """(file name, sha256) of one {name, sha256} object. A name is a plain .json file name, never a path."""
    item = _object(value, place, ("name", "sha256"))
    if not isinstance(item["name"], str) or not FILE_RE.fullmatch(item["name"]):
        _fail(place, "name_malformed")
    if not isinstance(item["sha256"], str) or not rulefile.SHA256_RE.fullmatch(item["sha256"]):
        _fail(place, "sha256_malformed")
    return item["name"], item["sha256"]


def _files(top):
    """(data object, (split name, sha256), ((rule file name, sha256), ...)) of a registration's top object."""
    data = _object(top["data"], "data", DATA_KEYS)
    if not isinstance(data["rules"], list) or not 1 <= len(data["rules"]) <= rulefile.MAX_ROWS:
        _fail("data.rules", "not_a_list")
    named = tuple(_named(item, f"data.rules[{index}]") for index, item in enumerate(data["rules"]))
    if len({name for name, _ in named}) != len(named):
        _fail("data.rules", "name_used_twice")
    return data, _named(data["split"], "data.split"), named


def named_files(raw):
    """(split file name, (rule file name, ...)) a registration's raw bytes name, so that a runner knows which files
    to read before parse_registration; RegistrationError otherwise."""
    _, split, rules = _files(_loaded(raw))
    return split[0], tuple(name for name, _ in rules)


def _level(value, place):
    """A decimal level above one half, as an exact Fraction (movement.student_t_quantile's domain)."""
    level = _rate(value, place)
    if not Fraction(1, 2) < level:
        _fail(place, "not_above_one_half")
    return level


def _split(data, expected, raw):
    """The Split of the split file's bytes and the registration's (explore, test) labels."""
    if not isinstance(raw, bytes) or registration_sha256(raw) != expected[1]:
        _fail("data.split", "sha256_mismatch")
    try:
        split = parse_split(raw)
    except PartitionError as error:
        _fail("data.split", str(error))
    partitions = _object(data["partitions"], "data.partitions", ("explore", "rule", "test"))
    _text(partitions["rule"], "data.partitions.rule")
    for key, role in (("explore", EXPLORE), ("test", HOLDOUT)):
        problem = split.label_problem(partitions[key])
        if problem is not None:
            _fail(f"data.partitions.{key}", problem)
        if split.role(partitions[key]) != role:
            _fail(f"data.partitions.{key}", f"role_not_{role}")
    return split, partitions["explore"], partitions["test"]


def _rule_files(named, raws, codes):
    """The Rules of the named rule files' bytes, after the checks of the module docstring; codes is the
    registration's population.codes as written."""
    if not isinstance(raws, dict):
        _fail("data.rules", "not_given")
    rules = []
    for index, (name, sha256) in enumerate(named):
        place = f"data.rules[{index}]"
        raw = raws.get(name)
        if not isinstance(raw, bytes):
            _fail(place, "not_given")
        if registration_sha256(raw) != sha256:
            _fail(place, "sha256_mismatch")
        try:
            rule = parse_rule(raw)
        except RuleError as error:
            _fail(place, str(error))
        if name != f"{rule.id}.json":
            _fail(place, "name_not_the_rule_id")
        if rule.select not in SELECTOR_CODES:
            _fail(place, "select_not_favourite")
        if len(rule.intents) != 1:
            _fail(place, "not_one_intent")
        if rule.intents[0].persist != LAPSE:
            _fail(place, "persist_not_lapse")
        if any(pull_s is None for _, pull_s in rule.times):
            _fail(place, "time_without_a_pull")
        if any(row.fixed is not None and tick_index(row.fixed) is None for row in rule.intents):
            _fail(place, "fixed_price_off_ladder")
        for row in rule.when:
            if row.code in HARNESS_CODES:
                _fail(place, "when_code_reserved")
            if row.code not in codes:
                _fail(place, "when_code_not_in_population_codes")
        rules.append(rule)
    if len({rule.lineage for rule in rules}) != 1:
        _fail("data.rules", "lineages_differ")
    return tuple(rules)


def parse_registration(raw, split_raw, rules_raw):
    """The Plan of one registration file's raw bytes, the raw bytes of the split file it names and
    {rule file name: raw bytes} of the rule files it names, after every check in the module docstring;
    RegistrationError otherwise."""
    top = _loaded(raw)
    _text(top["schema"], "schema", SCHEMA)
    _text(top["id"], "id", STUDY_ID)
    _text(top["status"], "status", "registered")
    if not isinstance(top["registered_on"], str) or not DATE_RE.fullmatch(top["registered_on"]):
        _fail("registered_on", "not_a_date")
    for key in ("decided_by", "doc", "summary"):
        _text(top[key], key)
    _texts(top["not_measured"], "not_measured")
    blind = _object(top["blind_pass"], "blind_pass", ("releases", "rule", "withholds"))
    _texts(blind["releases"], "blind_pass.releases")
    _texts(blind["withholds"], "blind_pass.withholds")
    _text(blind["rule"], "blind_pass.rule")
    registered = _object(top["registered_pass"], "registered_pass", ("releases", "rule"))
    _texts(registered["releases"], "registered_pass.releases")
    _text(registered["rule"], "registered_pass.rule")

    data, split_named, rules_named = _files(top)
    _text(data["provider"], "data.provider", "betfair_historical_data")
    for key in ("duplicates", "files", "month", "source"):
        _text(data[key], f"data.{key}")
    tiers = _texts(data["tiers"], "data.tiers")
    if tiers != TIERS:
        _fail("data.tiers", "not_supported")
    ladders = _object(data["ladders"], "data.ladders", tiers)
    if any(ladders[tier] != LADDER_NAMES[tier] for tier in tiers):
        _fail("data.ladders", "not_supported")
    traded = _object(data["traded_volume"], "data.traded_volume", ("after_the_hour_s", "basis", "divisor", "key",
                                                                    "rule"))
    _text(traded["key"], "data.traded_volume.key", TRADED_KEY)
    _text(traded["basis"], "data.traded_volume.basis", TRADED_BASIS)
    _text(traded["rule"], "data.traded_volume.rule")
    if _int(traded["divisor"], "data.traded_volume.divisor", 1, 100) != TRADED_DIVISOR:
        _fail("data.traded_volume.divisor", "not_the_basis")
    months = (data["start_month"], data["end_month"])
    if not all(isinstance(month, str) and MONTH_RE.fullmatch(month) for month in months) or months[0] > months[1]:
        _fail("data.months", "malformed")
    split, explore, test = _split(data, split_named, split_raw)

    population = _object(top["population"], "population", ("codes", "codes_rule", "price_ladder", "quotes", "source"))
    for key in ("codes_rule", "price_ladder", "quotes", "source"):
        _text(population[key], f"population.{key}")
    rules = _rule_files(rules_named, rules_raw, _texts(population["codes"], "population.codes"))
    codes = _codes(population["codes"], "population.codes", population_codes(rules))
    cells = tuple(Cell(rule.id, at_s, pull_s) for rule in rules for at_s, pull_s in rule.times)
    grid = _object(top["grid"], "grid", ("anchor", "cells", "rule", "trials"))
    _text(grid["anchor"], "grid.anchor", ANCHOR)
    _text(grid["trials"], "grid.trials", TRIALS)
    _text(grid["rule"], "grid.rule")
    if _int(grid["cells"], "grid.cells", 1, 10 ** 4) != len(cells):
        _fail("grid.cells", "not_the_rule_files_times")

    models = _object(top["fill_models"], "fill_models", ("kinds", "names", "queue_fraction", "rule"))
    _codes(models["kinds"], "fill_models.kinds", FILL_KINDS)
    _codes(models["names"], "fill_models.names", fills.MODELS)
    _text(models["rule"], "fill_models.rule")
    fractions = {}
    for model, item in _object(models["queue_fraction"], "fill_models.queue_fraction", fills.MODELS).items():
        place = f"fill_models.queue_fraction.{model}"
        if not isinstance(item, list) or len(item) != 2:
            _fail(place, "not_a_numerator_and_denominator")
        below = _int(item[1], place, 1, 10 ** 6)
        fractions[model] = Fraction(_int(item[0], place, 0, below), below)
    if [fractions[model] for model in fills.MODELS] != sorted(fractions.values()):
        _fail("fill_models.queue_fraction", "not_in_the_models_order")

    measurement = _object(top["measurement"], "measurement", (
        "bsp_ends", "bsp_valid_if", "classes", "classes_rule", "commission_rate", "commission_source",
        "deciding_net", "g", "intent_to_treat", "mid", "net_closed", "net_held", "part_and_rest", "removals", "unit"))
    _codes(measurement["classes"], "measurement.classes", CLASSES)
    _texts(measurement["bsp_valid_if"], "measurement.bsp_valid_if")
    for key in ("classes_rule", "commission_source", "deciding_net", "g", "intent_to_treat", "mid", "net_closed",
                "net_held", "part_and_rest", "removals", "unit"):
        _text(measurement[key], f"measurement.{key}")
    ends = _object(measurement["bsp_ends"], "measurement.bsp_ends", ("rule", "step", "whole_hundredths_share"))
    _text(ends["rule"], "measurement.bsp_ends.rule")
    if _rate(ends["step"], "measurement.bsp_ends.step") != BSP_STEP:
        _fail("measurement.bsp_ends.step", "not_supported")

    family = _object(top["family"], "family", ("alpha", "m", "method", "rule"))
    _text(family["method"], "family.method", HOLM)
    _text(family["rule"], "family.rule")
    if _int(family["m"], "family.m", 1, 1000) != len(rules):
        _fail("family.m", "not_one_primary_per_rule_file")

    exposure = _object(top["exposure"], "exposure", ("cites", "fresh_on_test", "lines", "not_fresh_on_test",
                                                     "printed_with_every_label"))
    cites = _texts(exposure["cites"], "exposure.cites")
    if len(set(cites)) != len(cites) or set(cites) != set(split.prior_exposure):
        _fail("exposure.cites", "not_the_splits_prior_exposure")
    _rules(exposure["lines"], "exposure.lines", cites)
    for key in ("fresh_on_test", "not_fresh_on_test", "printed_with_every_label"):
        _text(exposure[key], f"exposure.{key}")

    selection = _object(top["selection"], "selection", ("bsp_end", "explore_model", "measure", "rule", "test_model",
                                                        "tie"))
    _text(selection["measure"], "selection.measure", SELECT_MEASURE)
    _text(selection["bsp_end"], "selection.bsp_end", SELECT_END)
    _text(selection["tie"], "selection.tie", SELECT_TIE)
    _text(selection["rule"], "selection.rule")
    for key in ("explore_model", "test_model"):
        if selection[key] not in fills.MODELS:
            _fail(f"selection.{key}", "not_a_fill_model")

    null = _object(top["null"], "null", ("alpha", "blocked", "fraction_of_main", "label", "max_spread_ticks", "rule",
                                         "undefined_reasons"))
    _text(null["label"], "null.label", NULL_LABEL)
    _text(null["blocked"], "null.blocked", BLOCKED)
    _text(null["rule"], "null.rule")
    _codes(null["undefined_reasons"], "null.undefined_reasons", NULL_UNDEFINED)
    seeds = _object(top["seeds"], "seeds", ("null",))

    verdicts = _object(top["verdicts"], "verdicts", (
        "half_year_min_fills", "kill_level", "lower_bound_level", "names", "pilot_floor_below_zero", "prediction",
        "printed_with", "rule"))
    _codes(verdicts["names"], "verdicts.names", VERDICTS)
    for key in ("prediction", "printed_with", "rule"):
        _text(verdicts[key], f"verdicts.{key}")

    statistics = _object(top["statistics"], "statistics", (
        "detectable_level", "min_tested_race_dates", "min_tested_values", "min_values", "power", "quantile_method",
        "quantiles", "rule"))
    _text(statistics["quantile_method"], "statistics.quantile_method", QUANTILE_METHOD)
    _text(statistics["rule"], "statistics.rule")
    if not isinstance(statistics["quantiles"], list) or not statistics["quantiles"]:
        _fail("statistics.quantiles", "not_a_list")
    quantiles = tuple(_rate(item, "statistics.quantiles") for item in statistics["quantiles"])
    if list(quantiles) != sorted(set(quantiles)) or quantiles[0] <= 0:
        _fail("statistics.quantiles", "not_sorted_and_unique")
    if set(quantiles) != {1 - q for q in quantiles}:
        _fail("statistics.quantiles", "not_symmetric")    # the blind pass gives q(1 - a) - q(a) only
    min_values = _int(statistics["min_values"], "statistics.min_values", 2, 10 ** 6)

    uncertainty = _object(top["uncertainty"], "uncertainty", ("cluster", "confidence_level", "method",
                                                              "min_clusters", "rule"))
    _text(uncertainty["method"], "uncertainty.method", UNCERTAINTY_METHOD)
    _text(uncertainty["cluster"], "uncertainty.cluster", CLUSTER)
    _text(uncertainty["rule"], "uncertainty.rule")
    diagnostics = _object(top["diagnostics"], "diagnostics", ("rule", "trd_counts", "within_s"))
    _text(diagnostics["rule"], "diagnostics.rule")
    _codes(diagnostics["trd_counts"], "diagnostics.trd_counts", TRD_COUNTS)
    plan = Plan(
        sha256=registration_sha256(raw), study=STUDY_ID, lineage=rules[0].lineage, split=split, rules=rules,
        cells=cells, explore_label=explore, test_label=test, tiers=tiers,
        ladders={tier: LADDERS[tier] for tier in tiers}, start_month=months[0], end_month=months[1],
        divisor=TRADED_DIVISOR,
        after_the_hour_s=_int(traded["after_the_hour_s"], "data.traded_volume.after_the_hour_s", 1, 3599),
        within_s=_seconds(diagnostics["within_s"], "diagnostics.within_s", MAX_SECONDS), codes=codes,
        commission=_rate(measurement["commission_rate"], "measurement.commission_rate"), models=fills.MODELS,
        queue_fractions=fractions, family_m=len(rules), alpha=_rate(family["alpha"], "family.alpha"),
        null_seed=_int(seeds["null"], "seeds.null", 0, 2 ** 63 - 1),
        max_spread_ticks=_int(null["max_spread_ticks"], "null.max_spread_ticks", 1, len(LADDER_H) - 1),
        null_fraction=_rate(null["fraction_of_main"], "null.fraction_of_main"),
        null_alpha=_rate(null["alpha"], "null.alpha"), select_explore_model=selection["explore_model"],
        select_test_model=selection["test_model"], min_values=min_values,
        min_tested_values=_int(statistics["min_tested_values"], "statistics.min_tested_values", min_values, 10 ** 9),
        min_tested_race_dates=_int(statistics["min_tested_race_dates"], "statistics.min_tested_race_dates", 2,
                                   10 ** 6),
        min_clusters=_int(uncertainty["min_clusters"], "uncertainty.min_clusters", 2, 10 ** 6),
        confidence_level=_level(uncertainty["confidence_level"], "uncertainty.confidence_level"),
        kill_level=_level(verdicts["kill_level"], "verdicts.kill_level"),
        lower_bound_level=_level(verdicts["lower_bound_level"], "verdicts.lower_bound_level"),
        pilot_floor=-_rate(verdicts["pilot_floor_below_zero"], "verdicts.pilot_floor_below_zero"),
        half_year_min_fills=_int(verdicts["half_year_min_fills"], "verdicts.half_year_min_fills", 2, 10 ** 6),
        bsp_step=BSP_STEP,
        bsp_whole_share=_rate(ends["whole_hundredths_share"], "measurement.bsp_ends.whole_hundredths_share"),
        quantiles=quantiles, power=_level(statistics["power"], "statistics.power"),
        detectable_level=_level(statistics["detectable_level"], "statistics.detectable_level"),
        label_note=verdicts["printed_with"], exposure_note=exposure["printed_with_every_label"])
    if file_bytes(top) != raw:                  # last: every value is now known to be small (as partition.py)
        _fail("file", "not_canonical")
    return plan


# ---------------------------------------------------------------------------------------------------------------
# The record's gates and the view

def race_label(plan, record):
    """(race date, partition label or None, half-year or None) of an index record, or None for a record with no
    anchor (it has no race date)."""
    if record["decision_time"]["first_published_market_time"] is None:
        return None
    day = race_date(record)
    return day, plan.split.label(day), plan.split.half(day)


def record_code(plan, record, cell):
    """(code, ()) when the index record alone gives the cell a code (RECORD_CODES, then a flag code); else
    (None, the gated flags with no true or false value as of t): the cell reaches the book."""
    rule = plan.rule(cell.rule_id)
    market = rule.market
    gate = _Gate(plan.tiers, plan.start_month, plan.end_month, market.countries, market.event_type_id,
                 market.market_type, market.betting_type, market.number_of_winners, 0)
    code = market_code(gate, record, cell.at_s)
    if code is not None:
        return code, ()
    if race_label(plan, record)[1] not in plan.labels:
        return PARTITION_NOT_REGISTERED, ()
    t, definition, missing = decision_time(record, cell.at_s), record["decision_time"]["definition"], []
    for name, field, failed in FLAG_GATES:
        if getattr(market, name) is True:
            value = _as_of(definition.get(field), t)
            if value is False:
                return failed, ()
            if value is not True:
                missing.append(name)
    return None, tuple(missing)


def view_of(moment, active, malformed=False):
    """The rulefile.RuleView of a movement.Moment with a view. active: the ids of the runners ACTIVE and listed at
    t, which must be exactly the Moment's own (ValueError otherwise, and for a price or size that is not an exact
    Fraction). malformed: whether a trd entry published at or before t could not be read."""
    if moment.view is None:
        raise ValueError("view:no_view_at_time")
    active = tuple(sorted(active))
    if active != tuple(sorted(moment.active())):
        raise ValueError("view:active_runners_differ")
    runners = []
    for sid in active:
        back, lay = moment.quote(sid)
        back, lay = back or (None, None), lay or (None, None)
        if any(value is not None and type(value) is not Fraction for value in back + lay):
            raise ValueError("view:not_exact")
        ticks = None if back[0] is None or lay[0] is None else spread_ticks(back[0], lay[0])
        runners.append(RunnerView(sid, back[0], back[1], lay[0], ticks))
    opened = moment.view.field("status") == fills.OPEN and moment.view.field("inPlay") is not True
    return RuleView(at_s=moment.offset_s, market_open=1 if opened else 0, active_runners=len(runners),
                    ladder_malformed=bool(moment.ladder_malformed or malformed), runners=tuple(runners))


# ---------------------------------------------------------------------------------------------------------------
# The walk to the pull

@dataclass(frozen=True)
class Walked:
    """outcomes {Cell: a population code, or the OrderRow of an issued cell} for every cell of the plan;
    flags_missing {Cell: the gated flags with no value at t} for every cell that passed the record's gates;
    replayed, whether the member was read."""

    outcomes: dict
    flags_missing: dict
    replayed: bool


class _Order:
    """One placed order while it is walked: its cell, the rulefile.Intent, t and the pull time in ms, one lp.Leg per
    model, the runners ACTIVE and listed at t, the mid at t, and what the walk fills in."""

    __slots__ = ("cell", "intent", "t", "end", "legs", "active", "mid", "lapsed", "transition", "null", "trd")

    def __init__(self, cell, intent, t, end, legs, active, centre):
        self.cell, self.intent, self.t, self.end, self.legs, self.active = cell, intent, t, end, legs, active
        self.mid, self.lapsed, self.transition, self.null = centre, False, {}, None
        self.trd = dict.fromkeys(TRD_COUNTS, 0)


def _mid(plan, quote):
    """(the exact mid of a quote, None), or (None, why it has none): not_two_sided, or spread_above_max when the
    best lay is more than max_spread_ticks ticks above the best back (too far apart to be a market)."""
    centre = mid(quote)
    if centre is None:
        return None, "not_two_sided"
    return (None, "spread_above_max") if wide(plan, quote) else (centre, None)


def _null(plan, capture):
    """(mid at u, None) or (None, the first NULL_UNDEFINED reason) of what a null event captured: (quote, status,
    inPlay), or None when the walk could not read through u."""
    if capture is None:
        return None, "member_unreadable"
    quote, status, in_play = capture
    if not fills.is_open(status):
        return None, "market_not_open"
    if in_play is True:
        return None, "in_play"
    return _mid(plan, quote)


def _row(plan, record, placed, dated, malformed, in_play):
    """The OrderRow of one walked order (module docstring). dated: race_label's answer; malformed: the pts of
    unreadable ladder or trd entries; in_play: the market's inPlay timeline as far as it was read."""
    intent, t, end = placed.intent, placed.t, placed.end
    klass, bsp = measure(record, t, intent.selection_id, placed.active)
    if klass == ELIGIBLE:
        klass = MALFORMED_AFTER if any(t < pt <= end for pt in malformed) else SCORED
    lapsed = intent.persist == LAPSE and (_as_of(in_play, t) is True
                                          or any(t < pt < end and value is True for pt, value in in_play))
    found = {}
    for model, leg in placed.legs.items():
        if leg.fill_pt is None:
            found[model] = Fill(leg.queue, None, None, (False,) * len(plan.within_s), False, False,
                                LAPSED if lapsed else PULLED)
            continue
        took = leg.fill_pt - t
        found[model] = Fill(leg.queue, leg.fill_kind, took / 1000, tuple(took <= s * 1000 for s in plan.within_s),
                            after_the_hour(leg.fill_pt, plan.after_the_hour_s), placed.transition[model], None)
    null_mid, null_reason = _null(plan, placed.null)
    day, label, half = dated
    return OrderRow(side=intent.side, price=intent.price, label=label, half=half, race_date=day, klass=klass,
                    bsp=bsp if klass == SCORED else None, mid=placed.mid, fills=found, null_mid=null_mid,
                    null_reason=null_reason, trd=dict(placed.trd))


def walk(stream, record, plan, limits):
    """The Walked of one index record: every cell's code from the record alone and, when some cell reaches the
    book, the rest from the record's primary member's decompressed stream (module docstring). stream is not read
    when no cell reaches the book. ReplayProblem when the stream is not what the index says."""
    market_id = record["market_id"]
    outcomes, missing = {}, {}
    for cell in plan.cells:
        outcomes[cell], flags = record_code(plan, record, cell)
        if outcomes[cell] is None:
            missing[cell] = flags
    cells = [cell for cell in plan.cells if outcomes[cell] is None]
    if not cells:
        return Walked(outcomes=outcomes, flags_missing=missing, replayed=False)
    off, dated = decision_time(record, 0), race_label(plan, record)
    until = off + max(cell.pull_s for cell in cells) * 1000
    order = itertools.count()
    events = [(off + cell.at_s * 1000, next(order), DECIDE, cell) for cell in cells]
    heapq.heapify(events)
    market, prices, ladders, traded = None, PriceFold(), LadderFold(plan.ladders[record["provenance"]["plan"]]), \
        TradedFold()
    orders = []

    def state():                                # (status, inPlay) in force after every message applied so far
        if market is None:
            return None, None
        return _last(market.fields.get("status")), _last(market.fields.get("inPlay"))

    def decide(time, cell):
        moment = _moment(record, market, prices, ladders, cell.at_s, time)
        if moment.view is None:
            return moment.reason
        active = active_ids(record, time)
        decision = rulefile.decide(view_of(moment, active, any(pt <= time for pt in traded.malformed)),
                                   plan.rule(cell.rule_id))
        if decision.code is not None:
            if decision.code not in plan.codes:
                raise ValueError("resting:code_not_in_population_codes")
            return decision.code
        intent = decision.intents[0]            # one order a cell: the plan's rule files have one intent
        if tick_index(intent.price) is None:
            return PRICE_OFF_LADDER
        sid, end = intent.selection_id, off + intent.pull_s * 1000
        back_book, lay_book = ladders.books.get(sid, ({}, {}))
        shown = (back_book if intent.side == LAY else lay_book).get(intent.price)    # a LAY rests on the back side
        shown, base = shown[1] if shown else Fraction(0), dict(traded.counted.get(sid, {}))
        legs = {model: Leg(cell.at_s, model, intent.side, sid, intent.price,
                           fills.queue_ahead(plan.queue_fractions[model], shown), time, end, base)
                for model in plan.models}
        placed = _Order(cell, intent, time, end, legs, active, _mid(plan, moment.quote(sid))[0])
        label = NULL_LABEL.format(market_id=market_id, at_s=cell.at_s, rule_id=cell.rule_id)
        heapq.heappush(events, (time + 1 + HashStream(plan.null_seed, label).below(end - time), next(order), NULL,
                                placed))
        orders.append(placed)
        return None

    def handle(time, kind, payload):
        if kind == DECIDE:
            outcomes[payload] = decide(time, payload)
        else:
            payload.null = (top(ladders, payload.intent.selection_id), *state())

    def fire(limit):                            # every event before limit (None: every event)
        while events and (limit is None or events[0][0] < limit):
            time, _, kind, payload = heapq.heappop(events)
            handle(time, kind, payload)

    def close(pt, batch, prior, was_open, was_in_play):     # one pt batch is complete: count its trades, then fill
        counted = {}
        for rid, price, rise in fills.counted_rises(batch, was_open):
            traded.count(rid, price, rise)
            counted.setdefault(rid, set()).add(price)
        hour, left_open = after_the_hour(pt, plan.after_the_hour_s), fills.is_open(state()[0])
        for placed in orders:
            if not placed.t < pt <= placed.end:
                continue
            intent = placed.intent
            sid, now = intent.selection_id, counted.get(intent.selection_id, ())
            for rid, price, _ in batch.rises:
                if rid != sid:
                    continue
                if not was_open:
                    placed.trd[TRD_COUNTS[2]] += 1
                elif price in now:
                    placed.trd[TRD_COUNTS[0]] += 1
                elif not hour and any(quote is not None and quote[0] == price for quote in prior[sid]):
                    placed.trd[TRD_COUNTS[1]] += 1
            placed.lapsed = placed.lapsed or (was_in_play and intent.persist == LAPSE)
            shown = None
            if batch.image or sid in batch.changed:
                shown = facing(ladders, placed.legs[plan.models[0]])     # one side and price for every model
            at_price = traded.counted.get(sid, {}).get(intent.price, 0)
            for model, leg in placed.legs.items():
                if not fills.judged(leg, pt, was_open, placed.lapsed, intent.persist):
                    continue
                kind = fills.judge(leg, intent.stake, plan.divisor, now, at_price, shown)
                if kind is not None:
                    leg.fill_pt, leg.fill_kind, placed.transition[model] = pt, kind, not left_open

    # A batch no order's window holds is folded but not kept: fills and the counts read counted volume only as its
    # rise since t, and only in (t, pull time].
    unreadable_from, applied, batch, pending, prior, fresh = None, None, None, None, {}, False
    was_open = was_in_play = False
    messages = _messages(stream, limits)
    while True:
        try:                                    # only reading the stream can make the member unreadable
            item = next(messages, None)
        except _Unreadable as error:
            unreadable_from = error.pt if error.pt is not None else applied
            unreadable_from = -1 if unreadable_from is None else unreadable_from
            break
        except (OSError, EOFError, ValueError):
            unreadable_from = -1 if applied is None else applied
            break
        if item is None:
            break
        pt, message = item
        if pt != batch:
            if pending is not None:
                close(batch, pending, prior, was_open, was_in_play)
            batch, pending, fresh = pt, None, True
        fire(pt)
        if pt > until:
            break
        try:
            checked = _checked_changes(message)
        except _Unreadable:
            unreadable_from = pt
            break
        if fresh:                               # the market and the resting orders' quotes just before this pt batch
            runners, fresh = {placed.intent.selection_id for placed in orders if placed.t < pt <= placed.end}, False
            prior = {sid: top(ladders, sid) for sid in runners}
            pending = _Batch() if runners else None
            status, in_play = state()
            was_open, was_in_play = fills.is_open(status), in_play is True
        for mid_, change in checked:
            if mid_ != market_id:
                continue
            if market is None:
                market = _Market(market_id, pt)
            market.change(pt, change)
            prices.change(pt, change)
            ladders.change(pt, change)
            moved = traded.change(pt, change)
            if pending is not None:
                pending.note(ladders.keys, change, moved)
        applied = pt
    if pending is not None:
        close(batch, pending, prior, was_open, was_in_play)
    if unreadable_from is not None and unreadable_from > until:
        unreadable_from = None
    if unreadable_from is None:
        fire(None)
    else:
        if record["evaluator_only_outcome"]["read_complete"] is True:
            raise ReplayProblem(f"replay:{market_id}:unreadable_where_the_index_read")
        fire(unreadable_from)
        if any(kind == DECIDE for _, _, kind, _ in events):     # the index says each walked cell has a view at t
            raise ReplayProblem(f"replay:{market_id}:unreadable_before_view_ends")
    malformed = sorted(set(ladders.malformed) | set(traded.malformed))
    in_play = market.fields.get("inPlay", []) if market is not None else []
    for placed in orders:
        outcomes[placed.cell] = _row(plan, record, placed, dated, malformed, in_play)
    return Walked(outcomes=outcomes, flags_missing=missing, replayed=True)
