"""Registered measurement lp-v1: two resting GBP 2 orders inside a wide spread, replayed on historical files (pure).

Yu asked on 2026-11-01 whether providing liquidity in thin, wide-spread markets can pay (docs/lp-study.md). The
question this measures: when a patient resting order gets filled, is the counterparty impatient (the spread pays us)
or informed (we are picked off)? It is not a backtest contract and changes no existing source file, but as a module
of this package it (with lp_study.py) is in the engine certificate's source descriptor, so adding it changed the
certificate key again after movement-v1: any later P0 run-backtest needs certify-engine at these sources. The
registration (research/studies/lp-v1.json) holds every rule that decides anything; this module reads its values from
there, refuses one whose rule tokens are not the ones implemented here, and computes one market at a time from what
the index and the market's own replayed stream say. lp_study.py reads the files, aggregates and writes; nothing here
opens a file, reads a clock or uses the random module.

Reused from movement-v1 (movement.py), unchanged: the registration helpers, the history gates and market predicates
(market_code), LadderFold (book.py's F54 rules) and its Moment at a decision time, checked against the index
(replay.view_from_fold through movement._moment), the outcome classes (measure, valid_bsp), the window checks
(window_problem), the CLASSIC ladder and tick positions, CR1 by race date with t(G - 1), Holm and the Student t
routine. New here:

Registration (parse_registration): the exact keys of research/studies/lp-v1.json at every level, every value of its
type and domain, and every list of codes, classes, legs, fill kinds, states, effects, series, reasons and controls
equal to this module's own, in order. Tiers are PRO alone: the backer's stake x 2 basis of trd is documented for PRO
historical data (F60) and the queue rule reads full-depth atb/atl. The price bands must cover every CLASSIC price from
best_back_min_h to best_back_max_h once and the spread buckets every tick count from min_spread_ticks to
max_spread_ticks; min_spread_ticks must leave the LAY below the BACK. The quantiles must hold 1 - q beside every q,
since the blind pass releases them only as the ranges between such pairs. Event type, countries, market type, betting
type, number of winners and the minimum field are data, so a greyhound registration (4339) needs a new file, not new
code.

Traded volume (TradedFold): PRO rc.trd is price-keyed [price, volume] deltas of each runner's cumulative traded volume
at that price, folded by book.py's _apply_traded rules (F54): an entry replaces that price's volume, a volume of 0
deletes the point, an update that would empty or zero every existing point is the settlement artefact
(SA/4802553453457), counted and ignored, a null changes nothing, and an image (img true) replaces every runner's points
with those it carries (book.py starts a fresh market at an image). The files count backer's stake x 2 (F60;
SA/360002401937: "Traded volume (tv) is calculated as backers stake x 2", and its worked tv of 43.5 is the sum of the
update's trd amounts, 2.38 + 41.12), while atb and atl sizes are absolute, backer's stake with no x 2 (the same
article). Not every trd change is a trade: SA/360006560978 and SA/360002401937 say "Some traded volume updates ('trd')
are sent due to changes in the underlying foreign currency exchange rates", which may send "a new traded update ...
for a nominal amount" or "a decrease in the initial reported traded amount", the rates being "updated once every hour
a few seconds after the hour (Monday-Friday)". So each price also keeps its last published value, which a deletion or
an image never resets; change() returns, per rc entry, each price's rise above it and how many values fell below it
(a fall becomes the new last value, so a currency decrease cannot hide a later trade). Which rises are trades is
decided per pt batch by walk_market, and count() accumulates those. A malformed entry (not a [price, volume] pair of
finite numbers, a volume below 0, a price at or below 1 with a volume above 0, a missing price) is recorded by pt and
skipped, as LadderFold records its own; a decision on such a stream is never guessed.

Continuous replay (walk_market): one pass over the decompressed primary member with the index's reader and fold
(market_index._messages, _checked_changes, _Market; replay.PriceFold), LadderFold and TradedFold, processing each
message in file order until the first one published after C + the longest mark-out horizon, C = off +
cancel_offset_s. Messages sharing a pt are one batch, closed when the next pt (or the end) is read. At the close a
rise counts as traded volume only when the batch also carries an atb or atl entry at that price for that runner. The
registration marks the reason as an inference its blind pass checks: a trade consumes the order it matched, so its
level should change in the batch that carries its trd delta, and a currency restatement at a price nobody is trading
changes none. Events are kept in a heap by time and fired, in time order, after the close of the batch before the
first message published after their time, so the state an event reads holds every message published at or before
it, the batch at its time included. A decision event captures movement's Moment at t
(refusing, ReplayProblem, unless the fold equals the index at t), selects the runner and places its legs: the two main
legs one tick inside the spread and the positive control's two legs at the best prices, each with its queue (the size
shown at t at its price on its own side) and a copy of the runner's counted volume at t. Each leg reads every batch
published after t up to and including C and fills (once, in full, at its own price) at the first close where, by
FILL_KINDS' precedence, the batch counted a rise at a price beyond its own, below a LAY's price or above a BACK's
(trade_through); or its opposite side (the lay side for a LAY, the back side for a BACK) shows size at or through its
price summing to queue + stake (quote_cross); or the counted volume at its price has risen since t by divisor x (queue
+ stake) (volume_at_price). Its start is the runner's quote just before that batch, and events at fill + each horizon
capture the quote then. The first close at which the opposite best quote reached or crossed a live leg's price (any
size) is kept as its touch (a diagnostic). Over each decision's window (t, C] the selected runner's rises and falls
are counted for the diagnostics: counted or not; a counted rise by whether an atb or atl entry at its price in the
batch lowered or deleted that level, else raised or created it, else left it as shown (each entry against the level's
size just before its message); a rise not counted by where it was (the touch just before the batch, another
displayed price, an undisplayed one), with those at the touch outside the after-the-hour seconds apart; those in the
first after_the_hour_s seconds after a whole hour on a weekday; and rc entries raising three or more prices that are
not one run of adjacent CLASSIC ticks. A main leg's null control time u is drawn at placement from metrics.HashStream
with the registered seed and label, uniform over the whole milliseconds of (t, C], and events capture the quote at u
and at u + each horizon. A member that stops being readable fires the events before that point; a decision the index
says has a view after it refuses (unreadable_before_view_ends), as movement.replay_market does, and later captures are
missing (member_unreadable).

Selection (choose): the runners ACTIVE and listed at t (index) with a best back below the best lay, both on the
CLASSIC ladder, the best back within the band, are in band; those with a spread from min_spread_ticks to
max_spread_ticks are eligible; the one with the lowest best back (the shortest-priced eligible runner, rarely the
market favourite, whose spread is usually narrow), then best lay, then selection id is chosen. Classes (classify):
movement.measure's outcome classes, then the window classes over (t, C] (window_class).

Valuation (valued): the market P&L of the filled main legs in each outcome, commission at the registered rate on a
positive net rounded to the nearest penny half up (penny), and the expectation under p = 1 / BSP; with it the same
expectation under unrounded commission and the rounding effect, and (descriptive) under p normalised by the market's
book, the sum of 1 / BSP over its runners with a valid BSP (bsp_book). Mark-outs (markouts, against, edge): the move
of the mid or the opposite best quote against a leg, in ticks or implied-probability points, and the edge of its own
price over the start mid; a start or horizon quote whose best lay is more than max_spread_ticks above its best back
(wide) is too far apart to be a market, and gives spread_above_max. Statistics (cr1_test, decide): CR1 tests against 0
with the registered gates (a test below them reports counts only), Holm per family, the realised spread's check, the
positive control's check, the null control's kill rule and the discovery flags. Floats only where aggregated; prices,
stakes and P&L are exact Fractions until then.

Standard library only; it imports movement, market_index, replay and metrics and, through them, the modules movement
imports. Nothing here places an order, and nothing here is evidence of profitability.
"""

