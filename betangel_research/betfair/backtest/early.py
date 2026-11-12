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
time: no market id, selection id, name or time of day.

Scores (score_of, series). Over the value rows, as pure functions. x = LTP / BSP - 1 is resting.score with the
LTP in place of an order's price: BACK reads x and LAY reads -x, and net_held takes off the commission of a bet
held to the result. Every figure is worked out at the BSP as written and at BSP + step; the switch (bsp_switch)
counts the scored values of the level cells at entry_s, both labels together, and when under bsp_whole_share of
their BSPs are whole hundredths the written value decides alone (resting.deciding_end). The cluster is the race
date; a mean has a CR1 SE with t(G - 1) (movement.cr1_mean), and its count is named values: nothing is a bet.

Blind figures (blind_names, blind_spreads). Per move cell, over its scored values of both labels: the SD and
symmetric ranges of x (resting.spread), and the detectable effect on the test label from the CR1 SE of those
values (detectable), empty whenever the SD is. The two moved cells of a start and band are named moved_larger and
moved_smaller, so the blind pass gives no sign. When the two starts' scored members in a band differ by fewer than
min_values values, the earlier start's SDs, ranges and effects are withheld.

Selection (select), on the explore label only, from its rows, two counts per move cell from the test label and the
switch, nothing else. A cell is eligible when both labels pass the size gate, it has a CR1 SE of x on the explore
label at the BSP as written, and its predicted detectable effect is at most max_detectable. Each side's primary is
the eligible cell with the highest mean / CR1 SE of its gross x at its claim end; a tie goes to the cell listed
first. The test (verdicts) is one-sided CR1 that the side's mean is above 0 on the test label, Holm over m = the
number of sides. Labels, per side, the first that applies: kill_free_fill, underpowered, cost_clearing_candidate,
signal_candidate, unresolved (the registration's verdicts.rule), each printed with its fixed texts.

Part and rest (given_cells, given_move_cells, kill_given, shown). Inside the level cell at entry_s, per band and
label, the move cells form partitions: on the explore label one per start, on the test label one per primary.
resting.given_together decides inside a partition; across two, a nested pair of unions that differs by 1 to
min_values - 1 rows withholds the later partition, and a crossing does not. A primary that is not given keeps its
label, p and Holm result and loses its mean, SE, interval, bounds, detectable effect and the line at both BSP ends.

What this does not do: it reads nothing after the latest time that reached the view, never re-prices a removal,
and builds no table, Markdown or file. Standard library only; it imports movement, replay, market_index, partition,
resting and lp and, through them, the modules they import. Nothing here is evidence of profitability.
"""

from collections import namedtuple
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
import itertools
import math

from .lp import BACK, LAY, _rules
from .market_index import _as_of, _checked_changes, _Market, _messages, _Unreadable, loads_strict
from .movement import (ANCHOR, CLUSTER, DATE_RE, ELIGIBLE, MARKET_CODES, MAX_REGISTRATION_BYTES, MONTH_RE,
                       QUANTILE_METHOD, REMOVED_STATUSES, SCHEMA, UNCERTAINTY_METHOD, MarketReplay, Moment, _codes,
                       _fail, _int, _object, _offset, _rate, _text, _texts, active_ids, cr1_mean, decision_time, exact,
                       holm, log_move, market_code, measure, race_date, registration_sha256, student_t_quantile,
                       view_problem, window_problem)
from .movement import CLASSES as MEASURE_CLASSES
from .partition import file_bytes
from .replay import PriceFold, ReplayProblem, usable_ltp, view_from_fold
from .resting import (AS_WRITTEN, BSP_ENDS, BSP_STEP, CLAIM_END, COUNTS_ONLY, G, KILL_END, NET_HELD, PLUS_STEP,
                      REST_NOT_GIVEN, SCORED, _Gate, _level, _named, _split, bound, cr1_test, deciding_end, described,
                      ends_apart, gate, gated, given_together, score, scored, spread, whole_hundredths, withheld)

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
# The names the blind pass shows for a start's two moved cells in a band: by scored values, larger first.
MOVED_LARGER, MOVED_SMALLER = "moved_larger", "moved_smaller"
KILL, UNDERPOWERED_LABEL, COST_CLEARING, SIGNAL, UNRESOLVED = VERDICTS = (
    "kill_free_fill", "underpowered", "cost_clearing_candidate", "signal_candidate", "unresolved")
# The checks every label is printed with, yes or no (verdicts).
CHECKS = ("kill_bound_below_0", "se_on_test", "detectable_within_ceiling", "rejected_under_holm",
          "mean_above_0_in_each_half_year", "net_lower_bound_above_allowance")
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
# The BSP switch (bsp_switch): whole hundredths among how many scored BSPs, and whether the two ends are apart.
Switch = namedtuple("Switch", ("whole", "of", "apart"))
# The selection (select): the eligible cells in the registered order, {side: primary or None}, and {MoveCell: what
# R3 prints of it}.
Selection = namedtuple("Selection", ("eligible", "primaries", "cells"))


# ---------------------------------------------------------------------------------------------------------------
# Registration

@dataclass(frozen=True)
class Plan:
    """What one registration and its split file say (parse_registration). sha256 is the registration's; split the
    partition.Split; halves ((name, first, last), ...) of the test label; times_s and fresh_s one entry per time;
    bands ((name, low, high), ...) as exact prices, low included and high not. Levels, rates and thresholds are
    exact Fractions. label_note, exposure_note, bsp_limit and removal_note are printed with every label,
    label_texts with each one."""

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
    bsp_limit: str
    removal_note: str
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
        exposure_note=exposure["printed_with_every_label"], bsp_limit=ends["limit"],
        removal_note=measurement["removals"], test_label_move_means=PRIMARIES_ONLY)
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


# ---------------------------------------------------------------------------------------------------------------
# Cells and scores. Every function from here on is pure and takes id-free Value rows.

def by_cell(plan, values):
    """{cell: its member rows}, every LevelCell and then every MoveCell in the plan's order. A level cell's members
    are the rows priced at its time in its band; a move cell's are the members of the level cell at entry_s in its
    band whose move from its start is that cell's. Rows of every class and label are kept: scored() picks values."""
    cells = {cell: [] for cell in plan.level_cells + plan.move_cells}
    for row in values:
        if row.code != PRICED:
            continue
        cells[LevelCell(row.at_s, row.band)].append(row)
        for move in row.moves:
            if move.cell is not None:
                cells[MoveCell(move.start_s, move.cell, row.band)].append(row)
    return cells


def _move(row, start_s):
    """The Move of an entry member from one start."""
    return next(move for move in row.moves if move.start_s == start_s)


def _dates(rows):
    return len({row.race_date for row in rows})


def score_of(plan, row, side, end, what=G):
    """One exact figure of a scored row for a side at one BSP end: gross (BACK x = LTP / BSP - 1, LAY -x) or
    net_held, by resting.score with the LTP in place of an order's price."""
    return score(plan, side, row.ltp, row.bsp + {AS_WRITTEN: 0, PLUS_STEP: plan.bsp_step}[end], what)


def series(plan, rows, side, end, what=G):
    """[(race date, float)] of one figure over the scored rows among rows; each exact figure is rounded once."""
    return [(row.race_date, float(score_of(plan, row, side, end, what))) for row in rows if row.klass == SCORED]


def bsp_switch(plan, cells):
    """The Switch over the scored values of the level cells at entry_s, both labels together (registration
    measurement.bsp_ends). cells: by_cell of both labels' rows."""
    whole, of = whole_hundredths(plan, [row for name, _, _ in plan.bands
                                        for row in cells[LevelCell(plan.entry_s, name)]])
    return Switch(whole, of, ends_apart(plan, whole, of))


def _counted(result):
    """A movement.cr1_mean result with its count named values."""
    return {"values" if key == "bets" else key: item for key, item in result.items()}


def figures(plan, rows, given=True):
    """R1 and R2 for one cell's rows on one label: members, each class's size, and x at both BSP ends
    (resting.described: under min_values values, counts only). given False keeps the counts and withholds every
    statistic (rest_not_given)."""
    found = {end: _counted(described(plan, series(plan, rows, BACK, end))) for end in BSP_ENDS}
    return {"members": len(rows), "classes": {klass: sum(1 for row in rows if row.klass == klass)
                                              for klass in plan.classes},
            "x": found if given else {end: withheld(item) for end, item in found.items()}}


def detectable(plan, se, values, test_values, test_race_dates):
    """The smallest true effect the test on the test label would catch with the registered power:
    (t(detectable_level; G_V - 1) + t(power; G_V - 1)) x se x sqrt(values / test_values), where se is a CR1 SE over
    values values and G_V is test_race_dates. None without an SE above 0, a value, a test value or two test race
    dates."""
    if not se or values < 1 or test_values < 1 or test_race_dates < 2:
        return None
    df = test_race_dates - 1
    factor = student_t_quantile(float(plan.detectable_level), df) + student_t_quantile(float(plan.power), df)
    return factor * se * math.sqrt(values / test_values)


def _se(plan, values):
    """The CR1 SE of [(race date, float)] values, or None (too few race dates, or no spread between them)."""
    return cr1_mean(values, plan.confidence_level, plan.min_clusters)["se"] or None


# ---------------------------------------------------------------------------------------------------------------
# The blind pass

def blind_names(plan, cells):
    """{MoveCell: the name the blind pass shows it by}. FLAT keeps its name. Per start and band the other two are
    moved_larger and moved_smaller, by scored values of both labels together, a tie to the registered order: named
    counts would give the rough sign of the early level cells. cells: by_cell of both labels' rows."""
    names = {}
    for start in plan.starts_s:
        for band, _, _ in plan.bands:
            moved = sorted((MoveCell(start, move, band) for move in (DRIFT, STEAM)),
                           key=lambda cell: -len(scored(cells[cell])))       # sorted is stable
            names.update({moved[0]: MOVED_LARGER, moved[1]: MOVED_SMALLER, MoveCell(start, FLAT, band): FLAT})
    return {cell: names[cell] for cell in plan.move_cells}


def blind_spreads(plan, cells):
    """B7 and B8: {MoveCell: resting.spread of x at the BSP as written over the cell's scored values of both labels,
    its reason (None, counts_only or rest_not_given) and detectable {test_values, test_race_dates, effect}}. The
    effect uses the CR1 SE of those pooled values and is None whenever the SD is. Per band, when the two starts'
    scored members (both labels) differ by fewer than min_values values, 0 included, the first start's SD, ranges
    and effect are withheld: two partitions of nearly one group would give the gap between their spreads."""
    close = {}
    for band, _, _ in plan.bands:
        rows = scored(cells[LevelCell(plan.entry_s, band)])
        sets = [{place for place, row in enumerate(rows) if _move(row, start).cell is not None}
                for start in plan.starts_s]
        close[band] = any(len(sets[0] ^ other) < plan.min_values for other in sets[1:])
    out = {}
    for cell in plan.move_cells:
        pooled, test = series(plan, cells[cell], BACK, AS_WRITTEN), scored(cells[cell], plan.test_label)
        found = spread(plan, pooled)
        effect, reason = None, COUNTS_ONLY
        if found["sd"] is not None:
            effect, reason = detectable(plan, _se(plan, pooled), len(pooled), len(test), _dates(test)), None
        if close[cell.band] and cell.start_s == plan.starts_s[0]:
            found, effect, reason = dict(found, sd=None, ranges=None), None, REST_NOT_GIVEN
        out[cell] = dict(found, reason=reason, detectable={"test_values": len(test), "test_race_dates": _dates(test),
                                                           "effect": effect})
    return out


# ---------------------------------------------------------------------------------------------------------------
# Selection, the test and the labels

def test_counts(plan, cells):
    """{MoveCell: (scored values on the test label, their race dates)}: all that the selection takes of it."""
    found = {cell: scored(cells[cell], plan.test_label) for cell in plan.move_cells}
    return {cell: (len(rows), _dates(rows)) for cell, rows in found.items()}


def select(plan, explore, counts, apart):
    """The Selection (registration selection.rule). explore: {MoveCell: its rows on the explore label}; counts:
    test_counts; apart: the switch. Nothing else of the test label is taken, and a row of another label is refused
    (ValueError). Per cell, cells holds both labels' counts, the three checks (sizes; an SE of x on the explore
    label at the BSP as written; the predicted detectable effect at most max_detectable; the last two None when
    the explore label fails the size gate, as is every figure), that SE, the effect, mean / SE for each side at
    its claim end, and eligible. primaries: {side: the eligible cell with the highest mean / SE, a tie to the cell
    listed first; None with no eligible cell}. Level cells are never candidates."""
    cells, eligible, best = {}, [], dict.fromkeys(plan.sides)
    for cell in plan.move_cells:
        if any(row.label != plan.explore_label for row in explore[cell]):
            raise ValueError("select:not_the_explore_label")
        rows = scored(explore[cell])
        values, days = len(rows), _dates(rows)
        test_values, test_days = counts[cell]
        se = effect = None
        ranks, checks = dict.fromkeys(plan.sides), {"sizes": gate(plan, values, days) and gate(plan, *counts[cell]),
                                                    "se": None, "detectable_within_ceiling": None}
        if gate(plan, values, days):
            se = _se(plan, series(plan, rows, BACK, AS_WRITTEN))
            effect = detectable(plan, se, values, test_values, test_days)
            checks.update(se=se is not None,
                          detectable_within_ceiling=effect is not None and effect <= plan.max_detectable)
            for side in plan.sides:
                found = cr1_mean(series(plan, rows, side, deciding_end(side, CLAIM_END, apart)),
                                 plan.confidence_level, plan.min_clusters)
                ranks[side] = found["mean"] / found["se"] if found["se"] else None
        cells[cell] = {"explore": {"values": values, "race_dates": days},
                       "test": {"values": test_values, "race_dates": test_days}, "checks": checks, "se": se,
                       "detectable": effect, "mean_over_se": ranks, "eligible": all(checks.values())}
        if not all(checks.values()):
            continue
        eligible.append(cell)
        for side in plan.sides:
            if ranks[side] is not None and (best[side] is None or ranks[side] > best[side][0]):
                best[side] = (ranks[side], cell)
    return Selection(tuple(eligible), {side: None if item is None else item[1] for side, item in best.items()},
                     cells)


def _half_years(plan, rows, side, end):
    """{half: {values, race_dates, mean_above_0}} of the test label's rows: the mean is read, never given."""
    out = {}
    for name, _, _ in plan.halves:
        part = series(plan, [row for row in rows if row.half == name], side, end)
        out[name] = {"values": len(part), "race_dates": len({day for day, _ in part}),
                     "mean_above_0": len(part) >= plan.half_year_min_values
                     and math.fsum(x for _, x in part) / len(part) > 0}
    return out


def verdicts(plan, primaries, cells, apart):
    """{side: its label and what is printed with it} (registration verdicts.rule), decided on the full values.
    primaries: Selection.primaries; cells: {MoveCell: its rows of both labels}; apart: the switch.
    The test is the one-sided CR1 test that the side's mean gross x on the test label at the claim end is above 0,
    with Holm over family_m (a side with no primary counts and is never rejected). Per side, the first that applies:
    kill_free_fill: over both labels pooled the size gate passes and the upper bound of mean net_held at the kill
    end, mean + t(kill_level; G - 1) x SE, is below 0. underpowered: no primary; or no SE on the test label; or
    the detectable effect from that SE is above underpowered_above. cost_clearing_candidate: the conditions of
    signal_candidate, and the lower bound of mean net_held on the test label at the claim end,
    mean - t(lower_bound_level; G - 1) x SE, above the band's half_gap_allowance. signal_candidate: rejected under
    Holm, and the mean above 0 in each half-year with half_year_min_values values. unresolved.
    Each answer: side, cell, band, label, text, printed_with, allowance, members and two shares on the test label
    (not scored; at k = 1 among the scored), test (with Holm), detectable, kill and net_lower (each bound keyed
    bound, so resting.withheld empties it), both (gross and net_held at both BSP ends), half_years and checks."""
    work = {}
    for side in plan.sides:
        cell = primaries[side]
        if cell is not None:
            rows = [row for row in cells[cell] if row.label == plan.test_label]
            claim = deciding_end(side, CLAIM_END, apart)
            work[side] = (cell, rows, claim, cr1_test(plan, series(plan, rows, side, claim)))
    adjusted = holm({side: work[side][3]["p_one_sided"] if side in work else None for side in plan.sides},
                    plan.alpha, plan.family_m)
    found = {}
    for side in plan.sides:
        answer = {"side": side, "cell": None, "band": None, "label": UNDERPOWERED_LABEL,
                  "text": plan.label_texts[UNDERPOWERED_LABEL],
                  "printed_with": (plan.label_note, plan.exposure_note, plan.bsp_limit, plan.removal_note),
                  "allowance": None, "members": None, "not_scored_share": None, "k_1_share": None, "test": None,
                  "detectable": None, "kill": None, "net_lower": None, "both": None, "half_years": None,
                  "checks": dict.fromkeys(CHECKS, False)}
        if side not in work:
            found[side] = answer
            continue
        cell, rows, claim, test = work[side]
        held, kill_end = scored(rows), deciding_end(side, KILL_END, apart)
        kill = gated(plan, series(plan, cells[cell], side, kill_end, NET_HELD))
        net = gated(plan, series(plan, rows, side, claim, NET_HELD))
        upper, lower = bound(kill, plan.kill_level, 1), bound(net, plan.lower_bound_level, -1)
        effect = detectable(plan, test["se"] if test["df"] is not None else None, len(held), len(held),
                            _dates(held))
        halves = _half_years(plan, rows, side, claim)
        allowance = plan.half_gap_allowance[cell.band]
        checks = {"kill_bound_below_0": upper is not None and upper < 0, "se_on_test": test["df"] is not None,
                  "detectable_within_ceiling": effect is not None and effect <= plan.underpowered_above,
                  "rejected_under_holm": adjusted[side][1],
                  "mean_above_0_in_each_half_year": all(item["mean_above_0"] for item in halves.values()),
                  "net_lower_bound_above_allowance": lower is not None and lower > allowance}
        if checks["kill_bound_below_0"]:
            label = KILL
        elif not (checks["se_on_test"] and checks["detectable_within_ceiling"]):
            label = UNDERPOWERED_LABEL
        elif checks["rejected_under_holm"] and checks["mean_above_0_in_each_half_year"]:
            label = COST_CLEARING if checks["net_lower_bound_above_allowance"] else SIGNAL
        else:
            label = UNRESOLVED
        found[side] = dict(
            answer, cell=cell, band=cell.band, label=label, text=plan.label_texts[label], allowance=float(allowance),
            members=len(rows), not_scored_share=(len(rows) - len(held)) / len(rows) if rows else None,
            k_1_share=sum(1 for row in held if _move(row, cell.start_s).k == 1) / len(held) if held else None,
            test=dict(_counted({key: item for key, item in test.items() if key != "t"}),
                      holm_p_adjusted=adjusted[side][0], holm_rejected=adjusted[side][1], bsp_end=claim),
            detectable=effect, kill=dict(_counted(kill), bound=upper, bsp_end=kill_end),
            net_lower=dict(_counted(net), bound=lower, bsp_end=claim),
            both={end: {what: _counted(described(plan, series(plan, rows, side, end, what))) for what in (G, NET_HELD)}
                  for end in BSP_ENDS},
            half_years=halves, checks=checks)
    return found


# ---------------------------------------------------------------------------------------------------------------
# Part and rest in the registered pass, applied after the selection and the labels

def _unions(classes):
    """Every union of some of the classes, the empty one and all of them included."""
    return [frozenset().union(*chosen) for size in range(len(classes) + 1)
            for chosen in itertools.combinations(classes, size)]


def _nested(plan, first, second):
    """Whether a union of first lies inside a union of second, or the other way, with 1 to min_values - 1 rows
    between them: the only sums the given figures can work out besides the classes themselves."""
    return any((one <= two or two <= one) and 0 < abs(len(one) - len(two)) < plan.min_values
               for one in first for two in second)


def given_cells(plan, count, partitions):
    """Part and rest across partitions of one universe of count rows (registration measurement.part_and_rest (2)).
    partitions: [[(name, the positions of its rows), ...], ...], the first given first; the groups of a partition
    share no row (ValueError otherwise). Inside a partition resting.given_together decides, and it is exact there.
    A partition's classes are then its given groups and the rest of the universe. When some union of a partition's
    classes lies inside a union of an earlier given partition's classes and the two differ by 1 to min_values - 1
    rows, nothing of the later partition is given. A crossing of two partitions, however small, is safe. The
    names given, in order."""
    universe, kept, given = frozenset(range(count)), [], []
    for groups in partitions:
        sets = [frozenset(members) for _, members in groups]
        if sum(map(len, sets)) != len(frozenset().union(*sets)):
            raise ValueError("given:groups_share_a_row")
        names = given_together(plan, count, [(name, set(members)) for name, members in groups])
        held = [members for (name, _), members in zip(groups, sets) if name in names]
        classes = [part for part in held + [universe.difference(*held)] if part]
        if any(_nested(plan, earlier, _unions(classes)) for earlier in kept):
            continue
        kept.append(_unions(classes))
        given += [name for name in names if name not in given]
    return given


def given_move_cells(plan, cells, label, primaries):
    """The set of move cells whose figures one label gives. cells: by_cell of rows of that label (or both);
    primaries: Selection.primaries. Per band, the universe is the scored rows of the level cell at entry_s on the
    label. On the explore label the partitions are the two starts, three cells each: the start with the BACK primary
    of this band first, else the one with the LAY primary, else the first start; inside a start the primaries
    first. On the test label each primary of this band is a partition of one group, BACK first; no other move cell
    gives a figure there (registered_pass.test_label_move_means)."""
    if label not in plan.labels:
        raise ValueError("given:label_not_registered")
    out = set()
    for band, _, _ in plan.bands:
        rows = scored(cells[LevelCell(plan.entry_s, band)], label)
        chosen = []
        for side in plan.sides:
            if primaries[side] is not None and primaries[side].band == band and primaries[side] not in chosen:
                chosen.append(primaries[side])
        if label == plan.explore_label:
            first = next((cell.start_s for cell in chosen), plan.starts_s[0])
            starts = [first] + [start for start in plan.starts_s if start != first]
            partitions = [[cell for cell in chosen if cell.start_s == start]
                          + [cell for cell in plan.move_cells if (cell.start_s, cell.band) == (start, band)
                             and cell not in chosen] for start in starts]
        else:
            partitions = [[cell] for cell in chosen]
        out.update(given_cells(plan, len(rows), [
            [(cell, {place for place, row in enumerate(rows) if _move(row, cell.start_s).cell == cell.move})
             for cell in partition] for partition in partitions]))
    return out


def kill_given(plan, rows, given):
    """Whether a primary's pooled kill gives its figures (measurement.part_and_rest (3)): each label holds
    min_values of the cell's scored values and gives that cell's own figures. rows: the cell's rows of both labels;
    given: {label: whether that label gives the cell}. The label is printed either way."""
    return all(len(scored(rows, label)) >= plan.min_values and given[label] for label in plan.labels)


def shown(verdict, given, kill_figures):
    """A verdict as R4 prints it. given False (its cell is not given on the test label): the label, text, p, Holm
    result, counts, shares and checks stay; the mean, SE, interval, both bounds, the detectable effect and the line
    at both BSP ends are withheld. kill_figures False withholds the kill's figures. A side with no primary is
    unchanged."""
    if verdict["cell"] is None:
        return dict(verdict, figures_given=None, kill_figures_given=None)
    out = dict(verdict, figures_given=given, kill_figures_given=kill_figures)
    if not given:
        out.update(test=withheld(verdict["test"]), net_lower=withheld(verdict["net_lower"]), detectable=None,
                   both={end: {what: withheld(item) for what, item in found.items()}
                         for end, found in verdict["both"].items()})
    if not kill_figures:
        out["kill"] = withheld(verdict["kill"])
    return out
