"""Per-variant P0 metrics from a Settlement: N_R, S_R, S_U, both ROIs, coverage, drawdown and a block bootstrap.

Definitions (docs/backtest-plan.md section 8; Astra's R1 N3), always for one variant: N_R is the net of its
resolved (variant, market)s after each market's commission, S_R their stakes and S_U the stakes of its unresolved
bets. Resolved ROI = N_R / S_R (the primary metric, sum_net_over_sum_stakes_resolved); it is conditional on
settlement being available, so its sign alone says nothing about every issued bet. Pessimistic all-issued ROI =
(N_R - S_U) / (S_R + S_U) (roi_worst_case_unresolved_lost): every unresolved bet lost, with no commission (an
unresolved market is unresolved whole: accounting module docstring). It is the exact worst case for P0's one bet
per variant per market; with more bets per market it is a valid lower bound, not the exact worst case (plan
section 8). A zero denominator leaves a ROI undefined (None), never 0. Bet counts are given by status (won, lost,
unresolved) and by primary reason, and every reason any bet carries is counted in reason_mentions. Ratios are exact
Fractions.

Settlement coverage (settlement_coverage_by_count and _by_stake) is resolved over issued, by bet count and by stake
(None when nothing was issued). It is not the contract's required secondary metric coverage, which plan section 8
defines as the cohort, decision, abstain and unsupported counts of the issuance ledger: that comes from task (c2)'s
ledger, which this module never sees, and reports beside these numbers, never in their place.

Variants are never netted together. Commission was already charged per (variant, market) by settle, and nothing
here adds, subtracts or pools two variants: a comparison of a candidate with a baseline is two VariantMetrics (or
two BootstrapResults) side by side, two numbers, never a difference or a combined net. A variant id the
settlement's rules do not declare is refused (undeclared_variant, as settle refuses it), so a typo cannot pass for
an idle variant; without declared variants (rules.variants None) any id is taken, and one with no bets has zero
counts and every ratio None.

Drawdown (max_drawdown_global_chronology): the cumulative resolved net from 0 over global chronology, one point per
distinct first-published scheduled off. Markets with the same off are simultaneous in that chronology, so their
nets enter one point together and the order of their ids cannot move the drawdown; per market traces are never
concatenated. The maximum drawdown is the largest fall from a running peak (the start, 0, counts as a peak) to a
later point; peak_off is None when that peak is the start. Unresolved markets do not move the curve; their count
and stake are reported beside it.

Block bootstrap (uncertainty: block_bootstrap, net_by market): each (variant, market) is first netted (its net,
resolved stake and unresolved stake), then summed into blocks (race_day: the market's Europe/London race day;
iso_week and calendar_month group race days). The blocks are the distinct blocks holding at least one issued bet
of the variant, in sorted order. Each resample draws that many blocks with replacement and takes both ROIs as a
ratio of sums over the drawn blocks. The interval is the percentile interval by nearest rank (percentile_interval):
with B sorted resample values and q = (1 - level) / 2, low is value ceil(q * B) and high value ceil((1 - q) * B),
counted from 1, with q exact (the contract's confidence_level read as Fraction(repr(level))). A ROI undefined in any
resample (no resolved stake drawn) leaves that interval None, with the count of undefined resamples beside it,
instead of an interval conditioned on the defined ones. Fewer than MIN_BLOCKS blocks leave both intervals None with
intervals_undefined_reason too_few_blocks: every resample of one block is that block, so its "interval" would be
the point estimate with zero width, not an uncertainty (the undefined counts are still given). Two or more blocks
give an interval, however crude; blocks is reported beside it. Resamples, level, block and seed come from the
contract (BootstrapPlan.from_contract: uncertainty and seeds.bootstrap).

Randomness: HashStream, with no random module and no clock. Word k of a stream is bytes 8(k mod 4) to 8(k mod 4)+8,
big-endian, of sha256(STREAM_DOMAIN, 0x00, canonical JSON [seed, label], 0x00, floor(k / 4) as 8 bytes big-endian).
below(n) takes words until one is under the largest multiple of n not above 2^64 and returns it mod n: rejection
sampling, so there is no modulo bias. The label is "<variant>|<block>", so a variant's draws do not depend on which
other variants run. sha256, integer and Fraction arithmetic are exact, so the same settlement, plan and seed give
identical results on any Python 3.10+.

as_record turns any result here or any record in accounting (inputs included) into JSON-ready data: a Fraction as
{"exact": "n/d", "decimal": fixed point with 8 places, rounded half even exactly}, a Decimal as its text, dates and
UTC datetimes as ISO text, a set as a sorted list. Standard library only.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
from itertools import groupby
import math
from operator import itemgetter

from .accounting import BET_STATUSES, LOST, UNRESOLVED, UNRESOLVED_REASONS, WON, Settlement
from .contract import BLOCKS, RESAMPLE_RANGE, SEED_RANGE, canonical_json, validate_contract

STREAM_DOMAIN = b"betangel-research/backtest/hash-stream/v1"
WORD_RANGE = 1 << 64
INTERVAL_METHOD = "percentile_nearest_rank"
MIN_BLOCKS = 2
TOO_FEW_BLOCKS = "too_few_blocks"
DISPLAY_PLACES = 8


def _ratio(numerator, denominator):
    return None if denominator == 0 else Fraction(numerator, denominator)


def _check_variant(settlement, variant):
    declared = settlement.rules.variants
    if declared is not None and variant not in declared:
        raise ValueError(f"undeclared_variant:{variant}")


@dataclass(frozen=True)
class Drawdown:
    """Maximum drawdown of the cumulative resolved net in global chronology, in pence.

    peak_pence and trough_pence bound the deepest fall, at first-published offs peak_off and trough_off (UTC);
    peak_off is None when that peak is the start (0)."""

    max_drawdown_pence: int
    peak_pence: int
    trough_pence: int
    peak_off: object
    trough_off: object
    final_net_pence: int
    resolved_markets: int
    unresolved_markets: int
    unresolved_stake_pence: int


def max_drawdown(markets):
    """Drawdown over one variant's MarketResults (any order: they are put in global chronology here; markets with
    one off form one point, so their order among themselves is immaterial)."""
    ordered = sorted(markets, key=lambda m: m.first_published_off)
    if len({m.variant for m in ordered}) > 1:
        raise ValueError("drawdown_takes_one_variant")
    cumulative = peak = 0
    peak_off = None
    deepest = (0, 0, 0, None, None)                    # (drawdown, peak, trough, peak off, trough off)
    resolved = [m for m in ordered if m.resolved]
    for off, group in groupby(resolved, key=lambda m: m.first_published_off):
        cumulative += sum(market.net_pence for market in group)
        if cumulative > peak:
            peak, peak_off = cumulative, off
        if peak - cumulative > deepest[0]:
            deepest = (peak - cumulative, peak, cumulative, peak_off, off)
    unresolved = [m for m in ordered if not m.resolved]
    return Drawdown(max_drawdown_pence=deepest[0], peak_pence=deepest[1], trough_pence=deepest[2],
                    peak_off=deepest[3], trough_off=deepest[4], final_net_pence=cumulative,
                    resolved_markets=len(resolved), unresolved_markets=len(unresolved),
                    unresolved_stake_pence=sum(m.unresolved_stake_pence for m in unresolved))


@dataclass(frozen=True)
class VariantMetrics:
    """One variant's accounting (module docstring); counts are tuples of (name, count) pairs."""

    variant: str
    issued_bets: int
    issued_stake_pence: int
    resolved_bets: int
    resolved_stake_pence: int
    unresolved_bets: int
    unresolved_stake_pence: int
    resolved_markets: int
    unresolved_markets: int
    gross_resolved_pence: int
    commission_pence: int
    net_resolved_pence: int
    bets_by_status: tuple
    bets_by_reason: tuple
    reason_mentions: tuple
    settlement_coverage_by_count: object
    settlement_coverage_by_stake: object
    roi_resolved: object
    roi_all_issued_pessimistic: object
    drawdown: Drawdown


