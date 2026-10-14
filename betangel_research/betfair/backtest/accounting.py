"""P0 settlement and commission: fixed-stake BACK at BSP, exact pence, every exceptional case unresolved.

Nothing here reads a file or a clock. Task (c2) fills three small input records from the task (b) index and the
issuance ledger; this module settles them (docs/backtest-plan.md sections 7 and 8, the contract's staking,
settlement and costs sections).

Inputs:
- IssuedBet: one issued decision (decision_id, variant, market_id, selection_id, stake_pence > 0, side BACK, basis
  BSP). Only issued decisions are bets: abstained, unsupported and not_selected rows, and NO_VIEW markets (no
  decision anchor, so never population), stay in c2's issuance ledger and never reach here. Eligibility is c2's
  decision-time fact; nothing below can drop or add a bet. decision_id is c2's stable id of the issuance-ledger row
  (market, runner, decision point; plan section 6), unique within a variant: each variant has one outcome per row,
  so two variants backing the same runner at the same decision point share the id, and (variant, decision_id) is
  the bet's key.
- MarketOutcome: what the evaluator alone knows about one market, never shown to a strategy. Its settlement
  indicators are tri-state, True, False or None, and None (unknown) always leaves the market's bets unresolved, so
  an empty or unreadable outcome is unresolved, never void and never a zero return. read_complete is the
  outcome source's completeness (the index's read_complete), separate from decision eligibility: a market eligible
  at its decision time whose file later failed to read keeps its bets, unresolved. closed: the final market status
  is CLOSED (the index's final_status, not closed_reached). market_void: positive evidence of a void; c2 sets False
  only from a complete result, never from absence. result_revised_after_close is the index's
  results_revised_after_close, dead_heat its dead_heat, bsp_reconciled whether the last bspReconciled in its
  outcome_definition is true, and declared_winners its declared_number_of_winners (None when unknown, which leaves
  the bets unresolved like any unknown indicator). removed_after_decision: the selection ids ACTIVE in the latest
  definition at or before the decision time and REMOVED or REMOVED_VACANT at any later pt (c2 compares its
  decision-time view with the index's removals); None when unknown. runners: final status and bsp of each runner
  listed at the end (the index's final_runners with listed true; an unlisted runner's old status is no result).
  first_published_off is the first-published scheduled off (aware, kept in UTC), used for chronology; race_day
  is derived from it (race_day_of): its calendar date in Europe/London.
- RunnerOutcome: final status (any str, compared exactly: text the index passes through unclassified, such as
  "REMOVED VACANT" or "", is unexpected, so unresolved, never refused; or None) and final bsp, a Decimal with the
  precision supplied (no tick snapping, plan section 7), or None. A float is refused, as is a Decimal with more than
  MAX_BSP_PLACES places: Decimal(3.17) is 3.1699999999999999289457264239899814128875732421875, the binary float, not
  the 3.17 the file said (it would settle 216p instead of 217p on 100p). decimal_from_json_number recovers the
  literal from a float the index parsed (repr is the shortest text that reads back as the same float, which is the
  file's literal for any literal of at most 15 significant digits) and never exceeds the bound for a usable bsp:
  repr has at most 17 significant digits, so at most 16 places at 1.01 or more (the index's final bsp is null
  below MIN_BSP), while the exact expansion of the float nearest a literal in [MIN_BSP, MAX_BSP] of at most 10
  places that it does not represent exactly has more than 20 (such a float is no multiple of 2^-20: a literal of
  at most 10 places lies at least 2^10 / (10^10 * 2^20), about 1e-13, from every multiple, more than half a float
  step below 1024).

Market type: P0 settles single-winner WIN markets only. The cohort, c2's decision-time rule, admits WIN markets
only (contract population.market_types); MarketOutcome carries no market type, so the guard here is the declared
number of winners: anything but 1 (PLACE and EACH_WAY markets declare their number of places; flumine reads 0 as a
variable number) is not_single_winner_market, and a PLACED status on another runner is unexpected.

Settlement (plan section 7; every case in the contract's settlement section must be "unresolved", the only rule
schema v1 allows, or AccountingRules refuses). A bet is resolved only when none of UNRESOLVED_REASONS applies: the
outcome read whole, the market CLOSED at the end, not void, not revised after close, BSP reconciled, exactly one
winner declared, no dead heat (flag False and no more WINNERs than declared), at least one WINNER, no other cohort
runner removed after the decision, every other listed runner with a final status (WINNER, LOSER, REMOVED,
REMOVED_VACANT or HIDDEN: None, ACTIVE, PLACED or other text leaves the WINNER count, so a dead heat, unjudgeable),
our runner not removed, listed with status WINNER or LOSER and a bsp in [MIN_BSP, MAX_BSP]. A LOSER needs its bsp
too: the table settles "WINNER/LOSER with BSP" and anything else is missing_bsp_or_result. Every applicable reason
is recorded in precedence order (reasons) and the first is the bet's reason; SETTLEMENT_CASE maps each to the
contract case that governs it (an unknown indicator, and a result the P0 rule cannot settle, map to
missing_bsp_or_result, never to market_void). 1001 is the sentinel Betfair's BSP archive documents for no backers
at SP (docs/data-audit.md) and lies above MAX_BSP, the top of the price ladder; 0 or anything below 1.01 is not a
price. An unresolved bet keeps its stake as exposure (S_U) and has gross_pence None: it is never settled as zero and
never dropped.

Amounts: a resolved WINNER is +stake * (bsp - 1), computed exactly in Decimal and then rounded down to the penny
(WIN_ROUNDING, the single rounding point of a bet); a LOSER is -stake. Assumption: Betfair's own penny rounding of
BSP winnings is unverified (plan section 7 claims no penny-exact reconstruction), so the rounding goes against us,
as pnl.order_pnl_pence already floors profits. Commission is per (variant, market) on the positive net of that
variant's bets in that market, c * max(g, 0), at the contract's rate taken exactly (Decimal(repr(rate))) and
rounded to the penny by the contract's rounding (per_market_ceil_penny: up). Variants are never netted together:
a candidate and a baseline in one market are two separate markets for commission.

Mixed markets: when some of a variant's bets in a market are unresolved, the whole (variant, market) is unresolved
(every bet in it: the resolvable ones with reason market_partly_unresolved, settlement case None, since it is
this accounting rule rather than a contract case), because commission is a market-level amount that cannot be
computed from part of a market. This is the conservative choice (plan section 8 rederives the formulas for more
than one bet per market and section 7 leaves unresolved what cannot be computed): every stake stays in S_U, so the
pessimistic all-issued ROI counts all of an unresolved market's stakes lost with no commission. For P0's one bet
per variant per market that is the exact worst case; with more bets per market on different runners of a
single-winner market they cannot all lose, so it is a valid lower bound, not the exact worst case (plan section 8).
P0 issues at most one bet per variant per market, so for P0 a mixed market never arises.

Output (settle): a Settlement holding one BetResult per bet (status won, lost or unresolved, reason, reasons,
settlement case, bsp, gross_pence) and one MarketResult per (variant, market) (bets, stake, resolved, gross,
commission, net, unresolved stake, reasons), both in chronological order (first_published_off, then market id
numerically and then as text, then variant, selection and decision id), so the result does not depend on input
order. A bet whose market has no MarketOutcome, a duplicate (variant, decision_id) or market outcome, a variant the
contract does not declare or a stake other than the contract's fixed stake is refused (ValueError), never settled:
c2 gives a market the index says nothing about MarketOutcome.unknown(...), which leaves its bets unresolved with
explicit reasons. A ValueError from IssuedBet, RunnerOutcome or MarketOutcome means c2 built a record of the wrong
type or shape (a float bsp, a naive time), not a data oddity: unknown status text is accepted, and c2 maps any
value the index passes through raw but which is not of the record's type (a numberOfWinners that is not a
non-negative int, say) to None, so odd data comes out unresolved with a reason instead of aborting the run.

race_day (race_day_of): the calendar date of the first-published scheduled off in Europe/London, computed without
zoneinfo data from the rule in force since 1996 (British Summer Time from 01:00 UTC on the last Sunday in March to
01:00 UTC on the last Sunday in October; Ireland changes at the same instants, so the date is the same in Dublin).
Years before 1996 are refused; a future change of the UK rule would need a new version of this function. The
index's months and path dates are UTC; the two dates differ only for an off between 23:00 and 24:00 UTC in summer.
Standard library only; contract.py is the only backtest module imported.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, Inexact, localcontext
import re

from .contract import SETTLEMENT_CASES, SETTLEMENT_RULES, STAKE_RANGE_PENCE, validate_contract

SIDES = ("BACK",)
BASES = ("BSP",)
COMMISSION_BASIS = "positive_net_market_winnings"
COMMISSION_ROUNDING = {"per_market_ceil_penny": ROUND_CEILING, "per_market_half_up_penny": ROUND_HALF_UP}
WIN_ROUNDING = ROUND_FLOOR                                   # a win is rounded down to the penny, against us
MAX_RATE_PLACES = 12
MIN_BSP = Decimal("1.01")                                    # the lowest price on the ladder
MAX_BSP = Decimal("1000")                                    # the highest; 1001 is the no-backers sentinel
MAX_BSP_PLACES = 20                                          # a float's repr: at most 16; its exact expansion: > 40
LONDON_RULE_FROM_YEAR = 1996
WON, LOST, UNRESOLVED = "won", "lost", "unresolved"
BET_STATUSES = (LOST, UNRESOLVED, WON)
RESULT_STATUSES = {"LOSER": LOST, "WINNER": WON}
REMOVED_STATUSES = ("REMOVED", "REMOVED_VACANT")
# Final statuses of a runner that is not a WINNER of a single-winner market; any other (None, ACTIVE, PLACED, text)
# on another listed runner of a CLOSED market leaves the WINNER count unjudgeable.
NON_WINNING_FINAL_STATUSES = ("HIDDEN", "LOSER", "REMOVED", "REMOVED_VACANT")
# (reason, the contract settlement case that governs it), in precedence order: market-level reasons first.
_REASONS = (
    ("outcome_incomplete", "missing_bsp_or_result"),               # read_complete False
    ("outcome_completeness_unknown", "missing_bsp_or_result"),     # read_complete None
    ("market_not_closed", "missing_bsp_or_result"),                # closed False: no CLOSED definition at the end
    ("closed_unknown", "missing_bsp_or_result"),
    ("market_void", "market_void"),
    ("void_unknown", "missing_bsp_or_result"),                     # unknown is never void
    ("result_revised_after_close", "result_revised_after_close"),
    ("revision_unknown", "missing_bsp_or_result"),
    ("bsp_not_reconciled", "missing_bsp_or_result"),
    ("bsp_reconciled_unknown", "missing_bsp_or_result"),
    ("not_single_winner_market", "missing_bsp_or_result"),         # declared_winners not 1: not a P0 WIN result
    ("dead_heat", "dead_heat"),                                    # the flag, or more WINNERs than declared (>= 1)
    ("dead_heat_unknown", "missing_bsp_or_result"),                # the flag or declared_winners None
    ("no_winner_in_result", "missing_bsp_or_result"),              # CLOSED with no runner WINNER
    ("removals_unknown", "missing_bsp_or_result"),
    ("other_runner_removed_after_decision", "other_runner_removed_after_decision"),
    ("other_runner_status_unexpected", "missing_bsp_or_result"),   # CLOSED, another listed runner not final
    ("own_runner_removed_after_decision", "own_runner_removed_after_decision"),
    ("own_runner_not_listed", "missing_bsp_or_result"),
    ("own_status_unknown", "missing_bsp_or_result"),
    ("own_status_unexpected", "missing_bsp_or_result"),            # PLACED, ACTIVE, HIDDEN or any other text
    ("bsp_missing", "missing_bsp_or_result"),
    ("bsp_out_of_range", "missing_bsp_or_result"),                 # below 1.01, above 1000 (the 1001 sentinel)
    ("market_partly_unresolved", None),                            # this module's mixed-market rule
)
UNRESOLVED_REASONS = tuple(reason for reason, _ in _REASONS)
SETTLEMENT_CASE = dict(_REASONS)
_PRECEDENCE = {reason: index for index, reason in enumerate(UNRESOLVED_REASONS)}

_DECISION_ID_RE = re.compile(r"[\x21-\x7e]{1,128}")
_VARIANT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MARKET_ID_RE = re.compile(r"[0-9]{1,6}\.[0-9]{1,15}")
_UTC_TEXT_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?Z")
_NUMBER_TEXT_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")


def _positive_int(value, name, high=None):
    if type(value) is not int or value < 1 or (high is not None and value > high):
        raise ValueError(f"{name}_not_a_positive_integer")
    return value


def _tri_state(value, name):
    if value is not None and type(value) is not bool:
        raise ValueError(f"{name}_not_true_false_or_none")
    return value


def market_sort_key(market_id):
    """(exchange, number, text): integers first, so market 1.9 sorts before 1.10; the text last, so ids that differ
    only in leading zeros (1.01 and 1.1, which real Betfair ids do not have) still sort in one fixed order."""
    exchange, _, number = market_id.partition(".")
    return int(exchange), int(number), market_id


def utc_from_text(text):
    """An aware UTC datetime from a Betfair timestamp such as 2015-05-01T13:30:00.000Z (ValueError otherwise)."""
    match = _UTC_TEXT_RE.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise ValueError("timestamp_malformed")
    *parts, fraction = match.groups()
    micros = int((fraction or "0").ljust(6, "0"))
    return datetime(*(int(part) for part in parts), micros, tzinfo=timezone.utc)


def _last_sunday(year, month):
    end = date(year, month, 31)                                # March and October both have 31 days
    return end - timedelta(days=(end.weekday() - 6) % 7)


def race_day_of(moment):
    """The Europe/London calendar date of an aware moment, by the UK rule in force since 1996 (module docstring)."""
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("moment_not_aware")
    utc = moment.astimezone(timezone.utc)
    if utc.year < LONDON_RULE_FROM_YEAR:
        raise ValueError("before_london_rule")
    start = datetime.combine(_last_sunday(utc.year, 3), time(1), tzinfo=timezone.utc)
    end = datetime.combine(_last_sunday(utc.year, 10), time(1), tzinfo=timezone.utc)
    return (utc + timedelta(hours=1 if start <= utc < end else 0)).date()


def decimal_from_json_number(value):
    """Decimal of a JSON number as the index parsed it, with the file's precision; None stays None.

    int exactly; float by repr (the shortest text that reads back as that float: the file's literal whenever it
    had at most 15 significant digits); a number text as written. bool, NaN, infinity and anything else: ValueError."""
    if value is None:
        return None
    if type(value) is int:
        return Decimal(value)
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("number_not_finite")
        return Decimal(repr(value))
    if isinstance(value, str) and _NUMBER_TEXT_RE.fullmatch(value):
        return Decimal(value)
    raise ValueError("not_a_json_number")


@dataclass(frozen=True)
class IssuedBet:
    """One issued decision of one variant: a fixed-stake BACK at BSP on one runner of one market.

    decision_id names the issuance-ledger row and is unique within a variant; variants may share it."""

    decision_id: str
    variant: str
    market_id: str
    selection_id: int
    stake_pence: int
    side: str
    basis: str

    def __post_init__(self):
        if not (isinstance(self.decision_id, str) and _DECISION_ID_RE.fullmatch(self.decision_id)):
            raise ValueError("decision_id_malformed")
        if not (isinstance(self.variant, str) and _VARIANT_RE.fullmatch(self.variant)):
            raise ValueError("variant_malformed")
        if not (isinstance(self.market_id, str) and _MARKET_ID_RE.fullmatch(self.market_id)):
            raise ValueError("market_id_malformed")
        _positive_int(self.selection_id, "selection_id")
        _positive_int(self.stake_pence, "stake_pence", STAKE_RANGE_PENCE[1])
        if self.side not in SIDES:
            raise ValueError("side_not_supported")
        if self.basis not in BASES:
            raise ValueError("basis_not_supported")


@dataclass(frozen=True)
class RunnerOutcome:
    """A runner's final status (any str or None) and final bsp (Decimal with its supplied precision, or None)."""

    status: object
    bsp: object

    def __post_init__(self):
        if self.status is not None and not isinstance(self.status, str):
            raise ValueError("status_malformed")
        if self.bsp is None:
            return
        if type(self.bsp) is not Decimal:
            raise ValueError("bsp_not_decimal")
        if not self.bsp.is_finite():
            raise ValueError("bsp_not_finite")
        if self.bsp.as_tuple().exponent < -MAX_BSP_PLACES:
            raise ValueError("bsp_too_precise")


