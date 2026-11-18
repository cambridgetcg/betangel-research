"""Registered measurements close-v1 and close-early-v1: resting orders before the off, on historical files (pure).

close-v1 asks whether a resting GBP 2 order on the favourite, placed minutes before the off and pulled before it,
gets a price that beats BSP once only the orders that would fill are counted (docs/research-loop.md). close-early-v1
asks the same of resting orders placed hours before the off, on the favourite and on each runner of a price range,
and reads the explore label alone. So there are two kinds of study here: close-v1 (STUDY_ID), and the new ids
(EARLY_IDS: close-early-v1 and its registered fallback close-early-fav-v1 and close-early-band-v1, parsed alike).
An "order" here is a simulated intent inside the backtest package; nothing here can reach an exchange. It is not a
backtest contract, but as modules of this package fills.py and this file are in the engine certificate's source
descriptor, so any change to them moves the certificate key: any later P0 run-backtest needs certify-engine at
these sources. Nothing here opens a file, reads a clock or uses the random module.

Three files decide everything, and this module reads its values from them: the registration
(research/studies/close-v1.json, or a new id's), the rule files it names (research/rules/, parsed by
rulefile.parse_rule) and the split file it names (parsed by partition.parse_split).

Registration (parse_registration): the exact keys of the registration at every level, every value of its type and
domain, the file in its one byte form (sorted keys, indent 2, ASCII, one final newline), and every list of codes,
classes, models, fill kinds, reasons and labels equal to this module's own, in order. It is given the raw bytes
of the split file and of each rule file, checks each sha256 against the one the registration names, and refuses:
rule files whose lineages differ; a partition label the split refuses (unknown, sealed, never, no budget) or of
the wrong role; a rule file of any schema but betangel.rule/v1 (rule_schema_not_for_this_study: every study here
walks v1 views); a rule file whose when row uses one of this module's own codes (HARNESS_CODES) or a code the
registration's population.codes does not hold; a fixed price off the CLASSIC ladder. Its id is close-v1 or one of
EARLY_IDS (any other: id:not_supported), and Plan.study is the file's id. A new id's registration also holds
data.traded_volume.hour_rises, "not_counted", and data.partitions.read, "explore_only", and lists four trade
counts (EARLY_TRD_COUNTS); close-v1's holds neither key, so its key sets stay exact, and lists the three
(TRD_COUNTS). Each cell walks its orders to a pull time before the off, so every rule file has exactly one intent,
lapses and has a pull time at every time; close-v1's rule files select the favourite (select_not_favourite), a new
id's select the favourite or each_active. The answer is one frozen Plan, whose last fields default to close-v1's
values. A cell is one rule file at one of its times (Cell).

Population (record_code, then the walk): per cell, the first code that applies, in population_codes' order.
From the index record alone: movement.market_code's first twelve codes, through an adapter that holds the rule
file's market block and a minimum field of 0 (the field size is the rule file's own row); partition_not_registered
when the race date's label is not one the plan reads (Plan.labels_read: both labels the registration names for
close-v1, the explore label alone for a new id); then a flag code (bsp_market_false) when the rule file gates that
flag and its value as of t is exactly false. A gated flag with no value at t passes and is reported
(flags_missing). A record whose cells all have a code is never opened. In the walk: a view problem (as
lp.walk_market, never met for a cell that passed the record's gates); the code rulefile.decide gives for the view
at t (ladder_malformed first: a ladder entry, a trd entry or an rc runner id published at or before t could not be
read; under each_active, when no runner passes, the first runner's code); price_off_ladder when no order's price
is a CLASSIC price. A favourite cell with no code holds one order. An each_active cell with no code holds one order
on each runner that passes, each judged alone; an order whose price is off the CLASSIC ladder is not placed.

The view (view_of): the rulefile.RuleView of a movement.Moment. It holds one RunnerView per runner ACTIVE and
listed at t, and refuses (ValueError) unless the ids it is given are exactly the Moment's own active runners and
every price and size is an exact Fraction. So active_runners equals len(runners), which decide trusts.
market_open is 1 when the status as of t is OPEN and inPlay as of t is not true. spread_ticks is lp.spread_ticks.

The walk (walk): one pass over the decompressed primary member, following lp.walk_market's loop: the index's
reader and fold (market_index._messages, _checked_changes, _Market), replay.PriceFold, movement.LadderFold and
lp.TradedFold, messages sharing a pt as one batch, and a heap of timed events fired after the last batch
published at or before their time. It stops at the first message published after the last pull time of the cells
it walks; nothing later is parsed. A decide event captures movement's Moment at t (which refuses, ReplayProblem,
unless the fold equals the index at t), builds the view, asks rulefile.decide and places each order as one lp.Leg
per fill model: its queue is the model's fraction of the size shown at t at its price on its own side (a LAY
rests on atb, a BACK on atl), its base a copy of the runner's counted volume at t. Each batch after t up to and
including the pull time is judged by fills.py's rules. The hour switch: under a plan that does not count hour
rises (Plan.hour_rises_counted false: a new id), a batch whose pt lies in the after-the-hour seconds counts no
rise, so lp.TradedFold.count is not called and such a rise fills nothing by trade_through or volume_at_price, then
or later; quote_cross is judged as before. The best back and best lay just before a batch, which the second trade
count reads, are each resting runner's lp.top, kept between batches and read again only after a change that may
move that runner's ladder (_stale); with KEEP_TOPS false every batch reads them afresh, as before. A lapse order
is no longer judged once inPlay was true before a batch; it stays ended even if inPlay is later published false. A
null event captures the runner's quote and the market's status and inPlay at a time u drawn at placement from
metrics.HashStream with the registered seed and label, uniform over the whole milliseconds of (t, pull time]: one
draw per market and cell, which the orders of an each_active cell share. For a cell of an each_active rule file
the walk also counts the runners of the view at t by their own code (Walked.runner_codes, runner_codes).

Per cell the walk returns a code, one OrderRow (a favourite cell) or a tuple of OrderRows in selection-id order (an
each_active cell). A row holds no market id, no selection id and no clock time: the side and price; per model a
Fill (the queue at t, the fill kind or None, seconds from t to the fill, whether it came within each of the
registered seconds of t, in the after-the-hour seconds, in a batch that left the market not OPEN, in the batch that
turned the market in play, and for an unfilled order whether the pull or the lapse ended it: the lapse when inPlay
was true in force after some batch at or after t and before the pull time); the mid at t; the mid at u or the
reason it has none (NULL_UNDEFINED); the plan's counts of the runner's trd rises over (t, pull time]
(Plan.trd_counts: TRD_COUNTS, and for a new id a fourth, LEFT_OUT); whether some batch in (t, pull time] began with
no market status at all (no_status: a definition that carried none, or an image with no definition, came before
it; such a batch is not open, so nothing fills in it); the class; the BSP as written when the class is scored; the
partition label, the half-year and the race date (the cluster key); then, as last fields with defaults, the gap in
CLASSIC ticks from the runner's best back to its best lay at t (gap_ticks, None when either is missing or off the
ladder), whether t lies in the after-the-hour seconds (placed_in_the_hour), and per model whether its fill came in
the second or the third minute after a whole UTC hour on a Monday to Friday (fill_minute: 2, 3 or None).

betangel.rule/v2's walk (Plan.views V2; no registration sets a v2 field yet, and the Plan's defaults leave every
walk as it was). The view is view_of2's rulefile.RuleView2: view_of's fields by view_of's checks, then per runner
best_lay_size, back_depth_3 and lay_depth_3 (the sizes at the first three price points of its PRO books at t, read
by a partial sort, summed) and moves, one item per window of rulefile.windows(rule) in that order; and over the whole
field back_book_pct and lay_book_pct (100 x the sum of 1 / best price, None when some runner has none). Every value
put in it is an exact Fraction (view:not_exact otherwise). A window (from_s, to_s) is read from captures: one capture
event at each offset of Plan.windows that is at or before the walk's latest decision, shared by every cell, keeps the
Moment's quotes, whether the market was OPEN and not in play, and the runners ACTIVE and listed then (values, never a
reference into the folds); a null to_s ends at the decision's own Moment. Events are (time, rank, counter, kind,
payload), and at one ms a capture fires before a decision and a decision before a null (EVENTS), so a window that
ends at t reads the batch at t; every decision is pushed before any null, as before. A move is the mid at the end
over the mid at the start, exact, or None for the first of MOVE_UNDEFINED that holds; its removals run over (start,
t] and are read from the walk's own market fold at the decision, which holds nothing published after t. A window the
walk did not compute raises (view:move_window_missing): it is never read as None. Walked.move_undefined counts, per
cell and window, the runners of each reason and the moves that kept their value with a status other than OPEN inside
the window (suspension_in_window). Two fill switches are dormant by default. Under Plan.image_rises_counted false no
rise of an image batch is counted, then or later (the hour switch counts first), and each is counted as
IMAGE_LEFT_OUT, which the plan's trade counts must then list. Under Plan.through_needs_lowered a counted price beyond
an order's reaches fills.judge only when the batch lowered or deleted the level shown at that price on either side
(lp._Batch.levels, called before the ladder fold); a batch in which only that keeps a judged leg from filling by any
kind is one refusal. A v2 walk's rows also hold, per model, the fill's MINUTE_BUCKETS name (minutes), the refusals
by bucket (through_refused) and whether a close was possible (closable: the state after the fill's batch, or after
some later batch up to the pull, was OPEN and not in play; None unfilled); per order, the image batches met in (t,
pull time] and the image rises left out, by bucket (images), and whether inPlay was true by the pull
(in_play_before_pull); and its Fills say whether the fill came in an image batch, was a trade_through whose volume
through the price in its batch was below the stake, or a quote_cross in a batch that left the book crossed or
locked. A v1 walk leaves every one of these at its default, None. A plan with either switch on must build v2 views
(walk:fill_switch_without_v2_views), so that every refusal is counted on its row.

Classes (CLASSES), from facts that do not depend on a fill, the first that applies: movement.measure's five
(outcome_unknown, void_own_runner_removed, post_decision_removal, result_revised_after_close, missing_bsp); then
stream_malformed_after_placement when a ladder entry, a trd entry or an rc runner id published after t and up to
the pull time could not be read; else scored. A suspension, an early off or a revised schedule after t drops no
order. A removal classes the order and never re-prices it.

The metric (score, series): per GBP 1 of backer's stake, exact. g is a BACK's price / BSP - 1 and a LAY's
1 - price / BSP; net_closed is g - c x max(g, 0); net_held is the order held to the result. The stream cuts BSP to
two decimals, so every figure exists at two BSP ends (BSP_ENDS): as written and plus the step. The kill end is the
one that favours the order, the claim end the other (deciding_end); they decide apart only when the registered
share of scored BSPs are whole hundredths (ends_apart), else the as-written figure decides alone.

Part and rest (filled_part): a model's "filled" figures are taken over its filled scored orders only when the
unfilled ones number statistics.min_values; else over every scored order, flagged filled_is_all_placed. A size
gate always counts the model's own fills. The three models' fills lie inside one another, so the rule is also
applied across them (given_figures, given_together): one group's figures are given model by model, in
GIVEN_FIRST's order, and a model's only when no set of fewer than min_values orders can then be told apart by the
groups given; a figure held back keeps its counts (withheld, reason rest_not_given). figures gives the four
intent-to-treat figures of a group, gap the filled-minus-unfilled contrast, blind_outcome the spread and the
detectable effect the blind pass may give.

Statistics: CR1 by race date with t(G - 1) (movement.cr1_mean); the one-sided p for a mean above 0
(one_sided_p, cr1_test); the kill bound and the lower bound (bound); Holm (movement.holm); 100 values to give a
statistic and 200 on 15 race dates to test, bound or select (gate), read on the model each rule reads;
nearest-rank quantiles. The functions return unrounded floats; rounded gives the registered 6 places.

Selection (select_primaries) sees the explore label's rows and the test label's counts, nothing else. The null
(null_gate) can block a claim and never kills. Labels (verdicts): the first of VERDICTS that applies, per primary.

What this does not do: it reads nothing after the last pull time, models no latency, partial fill, re-pricing
or closing order, builds no table and writes nothing. Standard library only; it imports fills, rulefile,
partition, lp, movement, market_index, replay and metrics and, through them, the modules they import. Nothing
here is evidence of profitability.
"""

