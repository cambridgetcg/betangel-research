"""Registered measurement early-price-v1: early last traded prices against BSP, on the BASIC files (pure).

It asks two things of GB and IE horse-racing WIN markets on the BASIC files of 2023 (explore) and 2024 (test): is
the last traded price (LTP) at fixed times before the off a fair guide to the Betfair Starting Price (BSP), and do
earlier price moves keep going or come back by 10 minutes before the off (docs/research-loop.md). The score is
x = LTP / BSP - 1 per runner. An LTP is not a price anyone could have taken, so this can flag or kill an idea and
never show an edge. Race results are never used. It is not a backtest contract and changes no existing source
file, but as a module of this package it is in the engine certificate's source descriptor, so adding it changed
the certificate key again. Nothing here opens a file, reads a clock or uses the random module.

Two files decide everything: the registration (research/studies/early-price-v1.json) and the split file it names
(research/partitions-v2.json, parsed by partition.parse_split).

Registration (parse_registration): the exact keys at every level, every value of its type and domain, the file in
its one byte form, and every list of codes, classes, cells, reasons and labels equal to this module's own, in
order. It also refuses: times that are not sorted and unique; a fresh_s that is not one positive number per time;
change windows [t - fresh_s, t] that overlap; an entry fresh_s that is not below the gap from the signal's end to
the entry; starts that are not before the end, or an end not before the entry, or any of them not among the times;
bands that are not contiguous or reuse a name; half-years that do not cover the test label's dates exactly; a
label the split refuses or of the wrong role (resting._split); cites that are not the split's exposure ids; a
family size that is not the number of sides; a selection ceiling that is not the underpowered line. The answer is
one frozen Plan. Its attribute names are the ones the shared helpers of resting.py read (min_values,
confidence_level, commission, bsp_step and the rest), with the same meaning.

Codes (record_code, then assess). Per record and time, one market code, the first that applies, else viewed. From
the index record alone: movement.market_code's first thirteen codes (through resting._Gate, at least
min_active_runners runners ACTIVE and listed at t); partition_not_registered when the race date's label is not one
of the two the registration names; bsp_market_false when bspMarket as of t is exactly false (a missing value
passes and is reported); market_not_open when the status as of t is not OPEN or inPlay as of t is true. A record
with one of these codes at every time is never opened. From the file: price_change_malformed when the view at t
counts an rc entry whose runner id did not parse. Per runner ACTIVE and listed at a viewed time, one runner code,
the first that applies, else priced: no_ltp, ltp_out_of_bands, ltp_never_changed, ltp_before_last_removal,
ltp_stale (price_at).

The fold (fold): movement.replay_market's loop without the ladder fold. It folds the market's messages in file
order with the index's reader and fold (market_index._messages, _checked_changes, _Market) and replay.PriceFold,
captures a movement.Moment at each asked time with replay.view_from_fold (which refuses, ReplayProblem, unless
the fold equals the index record at t) and stops at the first message published after `until`; nothing later is
applied. It returns the movement.MarketReplay, so movement.window_problem works on it, and the PriceFold, whose
ltp timelines the price rules read.

Prices. The LTP at t is the view's: the value of the runner's last ltp entry published at or before t, the batch
at t included, read exactly (movement.exact); usable only as a finite number above 0 (replay.usable_ltp). Else it
is no_ltp, of one of three kinds (NO_LTP_KINDS): never sent, reset by an image and not sent again, or not a usable
number. A timeline entry is not always a change of price: an image that leaves a price out adds a null, and 5 then
5.0 are two entries. So a change is a usable entry whose exact value differs from the usable entry before it
(changes); the first usable entry is not one. changed_pt is the pt of the last change at or before t, and the age
is t - changed_pt in ms. A price counts at t when it lies in a band (Plan.band: low included, high not), has a
change, changed after the last removal published at or before t (a withdrawal re-bases every price) and is at
most fresh_s old at t (both ends included).

Cells. A level cell is one time and one band; its members are the runners priced there. A move cell is one start,
one of DRIFT, FLAT and STEAM, and one band; its members are the level cell's members at the entry time whose move
from that start is defined (move_of). The move is undefined for the first of UNDEFINED that applies; a suspension in
the window does not undefine it but flags it. Else M = ln(LTP at the end / LTP at the start) (movement.log_move):
DRIFT when M >= +threshold, STEAM when M <= -threshold, else FLAT. Price up is DRIFT, the opposite of
movement.late_move_cell, whose move is in probability share. k is the number of changes after the end price's
changed_pt, up to and including the entry's (1, 2, or 3 and more): published prices take turns between the two
sides of the gap, so k says how far apart the signal and the entry really are.

Classes, from facts after t (movement.measure, its eligible renamed scored): outcome_unknown,
void_own_runner_removed, post_decision_removal, result_revised_after_close, missing_bsp, scored. A class never
enters a cell's membership; codes, cells, flags and k read only what was published at or before their time.

assess gives, per record, each time's code and one id-free Value row per runner ACTIVE and listed at a viewed
time: no market id, selection id, name or time of day. A later step scores the rows (x through resting.score) and
builds the tables.

What this does not do: it reads nothing after the latest time that reached the view, never re-prices a removal,
scores nothing, selects nothing and builds no table. Standard library only; it imports movement, replay,
market_index, partition, resting and lp and, through them, the modules they import. Nothing here is evidence of
profitability.
"""

