"""P0 population, cohort, the two P0 variants and the issuance ledger, from decision-time facts only (task c2a).

Inputs: a contract (valid; CohortRules.from_contract), one task (b) index record per market, and, for a market whose
decision needs a view, the DecisionView replay.replay_view builds from the record's primary member (a view whose
market, plan, anchor or decision time is not the record's refuses: view_for_another_decision; a view carries no
archive or member identity, so a same-plan view from another archive is the caller's to rule out, as the runner's
provenance and duplicate checks do). Nothing here reads a file, a clock or an outcome: the evaluator_only_outcome,
coverage, checks and other_sources parts of a record are never read, and neither is a whole-file hash. decide_market
takes the replay as a callable, so a market that needs no view is never replayed.

Decision time (plan section 5, contract decision_timing): t = the first-published scheduled off (the anchor, the
index's first_published_market_time) + offset_s. The decision axis names it, for instance
"first_published_scheduled_off-300s"; delay_s gives each intent's effective time, t + delay_s seconds (P0 freezes 0:
an instantaneous simulated acceptance assumption, not a measured latency).

Population (one class per record; only "eligible" markets have issuance rows):
1. no_view: the index's explicit empty view. A diagnostic: it has no decision time, and whether such a record exists
   at all is not prefix invariant (market_index docstring), so it is outside the decision population.
2. anchor_not_known_by_decision: the anchor was published after t (a first definition at off-4m makes no off-5m
   decision). A diagnostic, distinct from no_view; no abstention is made up from later metadata. Whether such a
   record exists is not prefix invariant either (a stream cut at t holds no message of the market).
3. excluded, with the first known mismatch in this order, judged from the record's plan and the index's
   decision_time values as of t (entries published at or before t, all before view_ends): plan_not_contract_tier
   (the member path's plan against population.tier), month_not_evaluated (the UTC month of the anchor outside the
   evaluated split), event_type_not_horse_racing (eventTypeId not "7"), country_not_in_population (countryCode),
   market_type_not_in_population (marketType). A known mismatch in any field excludes, whatever another field is.
4. unclassifiable, with the first field (in the order above) that is absent or not text at t, when no known mismatch
   excludes the market: never guessed out (or in) of the population, and never filled from later metadata.
5. eligible.
Source-level refusals (the index head's refused_markets) never reach this module; the runner reports them apart.

Rows (plan section 6, D10): an eligible market has one row per runner ACTIVE (status exactly "ACTIVE") and listed
(listed exactly true) in the view at t, in selection-id order; a runner removed later keeps its row. A market whose
runner set is unknown at t (its view stopped at or before t, or a listed runner's status is not known) or known to be
empty has one row with selection_id null. Each row has exactly one outcome per variant: issued (with its intent),
not_selected, abstained (with a reason, and as detail the index's view_ends reason when the view stopped) or
unsupported (with a reason, a decision input of capabilities.decision or decision_view_unreadable, and a detail).

Market conditions, first applicable wins, and it applies to every row of the market for every variant. The reason is
the first condition that applies; the rows are decided apart from it: one row per ACTIVE+listed runner when the
runner set is known and not empty, else one selection-null row (so a listed runner whose status is unknown makes
the market one selection-null row whichever condition gives its reason):
1. The view stopped at or before t (the index's view_ends pt <= t): view_ends reason member_unreadable_from_pt is
   unsupported, decision_view_unreadable; any other reason (an outcome-bearing definition published by t) is
   abstained, market_not_open_pre_off. One selection-null row: the runner state is not read.
2. status (latest at t): not text is unsupported market_definition (status_unknown); text other than "OPEN" is
   abstained market_not_open_pre_off.
3. inPlay: not a bool is unsupported market_definition (in_play_unknown); true is abstained market_not_open_pre_off.
4. bspMarket: not a bool is unsupported market_definition (bsp_market_unknown); false is abstained bsp_market_false
   (P0 backs at BSP; the Historical Data feed specification describes bspMarket as the market supporting SP betting).
5. A listed runner whose status is not one the Historical Data spec lists (market_index.KNOWN_RUNNER_STATUSES; not
   text, or other text): unsupported market_definition (runner_status_unknown), one selection-null row, since whether
   it is ACTIVE is unknown. An ACTIVE listed runner whose id is not a positive integer (the index accepts 0; Betfair
   selection ids are positive and accounting refuses 0): unsupported market_definition (runner_id_not_positive).
6. No runner ACTIVE and listed: abstained no_active_runner, one selection-null row.
Then the price input, for every variant (ltp is a required decision input, capabilities.decision, so it gates
issuance for every variant; every row stays in the ledger): an rc entry at or before t whose runner id did not parse
(DecisionView.malformed_price_changes) makes every row unsupported ltp (malformed_price_change), since no price can
be attributed with certainty; otherwise each runner whose ltp at t is not usable (replay.usable_ltp: a finite
number above 0; absent, null or reset by an image is missing, never zero) is unsupported ltp (missing).

Variants (only the two P0 variants are supported; their contract text must say what follows):
- ltp_favourite (uses ltp_at_decision): when any row is unsupported ltp, no bet: the supported rows are abstained
  ltp_missing_in_market; when the lowest usable ltp is shared, no bet: every supported row is abstained ltp_tie;
  otherwise the unique lowest is issued and the other rows are not_selected.
- seeded_random_runner (uses no feature): one draw from ALL ACTIVE+listed runners, whatever the price inputs (n =
  their count, ids sorted numerically), with metrics.HashStream seeded by seeds.selection under the label
  "selection/v1/<market id>" (a namespace disjoint from the bootstrap's "<variant>|<block>" labels; a variant id
  holds no "/"), so the draw depends only on the seed and the market id, never on traversal order or any other
  market. The drawn runner is issued when its row is supported; when it is unsupported, no bet and no redraw: its
  row stays unsupported, every other supported row is not_selected, and the drawn id is recorded (drawn true) on the
  drawn row whatever its outcome (false on the others; null on every row of a market where a market condition left
  no draw to make).
An intent is {"side": "BACK", "price": "BSP", "stake_pence": staking.stake_pence, "effective_ms": t + 1000 * delay_s}.

Decision id (plan section 6, D9): sha256 of the canonical JSON of [DECISION_ID_DOMAIN, contract_sha256, market id,
selection id or null, decision axis]. It depends on nothing in the file but the market and runner ids (no position,
no hash of a file or input, no outcome), so a rerun of the same contract on the same input gives the same id, and a
row's id is shared by every variant: (variant, decision_id) is the bet key accounting.IssuedBet expects.

The issuance ledger is one canonical JSON line per row (ledger_schema COHORT_SCHEMA_VERSION), ordered by anchor time,
numeric market id and selection id (null first). Every function over several MarketDecisions refuses two of one
(plan, market id) or two rows of one decision id (CohortProblem), so decisions made in separate calls (separate
archives, say) cannot be counted twice; decide_records refuses such a duplicate before deciding anything: the choice
between two records of one market would have to come from what they hold later. issuance_counts reconciles the
ledger per variant: one outcome per row; issued rows carry the contract's intent and others none; abstained and
unsupported rows a reason and issued and not_selected rows none; at most one issued row per market (plan section 8's
pessimistic ROI assumes one bet per variant per market); and, for the seeded variant, a literal drawn flag on every
row, one drawn row per market with a draw and none without, the issued row being the drawn one, and the drawn row
issued or unsupported. Its counts by outcome and by (outcome, reason) count each row once, so they add up to the rows by
construction; the checks above are what can fail.

Limits (what prefix invariance covers, D13): a decision at t depends only on what is published at or before t, with
three bounded exceptions inherited from the index. A no_view record, and an anchor_not_known_by_decision one, may
exist or not depending on later messages (never a decision-time population either way). A line whose pt cannot be
read (not JSON, cut mid-line) right after the batch at t ends the view at the last pt applied, so it makes the
decision at t unsupported decision_view_unreadable although it may have been published after t: its time is
unknown, and the index never guesses it (market_index docstring). And a member the content gate refused at ingest
(only at the 2025-12/2026-01 boundary) is a source-level refusal decided from its content.
"""