@dataclass(frozen=True)
class MarketOutcome:
    """Evaluator-only facts about one market (module docstring); race_day is derived from first_published_off.

    runners is given as a mapping {selection_id: RunnerOutcome} and kept as sorted pairs; runner(id) looks one up."""

    market_id: str
    first_published_off: datetime
    read_complete: object
    closed: object
    market_void: object
    result_revised_after_close: object
    bsp_reconciled: object
    dead_heat: object
    declared_winners: object
    removed_after_decision: object
    runners: object
    race_day: date = field(init=False)
    _by_id: dict = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if not (isinstance(self.market_id, str) and _MARKET_ID_RE.fullmatch(self.market_id)):
            raise ValueError("market_id_malformed")
        if not isinstance(self.first_published_off, datetime) or self.first_published_off.utcoffset() is None:
            raise ValueError("first_published_off_not_aware")
        object.__setattr__(self, "first_published_off", self.first_published_off.astimezone(timezone.utc))
        object.__setattr__(self, "race_day", race_day_of(self.first_published_off))
        for name in ("read_complete", "closed", "market_void", "result_revised_after_close", "bsp_reconciled",
                     "dead_heat"):
            _tri_state(getattr(self, name), name)
        if self.declared_winners is not None and (type(self.declared_winners) is not int or self.declared_winners < 0):
            raise ValueError("declared_winners_not_a_count")
        removed = self.removed_after_decision
        if removed is not None:
            if not isinstance(removed, (frozenset, set, tuple, list)):
                raise ValueError("removed_after_decision_not_a_set")
            removed = frozenset(_positive_int(item, "removed_selection_id") for item in removed)
            object.__setattr__(self, "removed_after_decision", removed)
        if not hasattr(self.runners, "items"):
            raise ValueError("runners_not_a_mapping")
        pairs = []
        for selection_id, runner in self.runners.items():
            _positive_int(selection_id, "runner_selection_id")
            if not isinstance(runner, RunnerOutcome):
                raise ValueError("runner_not_a_runner_outcome")
            pairs.append((selection_id, runner))
        pairs.sort(key=lambda pair: pair[0])
        object.__setattr__(self, "runners", tuple(pairs))
        object.__setattr__(self, "_by_id", dict(pairs))

    @classmethod
    def unknown(cls, market_id, first_published_off):
        """A market the index says nothing about after its decision: every indicator None, no runners."""
        return cls(market_id=market_id, first_published_off=first_published_off, read_complete=None, closed=None,
                   market_void=None, result_revised_after_close=None, bsp_reconciled=None, dead_heat=None,
                   declared_winners=None, removed_after_decision=None, runners={})

    def runner(self, selection_id):
        return self._by_id.get(selection_id)