from collections import namedtuple
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

from .lp import BACK, LAY, _rules
from .market_index import _as_of, _checked_changes, _Market, _messages, _Unreadable, loads_strict
from .movement import (ANCHOR, CLUSTER, DATE_RE, ELIGIBLE, MARKET_CODES, MAX_REGISTRATION_BYTES, MONTH_RE,
                       QUANTILE_METHOD, REMOVED_STATUSES, SCHEMA, UNCERTAINTY_METHOD, MarketReplay, Moment, _codes,
                       _fail, _int, _object, _offset, _rate, _text, _texts, active_ids, decision_time, exact, log_move,
                       market_code, measure, race_date, registration_sha256, view_problem, window_problem)
from .movement import CLASSES as MEASURE_CLASSES
from .partition import file_bytes
from .replay import PriceFold, ReplayProblem, usable_ltp, view_from_fold
from .resting import BSP_ENDS, BSP_STEP, CLAIM_END, SCORED, _Gate, _level, _named, _split

STUDY_ID, LINEAGE = "early-price-v1", "early-price"
TIERS = ("BASIC",)
PRICE_KEY = "ltp"
SIDES = (BACK, LAY)
VIEWED = "viewed"
PARTITION_NOT_REGISTERED, BSP_MARKET_FALSE, MARKET_NOT_OPEN = ("partition_not_registered", "bsp_market_false",
                                                               "market_not_open")
PRICE_CHANGE_MALFORMED = "price_change_malformed"
# The codes the index record alone can give (movement.market_code's first thirteen, then this study's three), and
# every market code in evaluation order: the last one needs the file. viewed is not in it.
RECORD_CODES = MARKET_CODES[:13] + (PARTITION_NOT_REGISTERED, BSP_MARKET_FALSE, MARKET_NOT_OPEN)
CODES = RECORD_CODES + (PRICE_CHANGE_MALFORMED,)
NO_LTP, OUT_OF_BANDS, NEVER_CHANGED, BEFORE_REMOVAL, STALE = RUNNER_CODES = (
    "no_ltp", "ltp_out_of_bands", "ltp_never_changed", "ltp_before_last_removal", "ltp_stale")
PRICED = "priced"
# Why a runner is no_ltp at t: no entry at or before t; the last entry is an image's null; not a number above 0.
NEVER_SENT, RESET, NOT_USABLE = NO_LTP_KINDS = ("never_sent", "reset_not_sent_again", "not_a_usable_number")
CLASSES = MEASURE_CLASSES[:5] + (SCORED,)
DRIFT, FLAT, STEAM = MOVES = ("DRIFT", "FLAT", "STEAM")
WINDOW_UNKNOWN, REMOVAL_IN_WINDOW, SUSPENSION = "window_unknown", "removal_in_window", "suspension_in_window"
UNDEFINED = (WINDOW_UNKNOWN, REMOVAL_IN_WINDOW, "no_view_at_start", "no_start_price", "start_price_stale",
             "no_view_at_end", "no_end_price", "end_price_stale")
FLAGS = (SUSPENSION,)
CHANGES_BETWEEN = ("1", "2", "3_or_more")
VERDICTS = ("kill_free_fill", "underpowered", "cost_clearing_candidate", "signal_candidate", "unresolved")
HOLM = "holm"
SELECT_MEASURE, SELECT_RANK, SELECT_TIE = "x", "mean_over_cr1_se", "first_registered_cell"
SELECT_INPUTS = ("explore_rows", "test_counts", "bsp_switch")
SWITCH_ROWS = "level_cells_at_entry_both_labels"
PRIMARIES_ONLY = "primaries_only"
HALVES = ("H1", "H2")
TOP_KEYS = ("blind_pass", "cells", "data", "decided_by", "diagnostics", "doc", "exposure", "family", "id", "lineage",
            "measurement", "not_measured", "population", "registered_on", "registered_pass", "schema", "selection",
            "statistics", "status", "summary", "timing", "uncertainty", "verdicts")
DATA_KEYS = ("duplicates", "end_month", "files", "halves", "month", "partitions", "prices", "provider", "source",
             "split", "start_month", "tiers")

