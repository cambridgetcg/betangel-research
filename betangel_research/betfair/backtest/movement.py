"""Registered measurement movement-v1: Pilot 1's rule replayed on historical files and priced against BSP (pure).

Yu reversed the 2026-10-16 deferral of the price-movement diagnostic on 2026-10-31, so that this study runs before
Pilot 1's first live bet (docs/movement-study.md). It is not a backtest contract and changes no existing source
file, but as a module of this package it (with movement_study.py) is in the engine certificate's source descriptor,
so adding it changed the certificate key: any later P0 run-backtest needs certify-engine at these sources. The
registration (research/studies/movement-v1.json) holds every rule that decides anything, and this module reads its
values from there, refuses one whose rule tokens are not the ones implemented here, and computes one market at a
time from what the index and the market's own replayed stream say. movement_study.py reads the files, aggregates and
writes; nothing here opens a file, reads a clock or uses the random module.

Registration (parse_registration): the exact keys of research/studies/movement-v1.json at every level, every value
of its type and domain, and every list of codes, classes, cells, contrasts and controls equal to this module's
own, in order (so a registration cannot reorder a rule this code would silently ignore). The price bands must
cover every CLASSIC price from best_back_min_h to best_back_max_h exactly once, and the field-size bands every
count from min_active_runners up. registration_sha256 is the sha256 of the file's raw bytes, as contract p1's is.

Replay (replay_market): one pass over the decompressed primary member, with the index's own reader and fold
(market_index._messages, _checked_changes, _Market; replay.PriceFold), plus LadderFold, until the first message
published after `until` (the first-published off: nothing later is read). At every requested time t it captures a
Moment: the DecisionView replay.view_from_fold builds, which refuses (ReplayProblem) unless the fold's decision-safe
state at t equals the index record's decision_time entries at or before t, and each runner's best back and best lay
at t. A time with no view in the index (no anchor yet, or t at or after view_ends) gets no view and the index's
reason; a member that stops being readable before a time the index says has a view refuses
(unreadable_before_view_ends), as replay_view does. The caller hashes the member's bytes and compares them with the
manifest, as replay_view does.

Ladders (LadderFold): the deltas are folded by the stream cache's rules that betangel_research/betfair/book.py
documents and implements (F54: _apply_points, _apply_levels, _level_prefix; tests/test_backtest_movement.py checks
the two agree). PRO's atb and atl are price-keyed [price, size] deltas: a size of 0 deletes that price, any other
entry replaces it, and an empty list empties that ladder. ADVANCED's batb and batl are level-keyed [level, price,
size] deltas: a size of 0 at level k empties level k and every deeper level (best-N ladders are contiguous, so
[0, 0, 0] empties the ladder), any other entry replaces its level, and an empty list changes nothing. The registration
says which keys a tier uses and the other is never read. A null value changes nothing, as in book.py, and its pt is
kept (nulls) for a diagnostic count. An image (img true) replaces every runner's ladders: a runner it carries has
exactly the levels it carries (none when its rc entry has no such key) and one it does not carry has none (plan
section 5, img_replaces_market_state; docs/data-audit.md: images may omit runners). A PRO best back is the highest
price, a best lay the lowest; an ADVANCED best back or lay is level 0 of the contiguous prefix from level 0 that is
strictly ordered by price (level_prefix; a missing level 0 means none). Each is the exact number the file wrote (an
int, or a float by its shortest repr). An rc entry whose runner id does not parse, or a ladder entry that is not a
list of the tier's width holding finite numbers (a level a non-negative int, a size at least 0, a price above 1
unless its size is 0, and a price present on a PRO entry of size 0, which names the point), is malformed: the market
is ladder_malformed at every time from then on, never guessed. Unlike book.py, the fold does not clear quotes while
the market is not OPEN: pilot.py has no status predicate, so movement_study.py counts such decisions instead.

Decisions: market_code reads the record's plan, anchor, view and decision_time entries as of t (the codes before
the book predicates need no replay); select applies Pilot 1's selection predicates (pilot.py evaluate_predicates,
in its order, with its codes) to the replayed quotes at t, favourite giving the unique lowest best back first.
measure classes an issued decision from the record's evaluator_only_outcome (outcome._removed_after for removals;
results_revised_after_close apart, since Pilot 1 freezes BSP at the first CLOSED read and these are final values)
and nclv computes contract p1's NCLV exactly.

Late move: field_share gives a runner's field-normalised midpoint share at one Moment, or the registered reason it
is undefined; late_move_cell maps M to STEAM, FLAT or DRIFT; window_problem checks a window (start, end] of the
replayed fold for a member not read through it, a removal or a status other than OPEN.

Statistics (sample_sd, nearest_rank, cr1_mean, cr1_contrast, permuted_contrast, holm, student_t_cdf,
student_t_quantile, drawn, kill_and_flags): standard library floats, except NCLV itself, which is exact until it is
aggregated. permuted_contrast is the shuffled-M control's exact within-date permutation expectation, so no seed
decides it; kill_and_flags applies the registered kill and discovery rules to computed contrasts. The Student t
routine is pilot.py's (Lentz continued fraction, Numerical Recipes 6.4) copied, not imported: importing pilot.py
would add it with constants.py and ticks.py to the backtest closure that the engine certificate's source descriptor
pins to this package (tests/test_betfair_backtest_boundaries.py), and so would importing the CLASSIC bands from
constants.py (CLASSIC_BANDS_H here; tests/test_backtest_movement.py checks both copies). Seeded draws use
metrics.HashStream.

Standard library only; it imports archive, market_index, replay, outcome, accounting, metrics and the repository's
temporal module through them. Nothing here is evidence of profitability.
"""

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import hashlib
import math
import re

from .accounting import race_day_of
from .archive import utc_moment, utc_month
from .market_index import _as_of, _checked_changes, _Market, _messages, _runner_id, _Unreadable, loads_strict
from .metrics import HashStream
from .outcome import _removed_after
from .replay import PriceFold, ReplayProblem, anchor_and_decision, view_from_fold

SCHEMA = "betangel.study-registration/v1"
STUDY_ID = "movement-v1"
LADDERS = {"ADVANCED": ("batb", "batl"), "PRO": ("atb", "atl")}
LADDER_NAMES = {"ADVANCED": "batb_batl", "PRO": "atb_atl"}
ANCHOR = "first_published_scheduled_off"
UNREADABLE_VIEW_END = "member_unreadable_from_pt"
ISSUED = "issued"
# Market codes in evaluation order (registration population.codes): the history gates, then Pilot 1's.
MARKET_CODES = ("plan_not_registered", "no_view", "month_not_registered", "anchor_not_known_by_decision",
                "view_ended_before_decision", "member_unreadable_before_decision", "schedule_revised_before_decision",
                "country_not_allowed", "event_type_not_allowed", "market_type_not_allowed", "betting_type_not_odds",
                "number_of_winners_not_1", "too_few_active_runners", "ladder_malformed", "no_priced_active_runner",
                "favourite_tie", "best_back_off_ladder", "best_back_out_of_range", "depth_below_min",
                "best_lay_missing", "book_crossed", "best_lay_off_ladder", "spread_too_wide")