from collections import namedtuple
from dataclasses import dataclass, field as dataclass_field, replace     # record_code has a local named field
from fractions import Fraction
import heapq
import itertools
import math
import re

from . import fills, rulefile
from .lp import (BACK, CROSS, DAY_MS, FILL_KINDS, HOUR_MS, LAY, THROUGH, TIERS, TRADED_BASIS, TRADED_DIVISOR,
                 TRADED_KEY, TradedFold, _Batch, _rules, _seconds, Leg, after_the_hour, facing, mid, spread_ticks, top,
                 wide)
from .market_index import _as_of, _checked_changes, _last, _Market, _messages, _runner_id, _Unreadable, loads_strict
from .metrics import HashStream
from .movement import (ANCHOR, CLUSTER, DATE_RE, ELIGIBLE, ISSUED, LADDER_H, LADDER_NAMES, LADDERS, MARKET_CODES,
                       MAX_REGISTRATION_BYTES, MONTH_RE, QUANTILE_METHOD, SCHEMA, UNCERTAINTY_METHOD, LadderFold,
                       _codes, _fail, _int, _moment, _object, _rate, _text, _texts, active_ids, cr1_contrast,
                       cr1_mean, decision_time, holm, market_code, measure, nclv, nearest_rank, race_date,
                       registration_sha256, sample_sd, student_t_cdf, student_t_quantile, tick_index)
from .partition import EXPLORE, HALVES, HOLDOUT, PartitionError, file_bytes, parse_split
from .replay import PriceFold, ReplayProblem
from .rulefile import (EACH_ACTIVE, FAVOURITE, LAPSE, RuleError, RuleView, RuleView2, RunnerView, RunnerView2,
                       parse_rule)

STUDY_ID = "close-v1"
# The new ids: close-early-v1, and its registered fallback, one rule file each. They are parsed alike.
EARLY_IDS = ("close-early-v1", "close-early-fav-v1", "close-early-band-v1")
STUDY_IDS = (STUDY_ID,) + EARLY_IDS
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
SELECTOR_CODES = {FAVOURITE: (rulefile.NO_PRICED_ACTIVE_RUNNER, rulefile.FAVOURITE_TIE),
                  EACH_ACTIVE: (rulefile.NO_ACTIVE_RUNNER,)}
# The selectors a rule file may use: close-v1's walk is the favourite's alone; a new id's takes both.
SELECTORS = {STUDY_ID: (FAVOURITE,), **dict.fromkeys(EARLY_IDS, (FAVOURITE, EACH_ACTIVE))}
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
# A new id's fourth count: rises of the order's runner in an open batch in the after-the-hour seconds, at a price
# the batch also carries an atb or atl entry at, so that the counting rule would have counted them: what the hour
# switch left out (data.traded_volume.hour_rises).
LEFT_OUT = "rises_left_out_in_the_hour_seconds"
EARLY_TRD_COUNTS = TRD_COUNTS + (LEFT_OUT,)
# The image switch's count (Plan.image_rises_counted false): rises of the order's runner in an open image batch that
# the hour switch has not left out already, at a price the batch also carries an atb or atl entry at, so that the
# counting rule would have counted them. A plan with the switch on lists it in its trade counts.
IMAGE_LEFT_OUT = "rises_left_out_in_an_image_batch"
NOT_COUNTED, EXPLORE_ONLY = "not_counted", "explore_only"      # a new id's hour_rises and partitions.read
SECOND, THIRD = 2, 3                        # the minutes after a whole hour that a fill_minute names
# The views a plan's walk builds: v1's (view_of, rulefile.RuleView) or betangel.rule/v2's (view_of2,
# rulefile.RuleView2, and the row fields only a v2 walk fills).
V1, V2 = VIEWS = ("v1", "v2")
# Why a move of a v2 view (mid_ratio) has no value, the first that applies (window_moves). Each is counted per cell
# and window (Walked.move_undefined), never as a cell code; suspension_in_window is counted beside them, for a move
# that has its value although the market was not OPEN at some pt inside its window.
MOVE_UNDEFINED = ("anchor_not_known_at_start", "not_open_at_start", "not_open_at_end", "runner_not_active_at_start",
                  "removal_in_window", "not_two_sided_at_start", "not_two_sided_at_end", "spread_above_max_at_start",
                  "spread_above_max_at_end")
SUSPENSION_IN_WINDOW = "suspension_in_window"
MOVE_COUNTS = MOVE_UNDEFINED + (SUSPENSION_IN_WINDOW,)
# A v2 row's buckets of a pt (minute_bucket): minutes 0 to 5 after a weekday whole UTC hour, the rest of a weekday
# hour, and a weekend day.
MINUTE_BUCKETS = ("0", "1", "2", "3", "4", "5", "6-59", "weekend")
# What a v2 row counts by bucket: image batches met in (t, pull time] and the rises the image switch left out in
# them (OrderRow.images); per model, the level rule's refusals (OrderRow.through_refused counts these); and, read
# from the rows, the orders with a refusal still unfilled at the pull (orders_unfilled_after_through_refused).
IMAGE_BATCHES = "image_batches"
THROUGH_NOT_LOWERED = "through_refused_level_not_lowered"
UNFILLED_AFTER_REFUSAL = "orders_unfilled_after_through_refused"
VERDICTS = ("kill_optimistic", "underpowered", "edge_candidate", "measurement_pilot_candidate", "unresolved")
BLOCKED, FILLED_IS_ALL_PLACED = "claim_blocked_by_placebo", "filled_is_all_placed"
HOLM = "holm"
SELECT_MEASURE, SELECT_END, SELECT_TIE = "net_held", "claim_end", "later_time"
BSP_STEP = Fraction(1, 100)
# The two BSP values every figure is computed against, and what reads which (deciding_end).
AS_WRITTEN, PLUS_STEP = BSP_ENDS = ("bsp_as_written", "bsp_plus_step")
KILL_END, CLAIM_END = "kill_end", SELECT_END
G, NET_CLOSED, NET_HELD = MEASURES = ("g", "net_closed", SELECT_MEASURE)
# Why the null blocks a claim: it or the main mean over the same orders has no test; or it is too large.
NULL_BLOCKS = ("not_computed", "at_least_the_fraction_of_main")
COUNTS_ONLY, UNDERPOWERED = "counts_only", "underpowered"
REST_NOT_GIVEN = "rest_not_given"                   # a figure withheld so that a small group's cannot be worked out
# What part and rest withholds of a figure: every statistic, never a count.
STATISTICS = ("mean", "estimate", "se", "df", "t_critical", "ci_low", "ci_high", "sd", "bound")
# Part and rest across the models: whose figures are given first (the model a claim reads, the model a kill reads,
# then the one between them), and the two kinds of group a model has: what stands for its filled orders, and its
# fills after the first registered second.
GIVEN_FIRST = (fills.PESSIMISTIC, fills.OPTIMISTIC, fills.CENTRAL)
FILLED, LATER = "filled", "later"
FLOAT_PLACES, QUANTILE_PLACES = 6, 4
MAX_SECONDS = 3600
FILE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\.json")
TOP_KEYS = ("blind_pass", "data", "decided_by", "diagnostics", "doc", "exposure", "family", "fill_models", "grid",
            "id", "measurement", "not_measured", "null", "population", "registered_on", "registered_pass", "schema",
            "seeds", "selection", "statistics", "status", "summary", "uncertainty", "verdicts")
DATA_KEYS = ("duplicates", "end_month", "files", "ladders", "month", "partitions", "provider", "rules", "source",
             "split", "start_month", "tiers", "traded_volume")
CAPTURE, DECIDE, NULL = EVENTS = ("capture", "decide", "null")      # the walk's events, in rank order at one ms
# The walk keeps each resting runner's top of book between batches (_stale). False: every batch reads lp.top afresh,
# as before; the outputs are the same, and the tests walk both ways.
KEEP_TOPS = True

