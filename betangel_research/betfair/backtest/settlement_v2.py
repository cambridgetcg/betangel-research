"""Settlement rule v2, research only: market_void False from a complete published settlement, for a study that opts
in (docs/settlement-v2.md). Proposed 2026-10-31 under Yu's decision 2; it needs Astra's agreement before any study
relies on it.

It overrides c2 D11 option (a) and Q5 (outcome.py's docstring: market_void always None) only for a research study that
calls this module with RULE, the token its own registration names. Nothing else changes: outcome.market_outcome still
sets market_void None, so every P0 bet on BASIC stays unresolved (void_unknown); contract schema 1 still allows only
"unresolved" in settlement, and AccountingRules refuses anything else; the runner, the certification and pilot.py do
not import this module (a test pins that no module imports it), and contract p1's ledger keeps its own rules.

The rule, on the accounting.MarketOutcome that outcome.market_outcome builds from the primary member's index record
(CONDITIONS, in this order; unmet() names the ones not shown):
1. closed: the market is CLOSED at the end (closed True: the index's final_status, not closed_reached, so a market
   reopened or reset after CLOSED fails);
2. bsp_reconciled: the last bspReconciled is true (bsp_reconciled True);
3. runner_statuses_final: at least one runner is listed at the end, and every runner listed at the end has a final
   status, one of FINAL_STATUSES (WINNER, LOSER, REMOVED, REMOVED_VACANT, HIDDEN). ACTIVE, PLACED, null and any other
   text are not final. A runner not listed at the end is no result (accounting module docstring) and is not looked at;
4. winner_count_is_declared: the declared numberOfWinners is an int of at least 1, and exactly that many runners
   listed at the end are WINNER;
and the two guards accounting already applies: 5. read_whole (read_complete True) and 6. not_revised_after_close
(result_revised_after_close False). An unknown (None) never meets a condition.

apply() sets market_void False when every condition is met and market_void is None, and otherwise returns the outcome
unchanged. It never sets True: the files carry no void flag. It never turns a True into False, and it adds or removes
no other reason, so every other case stays with accounting. A dead heat (two WINNERs against numberOfWinners 1) fails
condition 4 and keeps void_unknown and dead_heat. A WIN market declaring 2 with two WINNERs meets the rule and is
still not_single_winner_market. A bet on a HIDDEN, removed or unpriced runner of a complete market stays unresolved for
its own reason. What it gives is an inference from the file that the market was settled normally. It is not Betfair's
settlement, and it is no evidence that the race was run or not voided. Results resting on it are labelled "settled
as published in the file, research only".

Report groups (GROUPS): a study reports unresolved bets apart, never mixed into the resolved figures, grouped by each
bet's full reasons tuple, not only its first reason. Each accounting reason but void_unknown is in at most one named
group. void_unknown is on every bet of a market the rule did not resolve, so it only says that the rule failed; the
other reasons say why. market_void (never set by this rule) is in no named group. A bet with no reason in a named
group is counted in OTHER. A bet may be in several groups, and only_here counts the bets whose every reason but
void_unknown is in that one group.

Sensitivity (sensitivity()): for one variant, the resolved ROI N_R / S_R (every unresolved bet dropped: the primary)
beside the worst case (N_R - S_U) / (S_R + S_U) (every unresolved bet lost, metrics' roi_all_issued_pessimistic),
whether the worst case turns a positive resolved ROI negative, and for each group the worst case with that group's
only_here bets dropped instead of counted lost. A dead heat settled by a dead-heat rule is not computed: the
repository cites no source for Betfair's dead-heat rule on BSP backs.

Coverage gate (coverage_check()): the rule rests on four definition fields. That ADVANCED and PRO files carry
bspReconciled is an inference from MARKET_DEF being in every tier (docs/backtest-plan.md section 2.1), not something
the repository has seen. A study therefore checks the coverage audit of its own data root first. For its tier and
market types, each of REQUIRED_FIELDS must be present in every market, and the audit must carry SIZE_COUNTS
(closed_no_winner and closed_bsp_not_reconciled were added for this rule). Any problem stops the study until a
person has explained it in the study's registration. Nothing in code ties coverage_check() to market_outcome(): the
gate is procedural, so a study that adds itself to the test that no module imports this one also shows its
coverage_check() result in its registration, and its review checks that. Standard library only.
"""