CLASSES = ("outcome_unknown", "void_own_runner_removed", "post_decision_removal", "result_revised_after_close",
           "missing_bsp", "eligible")
ELIGIBLE = "eligible"
LATE_EXCLUSIONS = ("window_unknown", "removal_in_window", "suspension_in_window", "cell_undefined")
UNDEFINED_REASONS = ("no_view_at_time", "ladder_malformed", "runner_not_active", "runner_not_two_sided",
                     "other_runner_not_two_sided", "runner_off_ladder", "runner_spread_too_wide")
STEAM, FLAT, DRIFT = "STEAM", "FLAT", "DRIFT"
CELLS = (STEAM, FLAT, DRIFT)
CONTRASTS = (("STEAM-FLAT", STEAM, FLAT), ("DRIFT-FLAT", DRIFT, FLAT))
CONTROLS = ("earlier_window", "shuffled_m", "random_runner")
# Why a control blocks a flag (registration registered_pass.kill_rule), in the order they are tested.
BLOCKS = ("control_not_computed", "main_not_computed_on_same_bets", "control_not_below")
UNCERTAINTY_METHOD = "cr1_t_g_minus_1"
CLUSTER = "race_date_europe_london"
QUANTILE_METHOD = "nearest_rank"
RANDOM_RUNNER_LABEL = "movement-v1/random-runner/{market_id}"
RESULT_STATUSES = ("LOSER", "WINNER")
REMOVED_STATUSES = ("REMOVED", "REMOVED_VACANT")
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
RATE_RE = re.compile(r"0\.[0-9]{1,6}")
MAX_REGISTRATION_BYTES = 1 << 20
MAX_OFFSET_S = 86400
# The CLASSIC ladder in hundredths, (from inclusive, to exclusive, step): betfair/constants.py's CLASSIC_BANDS (fact
# F41), copied (module docstring); the final bound, 1000.00, is itself on the ladder.
CLASSIC_BANDS_H = ((101, 200, 1), (200, 300, 2), (300, 400, 5), (400, 600, 10), (600, 1000, 20), (1000, 2000, 50),
                   (2000, 3000, 100), (3000, 5000, 200), (5000, 10000, 500), (10000, 100000, 1000))
LADDER_H = tuple(price for low, high, step in CLASSIC_BANDS_H for price in range(low, high, step)) + (100000,)
_TICK = {price: index for index, price in enumerate(LADDER_H)}


class RegistrationError(ValueError):
    """A registration this module cannot run; str() is "registration:<place>:<reason>"."""


# ---------------------------------------------------------------------------------------------------------------
# Registration