@dataclass(frozen=True)
class AccountingRules:
    """The contract's commission rate (exact Decimal), commission rounding and settlement rules, and optionally its
    fixed stake and variant ids, which every bet must then match."""

    commission_rate: Decimal
    rounding: str
    settlement: object = None
    stake_pence: object = None
    variants: object = None

    def __post_init__(self):
        rate = self.commission_rate
        if type(rate) is not Decimal or not rate.is_finite() or not Decimal(0) <= rate < Decimal(1):
            raise ValueError("commission_rate_not_a_decimal_fraction")
        if rate.as_tuple().exponent < -MAX_RATE_PLACES:
            raise ValueError("commission_rate_too_precise")
        if self.rounding not in COMMISSION_ROUNDING:
            raise ValueError("rounding_not_supported")
        settlement = dict.fromkeys(SETTLEMENT_CASES, "unresolved") if self.settlement is None else self.settlement
        if not hasattr(settlement, "items") or sorted(settlement) != sorted(SETTLEMENT_CASES):
            raise ValueError("settlement_cases_not_the_contract_cases")
        if any(rule not in SETTLEMENT_RULES for rule in settlement.values()):
            raise ValueError("settlement_rule_not_supported")
        object.__setattr__(self, "settlement", tuple(sorted(settlement.items())))
        if self.stake_pence is not None:
            _positive_int(self.stake_pence, "stake_pence", STAKE_RANGE_PENCE[1])
        if self.variants is not None:
            if isinstance(self.variants, (str, bytes)):              # one id given bare would split into letters
                raise ValueError("variants_malformed")
            variants = tuple(self.variants)
            if not variants or any(not (isinstance(v, str) and _VARIANT_RE.fullmatch(v)) for v in variants):
                raise ValueError("variants_malformed")
            object.__setattr__(self, "variants", tuple(sorted(set(variants))))

    @classmethod
    def from_contract(cls, contract):
        """The rules a valid contract (draft or frozen; validate_contract finds no problem) declares."""
        problems, _ = validate_contract(contract)
        if problems:
            raise ValueError("contract_invalid")
        staking, costs = contract["staking"], contract["costs"]
        if staking["side"] not in SIDES or staking["price"] not in BASES or costs["basis"] != COMMISSION_BASIS:
            raise ValueError("contract_staking_not_supported")
        return cls(commission_rate=Decimal(repr(costs["commission_rate"])), rounding=costs["rounding"],
                   settlement=contract["settlement"], stake_pence=staking["stake_pence"],
                   variants=[variant["id"] for variant in contract["variants"]])