from dataclasses import dataclass
import hashlib

from .archive import utc_month
from .contract import canonical_json, validate_contract
from .market_index import KNOWN_RUNNER_STATUSES, market_sort_key
from .metrics import HashStream
from .replay import anchor_and_decision, usable_ltp

COHORT_SCHEMA_VERSION = 1
DECISION_ID_DOMAIN = "betangel-research/backtest/decision-id/v1"
SELECTION_LABEL = "selection/v1/{market_id}"
P0_VARIANTS = {"ltp_favourite": ("ltp_at_decision",), "seeded_random_runner": ()}
SEEDED = "seeded_random_runner"
SUPPORTED_ANCHORS = ("first_published_scheduled_off",)
UNREADABLE_VIEW_END = "member_unreadable_from_pt"
HORSE_RACING_EVENT_TYPE = "7"

ISSUED, NOT_SELECTED, ABSTAINED, UNSUPPORTED = "issued", "not_selected", "abstained", "unsupported"
OUTCOMES = (ABSTAINED, ISSUED, NOT_SELECTED, UNSUPPORTED)
ELIGIBLE, NO_VIEW, ANCHOR_NOT_KNOWN, EXCLUDED, UNCLASSIFIABLE = (
    "eligible", "no_view", "anchor_not_known_by_decision", "excluded", "unclassifiable")