LevelCell = namedtuple("LevelCell", ("at_s", "band"))
MoveCell = namedtuple("MoveCell", ("start_s", "move", "band"))
# What the price rules say of one runner at one time (price_at): its runner code (or priced), the no_ltp kind, the
# band, the age in ms, the exact LTP and changed_pt (each None when it does not apply).
Price = namedtuple("Price", ("code", "no_ltp", "band", "age_ms", "ltp", "changed_pt"))
# One start's move of an entry member: its cell (DRIFT, FLAT, STEAM) or the reason it has none, whether a suspension
# in the window flagged it, and k (changes after the end price's last change up to the entry's; None when undefined).
Move = namedtuple("Move", ("start_s", "cell", "reason", "flagged", "k"))
# One runner ACTIVE and listed at one viewed time. No market id, selection id, name or time of day. ltp is the
# exact LTP when usable; klass and bsp only for a priced runner (bsp only when scored); moves one Move per start for
# a member at the entry time, else ().
Value = namedtuple("Value", ("at_s", "code", "no_ltp", "band", "age_ms", "ltp", "label", "half", "race_date",
                             "klass", "bsp", "moves"))


# ---------------------------------------------------------------------------------------------------------------
# Registration

@dataclass(frozen=True)
class Plan:
    """What one registration and its split file say (parse_registration). sha256 is the registration's; split the
    partition.Split; halves ((name, first, last), ...) of the test label; times_s and fresh_s one entry per time;
    bands ((name, low, high), ...) as exact prices, low included and high not. Levels, rates and thresholds are
    exact Fractions. label_note and exposure_note are printed with every label, label_texts with each one."""

    sha256: str
    study: str
    lineage: str
    split: object
    explore_label: str
    test_label: str
    halves: tuple
    tiers: tuple
    start_month: str
    end_month: str
    countries: tuple
    event_type_id: str
    market_type: str
    betting_type: str
    number_of_winners: int
    min_active_runners: int
    codes: tuple
    runner_codes: tuple
    times_s: tuple
    fresh_s: tuple
    bands: tuple
    starts_s: tuple
    end_s: int
    entry_s: int
    threshold: Fraction
    sides: tuple
    commission: Fraction
    classes: tuple
    bsp_step: Fraction
    bsp_whole_share: Fraction
    family_m: int
    alpha: Fraction
    max_detectable: Fraction
    min_values: int
    min_tested_values: int
    min_tested_race_dates: int
    quantiles: tuple
    power: Fraction
    detectable_level: Fraction
    min_clusters: int
    confidence_level: Fraction
    kill_level: Fraction
    lower_bound_level: Fraction
    underpowered_above: Fraction
    half_year_min_values: int
    half_gap_allowance: dict
    label_note: str
    label_texts: dict
    exposure_note: str
    test_label_move_means: str

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

    @property
    def level_cells(self):
        """Every LevelCell: time by time, band by band."""
        return tuple(LevelCell(at_s, name) for at_s in self.times_s for name, _, _ in self.bands)

    @property
    def move_cells(self):
        """Every MoveCell in the registered order: start by start, then DRIFT, FLAT, STEAM, then band by band."""
        return tuple(MoveCell(start, move, name) for start in self.starts_s for move in MOVES
                     for name, _, _ in self.bands)

    def fresh(self, at_s):
        """fresh_s of one registered time, in seconds."""
        return self.fresh_s[self.times_s.index(at_s)]

    def band(self, price):
        """The name of the band holding an exact price (low included, high not), or None."""
        return next((name for name, low, high in self.bands if low <= price < high), None)

    def half(self, day):
        """The registration's half-year of a race date (ISO text), or None outside both."""
        return next((name for name, first, last in self.halves if first <= day <= last), None)