from dataclasses import dataclass
from fractions import Fraction
import heapq
import itertools
import math

from .market_index import _as_of, _checked_changes, _Market, _messages, _runner_id, _Unreadable, loads_strict
from .metrics import HashStream
from .movement import (_TICK, ANCHOR, BLOCKS, CLUSTER, DATE_RE, ELIGIBLE, LADDER_H, LADDER_NAMES, LADDERS,
                       MARKET_CODES, MAX_REGISTRATION_BYTES, MONTH_RE, QUANTILE_METHOD, SCHEMA, UNCERTAINTY_METHOD,
                       LadderFold, RegistrationError, _bands, _codes, _fail, _int, _moment, _object, _offset, _rate,
                       _text, _texts, active_ids, cr1_mean, decision_time, exact, final_runners, holm, hundredths,
                       measure, registration_sha256, student_t_cdf, tick_index, tick_position, valid_bsp,
                       view_problem, window_problem)
from .replay import PriceFold, ReplayProblem

STUDY_ID = "lp-v1"
TIERS = ("PRO",)
TRADED_KEY, TRADED_BASIS, TRADED_DIVISOR = "trd", "backers_stake_x2", 2
TRIALS = "independent"
RUNNERS_PER_MARKET = "shortest_eligible"
# Market codes in evaluation order (registration population.codes): movement-v1's history gates and market predicates
# through ladder_malformed, then the selection's.
CODES = MARKET_CODES[:14] + ("no_runner_in_band", "spread_below_min", "spread_above_max")
OUTCOME_CLASSES = ("outcome_unknown", "void_own_runner_removed", "post_decision_removal", "result_revised_after_close",
                   "missing_bsp")
WINDOW_CLASSES = ("window_unknown", "removal_in_window", "suspension_in_window", "in_play_in_window",
                  "schedule_revised_in_window", "stream_malformed_in_window")
CLASSES = OUTCOME_CLASSES + WINDOW_CLASSES + (ELIGIBLE,)
LAY, BACK = "LAY", "BACK"
SIDES = (LAY, BACK)
MAIN, JOIN = "main", "join"
VOLUME, CROSS, THROUGH = "volume_at_price", "quote_cross", "trade_through"
FILL_KINDS = (THROUGH, CROSS, VOLUME)        # in precedence order: one batch meeting two rules is the first's
INVENTORY = ("no_fill", "lay_only", "back_only", "round_trip")
ROUNDING = ("no_positive_outcome", "exact", "rounded_up", "rounded_down", "rounded_to_zero")
COMMISSION_ROUNDING = "nearest_penny_half_up"
SERIES = ("mid_ticks", "mid_points", "opposite_ticks", "opposite_points")
MID_SERIES = SERIES[:2]
UNDEFINED = ("start_not_two_sided", "member_unreadable", "market_not_open", "in_play", "not_two_sided",
             "spread_above_max")
WIDE = UNDEFINED[-1]
BSP_HORIZON = "bsp"
CONTROLS = ("join", "null")
NULL_LABEL = "lp-v1/null/{market_id}/{offset_s}/{side}"
PRIMARY, SECONDARY, REALISED = "net_per_decision", "mid_points", "edge_less_markout_mid_points"
VOLUME_TERCILES = ("V1", "V2", "V3")
# Per eligible decision, over its window (t, C] and its selected runner (registration diagnostics.rule).
TRD_COUNTS = ("rises_counted", "rises_counted_level_lowered", "rises_counted_level_raised",
              "rises_counted_level_unchanged", "rises_counted_after_the_hour", "rises_not_counted_at_touch",
              "rises_not_counted_at_touch_outside_the_hour", "rises_not_counted_displayed",
              "rises_not_counted_not_displayed", "rises_not_counted_after_the_hour", "values_below_last_published",
              "rc_raising_3_non_adjacent_prices")
MAX_HORIZON_S = 3600
HOUR_MS, DAY_MS = 3_600_000, 86_400_000
TOP_KEYS = ("blind_pass", "controls", "data", "decided_by", "diagnostics", "doc", "id", "measurement", "orders",
            "population", "power_note", "registered_on", "registered_pass", "schema", "scope", "seeds", "statistics",
            "status", "strata", "summary", "timing", "uncertainty")


# ---------------------------------------------------------------------------------------------------------------
# Registration

@dataclass(frozen=True)
class Registration:
    """The values of research/studies/lp-v1.json this module runs (parse_registration). The attribute names
    movement.market_code reads are movement's."""

    sha256: str
    tiers: tuple
    ladders: dict
    divisor: int
    start_month: str
    end_month: str
    decision_offsets_s: tuple
    cancel_offset_s: int
    countries: tuple
    event_type_id: str
    market_type: str
    betting_type: str
    number_of_winners: int
    min_active_runners: int
    best_back_min_h: int
    best_back_max_h: int
    min_spread_ticks: int
    max_spread_ticks: int
    stake: Fraction
    improve_ticks: int
    commission: Fraction
    horizons_s: tuple
    price_bands: tuple
    spread_buckets: tuple
    null_seed: int
    secondary_horizon_s: int
    holm_alpha: Fraction
    min_tested_bets: int
    min_tested_race_dates: int
    kill_fraction: Fraction
    kill_alpha: Fraction
    confidence_level: Fraction
    min_clusters: int
    quantiles: tuple
    min_bets_for_statistics: int
    power: Fraction
    after_the_hour_s: int
    latency_s: tuple

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
    def horizons(self):
        """Every mark-out horizon: the seconds after the start, then BSP."""
        return self.horizons_s + (BSP_HORIZON,)


def _rules(value, place, keys):
    """An object of exactly keys, each holding text."""
    _object(value, place, keys)
    for key in keys:
        _text(value[key], f"{place}.{key}")
    return value


def _seconds(value, place, high):
    """A non-empty, sorted list of distinct whole seconds from 1 to high."""
    if not isinstance(value, list) or not value:
        _fail(place, "not_a_list")
    seconds = tuple(_int(item, place, 1, high) for item in value)
    if list(seconds) != sorted(set(seconds)):
        _fail(place, "not_sorted_and_unique")
    return seconds