def variant_metrics(settlement, variant):
    """VariantMetrics of one variant (one with no bets: zero counts and every ratio None); undeclared: ValueError."""
    if not isinstance(settlement, Settlement):
        raise ValueError("not_a_settlement")
    _check_variant(settlement, variant)
    bets = [bet for bet in settlement.bets if bet.variant == variant]
    markets = [market for market in settlement.markets if market.variant == variant]
    resolved = [market for market in markets if market.resolved]
    s_r = sum(market.stake_pence for market in resolved)
    s_u = sum(market.unresolved_stake_pence for market in markets)
    n_r = sum(market.net_pence for market in resolved)
    issued_stake = sum(bet.stake_pence for bet in bets)
    status_counts = {status: sum(1 for bet in bets if bet.status == status) for status in BET_STATUSES}
    resolved_bets = status_counts[WON] + status_counts[LOST]
    # Two independent sums must agree: the bets' own stakes and statuses against the market totals.
    if (issued_stake != s_r + s_u or resolved_bets + status_counts[UNRESOLVED] != len(bets)
            or s_r != sum(bet.stake_pence for bet in bets if bet.status != UNRESOLVED)
            or n_r != sum(bet.gross_pence for bet in bets if bet.status != UNRESOLVED)
            - sum(market.commission_pence for market in resolved)):
        raise ValueError("reconciliation_failed")
    by_reason = {reason: sum(1 for bet in bets if bet.reason == reason) for reason in UNRESOLVED_REASONS}
    mentions = {reason: sum(1 for bet in bets if reason in bet.reasons) for reason in UNRESOLVED_REASONS}
    return VariantMetrics(
        variant=variant, issued_bets=len(bets), issued_stake_pence=issued_stake, resolved_bets=resolved_bets,
        resolved_stake_pence=s_r, unresolved_bets=status_counts[UNRESOLVED], unresolved_stake_pence=s_u,
        resolved_markets=len(resolved), unresolved_markets=len(markets) - len(resolved),
        gross_resolved_pence=sum(market.gross_pence for market in resolved),
        commission_pence=sum(market.commission_pence for market in resolved), net_resolved_pence=n_r,
        bets_by_status=tuple(sorted(status_counts.items())),
        bets_by_reason=tuple((reason, count) for reason, count in by_reason.items() if count),
        reason_mentions=tuple((reason, count) for reason, count in mentions.items() if count),
        settlement_coverage_by_count=_ratio(resolved_bets, len(bets)),
        settlement_coverage_by_stake=_ratio(s_r, issued_stake),
        roi_resolved=_ratio(n_r, s_r), roi_all_issued_pessimistic=_ratio(n_r - s_u, s_r + s_u),
        drawdown=max_drawdown(markets))