def _loaded(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_REGISTRATION_BYTES:
        _fail("file", "not_bytes_or_too_large")
    try:
        value = loads_strict(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        _fail("file", "not_json")
    return _object(value, "top", TOP_KEYS)


def split_named(raw):
    """(split file name, sha256) a registration's raw bytes name, so that a runner knows which file to read before
    parse_registration; RegistrationError otherwise."""
    return _named(_object(_loaded(raw)["data"], "data", DATA_KEYS)["split"], "data.split")


def _day(value, place):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        _fail(place, "not_a_date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(place, "not_a_date")


def _halves(value, split, test):
    """((name, first, last), ...) of data.halves, after checking they cover the test label's range exactly."""
    halves = _object(value, "data.halves", HALVES + ("rule",))
    _text(halves["rule"], "data.halves.rule")
    spans = []
    for name in HALVES:
        item = _object(halves[name], f"data.halves.{name}", ("first", "last"))
        first, last = _day(item["first"], f"data.halves.{name}"), _day(item["last"], f"data.halves.{name}")
        if last < first:
            _fail(f"data.halves.{name}", "ends_before_it_starts")
        spans.append((name, first, last))
    held = [item for item in split.ranges if test in item.labels]
    if len(held) != 1 or spans[0][1].isoformat() != held[0].first or spans[1][2].isoformat() != held[0].last \
            or spans[1][1] != spans[0][2] + timedelta(days=1):
        _fail("data.halves", "not_the_test_labels_dates")
    return tuple((name, first.isoformat(), last.isoformat()) for name, first, last in spans)


def _times(timing):
    """(times_s, fresh_s) after the checks of the module docstring."""
    times = tuple(_offset(item, "timing.times_s") for item in _list(timing["times_s"], "timing.times_s"))
    if list(times) != sorted(set(times)):
        _fail("timing.times_s", "not_sorted_and_unique")
    fresh = tuple(_int(item, "timing.fresh_s", 1, 86400) for item in _list(timing["fresh_s"], "timing.fresh_s"))
    if len(fresh) != len(times):
        _fail("timing.fresh_s", "not_one_per_time")
    return times, fresh


def _windows(times, fresh):
    """Refuses change windows [t - fresh_s, t] that overlap, ends included (checked after the move times, so that
    the entry's own check, which this one implies, can refuse first)."""
    if any(earlier >= later - window for earlier, later, window in zip(times, times[1:], fresh[1:])):
        _fail("timing.fresh_s", "windows_overlap")


def _list(value, place):
    if not isinstance(value, list) or not value:
        _fail(place, "not_a_list")
    return value


def _bands(value):
    """((name, low, high), ...) of cells.bands_h, prices in hundredths made exact, after checking them."""
    bands, previous = [], None
    for item in _list(value, "cells.bands_h"):
        if not (isinstance(item, list) and len(item) == 3 and isinstance(item[0], str) and item[0].strip()
                and type(item[1]) is int and type(item[2]) is int and 101 <= item[1] < item[2] <= 100000):
            _fail("cells.bands_h", "malformed_band")
        if previous is not None and item[1] != previous:
            _fail("cells.bands_h", "not_contiguous")
        previous = item[2]
        bands.append((item[0], Fraction(item[1], 100), Fraction(item[2], 100)))
    if len({name for name, _, _ in bands}) != len(bands):
        _fail("cells.bands_h", "name_used_twice")
    return tuple(bands)


def _moves(move, times, fresh):
    """(starts_s, end_s, entry_s) of cells.move, after the checks of the module docstring."""
    starts = tuple(_offset(item, "cells.move.starts_s") for item in _list(move["starts_s"], "cells.move.starts_s"))
    end, entry = _offset(move["end_s"], "cells.move.end_s"), _offset(move["entry_s"], "cells.move.entry_s")
    if list(starts) != sorted(set(starts)) or starts[-1] >= end or end >= entry:
        _fail("cells.move", "not_starts_then_end_then_entry")
    if any(item not in times for item in starts + (end, entry)):
        _fail("cells.move", "time_not_registered")
    if fresh[times.index(entry)] >= entry - end:
        _fail("cells.move", "entry_fresh_not_below_the_gap")
    return starts, end, entry


def parse_registration(raw, split_raw):
    """The Plan of one registration file's raw bytes and the raw bytes of the split file it names, after every
    check in the module docstring; RegistrationError otherwise."""
    top = _loaded(raw)
    _text(top["schema"], "schema", SCHEMA)
    _text(top["id"], "id", STUDY_ID)
    _text(top["lineage"], "lineage", LINEAGE)
    _text(top["status"], "status", "registered")
    if not isinstance(top["registered_on"], str) or not DATE_RE.fullmatch(top["registered_on"]):
        _fail("registered_on", "not_a_date")
    for key in ("decided_by", "doc", "summary"):
        _text(top[key], key)
    _texts(top["not_measured"], "not_measured")
    blind = _object(top["blind_pass"], "blind_pass", ("releases", "rule", "withholds"))
    for key in ("releases", "withholds"):
        _texts(blind[key], f"blind_pass.{key}")
    _text(blind["rule"], "blind_pass.rule")
    registered = _object(top["registered_pass"], "registered_pass", ("releases", "rule", "test_label_move_means"))
    _texts(registered["releases"], "registered_pass.releases")
    _text(registered["rule"], "registered_pass.rule")
    _text(registered["test_label_move_means"], "registered_pass.test_label_move_means", PRIMARIES_ONLY)
    diagnostics = _object(top["diagnostics"], "diagnostics", ("no_ltp_kinds", "rule"))
    _codes(diagnostics["no_ltp_kinds"], "diagnostics.no_ltp_kinds", NO_LTP_KINDS)
    _text(diagnostics["rule"], "diagnostics.rule")

    data = _object(top["data"], "data", DATA_KEYS)
    _text(data["provider"], "data.provider", "betfair_historical_data")
    for key in ("duplicates", "files", "month", "source"):
        _text(data[key], f"data.{key}")
    if _texts(data["tiers"], "data.tiers") != TIERS:
        _fail("data.tiers", "not_supported")
    prices = _object(data["prices"], "data.prices", ("key", "rule"))
    _text(prices["key"], "data.prices.key", PRICE_KEY)
    _text(prices["rule"], "data.prices.rule")
    months = (data["start_month"], data["end_month"])
    if not all(isinstance(month, str) and MONTH_RE.fullmatch(month) for month in months) or months[0] > months[1]:
        _fail("data.months", "malformed")
    split, explore, test = _split(data, _named(data["split"], "data.split"), split_raw)
    halves = _halves(data["halves"], split, test)

    timing = _object(top["timing"], "timing", ("anchor", "change_rule", "fresh_s", "rule", "times_s"))
    _text(timing["anchor"], "timing.anchor", ANCHOR)
    for key in ("change_rule", "rule"):
        _text(timing[key], f"timing.{key}")
    times, fresh = _times(timing)

    population = _object(top["population"], "population", (
        "betting_type", "bsp_market", "codes", "codes_rule", "countries", "event_type_id", "market_type",
        "min_active_runners", "number_of_winners", "runner_codes", "source"))
    for key in ("betting_type", "codes_rule", "event_type_id", "market_type", "source"):
        _text(population[key], f"population.{key}")
    if population["bsp_market"] is not True:
        _fail("population.bsp_market", "not_supported")
    countries = _texts(population["countries"], "population.countries")
    codes = _codes(population["codes"], "population.codes", CODES)
    runner_codes = _codes(population["runner_codes"], "population.runner_codes", RUNNER_CODES)

    cells = _object(top["cells"], "cells", ("bands_h", "level", "move"))
    bands = _bands(cells["bands_h"])
    level = _object(cells["level"], "cells.level", ("count", "rule", "times_s"))
    _text(level["rule"], "cells.level.rule")
    if level["times_s"] != list(times):
        _fail("cells.level.times_s", "not_the_registered_times")
    if _int(level["count"], "cells.level.count", 1, 10 ** 4) != len(times) * len(bands):
        _fail("cells.level.count", "not_times_by_bands")
    move = _object(cells["move"], "cells.move", ("changes_between", "count", "end_s", "entry_s", "flags", "names",
                                                 "rule", "starts_s", "threshold", "undefined_reasons"))
    _codes(move["names"], "cells.move.names", MOVES)
    _codes(move["undefined_reasons"], "cells.move.undefined_reasons", UNDEFINED)
    _codes(move["flags"], "cells.move.flags", FLAGS)
    _codes(move["changes_between"], "cells.move.changes_between", CHANGES_BETWEEN)
    _text(move["rule"], "cells.move.rule")
    starts, end, entry = _moves(move, times, fresh)
    _windows(times, fresh)
    if _int(move["count"], "cells.move.count", 1, 10 ** 4) != len(starts) * len(MOVES) * len(bands):
        _fail("cells.move.count", "not_starts_by_moves_by_bands")

    measurement = _object(top["measurement"], "measurement", (
        "bsp_ends", "bsp_valid_if", "classes", "classes_rule", "commission_rate", "net_held", "not_a_price",
        "part_and_rest", "removals", "sides", "unit", "x"))
    sides = _codes(measurement["sides"], "measurement.sides", SIDES)
    classes = _codes(measurement["classes"], "measurement.classes", CLASSES)
    _texts(measurement["bsp_valid_if"], "measurement.bsp_valid_if")
    for key in ("classes_rule", "net_held", "not_a_price", "part_and_rest", "removals", "unit", "x"):
        _text(measurement[key], f"measurement.{key}")
    ends = _object(measurement["bsp_ends"], "measurement.bsp_ends", ("limit", "names", "rule", "step",
                                                                     "switch_rows", "whole_hundredths_share"))
    _codes(ends["names"], "measurement.bsp_ends.names", BSP_ENDS)
    for key in ("limit", "rule"):
        _text(ends[key], f"measurement.bsp_ends.{key}")
    _text(ends["switch_rows"], "measurement.bsp_ends.switch_rows", SWITCH_ROWS)
    if _rate(ends["step"], "measurement.bsp_ends.step") != BSP_STEP:
        _fail("measurement.bsp_ends.step", "not_supported")

    family = _object(top["family"], "family", ("alpha", "m", "method", "rule"))
    _text(family["method"], "family.method", HOLM)
    _text(family["rule"], "family.rule")
    if _int(family["m"], "family.m", 1, 1000) != len(sides):
        _fail("family.m", "not_one_primary_per_side")

    selection = _object(top["selection"], "selection", ("bsp_end", "inputs", "max_detectable", "measure", "rank",
                                                        "rule", "tie"))
    _text(selection["measure"], "selection.measure", SELECT_MEASURE)
    _text(selection["rank"], "selection.rank", SELECT_RANK)
    _text(selection["bsp_end"], "selection.bsp_end", CLAIM_END)
    _text(selection["tie"], "selection.tie", SELECT_TIE)
    _codes(selection["inputs"], "selection.inputs", SELECT_INPUTS)
    _text(selection["rule"], "selection.rule")

    statistics = _object(top["statistics"], "statistics", (
        "detectable_level", "min_tested_race_dates", "min_tested_values", "min_values", "power", "quantile_method",
        "quantiles", "rule"))
    _text(statistics["quantile_method"], "statistics.quantile_method", QUANTILE_METHOD)
    _text(statistics["rule"], "statistics.rule")
    quantiles = tuple(_rate(item, "statistics.quantiles") for item in _list(statistics["quantiles"],
                                                                            "statistics.quantiles"))
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

    verdicts = _object(top["verdicts"], "verdicts", (
        "half_gap_allowance", "half_year_min_values", "kill_level", "lower_bound_level", "names", "prediction",
        "printed_with", "printed_with_label", "rule", "underpowered_above"))
    _codes(verdicts["names"], "verdicts.names", VERDICTS)
    for key in ("prediction", "printed_with", "rule"):
        _text(verdicts[key], f"verdicts.{key}")
    label_texts = dict(_rules(verdicts["printed_with_label"], "verdicts.printed_with_label", VERDICTS))
    allowance = _object(verdicts["half_gap_allowance"], "verdicts.half_gap_allowance",
                        tuple(name for name, _, _ in bands))
    allowance = {name: _rate(allowance[name], f"verdicts.half_gap_allowance.{name}") for name, _, _ in bands}
    underpowered = _rate(verdicts["underpowered_above"], "verdicts.underpowered_above")
    if _rate(selection["max_detectable"], "selection.max_detectable") != underpowered:
        _fail("selection.max_detectable", "not_the_underpowered_line")

    exposure = _object(top["exposure"], "exposure", ("blind_pass_releases", "cites", "fresh_on_test", "lines",
                                                     "not_fresh_on_test", "printed_with_every_label"))
    cites = _texts(exposure["cites"], "exposure.cites")
    if len(set(cites)) != len(cites) or set(cites) != set(split.prior_exposure):
        _fail("exposure.cites", "not_the_splits_prior_exposure")
    _rules(exposure["lines"], "exposure.lines", cites)
    for key in ("blind_pass_releases", "fresh_on_test", "not_fresh_on_test", "printed_with_every_label"):
        _text(exposure[key], f"exposure.{key}")

    plan = Plan(
        sha256=registration_sha256(raw), study=STUDY_ID, lineage=LINEAGE, split=split, explore_label=explore,
        test_label=test, halves=halves, tiers=TIERS, start_month=months[0], end_month=months[1],
        countries=countries, event_type_id=population["event_type_id"], market_type=population["market_type"],
        betting_type=population["betting_type"],
        number_of_winners=_int(population["number_of_winners"], "population.number_of_winners", 1, 100),
        min_active_runners=_int(population["min_active_runners"], "population.min_active_runners", 1, 1000),
        codes=codes, runner_codes=runner_codes, times_s=times, fresh_s=fresh, bands=bands, starts_s=starts,
        end_s=end, entry_s=entry, threshold=_rate(move["threshold"], "cells.move.threshold"), sides=sides,
        commission=_rate(measurement["commission_rate"], "measurement.commission_rate"), classes=classes,
        bsp_step=BSP_STEP,
        bsp_whole_share=_rate(ends["whole_hundredths_share"], "measurement.bsp_ends.whole_hundredths_share"),
        family_m=len(sides), alpha=_rate(family["alpha"], "family.alpha"), max_detectable=underpowered,
        min_values=min_values,
        min_tested_values=_int(statistics["min_tested_values"], "statistics.min_tested_values", min_values, 10 ** 9),
        min_tested_race_dates=_int(statistics["min_tested_race_dates"], "statistics.min_tested_race_dates", 2,
                                   10 ** 6),
        quantiles=quantiles, power=_level(statistics["power"], "statistics.power"),
        detectable_level=_level(statistics["detectable_level"], "statistics.detectable_level"),
        min_clusters=_int(uncertainty["min_clusters"], "uncertainty.min_clusters", 2, 10 ** 6),
        confidence_level=_level(uncertainty["confidence_level"], "uncertainty.confidence_level"),
        kill_level=_level(verdicts["kill_level"], "verdicts.kill_level"),
        lower_bound_level=_level(verdicts["lower_bound_level"], "verdicts.lower_bound_level"),
        underpowered_above=underpowered,
        half_year_min_values=_int(verdicts["half_year_min_values"], "verdicts.half_year_min_values", 2, 10 ** 6),
        half_gap_allowance=allowance, label_note=verdicts["printed_with"], label_texts=label_texts,
        exposure_note=exposure["printed_with_every_label"], test_label_move_means=PRIMARIES_ONLY)
    if file_bytes(top) != raw:                  # last: every value is now known to be small (as partition.py)
        _fail("file", "not_canonical")
    return plan


# ---------------------------------------------------------------------------------------------------------------
# The record's codes

def race_label(plan, record):
    """(race date, partition label or None, the registration's half-year or None) of an index record, or None for
    a record with no anchor (it has no race date)."""
    if record["decision_time"]["first_published_market_time"] is None:
        return None
    day = race_date(record)
    return day, plan.split.label(day), plan.half(day)


def record_code(plan, record, at_s):
    """(code, False) when the index record alone gives the time a code (RECORD_CODES); else (None, whether bspMarket
    has no true or false value as of t): the time reaches the view."""
    gate = _Gate(plan.tiers, plan.start_month, plan.end_month, plan.countries, plan.event_type_id,
                 plan.market_type, plan.betting_type, plan.number_of_winners, plan.min_active_runners)
    code = market_code(gate, record, at_s)
    if code is not None:
        return code, False
    if race_label(plan, record)[1] not in plan.labels:
        return PARTITION_NOT_REGISTERED, False
    t, definition = decision_time(record, at_s), record["decision_time"]["definition"]
    flag = _as_of(definition.get("bspMarket"), t)
    if flag is False:
        return BSP_MARKET_FALSE, False
    if _as_of(definition.get("status"), t) != "OPEN" or _as_of(definition.get("inPlay"), t) is True:
        return MARKET_NOT_OPEN, False
    return None, flag is not True


# ---------------------------------------------------------------------------------------------------------------
# The fold

def _capture(record, market, prices, offset, t):
    reason = view_problem(record, t)
    if reason is not None:
        return Moment(offset_s=offset, t=t, view=None, reason=reason, quotes=(), ladder_malformed=False)
    return Moment(offset_s=offset, t=t, view=view_from_fold(record, market, prices, offset), reason=None,
                  quotes=(), ladder_malformed=False)


def fold(stream, record, offsets, until, limits):
    """(movement.MarketReplay, replay.PriceFold) of one index record from its primary member's decompressed stream:
    movement.replay_market's loop without the ladder fold (module docstring). ReplayProblem when the stream is not
    what the index says, ValueError for a record with no anchor."""
    market_id = record["market_id"]
    pending = []
    for offset in offsets:
        t = decision_time(record, offset)
        if t is None:
            raise ValueError("replay:no_view")
        pending.append((t, offset))
    pending.sort()
    market, prices, moments = None, PriceFold(), {}

    def capture(before):
        while pending and (before is None or pending[0][0] < before):
            t, offset = pending.pop(0)
            moments[offset] = _capture(record, market, prices, offset, t)

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
            moments[offset] = _capture(record, market, prices, offset, t)
    return MarketReplay(moments=moments, market=market, unreadable_from=unreadable_from), prices


# ---------------------------------------------------------------------------------------------------------------
# Prices

def changes(timeline, t):
    """The pts of the changes of value in one ltp timeline at or before t (module docstring): usable entries whose
    exact value differs from the usable entry before them. Nulls and unusable values are skipped."""
    found, last = [], None
    for pt, value in timeline:
        if pt > t:
            break
        if not usable_ltp(value):
            continue
        price = exact(value)
        if last is not None and price != last:
            found.append(pt)
        last = price
    return found


def price_at(plan, view, timeline, selection_id, at_s, banded=True):
    """The Price of one runner in the view at t: its first runner code in RUNNER_CODES' order, else priced.
    timeline is the runner's PriceFold ltp timeline. banded False skips the band test (a signal price)."""
    t = view.decision_ms
    runner = next(item for item in view.runners if item.selection_id == selection_id)
    if runner.ltp is None:
        return Price(NO_LTP, NEVER_SENT if runner.ltp_pt is None else RESET, None, None, None, None)
    if not usable_ltp(runner.ltp):
        return Price(NO_LTP, NOT_USABLE, None, None, None, None)
    ltp = exact(runner.ltp)
    band = plan.band(ltp)
    if banded and band is None:
        return Price(OUT_OF_BANDS, None, None, None, ltp, None)
    found = changes(timeline, t)
    if not found:
        return Price(NEVER_CHANGED, None, band, None, ltp, None)
    changed, removed = found[-1], [pt for _, pt, status in view.removals if status in REMOVED_STATUSES]
    age = t - changed
    if removed and changed <= max(removed):
        return Price(BEFORE_REMOVAL, None, band, age, ltp, changed)
    if age > plan.fresh(at_s) * 1000:
        return Price(STALE, None, band, age, ltp, changed)
    return Price(PRICED, None, band, age, ltp, changed)


def move_cell(plan, move):
    """DRIFT when M >= +threshold, STEAM when M <= -threshold, else FLAT. M is ln(later / earlier) of prices, so
    price up is DRIFT (movement.late_move_cell reads a share, where up is STEAM)."""
    threshold = float(plan.threshold)
    if move >= threshold:
        return DRIFT
    if move <= -threshold:
        return STEAM
    return FLAT


def changes_between(k):
    """The CHANGES_BETWEEN token of a count k of at least 1."""
    return CHANGES_BETWEEN[min(k, len(CHANGES_BETWEEN)) - 1]


def move_of(plan, record, replay, codes, timeline, selection_id, start_s):
    """The Move of one entry member from one start (module docstring). codes {time: market code or viewed}."""
    entry = replay.moments[plan.entry_s]
    problem = window_problem(replay, decision_time(record, start_s), entry.t)
    if problem in (WINDOW_UNKNOWN, REMOVAL_IN_WINDOW):
        return Move(start_s, None, problem, False, None)
    found = []
    for at_s, reasons in ((start_s, UNDEFINED[2:5]), (plan.end_s, UNDEFINED[5:8])):
        if codes[at_s] != VIEWED:
            return Move(start_s, None, reasons[0], False, None)
        moment = replay.moments[at_s]
        if selection_id not in moment.active():
            return Move(start_s, None, reasons[1], False, None)
        price = price_at(plan, moment.view, timeline, selection_id, at_s, banded=False)
        if price.code in (NO_LTP, NEVER_CHANGED, BEFORE_REMOVAL):
            return Move(start_s, None, reasons[1], False, None)
        if price.code == STALE:
            return Move(start_s, None, reasons[2], False, None)
        found.append(price)
    start, end = found
    k = sum(1 for pt in changes(timeline, entry.t) if pt > end.changed_pt)
    return Move(start_s, move_cell(plan, log_move(end.ltp, start.ltp)), None, problem == SUSPENSION, k)


# ---------------------------------------------------------------------------------------------------------------
# One record

@dataclass(frozen=True)
class Assessed:
    """codes {time: market code or viewed} for every registered time; bsp_unknown {time: True when the time reached
    the view with no true or false bspMarket as of t}; values, one Value per runner ACTIVE and listed at a viewed
    time, time by time; replayed, whether the member was read."""

    codes: dict
    bsp_unknown: dict
    values: tuple
    replayed: bool


def assess(stream, record, plan, limits):
    """The Assessed of one index record: every time's code from the record alone and, when some time reaches the
    view, the rest from the record's primary member's decompressed stream, read to the latest such time and no
    further. stream is not read when no time reaches the view. ReplayProblem when the stream is not what the index
    says."""
    codes, unknown = {}, {}
    for at_s in plan.times_s:
        codes[at_s], unknown[at_s] = record_code(plan, record, at_s)
    reached = [at_s for at_s in plan.times_s if codes[at_s] is None]
    if not reached:
        return Assessed(codes=codes, bsp_unknown=unknown, values=(), replayed=False)
    replay, prices = fold(stream, record, reached, max(decision_time(record, at_s) for at_s in reached),
                          limits)
    for at_s in reached:
        codes[at_s] = PRICE_CHANGE_MALFORMED if replay.moments[at_s].view.malformed_price_changes else VIEWED
    day, label, half = race_label(plan, record)
    values = []
    for at_s in plan.times_s:
        if codes[at_s] != VIEWED:
            continue
        moment = replay.moments[at_s]
        active = active_ids(record, moment.t)
        for sid in active:
            timeline = prices.ltp.get(sid, [])
            price = price_at(plan, moment.view, timeline, sid, at_s)
            klass, bsp, moves = None, None, ()
            if price.code == PRICED:
                klass, bsp = measure(record, moment.t, sid, active)
                klass = SCORED if klass == ELIGIBLE else klass
                if at_s == plan.entry_s:
                    moves = tuple(move_of(plan, record, replay, codes, timeline, sid, start)
                                  for start in plan.starts_s)
            values.append(Value(at_s=at_s, code=price.code, no_ltp=price.no_ltp, band=price.band,
                                age_ms=price.age_ms, ltp=price.ltp, label=label, half=half, race_date=day,
                                klass=klass, bsp=bsp, moves=moves))
    return Assessed(codes=codes, bsp_unknown=unknown, values=tuple(values), replayed=True)