def parse_registration(raw):
    """The Registration of one registration file's raw bytes after every check in the module docstring;
    RegistrationError otherwise."""
    if not isinstance(raw, bytes) or len(raw) > MAX_REGISTRATION_BYTES:
        _fail("file", "not_bytes_or_too_large")
    try:
        value = loads_strict(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        _fail("file", "not_json")
    top = _object(value, "top", TOP_KEYS)
    _text(top["schema"], "schema", SCHEMA)
    _text(top["id"], "id", STUDY_ID)
    _text(top["status"], "status", "registered")
    if not isinstance(top["registered_on"], str) or not DATE_RE.fullmatch(top["registered_on"]):
        _fail("registered_on", "not_a_date")
    for key in ("decided_by", "doc", "power_note", "scope", "summary"):
        _text(top[key], key)
    blind = _object(top["blind_pass"], "blind_pass", ("releases", "rule", "withholds"))
    _texts(blind["releases"], "blind_pass.releases")
    _texts(blind["withholds"], "blind_pass.withholds")
    _text(blind["rule"], "blind_pass.rule")

    data = _object(top["data"], "data", ("duplicates", "end_month", "ladders", "month", "provider", "source",
                                         "start_month", "tiers", "traded_volume"))
    _text(data["provider"], "data.provider", "betfair_historical_data")
    for key in ("duplicates", "month", "source"):
        _text(data[key], f"data.{key}")
    tiers = _texts(data["tiers"], "data.tiers")
    if tiers != TIERS:
        _fail("data.tiers", "not_supported")
    ladders = _object(data["ladders"], "data.ladders", tiers)
    if any(ladders[tier] != LADDER_NAMES[tier] for tier in tiers):
        _fail("data.ladders", "not_supported")
    traded = _object(data["traded_volume"], "data.traded_volume", ("basis", "divisor", "key", "rule"))
    _text(traded["key"], "data.traded_volume.key", TRADED_KEY)
    _text(traded["basis"], "data.traded_volume.basis", TRADED_BASIS)
    _text(traded["rule"], "data.traded_volume.rule")
    if _int(traded["divisor"], "data.traded_volume.divisor", 1, 100) != TRADED_DIVISOR:
        _fail("data.traded_volume.divisor", "not_the_basis")
    months = (data["start_month"], data["end_month"])
    if not all(isinstance(month, str) and MONTH_RE.fullmatch(month) for month in months) or months[0] > months[1]:
        _fail("data.months", "malformed")

    timing = _object(top["timing"], "timing", ("anchor", "cancel_offset_s", "decision_offsets_s", "rule", "trials"))
    _text(timing["anchor"], "timing.anchor", ANCHOR)
    _text(timing["trials"], "timing.trials", TRIALS)
    _text(timing["rule"], "timing.rule")
    if not isinstance(timing["decision_offsets_s"], list) or not timing["decision_offsets_s"]:
        _fail("timing.decision_offsets_s", "not_a_list")
    offsets = tuple(_offset(item, "timing.decision_offsets_s") for item in timing["decision_offsets_s"])
    if list(offsets) != sorted(set(offsets)):
        _fail("timing.decision_offsets_s", "not_sorted_and_unique")
    cancel = _offset(timing["cancel_offset_s"], "timing.cancel_offset_s")
    if not offsets[-1] < cancel:
        _fail("timing.cancel_offset_s", "not_after_every_decision")

    population = _object(top["population"], "population", (
        "best_back_max_h", "best_back_min_h", "betting_type", "codes", "codes_rule", "countries", "event_type_id",
        "market_type", "max_spread_ticks", "min_active_runners", "min_spread_ticks", "not_checked",
        "number_of_winners", "price_ladder", "quotes", "runners_per_market", "selection_rule", "source"))
    _codes(population["codes"], "population.codes", CODES)
    _text(population["runners_per_market"], "population.runners_per_market", RUNNERS_PER_MARKET)
    for key in ("codes_rule", "not_checked", "price_ladder", "quotes", "selection_rule", "source"):
        _text(population[key], f"population.{key}")
    countries = _texts(population["countries"], "population.countries")
    low_h = _int(population["best_back_min_h"], "population.best_back_min_h", LADDER_H[0], LADDER_H[-1])
    high_h = _int(population["best_back_max_h"], "population.best_back_max_h", low_h, LADDER_H[-1])
    if low_h not in _TICK or high_h not in _TICK:
        _fail("population.best_back", "off_ladder")
    min_active = _int(population["min_active_runners"], "population.min_active_runners", 1, 1000)
    min_spread = _int(population["min_spread_ticks"], "population.min_spread_ticks", 1, len(LADDER_H) - 1)
    max_spread = _int(population["max_spread_ticks"], "population.max_spread_ticks", min_spread, len(LADDER_H) - 1)

    orders = _object(top["orders"], "orders", ("fill_kinds", "fill_rule", "improve_ticks", "legs", "not_modelled",
                                               "rule", "stake_p"))
    _codes(orders["legs"], "orders.legs", SIDES)
    _codes(orders["fill_kinds"], "orders.fill_kinds", FILL_KINDS)
    for key in ("fill_rule", "not_modelled", "rule"):
        _text(orders[key], f"orders.{key}")
    stake_p = _int(orders["stake_p"], "orders.stake_p", 1, 10 ** 6)
    improve = _int(orders["improve_ticks"], "orders.improve_ticks", 1, 100)
    if min_spread < 2 * improve + 1:
        _fail("population.min_spread_ticks", "lay_not_below_back")

    measurement = _object(top["measurement"], "measurement", (
        "bsp_valid_if", "classes", "classes_rule", "commission_rate", "commission_rounding", "edge_at_fill",
        "inventory_rule", "inventory_states", "markouts", "points", "rounding_effects", "rounding_rule", "ticks",
        "valuation", "window_rule"))
    _codes(measurement["classes"], "measurement.classes", CLASSES)
    _codes(measurement["inventory_states"], "measurement.inventory_states", INVENTORY)
    _codes(measurement["rounding_effects"], "measurement.rounding_effects", ROUNDING)
    _text(measurement["commission_rounding"], "measurement.commission_rounding", COMMISSION_ROUNDING)
    _texts(measurement["bsp_valid_if"], "measurement.bsp_valid_if")
    for key in ("classes_rule", "edge_at_fill", "inventory_rule", "points", "rounding_rule", "ticks", "valuation",
                "window_rule"):
        _text(measurement[key], f"measurement.{key}")
    markouts = _object(measurement["markouts"], "measurement.markouts", ("horizons_s", "rule", "series",
                                                                         "undefined_reasons"))
    _codes(markouts["series"], "measurement.markouts.series", SERIES)
    _codes(markouts["undefined_reasons"], "measurement.markouts.undefined_reasons", UNDEFINED)
    _text(markouts["rule"], "measurement.markouts.rule")
    horizons = _seconds(markouts["horizons_s"], "measurement.markouts.horizons_s", MAX_HORIZON_S)

    strata = _object(top["strata"], "strata", ("price_band_rule", "price_bands_h", "spread_bucket_rule",
                                               "spread_buckets_ticks", "volume_rule", "volume_terciles"))
    price_bands = _bands(strata["price_bands_h"], "strata.price_bands_h", low_h, ladder=True)
    if price_bands[-1][2] != high_h:
        _fail("strata.price_bands_h", "bands_not_contiguous")
    spread_buckets = _bands(strata["spread_buckets_ticks"], "strata.spread_buckets_ticks", min_spread, ladder=False)
    if spread_buckets[-1][2] != max_spread:
        _fail("strata.spread_buckets_ticks", "last_band_not_max_spread")
    _codes(strata["volume_terciles"], "strata.volume_terciles", VOLUME_TERCILES)
    for key in ("price_band_rule", "spread_bucket_rule", "volume_rule"):
        _text(strata[key], f"strata.{key}")

    controls = _object(top["controls"], "controls", ("join", "names", "null"))
    _codes(controls["names"], "controls.names", CONTROLS)
    _rules(controls["join"], "controls.join", ("failure", "rule"))
    null = _rules(controls["null"], "controls.null", ("failure", "label", "rule"))
    _text(null["label"], "controls.null.label", NULL_LABEL)
    seeds = _object(top["seeds"], "seeds", ("null",))

    registered = _object(top["registered_pass"], "registered_pass", ("discovery", "holm", "kill_rule", "primary",
                                                                     "realised_spread", "releases", "secondary",
                                                                     "tests"))
    _text(registered["releases"], "registered_pass.releases")
    primary = _object(registered["primary"], "registered_pass.primary", ("measure", "population", "rule"))
    _text(primary["measure"], "registered_pass.primary.measure", PRIMARY)
    _text(primary["population"], "registered_pass.primary.population")
    _text(primary["rule"], "registered_pass.primary.rule")
    secondary = _object(registered["secondary"], "registered_pass.secondary", ("horizon_s", "measure", "rule"))
    _text(secondary["measure"], "registered_pass.secondary.measure", SECONDARY)
    _text(secondary["rule"], "registered_pass.secondary.rule")
    secondary_horizon = _int(secondary["horizon_s"], "registered_pass.secondary.horizon_s", 1, MAX_HORIZON_S)
    if secondary_horizon not in horizons:
        _fail("registered_pass.secondary.horizon_s", "not_a_markout_horizon")
    realised = _rules(registered["realised_spread"], "registered_pass.realised_spread", ("measure", "rule"))
    _text(realised["measure"], "registered_pass.realised_spread.measure", REALISED)
    holm_object = _object(registered["holm"], "registered_pass.holm", ("alpha_familywise", "families"))
    _text(holm_object["families"], "registered_pass.holm.families")
    tests = _object(registered["tests"], "registered_pass.tests", ("min_bets", "min_race_dates", "rule"))
    _text(tests["rule"], "registered_pass.tests.rule")
    kill = _object(registered["kill_rule"], "registered_pass.kill_rule", ("alpha", "fraction_of_main", "rule"))
    _text(kill["rule"], "registered_pass.kill_rule.rule")
    _rules(registered["discovery"], "registered_pass.discovery", ("rule",))

    statistics = _object(top["statistics"], "statistics", ("min_bets_for_statistics", "power", "quantile_method",
                                                           "quantiles", "rule"))
    _text(statistics["quantile_method"], "statistics.quantile_method", QUANTILE_METHOD)
    _text(statistics["rule"], "statistics.rule")
    if not isinstance(statistics["quantiles"], list) or not statistics["quantiles"]:
        _fail("statistics.quantiles", "not_a_list")
    quantiles = tuple(_rate(item, "statistics.quantiles") for item in statistics["quantiles"])
    if list(quantiles) != sorted(set(quantiles)) or quantiles[0] <= 0:
        _fail("statistics.quantiles", "not_sorted_and_unique")
    if set(quantiles) != {1 - q for q in quantiles}:
        _fail("statistics.quantiles", "not_symmetric")    # the blind pass releases q(1 - a) - q(a) only
    power = _rate(statistics["power"], "statistics.power")
    if not Fraction(1, 2) < power:
        _fail("statistics.power", "not_above_one_half")

    uncertainty = _object(top["uncertainty"], "uncertainty", ("cluster", "confidence_level", "method",
                                                              "min_clusters", "rule"))
    _text(uncertainty["method"], "uncertainty.method", UNCERTAINTY_METHOD)
    _text(uncertainty["cluster"], "uncertainty.cluster", CLUSTER)
    _text(uncertainty["rule"], "uncertainty.rule")
    diagnostics = _object(top["diagnostics"], "diagnostics", ("after_the_hour_s", "latency_s", "rule"))
    _text(diagnostics["rule"], "diagnostics.rule")
    return Registration(
        sha256=registration_sha256(raw), tiers=tiers, ladders={tier: LADDERS[tier] for tier in tiers},
        divisor=TRADED_DIVISOR, start_month=months[0], end_month=months[1], decision_offsets_s=offsets,
        cancel_offset_s=cancel, countries=countries,
        event_type_id=_text(population["event_type_id"], "population.event_type_id"),
        market_type=_text(population["market_type"], "population.market_type"),
        betting_type=_text(population["betting_type"], "population.betting_type"),
        number_of_winners=_int(population["number_of_winners"], "population.number_of_winners", 1, 1000),
        min_active_runners=min_active, best_back_min_h=low_h, best_back_max_h=high_h, min_spread_ticks=min_spread,
        max_spread_ticks=max_spread, stake=Fraction(stake_p, 100), improve_ticks=improve,
        commission=_rate(measurement["commission_rate"], "measurement.commission_rate"), horizons_s=horizons,
        price_bands=price_bands, spread_buckets=spread_buckets,
        null_seed=_int(seeds["null"], "seeds.null", 0, 2 ** 63 - 1), secondary_horizon_s=secondary_horizon,
        holm_alpha=_rate(holm_object["alpha_familywise"], "registered_pass.holm.alpha_familywise"),
        min_tested_bets=_int(tests["min_bets"], "registered_pass.tests.min_bets", 2, 10 ** 9),
        min_tested_race_dates=_int(tests["min_race_dates"], "registered_pass.tests.min_race_dates", 2, 10 ** 6),
        kill_fraction=_rate(kill["fraction_of_main"], "registered_pass.kill_rule.fraction_of_main"),
        kill_alpha=_rate(kill["alpha"], "registered_pass.kill_rule.alpha"),
        confidence_level=_rate(uncertainty["confidence_level"], "uncertainty.confidence_level"),
        min_clusters=_int(uncertainty["min_clusters"], "uncertainty.min_clusters", 2, 10 ** 6),
        quantiles=quantiles,
        min_bets_for_statistics=_int(statistics["min_bets_for_statistics"], "statistics.min_bets_for_statistics", 2,
                                     10 ** 6),
        power=power,
        after_the_hour_s=_int(diagnostics["after_the_hour_s"], "diagnostics.after_the_hour_s", 1, 3599),
        latency_s=_seconds(diagnostics["latency_s"], "diagnostics.latency_s", MAX_HORIZON_S))


# ---------------------------------------------------------------------------------------------------------------
# Traded volume

class TradedFold:
    """Each runner's PRO traded volume by price, folded from market changes in file order (module docstring): points
    {runner: {price: volume as last published}}, last {runner: {price: the last value above 0 published there}},
    counted {runner: {price: the counted rises so far}}; malformed, zeroing and decreases hold the pt of every
    malformed trd update, ignored settlement-zeroing frame and value below its price's last published value."""

    def __init__(self):
        self.points = {}
        self.last = {}
        self.counted = {}
        self.malformed = []
        self.zeroing = []
        self.decreases = []

    @staticmethod
    def _parsed(entries):
        """[(price, volume)] of one trd update as exact Fractions, or None when any entry is malformed."""
        if not isinstance(entries, list):
            return None
        parsed = []
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                return None
            price, volume = exact(entry[0]), exact(entry[1])
            if price is None or volume is None or volume < 0 or (volume > 0 and price <= 1):
                return None
            parsed.append((price, volume))
        return parsed

    def change(self, pt, change):
        """Fold one market change; [(runner id, [(price, rise)], falls)] for each rc entry whose trd update was
        applied, in rc order: rise the amount a value exceeds its price's last published value (0 for a price never
        published), falls how many values lay below theirs."""
        if change.get("img") is True:
            for points in self.points.values():
                points.clear()              # book.py: an image starts the market afresh, trd included
        moved = []
        for item in change.get("rc") or ():
            rid = _runner_id(item.get("id"))
            if rid is None or item.get(TRADED_KEY) is None:
                continue                    # LadderFold records an unattributable rc entry; a null changes nothing
            parsed = self._parsed(item[TRADED_KEY])
            if parsed is None:
                self.malformed.append(pt)
                continue
            points, last = self.points.setdefault(rid, {}), self.last.setdefault(rid, {})
            if points and (not parsed or (all(volume == 0 for _, volume in parsed)
                                          and set(points) <= {price for price, _ in parsed})):
                self.zeroing.append(pt)     # F54, book.py _apply_traded: the settlement artefact, ignored
                continue
            rises, falls = [], 0
            for price, volume in parsed:
                if volume == 0:
                    points.pop(price, None)  # a deletion: the last published value stays
                    continue
                points[price] = volume
                before = last.get(price, 0)
                if volume > before:
                    rises.append((price, volume - before))
                elif volume < before:
                    falls += 1
                    self.decreases.append(pt)
                last[price] = volume
            moved.append((rid, rises, falls))
        return moved

    def count(self, rid, price, rise):
        """Add a rise that counts as traded volume (walk_market decides which do)."""
        counted = self.counted.setdefault(rid, {})
        counted[price] = counted.get(price, 0) + rise

    def matched(self):
        """The market's traded volume as published now, summed over runners and prices (the files' units)."""
        return sum((sum(points.values(), Fraction(0)) for points in self.points.values()), Fraction(0))


def adjacent(prices):
    """Whether exact prices form one run of adjacent CLASSIC ticks (False when any is off the ladder)."""
    indices = {tick_index(price) for price in prices}
    if None in indices:
        return False
    return max(indices) - min(indices) + 1 == len(indices)


def after_the_hour(pt, seconds):
    """Whether ms pt lies in the first `seconds` seconds after a whole UTC hour on a Monday to Friday (UTC): when
    Betfair's currency rates update (SA/360006560978). 1970-01-01 was a Thursday."""
    return (pt // DAY_MS + 3) % 7 < 5 and pt % HOUR_MS < seconds * 1000


def after_the_hour_ms(start, end, seconds):
    """How many whole milliseconds of (start, end] lie in after_the_hour's minutes."""
    total, hour = 0, (start + 1) // HOUR_MS * HOUR_MS
    while hour <= end:
        if (hour // DAY_MS + 3) % 7 < 5:
            total += max(0, min(end, hour + seconds * 1000 - 1) - max(start + 1, hour) + 1)
        hour += HOUR_MS
    return total


# ---------------------------------------------------------------------------------------------------------------
# Selection and legs

def tick_step(price, ticks):
    """The CLASSIC price `ticks` ticks above (or below, when negative) an exact ladder price, or None off the
    ladder."""
    scaled = hundredths(price)
    index = _TICK.get(scaled) if scaled is not None else None
    if index is None or not 0 <= index + ticks < len(LADDER_H):
        return None
    return Fraction(LADDER_H[index + ticks], 100)


def spread_ticks(back, lay):
    """Ticks from a best back to a best lay, both exact ladder prices, or None when either is off the ladder."""
    low, high = hundredths(back), hundredths(lay)
    if low not in _TICK or high not in _TICK:
        return None
    return _TICK[high] - _TICK[low]


@dataclass(frozen=True)
class Selection:
    """The runner a decision selected: its best back and best lay ((price, size), exact) and spread in ticks."""

    selection_id: int
    back: tuple
    lay: tuple
    spread: int


def choose(reg, active, moment, malformed):
    """(code, None) or (None, Selection) by the registration's selection_rule over the runners ACTIVE and listed at t
    (active) and the quotes at t; malformed: whether a trd entry published at or before t could not be read."""
    if moment.ladder_malformed or malformed:
        return "ladder_malformed", None
    in_band = []
    for sid in active:
        back, lay = moment.quote(sid)
        if back is None or lay is None or not back[0] < lay[0]:
            continue
        ticks, back_h = spread_ticks(back[0], lay[0]), hundredths(back[0])
        if ticks is not None and reg.best_back_min_h <= back_h <= reg.best_back_max_h:
            in_band.append((back[0], lay[0], sid, back, lay, ticks))
    if not in_band:
        return "no_runner_in_band", None
    wide = [item for item in in_band if item[5] >= reg.min_spread_ticks]
    if not wide:
        return "spread_below_min", None
    eligible = sorted(item for item in wide if item[5] <= reg.max_spread_ticks)
    if not eligible:
        return "spread_above_max", None
    _, _, sid, back, lay, ticks = eligible[0]
    return None, Selection(selection_id=sid, back=back, lay=lay, spread=ticks)


class Leg:
    """One simulated resting order: offset_s and role (main or join) of its decision, side, runner, exact price,
    queue (GBP shown ahead of it at t), placed (t) and cancel (C) in ms, base (the runner's counted volume at t);
    filled in by the walk: fill_pt and fill_kind, start (the runner's quote just before the fill's batch), horizons
    {seconds: quote at fill + seconds}, touched_pt (the first close at which the opposite best quote reached or
    crossed its price while it was unfilled), and for a main leg null_time u and null {0 or seconds: quote at u +
    seconds}. A quote is (best back, best lay), each (price, size) or None."""

    __slots__ = ("offset_s", "role", "side", "selection_id", "price", "queue", "placed", "cancel", "base", "fill_pt",
                 "fill_kind", "start", "horizons", "touched_pt", "null_time", "null")

    def __init__(self, offset_s, role, side, selection_id, price, queue, placed, cancel, base):
        self.offset_s, self.role, self.side, self.selection_id = offset_s, role, side, selection_id
        self.price, self.queue, self.placed, self.cancel, self.base = price, queue, placed, cancel, base
        self.fill_pt = self.fill_kind = self.start = self.null_time = self.touched_pt = None
        self.horizons, self.null = {}, {}

    def beyond(self, price):
        """Whether a trade at price is through this leg: below a LAY's price, above a BACK's."""
        return price < self.price if self.side == LAY else price > self.price

    def live(self, pt):
        return self.fill_pt is None and self.placed < pt <= self.cancel


def top(ladders, selection_id):
    """(best back, best lay) of one runner in a LadderFold now, each (price, size) or None."""
    if ladders.level_keyed:
        backs, lays = ladders.ladders(selection_id)
        return (backs[0] if backs else None), (lays[0] if lays else None)
    back, lay = ladders.books.get(selection_id, ({}, {}))
    return (back[max(back)] if back else None), (lay[min(lay)] if lay else None)


def facing(ladders, leg):
    """The size shown now on a leg's opposite side at prices at or through its price (the lay side at or below a
    LAY's price, the back side at or above a BACK's), summed as displayed (backer's stake), or None when no price is
    there."""
    backs, lays = ladders.ladders(leg.selection_id) if ladders.level_keyed else \
        (list(book.values()) for book in ladders.books.get(leg.selection_id, ({}, {})))
    if leg.side == LAY:
        shown = [size for price, size in lays if price <= leg.price]
    else:
        shown = [size for price, size in backs if price >= leg.price]
    return sum(shown, Fraction(0)) if shown else None


def place(reg, offset_s, selection, ladders, traded, t, cancel):
    """The decision's four legs (orders.rule, controls.join): main LAY and BACK one improve_ticks inside the spread,
    join LAY and BACK at the best prices, each with its queue at t and a copy of the runner's counted volume at t."""
    sid = selection.selection_id
    back_book, lay_book = ladders.books.get(sid, ({}, {}))
    base = dict(traded.counted.get(sid, {}))
    prices = ((MAIN, LAY, tick_step(selection.back[0], reg.improve_ticks)),
              (MAIN, BACK, tick_step(selection.lay[0], -reg.improve_ticks)),
              (JOIN, LAY, selection.back[0]), (JOIN, BACK, selection.lay[0]))
    legs = []
    for role, side, price in prices:
        own = back_book if side == LAY else lay_book       # a LAY rests on the back side, a BACK on the lay side
        shown = own.get(price)
        legs.append(Leg(offset_s, role, side, sid, price, shown[1] if shown else Fraction(0), t, cancel, base))
    return legs


# ---------------------------------------------------------------------------------------------------------------
# The continuous replay

@dataclass(frozen=True)
class MarketWalk:
    """moments {offset: Moment} and selections {offset: (code, Selection or None)} for every offset walked; matched
    {offset: the market's traded volume at t in the files' units}; legs, every Leg placed in placement order; trd
    {offset: {TRD_COUNTS name: count}} over each placed decision's window and runner; market, the index fold of every
    message read; unreadable_from as movement.MarketReplay's; stream_malformed, the sorted pts of malformed ladder or
    trd entries and unattributable rc entries; null_ladder_updates, trd_zeroing and trd_decreases, the folds'
    diagnostic counts over the whole read."""

    moments: dict
    selections: dict
    matched: dict
    legs: tuple
    trd: dict
    market: object
    unreadable_from: object
    stream_malformed: tuple
    null_ladder_updates: int = 0
    trd_zeroing: int = 0
    trd_decreases: int = 0


class _Batch:
    """What one pt batch carried: rises [(runner, price, rise)], entries {(runner, price)} of its atb/atl entries,
    lowered and raised {(runner, price)} where one of them lowered or deleted, or raised or created, the level shown
    at that price on its side, changed (runners with a ladder key), image, falls {runner: count} and scattered {runner:
    rc entries raising three or more prices not adjacent on the ladder}."""

    __slots__ = ("rises", "entries", "lowered", "raised", "changed", "image", "falls", "scattered")

    def __init__(self):
        self.rises, self.entries, self.changed, self.image = [], set(), set(), False
        self.lowered, self.raised, self.falls, self.scattered = set(), set(), {}, {}

    def levels(self, ladders, change):
        """Before ladders folds a change: each readable atb/atl entry's size against the size shown at its price on
        its side just before (0 when none). PRO ladders are keyed by price (registration data.tiers)."""
        for item in change.get("rc") or ():
            rid = _runner_id(item.get("id"))
            if rid is None:
                continue
            book = ladders.books.get(rid, ({}, {}))
            for side, key in enumerate(ladders.keys):
                parsed = ladders._parsed(item[key]) if item.get(key) is not None else None
                for _, price, size in parsed or ():
                    shown = book[side].get(price)
                    before = shown[1] if shown else 0
                    if size < before:
                        self.lowered.add((rid, price))
                    elif size > before:
                        self.raised.add((rid, price))

    def note(self, keys, change, moved):
        self.image = self.image or change.get("img") is True
        for item in change.get("rc") or ():
            rid = _runner_id(item.get("id"))
            if rid is None:
                continue
            for key in keys:
                entries = item.get(key)
                if key in item:
                    self.changed.add(rid)
                for entry in entries if isinstance(entries, list) else ():
                    price = exact(entry[-2]) if isinstance(entry, list) and len(entry) >= 2 else None
                    if price is not None:
                        self.entries.add((rid, price))
        for rid, rises, falls in moved:
            self.rises += [(rid, price, rise) for price, rise in rises]
            self.falls[rid] = self.falls.get(rid, 0) + falls
            if len(rises) >= 3 and not adjacent([price for price, _ in rises]):
                self.scattered[rid] = self.scattered.get(rid, 0) + 1


def null_time(reg, market_id, offset_s, side, t, cancel):
    """The null control's time u for a main leg: uniform over the whole milliseconds of (t, cancel]."""
    label = NULL_LABEL.format(market_id=market_id, offset_s=offset_s, side=side)
    return t + 1 + HashStream(reg.null_seed, label).below(cancel - t)


def walk_market(stream, record, reg, offsets, limits):
    """The MarketWalk of one index record from its primary member's decompressed stream, deciding at every offset in
    offsets (module docstring); ReplayProblem when the stream is not what the index says, ValueError for a record
    with no anchor."""
    market_id = record["market_id"]
    off = decision_time(record, 0)
    if off is None:
        raise ValueError("replay:no_view")
    cancel = off + reg.cancel_offset_s * 1000
    until = cancel + reg.horizons_s[-1] * 1000
    order = itertools.count()
    events = [(decision_time(record, offset), next(order), "decide", offset) for offset in offsets]
    heapq.heapify(events)
    market, prices, ladders, traded = None, PriceFold(), LadderFold(reg.ladders[record["provenance"]["plan"]]), \
        TradedFold()
    moments, selections, matched, legs, windows, trd = {}, {}, {}, [], {}, {}

    def push(time, kind, payload):
        heapq.heappush(events, (time, next(order), kind, payload))

    def handle(time, kind, payload):
        if kind == "decide":
            moment = _moment(record, market, prices, ladders, payload, time)
            moments[payload] = moment
            if moment.view is None:
                selections[payload] = (moment.reason, None)
                return
            code, selection = choose(reg, active_ids(record, time), moment,
                                     any(pt <= time for pt in traded.malformed))
            selections[payload] = (code, selection)
            matched[payload] = traded.matched()
            if selection is None:
                return
            windows[payload] = (selection.selection_id, time)
            trd[payload] = dict.fromkeys(TRD_COUNTS, 0)
            for leg in place(reg, payload, selection, ladders, traded, time, cancel):
                legs.append(leg)
                if leg.role == MAIN:
                    leg.null_time = null_time(reg, market_id, payload, leg.side, time, cancel)
                    for seconds in (0,) + reg.horizons_s:
                        push(leg.null_time + seconds * 1000, "null", (leg, seconds))
        elif kind == "null":
            leg, seconds = payload
            leg.null[seconds] = top(ladders, leg.selection_id)
        else:
            leg, seconds = payload
            leg.horizons[seconds] = top(ladders, leg.selection_id)

    def fire(limit):                            # every event before limit (None: every event)
        while events and (limit is None or events[0][0] < limit):
            time, _, kind, payload = heapq.heappop(events)
            handle(time, kind, payload)

    def open_runners(pt):                       # the selected runners whose window (t, C] holds pt
        return {sid for sid, t in windows.values() if t < pt <= cancel}

    def where(sid, price, prior):               # a rise not counted: where its price was just before the batch
        back, lay = prior.get(sid, (None, None))
        if any(quote is not None and quote[0] == price for quote in (back, lay)):
            return "rises_not_counted_at_touch"
        books = ladders.books.get(sid, ({}, {}))  # no entry at price in the batch: unchanged by it
        shown = any(price in book for book in books) if not ladders.level_keyed else \
            any(level[0] == price for book in books for level in book.values())
        return "rises_not_counted_displayed" if shown else "rises_not_counted_not_displayed"

    def close(pt, batch, prior):                # one pt batch is complete: count its trades, then fill
        counted = {}
        for rid, price, rise in batch.rises:
            if (rid, price) in batch.entries:
                traded.count(rid, price, rise)
                counted.setdefault(rid, set()).add(price)
        hour = after_the_hour(pt, reg.after_the_hour_s)
        for offset, (sid, t) in windows.items():
            if not t < pt <= cancel:
                continue
            counts = trd[offset]
            for rid, price, rise in batch.rises:
                if rid != sid:
                    continue
                if (rid, price) in batch.entries:
                    counts["rises_counted"] += 1
                    counts["rises_counted_level_lowered" if (rid, price) in batch.lowered else
                           "rises_counted_level_raised" if (rid, price) in batch.raised else
                           "rises_counted_level_unchanged"] += 1
                    counts["rises_counted_after_the_hour"] += hour
                else:
                    found = where(sid, price, prior)
                    counts[found] += 1
                    counts["rises_not_counted_after_the_hour"] += hour
                    if found == "rises_not_counted_at_touch" and not hour:
                        counts["rises_not_counted_at_touch_outside_the_hour"] += 1
            counts["values_below_last_published"] += batch.falls.get(sid, 0)
            counts["rc_raising_3_non_adjacent_prices"] += batch.scattered.get(sid, 0)
        for leg in legs:
            if not leg.live(pt):
                continue
            sid, kinds = leg.selection_id, set()
            now = counted.get(sid, ())
            if any(leg.beyond(price) for price in now):
                kinds.add(THROUGH)
            if batch.image or sid in batch.changed:
                shown = facing(ladders, leg)
                if shown is not None:
                    if leg.touched_pt is None:
                        leg.touched_pt = pt
                    if shown >= leg.queue + reg.stake:
                        kinds.add(CROSS)
            if leg.price in now and traded.counted[sid][leg.price] - leg.base.get(leg.price, 0) >= \
                    reg.divisor * (leg.queue + reg.stake):
                kinds.add(VOLUME)
            kind = next((kind for kind in FILL_KINDS if kind in kinds), None)
            if kind is not None:
                leg.fill_pt, leg.fill_kind, leg.start = pt, kind, prior[sid]
                for seconds in reg.horizons_s:
                    push(pt + seconds * 1000, "horizon", (leg, seconds))

    # A batch no window holds is folded but not kept: fills and the diagnostics read counted volume only as its rise
    # since t, and only in open windows.
    unreadable_from, applied, batch, pending, prior, fresh = None, None, None, None, {}, False
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
                close(batch, pending, prior)
            batch, pending, fresh = pt, None, True
        fire(pt)
        if pt > until:
            break
        try:
            checked = _checked_changes(message)
        except _Unreadable:
            unreadable_from = pt
            break
        if fresh:                               # the open windows' runners' quotes just before this pt batch
            runners, fresh = open_runners(pt), False
            prior = {sid: top(ladders, sid) for sid in runners}
            pending = _Batch() if runners else None
        for mid, change in checked:
            if mid != market_id:
                continue
            if market is None:
                market = _Market(market_id, pt)
            if pending is not None:
                pending.levels(ladders, change)
            market.change(pt, change)
            prices.change(pt, change)
            ladders.change(pt, change)
            moved = traded.change(pt, change)
            if pending is not None:
                pending.note(ladders.keys, change, moved)
        applied = pt
    if pending is not None:
        close(batch, pending, prior)
    if unreadable_from is not None and unreadable_from > until:
        unreadable_from = None
    if unreadable_from is None:
        fire(None)
    else:
        fire(unreadable_from)
        for time, _, kind, payload in sorted(events):
            if kind == "decide":
                if view_problem(record, time) is None:
                    raise ReplayProblem(f"replay:{market_id}:unreadable_before_view_ends")
                handle(time, kind, payload)
    return MarketWalk(moments=moments, selections=selections, matched=matched, legs=tuple(legs), trd=trd,
                      market=market, unreadable_from=unreadable_from,
                      stream_malformed=tuple(sorted(set(ladders.malformed) | set(traded.malformed))),
                      null_ladder_updates=len(ladders.nulls), trd_zeroing=len(traded.zeroing),
                      trd_decreases=len(traded.decreases))


# ---------------------------------------------------------------------------------------------------------------
# Classes and valuation

def window_class(walk, start, end):
    """The first window class for (start, end] in ms (registration measurement.window_rule), or None."""
    problem = window_problem(walk, start, end)
    if problem is not None:
        return problem
    fields = walk.market.fields
    in_play = fields.get("inPlay", [])
    if _as_of(in_play, start) is True or any(start < pt <= end and value is True for pt, value in in_play):
        return "in_play_in_window"
    times = fields.get("marketTime", [])
    first = times[0][1] if times else None
    if any(start < pt <= end and value is not None and value != first for pt, value in times):
        return "schedule_revised_in_window"
    if any(start < pt <= end for pt in walk.stream_malformed):
        return "stream_malformed_in_window"
    return None


def classify(record, walk, t, cancel, selection_id, active):
    """(class, BSP or None) of one issued decision (registration measurement.classes_rule)."""
    klass, bsp = measure(record, t, selection_id, active)
    if klass != ELIGIBLE:
        return klass, None
    window = window_class(walk, t, cancel)
    return (window, None) if window is not None else (ELIGIBLE, bsp)


def penny(value):
    """An exact amount at or above 0 rounded to the nearest penny, a half penny up (Exchange General Rules, Results
    and market settlement: GBP 3.335 settles as 3.34)."""
    return Fraction(math.floor(value * 100 + Fraction(1, 2)), 100)


def outcome_pnl(stake, lay_price, back_price):
    """(P&L if the selected runner wins, P&L if it loses) of the filled main legs; a price of None did not fill."""
    win = lose = Fraction(0)
    if back_price is not None:
        win, lose = win + stake * (back_price - 1), lose - stake
    if lay_price is not None:
        win, lose = win - stake * (lay_price - 1), lose + stake
    return win, lose


def valued(reg, lay_price, back_price, bsp):
    """(net, net with unrounded commission, rounding effect) of one eligible decision (registration
    measurement.valuation and rounding_rule), net and the unrounded net exact."""
    if bsp <= 1:
        raise ValueError("bsp_not_above_one")
    win, lose = outcome_pnl(reg.stake, lay_price, back_price)
    charged = {name: (penny(reg.commission * net), reg.commission * net) if net > 0 else (Fraction(0), Fraction(0))
               for name, net in (("win", win), ("lose", lose))}
    p = 1 / bsp
    net = p * (win - charged["win"][0]) + (1 - p) * (lose - charged["lose"][0])
    unrounded = p * (win - charged["win"][1]) + (1 - p) * (lose - charged["lose"][1])
    positive = [charged[name] for name, value in (("win", win), ("lose", lose)) if value > 0]
    if not positive:
        effect = "no_positive_outcome"
    else:
        rounded, raw = positive[0]
        effect = ("exact" if rounded == raw else "rounded_to_zero" if rounded == 0 else
                  "rounded_up" if rounded > raw else "rounded_down")
    return net, unrounded, effect


def bsp_book(record):
    """The market's book at BSP, the sum of 1 / BSP over the runners listed at the end with a valid BSP
    (movement.valid_bsp), exact; None when none has one. The index record holds every runner's final BSP."""
    bsps = [valid_bsp(record, sid) for sid in final_runners(record)]
    bsps = [bsp for bsp in bsps if bsp is not None]
    return sum((1 / bsp for bsp in bsps), Fraction(0)) if bsps else None


def valued_normalised(reg, lay_price, back_price, bsp, book):
    """The net of valued() with rounded commission, but under p = (1 / BSP) / book, book the market's bsp_book
    (registration measurement.valuation: descriptive, never deciding), exact."""
    win, lose = outcome_pnl(reg.stake, lay_price, back_price)
    p = 1 / bsp / book
    return sum((weight * (net - (penny(reg.commission * net) if net > 0 else 0))
                for weight, net in ((p, win), (1 - p, lose))), Fraction(0))


def inventory(lay_filled, back_filled):
    """The inventory state of a decision's main legs (registration measurement.inventory_rule)."""
    return INVENTORY[int(lay_filled) + 2 * int(back_filled)]


# ---------------------------------------------------------------------------------------------------------------
# Mark-outs

def mid(quote):
    """The exact mid of a two-sided quote (best back below best lay), else None."""
    back, lay = quote
    if back is None or lay is None or not back[0] < lay[0]:
        return None
    return (back[0] + lay[0]) / 2


def opposite(side, quote):
    """The best price on the other side of the book from a leg: the best back for a BACK, the best lay for a LAY."""
    found = quote[0] if side == BACK else quote[1]
    return None if found is None else found[0]


def against(side, start, end, unit):
    """The move from price start to price end against a leg (positive hurts it), in ticks or implied-probability
    points: a BACK is hurt when the price lengthens, a LAY when it shortens."""
    if unit == "ticks":
        change = tick_position(end) - tick_position(start)
    else:
        change = 100 / start - 100 / end
    return change if side == BACK else -change


def favourable(side, price, reference, unit):
    """How far a leg's own price lies from a reference price in its favour (a BACK above it, a LAY below it)."""
    return against(side, reference, price, unit)


def wide(reg, quote):
    """Whether a quote's best lay lies more than max_spread_ticks CLASSIC ticks above its best back, by tick_position
    (registration measurement.markouts: a quote too far apart to be a market, as the selection's cap); False unless
    both sides show a price."""
    back, lay = quote
    if back is None or lay is None:
        return False
    low, high = tick_position(back[0]), tick_position(lay[0])
    return low is None or high is None or high - low > reg.max_spread_ticks


def horizon_problem(reg, walk, time, quote, need):
    """The undefined reason of a capture at time ms (registration measurement.markouts), or None; need is the price
    the series reads from the quote (None when missing)."""
    if quote is None or (walk.unreadable_from is not None and time >= walk.unreadable_from):
        return "member_unreadable"
    fields = walk.market.fields
    if _as_of(fields.get("status", []), time) != "OPEN":
        return "market_not_open"
    if _as_of(fields.get("inPlay", []), time) is True:
        return "in_play"
    if need is None:
        return "not_two_sided"
    if wide(reg, quote):
        return WIDE
    return None


def markouts(reg, walk, side, start_time, start, captures, bsp, start_reason="start_not_two_sided"):
    """{(series, horizon): value as a float, or the undefined reason} of one leg (registration
    measurement.markouts): start, the quote at the start (None when it has none: start_reason then names why; a
    start too wide to be a market gives spread_above_max); captures {seconds: quote}; horizons in seconds, then BSP
    for the mid series."""
    out = {}
    for series in SERIES:
        unit = "ticks" if series.endswith("ticks") else "points"
        read = mid if series in MID_SERIES else (lambda quote: opposite(side, quote))
        begin = read(start) if start is not None else None
        missing = start_reason if start is None else "start_not_two_sided"
        if begin is not None and wide(reg, start):
            begin, missing = None, WIDE
        for seconds, quote in sorted(captures.items()):
            key = (series, seconds)
            if begin is None:
                out[key] = missing
                continue
            end = read(quote) if quote is not None else None
            problem = horizon_problem(reg, walk, start_time + seconds * 1000, quote, end)
            out[key] = problem if problem is not None else float(against(side, begin, end, unit))
        if series in MID_SERIES:
            out[(series, BSP_HORIZON)] = missing if begin is None else float(against(side, begin, bsp, unit))
    return out


def leg_markouts(reg, walk, leg, bsp):
    """markouts() of a filled leg from its start, with a capture missing (member unreadable) at any horizon the walk
    did not reach."""
    captures = {seconds: leg.horizons.get(seconds) for seconds in reg.horizons_s}
    return markouts(reg, walk, leg.side, leg.fill_pt, leg.start, captures, bsp)


def null_markouts(reg, walk, leg, bsp):
    """The null control's mid moves of a main leg from its time u (registration controls.null): a start at u that
    is unreadable, not OPEN, in play or too wide gives that reason, one with no two-sided quote start_not_two_sided."""
    captures = {seconds: leg.null.get(seconds) for seconds in reg.horizons_s}
    start = leg.null.get(0)
    problem = horizon_problem(reg, walk, leg.null_time, start, mid(start) if start is not None else None)
    values = markouts(reg, walk, leg.side, leg.null_time, None if problem else start, captures, bsp,
                      start_reason="start_not_two_sided" if problem in (None, "not_two_sided") else problem)
    return {key: value for key, value in values.items() if key[0] in MID_SERIES}


def edge(reg, leg):
    """{ticks, points} of a filled leg's own price over its start mid, in its favour; or why it has none,
    start_not_two_sided without a start mid or spread_above_max with a start too wide (registration
    measurement.edge_at_fill)."""
    centre = mid(leg.start) if leg.start is not None else None
    if centre is None:
        return UNDEFINED[0]
    if wide(reg, leg.start):
        return WIDE
    return {unit: float(favourable(leg.side, leg.price, centre, unit)) for unit in ("ticks", "points")}


# ---------------------------------------------------------------------------------------------------------------
# Tests, controls and flags

def cr1_test(reg, rows):
    """A CR1 test against 0 over [(race date, float x)] rows (registration registered_pass.tests): cr1_mean's keys
    with t and p_two_sided; reason underpowered (below the gates: not tested, and counts only, no mean, SE or
    interval, as movement.cr1_contrast), too_few_clusters or se_undefined."""
    result = dict(cr1_mean(rows, reg.confidence_level, reg.min_clusters), t=None, p_two_sided=None)
    if result["bets"] < reg.min_tested_bets or result["race_dates"] < reg.min_tested_race_dates:
        return dict(result, mean=None, se=None, df=None, t_critical=None, ci_low=None, ci_high=None,
                    reason="underpowered")
    if result["se"] is None or result["se"] == 0:
        return result
    t = result["mean"] / result["se"]
    return dict(result, t=t, p_two_sided=2.0 * (1.0 - student_t_cdf(abs(t), result["df"])))


def _tested(result):
    return result["p_two_sided"] is not None


def decide(reg, primary, secondary, realised, join, null, null_main):
    """(tests, controls, flags) by the registered rules, each {offset: ...}; every argument {offset: cr1_test
    result}: primary over the eligible decisions' net, secondary over the main legs' move at the secondary horizon,
    realised over their edge less that move, join over the positive control's move, null and null_main the null
    control's and the main legs' over the same legs. tests holds the two Holm families and, judged unadjusted, the
    realised spread. Pure: no file, clock or draw."""
    alpha, fraction = float(reg.kill_alpha), float(reg.kill_fraction)
    offsets = list(primary)
    holm_primary = holm({o: primary[o]["p_two_sided"] for o in offsets}, reg.holm_alpha, len(offsets))
    holm_secondary = holm({o: secondary[o]["p_two_sided"] for o in offsets}, reg.holm_alpha, len(offsets))
    tests, controls, flags = {"primary": {}, "secondary": {}, "realised_spread": {}}, {"join": {}, "null": {}}, {}
    for o in offsets:
        p_main, s_main, r_main = primary[o], secondary[o], realised[o]
        tests["primary"][o] = dict(p_main, holm_p_adjusted=holm_primary[o][0], holm_rejected=holm_primary[o][1])
        tests["secondary"][o] = dict(s_main, holm_p_adjusted=holm_secondary[o][0], holm_rejected=holm_secondary[o][1],
                                     adverse_selection_detected=holm_secondary[o][1] and s_main["mean"] > 0)
        # Unadjusted, so that no multiplicity correction can help a flag; an untestable realised spread fails.
        negative = r_main["mean"] is not None and r_main["mean"] < 0 and \
            (r_main["p_two_sided"] is None or r_main["p_two_sided"] < alpha)
        holds = r_main["reason"] != "underpowered" and r_main["mean"] is not None and not negative
        tests["realised_spread"][o] = dict(r_main, not_significantly_negative=holds)
        detects = _tested(join[o]) and join[o]["mean"] > 0 and join[o]["p_two_sided"] < alpha
        controls["join"][o] = dict(join[o], detects_adverse_selection=detects)
        own, same = null[o], null_main[o]
        if own["reason"] == "underpowered" or own["mean"] is None:
            why = BLOCKS[0]
        elif same["reason"] == "underpowered" or same["mean"] is None:
            why = BLOCKS[1]
        elif abs(own["mean"]) >= fraction * abs(same["mean"]) and (own["p_two_sided"] is None
                                                                   or own["p_two_sided"] < alpha):
            why = BLOCKS[2]
        else:
            why = None
        controls["null"][o] = {"null": own, "main_same_legs": same, "blocks": why is not None, "why": why}
        checks = {"primary_rejected_under_holm": holm_primary[o][1],
                  "primary_positive": p_main["mean"] is not None and p_main["mean"] > 0,
                  "primary_ci_low_above_0": p_main["ci_low"] is not None and p_main["ci_low"] > 0,
                  "realised_spread_not_significantly_negative": holds,
                  "join_detects_adverse_selection": detects,
                  "null_does_not_block": why is None}
        flags[o] = dict(checks, discovery_flag=all(checks.values()))
    return tests, controls, flags
