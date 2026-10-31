"""Settlement rule v2, research only (betangel_research.betfair.backtest.settlement_v2; docs/settlement-v2.md).

Markets are synthetic market definitions sent through the real ingest, index, replay, cohort and outcome (OutcomeCase
from tests/test_betfair_backtest_outcome.py); the report and sensitivity cases build accounting records directly.
The tests pin every condition of the rule, dead heats, HIDDEN, revisions after CLOSED and missing fields. They also
pin that c2's market_outcome, the contract schema and AccountingRules are unchanged and that no module imports the
rule, so P0 never resolves a bet through it; the report groups, the sensitivity check and the coverage gate. They say
nothing about real files or results.
"""

import ast
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import unittest

from betangel_research.betfair.backtest import settlement_v2 as v2
from betangel_research.betfair.backtest.accounting import (UNRESOLVED_REASONS, AccountingRules, IssuedBet,
                                                           MarketOutcome, RunnerOutcome, settle)
from betangel_research.betfair.backtest.contract import validate_contract
from betangel_research.betfair.backtest.coverage_audit import audit
from betangel_research.betfair.backtest.outcome import issued_bets, market_outcome

import test_betfair_backtest_replay as fx
from test_betfair_backtest_outcome import M, OutcomeCase
from test_betfair_backtest_replay import DROP, OFF, definition, md, path, prices, runner

REPO = Path(__file__).resolve().parent.parent
CLOSES = OFF + timedelta(minutes=8)
FAVOURITE = 2                                               # lowest LTP at t in pre() below


def result(statuses, bsps=None, status="CLOSED", when=CLOSES, **extra):
    """The result message at when: runners {id: status} with bsps {id: bsp}, the market status, and bspReconciled
    true unless given."""
    bsps = bsps or {}
    final = [runner(rid, statuses[rid], bsps.get(rid)) for rid in sorted(statuses)]
    fields = {"bspReconciled": True, "settledTime": fx.iso(CLOSES), **extra}
    return md(M, when, definition(OFF, final, status=status, in_play=True, **fields))


NORMAL = {1: "LOSER", 2: "WINNER", 3: "LOSER"}
BSPS = {1: 3.1, 2: 2.4, 3: 7.0}
LATER = CLOSES + timedelta(minutes=5)
HEARTBEAT = {"op": "mcm", "clk": "h", "pt": fx.ms(LATER), "ct": "HEARTBEAT"}


class RuleCase(OutcomeCase):
    def pre(self, **extra):
        return [md(M, OFF - timedelta(hours=2), definition(OFF, [runner(rid) for rid in (1, 2, 3)], **extra)),
                prices(M, OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0})]

    def run_rule(self, messages):
        """(c2's MarketOutcome, the rule's, the rule's Settlement under the P0 accounting rules, the decision)."""
        record, decision = self.decided(messages)
        c2 = market_outcome(record, decision)
        ruled = v2.market_outcome(record, decision, void_rule=v2.RULE)
        rules = AccountingRules.from_contract(self.contract)
        return c2, ruled, settle(issued_bets(decision), [ruled], rules), decision

    def favourite_bet(self, settlement):
        return [bet for bet in settlement.bets if bet.variant == "ltp_favourite"][0]