POPULATION_CLASSES = (ANCHOR_NOT_KNOWN, ELIGIBLE, EXCLUDED, NO_VIEW, UNCLASSIFIABLE)
# Eligibility fields in judging order: (field, exclusion reason, test of a value that is text).
ELIGIBILITY = (("eventTypeId", "event_type_not_horse_racing", lambda rules, value: value == HORSE_RACING_EVENT_TYPE),
               ("countryCode", "country_not_in_population", lambda rules, value: value in rules.countries),
               ("marketType", "market_type_not_in_population", lambda rules, value: value in rules.market_types))


class CohortProblem(Exception):
    """An input that cannot be decided or counted honestly (a duplicate market or row); str() is "<where>:<reason>"."""


@dataclass(frozen=True)
class CohortRules:
    """What the cohort reads from a valid contract (from_contract)."""

    contract_sha256: str
    axis: str
    offset_s: int
    delay_s: int
    selection_seed: int
    stake_pence: int
    tier: str
    countries: tuple
    market_types: tuple
    start_month: str
    end_month: str
    variants: tuple

    @classmethod
    def from_contract(cls, contract):
        """The rules of a valid contract whose decision timing, delay and variants this module supports; ValueError
        otherwise (a draft's null delay_s included: nothing is decided under a delay not yet chosen)."""
        problems, digest = validate_contract(contract)
        if problems:
            raise ValueError("contract:invalid")
        timing = contract["decision_timing"]
        if timing["anchor"] not in SUPPORTED_ANCHORS:
            raise ValueError("decision_timing:anchor_not_supported")
        if timing["delay_s"] is None:
            raise ValueError("decision_timing:delay_s_null")
        declared = {item["id"]: tuple(item["uses_features"]) for item in contract["variants"]}
        if declared != P0_VARIANTS:
            raise ValueError("variants:not_the_p0_variants")
        population = contract["population"]
        split = population["splits"][population["evaluate_split"]]
        return cls(contract_sha256=digest, axis=f"{timing['anchor']}{timing['offset_s']:+d}s",
                   offset_s=timing["offset_s"], delay_s=timing["delay_s"],
                   selection_seed=contract["seeds"]["selection"],
                   stake_pence=contract["staking"]["stake_pence"], tier=population["tier"],
                   countries=tuple(population["countries"]), market_types=tuple(population["market_types"]),
                   start_month=split["start_month"], end_month=split["end_month"], variants=tuple(sorted(declared)))


@dataclass(frozen=True)
class MarketDecision:
    """One record's population class (and reason, for excluded and unclassifiable), its anchor pt and time and
    decision time when it has an anchor, and, for an eligible market, its issuance rows (canonical-JSON-ready
    dicts)."""

    market_id: str
    plan: str
    population: str
    reason: object
    anchor_pt: object
    anchor_ms: object
    decision_ms: object
    rows: tuple


def decision_id(rules, market_id, selection_id):
    return hashlib.sha256(canonical_json([DECISION_ID_DOMAIN, rules.contract_sha256, market_id, selection_id,
                                          rules.axis])).hexdigest()


def _as_of_view(decision, key, t):
    """The latest value of one definition field in the index's decision_time at or before t (entries there are all
    before view_ends), and whether one was published."""
    found, value = False, None
    for pt, item in decision["definition"].get(key, []):
        if pt > t:
            break
        found, value = True, item
    return found, value