Cell = namedtuple("Cell", ("rule_id", "at_s", "pull_s"))
# The scored rows that stand for one model's filled orders, that model's own fills and their race dates (what a
# size gate counts), and whether the rows are every scored order (filled_is_all_placed).
Part = namedtuple("Part", ("rows", "fills", "fill_dates", "all_placed"))
Selection = namedtuple("Selection", ("eligible", "primaries"))
# One model's answer for one order (module docstring). within: one bool per Plan.within_s. The last three fields
# are a v2 walk's (None from a v1 walk): whether the fill came in an image batch, a trade_through whose through
# volume in its batch was below the stake, and a quote_cross in a batch that left the runner's book crossed or locked.
Fill = namedtuple("Fill", ("queue", "kind", "seconds", "within", "after_the_hour", "transition", "turn", "ended",
                           "image", "through_below_stake", "crossed"), defaults=(None, None, None))
# The fields from gap_ticks on have defaults, so a row can still be built from the thirteen older fields alone; the
# last five are a v2 walk's and stay None from a v1 walk (module docstring).
OrderRow = namedtuple("OrderRow", ("side", "price", "label", "half", "race_date", "klass", "bsp", "mid", "fills",
                                   "null_mid", "null_reason", "trd", "no_status", "gap_ticks", "placed_in_the_hour",
                                   "fill_minute", "minutes", "images", "through_refused", "closable",
                                   "in_play_before_pull"), defaults=(None, False, None, None, None, None, None, None))
# What a v2 walk keeps of one time before a decision (capture_of): t in ms, view_problem's code then (None with a
# view), the Moment's quotes, whether the market was OPEN and not in play, and the runners ACTIVE and listed then.
# Values only: never a reference into the folds, which keep changing.
Capture = namedtuple("Capture", ("t", "reason", "quotes", "opened", "active"))
_Cap = namedtuple("_Cap", ("max_spread_ticks",))           # what lp.wide reads, for a move's own spread cap
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
    the registration says are printed with every label. hour_rises_counted, explore_only and trd_counts default to
    close-v1's values: every rise in the after-the-hour seconds counts, a pass reads both labels (labels_read), and
    the trade counts are TRD_COUNTS. A new id's plan sets them from its registration. The last five fields are
    betangel.rule/v2's walk (module docstring), and their defaults leave every walk as it was: views V1 (view_of);
    windows (), the move windows whose times the walk captures (windows_of(rules) for a v2 plan);
    move_max_spread_ticks None, a move's spread cap in CLASSIC ticks, an int from 1 that a plan with windows must
    set; image_rises_counted True (False: the image switch); through_needs_lowered False (True: the level rule). A
    plan with either switch on must build v2 views (walk:fill_switch_without_v2_views), so that every refusal is
    counted on its row. No registration sets them yet: parse_registration leaves them at their defaults."""

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
    hour_rises_counted: bool = True         # data.traded_volume.hour_rises; a new id's is "not_counted"
    explore_only: bool = False              # data.partitions.read; a new id's is "explore_only"
    trd_counts: tuple = TRD_COUNTS          # diagnostics.trd_counts
    views: str = V1                         # V2: view_of2, the captures and the v2 row fields
    windows: tuple = ()                     # ((from_s, to_s), ...): the windows the walk captures
    move_max_spread_ticks: object = None    # an int from 1 when windows is not empty
    image_rises_counted: bool = True        # False: no rise of an image batch counts (the image switch)
    through_needs_lowered: bool = False     # True: a trade_through needs its level lowered (the level rule)

    @property
    def labels(self):
        """The two partition labels the registration names: (explore, test)."""
        return self.explore_label, self.test_label

    @property
    def labels_read(self):
        """The partition labels a pass reads: both labels, or the explore label alone (explore_only)."""
        return (self.explore_label,) if self.explore_only else self.labels

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
    selector's and its runner rows', then no_reference_price and price_off_ladder. issued is not in it. Which rows
    are market rows and which are runner rows is each rule file's schema's (rulefile.market_features and
    runner_features: v1's tuples for a v1 file, as before)."""
    flags = tuple(code for name, _, code in FLAG_GATES if any(getattr(rule.market, name) is True for rule in rules))
    decided = []
    for rule in rules:
        market = [row.code for row in rule.when if row.feature in rulefile.market_features(rule)]
        runner = [row.code for row in rule.when if row.feature in rulefile.runner_features(rule)]
        for code in market + list(SELECTOR_CODES.get(rule.select, ())) + runner:
            if code not in decided:
                decided.append(code)
    return RECORD_CODES + flags + (rulefile.LADDER_MALFORMED,) + tuple(decided) + (rulefile.NO_REFERENCE_PRICE,
                                                                                    PRICE_OFF_LADDER)


def windows_of(rules):
    """The distinct move windows of rule files, rule by rule, each in rulefile.windows' order: what a v2 plan's
    walk captures (Plan.windows). Never sorted; () when no rule file has a move row."""
    found = []
    for rule in rules:
        for window in rulefile.windows(rule):
            if window not in found:
                found.append(window)
    return tuple(found)


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


def _split(data, expected, raw, read=False):
    """The Split of the split file's bytes and the registration's (explore, test) labels. read: whether
    data.partitions holds the key read (a new id's), which must then be "explore_only"."""
    if not isinstance(raw, bytes) or registration_sha256(raw) != expected[1]:
        _fail("data.split", "sha256_mismatch")
    try:
        split = parse_split(raw)
    except PartitionError as error:
        _fail("data.split", str(error))
    partitions = _object(data["partitions"], "data.partitions",
                         ("explore", "read", "rule", "test") if read else ("explore", "rule", "test"))
    _text(partitions["rule"], "data.partitions.rule")
    if read:
        _text(partitions["read"], "data.partitions.read", EXPLORE_ONLY)
    for key, role in (("explore", EXPLORE), ("test", HOLDOUT)):
        problem = split.label_problem(partitions[key])
        if problem is not None:
            _fail(f"data.partitions.{key}", problem)
        if split.role(partitions[key]) != role:
            _fail(f"data.partitions.{key}", f"role_not_{role}")
    return split, partitions["explore"], partitions["test"]


def _rule_files(named, raws, codes, selectors=(FAVOURITE,)):
    """The Rules of the named rule files' bytes, after the checks of the module docstring; codes is the
    registration's population.codes as written, selectors the study's (SELECTORS)."""
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
        if rule.schema != rulefile.SCHEMA:
            _fail(place, "rule_schema_not_for_this_study")      # every study here walks v1 views (RuleView)
        if name != f"{rule.id}.json":
            _fail(place, "name_not_the_rule_id")
        if rule.select not in selectors:
            _fail(place, "select_not_favourite")    # close-v1's; a new id takes every selector rulefile does
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
    if _text(top["id"], "id") not in STUDY_IDS:
        _fail("id", "not_supported")
    early = top["id"] in EARLY_IDS              # a new id: the hour switch, the explore label alone, four counts
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
                                                                    "rule") + (("hour_rises",) if early else ()))
    if early:
        _text(traded["hour_rises"], "data.traded_volume.hour_rises", NOT_COUNTED)
    _text(traded["key"], "data.traded_volume.key", TRADED_KEY)
    _text(traded["basis"], "data.traded_volume.basis", TRADED_BASIS)
    _text(traded["rule"], "data.traded_volume.rule")
    if _int(traded["divisor"], "data.traded_volume.divisor", 1, 100) != TRADED_DIVISOR:
        _fail("data.traded_volume.divisor", "not_the_basis")
    months = (data["start_month"], data["end_month"])
    if not all(isinstance(month, str) and MONTH_RE.fullmatch(month) for month in months) or months[0] > months[1]:
        _fail("data.months", "malformed")
    split, explore, test = _split(data, split_named, split_raw, read=early)

    population = _object(top["population"], "population", ("codes", "codes_rule", "price_ladder", "quotes", "source"))
    for key in ("codes_rule", "price_ladder", "quotes", "source"):
        _text(population[key], f"population.{key}")
    rules = _rule_files(rules_named, rules_raw, _texts(population["codes"], "population.codes"), SELECTORS[top["id"]])
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
    ends = _object(measurement["bsp_ends"], "measurement.bsp_ends", ("names", "rule", "step",
                                                                     "whole_hundredths_share"))
    _codes(ends["names"], "measurement.bsp_ends.names", BSP_ENDS)
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

    null = _object(top["null"], "null", ("alpha", "blocked", "blocks", "fraction_of_main", "label",
                                         "max_spread_ticks", "rule", "undefined_reasons"))
    _text(null["label"], "null.label", NULL_LABEL)
    _text(null["blocked"], "null.blocked", BLOCKED)
    _codes(null["blocks"], "null.blocks", NULL_BLOCKS)
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
    trd_counts = _codes(diagnostics["trd_counts"], "diagnostics.trd_counts", EARLY_TRD_COUNTS if early else TRD_COUNTS)
    plan = Plan(
        sha256=registration_sha256(raw), study=top["id"], lineage=rules[0].lineage, split=split, rules=rules,
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
        label_note=verdicts["printed_with"], exposure_note=exposure["printed_with_every_label"],
        hour_rises_counted=not early, explore_only=early, trd_counts=trd_counts)
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
    (None, the gated flags with no true or false value as of t): the cell reaches the book. A record whose label is
    not one the plan reads (Plan.labels_read) is partition_not_registered here, at every cell its history gates let
    through; this is the one function the runner's record facts and the walk both ask."""
    rule = plan.rule(cell.rule_id)
    market = rule.market
    gate = _Gate(plan.tiers, plan.start_month, plan.end_month, market.countries, market.event_type_id,
                 market.market_type, market.betting_type, market.number_of_winners, 0)
    code = market_code(gate, record, cell.at_s)
    if code is not None:
        return code, ()
    if race_label(plan, record)[1] not in plan.labels_read:
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
    return RuleView(at_s=moment.offset_s, market_open=1 if _opened(moment.view) else 0, active_runners=len(runners),
                    ladder_malformed=bool(moment.ladder_malformed or malformed), runners=tuple(runners))