class RuleTests(RuleCase):
    def test_a_complete_settlement_is_not_void_only_under_the_rule(self):
        c2, ruled, settlement, decision = self.run_rule(self.pre() + [result(NORMAL, BSPS)])
        self.assertEqual(v2.unmet(c2), ())
        self.assertIsNone(c2.market_void)                       # c2 D11 option (a), Q5: unchanged
        self.assertIs(ruled.market_void, False)
        self.assertEqual(ruled, MarketOutcome(**{**{name: getattr(c2, name) for name in (
            "market_id", "first_published_off", "read_complete", "closed", "result_revised_after_close",
            "bsp_reconciled", "dead_heat", "declared_winners", "removed_after_decision")},
            "market_void": False, "runners": dict(c2.runners)}))
        favourite = self.favourite_bet(settlement)
        self.assertEqual((favourite.selection_id, favourite.status, favourite.reasons, favourite.gross_pence),
                         (FAVOURITE, "won", (), 140))               # 100p x (2.4 - 1)
        market = [m for m in settlement.markets if m.variant == "ltp_favourite"][0]
        self.assertEqual((market.commission_pence, market.net_pence), (3, 137))        # 2% of 140 = 2.8, up
        self.assertTrue(all(bet.status in ("won", "lost") for bet in settlement.bets))
        p0 = settle(issued_bets(decision), [c2], AccountingRules.from_contract(self.contract))
        self.assertEqual({(bet.status, bet.reason) for bet in p0.bets}, {("unresolved", "void_unknown")})
        # On P0's path a bet carries void_unknown alone: it is in OTHER only, and every named group is empty.
        tallies = {tally.group: (tally.bets, tally.only_here_bets, tally.roi_worst_case_without_only_here)
                   for tally in v2.sensitivity(p0, "ltp_favourite").groups}
        self.assertEqual(tallies, {**{group: (0, 0, Fraction(-1)) for group, _ in v2.GROUPS}, v2.OTHER: (1, 1, None)})

    def test_each_condition_and_each_missing_field(self):
        """Each case fails exactly the conditions named; the rule then leaves market_void None, so every bet keeps
        void_unknown, and accounting adds its own reasons."""
        unrevisable = ("closed", "not_revised_after_close")    # never CLOSED: revision after close is unknown
        cases = {
            "in play, never CLOSED": (self.pre() + [result(NORMAL, BSPS, status="OPEN")], unrevisable,
                                      "market_not_closed"),
            "CLOSED, then reopened": (self.pre() + [result(NORMAL, BSPS), result(NORMAL, BSPS, status="OPEN",
                                                                                 when=LATER)],
                                      ("closed",), "market_not_closed"),
            "market status never published at the end": (self.pre() + [result(NORMAL, BSPS, status=DROP)],
                                                         unrevisable, "closed_unknown"),
            "bspReconciled false": (self.pre() + [result(NORMAL, BSPS, bspReconciled=False)], ("bsp_reconciled",),
                                    "bsp_not_reconciled"),
            "bspReconciled null": (self.pre() + [result(NORMAL, BSPS, bspReconciled=None)], ("bsp_reconciled",),
                                   "bsp_reconciled_unknown"),
            "bspReconciled never published": (self.pre(bspReconciled=DROP) + [result(NORMAL, BSPS,
                                                                                     bspReconciled=DROP)],
                                              ("bsp_reconciled",), "bsp_reconciled_unknown"),
            "a runner ACTIVE at the end": (self.pre() + [result({**NORMAL, 3: "ACTIVE"}, BSPS)],
                                           ("runner_statuses_final",), "other_runner_status_unexpected"),
            "a runner PLACED at the end": (self.pre() + [result({**NORMAL, 3: "PLACED"}, BSPS)],
                                           ("runner_statuses_final",), "other_runner_status_unexpected"),
            "a runner status null": (self.pre() + [result({**NORMAL, 3: None}, BSPS)], ("runner_statuses_final",),
                                     "other_runner_status_unexpected"),
            "a runner status the spec does not list": (self.pre() + [result({**NORMAL, 3: "MYSTERY"}, BSPS)],
                                                       ("runner_statuses_final",), "other_runner_status_unexpected"),
            "no WINNER": (self.pre() + [result({1: "LOSER", 2: "LOSER", 3: "LOSER"}, BSPS)],
                          ("winner_count_is_declared",), "no_winner_in_result"),
            "a dead heat": (self.pre() + [result({1: "LOSER", 2: "WINNER", 3: "WINNER"}, BSPS)],
                            ("winner_count_is_declared",), "dead_heat"),
            "numberOfWinners 0": (self.pre() + [result(NORMAL, BSPS, numberOfWinners=0)],
                                  ("winner_count_is_declared",), "not_single_winner_market"),
            "numberOfWinners 2, one WINNER": (self.pre() + [result(NORMAL, BSPS, numberOfWinners=2)],
                                              ("winner_count_is_declared",), "not_single_winner_market"),
            # 0 WINNERs equal 0 declared: only "at least 1" rejects it.
            "numberOfWinners 0, no WINNER": (self.pre() + [result({1: "LOSER", 2: "LOSER", 3: "LOSER"}, BSPS,
                                                                  numberOfWinners=0)],
                                             ("winner_count_is_declared",), "not_single_winner_market"),
            "numberOfWinners never published": (self.pre(numberOfWinners=DROP) + [result(
                NORMAL, BSPS, numberOfWinners=DROP)], ("winner_count_is_declared",), "dead_heat_unknown"),
            # A later heartbeat first: a line whose pt cannot be read ends the record at the last pt applied, so
            # without it the CLOSED batch itself would be cut (market_index docstring).
            "a line that cannot be read after CLOSED": (self.pre() + [result(NORMAL, BSPS), HEARTBEAT,
                                                                      b"{not json\n"],
                                                        ("read_whole",), "outcome_incomplete"),
            "a line that cannot be read just after CLOSED": (self.pre() + [result(NORMAL, BSPS), b"{not json\n"],
                                                             v2.CONDITIONS, "outcome_incomplete"),
            "a bsp revised after CLOSED": (self.pre() + [result(NORMAL, BSPS), result(NORMAL, {**BSPS, 2: 2.5},
                                                                                     when=LATER)],
                                           ("not_revised_after_close",), "result_revised_after_close"),
            "a result revised after CLOSED": (self.pre() + [result(NORMAL, BSPS), result(
                {1: "WINNER", 2: "LOSER", 3: "LOSER"}, BSPS, when=LATER)], ("not_revised_after_close",),
                                              "result_revised_after_close"),
            "the WINNER unlisted after CLOSED": (self.pre() + [result(NORMAL, BSPS), result(
                {1: "LOSER", 3: "LOSER"}, BSPS, when=LATER)], ("winner_count_is_declared", "not_revised_after_close"),
                                                 "own_runner_not_listed"),
        }
        for name, (messages, unmet, reason) in cases.items():
            with self.subTest(case=name):
                c2, ruled, settlement, _ = self.run_rule(messages)
                self.assertEqual(v2.unmet(c2), unmet)
                self.assertFalse(v2.published_settlement_complete(c2))
                self.assertIsNone(ruled.market_void)
                self.assertEqual(ruled, c2)
                for bet in settlement.bets:
                    self.assertEqual(bet.status, "unresolved")
                    self.assertIn("void_unknown", bet.reasons)
                favourite = self.favourite_bet(settlement)
                self.assertIn(reason, favourite.reasons)

    def test_a_runner_without_a_status_key_is_not_final(self):
        no_status = {"id": 3, "sortPriority": 3, "name": "Horse 3", "bsp": 7.0}
        final = [runner(1, "LOSER", 3.1), runner(2, "WINNER", 2.4), no_status]
        c2, ruled, _, _ = self.run_rule(self.pre() + [md(M, CLOSES, definition(OFF, final, status="CLOSED",
                                                                                in_play=True, bspReconciled=True))])
        self.assertEqual((c2.runner(3).status, v2.unmet(c2), ruled.market_void),
                         (None, ("runner_statuses_final",), None))

    def test_hidden_is_a_final_status(self):
        c2, ruled, settlement, _ = self.run_rule(self.pre() + [result({**NORMAL, 3: "HIDDEN"}, {1: 3.1, 2: 2.4})])
        self.assertEqual((v2.unmet(c2), ruled.market_void), ((), False))
        self.assertEqual(self.favourite_bet(settlement).status, "won")

    def test_a_dead_heat_stays_unresolved_and_is_grouped_apart(self):
        _, ruled, settlement, _ = self.run_rule(self.pre() + [result({1: "LOSER", 2: "WINNER", 3: "WINNER"}, BSPS)])
        self.assertIs(ruled.dead_heat, True)
        favourite = self.favourite_bet(settlement)
        self.assertEqual((favourite.reason, favourite.status), ("void_unknown", "unresolved"))
        self.assertEqual(favourite.reasons, ("void_unknown", "dead_heat"))
        self.assertEqual(v2.groups_of(favourite), ("dead_heat",))

    def test_two_declared_winners_meet_the_rule_but_accounting_still_settles_one_winner_markets_only(self):
        _, ruled, settlement, _ = self.run_rule(self.pre() + [result({1: "LOSER", 2: "WINNER", 3: "WINNER"}, BSPS,
                                                                     numberOfWinners=2)])
        self.assertIs(ruled.market_void, False)
        favourite = self.favourite_bet(settlement)
        self.assertEqual((favourite.status, favourite.reasons), ("unresolved", ("not_single_winner_market",)))
        self.assertEqual(v2.groups_of(favourite), ("not_single_winner_or_unjudgeable",))

    def test_revisions_that_are_not_results_do_not_count(self):
        """The index's results_revised_after_close looks at runner status, bsp, listing and numberOfWinners only:
        bspReconciled turning true after CLOSED is no revision, and the rule reads its last value."""
        late = result(NORMAL, BSPS, when=LATER)
        c2, ruled, _, _ = self.run_rule(self.pre() + [result(NORMAL, BSPS, bspReconciled=False), late])
        self.assertEqual((c2.result_revised_after_close, c2.bsp_reconciled, v2.unmet(c2), ruled.market_void),
                         (False, True, (), False))