def classify(rules, record):
    """(population class, reason or None, (anchor pt, anchor ms, t) or None) of one index record."""
    timing = anchor_and_decision(record, rules.offset_s)
    if timing is None:
        return NO_VIEW, None, None
    anchor_pt, anchor_ms, t = timing
    if anchor_pt > t:
        return ANCHOR_NOT_KNOWN, None, timing
    if record["provenance"]["plan"] != rules.tier:
        return EXCLUDED, "plan_not_contract_tier", timing
    month = utc_month(record["decision_time"]["first_published_market_time"]["value"])
    if month is None or not rules.start_month <= month <= rules.end_month:
        return EXCLUDED, "month_not_evaluated", timing
    unknown = None
    for key, reason, accepts in ELIGIBILITY:
        found, value = _as_of_view(record["decision_time"], key, t)
        if not found or not isinstance(value, str):
            unknown = unknown or key
        elif not accepts(rules, value):
            return EXCLUDED, reason, timing
    if unknown is not None:
        return UNCLASSIFIABLE, unknown, timing
    return ELIGIBLE, None, timing


def _outcome(outcome, reason=None, detail=None, intent=None):
    return {"outcome": outcome, "reason": reason, "detail": detail, "intent": intent}


def _active(view):
    return [runner for runner in view.runners if runner.status == "ACTIVE" and runner.listed is True]


def _runner_set_known(view):
    """Whether the ACTIVE+listed runner set is known at t: no listed runner has a status the spec does not list."""
    return not any(runner.listed is True and runner.status not in KNOWN_RUNNER_STATUSES for runner in view.runners)


def _market_condition(view):
    """(outcome, reason, detail) of the first market condition 2 to 6 (module docstring) that applies, or None."""
    status, in_play, bsp_market = view.field("status"), view.field("inPlay"), view.field("bspMarket")
    if not isinstance(status, str):
        return UNSUPPORTED, "market_definition", "status_unknown"
    if status != "OPEN":
        return ABSTAINED, "market_not_open_pre_off", None
    if type(in_play) is not bool:
        return UNSUPPORTED, "market_definition", "in_play_unknown"
    if in_play:
        return ABSTAINED, "market_not_open_pre_off", None
    if type(bsp_market) is not bool:
        return UNSUPPORTED, "market_definition", "bsp_market_unknown"
    if not bsp_market:
        return ABSTAINED, "bsp_market_false", None
    if not _runner_set_known(view):
        return UNSUPPORTED, "market_definition", "runner_status_unknown"
    active = _active(view)
    if any(type(runner.selection_id) is not int or runner.selection_id < 1 for runner in active):
        return UNSUPPORTED, "market_definition", "runner_id_not_positive"
    if not active:
        return ABSTAINED, "no_active_runner", None
    return None


def _row(rules, market_id, plan, timing, selection_id, ltp, outcomes):
    anchor_pt, anchor_ms, t = timing
    return {"ledger_schema": COHORT_SCHEMA_VERSION, "decision_id": decision_id(rules, market_id, selection_id),
            "market_id": market_id, "plan": plan, "selection_id": selection_id,
            "decision": {"axis": rules.axis, "decision_ms": t, "anchor_pt": anchor_pt, "anchor_ms": anchor_ms},
            "input": ltp, "variants": outcomes}


def _intent(rules, t):
    return {"side": "BACK", "price": "BSP", "stake_pence": rules.stake_pence, "effective_ms": t + rules.delay_s * 1000}


def _favourite(rules, t, runners, unsupported):
    """{selection id: outcome} for ltp_favourite; unsupported maps a runner's id to its ltp detail."""
    if unsupported:
        return {r.selection_id: _outcome(UNSUPPORTED, "ltp", unsupported[r.selection_id])
                if r.selection_id in unsupported else _outcome(ABSTAINED, "ltp_missing_in_market") for r in runners}
    lowest = min(runner.ltp for runner in runners)
    best = [runner.selection_id for runner in runners if runner.ltp == lowest]
    if len(best) > 1:
        return {r.selection_id: _outcome(ABSTAINED, "ltp_tie") for r in runners}
    return {r.selection_id: _outcome(ISSUED, intent=_intent(rules, t)) if r.selection_id == best[0]
            else _outcome(NOT_SELECTED) for r in runners}