from dataclasses import dataclass, replace
from fractions import Fraction

from .accounting import UNRESOLVED, MarketOutcome
from .coverage_audit import AUDIT_KIND, AUDIT_SCHEMA_VERSION
from .metrics import variant_metrics
from .outcome import market_outcome as c2_market_outcome

RULE = "not_void_when_published_settlement_complete"
FINAL_STATUSES = ("HIDDEN", "LOSER", "REMOVED", "REMOVED_VACANT", "WINNER")
CONDITIONS = ("closed", "bsp_reconciled", "runner_statuses_final", "winner_count_is_declared", "read_whole",
              "not_revised_after_close")
VOID_UNKNOWN = "void_unknown"
OTHER = "other"
# (group, accounting reasons that place a bet in it), in report order.
GROUPS = (
    ("dead_heat", ("dead_heat",)),
    ("no_winner", ("no_winner_in_result",)),
    ("not_closed_or_not_reconciled", ("market_not_closed", "closed_unknown", "bsp_not_reconciled",
                                      "bsp_reconciled_unknown")),
    ("runner_not_final_or_not_listed", ("other_runner_status_unexpected", "own_runner_not_listed",
                                        "own_status_unknown", "own_status_unexpected")),
    ("not_single_winner_or_unjudgeable", ("not_single_winner_market", "dead_heat_unknown")),
    ("removed_after_decision", ("other_runner_removed_after_decision", "own_runner_removed_after_decision",
                                "removals_unknown")),
    ("revised_or_incomplete", ("result_revised_after_close", "revision_unknown", "outcome_incomplete",
                               "outcome_completeness_unknown")),
    ("bsp_unusable", ("bsp_missing", "bsp_out_of_range")),
    ("market_partly_unresolved", ("market_partly_unresolved",)),
)
GROUP_OF = {reason: group for group, reasons in GROUPS for reason in reasons}
REQUIRED_FIELDS = ("md.bspReconciled", "md.numberOfWinners", "md.runners.status", "md.status")
SIZE_COUNTS = ("closed_bsp_not_reconciled", "closed_dead_heat_not_assessable", "closed_no_winner", "dead_heats",
               "markets_never_closed", "markets_read_incomplete", "markets_with_unrecognised_runner_status",
               "results_revised_after_close")


def unmet(outcome):
    """The CONDITIONS an accounting.MarketOutcome does not show, in order; empty when its settlement is complete."""
    if not isinstance(outcome, MarketOutcome):
        raise ValueError("outcome_not_a_market_outcome")
    statuses = [runner.status for _, runner in outcome.runners]
    declared = outcome.declared_winners
    shown = {
        "closed": outcome.closed is True,
        "bsp_reconciled": outcome.bsp_reconciled is True,
        "runner_statuses_final": bool(statuses) and all(status in FINAL_STATUSES for status in statuses),
        "winner_count_is_declared": type(declared) is int and declared >= 1 and statuses.count("WINNER") == declared,
        "read_whole": outcome.read_complete is True,
        "not_revised_after_close": outcome.result_revised_after_close is False,
    }
    return tuple(condition for condition in CONDITIONS if not shown[condition])


def published_settlement_complete(outcome):
    return not unmet(outcome)


def apply(outcome):
    """outcome with market_void False when its published settlement is complete and market_void is None; else the
    same outcome (module docstring)."""
    if outcome.market_void is not None or unmet(outcome):
        return outcome
    return replace(outcome, market_void=False, runners=dict(outcome.runners))


def market_outcome(record, decision, *, void_rule):
    """outcome.market_outcome(record, decision) under this rule, for a study whose registration names RULE (passed as
    void_rule; anything else is refused, so the rule is never applied by default)."""
    if void_rule != RULE:
        raise ValueError("settlement_v2:rule_not_declared")
    return apply(c2_market_outcome(record, decision))


def groups_of(bet):
    """The named groups a settled bet is in (none when resolved, OTHER when no reason is in a named group)."""
    if bet.status != UNRESOLVED:
        return ()
    named = {GROUP_OF[reason] for reason in bet.reasons if reason in GROUP_OF}
    return tuple(group for group, _ in GROUPS if group in named) or (OTHER,)