def outcome(selections, market_id="1.900000001", **changes):
    """A MarketOutcome that meets the rule unless changed: selections {id: (status, bsp text or None)}."""
    runners = {sid: RunnerOutcome(status=status, bsp=None if bsp is None else Decimal(bsp))
               for sid, (status, bsp) in selections.items()}
    fields = dict(market_id=market_id, first_published_off=OFF, read_complete=True, closed=True, market_void=None,
                  result_revised_after_close=False, bsp_reconciled=True, dead_heat=False, declared_winners=1,
                  removed_after_decision=frozenset(), runners=runners)
    fields.update(changes)
    return MarketOutcome(**fields)


class UnitTests(unittest.TestCase):
    def test_unknowns_never_meet_a_condition(self):
        self.assertEqual(v2.unmet(MarketOutcome.unknown("1.900000001", OFF)), v2.CONDITIONS)
        self.assertEqual(v2.unmet(outcome({}, read_complete=None, closed=None, result_revised_after_close=None,
                                          bsp_reconciled=None, declared_winners=None)), v2.CONDITIONS)
        self.assertEqual(v2.unmet(outcome({1: ("WINNER", "2.0")})), ())
        with self.assertRaises(ValueError):
            v2.unmet({"closed": True})

    def test_apply_only_ever_turns_unknown_into_not_void(self):
        complete = outcome({1: ("WINNER", "2.0"), 2: ("LOSER", "3.0")})
        self.assertIs(v2.apply(complete).market_void, False)
        void = outcome({1: ("WINNER", "2.0")}, market_void=True)
        self.assertIs(v2.apply(void), void)                    # positive void evidence is never overridden
        incomplete = outcome({1: ("WINNER", "2.0")}, bsp_reconciled=False)
        self.assertIs(v2.apply(incomplete), incomplete)
        self.assertEqual(v2.apply(complete).race_day, complete.race_day)

    def test_an_own_hidden_runner_of_a_complete_market_stays_unresolved(self):
        ruled = v2.apply(outcome({1: ("WINNER", "2.0"), 2: ("HIDDEN", None)}))
        bet = IssuedBet(decision_id="d1", variant="v", market_id="1.900000001", selection_id=2, stake_pence=100,
                        side="BACK", basis="BSP")
        settled = settle([bet], [ruled], AccountingRules(commission_rate=Decimal("0.02"),
                                                         rounding="per_market_ceil_penny")).bets[0]
        self.assertEqual((ruled.market_void, settled.status, settled.reasons),
                         (False, "unresolved", ("own_status_unexpected", "bsp_missing")))