def metrics_by_variant(settlement, variants=None):
    """VariantMetrics for each variant, separately: the given ids, else the rules' declared variants, else every
    variant with a bet. Nothing is pooled across variants."""
    if variants is None:
        variants = settlement.rules.variants if settlement.rules.variants is not None else settlement.variants()
    return tuple(variant_metrics(settlement, variant) for variant in sorted(set(variants)))


class HashStream:
    """Deterministic uniform integers from sha256 (module docstring); no random module, no clock."""

    def __init__(self, seed, label):
        if type(seed) is not int or not SEED_RANGE[0] <= seed <= SEED_RANGE[1]:
            raise ValueError("seed_out_of_range")
        if not isinstance(label, str):
            raise ValueError("label_not_text")
        self._prefix = STREAM_DOMAIN + b"\x00" + canonical_json([seed, label]) + b"\x00"
        self._counter = 0
        self._words = []

    def word(self):
        """The next 64-bit word."""
        if not self._words:
            digest = hashlib.sha256(self._prefix + self._counter.to_bytes(8, "big")).digest()
            self._counter += 1
            self._words = [int.from_bytes(digest[start:start + 8], "big") for start in (24, 16, 8, 0)]
        return self._words.pop()

    def below(self, n):
        """A uniform integer in [0, n), by rejection sampling."""
        return self.draws(n, 1)[0]

    def draws(self, n, count):
        """count uniform integers in [0, n), in stream order."""
        if type(n) is not int or not 1 <= n <= WORD_RANGE:
            raise ValueError("bound_out_of_range")
        limit = WORD_RANGE - WORD_RANGE % n
        word, out = self.word, []
        while len(out) < count:
            value = word()
            if value < limit:
                out.append(value % n)
        return out


@dataclass(frozen=True)
class BootstrapPlan:
    """Resamples, confidence level (exact), seed and block of one block bootstrap."""

    resamples: int
    confidence_level: Fraction
    seed: int
    block: str = "race_day"

    def __post_init__(self):
        if type(self.resamples) is not int or not RESAMPLE_RANGE[0] <= self.resamples <= RESAMPLE_RANGE[1]:
            raise ValueError("resamples_out_of_range")
        if type(self.confidence_level) is not Fraction or not 0 < self.confidence_level < 1:
            raise ValueError("confidence_level_not_a_fraction_in_0_1")
        if type(self.seed) is not int or not SEED_RANGE[0] <= self.seed <= SEED_RANGE[1]:
            raise ValueError("seed_out_of_range")
        if self.block not in BLOCKS:
            raise ValueError("block_not_supported")

    @classmethod
    def from_contract(cls, contract):
        """The plan a valid contract declares: its uncertainty section and seeds.bootstrap."""
        problems, _ = validate_contract(contract)
        if problems:
            raise ValueError("contract_invalid")
        uncertainty = contract["uncertainty"]
        if uncertainty["method"] != "block_bootstrap" or uncertainty["net_by"] != "market":
            raise ValueError("uncertainty_not_supported")
        return cls(resamples=uncertainty["resamples"], confidence_level=Fraction(repr(uncertainty["confidence_level"])),
                   seed=contract["seeds"]["bootstrap"], block=uncertainty["block"])


@dataclass(frozen=True)
class Interval:
    low: Fraction
    high: Fraction