def _opened(view):
    """Whether a view's market is OPEN and not in play as of its time (a RuleView's market_open)."""
    return view.field("status") == fills.OPEN and view.field("inPlay") is not True


def capture_of(moment):
    """The Capture of a movement.Moment: with no view, its reason and nothing else."""
    if moment.view is None:
        return Capture(moment.t, moment.reason, (), False, ())
    return Capture(moment.t, None, moment.quotes, _opened(moment.view), moment.active())


def _quote_in(quotes, selection_id):
    """(best back, best lay) of one runner in a Moment's quotes, as movement.Moment.quote reads them."""
    for rid, back, lay in quotes:
        if rid == selection_id:
            return back, lay
    return None, None


def _move(start, end, selection_id, removed, cap):
    """(ratio, None) or (None, the first MOVE_UNDEFINED reason) of one runner's move from the Capture start to the
    Capture end: the mid at the end over the mid at the start (lp.mid), both exact. removed: whether some runner of
    the market was removed in the window (window_moves); cap: the move's spread cap in CLASSIC ticks (lp.wide).
    ValueError when the end has no view, which no window of a decision with a view can give."""
    if start.reason is not None:
        return None, "anchor_not_known_at_start"
    if not start.opened:
        return None, "not_open_at_start"
    if end.reason is not None:
        raise ValueError("view:no_view_at_the_window_end")
    if not end.opened:
        return None, "not_open_at_end"
    if selection_id not in start.active:
        return None, "runner_not_active_at_start"
    if removed:
        return None, "removal_in_window"
    quotes = (_quote_in(start.quotes, selection_id), _quote_in(end.quotes, selection_id))
    for quote, reason in zip(quotes, ("not_two_sided_at_start", "not_two_sided_at_end")):
        if mid(quote) is None:
            return None, reason
    for quote, reason in zip(quotes, ("spread_above_max_at_start", "spread_above_max_at_end")):
        if wide(_Cap(cap), quote):
            return None, reason
    return mid(quotes[1]) / mid(quotes[0]), None


def window_moves(moment, selection_ids, captures, windows, removals, cap):
    """{window: {selection id: (ratio, None) or (None, reason)}} of the runners selection_ids at a decision's
    Moment, for every window of windows (module docstring). captures: {window: (start Capture, end Capture, or None
    for a window whose to_s is null: its end is the decision's own Moment)}; removals: the pt of every removal the
    walk's market fold holds at the decision, which holds nothing published after t. A window's removals run from
    its start to t, not to its end: the view at t is the field that is left."""
    now, found = capture_of(moment), {}
    for window in windows:
        start, end = captures[window]
        removed = any(start.t < pt <= moment.t for pt in removals)
        found[window] = {sid: _move(start, now if end is None else end, sid, removed, cap) for sid in selection_ids}
    return found


def move_counts(moment, selection_ids, captures, windows, removals, statuses, cap):
    """{window: {MOVE_COUNTS name: runners}} of window_moves' answer: each undefined move by its reason, and each
    defined move whose window held a market status other than OPEN at some pt in (start, end] (statuses: the walk's
    market fold's status timeline at the decision)."""
    found = {}
    for window, moves in window_moves(moment, selection_ids, captures, windows, removals, cap).items():
        start, end = captures[window]
        until = moment.t if end is None else end.t
        suspended = any(start.t < pt <= until and value != fills.OPEN for pt, value in statuses)
        counts = dict.fromkeys(MOVE_COUNTS, 0)
        for _, reason in moves.values():
            if reason is not None:
                counts[reason] += 1
            elif suspended:
                counts[SUSPENSION_IN_WINDOW] += 1
        found[window] = counts
    return found


def _points(book, pick):
    """The first three (price, size) points of a PRO book {price: (price, size)}, best first: pick is heapq.nlargest
    for a back book, heapq.nsmallest for a lay book (a partial sort, never the whole book's)."""
    return [book[price] for price in pick(3, book)]


def _book_pct(prices):
    """100 x the sum of 1 / price over the field's best prices on one side, or None when the field is empty or some
    runner has no price there."""
    if not prices or any(price is None for price in prices):
        return None
    return 100 * sum(1 / price for price in prices)


def view_of2(moment, active, malformed, ladders, captures, windows, removals, cap):
    """The rulefile.RuleView2 of a movement.Moment with a view, for a rule whose move windows are windows
    (rulefile.windows(rule)): view_of's fields, by view_of's own checks, then per runner best_lay_size (the size at
    its best lay), back_depth_3 and lay_depth_3 (the sizes at the first three price points of its atb and atl books
    in ladders, best first, summed) and moves (window_moves, one item per window, in windows' order), and over the
    whole field back_book_pct and lay_book_pct. ladders is the walk's movement.LadderFold at t, keyed by price;
    captures, removals and cap as window_moves takes them. ValueError: view:move_window_missing unless every window
    is in captures (a window the walk did not compute is never read as None); view:move_cap_not_set; view:not_exact
    for a value put in the view that is not an exact Fraction; view:ladders_not_the_moments when the books' best
    points are not the Moment's quotes; view:ladders_not_keyed_by_price."""
    view = view_of(moment, active, malformed)
    if ladders.level_keyed:
        raise ValueError("view:ladders_not_keyed_by_price")
    if any(window not in captures for window in windows):
        raise ValueError("view:move_window_missing")
    if windows and (type(cap) is not int or cap < 1):
        raise ValueError("view:move_cap_not_set")
    moves = window_moves(moment, [item.selection_id for item in view.runners], captures, windows, removals, cap)
    runners = []
    for item in view.runners:
        sid = item.selection_id
        back_book, lay_book = ladders.books.get(sid, ({}, {}))
        backs, lays = _points(back_book, heapq.nlargest), _points(lay_book, heapq.nsmallest)
        if ((backs[0] if backs else None), (lays[0] if lays else None)) != moment.quote(sid):
            raise ValueError("view:ladders_not_the_moments")
        lay_size = lays[0][1] if lays else None
        back_3 = sum(size for _, size in backs) if backs else None
        lay_3 = sum(size for _, size in lays) if lays else None
        found = tuple((window, moves[window][sid][0]) for window in windows)
        if any(value is not None and type(value) is not Fraction
               for value in (lay_size, back_3, lay_3) + tuple(ratio for _, ratio in found)):
            raise ValueError("view:not_exact")
        runners.append(RunnerView2(*item, lay_size, back_3, lay_3, found))
    books = (_book_pct([item.best_back for item in view.runners]), _book_pct([item.best_lay for item in view.runners]))
    if any(value is not None and type(value) is not Fraction for value in books):
        raise ValueError("view:not_exact")
    return RuleView2(at_s=view.at_s, market_open=view.market_open, active_runners=view.active_runners,
                     ladder_malformed=view.ladder_malformed, runners=tuple(runners), back_book_pct=books[0],
                     lay_book_pct=books[1])


# ---------------------------------------------------------------------------------------------------------------
# The walk to the pull

@dataclass(frozen=True)
class Walked:
    """outcomes {Cell: a population code, the OrderRow of an issued favourite cell, or the tuple of OrderRows of an
    issued each_active cell} for every cell of the plan; flags_missing {Cell: the gated flags with no value at t} for
    every cell that passed the record's gates; replayed, whether the member was read; runner_codes {Cell:
    runner_codes' counts} for every cell of an each_active rule file whose view at t was built and whose market rows
    and selector held (B8); move_undefined {Cell: move_counts' answer} for every cell whose v2 view at t was built
    (a v2 plan's only)."""

    outcomes: dict
    flags_missing: dict
    replayed: bool
    runner_codes: dict = dataclass_field(default_factory=dict)
    move_undefined: dict = dataclass_field(default_factory=dict)


class _Order:
    """One placed order while it is walked: its cell, the rulefile.Intent, t and the pull time in ms, one lp.Leg per
    model, the runners ACTIVE and listed at t, the mid at t, the gap in ticks at t, and what the walk fills in; under
    a v2 plan also per model whether a close was possible (closable), the v2 Fill fields (fill), the level rule's
    refusals by bucket (refused), and the image batches and image rises left out by bucket (images)."""

    __slots__ = ("cell", "intent", "t", "end", "legs", "active", "mid", "lapsed", "transition", "null", "trd",
                 "no_status", "gap", "closable", "fill", "refused", "images")

    def __init__(self, cell, intent, t, end, legs, active, centre, names, gap, v2=False):
        self.cell, self.intent, self.t, self.end, self.legs, self.active = cell, intent, t, end, legs, active
        self.mid, self.lapsed, self.transition, self.null = centre, False, {}, None
        self.trd, self.no_status, self.gap = dict.fromkeys(names, 0), False, gap
        self.closable, self.fill = {}, {}
        self.refused = {model: dict.fromkeys(MINUTE_BUCKETS, 0) for model in legs} if v2 else None
        self.images = {name: dict.fromkeys(MINUTE_BUCKETS, 0) for name in (IMAGE_BATCHES, IMAGE_LEFT_OUT)} if v2 \
            else None


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