def _only_here(bet, group):
    rest = [reason for reason in bet.reasons if reason != VOID_UNKNOWN]
    if group == OTHER:
        return not any(reason in GROUP_OF for reason in rest)
    return all(GROUP_OF.get(reason) == group for reason in rest) and bool(rest)


@dataclass(frozen=True)
class GroupTally:
    """One report group of one variant: unresolved bets with a reason in it, and those with no reason elsewhere."""

    group: str
    bets: int
    stake_pence: int
    only_here_bets: int
    only_here_stake_pence: int
    roi_worst_case_without_only_here: object


@dataclass(frozen=True)
class Sensitivity:
    """One variant's resolved ROI beside its worst case and each group's tally (module docstring)."""

    variant: str
    resolved_bets: int
    resolved_stake_pence: int
    net_resolved_pence: int
    unresolved_bets: int
    unresolved_stake_pence: int
    roi_resolved: object
    roi_worst_case_unresolved_lost: object
    worst_case_turns_negative: bool
    groups: tuple


def _worst(net, resolved_stake, unresolved_stake):
    """(N_R - S_U) / (S_R + S_U), None when nothing is staked (as metrics: undefined, never 0)."""
    total = resolved_stake + unresolved_stake
    return None if total == 0 else Fraction(net - unresolved_stake, total)


def sensitivity(settlement, variant):
    """Sensitivity of one variant of an accounting.Settlement (module docstring); metrics.variant_metrics's checks
    apply (an undeclared variant or totals that do not reconcile: ValueError)."""
    metrics = variant_metrics(settlement, variant)
    bets = [bet for bet in settlement.bets if bet.variant == variant and bet.status == UNRESOLVED]
    tallies = []
    for group in [name for name, _ in GROUPS] + [OTHER]:
        members = [bet for bet in bets if group in groups_of(bet)]
        only = [bet for bet in members if _only_here(bet, group)]
        dropped = sum(bet.stake_pence for bet in only)
        tallies.append(GroupTally(
            group=group, bets=len(members), stake_pence=sum(bet.stake_pence for bet in members),
            only_here_bets=len(only), only_here_stake_pence=dropped,
            roi_worst_case_without_only_here=_worst(metrics.net_resolved_pence, metrics.resolved_stake_pence,
                                                    metrics.unresolved_stake_pence - dropped)))
    resolved, worst = metrics.roi_resolved, metrics.roi_all_issued_pessimistic
    return Sensitivity(
        variant=variant, resolved_bets=metrics.resolved_bets, resolved_stake_pence=metrics.resolved_stake_pence,
        net_resolved_pence=metrics.net_resolved_pence, unresolved_bets=metrics.unresolved_bets,
        unresolved_stake_pence=metrics.unresolved_stake_pence, roi_resolved=resolved,
        roi_worst_case_unresolved_lost=worst,
        worst_case_turns_negative=resolved is not None and resolved > 0 and worst is not None and worst < 0,
        groups=tuple(tallies))


def coverage_check(report, tier, market_types=("WIN",)):
    """(problems, summary) of a coverage_audit report for one tier and its market types (module docstring): problems
    is empty only when the report is an audited v1 report carrying SIZE_COUNTS, the tier has markets of those types,
    and each of REQUIRED_FIELDS is present in every one of them. summary holds integer counts only."""
    where = f"settlement_v2:{tier}"
    if (not isinstance(report, dict) or report.get("status") != "audited" or report.get("kind") != AUDIT_KIND
            or report.get("schema_version") != AUDIT_SCHEMA_VERSION):
        return [f"{where}:audit_not_audited"], None
    groups = [group for group in report["groups"] if group["tier"] == tier and group["market_type"] in market_types]
    problems = sorted({f"{where}:audit_lacks:{name}" for group in groups for name in SIZE_COUNTS if name not in group})
    markets = sum(group["markets"] for group in groups)
    if not markets:
        problems.append(f"{where}:no_markets")
    present = {field: sum(group["fields_present"].get(field, 0) for group in groups) for field in REQUIRED_FIELDS}
    problems += [f"{where}:{field}:present_in_{count}_of_{markets}" for field, count in present.items()
                 if markets and count != markets]
    summary = {"tier": tier, "market_types": sorted(market_types), "markets": markets, "fields_present": present,
               **{name: sum(group.get(name, 0) for group in groups) for name in SIZE_COUNTS}}
    return problems, summary