@dataclass(frozen=True)
class BootstrapResult:
    """Percentile intervals of both ROIs for one variant; an interval is None when any resample left it undefined,
    and both are None, with intervals_undefined_reason too_few_blocks, when blocks is below MIN_BLOCKS."""

    variant: str
    block: str
    blocks: int
    resamples: int
    confidence_level: Fraction
    seed: int
    method: str
    roi_resolved: object
    roi_resolved_undefined: int
    roi_all_issued_pessimistic: object
    roi_all_issued_pessimistic_undefined: int
    intervals_undefined_reason: object


def block_key(day, block):
    """The block a race day falls in: the day, its ISO (year, week) or its (year, month)."""
    if block == "race_day":
        return day
    if block == "iso_week":
        return tuple(day.isocalendar())[:2]
    if block == "calendar_month":
        return day.year, day.month
    raise ValueError("block_not_supported")


def _nearest_rank(ordered, q):
    return ordered[max(math.ceil(q * len(ordered)), 1) - 1]


def percentile_interval(values, level):
    """Interval(low, high) by nearest rank at level, an exact Fraction in (0, 1) (module docstring), or None when any
    value is None. A float level is refused: (1 - 0.95) / 2 is not 1/40, and ceil then misses the rank by one."""
    if type(level) is not Fraction or not 0 < level < 1:
        raise ValueError("level_not_a_fraction_in_0_1")
    values = list(values)
    if not values:
        raise ValueError("no_values")
    if any(value is None for value in values):
        return None
    ordered = sorted(values)
    tail = (1 - level) / 2
    return Interval(low=_nearest_rank(ordered, tail), high=_nearest_rank(ordered, 1 - tail))


def block_bootstrap(settlement, variant, plan):
    """BootstrapResult of one variant under plan (module docstring); an undeclared variant: ValueError."""
    if not isinstance(settlement, Settlement) or not isinstance(plan, BootstrapPlan):
        raise ValueError("bootstrap_takes_a_settlement_and_a_plan")
    _check_variant(settlement, variant)
    totals = {}
    for market in settlement.markets:
        if market.variant != variant:
            continue
        key = block_key(market.race_day, plan.block)
        net, s_r, s_u = totals.get(key, (0, 0, 0))
        if market.resolved:
            totals[key] = (net + market.net_pence, s_r + market.stake_pence, s_u)
        else:
            totals[key] = (net, s_r, s_u + market.unresolved_stake_pence)
    keys = sorted(totals)
    nets, resolved_stakes, unresolved_stakes = ([totals[key][i] for key in keys] for i in range(3))
    count = len(keys)
    resolved_values, pessimistic_values = [], []
    if count:
        stream = HashStream(plan.seed, f"{variant}|{plan.block}")
        for _ in range(plan.resamples):
            drawn = stream.draws(count, count)
            pick = itemgetter(*drawn) if count > 1 else (lambda series, i=drawn[0]: (series[i],))
            net, s_r, s_u = sum(pick(nets)), sum(pick(resolved_stakes)), sum(pick(unresolved_stakes))
            resolved_values.append(_ratio(net, s_r))
            pessimistic_values.append(_ratio(net - s_u, s_r + s_u))
    undefined = plan.resamples if not count else sum(1 for value in resolved_values if value is None)
    undefined_pessimistic = plan.resamples if not count else sum(1 for v in pessimistic_values if v is None)
    enough = count >= MIN_BLOCKS
    return BootstrapResult(
        variant=variant, block=plan.block, blocks=count, resamples=plan.resamples,
        confidence_level=plan.confidence_level, seed=plan.seed, method=INTERVAL_METHOD,
        roi_resolved=percentile_interval(resolved_values, plan.confidence_level) if enough else None,
        roi_resolved_undefined=undefined,
        roi_all_issued_pessimistic=percentile_interval(pessimistic_values, plan.confidence_level) if enough else None,
        roi_all_issued_pessimistic_undefined=undefined_pessimistic,
        intervals_undefined_reason=None if enough else TOO_FEW_BLOCKS)


def _decimal_text(value):
    """value rounded half even to DISPLAY_PLACES, exactly (round of a Fraction is exact), in fixed point."""
    scaled = round(value * 10 ** DISPLAY_PLACES)
    return f"{Decimal(f'{scaled}E-{DISPLAY_PLACES}'):f}"


def as_record(value):
    """JSON-ready data for any result of this module or record of accounting (module docstring); private fields
    skipped."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: as_record(getattr(value, f.name)) for f in fields(value) if not f.name.startswith("_")}
    if isinstance(value, Fraction):
        return {"exact": f"{value.numerator}/{value.denominator}", "decimal": _decimal_text(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [as_record(item) for item in value]
    if isinstance(value, (set, frozenset)):
        try:
            return [as_record(item) for item in sorted(value)]
        except TypeError:
            raise ValueError("not_recordable") from None
    if isinstance(value, dict):
        return {str(key): as_record(item) for key, item in value.items()}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise ValueError("not_recordable")