@dataclass(frozen=True)
class BetResult:
    """One bet settled: status won, lost or unresolved; gross_pence and bsp are None unless resolved."""

    decision_id: str
    variant: str
    market_id: str
    selection_id: int
    stake_pence: int
    status: str
    reason: object
    reasons: tuple
    settlement_case: object
    bsp: object
    gross_pence: object


@dataclass(frozen=True)
class MarketResult:
    """One (variant, market): its bets netted, commission charged once; amounts None when unresolved."""

    variant: str
    market_id: str
    first_published_off: datetime
    race_day: date
    bet_count: int
    stake_pence: int
    resolved: bool
    gross_pence: object
    commission_pence: object
    net_pence: object
    unresolved_stake_pence: int
    reasons: tuple

    @property
    def resolved_stake_pence(self):
        return self.stake_pence if self.resolved else 0


@dataclass(frozen=True)
class Settlement:
    rules: AccountingRules
    bets: tuple
    markets: tuple

    def variants(self):
        return tuple(sorted({market.variant for market in self.markets}))


def win_gross_pence(stake_pence, bsp):
    """stake * (bsp - 1), exact, rounded down to the penny (WIN_ROUNDING)."""
    with localcontext() as context:
        context.prec = 80
        context.traps[Inexact] = True
        return int((Decimal(stake_pence) * (bsp - 1)).to_integral_value(rounding=WIN_ROUNDING))