class ScopeTests(RuleCase):
    def test_the_rule_must_be_named(self):
        record, decision = self.decided(self.pre() + [result(NORMAL, BSPS)])
        for rule in ("unresolved", None, v2.RULE.upper()):
            with self.subTest(rule=rule), self.assertRaisesRegex(ValueError, "settlement_v2:rule_not_declared"):
                v2.market_outcome(record, decision, void_rule=rule)
        with self.assertRaises(TypeError):
            v2.market_outcome(record, decision, v2.RULE)            # keyword only

    def test_no_module_imports_the_rule(self):
        """Opting in is explicit and reviewed: today no module imports settlement_v2, so the runner, the certification
        and pilot.py cannot reach it. A study that opts in adds itself here."""
        importers = set()
        for source in sorted((REPO / "betangel_research").rglob("*.py")):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [alias.name for alias in node.names]
                if any(name.split(".")[-1] == "settlement_v2" for name in names):
                    importers.add(source.relative_to(REPO).as_posix())
        self.assertEqual(importers, set())

    def test_a_contract_cannot_declare_the_rule(self):
        contract = json.loads((REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json").read_text("utf-8"))
        self.assertEqual(set(contract["settlement"].values()), {"unresolved"})
        contract["settlement"]["market_void"] = v2.RULE
        self.assertEqual(validate_contract(contract)[0], ["settlement.market_void:not_allowed"])
        with self.assertRaisesRegex(ValueError, "settlement_rule_not_supported"):
            AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny",
                            settlement=contract["settlement"])


class ReportTests(unittest.TestCase):
    def test_every_reason_but_void_unknown_and_market_void_is_in_exactly_one_named_group(self):
        named = [reason for _, reasons in v2.GROUPS for reason in reasons]
        self.assertEqual(len(named), len(set(named)))
        self.assertEqual(set(named) | {"void_unknown", "market_void"}, set(UNRESOLVED_REASONS))
        self.assertNotIn(v2.OTHER, [group for group, _ in v2.GROUPS])

    def test_groups_and_the_sensitivity_check(self):
        """One variant, £1 stakes, 2% rounded up: a win at 3.0 (+200p, commission 4p) and a loss (-100p) resolve,
        N_R 96p over S_R 200p; five markets stay unresolved, 500p: a dead heat, a missing bsp, a removal after the
        decision, a market not CLOSED with a missing bsp, and a market with (synthetic) positive void evidence."""
        markets = {
            "1.900000001": (outcome({1: ("WINNER", "3.0"), 2: ("LOSER", "2.0")}, "1.900000001"), 1),
            "1.900000002": (outcome({1: ("LOSER", "3.0"), 2: ("WINNER", "2.0")}, "1.900000002"), 1),
            "1.900000003": (outcome({1: ("WINNER", "3.0"), 2: ("WINNER", "2.0")}, "1.900000003", dead_heat=True), 1),
            "1.900000004": (outcome({1: ("LOSER", None), 2: ("WINNER", "2.0")}, "1.900000004"), 1),
            "1.900000005": (outcome({1: ("WINNER", "3.0"), 2: ("REMOVED", None)}, "1.900000005",
                                    removed_after_decision=frozenset({2})), 1),
            "1.900000006": (outcome({1: ("LOSER", None), 2: ("WINNER", "2.0")}, "1.900000006", closed=False,
                                    result_revised_after_close=None, dead_heat=None), 1),
            "1.900000007": (outcome({1: ("WINNER", "3.0"), 2: ("LOSER", "2.0")}, "1.900000007", market_void=True), 1),
        }
        outcomes = [v2.apply(item) for item, _ in markets.values()]
        bets = [IssuedBet(decision_id=f"d{n}", variant="v", market_id=mid, selection_id=sid, stake_pence=100,
                          side="BACK", basis="BSP") for n, (mid, (_, sid)) in enumerate(markets.items())]
        rules = AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny", stake_pence=100,
                                variants=["v"])
        settlement = settle(bets, outcomes, rules)
        by_market = {bet.market_id: bet for bet in settlement.bets}
        self.assertEqual([by_market[mid].status for mid in sorted(by_market)],
                         ["won", "lost"] + ["unresolved"] * 5)
        self.assertEqual(by_market["1.900000003"].reasons, ("void_unknown", "dead_heat"))
        self.assertEqual(by_market["1.900000004"].reasons, ("bsp_missing",))
        self.assertEqual(v2.groups_of(by_market["1.900000006"]),
                         ("not_closed_or_not_reconciled", "not_single_winner_or_unjudgeable", "revised_or_incomplete",
                          "bsp_unusable"))
        self.assertEqual(v2.groups_of(by_market["1.900000007"]), (v2.OTHER,))
        self.assertEqual(v2.groups_of(by_market["1.900000001"]), ())

        report = v2.sensitivity(settlement, "v")
        self.assertEqual((report.resolved_bets, report.resolved_stake_pence, report.net_resolved_pence,
                          report.unresolved_bets, report.unresolved_stake_pence), (2, 200, 96, 5, 500))
        self.assertEqual(report.roi_resolved, Fraction(96, 200))
        self.assertEqual(report.roi_worst_case_unresolved_lost, Fraction(96 - 500, 700))
        self.assertTrue(report.worst_case_turns_negative)
        tallies = {tally.group: tally for tally in report.groups}
        self.assertEqual(list(tallies), [group for group, _ in v2.GROUPS] + [v2.OTHER])
        expected = {"dead_heat": (1, 100, 1), "bsp_unusable": (2, 200, 1), "removed_after_decision": (1, 100, 1),
                    "not_closed_or_not_reconciled": (1, 100, 0), "not_single_winner_or_unjudgeable": (1, 100, 0),
                    "revised_or_incomplete": (1, 100, 0), v2.OTHER: (1, 100, 1)}
        for group, tally in tallies.items():
            bets_, stake, only = expected.get(group, (0, 0, 0))
            with self.subTest(group=group):
                self.assertEqual((tally.bets, tally.stake_pence, tally.only_here_bets, tally.only_here_stake_pence),
                                 (bets_, stake, only, only * 100))
                self.assertEqual(tally.roi_worst_case_without_only_here,
                                 Fraction(96 - (500 - only * 100), 700 - only * 100))

    def test_a_resolved_roi_of_exactly_zero_does_not_turn_negative(self):
        """worst_case_turns_negative needs a resolved ROI above 0: a win at 2.03 (+103p, commission 3p: 2% of 103p
        rounded up) and a loss net 0p over 200p, and one unresolved bet makes the worst case -100p over 300p."""
        markets = [outcome({1: ("WINNER", "2.03"), 2: ("LOSER", "2.0")}, "1.900000001"),
                   outcome({1: ("LOSER", "3.0"), 2: ("WINNER", "2.0")}, "1.900000002"),
                   outcome({1: ("WINNER", "3.0"), 2: ("LOSER", "2.0")}, "1.900000003", bsp_reconciled=False)]
        bets = [IssuedBet(decision_id=f"d{n}", variant="v", market_id=item.market_id, selection_id=1, stake_pence=100,
                          side="BACK", basis="BSP") for n, item in enumerate(markets)]
        rules = AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny", stake_pence=100,
                                variants=["v"])
        report = v2.sensitivity(settle(bets, [v2.apply(item) for item in markets], rules), "v")
        self.assertEqual((report.resolved_bets, report.net_resolved_pence, report.roi_resolved,
                          report.roi_worst_case_unresolved_lost, report.worst_case_turns_negative),
                         (2, 0, Fraction(0), Fraction(-100, 300), False))

    def test_a_variant_with_nothing_resolved(self):
        rules = AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_ceil_penny", variants=["v", "w"])
        bet = IssuedBet(decision_id="d1", variant="v", market_id="1.900000001", selection_id=1, stake_pence=100,
                        side="BACK", basis="BSP")
        settlement = settle([bet], [outcome({1: ("WINNER", "2.0")}, bsp_reconciled=False)], rules)
        report = v2.sensitivity(settlement, "v")
        self.assertEqual((report.roi_resolved, report.roi_worst_case_unresolved_lost,
                          report.worst_case_turns_negative), (None, Fraction(-1), False))
        empty = v2.sensitivity(settlement, "w")
        self.assertEqual((empty.roi_resolved, empty.roi_worst_case_unresolved_lost, empty.worst_case_turns_negative),
                         (None, None, False))
        with self.assertRaisesRegex(ValueError, "undeclared_variant"):
            v2.sensitivity(settlement, "x")