def _seeded(rules, market_id, t, runners, unsupported):
    """{selection id: outcome} for seeded_random_runner (one draw over every runner, no redraw)."""
    ids = sorted(runner.selection_id for runner in runners)
    drawn = ids[HashStream(rules.selection_seed, SELECTION_LABEL.format(market_id=market_id)).below(len(ids))]
    outcomes = {}
    for selection_id in ids:
        if selection_id in unsupported:
            outcome = _outcome(UNSUPPORTED, "ltp", unsupported[selection_id])
        elif selection_id == drawn:
            outcome = _outcome(ISSUED, intent=_intent(rules, t))
        else:
            outcome = _outcome(NOT_SELECTED)
        outcomes[selection_id] = {**outcome, "drawn": selection_id == drawn}
    return outcomes


def decide_market(rules, record, replay):
    """The MarketDecision of one index record; replay(record) -> DecisionView is called only for an eligible market
    whose view runs past t (module docstring)."""
    market_id, plan = record["market_id"], record["provenance"]["plan"]
    population, reason, timing = classify(rules, record)
    anchor_pt, anchor_ms, t = timing if timing is not None else (None, None, None)

    def decided(rows):
        return MarketDecision(market_id, plan, population, reason, anchor_pt, anchor_ms, t, tuple(rows))

    if population != ELIGIBLE:
        return decided(())

    def every_variant(outcome):          # no draw is made: drawn is null on every seeded row
        return {variant: {**outcome, "drawn": None} if variant == SEEDED else dict(outcome)
                for variant in rules.variants}

    ends = record["decision_time"]["view_ends"]
    if ends["pt"] is not None and ends["pt"] <= t:
        if ends["reason"] == UNREADABLE_VIEW_END:
            stopped = _outcome(UNSUPPORTED, "decision_view_unreadable")
        else:
            stopped = _outcome(ABSTAINED, "market_not_open_pre_off", ends["reason"])
        return decided([_row(rules, market_id, plan, timing, None, None, every_variant(stopped))])
    view = replay(record)
    if ((view.market_id, view.plan, view.decision_ms, view.anchor_ms, view.anchor_pt)
            != (market_id, plan, t, anchor_ms, anchor_pt)):
        raise CohortProblem(f"cohort:{market_id}:view_for_another_decision")
    runners = _active(view)
    condition = _market_condition(view)
    if condition is not None:
        # Which reason applies and how many rows the market has are separate decisions: the rows are per runner only
        # when the runner set is known (and not empty), whichever condition gave the reason.
        outcome = _outcome(*condition)
        targets = runners if runners and _runner_set_known(view) else [None]
        return decided([_row(rules, market_id, plan, timing, None if runner is None else runner.selection_id,
                             None if runner is None else {"ltp": runner.ltp, "ltp_pt": runner.ltp_pt},
                             every_variant(outcome)) for runner in targets])
    if view.malformed_price_changes:
        unsupported = {runner.selection_id: "malformed_price_change" for runner in runners}
    else:
        unsupported = {runner.selection_id: "missing" for runner in runners if not usable_ltp(runner.ltp)}
    by_variant = {"ltp_favourite": _favourite(rules, t, runners, unsupported),
                  SEEDED: _seeded(rules, market_id, t, runners, unsupported)}
    return decided([_row(rules, market_id, plan, timing, runner.selection_id,
                         {"ltp": runner.ltp, "ltp_pt": runner.ltp_pt},
                         {variant: by_variant[variant][runner.selection_id] for variant in rules.variants})
                    for runner in runners])


def _unique(decisions):
    """The decisions as a list after refusing two of one (plan, market id) or two rows of one decision id."""
    decisions, markets, ids = list(decisions), set(), set()
    for decision in decisions:
        key = (decision.plan, decision.market_id)
        if key in markets:
            raise CohortProblem(f"cohort:{decision.market_id}:duplicate_market")
        markets.add(key)
        for row in decision.rows:
            if row["decision_id"] in ids:
                raise CohortProblem(f"cohort:{decision.market_id}:duplicate_decision_id")
            ids.add(row["decision_id"])
    return decisions


def decide_records(rules, records, replay):
    """MarketDecisions of every record, in anchor-time then numeric market id order (records without an anchor
    last); CohortProblem for two records of one (plan, market id), before anything is decided or replayed."""
    records, seen = list(records), set()
    for record in records:
        key = (record["provenance"]["plan"], record["market_id"])
        if key in seen:
            raise CohortProblem(f"cohort:{record['market_id']}:duplicate_market")
        seen.add(key)
    decisions = [decide_market(rules, record, replay) for record in records]
    return sorted(decisions, key=lambda d: (d.anchor_ms is None, d.anchor_ms or 0, market_sort_key(d.market_id),
                                            d.market_id, d.plan))