def minute_after_the_hour(pt):
    """SECOND or THIRD when ms pt lies in the second or the third minute after a whole UTC hour on a Monday to
    Friday (UTC, by lp.after_the_hour's day rule), else None: the check of the after-the-hour seconds, where a
    restatement sent later than they reach would show."""
    minute = pt % HOUR_MS // 60_000 + 1
    return minute if minute in (SECOND, THIRD) and (pt // DAY_MS + 3) % 7 < 5 else None


def minute_bucket(pt):
    """The MINUTE_BUCKETS name of ms pt: "0" to "5" for minutes 0 to 5 after a whole UTC hour on a Monday to Friday
    (minute k runs from :0k:00 to :0k:59), "6-59" for the rest of such an hour, "weekend" on a Saturday or a Sunday
    (UTC, by lp.after_the_hour's day rule). Minutes count from 0 here; minute_after_the_hour counts from 1."""
    if (pt // DAY_MS + 3) % 7 >= 5:
        return MINUTE_BUCKETS[-1]
    minute = pt % HOUR_MS // 60_000
    return MINUTE_BUCKETS[minute] if minute <= 5 else MINUTE_BUCKETS[6]


def orders_unfilled_after_through_refused(rows, model):
    """How many v2 rows had at least one level-rule refusal under model and were still unfilled under it at the
    pull: the only orders whose lost fills can lean the filled mean optimistic (read from OrderRow.through_refused
    and the fill, so it needs no field of its own)."""
    return sum(1 for row in rows if sum(row.through_refused[model].values()) and row.fills[model].kind is None)


def runner_codes(view, rule):
    """B8 of an each_active rule file at one view: {code: runners} over the view's runners, each by the code
    rulefile.decide gives for a view that keeps the market's fields and holds that runner alone (the code of its
    first failing runner row, or no_reference_price), price_off_ladder when its order's price is not a CLASSIC
    price, else issued (ordered on). {} unless the market rows and the selector hold: decide gives no_active_runner
    for the view with no runner (it reached the selector), and the view has a runner."""
    if not view.runners or rulefile.decide(replace(view, runners=()), rule).code != rulefile.NO_ACTIVE_RUNNER:
        return {}
    found = {}
    for runner in view.runners:
        decision = rulefile.decide(replace(view, runners=(runner,)), rule)
        code = decision.code
        if code is None:
            code = ISSUED if all(tick_index(intent.price) is not None for intent in decision.intents) \
                else PRICE_OFF_LADDER
        found[code] = found.get(code, 0) + 1
    return found


def _row(plan, record, placed, dated, malformed, in_play):
    """The OrderRow of one walked order (module docstring). dated: race_label's answer; malformed: the pts of
    unreadable ladder or trd entries; in_play: the market's inPlay timeline as far as it was read."""
    intent, t, end = placed.intent, placed.t, placed.end
    klass, bsp = measure(record, t, intent.selection_id, placed.active)
    if klass == ELIGIBLE:
        klass = MALFORMED_AFTER if any(t < pt <= end for pt in malformed) else SCORED
    # The value in force after each pt batch, as the walk reads it: a batch that publishes inPlay true and then
    # false leaves it false.
    lapsed = intent.persist == LAPSE and (_as_of(in_play, t) is True
                                          or any(t < pt < end and _as_of(in_play, pt) is True for pt, _ in in_play))
    v2 = plan.views == V2
    found = {}
    for model, leg in placed.legs.items():
        if leg.fill_pt is None:
            found[model] = Fill(leg.queue, None, None, (False,) * len(plan.within_s), False, False, False,
                                LAPSED if lapsed else PULLED, *((False,) * 3 if v2 else ()))
            continue
        took = leg.fill_pt - t
        found[model] = Fill(leg.queue, leg.fill_kind, took / 1000, tuple(took <= s * 1000 for s in plan.within_s),
                            after_the_hour(leg.fill_pt, plan.after_the_hour_s), *placed.transition[model], None,
                            *(placed.fill[model] if v2 else ()))
    null_mid, null_reason = _null(plan, placed.null)
    day, label, half = dated
    minutes = {model: None if leg.fill_pt is None else minute_after_the_hour(leg.fill_pt)
               for model, leg in placed.legs.items()}
    more = {}
    if v2:                                      # a v1 walk leaves these at their defaults
        more = dict(minutes={model: None if leg.fill_pt is None else minute_bucket(leg.fill_pt)
                             for model, leg in placed.legs.items()},
                    images={name: dict(counts) for name, counts in placed.images.items()},
                    through_refused={model: dict(counts) for model, counts in placed.refused.items()},
                    closable={model: None if leg.fill_pt is None else placed.closable[model]
                              for model, leg in placed.legs.items()},
                    in_play_before_pull=_as_of(in_play, t) is True or any(t < pt <= end and _as_of(in_play, pt) is True
                                                                          for pt, _ in in_play))
    return OrderRow(side=intent.side, price=intent.price, label=label, half=half, race_date=day, klass=klass,
                    bsp=bsp if klass == SCORED else None, mid=placed.mid, fills=found, null_mid=null_mid,
                    null_reason=null_reason, trd=dict(placed.trd), no_status=placed.no_status, gap_ticks=placed.gap,
                    placed_in_the_hour=after_the_hour(t, plan.after_the_hour_s), fill_minute=minutes, **more)


def _stale(tops, change):
    """Forget the kept lp.top ({runner: top}, walk) of every runner whose ladder one market change may move; called
    before movement.LadderFold folds the change. LadderFold.change moves a runner's books only through an rc entry
    that names it (by market_index._runner_id, as LadderFold reads the id) or through an image, which clears every
    book it does not carry. A definition moves none, but an image and a definition forget every runner all the same:
    a refresh too many costs one lp.top, a refresh too few could change an output."""
    if change.get("img") or "marketDefinition" in change:
        tops.clear()
        return
    for item in change.get("rc") or ():
        tops.pop(_runner_id(item.get("id")), None)


def walk(stream, record, plan, limits):
    """The Walked of one index record: every cell's code from the record alone and, when some cell reaches the
    book, the rest from the record's primary member's decompressed stream (module docstring). stream is not read
    when no cell reaches the book. ReplayProblem when the stream is not what the index says; ValueError for a plan
    whose betangel.rule/v2 fields do not fit together (walk:...) and for a view that cannot be built (view:...)."""
    if plan.views not in VIEWS:
        raise ValueError("walk:views_not_supported")
    if plan.windows and plan.views != V2:
        raise ValueError("walk:windows_without_v2_views")
    if plan.windows and (type(plan.move_max_spread_ticks) is not int or plan.move_max_spread_ticks < 1):
        raise ValueError("walk:move_max_spread_ticks_not_set")
    if not plan.image_rises_counted and IMAGE_LEFT_OUT not in plan.trd_counts:
        raise ValueError("walk:trd_counts_without_the_image_count")
    if plan.views != V2 and (not plan.image_rises_counted or plan.through_needs_lowered):
        raise ValueError("walk:fill_switch_without_v2_views")   # a refusal or an image is counted on v2 rows only
    v2 = plan.views == V2
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
    # Events are (time, rank, counter, kind, payload): at one ms a capture fires first, then a decision, then a
    # null (EVENTS). Every decision is pushed before any null, as before ranks, so old outputs do not move.
    events = [(off + cell.at_s * 1000, EVENTS.index(DECIDE), next(order), DECIDE, cell) for cell in cells]
    latest = max(time for time, *_ in events)
    for offset in sorted({offset for window in plan.windows for offset in window if offset is not None}):
        if off + offset * 1000 <= latest:       # one capture a time, shared by every cell and window that reads it
            events.append((off + offset * 1000, EVENTS.index(CAPTURE), next(order), CAPTURE, offset))
    heapq.heapify(events)
    market, prices, ladders, traded = None, PriceFold(), LadderFold(plan.ladders[record["provenance"]["plan"]]), \
        TradedFold()
    orders, runners_by_code, tops = [], {}, {}  # tops: {runner: lp.top} as of the last change folded (_stale)
    captured, moves_by_cell = {}, {}            # captured: {offset: Capture}

    def state():                                # (status, inPlay) in force after every message applied so far
        if market is None:
            return None, None
        return _last(market.fields.get("status")), _last(market.fields.get("inPlay"))

    def view_at(cell, moment, active, rule, malformed):
        if not v2:
            return view_of(moment, active, malformed)
        # The windows whose captures have fired; the market fold holds nothing published after t (M6).
        computed = {window: (captured[window[0]], None if window[1] is None else captured[window[1]])
                    for window in plan.windows
                    if window[0] in captured and (window[1] is None or window[1] in captured)}
        wanted, removals = rulefile.windows(rule), tuple(item["pt"] for item in market.removals())
        view = view_of2(moment, active, malformed, ladders, computed, wanted, removals, plan.move_max_spread_ticks)
        moves_by_cell[cell] = move_counts(moment, [item.selection_id for item in view.runners], computed, wanted,
                                          removals, market.fields.get("status", []), plan.move_max_spread_ticks)
        return view

    def decide(time, cell):
        moment = _moment(record, market, prices, ladders, cell.at_s, time)
        if moment.view is None:
            return moment.reason
        active, rule = active_ids(record, time), plan.rule(cell.rule_id)
        view = view_at(cell, moment, active, rule, any(pt <= time for pt in traded.malformed))
        decision = rulefile.decide(view, rule)
        if rule.select == EACH_ACTIVE:
            found = runner_codes(view, rule)
            if found:
                runners_by_code[cell] = found
        if decision.code is not None:
            if decision.code not in plan.codes:
                raise ValueError("resting:code_not_in_population_codes")
            return decision.code
        # Every intent is an order: one under the favourite (the plan's rule files have one intent), one per runner
        # that passes under each_active, each judged alone. An order whose price is off the CLASSIC ladder is not
        # placed; the cell's code is price_off_ladder only when no order is left.
        intents = sorted((intent for intent in decision.intents if tick_index(intent.price) is not None),
                         key=lambda intent: intent.selection_id)
        if not intents:
            return PRICE_OFF_LADDER
        end = off + intents[0].pull_s * 1000    # one pull time a cell
        label = NULL_LABEL.format(market_id=market_id, at_s=cell.at_s, rule_id=cell.rule_id)
        u = time + 1 + HashStream(plan.null_seed, label).below(end - time)     # one draw per market and cell
        gaps = {runner.selection_id: runner.spread_ticks for runner in view.runners}
        for intent in intents:
            sid = intent.selection_id
            back_book, lay_book = ladders.books.get(sid, ({}, {}))
            shown = (back_book if intent.side == LAY else lay_book).get(intent.price)    # a LAY rests on the back side
            shown, base = shown[1] if shown else Fraction(0), dict(traded.counted.get(sid, {}))
            legs = {model: Leg(cell.at_s, model, intent.side, sid, intent.price,
                               fills.queue_ahead(plan.queue_fractions[model], shown), time, end, base)
                    for model in plan.models}
            placed = _Order(cell, intent, time, end, legs, active, _mid(plan, moment.quote(sid))[0], plan.trd_counts,
                            gaps[sid], v2)
            heapq.heappush(events, (u, EVENTS.index(NULL), next(order), NULL, placed))
            orders.append(placed)
        return None

    def handle(time, kind, payload):
        if kind == CAPTURE:                     # values only, taken after the last batch at or before its time
            captured[payload] = capture_of(_moment(record, market, prices, ladders, payload, time))
        elif kind == DECIDE:
            outcomes[payload] = decide(time, payload)
        else:
            payload.null = (top(ladders, payload.intent.selection_id), *state())

    def fire(limit):                            # every event before limit (None: every event)
        while events and (limit is None or events[0][0] < limit):
            time, _, _, kind, payload = heapq.heappop(events)
            handle(time, kind, payload)

    def close(pt, batch, prior, before):        # one pt batch is complete: count its trades, then fill
        was_open, was_in_play = fills.is_open(before[0]), before[1] is True     # (status, inPlay) before the batch
        hour = after_the_hour(pt, plan.after_the_hour_s)                        # read first: the hour switch
        switched = hour and not plan.hour_rises_counted
        imaged = batch.image and not plan.image_rises_counted                   # the image switch
        counted, dropped, left_image, volume = {}, {}, {}, {}
        for rid, price, rise in fills.counted_rises(batch, was_open):
            if switched:                        # never counted, then or later: traded.count is not called
                dropped.setdefault(rid, set()).add(price)
                continue
            if imaged:                          # the same, for a rise of an image batch the hour switch let through
                left_image.setdefault(rid, set()).add(price)
                continue
            traded.count(rid, price, rise)
            counted.setdefault(rid, set()).add(price)
            volume[rid, price] = volume.get((rid, price), 0) + rise
        after = state()
        left = (not fills.is_open(after[0]), after[1] is True and not was_in_play)  # not OPEN now; turned in play
        closable = fills.is_open(after[0]) and after[1] is not True     # C4: read from after itself, not from left
        bucket = minute_bucket(pt) if v2 else None
        for placed in orders:
            if not placed.t < pt <= placed.end:
                continue
            placed.no_status = placed.no_status or before[0] is None
            intent = placed.intent
            sid, now = intent.selection_id, counted.get(intent.selection_id, ())
            out, out_image = dropped.get(sid, ()), left_image.get(sid, ())
            if v2 and batch.image:
                placed.images[IMAGE_BATCHES][bucket] += 1
            for rid, price, _ in batch.rises:
                if rid != sid:
                    continue
                if not was_open:
                    placed.trd[TRD_COUNTS[2]] += 1
                elif price in now:
                    placed.trd[TRD_COUNTS[0]] += 1
                elif price in out:              # empty unless the switch is on and the batch is in the hour seconds
                    placed.trd[LEFT_OUT] += 1
                elif price in out_image:        # empty unless the image switch is on and the batch is an image
                    placed.trd[IMAGE_LEFT_OUT] += 1
                    if v2:
                        placed.images[IMAGE_LEFT_OUT][bucket] += 1
                elif not hour and any(quote is not None and quote[0] == price for quote in prior[sid]):
                    placed.trd[TRD_COUNTS[1]] += 1
            placed.lapsed = placed.lapsed or (was_in_play and intent.persist == LAPSE)
            shown = None
            if (batch.image or sid in batch.changed) and any(leg.live(pt) for leg in placed.legs.values()):
                shown = facing(ladders, placed.legs[plan.models[0]])     # one side and price for every model
            at_price = traded.counted.get(sid, {}).get(intent.price, 0)
            for model, leg in placed.legs.items():
                if v2 and leg.fill_pt is not None and leg.fill_pt < pt:    # a later batch up to the pull time
                    placed.closable[model] = placed.closable[model] or closable
                if not fills.judged(leg, pt, was_open, placed.lapsed, intent.persist):
                    continue
                kinds = now
                if plan.through_needs_lowered:  # the level rule: a price beyond the leg's needs its level lowered
                    kinds = {price for price in now if not leg.beyond(price) or (sid, price) in batch.lowered}
                kind = fills.judge(leg, intent.stake, plan.divisor, kinds, at_price, shown)
                if v2 and kind is None and any(map(leg.beyond, now)) and not any(map(leg.beyond, kinds)):
                    placed.refused[model][bucket] += 1      # one refusal: no fill by any kind in this batch
                if kind is not None:
                    leg.fill_pt, leg.fill_kind, placed.transition[model] = pt, kind, left
                    if v2:                      # S6: the volume through the price, and the book the batch left
                        below = kind == THROUGH and sum(volume[sid, price] for price in kinds if leg.beyond(price)) \
                            / plan.divisor < intent.stake
                        back, lay = top(ladders, sid)
                        crossed = kind == CROSS and back is not None and lay is not None and back[0] >= lay[0]
                        placed.closable[model], placed.fill[model] = closable, (batch.image, below, crossed)

    # A batch no order's window holds is folded but not kept: fills and the counts read counted volume only as its
    # rise since t, and only in (t, pull time].
    unreadable_from, applied, batch, pending, prior, fresh = None, None, None, None, {}, False
    before = (None, None)
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
                close(batch, pending, prior, before)
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
            if KEEP_TOPS:                       # read again only where a change since may have moved the ladder
                for sid in runners:
                    if sid not in tops:
                        tops[sid] = top(ladders, sid)
                prior = {sid: tops[sid] for sid in runners}
            else:
                prior = {sid: top(ladders, sid) for sid in runners}
            pending = _Batch() if runners else None
            before = state()
        for mid_, change in checked:
            if mid_ != market_id:
                continue
            if market is None:
                market = _Market(market_id, pt)
            market.change(pt, change)
            prices.change(pt, change)
            if tops:                            # before the fold, so that no kept top is older than the books
                _stale(tops, change)
            if pending is not None and plan.through_needs_lowered:
                pending.levels(ladders, change)     # before the fold, as lp.walk_market: which levels it lowered
            ladders.change(pt, change)
            moved = traded.change(pt, change)
            if pending is not None:
                pending.note(ladders.keys, change, moved)
        applied = pt
    if pending is not None:
        close(batch, pending, prior, before)
    if unreadable_from is not None and unreadable_from > until:
        unreadable_from = None
    if unreadable_from is None:
        fire(None)
    else:
        if record["evaluator_only_outcome"]["read_complete"] is True:
            raise ReplayProblem(f"replay:{market_id}:unreadable_where_the_index_read")
        fire(unreadable_from)
        if any(kind == DECIDE for _, _, _, kind, _ in events):  # the index says each walked cell has a view at t
            raise ReplayProblem(f"replay:{market_id}:unreadable_before_view_ends")
    malformed = sorted(set(ladders.malformed) | set(traded.malformed))
    in_play = market.fields.get("inPlay", []) if market is not None else []
    rows = {}
    for placed in orders:                       # in placement order: a cell's orders by selection id
        rows.setdefault(placed.cell, []).append(_row(plan, record, placed, dated, malformed, in_play))
    for cell, found in rows.items():            # a favourite cell keeps its one row; an each_active cell, a tuple
        outcomes[cell] = tuple(found) if plan.rule(cell.rule_id).select == EACH_ACTIVE else found[0]
    return Walked(outcomes=outcomes, flags_missing=missing, replayed=True, runner_codes=runners_by_code,
                  move_undefined=moves_by_cell)


# ---------------------------------------------------------------------------------------------------------------
# The metric

def score(plan, side, price, bsp, what=G):
    """One exact figure of an order at price against one BSP value, per GBP 1 of backer's stake (registration
    measurement): g, a BACK's price / BSP - 1 and a LAY's 1 - price / BSP; net_closed, g - c x max(g, 0);
    net_held, a BACK's g - c x (price - 1) / BSP (movement.nclv) and a LAY's g - c x (1 - 1 / BSP). Every argument
    is a Fraction and so is the answer. The price may be a mid: the mid is then valued as the order is."""
    g = price / bsp - 1 if side == BACK else 1 - price / bsp
    if what == G:
        return g
    if what == NET_CLOSED:
        return g - plan.commission * max(g, 0)
    if what != NET_HELD:
        raise ValueError("score:figure_unknown")
    return nclv(price, bsp, plan.commission) if side == BACK else g - plan.commission * (1 - 1 / bsp)


def deciding_end(side, purpose, apart):
    """The BSP end a kill (KILL_END) or a claim (CLAIM_END) reads for a side. The kill end is the one that favours
    the order: a LAY gains from a higher BSP, a BACK from a lower one. The claim end is the other. apart is the
    registered switch (ends_apart); when it is false the as-written figure decides alone."""
    if not apart:
        return AS_WRITTEN
    return PLUS_STEP if (side == LAY) == (purpose == KILL_END) else AS_WRITTEN


def whole_hundredths(plan, rows):
    """(the scored rows whose BSP as written is a whole multiple of the step, the scored rows): two counts."""
    bsps = [row.bsp for row in rows if row.klass == SCORED]
    return sum(1 for bsp in bsps if (bsp / plan.bsp_step).denominator == 1), len(bsps)


def ends_apart(plan, whole, of):
    """The registered switch: whether at least bsp_whole_share of a pass's scored BSPs are whole hundredths."""
    return of > 0 and whole >= plan.bsp_whole_share * of


def scored(rows, label=None, half=None):
    """The scored rows, of one partition label and one half-year when given."""
    return [row for row in rows if row.klass == SCORED and label in (None, row.label) and half in (None, row.half)]


def series(plan, rows, what, end, at="price"):
    """[(race date, float)] of one figure of scored rows at one BSP end; the float is the exact figure, rounded
    once. at names the row's price that is valued: its own, or "mid" or "null_mid" (rows with none are left out)."""
    step = {AS_WRITTEN: 0, PLUS_STEP: plan.bsp_step}[end]
    return [(row.race_date, float(score(plan, row.side, getattr(row, at), row.bsp + step, what)))
            for row in rows if getattr(row, at) is not None]


# ---------------------------------------------------------------------------------------------------------------
# Part and rest, and the statistics. Unless it says otherwise, a function here takes scored rows only (scored()).

def _dates(rows):
    return len({row.race_date for row in rows})


def part_and_rest(plan, group, part):
    """(part, False) when the rows of group outside part number min_values; else (group, True): the group's figure
    stands for the part's, so that two given means never leave a small rest's mean to be worked out."""
    return (part, False) if len(group) - len(part) >= plan.min_values else (group, True)


def filled_part(plan, rows, model):
    """The Part of scored rows under one fill model (registration measurement.part_and_rest)."""
    filled = [row for row in rows if row.fills[model].kind is not None]
    chosen, flag = part_and_rest(plan, rows, filled)
    return Part(chosen, len(filled), _dates(filled), flag)


def _standing(plan, rows, model):
    """The positions in rows of the rows that stand for a model's filled orders (filled_part's rows)."""
    if filled_part(plan, rows, model).all_placed:
        return set(range(len(rows)))
    return {place for place, row in enumerate(rows) if row.fills[model].kind is not None}


def _later(plan, rows, model):
    """(positions, stands) of a model's fills that did not come within the first registered seconds of t
    (within_s[0]), under part_and_rest against what stands for its filled orders: when fewer than min_values of
    those fall outside, they all stand (stands is True)."""
    held = _standing(plan, rows, model)
    later = {place for place, row in enumerate(rows)
             if row.fills[model].kind is not None and not row.fills[model].within[0]}
    return (later, False) if len(held) - len(later) >= plan.min_values else (held, True)


def given_together(plan, count, groups):
    """Part and rest across several groups of the same count rows (registration measurement.part_and_rest).
    groups: [(name, the positions of its rows)], the first to be given first. Rows that are in exactly the same
    given groups are one class, and no figure given can tell the rows of one class apart. A group is given only if
    every class then holds min_values rows, so no sum over fewer rows can be worked out from the figures given.
    The names given, in order. It asks more than is needed when two groups cross without one holding the other."""
    given, classes = [], [set(range(count))]
    for name, members in groups:
        split = [part for held in classes for part in (held & members, held - members) if part]
        if min(map(len, split), default=0) >= plan.min_values:
            given.append(name)
            classes = split
    return given


def given_figures(plan, rows):
    """Which by-model figures one group of scored rows gives (one cell on a label, or a half-year of it): a list of
    (FILLED, model) for the model's filled figures and (LATER, model) for its figure without the first fills.
    The filled groups come first, in GIVEN_FIRST's order, so the later ones never cost a filled figure."""
    groups = [((FILLED, model), _standing(plan, rows, model)) for model in GIVEN_FIRST]
    groups += [((LATER, model), _later(plan, rows, model)[0]) for model in GIVEN_FIRST]
    return given_together(plan, len(rows), groups)


def withheld(figure):
    """A figure as part and rest withholds it: its counts and flags stay, every statistic is None, and the reason
    is rest_not_given. A figure that gave no mean or estimate keeps its own reason."""
    given = any(figure.get(key) is not None for key in ("mean", "estimate"))
    return {key: None if key in STATISTICS else REST_NOT_GIVEN if key == "reason" and given else item
            for key, item in figure.items()}


def gate(plan, values, race_dates):
    """Whether a count of values on a count of race dates may be tested, bounded or selected on."""
    return values >= plan.min_tested_values and race_dates >= plan.min_tested_race_dates


def _counts_only(result, reason):
    return dict(result, mean=None, se=None, df=None, t_critical=None, ci_low=None, ci_high=None, reason=reason)


def described(plan, values, given=True):
    """movement.cr1_mean of [(race date, float)] values, with the sample SD: n, race dates, mean, SD and the CR1
    interval. Below min_values, or when not given, counts only (reason counts_only). It decides nothing."""
    result = dict(cr1_mean(values, plan.confidence_level, plan.min_clusters), sd=None)
    if not given or len(values) < plan.min_values:
        return _counts_only(result, COUNTS_ONLY)
    return dict(result, sd=sample_sd([x for _, x in values]))


def one_sided_p(mean, se, df):
    """The p-value of H1 "the mean is above 0": 1 - student_t_cdf(mean / se, df)."""
    return 1.0 - student_t_cdf(mean / se, df)


def gated(plan, values, passes=None):
    """movement.cr1_mean of values when the size gate passes, else counts only (reason underpowered). passes is the
    gate of the model the rule reads; None means the values' own count and race dates."""
    if passes is None:
        passes = gate(plan, len(values), len({day for day, _ in values}))
    result = cr1_mean(values, plan.confidence_level, plan.min_clusters)
    return result if passes else _counts_only(result, UNDERPOWERED)


def cr1_test(plan, values, passes=None, two_sided=False):
    """gated() with t and one p-value: p_one_sided for a mean above 0, or p_two_sided. Both stay None when the
    gate fails or there is no SE (too_few_clusters, se_undefined): not tested."""
    key = "p_two_sided" if two_sided else "p_one_sided"
    result = dict(gated(plan, values, passes), t=None, **{key: None})
    if result["df"] is None:
        return result
    t = result["mean"] / result["se"]
    p = 2.0 * (1.0 - student_t_cdf(abs(t), result["df"])) if two_sided else one_sided_p(result["mean"], result["se"],
                                                                                         result["df"])
    return dict(result, t=t, **{key: p})


def bound(result, level, sign):
    """mean + sign x t(level; G - 1) x SE of a gated() result: the kill bound with sign 1, the lower bound with
    sign -1. None when the result has no SE."""
    if result["df"] is None:
        return None
    return result["mean"] + sign * student_t_quantile(float(level), result["df"]) * result["se"]


def _q(value):
    return format(float(value), "g")


def quantile_table(plan, values):
    """{q: the nearest-rank q quantile, to 4 places} of numbers for the registered quantiles; None below
    min_values. For values that are not outcomes (a queue, seconds), and for outcomes in the registered pass."""
    values = [float(value) for value in values]
    if len(values) < plan.min_values:
        return None
    return {_q(q): round(value, QUANTILE_PLACES) for q, value in nearest_rank(values, plan.quantiles).items()}


def spread(plan, values):
    """{values, race_dates, sd, ranges} of [(race date, float)] values: all the blind pass may give of an outcome.
    The sample SD and the symmetric ranges q(1 - a) - q(a), widest first, keep a group's spread but not where it
    lies or which way. Below min_values, counts only."""
    xs = [x for _, x in values]
    result = {"values": len(xs), "race_dates": len({day for day, _ in values}), "sd": None, "ranges": None}
    if len(xs) >= plan.min_values:
        found = nearest_rank(xs, plan.quantiles)
        result.update(sd=sample_sd(xs), ranges={f"{_q(1 - q)}-{_q(q)}": round(found[1 - q] - found[q],
                                                                              QUANTILE_PLACES)
                                                for q in plan.quantiles if q < Fraction(1, 2)})
    return result


def rounded(value, places=FLOAT_PLACES):
    """value with every float in it rounded (dicts, lists and plain tuples walked; a Cell is kept)."""
    if isinstance(value, float):
        return round(value, places)
    if isinstance(value, dict):
        return {key: rounded(item, places) for key, item in value.items()}
    return [rounded(item, places) for item in value] if type(value) in (list, tuple) else value


def detectable(plan, sd, values, race_dates):
    """The detectable effect of a mean of `values` values with SD sd on race_dates race dates:
    (t(detectable_level; G - 1) + t(power; G - 1)) x SD / sqrt(N). Under independence; clustering makes it larger.
    None without an SD, a value or two race dates."""
    if sd is None or values < 1 or race_dates < 2:
        return None
    factor = student_t_quantile(float(plan.detectable_level), race_dates - 1) + \
        student_t_quantile(float(plan.power), race_dates - 1)
    return factor * sd / math.sqrt(values)


def blind_outcome(plan, rows, model, end):
    """What the blind pass gives of one cell's outcome under one model. rows: the cell's rows of both labels.
    spread() of gross g over the pooled filled part, its flag, and the detectable effect on the test label from
    that SD and the test label's own part (its rows and their race dates). When the test label's part is every
    scored order and the pooled part is not, the SD is the one over every scored order of both labels: the
    registered figure is then an all-placed one."""
    pooled, test = (filled_part(plan, scored(rows, label), model) for label in (None, plan.test_label))
    result = spread(plan, series(plan, pooled.rows, G, end))
    sd = result["sd"]
    if test.all_placed and not pooled.all_placed:
        sd = spread(plan, series(plan, scored(rows), G, end))["sd"]
    return dict(result, **{FILLED_IS_ALL_PLACED: pooled.all_placed}, detectable={
        "values": len(test.rows), "race_dates": _dates(test.rows), FILLED_IS_ALL_PLACED: test.all_placed,
        "effect": detectable(plan, sd, len(test.rows), _dates(test.rows))})


# ---------------------------------------------------------------------------------------------------------------
# Intent-to-treat figures

def gap(plan, rows, model, end):
    """Mean g over a model's filled scored rows minus mean g over its unfilled ones (movement.cr1_contrast, by
    race date): {estimate, se, df, ci_low, ci_high, bets, race_dates, reason}. Given when each part holds
    min_values rows (else reason filled_is_all_placed or counts_only), with an interval from min_clusters race
    dates. It is descriptive: no p-value."""
    filled = [row for row in rows if row.fills[model].kind is not None]
    unfilled = [row for row in rows if row.fills[model].kind is None]
    found = cr1_contrast(series(plan, filled, G, end), series(plan, unfilled, G, end), plan.min_values, 1)
    result = dict({key: found[key] for key in ("estimate", "se", "df", "bets", "race_dates", "reason")},
                  ci_low=None, ci_high=None)
    if found["reason"] == "underpowered":
        return dict(result, reason=FILLED_IS_ALL_PLACED if len(unfilled) < plan.min_values else COUNTS_ONLY)
    if found["df"] is None:
        return result
    if found["df"] + 1 < plan.min_clusters:
        return dict(result, se=None, df=None, reason="too_few_clusters")
    width = student_t_quantile(float((1 + plan.confidence_level) / 2), found["df"]) * found["se"]
    return dict(result, ci_low=found["estimate"] - width, ci_high=found["estimate"] + width)


def figures(plan, rows, end):
    """The intent-to-treat figures of one group of scored rows (one cell; one label, or one half-year of it) at
    one BSP end, each as described(): placed, mean g over every order at its own price, with no fill model; mid,
    the mid at t valued the same way; and per model: fills and their race dates, the flag, filled {g, net_closed,
    net_held} over the filled part, value_per_placed (net_held when filled, 0 when not; given only when the
    filled figures are), and the gap. A model that given_figures does not give keeps its counts and flag, and
    its three figures are withheld()."""
    out = {"placed": described(plan, series(plan, rows, G, end)),
           "mid": described(plan, series(plan, rows, G, end, "mid")), "models": {}}
    held, given = series(plan, rows, NET_HELD, end), given_figures(plan, rows)
    for model in plan.models:
        part = filled_part(plan, rows, model)
        per_placed = [(day, value if part.all_placed or row.fills[model].kind is not None else 0.0)
                      for row, (day, value) in zip(rows, held)]
        found = {"filled": {what: described(plan, series(plan, part.rows, what, end)) for what in MEASURES},
                 "value_per_placed": described(plan, per_placed, len(part.rows) >= plan.min_values),
                 "gap": gap(plan, rows, model, end)}
        if (FILLED, model) not in given:
            found = {"filled": {what: withheld(item) for what, item in found["filled"].items()},
                     "value_per_placed": withheld(found["value_per_placed"]), "gap": withheld(found["gap"])}
        out["models"][model] = {"fills": part.fills, "fill_race_dates": part.fill_dates,
                                FILLED_IS_ALL_PLACED: part.all_placed, **found}
    return out


def without_first_fills(plan, rows, model):
    """(rows, stands) for the sensitivity line: the model's fills that did not come within the first registered
    seconds of t (within_s[0]). Under part_and_rest against what stands for its filled orders: when fewer than
    min_values of those fall outside, they all stand (stands is True)."""
    later, stands = _later(plan, rows, model)
    return [row for place, row in enumerate(rows) if place in later], stands


# ---------------------------------------------------------------------------------------------------------------
# Selection, the null and the labels

def _mean(values):
    return math.fsum(x for _, x in values) / len(values)


def select_primaries(plan, explore, test_counts, apart):
    """The Selection (registration selection.rule). explore: {Cell: the explore label's rows}; test_counts: {Cell:
    (the test model's fills on the test label, their race dates)}, two counts; apart: ends_apart, from counts.
    Nothing else of the test label is taken, and a row of another label is refused (ValueError). eligible: the
    cells that pass both gates, in the plan's order. primaries: {rule id: the eligible cell with the highest mean
    net_held over the explore model's filled part at the claim end, a tie to the later time; None with no
    eligible cell}."""
    eligible, primaries = [], {}
    for rule in plan.rules:
        best = None
        for cell in (cell for cell in plan.cells if cell.rule_id == rule.id):
            if any(row.label != plan.explore_label for row in explore[cell]):
                raise ValueError("select:not_the_explore_label")
            part = filled_part(plan, scored(explore[cell]), plan.select_explore_model)
            if not (gate(plan, part.fills, part.fill_dates) and gate(plan, *test_counts[cell])):
                continue
            eligible.append(cell)
            end = deciding_end(rule.intents[0].side, CLAIM_END, apart)
            key = (_mean(series(plan, part.rows, SELECT_MEASURE, end)), cell.at_s)
            if best is None or key > best[0]:
                best = (key, cell)
        primaries[rule.id] = None if best is None else best[1]
    return Selection(tuple(eligible), primaries)


def null_gate(plan, rows, end):
    """The random-time null over the rows a claim reads (registration null.rule): {null, main_same_orders,
    undefined, at_least_the_fraction, blocks, why}. null is the two-sided test of the mean null value (the mid at
    u valued as g) and main_same_orders the mean g, both over the rows with a null value; at_least_the_fraction
    says whether |mean null| >= null_fraction x |mean g| (None when either has no mean). It blocks (why, one of
    NULL_BLOCKS) when those rows fail the size gate, or when at_least_the_fraction holds and the null's p is
    below null_alpha or undefined. It can only block a claim. Part and rest: when 1 to min_values - 1 rows have
    no null value, main_same_orders is decided on as before and then withheld(): the claim's own mean g over
    every row is given elsewhere, and the two would give those few rows' mean."""
    same = [row for row in rows if row.null_mid is not None]
    null = cr1_test(plan, series(plan, same, G, end, "null_mid"), two_sided=True)
    main = gated(plan, series(plan, same, G, end))
    why = large = None
    if null["mean"] is None or main["mean"] is None:
        why = NULL_BLOCKS[0]
    else:
        large = bool(abs(null["mean"]) >= plan.null_fraction * abs(main["mean"]))
        if large and (null["p_two_sided"] is None or null["p_two_sided"] < plan.null_alpha):
            why = NULL_BLOCKS[1]
    undefined = len(rows) - len(same)
    if 0 < undefined < plan.min_values:
        main = withheld(main)
    return {"null": null, "main_same_orders": main, "undefined": undefined, "at_least_the_fraction": large,
            "blocks": why is not None, "why": why}


def _held_mean(plan, part, end):
    return described(plan, series(plan, part.rows, NET_HELD, end))["mean"]


def _both(plan, rows):
    """{BSP end: {net: mean or None}}: both nets at both BSP ends, as every label is printed with."""
    return {end: {net: described(plan, series(plan, rows, net, end))["mean"] for net in (NET_CLOSED, NET_HELD)}
            for end in BSP_ENDS}


def verdicts(plan, primaries, rows, apart):
    """{rule id: None for a rule file with no primary, else its label and what is printed with it} (registration
    verdicts.rule). primaries: Selection.primaries; rows: {Cell: its rows of both labels}; apart: ends_apart.
    Per primary, the first that applies:
    kill_optimistic: over both labels, the optimistic model passes the size gate and the kill bound of mean
    net_closed at the kill end is below 0. A low mean alone is not enough, and nothing else kills.
    underpowered: the pessimistic model fails the size gate on the test label.
    edge_candidate: on the test label, on net_held at the claim end: the one-sided test of the pessimistic mean is
    rejected under Holm (over family_m, a rule file with no primary included); the mean is above 0 under every
    model; the pessimistic mean is above 0 in each half-year, each with half_year_min_fills fills; and the null,
    read only now, does not block. If it blocks, blocked is claim_blocked_by_placebo and the label falls through.
    measurement_pilot_candidate: the pessimistic lower bound is above pilot_floor. Else unresolved.
    Each answer holds: cell, label, blocked, kill and test (the gated figures, bound, BSP end, fills, flag, and
    both nets at both BSP ends), models, half_years, checks, null, pessimistic_fill_rate and printed_with."""
    front, back = fills.OPTIMISTIC, fills.PESSIMISTIC
    work = {}
    for rule in plan.rules:
        cell = primaries[rule.id]
        if cell is None:
            continue
        side = rule.intents[0].side
        ends = [deciding_end(side, purpose, apart) for purpose in (KILL_END, CLAIM_END)]
        test_rows = scored(rows[cell], plan.test_label)
        pooled = filled_part(plan, scored(rows[cell]), front)
        parts = {model: filled_part(plan, test_rows, model) for model in plan.models}
        kill = gated(plan, series(plan, pooled.rows, NET_CLOSED, ends[0]), gate(plan, pooled.fills, pooled.fill_dates))
        test = cr1_test(plan, series(plan, parts[back].rows, NET_HELD, ends[1]),
                        gate(plan, parts[back].fills, parts[back].fill_dates))
        work[rule.id] = (cell, ends, test_rows, pooled, parts, kill, test)
    adjusted = holm({rule.id: work[rule.id][-1]["p_one_sided"] if rule.id in work else None for rule in plan.rules},
                    plan.alpha, plan.family_m)
    found = {}
    for rule in plan.rules:
        if rule.id not in work:
            found[rule.id] = None
            continue
        cell, ends, test_rows, pooled, parts, kill, test = work[rule.id]
        upper, lower = bound(kill, plan.kill_level, 1), bound(test, plan.lower_bound_level, -1)
        halves = {half: filled_part(plan, [row for row in test_rows if row.half == half], back) for half in HALVES}
        models, halves = ({name: {"fills": part.fills, FILLED_IS_ALL_PLACED: part.all_placed,
                                  "mean": _held_mean(plan, part, ends[1])} for name, part in group.items()}
                          for group in (parts, halves))
        checks = {"rejected_under_holm": adjusted[rule.id][1],
                  "mean_above_0_under_every_model": all(item["mean"] is not None and item["mean"] > 0
                                                        for item in models.values()),
                  "mean_above_0_in_each_half_year": all(
                      item["fills"] >= plan.half_year_min_fills and item["mean"] is not None and item["mean"] > 0
                      for item in halves.values())}
        null = None
        if upper is not None and upper < 0:
            label = VERDICTS[0]
        elif test["reason"] == UNDERPOWERED:
            label = VERDICTS[1]
        else:
            if all(checks.values()):
                null = null_gate(plan, parts[back].rows, ends[1])
            if null is not None and not null["blocks"]:
                label = VERDICTS[2]
            else:
                label = VERDICTS[3] if lower is not None and lower > plan.pilot_floor else VERDICTS[4]
        found[rule.id] = {
            "cell": cell, "label": label, "blocked": BLOCKED if null is not None and null["blocks"] else None,
            "kill": dict(kill, bound=upper, bsp_end=ends[0], fills=pooled.fills, both=_both(plan, pooled.rows),
                         **{FILLED_IS_ALL_PLACED: pooled.all_placed}),
            "test": dict(test, holm_p_adjusted=adjusted[rule.id][0], holm_rejected=adjusted[rule.id][1],
                         lower_bound=lower, bsp_end=ends[1], fills=parts[back].fills,
                         both=_both(plan, parts[back].rows), **{FILLED_IS_ALL_PLACED: parts[back].all_placed}),
            "models": models, "half_years": halves, "checks": checks, "null": null,
            "pessimistic_fill_rate": parts[back].fills / len(test_rows) if test_rows else None,
            "printed_with": (plan.label_note, plan.exposure_note)}
    return found