def commission_pence(net_pence, rate, rounding):
    """rate * max(net, 0), exact, rounded to the penny by the contract's rounding (ceil: up)."""
    if type(net_pence) is not int:
        raise ValueError("net_pence_not_an_integer")
    if net_pence <= 0:
        return 0
    with localcontext() as context:
        context.prec = 80
        context.traps[Inexact] = True
        return int((rate * Decimal(net_pence)).to_integral_value(rounding=COMMISSION_ROUNDING[rounding]))


def market_reasons(outcome):
    """Unresolved reasons that apply to every bet in the market, in precedence order."""
    found = []
    for value, bad, unknown in ((outcome.read_complete, "outcome_incomplete", "outcome_completeness_unknown"),
                                (outcome.closed, "market_not_closed", "closed_unknown")):
        if value is None:
            found.append(unknown)
        elif value is False:
            found.append(bad)
    for value, bad, unknown in ((outcome.market_void, "market_void", "void_unknown"),
                                (outcome.result_revised_after_close, "result_revised_after_close",
                                 "revision_unknown")):
        if value is None:
            found.append(unknown)
        elif value is True:
            found.append(bad)
    if outcome.bsp_reconciled is None:
        found.append("bsp_reconciled_unknown")
    elif outcome.bsp_reconciled is False:
        found.append("bsp_not_reconciled")
    winners = sum(1 for _, runner in outcome.runners if runner.status == "WINNER")
    declared = outcome.declared_winners
    if declared is not None and declared != 1:
        found.append("not_single_winner_market")
    if outcome.dead_heat is True or (declared is not None and declared >= 1 and winners > declared):
        found.append("dead_heat")
    elif outcome.dead_heat is None or declared is None:
        found.append("dead_heat_unknown")
    if outcome.closed is True and winners == 0:
        found.append("no_winner_in_result")
    if outcome.removed_after_decision is None:
        found.append("removals_unknown")
    return found