def ledger_rows(decisions):
    """Every issuance row, in ledger order (module docstring)."""
    rows = [row for decision in _unique(decisions) for row in decision.rows]
    return sorted(rows, key=lambda row: (row["decision"]["anchor_ms"], market_sort_key(row["market_id"]),
                                         row["market_id"], row["selection_id"] is not None,
                                         row["selection_id"] or 0))


def ledger_lines(decisions):
    """The issuance ledger: one canonical JSON line per row."""
    return [canonical_json(row) + b"\n" for row in ledger_rows(decisions)]


def population_counts(decisions):
    """{class: count} and {class: {reason: count}} over every record."""
    by_class, by_reason = {}, {}
    for decision in _unique(decisions):
        by_class[decision.population] = by_class.get(decision.population, 0) + 1
        if decision.reason is not None:
            reasons = by_reason.setdefault(decision.population, {})
            reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
    return {"by_class": dict(sorted(by_class.items())),
            "by_reason": {key: dict(sorted(value.items())) for key, value in sorted(by_reason.items())}}


def _row_problem(rules, row, variant, outcome):
    """None, or why one row's outcome for one variant breaks the ledger's rules (module docstring)."""
    if not isinstance(outcome, dict) or outcome.get("outcome") not in OUTCOMES:
        return "no_outcome"
    kind, reason, intent = outcome["outcome"], outcome.get("reason"), outcome.get("intent")
    if kind == ISSUED:
        if intent != _intent(rules, row["decision"]["decision_ms"]) or row["selection_id"] is None:
            return "issued_without_the_contract_intent"
    elif intent is not None:
        return "intent_without_issue"
    if (reason is None) != (kind in (ISSUED, NOT_SELECTED)):
        return "reason_does_not_match_outcome"
    if variant == SEEDED:
        if "drawn" not in outcome:
            return "drawn_missing"
        drawn = outcome["drawn"]
        if drawn is not None and type(drawn) is not bool:
            return "drawn_not_a_literal"
        if kind == ISSUED and drawn is not True:
            return "issued_but_not_drawn"
        if drawn is True and kind not in (ISSUED, UNSUPPORTED):
            return "drawn_but_neither_issued_nor_unsupported"
    return None


def issuance_counts(rules, decisions):
    """Per variant: rows, counts by outcome and by "outcome:reason", issued count and stake; ValueError when a row
    or a market breaks the ledger's rules (module docstring; issuance_exposure_reconciliation's decision half, the
    settlement half, issued = resolved + unresolved, needs the accounting)."""
    decisions = _unique(decisions)
    rows = ledger_rows(decisions)
    report = {}
    for variant in rules.variants:
        for decision in decisions:
            outcomes = [row["variants"].get(variant) for row in decision.rows]
            for row, outcome in zip(decision.rows, outcomes):
                problem = _row_problem(rules, row, variant, outcome)
                if problem:
                    raise ValueError(f"issuance:{row['decision_id']}:{variant}:{problem}")
            if sum(1 for outcome in outcomes if outcome["outcome"] == ISSUED) > 1:
                raise ValueError(f"issuance:{decision.market_id}:{variant}:more_than_one_issued")
            if variant == SEEDED and outcomes:
                drawn = [outcome["drawn"] for outcome in outcomes]
                if not (drawn.count(True) == 1 and drawn.count(False) == len(drawn) - 1
                        or drawn.count(None) == len(drawn)):
                    raise ValueError(f"issuance:{decision.market_id}:{variant}:draw_not_recorded_once")
        by_outcome, by_reason = {}, {}
        for row in rows:
            outcome = row["variants"][variant]
            by_outcome[outcome["outcome"]] = by_outcome.get(outcome["outcome"], 0) + 1
            key = f"{outcome['outcome']}:{outcome['reason']}"
            by_reason[key] = by_reason.get(key, 0) + 1
        issued = by_outcome.get(ISSUED, 0)
        report[variant] = {"rows": len(rows), "by_outcome": dict(sorted(by_outcome.items())),
                           "by_reason": dict(sorted(by_reason.items())), "issued": issued,
                           "issued_stake_pence": issued * rules.stake_pence}
    return report