def registration_sha256(raw):
    """The registration's identity: the sha256 of the file's raw bytes."""
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Registration:
    """The values of research/studies/movement-v1.json this module runs (parse_registration)."""

    sha256: str
    tiers: tuple
    ladders: dict
    start_month: str
    end_month: str
    decision_offsets_s: tuple
    countries: tuple
    event_type_id: str
    market_type: str
    betting_type: str
    number_of_winners: int
    min_active_runners: int
    best_back_min_h: int
    best_back_max_h: int
    min_available_p: int
    max_lay_ticks: int
    commission: Fraction
    price_bands: tuple
    field_bands: tuple
    late_offset_s: int
    late_from_offset_s: int
    move_threshold: Fraction
    max_selected_spread_ticks: int
    placebo_from_offset_s: int
    placebo_to_offset_s: int
    confidence_level: Fraction
    min_clusters: int
    quantiles: tuple
    min_bets_for_statistics: int
    holm_alpha: Fraction
    min_cell_bets: int
    min_cell_race_dates: int
    kill_fraction: Fraction
    kill_alpha: Fraction
    min_cell_mean_nclv: Fraction
    random_runner_seed: int
    min_stake_p: int

    @property
    def replay_offsets_s(self):
        """Every offset a replay captures: the decision times and both windows' ends."""
        return tuple(sorted(set(self.decision_offsets_s) | {self.late_from_offset_s, self.placebo_from_offset_s,
                                                            self.placebo_to_offset_s}))

    @property
    def months(self):
        """Every registered month from start_month to end_month, as "YYYY-MM" text."""
        year, month = map(int, self.start_month.split("-"))
        out = []
        while f"{year:04d}-{month:02d}" <= self.end_month:
            out.append(f"{year:04d}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return tuple(out)


def _fail(place, reason):
    raise RegistrationError(f"registration:{place}:{reason}")


def _object(value, place, keys):
    if not isinstance(value, dict):
        _fail(place, "not_an_object")
    if set(value) != set(keys):
        _fail(place, "keys_not_the_registered_set")
    return value


def _text(value, place, expected=None):
    if not isinstance(value, str) or not value.strip():
        _fail(place, "not_text")
    if expected is not None and value != expected:
        _fail(place, "not_supported")
    return value


def _int(value, place, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail(place, "not_an_int_in_range")
    return value


def _rate(value, place):
    if not isinstance(value, str) or not RATE_RE.fullmatch(value):
        _fail(place, "not_a_decimal_rate")
    return Fraction(Decimal(value))


def _texts(value, place):
    if not isinstance(value, list) or not value:
        _fail(place, "not_a_list")
    return tuple(_text(item, place) for item in value)


def _codes(value, place, expected):
    if tuple(_texts(value, place)) != tuple(expected):
        _fail(place, "not_the_implemented_list")
    return tuple(expected)


def _offset(value, place):
    return _int(value, place, -MAX_OFFSET_S, -1)


def _bands(value, place, lows_from, ladder):
    """((name, low, high or None), ...) after checking the bands are contiguous; ladder: whether bounds are CLASSIC
    prices (each on the ladder, and the next band starting at the next ladder price) or counts (next = high + 1)."""
    if not isinstance(value, list) or not value:
        _fail(place, "not_a_list")
    bands, expected = [], lows_from
    for index, item in enumerate(value):
        last = index == len(value) - 1
        if not (isinstance(item, list) and len(item) == 3 and isinstance(item[0], str) and type(item[1]) is int
                and (type(item[2]) is int or (item[2] is None and last and not ladder))):
            _fail(place, "malformed_band")
        name, low, high = item
        if low != expected or (high is not None and high < low) or (ladder and (low not in _TICK or high not in _TICK)):
            _fail(place, "bands_not_contiguous")
        bands.append((name, low, high))
        if high is not None:
            expected = LADDER_H[_TICK[high] + 1] if ladder and _TICK[high] + 1 < len(LADDER_H) else high + 1
    if len({name for name, _, _ in bands}) != len(bands):
        _fail(place, "band_names_not_unique")
    return tuple(bands)


def parse_registration(raw):
    """The Registration of one registration file's raw bytes after every check in the module docstring;
    RegistrationError otherwise."""
    if not isinstance(raw, bytes) or len(raw) > MAX_REGISTRATION_BYTES:
        _fail("file", "not_bytes_or_too_large")
    try:
        value = loads_strict(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        _fail("file", "not_json")
    top = _object(value, "top", ("blind_pass", "data", "decided_by", "diagnostics", "doc", "id", "late_move",
                                 "measurement", "population", "registered_on", "registered_pass", "schema", "seeds",
                                 "statistics", "status", "strata", "summary", "timing", "uncertainty"))
    _text(top["schema"], "schema", SCHEMA)
    _text(top["id"], "id", STUDY_ID)
    _text(top["status"], "status", "registered")
    if not isinstance(top["registered_on"], str) or not DATE_RE.fullmatch(top["registered_on"]):
        _fail("registered_on", "not_a_date")
    for key in ("decided_by", "doc", "summary"):
        _text(top[key], key)
    blind = _object(top["blind_pass"], "blind_pass", ("releases", "rule", "withholds"))
    _texts(blind["releases"], "blind_pass.releases")
    _texts(blind["withholds"], "blind_pass.withholds")
    _text(blind["rule"], "blind_pass.rule")

    data = _object(top["data"], "data", ("duplicates", "end_month", "ladders", "month", "provider", "source",
                                         "start_month", "tiers"))
    _text(data["provider"], "data.provider", "betfair_historical_data")
    for key in ("duplicates", "month", "source"):
        _text(data[key], f"data.{key}")
    tiers = _texts(data["tiers"], "data.tiers")
    if tiers != tuple(sorted(set(tiers))) or not set(tiers) <= set(LADDERS):
        _fail("data.tiers", "not_supported")
    ladders = _object(data["ladders"], "data.ladders", tiers)
    if any(ladders[tier] != LADDER_NAMES[tier] for tier in tiers):
        _fail("data.ladders", "not_supported")
    months = (data["start_month"], data["end_month"])
    if not all(isinstance(month, str) and MONTH_RE.fullmatch(month) for month in months) or months[0] > months[1]:
        _fail("data.months", "malformed")

    timing = _object(top["timing"], "timing", ("anchor", "decision_offsets_s", "rule"))
    _text(timing["anchor"], "timing.anchor", ANCHOR)
    _text(timing["rule"], "timing.rule")
    if not isinstance(timing["decision_offsets_s"], list) or not timing["decision_offsets_s"]:
        _fail("timing.decision_offsets_s", "not_a_list")
    offsets = tuple(_offset(item, "timing.decision_offsets_s") for item in timing["decision_offsets_s"])
    if list(offsets) != sorted(set(offsets)):
        _fail("timing.decision_offsets_s", "not_sorted_and_unique")

    population = _object(top["population"], "population", (
        "best_back_max_h", "best_back_min_h", "betting_type", "codes", "codes_rule", "countries", "event_type_id",
        "market_type", "max_best_lay_ticks_above_best_back", "min_active_runners", "min_available_at_best_back_p",
        "not_checked", "number_of_winners", "price_ladder", "quotes", "selection_rule", "source"))
    _codes(population["codes"], "population.codes", MARKET_CODES)
    for key in ("codes_rule", "not_checked", "price_ladder", "quotes", "selection_rule", "source"):
        _text(population[key], f"population.{key}")
    countries = _texts(population["countries"], "population.countries")
    low_h = _int(population["best_back_min_h"], "population.best_back_min_h", LADDER_H[0], LADDER_H[-1])
    high_h = _int(population["best_back_max_h"], "population.best_back_max_h", low_h, LADDER_H[-1])
    if low_h not in _TICK or high_h not in _TICK:
        _fail("population.best_back", "off_ladder")
    min_active = _int(population["min_active_runners"], "population.min_active_runners", 1, 1000)

    measurement = _object(top["measurement"], "measurement", ("P", "bsp_valid_if", "classes", "classes_rule",
                                                              "commission_rate", "nclv", "ticks"))
    _codes(measurement["classes"], "measurement.classes", CLASSES)
    _texts(measurement["bsp_valid_if"], "measurement.bsp_valid_if")
    for key in ("P", "classes_rule", "nclv", "ticks"):
        _text(measurement[key], f"measurement.{key}")

    strata = _object(top["strata"], "strata", ("field_size_bands", "field_size_rule", "price_band_rule",
                                               "price_bands_h"))
    price_bands = _bands(strata["price_bands_h"], "strata.price_bands_h", low_h, ladder=True)
    if price_bands[-1][2] != high_h:
        _fail("strata.price_bands_h", "bands_not_contiguous")
    field_bands = _bands(strata["field_size_bands"], "strata.field_size_bands", min_active, ladder=False)
    if field_bands[-1][2] is not None:
        _fail("strata.field_size_bands", "last_band_not_open")
    for key in ("field_size_rule", "price_band_rule"):
        _text(strata[key], f"strata.{key}")

    late = _object(top["late_move"], "late_move", ("cells", "decision_offset_s", "exclusions", "from_offset_s",
                                                   "max_selected_spread_ticks", "measure", "population", "q",
                                                   "threshold", "undefined", "undefined_reasons", "window"))
    cells = _object(late["cells"], "late_move.cells", CELLS)
    for cell in CELLS:
        _text(cells[cell], f"late_move.cells.{cell}")
    _codes(late["exclusions"], "late_move.exclusions", LATE_EXCLUSIONS)
    _codes(late["undefined_reasons"], "late_move.undefined_reasons", UNDEFINED_REASONS)
    for key in ("measure", "population", "q", "undefined", "window"):
        _text(late[key], f"late_move.{key}")
    late_offset = _offset(late["decision_offset_s"], "late_move.decision_offset_s")
    late_from = _offset(late["from_offset_s"], "late_move.from_offset_s")
    if late_offset not in offsets or not late_from < late_offset:
        _fail("late_move.offsets", "not_supported")

    uncertainty = _object(top["uncertainty"], "uncertainty", ("cluster", "confidence_level", "method",
                                                              "min_clusters", "rule"))
    _text(uncertainty["method"], "uncertainty.method", UNCERTAINTY_METHOD)
    _text(uncertainty["cluster"], "uncertainty.cluster", CLUSTER)
    _text(uncertainty["rule"], "uncertainty.rule")

    statistics = _object(top["statistics"], "statistics", ("min_bets_for_statistics", "quantile_method",
                                                           "quantiles", "rule"))
    _text(statistics["quantile_method"], "statistics.quantile_method", QUANTILE_METHOD)
    _text(statistics["rule"], "statistics.rule")
    if not isinstance(statistics["quantiles"], list) or not statistics["quantiles"]:
        _fail("statistics.quantiles", "not_a_list")
    quantiles = tuple(_rate(item, "statistics.quantiles") for item in statistics["quantiles"])
    if list(quantiles) != sorted(set(quantiles)) or quantiles[0] <= 0:
        _fail("statistics.quantiles", "not_sorted_and_unique")

    registered = _object(top["registered_pass"], "registered_pass", (
        "contrasts", "contrasts_rule", "controls", "discovery", "holm", "kill_rule", "min_cell_bets",
        "min_cell_race_dates", "releases"))
    _codes(registered["contrasts"], "registered_pass.contrasts", [name for name, _, _ in CONTRASTS])
    for key in ("contrasts_rule", "releases"):
        _text(registered[key], f"registered_pass.{key}")
    controls = _object(registered["controls"], "registered_pass.controls", ("names", *CONTROLS))
    _codes(controls["names"], "registered_pass.controls.names", CONTROLS)
    earlier = _object(controls["earlier_window"], "registered_pass.controls.earlier_window",
                      ("from_offset_s", "rule", "to_offset_s"))
    _text(earlier["rule"], "registered_pass.controls.earlier_window.rule")
    placebo_from = _offset(earlier["from_offset_s"], "registered_pass.controls.earlier_window.from_offset_s")
    placebo_to = _offset(earlier["to_offset_s"], "registered_pass.controls.earlier_window.to_offset_s")
    if not placebo_from < placebo_to <= late_from:
        _fail("registered_pass.controls.earlier_window", "not_before_the_late_window")
    for key in ("random_runner", "shuffled_m"):
        _text(controls[key], f"registered_pass.controls.{key}")
    discovery = _object(registered["discovery"], "registered_pass.discovery", ("min_cell_mean_nclv", "rule"))
    _text(discovery["rule"], "registered_pass.discovery.rule")
    holm = _object(registered["holm"], "registered_pass.holm", ("alpha_familywise", "family"))
    _text(holm["family"], "registered_pass.holm.family")
    kill = _object(registered["kill_rule"], "registered_pass.kill_rule", ("alpha", "fraction_of_main", "rule"))
    _text(kill["rule"], "registered_pass.kill_rule.rule")

    seeds = _object(top["seeds"], "seeds", ("random_runner",))
    diagnostics = _object(top["diagnostics"], "diagnostics", ("min_stake_p", "rule"))
    _text(diagnostics["rule"], "diagnostics.rule")
    return Registration(
        sha256=registration_sha256(raw), tiers=tiers, ladders={tier: LADDERS[tier] for tier in tiers},
        start_month=months[0], end_month=months[1], decision_offsets_s=offsets, countries=countries,
        event_type_id=_text(population["event_type_id"], "population.event_type_id"),
        market_type=_text(population["market_type"], "population.market_type"),
        betting_type=_text(population["betting_type"], "population.betting_type"),
        number_of_winners=_int(population["number_of_winners"], "population.number_of_winners", 1, 1000),
        min_active_runners=min_active, best_back_min_h=low_h, best_back_max_h=high_h,
        min_available_p=_int(population["min_available_at_best_back_p"], "population.min_available_at_best_back_p",
                             0, 10 ** 9),
        max_lay_ticks=_int(population["max_best_lay_ticks_above_best_back"],
                           "population.max_best_lay_ticks_above_best_back", 1, len(LADDER_H)),
        commission=_rate(measurement["commission_rate"], "measurement.commission_rate"),
        price_bands=price_bands, field_bands=field_bands, late_offset_s=late_offset, late_from_offset_s=late_from,
        move_threshold=_rate(late["threshold"], "late_move.threshold"),
        max_selected_spread_ticks=_int(late["max_selected_spread_ticks"], "late_move.max_selected_spread_ticks", 1,
                                       len(LADDER_H)),
        placebo_from_offset_s=placebo_from, placebo_to_offset_s=placebo_to,
        confidence_level=_rate(uncertainty["confidence_level"], "uncertainty.confidence_level"),
        min_clusters=_int(uncertainty["min_clusters"], "uncertainty.min_clusters", 2, 10 ** 6),
        quantiles=quantiles,
        min_bets_for_statistics=_int(statistics["min_bets_for_statistics"], "statistics.min_bets_for_statistics", 2,
                                     10 ** 6),
        holm_alpha=_rate(holm["alpha_familywise"], "registered_pass.holm.alpha_familywise"),
        min_cell_bets=_int(registered["min_cell_bets"], "registered_pass.min_cell_bets", 2, 10 ** 9),
        min_cell_race_dates=_int(registered["min_cell_race_dates"], "registered_pass.min_cell_race_dates", 2,
                                 10 ** 6),
        kill_fraction=_rate(kill["fraction_of_main"], "registered_pass.kill_rule.fraction_of_main"),
        kill_alpha=_rate(kill["alpha"], "registered_pass.kill_rule.alpha"),
        min_cell_mean_nclv=_rate(discovery["min_cell_mean_nclv"], "registered_pass.discovery.min_cell_mean_nclv"),
        random_runner_seed=_int(seeds["random_runner"], "seeds.random_runner", 0, 2 ** 63 - 1),
        min_stake_p=_int(diagnostics["min_stake_p"], "diagnostics.min_stake_p", 1, 10 ** 6))


# ---------------------------------------------------------------------------------------------------------------
# Numbers and the CLASSIC ladder

def exact(value):
    """The exact Fraction of a JSON number as the file wrote it (an int, or a float by its shortest repr, as
    accounting.decimal_from_json_number reads one), or None for anything else (a bool, text, null)."""
    if type(value) is int:
        return Fraction(value)
    if type(value) is float and math.isfinite(value):
        return Fraction(Decimal(repr(value)))
    return None


def hundredths(price):
    """int hundredths of an exact price that is a whole number of hundredths, else None."""
    scaled = price * 100
    return scaled.numerator if scaled.denominator == 1 else None


def tick_index(price):
    """The CLASSIC ladder index (0 for 1.01) of an exact price on the ladder, else None."""
    scaled = hundredths(price)
    return _TICK.get(scaled) if scaled is not None else None


def tick_position(price):
    """An exact price's position on the CLASSIC ladder in ticks from 1.01: its index when on the ladder, else linear
    between the two ladder prices around it; None outside 1.01 to 1000."""
    scaled = price * 100
    if not LADDER_H[0] <= scaled <= LADDER_H[-1]:
        return None
    index = bisect_right(LADDER_H, scaled) - 1
    if LADDER_H[index] == scaled:
        return Fraction(index)
    low, high = LADDER_H[index], LADDER_H[index + 1]
    return index + (scaled - low) / (high - low)


def nclv(price, bsp, commission):
    """Contract p1's NCLV, (1 + (P - 1) * (1 - c)) / BSP - 1, exactly; every argument a Fraction, BSP above 0."""
    if bsp <= 0:
        raise ValueError("bsp_not_positive")
    return (1 + (price - 1) * (1 - commission)) / bsp - 1


# ---------------------------------------------------------------------------------------------------------------
# Ladders and the replay

def level_prefix(levels, descending):
    """[(price, size), ...] of a level-keyed ladder {level: (price, size)}: the contiguous prefix from level 0 that is
    strictly ordered by price (descending for backs, ascending for lays), as book.py's _level_prefix reads one;
    whatever follows a missing or out-of-order level is dropped."""
    out, level = [], 0
    while level in levels:
        price, size = levels[level]
        if out and (price >= out[-1][0] if descending else price <= out[-1][0]):
            break
        out.append((price, size))
        level += 1
    return out


class LadderFold:
    """The tier's back and lay ladders per runner, folded from market changes in file order by the stream cache's
    rules (module docstring); malformed holds the pt of every malformed rc entry or ladder update, nulls the pt of
    every ladder key whose value is null (which changes nothing)."""

    def __init__(self, keys):
        if keys not in LADDERS.values():
            raise ValueError("ladder_not_supported")
        self.keys = keys
        self.level_keyed = keys == LADDERS["ADVANCED"]
        self.books = {}
        self.malformed = []
        self.nulls = []

    def _parsed(self, entries):
        """[(key, price, size)] of one ladder update, or None when any entry is malformed. A size of 0 needs a
        price only when the ladder is keyed by price (it names the point to delete)."""
        width = 3 if self.level_keyed else 2
        if not isinstance(entries, list):
            return None
        parsed = []
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != width:
                return None
            level = entry[0] if self.level_keyed else None
            price, size = exact(entry[-2]), exact(entry[-1])
            if self.level_keyed and (type(level) is not int or level < 0):
                return None
            if size is None or size < 0 or (size > 0 and (price is None or price <= 1)):
                return None
            if price is None and not self.level_keyed:
                return None
            parsed.append((level if self.level_keyed else price, price, size))
        return parsed

    def change(self, pt, change):
        image = change.get("img") is True
        carried = set()
        for item in change.get("rc") or ():
            rid = _runner_id(item.get("id"))
            if rid is None:
                self.malformed.append(pt)
                continue
            carried.add(rid)
            book = self.books.setdefault(rid, ({}, {}))
            for side, key in enumerate(self.keys):
                if image:
                    book[side].clear()
                if item.get(key) is None:
                    if key in item:
                        self.nulls.append(pt)
                    continue
                parsed = self._parsed(item[key])
                if parsed is None:
                    self.malformed.append(pt)
                    continue
                ladder = book[side]
                if not parsed and not self.level_keyed:
                    ladder.clear()                  # F54: an empty price-point list empties that ladder
                for place, price, size in parsed:     # place: the price (PRO) or the level (ADVANCED)
                    if size > 0:
                        ladder[place] = (price, size)
                    elif self.level_keyed:          # F54: size 0 at level k empties level k and every deeper one
                        for level in [level for level in ladder if level >= place]:
                            del ladder[level]
                    else:
                        ladder.pop(place, None)
        if image:
            for rid, book in self.books.items():
                if rid not in carried:
                    book[0].clear()
                    book[1].clear()

    def ladders(self, selection_id):
        """(back, lay) of one runner, each [(price, size), ...] best first: every PRO price point (backs highest
        first, lays lowest first), or the ADVANCED level prefix (level_prefix); ([], []) for a runner never seen."""
        back, lay = self.books.get(selection_id, ({}, {}))
        if self.level_keyed:
            return level_prefix(back, descending=True), level_prefix(lay, descending=False)
        return [back[price] for price in sorted(back, reverse=True)], [lay[price] for price in sorted(lay)]

    def quotes(self):
        """((selection id, best back, best lay), ...) by id; each quote (price, size) as exact Fractions, or None."""
        return tuple((rid, *((ladder[0] if ladder else None) for ladder in self.ladders(rid)))
                     for rid in sorted(self.books))


@dataclass(frozen=True)
class Moment:
    """One replayed time point: t (ms), the DecisionView (None, with reason, when the index holds no view at t),
    each runner's (id, best back, best lay) at t, and whether a malformed rc entry or ladder update was published
    at or before t."""

    offset_s: int
    t: int
    view: object
    reason: object
    quotes: tuple
    ladder_malformed: bool

    def quote(self, selection_id):
        for rid, back, lay in self.quotes:
            if rid == selection_id:
                return back, lay
        return None, None

    def active(self):
        """The ids of the runners ACTIVE and listed in the view at t, sorted; () without a view."""
        if self.view is None:
            return ()
        return tuple(r.selection_id for r in self.view.runners if r.status == "ACTIVE" and r.listed is True)


@dataclass(frozen=True)
class MarketReplay:
    """moments {offset: Moment}; market, the index fold of every message up to `until`; unreadable_from, the pt from
    which the member could not be read when that happened by then (as replay._fold), else None;
    null_ladder_updates, how many of the tier's ladder keys up to `until` had a null value (LadderFold.nulls)."""

    moments: dict
    market: object
    unreadable_from: object
    null_ladder_updates: int = 0


def view_problem(record, t):
    """The code of a record with no view at t (no_view, anchor_not_known_by_decision, view_ended_before_decision,
    member_unreadable_before_decision), or None."""
    decision = record["decision_time"]
    first = decision["first_published_market_time"]
    if first is None:
        return "no_view"
    if first["pt"] > t:
        return "anchor_not_known_by_decision"
    ends = decision["view_ends"]
    if ends["pt"] is None or t >= ends["pt"]:
        return "member_unreadable_before_decision" if ends["reason"] == UNREADABLE_VIEW_END \
            else "view_ended_before_decision"
    return None


def _moment(record, market, prices, ladders, offset, t):
    reason = view_problem(record, t)
    if reason is not None:
        return Moment(offset_s=offset, t=t, view=None, reason=reason, quotes=(), ladder_malformed=False)
    view = view_from_fold(record, market, prices, offset)
    return Moment(offset_s=offset, t=t, view=view, reason=None, quotes=ladders.quotes(),
                  ladder_malformed=any(pt <= t for pt in ladders.malformed))


def replay_market(stream, record, keys, offsets, until, limits):
    """The MarketReplay of one index record from its primary member's decompressed stream (module docstring);
    ReplayProblem when the stream is not what the index says, ValueError for a record with no anchor."""
    market_id = record["market_id"]
    pending = []
    for offset in offsets:
        timing = anchor_and_decision(record, offset)
        if timing is None:
            raise ValueError("replay:no_view")
        pending.append((timing[2], offset))
    pending.sort()
    market, prices, ladders, moments = None, PriceFold(), LadderFold(keys), {}

    def capture(before):
        while pending and (before is None or pending[0][0] < before):
            t, offset = pending.pop(0)
            moments[offset] = _moment(record, market, prices, ladders, offset, t)

    unreadable_from, applied, messages = None, None, _messages(stream, limits)
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
        capture(pt)
        if pt > until:
            break
        try:
            checked = _checked_changes(message)
        except _Unreadable:
            unreadable_from = pt
            break
        for mid, change in checked:
            if mid != market_id:
                continue
            if market is None:
                market = _Market(market_id, pt)
            market.change(pt, change)
            prices.change(pt, change)
            ladders.change(pt, change)
        applied = pt
    if unreadable_from is not None and unreadable_from > until:
        unreadable_from = None
    if unreadable_from is None:
        capture(None)
    else:
        capture(unreadable_from)
        for t, offset in pending:
            if view_problem(record, t) is None:
                raise ReplayProblem(f"replay:{market_id}:unreadable_before_view_ends")
            moments[offset] = _moment(record, market, prices, ladders, offset, t)
    return MarketReplay(moments=moments, market=market, unreadable_from=unreadable_from,
                        null_ladder_updates=len(ladders.nulls))


# ---------------------------------------------------------------------------------------------------------------
# Decisions and measurement

def decision_time(record, offset_s):
    """t in ms, or None for a record with no anchor."""
    timing = anchor_and_decision(record, offset_s)
    return None if timing is None else timing[2]


def race_date(record):
    """The Europe/London calendar date of the first-published off, as ISO text."""
    return race_day_of(utc_moment(record["decision_time"]["first_published_market_time"]["value"])).isoformat()


def active_ids(record, t):
    """The ids of the runners ACTIVE and listed at t in the index's decision_time, sorted."""
    return tuple(sorted(item["id"] for item in record["decision_time"]["runners"] if item["first_pt"] <= t
                        and _as_of(item.get("status"), t) == "ACTIVE" and _as_of(item.get("listed"), t) is True))


def _revised(decision, t):
    """Whether the marketTime was published with another value at or before t (an image's null reset is not)."""
    timeline = decision["definition"].get("marketTime", [])
    first = timeline[0][1] if timeline else None
    return any(pt <= t and value is not None and value != first for pt, value in timeline)


def market_code(reg, record, offset_s):
    """The first market code from the record alone (plan_not_registered to too_few_active_runners), or None when
    the decision at offset_s reaches the book predicates."""
    if record["provenance"]["plan"] not in reg.tiers:
        return "plan_not_registered"
    t = decision_time(record, offset_s)
    if t is None:
        return "no_view"
    decision = record["decision_time"]
    month = utc_month(decision["first_published_market_time"]["value"])
    if month is None or not reg.start_month <= month <= reg.end_month:
        return "month_not_registered"
    problem = view_problem(record, t)
    if problem is not None:
        return problem
    if _revised(decision, t):
        return "schedule_revised_before_decision"

    def field(key):
        return _as_of(decision["definition"].get(key), t)

    country = field("countryCode")
    if not isinstance(country, str) or country not in reg.countries:
        return "country_not_allowed"
    if type(field("eventTypeId")) is not str or field("eventTypeId") != reg.event_type_id:
        return "event_type_not_allowed"
    if type(field("marketType")) is not str or field("marketType") != reg.market_type:
        return "market_type_not_allowed"
    if type(field("bettingType")) is not str or field("bettingType") != reg.betting_type:
        return "betting_type_not_odds"
    if type(field("numberOfWinners")) is not int or field("numberOfWinners") != reg.number_of_winners:
        return "number_of_winners_not_1"
    if len(active_ids(record, t)) < reg.min_active_runners:
        return "too_few_active_runners"
    return None


@dataclass(frozen=True)
class Selection:
    """The runner a decision selected, with its best back and best lay ((price, size) as exact Fractions)."""

    selection_id: int
    back: tuple
    lay: tuple


def favourite(active, moment):
    """(code, None) or (None, (id, best back, best lay)): the only ACTIVE listed runner (active) with the lowest best
    back at t, after ladder_malformed, no_priced_active_runner and favourite_tie."""
    if moment.ladder_malformed:
        return "ladder_malformed", None
    priced = [(sid, *moment.quote(sid)) for sid in active if moment.quote(sid)[0] is not None]
    if not priced:
        return "no_priced_active_runner", None
    lowest = min(back[0] for _, back, _ in priced)
    favourites = [item for item in priced if item[1][0] == lowest]
    if len(favourites) != 1:
        return "favourite_tie", None
    return None, favourites[0]


def select(reg, active, moment):
    """(code, None) or (None, Selection): Pilot 1's selection predicates in pilot.py's order over the ACTIVE listed
    runners at t (active) and the quotes at t, after ladder_malformed."""
    code, found = favourite(active, moment)
    if code is not None:
        return code, None
    sid, back, lay = found
    back_h = hundredths(back[0])
    if back_h is None or back_h not in _TICK:
        return "best_back_off_ladder", None
    if not reg.best_back_min_h <= back_h <= reg.best_back_max_h:
        return "best_back_out_of_range", None
    if back[1] * 100 < reg.min_available_p:
        return "depth_below_min", None
    if lay is None:
        return "best_lay_missing", None
    if lay[0] <= back[0]:
        return "book_crossed", None
    lay_h = hundredths(lay[0])
    if lay_h is None or lay_h not in _TICK:
        return "best_lay_off_ladder", None
    if not 1 <= _TICK[lay_h] - _TICK[back_h] <= reg.max_lay_ticks:
        return "spread_too_wide", None
    return None, Selection(selection_id=sid, back=back, lay=lay)


def final_runners(record):
    """{id: final runner entry} of the runners listed at the end of the record's evaluator_only_outcome."""
    return {item["id"]: item for item in record["evaluator_only_outcome"]["final_runners"] if item["listed"] is True}


def valid_bsp(record, selection_id):
    """The selected runner's BSP as an exact Fraction when it is valid (registration measurement.bsp_valid_if),
    else None."""
    outcome = record["evaluator_only_outcome"]
    reconciled = (outcome["outcome_definition"].get("bspReconciled") or [[None, None]])[-1][1]
    final = final_runners(record).get(selection_id)
    if outcome["final_status"] != "CLOSED" or reconciled is not True or final is None:
        return None
    if final["status"] not in RESULT_STATUSES:
        return None
    value = exact(final["bsp"])
    return value if value is not None and value > 1 else None


def measure(record, t, selection_id, active):
    """(class, BSP or None) of one issued decision (registration measurement.classes_rule)."""
    removed = _removed_after(record, t, set(active), set(final_runners(record)))
    if removed is None:
        return "outcome_unknown", None
    if selection_id in removed:
        return "void_own_runner_removed", None
    if removed:
        return "post_decision_removal", None
    if record["evaluator_only_outcome"]["results_revised_after_close"] is True:
        return "result_revised_after_close", None
    bsp = valid_bsp(record, selection_id)
    return ("missing_bsp", None) if bsp is None else (ELIGIBLE, bsp)


def band(bands, value):
    """The name of the band holding value (inclusive bounds; None as high is unbounded), or None."""
    for name, low, high in bands:
        if low <= value and (high is None or value <= high):
            return name
    return None


# ---------------------------------------------------------------------------------------------------------------
# Late move

def field_share(reg, moment, selection_id):
    """(q, None) or (None, undefined reason): the runner's field-normalised midpoint share at one Moment
    (registration late_move.q and late_move.undefined), q an exact Fraction."""
    if moment.view is None:
        return None, "no_view_at_time"
    if moment.ladder_malformed:
        return None, "ladder_malformed"
    active = moment.active()
    if selection_id not in active:
        return None, "runner_not_active"
    inverse = {}
    for sid in active:
        back, lay = moment.quote(sid)
        if back is None or lay is None or not back[0] < lay[0]:
            return None, "runner_not_two_sided" if sid == selection_id else "other_runner_not_two_sided"
        inverse[sid] = 2 / (back[0] + lay[0])
    back, lay = moment.quote(selection_id)
    low, high = tick_index(back[0]), tick_index(lay[0])
    if low is None or high is None:
        return None, "runner_off_ladder"
    if high - low > reg.max_selected_spread_ticks:
        return None, "runner_spread_too_wide"
    return inverse[selection_id] / sum(inverse.values()), None


def log_move(later, earlier):
    """M = ln(later / earlier) of two exact shares, as a float."""
    return math.log(later / earlier)


def late_move_cell(reg, move):
    """STEAM when M >= +threshold, DRIFT when M <= -threshold, else FLAT."""
    threshold = float(reg.move_threshold)
    if move >= threshold:
        return STEAM
    if move <= -threshold:
        return DRIFT
    return FLAT


def window_problem(replay, start, end):
    """The first late-move exclusion for the window (start, end] in ms, or None: window_unknown when the member
    could not be read through end; removal_in_window when a runner became REMOVED or REMOVED_VACANT in it;
    suspension_in_window when the status published by start is anything but OPEN, or any status published in it
    (an image's null reset included) is. A market not yet published at start has no status then: its view decides
    (field_share's no_view_at_time)."""
    if replay.unreadable_from is not None and replay.unreadable_from <= end:
        return "window_unknown"
    market = replay.market
    if any(start < item["pt"] <= end and item["status"] in REMOVED_STATUSES for item in market.removals()):
        return "removal_in_window"
    statuses = market.fields.get("status", [])
    if _as_of(statuses, start) not in (None, "OPEN") or any(start < pt <= end and value != "OPEN"
                                                             for pt, value in statuses):
        return "suspension_in_window"
    return None


# ---------------------------------------------------------------------------------------------------------------
# Statistics

def sample_sd(values):
    """The sample SD (n - 1) of floats, or None below two values."""
    values = list(values)
    if len(values) < 2:
        return None
    mean = math.fsum(values) / len(values)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1))


def nearest_rank(values, quantiles):
    """{q: value ceil(q * n) of the sorted values, counted from 1} for each exact q in (0, 1]."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no_values")
    return {q: ordered[max(math.ceil(q * len(ordered)), 1) - 1] for q in quantiles}


def _betacf(a, b, x):
    tiny, eps = 1e-300, 1e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (tiny if abs(d) < tiny else d)
    h = d
    for m in range(1, 301):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    raise ArithmeticError("betainc_not_converged")


def betainc_reg(a, b, x):
    """The regularised incomplete beta I_x(a, b) (pilot.py's routine)."""
    if not (a > 0 and b > 0):
        raise ValueError("betainc_domain")
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t, df):
    if type(df) is not int or df < 1:
        raise ValueError("df_invalid")
    tail = 0.5 * betainc_reg(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t >= 0 else tail


def student_t_quantile(p, df):
    """The p quantile of Student's t, 0.5 < p < 1, by bisection (pilot.py's routine)."""
    if not (isinstance(p, float) and 0.5 < p < 1.0):
        raise ValueError("quantile_p_invalid")
    lo, hi = 0.0, 1.0
    while student_t_cdf(hi, df) < p:
        hi *= 2.0
        if hi > 1e300:
            raise ArithmeticError("quantile_not_bracketed")
    for _ in range(400):
        if hi - lo <= 1e-12:
            break
        mid = (lo + hi) / 2.0
        if mid <= lo or mid >= hi:
            break
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def cr1_mean(rows, level, min_clusters):
    """CR1 over [(race date, float x)] (registration uncertainty.rule): {bets, race_dates, mean, se, df,
    t_critical, ci_low, ci_high, reason}; reason too_few_clusters or se_undefined leaves the interval None."""
    n = len(rows)
    groups = {}
    for day, x in rows:
        groups.setdefault(day, []).append(x)
    g = len(groups)
    result = {"bets": n, "race_dates": g, "mean": None, "se": None, "df": None, "t_critical": None,
              "ci_low": None, "ci_high": None, "reason": None}
    if n == 0:
        return dict(result, reason="no_bets")
    mean = math.fsum(x for _, x in rows) / n
    result["mean"] = mean
    if g < min_clusters:
        return dict(result, reason="too_few_clusters")
    se = math.sqrt(g / (g - 1) * math.fsum(math.fsum(x - mean for x in xs) ** 2 for xs in groups.values())) / n
    if se == 0:
        return dict(result, se=se, reason="se_undefined")
    t = student_t_quantile(float((1 + level) / 2), g - 1)
    return dict(result, se=se, df=g - 1, t_critical=t, ci_low=mean - t * se, ci_high=mean + t * se)


def cr1_contrast(first, second, min_bets, min_race_dates):
    """The difference of two groups' mean x over [(race date, x)] rows, two-sided, with the CR1 variance of the
    difference clustered by race date and t(G - 1) (registration registered_pass.contrasts_rule): {estimate, se,
    df, t, p_two_sided, bets, race_dates, reason}; reason underpowered (not tested) or se_undefined."""
    sizes = [len(first), len(second)]
    dates = [len({day for day, _ in rows}) for rows in (first, second)]
    result = {"estimate": None, "se": None, "df": None, "t": None, "p_two_sided": None, "bets": sizes,
              "race_dates": dates, "reason": None}
    if min(sizes) < min_bets or min(dates) < min_race_dates or min(sizes) == 0:
        return dict(result, reason="underpowered")
    means = [math.fsum(x for _, x in rows) / len(rows) for rows in (first, second)]
    influence = {}
    for sign, rows, mean in ((1, first, means[0]), (-1, second, means[1])):
        for day, x in rows:
            influence.setdefault(day, []).append(sign * (x - mean) / len(rows))
    g = len(influence)
    estimate = means[0] - means[1]
    if g < 2:
        return dict(result, estimate=estimate, reason="se_undefined")
    se = math.sqrt(g / (g - 1) * math.fsum(math.fsum(values) ** 2 for values in influence.values()))
    if se == 0:
        return dict(result, estimate=estimate, se=se, reason="se_undefined")
    t = estimate / se
    return dict(result, estimate=estimate, se=se, df=g - 1, t=t, p_two_sided=2.0 * (1.0 - student_t_cdf(abs(t), g - 1)))


def holm(pvalues, alpha, family_size):
    """{name: (adjusted p or None, rejected)} by Holm's step-down at familywise alpha over family_size hypotheses;
    a name whose p is None was not tested and is never rejected (it still counts in family_size)."""
    tested = sorted((p, name) for name, p in pvalues.items() if p is not None)
    result = {name: (None, False) for name, p in pvalues.items() if p is None}
    running, rejecting = 0.0, True
    for rank, (p, name) in enumerate(tested):
        factor = family_size - rank
        running = max(running, min(1.0, factor * p))
        rejecting = rejecting and p <= float(alpha) / factor
        result[name] = (running, rejecting)
    return result


def permuted_contrast(rows, first, second, min_bets, min_race_dates):
    """The expected first-minus-second difference of mean x over every permutation of the cells among the rows of
    each race date, exactly (no draw), over [(race date, cell, x)] rows (registration registered_pass.controls
    .shuffled_m): sum over race dates d of (n_d,first / n_first - n_d,second / n_second) * mean_d, mean_d the mean x
    of every row of d (a permutation keeps each date's cell counts, and puts each cell on each row of d with
    probability n_d,cell / n_d). It is the difference of two weighted means of x, so its CR1 variance is
    cr1_contrast's with those weights: G / (G - 1) * sum over d of (a_d * (mean_d - m_a) - b_d * (mean_d - m_b))^2,
    a_d = n_d,first / n_first, b_d = n_d,second / n_second, m_a and m_b the weighted means, G the race dates holding a
    row of either cell, with t(G - 1). The same keys, gates and reasons as cr1_contrast."""
    sizes = [sum(1 for _, cell, _ in rows if cell == which) for which in (first, second)]
    dates = [len({day for day, cell, _ in rows if cell == which}) for which in (first, second)]
    result = {"estimate": None, "se": None, "df": None, "t": None, "p_two_sided": None, "bets": sizes,
              "race_dates": dates, "reason": None}
    if min(sizes) < min_bets or min(dates) < min_race_dates or min(sizes) == 0:
        return dict(result, reason="underpowered")
    days = {}
    for day, cell, x in rows:
        days.setdefault(day, []).append((cell, x))
    weights = []
    for items in days.values():
        a = sum(1 for cell, _ in items if cell == first) / sizes[0]
        b = sum(1 for cell, _ in items if cell == second) / sizes[1]
        if a or b:
            weights.append((a, b, math.fsum(x for _, x in items) / len(items)))
    m_a = math.fsum(a * mean for a, _, mean in weights)
    m_b = math.fsum(b * mean for _, b, mean in weights)
    estimate, g = m_a - m_b, len(weights)
    if g < 2:
        return dict(result, estimate=estimate, reason="se_undefined")
    se = math.sqrt(g / (g - 1) * math.fsum((a * (mean - m_a) - b * (mean - m_b)) ** 2 for a, b, mean in weights))
    if se == 0:
        return dict(result, estimate=estimate, se=se, reason="se_undefined")
    t = estimate / se
    return dict(result, estimate=estimate, se=se, df=g - 1, t=t, p_two_sided=2.0 * (1.0 - student_t_cdf(abs(t), g - 1)))


def drawn(seed, label, count):
    """One uniform index in [0, count) from metrics.HashStream(seed, label)."""
    return HashStream(seed, label).below(count)


def kill_and_flags(reg, main, decisions, controls, levels):
    """(kill, flags) by the registered kill and discovery rules (registration registered_pass.kill_rule and
    .discovery). main: {contrast: cr1_contrast result over the late-move population}; decisions: {contrast: (Holm
    adjusted p, rejected)}; controls: {control: ({contrast: the control's result}, {contrast: the main contrast over
    the bets that control is computed on})}; levels: {cell: the cell's mean NCLV, or None when not reported}.

    For each contrast and control, the control blocks the flag (BLOCKS, the first that applies) when it has no
    estimate (underpowered), when the main contrast over the same bets has none, or when its absolute estimate is at
    least kill_fraction times that main estimate's and its own two-sided p is below kill_alpha or undefined
    (se_undefined: its noise cannot be judged). A STEAM or DRIFT cell is flagged only when its mean NCLV is at least
    min_cell_mean_nclv, its contrast with FLAT is positive, that contrast is rejected under Holm, and no control
    blocks it. Pure: no file, clock or draw."""
    fraction, alpha = float(reg.kill_fraction), float(reg.kill_alpha)
    kill, flags = {}, {}
    for name, cell, _ in CONTRASTS:
        kill[name] = {}
        for control in CONTROLS:
            result, same = controls[control][0][name], controls[control][1][name]
            p = result["p_two_sided"]
            if result["estimate"] is None:
                why = BLOCKS[0]
            elif same["estimate"] is None:
                why = BLOCKS[1]
            elif abs(result["estimate"]) >= fraction * abs(same["estimate"]) and (p is None or p < alpha):
                why = BLOCKS[2]
            else:
                why = None
            kill[name][control] = {"control_estimate": result["estimate"], "control_p_two_sided": p,
                                   "main_estimate_same_bets": same["estimate"], "blocks": why is not None, "why": why}
        level, estimate = levels.get(cell), main[name]["estimate"]
        checks = {"cell_nclv_at_least_min": level is not None and level >= float(reg.min_cell_mean_nclv),
                  "contrast_positive": estimate is not None and estimate > 0,
                  "contrast_rejected_under_holm": decisions[name][1] is True,
                  "no_control_blocks": not any(item["blocks"] for item in kill[name].values())}
        flags[cell] = dict(checks, tradable_flag=all(checks.values()))
    return kill, flags