class CoverageGateTests(fx.Workspace):
    def audited(self, members):
        root, _, _ = self.build(members)
        report = audit(root)
        self.assertEqual(report["status"], "audited", report)
        return report

    def test_complete_coverage_passes_and_sizes_the_groups(self):
        report = self.audited([(path("1.100000001"), fx.standard_market("1.100000001")),
                               (path("1.100000002"), fx.standard_market("1.100000002", winner=2))])
        problems, summary = v2.coverage_check(report, "BASIC")
        self.assertEqual(problems, [])
        self.assertEqual((summary["tier"], summary["market_types"], summary["markets"], summary["fields_present"]),
                         ("BASIC", ["WIN"], 2, dict.fromkeys(v2.REQUIRED_FIELDS, 2)))
        self.assertEqual({name: summary[name] for name in v2.SIZE_COUNTS}, dict.fromkeys(v2.SIZE_COUNTS, 0))

    def test_a_field_missing_from_a_market_a_tier_without_markets_and_an_old_audit_are_problems(self):
        settled = fx.standard_market("1.100000001")
        bare = [md("1.100000002", OFF - timedelta(hours=2),
                   definition(OFF, [runner(1), runner(2)], bspReconciled=DROP)),
                md("1.100000002", OFF + timedelta(minutes=8),
                   definition(OFF, [runner(1, "WINNER", 2.0), runner(2, "LOSER", 3.0)], status="CLOSED",
                              in_play=True, bspReconciled=DROP))]
        report = self.audited([(path("1.100000001"), settled), (path("1.100000002"), bare)])
        problems, summary = v2.coverage_check(report, "BASIC")
        self.assertEqual(problems, ["settlement_v2:BASIC:md.bspReconciled:present_in_1_of_2"])
        self.assertEqual(summary["closed_bsp_not_reconciled"], 1)
        self.assertEqual(v2.coverage_check(report, "PRO")[0], ["settlement_v2:PRO:no_markets"])
        self.assertEqual(v2.coverage_check(report, "BASIC", ("PLACE",))[0], ["settlement_v2:BASIC:no_markets"])
        old = json.loads(json.dumps(report))
        for group in old["groups"]:
            del group["closed_no_winner"]
        self.assertIn("settlement_v2:BASIC:audit_lacks:closed_no_winner", v2.coverage_check(old, "BASIC")[0])
        for refused in ({"status": "refused", "problems": ["x"]}, {**report, "schema_version": 2}, None):
            with self.subTest(report=str(refused)[:40]):
                self.assertEqual(v2.coverage_check(refused, "BASIC"), (["settlement_v2:BASIC:audit_not_audited"],
                                                                       None))


if __name__ == "__main__":
    unittest.main()