def bet_reasons(outcome, bet, common):
    """Every unresolved reason for one bet (the market's common ones plus its runner's), in precedence order."""
    found = list(common)
    removed = outcome.removed_after_decision
    selection = bet.selection_id
    if removed is not None and removed - {selection}:
        found.append("other_runner_removed_after_decision")
    if outcome.closed is True and any(other != selection and runner.status != "WINNER"
                                      and runner.status not in NON_WINNING_FINAL_STATUSES
                                      for other, runner in outcome.runners):
        found.append("other_runner_status_unexpected")
    runner = outcome.runner(selection)
    status = runner.status if runner is not None else None
    if (removed is not None and selection in removed) or status in REMOVED_STATUSES:
        found.append("own_runner_removed_after_decision")
    if runner is None:
        found.append("own_runner_not_listed")
    elif status is None:
        found.append("own_status_unknown")
    elif status not in RESULT_STATUSES and status not in REMOVED_STATUSES:
        found.append("own_status_unexpected")
    if runner is not None and status not in REMOVED_STATUSES:
        if runner.bsp is None:
            found.append("bsp_missing")
        elif not MIN_BSP <= runner.bsp <= MAX_BSP:
            found.append("bsp_out_of_range")
    return sorted(set(found), key=_PRECEDENCE.__getitem__)


def _bet_result(bet, status, reasons, bsp=None, gross=None):
    reason = reasons[0] if reasons else None
    return BetResult(decision_id=bet.decision_id, variant=bet.variant, market_id=bet.market_id,
                     selection_id=bet.selection_id, stake_pence=bet.stake_pence, status=status, reason=reason,
                     reasons=tuple(reasons), settlement_case=SETTLEMENT_CASE[reason] if reason else None, bsp=bsp,
                     gross_pence=gross)


def settle(bets, outcomes, rules):
    """A Settlement of every bet against its market's outcome under rules (module docstring); ValueError on
    inconsistent input (a duplicate (variant, decision_id) or market outcome, a bet without a market outcome, an
    undeclared variant, an unfixed stake)."""
    if not isinstance(rules, AccountingRules):
        raise ValueError("rules_not_accounting_rules")
    bets, by_market = tuple(bets), {}
    for outcome in outcomes:
        if not isinstance(outcome, MarketOutcome):
            raise ValueError("outcome_not_a_market_outcome")
        if outcome.market_id in by_market:
            raise ValueError(f"duplicate_market_outcome:{outcome.market_id}")
        by_market[outcome.market_id] = outcome
    seen, groups = set(), {}
    for bet in bets:
        if not isinstance(bet, IssuedBet):
            raise ValueError("bet_not_an_issued_bet")
        key = (bet.variant, bet.decision_id)
        if key in seen:
            raise ValueError(f"duplicate_decision_id:{bet.variant}:{bet.decision_id}")
        seen.add(key)
        if bet.market_id not in by_market:
            raise ValueError(f"missing_market_outcome:{bet.market_id}")
        if rules.variants is not None and bet.variant not in rules.variants:
            raise ValueError(f"undeclared_variant:{bet.variant}")
        if rules.stake_pence is not None and bet.stake_pence != rules.stake_pence:
            raise ValueError(f"stake_not_the_fixed_stake:{bet.variant}:{bet.decision_id}")
        groups.setdefault((bet.variant, bet.market_id), []).append(bet)
    common = {market_id: market_reasons(by_market[market_id]) for market_id in {m for _, m in groups}}
    bet_results, market_results = [], []
    for (variant, market_id), members in groups.items():
        outcome = by_market[market_id]
        members.sort(key=lambda bet: (bet.selection_id, bet.decision_id))
        provisional = [(bet, bet_reasons(outcome, bet, common[market_id])) for bet in members]
        resolved = not any(reasons for _, reasons in provisional)
        gross_total, results = 0, []
        for bet, reasons in provisional:
            if reasons:
                results.append(_bet_result(bet, UNRESOLVED, reasons))
            elif not resolved:
                results.append(_bet_result(bet, UNRESOLVED, ["market_partly_unresolved"]))
            else:
                runner = outcome.runner(bet.selection_id)
                status = RESULT_STATUSES[runner.status]
                gross = win_gross_pence(bet.stake_pence, runner.bsp) if status == WON else -bet.stake_pence
                gross_total += gross
                results.append(_bet_result(bet, status, [], runner.bsp, gross))
        bet_results += results
        stake = sum(bet.stake_pence for bet in members)
        reasons = sorted({result.reason for result in results if result.reason}, key=_PRECEDENCE.__getitem__)
        commission = commission_pence(gross_total, rules.commission_rate, rules.rounding) if resolved else None
        market_results.append(MarketResult(
            variant=variant, market_id=market_id, first_published_off=outcome.first_published_off,
            race_day=outcome.race_day, bet_count=len(members), stake_pence=stake, resolved=resolved,
            gross_pence=gross_total if resolved else None, commission_pence=commission,
            net_pence=gross_total - commission if resolved else None,
            unresolved_stake_pence=0 if resolved else stake, reasons=tuple(reasons)))

    def chronology(item):
        outcome = by_market[item.market_id]
        return outcome.first_published_off, market_sort_key(item.market_id), item.variant

    bet_results.sort(key=lambda b: (*chronology(b), b.selection_id, b.decision_id))
    market_results.sort(key=chronology)
    return Settlement(rules=rules, bets=tuple(bet_results), markets=tuple(market_results))
