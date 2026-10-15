"""P0 population, cohort, variants, decision ids and the issuance ledger (betangel_research.betfair.backtest.cohort).

Every archive is synthetic and goes through the real task (b) ingest and index, and every view through the real
replay, as in tests/test_betfair_backtest_replay.py (whose helpers these reuse). The prefix-invariance tests rebuild a
separate archive, manifest and index for each variant of the input (the full streams, each stream cut after its
decision-time batch, streams whose post-decision outcomes were deleted, and streams whose post-decision outcomes and
metadata were altered, a resettlement included) and compare population classes and every issuance row whole. They
show these properties on these synthetic shapes only; they say nothing about real files or results. Existence of a
no_view record or of an anchor_not_known_by_decision diagnostic is not prefix invariant (the index docstring), so
those are only checked never to become decision-time population.
"""

import copy
import dataclasses
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import unittest

from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS
from betangel_research.betfair.backtest.cohort import (ABSTAINED, ANCHOR_NOT_KNOWN, ELIGIBLE, EXCLUDED, ISSUED,
                                                      NO_VIEW, NOT_SELECTED, UNCLASSIFIABLE, UNSUPPORTED,
                                                      CohortProblem, CohortRules, decide_market, decide_records,
                                                      decision_id, issuance_counts, ledger_lines, ledger_rows,
                                                      population_counts)
from betangel_research.betfair.backtest.contract import canonical_json, contract_sha256
from betangel_research.betfair.backtest.metrics import HashStream
from betangel_research.betfair.backtest.replay import ReplayProblem, replay_view

import test_betfair_backtest_replay as fx
from test_betfair_backtest_replay import DROP, OFF, OFFSET_S, T, definition, md, mcm, ms, path, prices, runner

M = "1.100000001"
VARIANTS = ("ltp_favourite", "seeded_random_runner")


def no_replay(record):
    raise AssertionError(f"replayed {record['market_id']} although its decision needs no view")


class CohortCase(fx.Workspace):
    def setUp(self):
        super().setUp()
        self.contract = fx.frozen_contract()
        self.rules = CohortRules.from_contract(self.contract)

    def decide(self, members, rules=None):
        """{market id: MarketDecision} of a fresh archive of members."""
        root, manifest, records = self.build(members)
        rules = rules or self.rules
        decisions = decide_records(rules, records.values(),
                                   lambda record: replay_view(root, manifest, record, rules.offset_s))
        return {decision.market_id: decision for decision in decisions}

    def one(self, messages, market=M, **kwargs):
        return self.decide([(path(market), messages)], **kwargs)[market]

    def outcomes(self, decision, variant):
        return [(row["selection_id"], row["variants"][variant]["outcome"], row["variants"][variant]["reason"],
                 row["variants"][variant]["detail"]) for row in decision.rows]


class RulesTests(CohortCase):
    def test_rules_from_a_frozen_p0_contract(self):
        rules = self.rules
        self.assertEqual((rules.axis, rules.offset_s, rules.delay_s, rules.selection_seed, rules.stake_pence),
                         ("first_published_scheduled_off-300s", -300, 0, 1, 100))
        self.assertEqual((rules.tier, rules.countries, rules.market_types, rules.start_month, rules.end_month),
                         ("BASIC", ("GB", "IE"), ("WIN",), "2015-05", "2016-04"))
        self.assertEqual(rules.variants, VARIANTS)
        self.assertEqual(rules.contract_sha256, contract_sha256(self.contract))

    def test_refusals(self):
        draft = json.loads(fx.DRAFT.read_text(encoding="utf-8"))
        draft["decision_timing"]["delay_s"] = None
        changed = fx.frozen_contract()
        changed["variants"][1]["uses_features"] = ["ltp_at_decision"]
        broken = fx.frozen_contract()
        broken["staking"]["stake_pence"] = -1
        for contract, reason in ((draft, "decision_timing:delay_s_null"),
                                 (changed, "variants:not_the_p0_variants"), (broken, "contract:invalid")):
            with self.subTest(reason=reason), self.assertRaises(ValueError) as caught:
                CohortRules.from_contract(contract)
            self.assertEqual(str(caught.exception), reason)


class PopulationTests(CohortCase):
    def test_classes_and_reasons(self):
        later = OFF.replace(year=2023, month=1)
        three = [runner(1), runner(2), runner(3)]
        members = [
            (path("1.100000001"), fx.standard_market("1.100000001")),
            (path("1.100000002"), [md("1.100000002", OFF + timedelta(minutes=1), definition(OFF, three))]),
            (path("1.100000003"), [md("1.100000003", OFF - timedelta(minutes=4), definition(OFF, three))]),
            (path("1.100000004", plan="ADVANCED"), fx.standard_market("1.100000004")),
            (path("1.100000005", when=later), fx.standard_market("1.100000005", off=later)),
            (path("1.100000006"), [md("1.100000006", OFF - timedelta(hours=1),
                                      definition(OFF, three, eventTypeId="4339"))]),
            (path("1.100000007"), [md("1.100000007", OFF - timedelta(hours=1),
                                      definition(OFF, three, countryCode="US"))]),
            (path("1.100000008"), [md("1.100000008", OFF - timedelta(hours=1),
                                      definition(OFF, three, marketType="PLACE"))]),
            (path("1.100000009"), [md("1.100000009", OFF - timedelta(hours=1),
                                      definition(OFF, three, countryCode=DROP))]),
            (path("1.100000010"), [md("1.100000010", OFF - timedelta(hours=1),
                                      definition(OFF, three, marketType=5))]),
            (path("1.100000011"), [md("1.100000011", OFF - timedelta(hours=1),
                                      definition(OFF, three, countryCode="IE"))]),
            (path("1.100000012"), [md("1.100000012", OFF - timedelta(hours=1), definition(OFF, three)),
                                   md("1.100000012", OFF - timedelta(minutes=2),
                                      definition(OFF, three, countryCode="US"))]),
        ]
        decisions = self.decide(members)
        expected = {"1.100000001": (ELIGIBLE, None), "1.100000002": (NO_VIEW, None),
                    "1.100000003": (ANCHOR_NOT_KNOWN, None), "1.100000004": (EXCLUDED, "plan_not_contract_tier"),
                    "1.100000005": (EXCLUDED, "month_not_evaluated"),
                    "1.100000006": (EXCLUDED, "event_type_not_horse_racing"),
                    "1.100000007": (EXCLUDED, "country_not_in_population"),
                    "1.100000008": (EXCLUDED, "market_type_not_in_population"),
                    "1.100000009": (UNCLASSIFIABLE, "countryCode"), "1.100000010": (UNCLASSIFIABLE, "marketType"),
                    "1.100000011": (ELIGIBLE, None), "1.100000012": (ELIGIBLE, None)}
        self.assertEqual({mid: (d.population, d.reason) for mid, d in decisions.items()}, expected)
        for mid, decision in decisions.items():
            self.assertEqual(bool(decision.rows), decision.population == ELIGIBLE, mid)
        counts = population_counts(decisions.values())
        self.assertEqual(counts["by_class"], {ANCHOR_NOT_KNOWN: 1, ELIGIBLE: 3, EXCLUDED: 5, NO_VIEW: 1,
                                              UNCLASSIFIABLE: 2})
        self.assertEqual(counts["by_reason"][UNCLASSIFIABLE], {"countryCode": 1, "marketType": 1})

    def test_a_known_mismatch_excludes_before_an_unknown_field_is_unclassifiable(self):
        three = [runner(1), runner(2), runner(3)]
        decision = self.one([md(M, OFF - timedelta(hours=1),
                                definition(OFF, three, eventTypeId=DROP, countryCode="US"))])
        self.assertEqual((decision.population, decision.reason), (EXCLUDED, "country_not_in_population"))
        decision = self.one([md(M, OFF - timedelta(hours=1), definition(OFF, three, eventTypeId=DROP, marketType=7))])
        self.assertEqual((decision.population, decision.reason), (UNCLASSIFIABLE, "eventTypeId"))

    def test_diagnostics_keep_their_anchor_and_decision_time(self):
        decision = self.one([md(M, OFF - timedelta(minutes=4), definition(OFF, [runner(1)]))])
        self.assertEqual((decision.population, decision.anchor_pt, decision.anchor_ms, decision.decision_ms),
                         (ANCHOR_NOT_KNOWN, ms(OFF - timedelta(minutes=4)), ms(OFF), ms(T)))

    def test_a_duplicate_market_refuses(self):
        root, manifest, records = self.build([(path(M), fx.standard_market())])
        with self.assertRaises(CohortProblem) as caught:
            decide_records(self.rules, [records[M], records[M]], no_replay)
        self.assertEqual(str(caught.exception), f"cohort:{M}:duplicate_market")


class MarketConditionTests(CohortCase):
    def market(self, early=(), **pre_off):
        """A three-runner market with prices at off-10m, a definition at off-20m changed by pre_off, and early
        extra messages."""
        three = [runner(1), runner(2), runner(3)]
        return [md(M, OFF - timedelta(hours=2), definition(OFF, three)),
                md(M, OFF - timedelta(minutes=20), definition(OFF, three, **pre_off)),
                prices(M, OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0}), *early]

    def assertEveryRow(self, decision, expected, selection_ids=(1, 2, 3)):
        for variant in VARIANTS:
            self.assertEqual([item[0] for item in self.outcomes(decision, variant)], list(selection_ids))
            self.assertEqual({item[1:] for item in self.outcomes(decision, variant)}, {expected})

    def test_status_in_play_and_bsp_market(self):
        for pre_off, expected in (
                ({"status": "SUSPENDED"}, (ABSTAINED, "market_not_open_pre_off", None)),
                ({"status": DROP}, (UNSUPPORTED, "market_definition", "status_unknown")),
                ({"status": 7}, (UNSUPPORTED, "market_definition", "status_unknown")),
                ({"inPlay": True}, (ABSTAINED, "market_not_open_pre_off", None)),
                ({"inPlay": DROP}, (UNSUPPORTED, "market_definition", "in_play_unknown")),
                ({"inPlay": "false"}, (UNSUPPORTED, "market_definition", "in_play_unknown")),
                ({"bspMarket": False}, (ABSTAINED, "bsp_market_false", None)),
                ({"bspMarket": DROP}, (UNSUPPORTED, "market_definition", "bsp_market_unknown")),
                ({"status": "SUSPENDED", "bspMarket": False}, (ABSTAINED, "market_not_open_pre_off", None)),
                ({"inPlay": DROP, "bspMarket": False}, (UNSUPPORTED, "market_definition", "in_play_unknown"))):
            with self.subTest(pre_off=pre_off):
                self.assertEveryRow(self.one(self.market(**pre_off)), expected)

    def test_no_active_runner_is_one_selection_null_row(self):
        removed = [runner(1, "REMOVED"), runner(2, "REMOVED"), runner(3, "REMOVED")]
        decision = self.one(self.market([md(M, OFF - timedelta(minutes=8), definition(OFF, removed))]))
        self.assertEveryRow(decision, (ABSTAINED, "no_active_runner", None), selection_ids=(None,))

    def test_only_active_and_listed_runners_have_rows(self):
        messages = self.market([md(M, OFF - timedelta(minutes=8),
                                   definition(OFF, [runner(1), runner(2, "REMOVED"), runner(3)])),
                                md(M, OFF - timedelta(minutes=7), definition(OFF, [runner(1), runner(2, "REMOVED")]))])
        decision = self.one(messages)
        self.assertEqual([row["selection_id"] for row in decision.rows], [1])

    def test_an_unattributable_price_change_makes_ltp_unsupported_but_the_draw_is_made(self):
        decision = self.one(self.market([mcm(OFF - timedelta(minutes=8), [{"id": M, "rc": [{"id": "x", "ltp": 2}]}])]))
        self.assertEveryRow(decision, (UNSUPPORTED, "ltp", "malformed_price_change"))
        drawn = [row["variants"]["seeded_random_runner"]["drawn"] for row in decision.rows]
        self.assertEqual(sorted(drawn), [False, False, True])
        self.assertEqual(issuance_counts(self.rules, [decision])["seeded_random_runner"]["issued"], 0)

    def test_a_listed_runner_whose_status_is_unknown_leaves_the_runner_set_unknown(self):
        for status in (DROP, 5, "REMOVED VACANT", ""):
            with self.subTest(status=status):
                odd = {"id": 3, "sortPriority": 3, "name": "Horse 3"}
                if status is not DROP:
                    odd["status"] = status
                decision = self.one(self.market([md(M, OFF - timedelta(minutes=8),
                                                    definition(OFF, [runner(1), runner(2), odd]))]))
                self.assertEveryRow(decision, (UNSUPPORTED, "market_definition", "runner_status_unknown"),
                                    selection_ids=(None,))

    def test_an_unknown_runner_set_is_one_row_whichever_condition_gives_the_reason(self):
        """Reason precedence and row cardinality are separate (Astra, c2a review R1): with runner 3 listed without a
        status, every earlier condition keeps its reason but the market has one selection-null row, no draw and no
        intent; with runner 3 ACTIVE (a known set) the same conditions keep a row per runner."""
        conditions = (({"status": "SUSPENDED"}, (ABSTAINED, "market_not_open_pre_off", None)),
                      ({"status": DROP}, (UNSUPPORTED, "market_definition", "status_unknown")),
                      ({"inPlay": "unknown"}, (UNSUPPORTED, "market_definition", "in_play_unknown")),
                      ({"inPlay": True}, (ABSTAINED, "market_not_open_pre_off", None)),
                      ({"bspMarket": False}, (ABSTAINED, "bsp_market_false", None)),
                      ({"bspMarket": DROP}, (UNSUPPORTED, "market_definition", "bsp_market_unknown")),
                      ({}, (UNSUPPORTED, "market_definition", "runner_status_unknown")))
        for pre_off, expected in conditions:
            for known in (False, True):
                with self.subTest(pre_off=pre_off, known=known):
                    third = runner(3) if known else {"id": 3, "sortPriority": 3, "name": "Horse 3"}
                    messages = [md(M, OFF - timedelta(minutes=20), definition(OFF, [runner(1), runner(2), third],
                                                                              **pre_off)),
                                prices(M, OFF - timedelta(minutes=10), {1: 2.5, 2: 4.0, 3: 6.0})]
                    decision = self.one(messages)
                    if known and not pre_off:
                        self.assertIn(ISSUED, {row["variants"]["ltp_favourite"]["outcome"] for row in decision.rows})
                        continue
                    self.assertEveryRow(decision, expected, selection_ids=(1, 2, 3) if known else (None,))
                    for row in decision.rows:
                        self.assertIsNone(row["variants"]["seeded_random_runner"]["drawn"])
                        self.assertTrue(all(row["variants"][v]["intent"] is None for v in VARIANTS))
                    counts = issuance_counts(self.rules, [decision])
                    for variant in VARIANTS:
                        self.assertEqual((counts[variant]["rows"], counts[variant]["issued"]), (3 if known else 1, 0))

    def test_a_hidden_or_unlisted_runner_is_known_not_active(self):
        decision = self.one(self.market([md(M, OFF - timedelta(minutes=8),
                                            definition(OFF, [runner(1), runner(2), runner(3, "HIDDEN")]))]))
        self.assertEqual([row["selection_id"] for row in decision.rows], [1, 2])

    def test_an_active_runner_with_id_zero_is_unsupported(self):
        messages = [md(M, OFF - timedelta(hours=2), definition(OFF, [runner(0), runner(1)])),
                    prices(M, OFF - timedelta(minutes=10), {0: 3.0, 1: 2.5})]
        self.assertEveryRow(self.one(messages), (UNSUPPORTED, "market_definition", "runner_id_not_positive"),
                            selection_ids=(0, 1))

    def test_the_unknown_pt_limit_is_bounded_and_explicit(self):
        """A line whose pt cannot be read right after the batch at t ends the view at the last pt applied: the
        decision at t is unsupported in the full stream although the same stream cut at t issues (module docstring,
        Limits)."""
        pre = self.market()
        full = self.one(pre + [b"{not json\n", prices(M, OFF - timedelta(minutes=2), {1: 2.0})])
        self.assertEveryRow(full, (UNSUPPORTED, "decision_view_unreadable", None), selection_ids=(None,))
        cut = self.one(pre)
        self.assertIn(ISSUED, {row["variants"]["ltp_favourite"]["outcome"] for row in cut.rows})

    def test_a_view_stopped_by_t_is_decided_from_the_index_alone(self):
        three = [runner(1), runner(2), runner(3)]
        closed = [runner(1, "WINNER", 2.0), runner(2, "LOSER", 3.0), runner(3, "LOSER", 4.0)]
        early_close = [md(M, OFF - timedelta(hours=2), definition(OFF, three)),
                       md(M, OFF - timedelta(minutes=10), definition(OFF, closed, status="CLOSED"))]
        unreadable = self.market([b"{not json\n", prices(M, OFF - timedelta(minutes=2), {1: 2.0})])
        for messages, expected in (
                (early_close, (ABSTAINED, "market_not_open_pre_off", "first_outcome_bearing_definition")),
                (unreadable, (UNSUPPORTED, "decision_view_unreadable", None))):
            with self.subTest(expected=expected):
                root, manifest, records = self.build([(path(M), messages)])
                decision = decide_market(self.rules, records[M], no_replay)
                self.assertEqual(decision.population, ELIGIBLE)
                self.assertEveryRow(decision, expected, selection_ids=(None,))
                self.assertIsNone(decision.rows[0]["variants"]["seeded_random_runner"]["drawn"])

    def test_a_view_for_another_decision_refuses(self):
        root, manifest, records = self.build([(path(M), fx.standard_market())])
        with self.assertRaises(CohortProblem):
            decide_market(self.rules, records[M], lambda record: replay_view(root, manifest, record, -600))

    def test_a_view_of_the_same_market_from_another_tier_refuses(self):
        """The same market and timing in another tier, with other prices, is another decision's view."""
        basic = self.build([(path(M), fx.standard_market(M, ltps={1: 3.0, 2: 2.5, 3: 6.0}))])
        advanced = self.build([(path(M, plan="ADVANCED"), fx.standard_market(M, ltps={1: 2.0, 2: 2.5, 3: 6.0}))])

        def view_from(material):
            root, manifest, records = material
            return lambda record: replay_view(root, manifest, records[M], self.rules.offset_s)

        record = basic[2][M]
        self.assertEqual(advanced[2][M]["provenance"]["plan"], "ADVANCED")
        with self.assertRaisesRegex(CohortProblem, "view_for_another_decision"):
            decide_market(self.rules, record, view_from(advanced))
        own = decide_market(self.rules, record, view_from(basic))               # the same tier: decided
        self.assertEqual([row["variants"]["ltp_favourite"]["outcome"] for row in own.rows],
                         [NOT_SELECTED, ISSUED, NOT_SELECTED])


class VariantTests(CohortCase):
    def ltp_market(self, ltps, market=M):
        three = [runner(1), runner(2), runner(3)]
        return [md(market, OFF - timedelta(hours=2), definition(OFF, three)),
                prices(market, OFF - timedelta(minutes=10), ltps)]

    def drawn(self, market, ids=(1, 2, 3), seed=1):
        return sorted(ids)[HashStream(seed, f"selection/v1/{market}").below(len(ids))]

    def test_the_unique_favourite_is_issued(self):
        decision = self.one(self.ltp_market({1: 3.0, 2: 2.5, 3: 6.0}))
        self.assertEqual(self.outcomes(decision, "ltp_favourite"),
                         [(1, NOT_SELECTED, None, None), (2, ISSUED, None, None), (3, NOT_SELECTED, None, None)])
        intent = decision.rows[1]["variants"]["ltp_favourite"]["intent"]
        self.assertEqual(intent, {"side": "BACK", "price": "BSP", "stake_pence": 100, "effective_ms": ms(T)})
        self.assertEqual([row["input"] for row in decision.rows],
                         [{"ltp": price, "ltp_pt": ms(OFF - timedelta(minutes=10))} for price in (3.0, 2.5, 6.0)])

    def test_a_tie_or_a_missing_price_issues_no_favourite(self):
        tie = self.one(self.ltp_market({1: 2.5, 2: 2.5, 3: 6.0}))
        self.assertEqual({item[1:3] for item in self.outcomes(tie, "ltp_favourite")}, {(ABSTAINED, "ltp_tie")})
        for ltps in ({1: 3.0, 2: 2.5}, {1: 3.0, 2: 2.5, 3: 0}, {1: 3.0, 2: 2.5, 3: None}, {1: 3.0, 2: 2.5, 3: "1.5"}):
            with self.subTest(ltps=ltps):
                missing = self.one(self.ltp_market(ltps))
                self.assertEqual(self.outcomes(missing, "ltp_favourite"),
                                 [(1, ABSTAINED, "ltp_missing_in_market", None),
                                  (2, ABSTAINED, "ltp_missing_in_market", None), (3, UNSUPPORTED, "ltp", "missing")])

    def test_the_seeded_draw_is_over_every_runner_and_never_redrawn(self):
        missing_three, issued_three = None, None
        for number in range(1, 400):
            market = f"1.2{number:08d}"
            if self.drawn(market) == 3 and missing_three is None:
                missing_three = market
            elif self.drawn(market) != 3 and issued_three is None:
                issued_three = market
            if missing_three and issued_three:
                break
        unsupported = self.one(self.ltp_market({1: 3.0, 2: 2.5}, missing_three), market=missing_three)
        self.assertEqual(self.outcomes(unsupported, "seeded_random_runner"),
                         [(1, NOT_SELECTED, None, None), (2, NOT_SELECTED, None, None),
                          (3, UNSUPPORTED, "ltp", "missing")])
        self.assertEqual([row["variants"]["seeded_random_runner"]["drawn"] for row in unsupported.rows],
                         [False, False, True])
        issued = self.one(self.ltp_market({1: 3.0, 2: 2.5}, issued_three), market=issued_three)
        drawn = self.drawn(issued_three)
        self.assertEqual([(item[0], item[1]) for item in self.outcomes(issued, "seeded_random_runner")],
                         [(rid, ISSUED if rid == drawn else UNSUPPORTED if rid == 3 else NOT_SELECTED)
                          for rid in (1, 2, 3)])

    def test_the_draw_depends_on_the_seed_and_market_id_only(self):
        markets = [f"1.3{number:08d}" for number in range(1, 13)]
        members = [(path(market), self.ltp_market({1: 3.0, 2: 2.5, 3: 6.0}, market)) for market in markets]
        forward = self.decide(members)
        backward = self.decide(list(reversed(members))[:7])
        for market in markets:
            rows = [r for r in forward[market].rows if r["variants"]["seeded_random_runner"]["outcome"] == ISSUED]
            self.assertEqual([r["selection_id"] for r in rows], [self.drawn(market)])
            if market in backward:
                self.assertEqual(backward[market].rows, forward[market].rows)
        self.assertGreater(len({self.drawn(market) for market in markets}), 1)


class IdentityAndLedgerTests(CohortCase):
    def test_decision_ids_are_stable_shared_and_contract_bound(self):
        decision = self.one(fx.standard_market())
        for row in decision.rows:
            expected = hashlib.sha256(canonical_json(["betangel-research/backtest/decision-id/v1",
                                                      self.rules.contract_sha256, M, row["selection_id"],
                                                      "first_published_scheduled_off-300s"])).hexdigest()
            self.assertEqual(row["decision_id"], expected)
            self.assertEqual(row["decision_id"], decision_id(self.rules, M, row["selection_id"]))
        reseeded = fx.frozen_contract(seeds={"selection": 7, "bootstrap": 2})
        other = CohortRules.from_contract(reseeded)
        self.assertNotEqual(decision_id(other, M, 1), decision_id(self.rules, M, 1))
        moved = self.decide([(path("1.100000009"), fx.standard_market("1.100000009")),
                             (path(M), fx.standard_market())])[M]
        self.assertEqual(moved.rows, decision.rows)

    def test_the_ledger_is_canonical_ordered_and_reconciled(self):
        later = OFF + timedelta(hours=1)
        decisions = self.decide([(path("1.100000002", when=OFF), fx.standard_market("1.100000002", off=later)),
                                 (path(M), fx.standard_market()),
                                 (path("1.100000003"), [md("1.100000003", OFF - timedelta(hours=1),
                                                           definition(OFF, [runner(1, "REMOVED")]))])])
        rows = ledger_rows(decisions.values())
        self.assertEqual([(row["market_id"], row["selection_id"]) for row in rows],
                         [(M, 1), (M, 2), (M, 3), ("1.100000003", None),        # same anchor: market id order
                          ("1.100000002", 1), ("1.100000002", 2), ("1.100000002", 3)])
        lines_ = ledger_lines(decisions.values())
        self.assertEqual(lines_, [canonical_json(row) + b"\n" for row in rows])
        counts = issuance_counts(self.rules, decisions.values())
        for variant in VARIANTS:
            self.assertEqual(counts[variant]["rows"], 7)
            self.assertEqual(sum(counts[variant]["by_outcome"].values()), 7)
            self.assertEqual(counts[variant]["issued"], 2)
            self.assertEqual(counts[variant]["issued_stake_pence"], 200)
            self.assertEqual(counts[variant]["by_reason"]["abstained:no_active_runner"], 1)
        broken = decisions[M].rows[0]
        del broken["variants"]["ltp_favourite"]
        with self.assertRaises(ValueError):
            issuance_counts(self.rules, decisions.values())

    def test_decisions_from_separate_calls_cannot_be_counted_twice(self):
        first = self.decide([(path(M), fx.standard_market())])[M]
        second = self.decide([(path(M), fx.standard_market())])[M]
        for function in (ledger_rows, ledger_lines, population_counts):
            with self.subTest(function=function.__name__), self.assertRaises(CohortProblem):
                function([first, second])
        with self.assertRaises(CohortProblem):
            issuance_counts(self.rules, [first, second])
        late = [md(M, OFF + timedelta(minutes=1), definition(OFF, [runner(1)]))]
        no_view_twice = [self.one(late), self.one(late)]               # no rows: only the market key can tell
        with self.assertRaises(CohortProblem) as caught:
            population_counts(no_view_twice)
        self.assertTrue(str(caught.exception).endswith("duplicate_market"))
        other_plan = dataclasses.replace(second, plan="ADVANCED")      # same ids under another market key
        with self.assertRaises(CohortProblem) as caught:
            ledger_rows([first, other_plan])
        self.assertTrue(str(caught.exception).endswith("duplicate_decision_id"))

    def test_issuance_counts_refuses_a_ledger_that_breaks_its_rules(self):
        intent = {"side": "BACK", "price": "BSP", "stake_pence": 100, "effective_ms": ms(T)}

        def broken(edit):
            decision = self.one(fx.standard_market())
            edit(decision.rows)
            return decision

        def issue_all(rows):
            for row in rows:
                row["variants"]["ltp_favourite"].update(outcome=ISSUED, reason=None, intent=dict(intent))

        def issue_without_intent(rows):
            rows[0]["variants"]["ltp_favourite"].update(outcome=ISSUED, reason=None, intent=None)

        def other_stake(rows):
            for row in rows:
                if row["variants"]["ltp_favourite"]["outcome"] == ISSUED:
                    row["variants"]["ltp_favourite"]["intent"]["stake_pence"] = 200

        def stray_intent(rows):
            rows[0]["variants"]["ltp_favourite"]["intent"] = dict(intent)

        def abstain_without_reason(rows):
            rows[0]["variants"]["ltp_favourite"].update(outcome=ABSTAINED, reason=None)

        def not_selected_with_reason(rows):
            rows[0]["variants"]["seeded_random_runner"]["reason"] = "x"

        def seeded(rows, issued):
            return [row["variants"]["seeded_random_runner"] for row in rows
                    if (row["variants"]["seeded_random_runner"]["outcome"] == ISSUED) == issued]

        def draw_twice(rows):                  # a second drawn row, unsupported: each row is fine, the market is not
            seeded(rows, False)[0].update(outcome=UNSUPPORTED, reason="ltp", detail="missing", drawn=True)

        def draw_none(rows):                   # nothing drawn and nothing issued in a market that had a draw
            seeded(rows, True)[0].update(outcome=NOT_SELECTED, drawn=False, intent=None)

        def issued_not_drawn(rows):
            seeded(rows, True)[0]["drawn"] = False
            seeded(rows, False)[0]["drawn"] = True

        def drawn_not_selected(rows):
            seeded(rows, False)[0]["drawn"] = True

        def drawn_text(rows):
            seeded(rows, False)[0]["drawn"] = "false"

        def draw_missing(rows):
            del rows[0]["variants"]["seeded_random_runner"]["drawn"]

        for edit, problem in ((issue_all, "more_than_one_issued"), (issue_without_intent,
                                                                     "issued_without_the_contract_intent"),
                              (other_stake, "issued_without_the_contract_intent"),
                              (stray_intent, "intent_without_issue"),
                              (abstain_without_reason, "reason_does_not_match_outcome"),
                              (not_selected_with_reason, "reason_does_not_match_outcome"),
                              (draw_twice, "draw_not_recorded_once"), (draw_none, "draw_not_recorded_once"),
                              (issued_not_drawn, "issued_but_not_drawn"),
                              (drawn_not_selected, "drawn_but_neither_issued_nor_unsupported"),
                              (drawn_text, "drawn_not_a_literal"), (draw_missing, "drawn_missing")):
            with self.subTest(edit=edit.__name__):
                decision = broken(edit)
                with self.assertRaises(ValueError) as caught:
                    issuance_counts(self.rules, [decision])
                self.assertTrue(str(caught.exception).endswith(problem), str(caught.exception))


class ReplayBoundaryTests(CohortCase):
    """Edges found by mutation testing: limits, large members, a market absent from its member, a runner first
    listed after t and an anchor published exactly at t."""

    def test_limits_that_refuse_the_first_line_refuse_the_replay(self):
        root, manifest, records = self.build([(path(M), fx.standard_market(M))])
        with self.assertRaises(ReplayProblem) as caught:
            replay_view(root, manifest, records[M], OFFSET_S, limits=DEFAULT_LIMITS.stricter(max_line_bytes=100))
        self.assertEqual(str(caught.exception), f"replay:{M}:unreadable_before_view_ends")

    def test_a_replay_that_fails_where_the_index_did_not_refuses(self):
        messages = [md(M, OFF - timedelta(hours=2), definition(OFF, three())),
                    prices(M, ms(T), {1: 3.0, 2: 2.5, 3: 6.0}),
                    {"op": "heartbeat", "pad": "a" * 5000},
                    prices(M, OFF - timedelta(minutes=2), {1: 2.0})]
        root, manifest, records = self.build([(path(M), messages)])
        with self.assertRaises(ReplayProblem) as caught:
            replay_view(root, manifest, records[M], OFFSET_S, limits=DEFAULT_LIMITS.stricter(max_line_bytes=4000))
        self.assertEqual(str(caught.exception), f"replay:{M}:unreadable_before_view_ends")

    def test_a_member_larger_than_one_read_is_hashed_whole(self):
        rng = random.Random(5)
        pad = [{"op": "heartbeat", "pad": "%0500x" % rng.getrandbits(2000)} for _ in range(4000)]
        market = fx.standard_market(M)
        messages = market[:4] + pad + market[4:]
        root, manifest, records = self.build([(path(M), messages)])
        member = [m for m in manifest["members"] if m["path"] == records[M]["provenance"]["member_path"]][0]
        self.assertGreater(member["size_bytes"], 6 * 131072)
        view = replay_view(root, manifest, records[M], OFFSET_S)
        self.assertEqual([r.ltp for r in view.runners], [3.0, 2.5, 6.0])

    def test_a_market_its_member_never_mentions_refuses(self):
        root, manifest, records = self.build([(path(M), fx.standard_market(M))])
        record = copy.deepcopy(records[M])
        record["market_id"] = "1.100000099"
        with self.assertRaises(ReplayProblem) as caught:
            replay_view(root, manifest, record, OFFSET_S)
        self.assertEqual(str(caught.exception), "replay:1.100000099:market_not_replayed")

    def test_a_runner_first_listed_after_t_is_not_in_the_view(self):
        messages = [md(M, OFF - timedelta(hours=2), definition(OFF, three())),
                    prices(M, OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0}),
                    md(M, OFF - timedelta(minutes=2), definition(OFF, three() + [runner(4)]))]
        root, manifest, records = self.build([(path(M), messages)])
        view = replay_view(root, manifest, records[M], OFFSET_S)
        self.assertEqual([r.selection_id for r in view.runners], [1, 2, 3])

    def test_an_anchor_published_exactly_at_t_is_known_by_t(self):
        messages = [md(M, ms(T), definition(OFF, three())), prices(M, ms(T), {1: 3.0, 2: 2.5, 3: 6.0})]
        root, manifest, records = self.build([(path(M), messages)])
        view = replay_view(root, manifest, records[M], OFFSET_S)
        self.assertEqual(view.anchor_pt, view.decision_ms)
        decision = self.one(messages)
        self.assertEqual(decision.population, ELIGIBLE)
        self.assertEqual([r["variants"]["ltp_favourite"]["outcome"] for r in decision.rows],
                         ["not_selected", ISSUED, "not_selected"])


class CohortBoundaryTests(CohortCase):
    """Edges found by mutation testing: class and condition precedence, inclusive month bounds, revisions at t,
    the contract's seed, delay and stake, ledger and decide_records order, and refusals."""

    def test_an_unknown_anchor_is_judged_before_the_plan(self):
        messages = [md(M, OFF - timedelta(minutes=4), definition(OFF, three()))]
        decision = self.decide([(path(M, plan="ADVANCED"), messages)])[M]
        self.assertEqual((decision.population, decision.reason), (ANCHOR_NOT_KNOWN, None))

    def test_the_evaluated_months_are_inclusive(self):
        cases = {"1.100000001": (datetime(2015, 5, 1, 14, tzinfo=timezone.utc), ELIGIBLE),
                 "1.100000002": (datetime(2016, 4, 30, 14, tzinfo=timezone.utc), ELIGIBLE),
                 "1.100000003": (datetime(2015, 4, 30, 14, tzinfo=timezone.utc), EXCLUDED),
                 "1.100000004": (datetime(2016, 5, 1, 14, tzinfo=timezone.utc), EXCLUDED)}
        members = [(path(mid, when=off), fx.standard_market(mid, off=off)) for mid, (off, _) in cases.items()]
        decisions = self.decide(members)
        self.assertEqual({mid: d.population for mid, d in decisions.items()},
                         {mid: expected for mid, (_, expected) in cases.items()})

    def test_the_first_known_mismatch_in_field_order_excludes(self):
        members = [(path("1.100000001"), [md("1.100000001", OFF - timedelta(hours=1),
                                             definition(OFF, three(), countryCode="US", marketType="PLACE"))]),
                   (path("1.100000002"), [md("1.100000002", OFF - timedelta(hours=1),
                                             definition(OFF, three(), countryCode="US", marketType=DROP))])]
        decisions = self.decide(members)
        self.assertEqual({mid: (d.population, d.reason) for mid, d in decisions.items()},
                         {"1.100000001": (EXCLUDED, "country_not_in_population"),
                          "1.100000002": (EXCLUDED, "country_not_in_population")})

    def test_an_eligibility_revision_at_t_counts(self):
        messages = [md(M, OFF - timedelta(hours=1), definition(OFF, three())),
                    md(M, ms(T), definition(OFF, three(), countryCode="US"))]
        decision = self.one(messages)
        self.assertEqual((decision.population, decision.reason), (EXCLUDED, "country_not_in_population"))

    def test_a_view_ending_exactly_at_t_is_stopped(self):
        closed = [runner(1, "WINNER", 2.0), runner(2, "LOSER", 3.0), runner(3, "LOSER", 4.0)]
        messages = [md(M, OFF - timedelta(hours=2), definition(OFF, three())),
                    md(M, ms(T), definition(OFF, closed, status="CLOSED"))]
        root, manifest, records = self.build([(path(M), messages)])
        self.assertEqual(records[M]["decision_time"]["view_ends"]["pt"], ms(T))
        decision = decide_market(self.rules, records[M], no_replay)
        self.assertEqual([(r["selection_id"], r["variants"]["ltp_favourite"]["reason"]) for r in decision.rows],
                         [(None, "market_not_open_pre_off")])

    def market(self, early=(), **pre_off):
        return [md(M, OFF - timedelta(hours=2), definition(OFF, three())),
                md(M, OFF - timedelta(minutes=20), definition(OFF, three(), **pre_off)),
                prices(M, OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0}), *early]

    def reasons(self, decision):
        return {(r["selection_id"], r["variants"][v]["outcome"], r["variants"][v]["reason"],
                 r["variants"][v]["detail"]) for r in decision.rows for v in ("ltp_favourite", "seeded_random_runner")}

    def test_a_bsp_market_flag_that_is_not_a_bool_is_unknown(self):
        decision = self.one(self.market(bspMarket="true"))
        self.assertEqual({item[1:] for item in self.reasons(decision)},
                         {(UNSUPPORTED, "market_definition", "bsp_market_unknown")})

    def test_status_is_judged_before_in_play(self):
        decision = self.one(self.market(status="SUSPENDED", inPlay=DROP))
        self.assertEqual({item[1:] for item in self.reasons(decision)}, {(ABSTAINED, "market_not_open_pre_off", None)})

    def test_no_active_runner_is_judged_before_prices(self):
        removed = [runner(1, "REMOVED"), runner(2, "REMOVED"), runner(3, "REMOVED")]
        decision = self.one(self.market([md(M, OFF - timedelta(minutes=8), definition(OFF, removed)),
                                         mcm(OFF - timedelta(minutes=7), [{"id": M, "rc": [{"id": "x", "ltp": 2}]}])]))
        self.assertEqual(self.reasons(decision), {(None, ABSTAINED, "no_active_runner", None)})

    def test_status_is_judged_before_the_runner_set(self):
        removed = [runner(1, "REMOVED"), runner(2, "REMOVED"), runner(3, "REMOVED")]
        decision = self.one(self.market([md(M, OFF - timedelta(minutes=8), definition(OFF, removed,
                                                                                     status="SUSPENDED"))]))
        self.assertEqual(self.reasons(decision), {(None, ABSTAINED, "market_not_open_pre_off", None)})

    def test_rows_under_a_market_condition_keep_their_input(self):
        decision = self.one(self.market(status="SUSPENDED"))
        self.assertEqual([r["input"] for r in decision.rows],
                         [{"ltp": p, "ltp_pt": ms(OFF - timedelta(minutes=10))} for p in (3.0, 2.5, 6.0)])

    def test_the_contracts_selection_seed_is_used(self):
        rules = CohortRules.from_contract(fx.frozen_contract(seeds={"selection": 7, "bootstrap": 2}))

        def draw(seed, market):
            return HashStream(seed, f"selection/v1/{market}").below(3)

        market = next(f"1.5{n:08d}" for n in range(1, 400) if draw(7, f"1.5{n:08d}") != draw(1, f"1.5{n:08d}"))
        messages = [md(market, OFF - timedelta(hours=2), definition(OFF, three())),
                    prices(market, OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0})]
        decision = self.one(messages, market=market, rules=rules)
        issued = [r["selection_id"] for r in decision.rows
                  if r["variants"]["seeded_random_runner"]["outcome"] == ISSUED]
        self.assertEqual(issued, [[1, 2, 3][HashStream(7, f"selection/v1/{market}").below(3)]])

    def test_intent_and_stake_follow_the_contract(self):
        contract = fx.frozen_contract(delay_s=2)
        contract["staking"]["stake_pence"] = 250
        rules = CohortRules.from_contract(contract)
        decision = self.one(fx.standard_market(), rules=rules)
        want = {"side": "BACK", "price": "BSP", "stake_pence": 250, "effective_ms": ms(T) + 2000}
        for variant in ("ltp_favourite", "seeded_random_runner"):
            intents = [r["variants"][variant]["intent"] for r in decision.rows
                       if r["variants"][variant]["outcome"] == ISSUED]
            self.assertEqual(intents, [want])
        counts = issuance_counts(rules, [decision])
        self.assertEqual(counts["ltp_favourite"]["issued_stake_pence"], 250)

    def test_the_ledger_orders_market_ids_numerically(self):
        decisions = self.decide([(path("1.99999999"), fx.standard_market("1.99999999")),
                                 (path("1.100000001"), fx.standard_market("1.100000001"))])
        self.assertEqual([r["market_id"] for r in ledger_rows(decisions.values())][::3], ["1.99999999", "1.100000001"])

    def test_decide_records_puts_records_without_an_anchor_last(self):
        root, manifest, records = self.build([
            (path("1.100000002"), [md("1.100000002", OFF + timedelta(minutes=1), definition(OFF, three()))]),
            (path(M), fx.standard_market(M))])
        decisions = decide_records(self.rules, [records["1.100000002"], records[M]],
                                   lambda record: replay_view(root, manifest, record, self.rules.offset_s))
        self.assertEqual([d.market_id for d in decisions], [M, "1.100000002"])

    def test_an_unknown_outcome_refuses(self):
        decision = self.one(fx.standard_market())
        decision.rows[0]["variants"]["ltp_favourite"]["outcome"] = "bogus"
        with self.assertRaises(ValueError):
            issuance_counts(self.rules, [decision])

    def test_one_market_in_two_plans_is_not_a_duplicate(self):
        root, manifest, records = self.build([(path(M), fx.standard_market())])
        other = copy.deepcopy(records[M])
        other["provenance"]["plan"] = "ADVANCED"
        decisions = decide_records(self.rules, [records[M], other],
                                   lambda record: replay_view(root, manifest, record, self.rules.offset_s))
        self.assertEqual(sorted((d.plan, d.population) for d in decisions),
                         [("ADVANCED", EXCLUDED), ("BASIC", ELIGIBLE)])

    def test_a_view_of_another_market_refuses(self):
        other = "1.100000002"
        root, manifest, records = self.build([(path(M), fx.standard_market(M)),
                                              (path(other), fx.standard_market(other))])
        with self.assertRaises(CohortProblem):
            decide_market(self.rules, records[M], lambda record: replay_view(root, manifest, records[other], OFFSET_S))

    def test_a_rows_decision_block(self):
        decision = self.one(fx.standard_market())
        self.assertEqual(decision.rows[0]["decision"],
                         {"axis": "first_published_scheduled_off-300s", "decision_ms": ms(T),
                          "anchor_pt": ms(OFF - timedelta(hours=2)), "anchor_ms": ms(OFF)})


# Prefix invariance: each scenario market gives its messages before or at t (pre) and after t (post), the post part
# in a normal, an outcome-deleted and an altered (resettled, revised) form.

def three():
    return [runner(1), runner(2), runner(3)]


def post_normal(mid, winner=2, bsps=(3.1, 2.4, 7.0)):
    return [prices(mid, OFF - timedelta(minutes=2), {1: 1.9, 2: 8.0, 3: 1.5}),
            *fx.settle(mid, OFF, {1: "LOSER", 2: "LOSER", 3: "LOSER", winner: "WINNER"}, dict(zip((1, 2, 3), bsps)))]


def post_deleted(mid):
    """Post-t messages that bear no outcome: a price and a pre-off definition."""
    return [prices(mid, OFF - timedelta(minutes=2), {1: 1.9, 2: 8.0, 3: 1.5}),
            md(mid, OFF - timedelta(minutes=1), definition(OFF, three()))]


def post_altered(mid):
    """Every decision-time field flipped after t (in play, no BSP, another country, event type and market type), a
    runner removed after t, a marketTime revision, another winner and bsps, then a resettlement."""
    revised = OFF + timedelta(minutes=15)
    return [md(mid, OFF - timedelta(minutes=4), definition(OFF, three(), in_play=True, bspMarket=False,
                                                           countryCode="US", eventTypeId="4339", marketType="PLACE",
                                                           status="SUSPENDED")),
            md(mid, OFF - timedelta(minutes=3), definition(revised, [runner(1), runner(2, "REMOVED"), runner(3)])),
            prices(mid, OFF - timedelta(minutes=2), {1: 50.0, 3: 1.01}),
            md(mid, OFF + timedelta(minutes=1), definition(revised, [runner(1), runner(2, "REMOVED"), runner(3)],
                                                           in_play=True, bspReconciled=True)),
            *fx.settle(mid, OFF, {1: "WINNER", 2: "REMOVED", 3: "LOSER"}, {1: 9.0, 2: None, 3: 1.2}),
            *fx.settle(mid, OFF + timedelta(minutes=30), {1: "LOSER", 2: "REMOVED", 3: "WINNER"},
                       {1: 9.0, 2: None, 3: 1.3})]


def segmented(mid):
    """A message the index cannot read, with a known pt: the first message after t (off-4m)."""
    return {"op": "mcm", "clk": "1", "pt": ms(OFF - timedelta(minutes=4)), "segmentType": "SEG_START",
            "mc": [{"id": mid}]}


def pre_standard(mid, ltps=None):
    return [md(mid, OFF - timedelta(hours=2), definition(OFF, three())),
            prices(mid, OFF - timedelta(minutes=10), ltps or {1: 3.0, 2: 2.5, 3: 6.0})]


SCENARIOS = {
    # standard
    "1.400000001": lambda: (pre_standard("1.400000001"), "standard"),
    # an equal-pt batch at t, the second message changing the favourite
    "1.400000002": lambda: (pre_standard("1.400000002") + [prices("1.400000002", ms(T), {1: 2.0}),
                                                            prices("1.400000002", ms(T), {3: 1.5})], "standard"),
    # an image before t that drops runner 3 and resets prices
    "1.400000003": lambda: (pre_standard("1.400000003") + [
        md("1.400000003", OFF - timedelta(minutes=8), definition(OFF, [runner(1), runner(2)]), img=True,
           rc=[{"id": 1, "ltp": 4.0}, {"id": 2, "ltp": 2.0}])], "standard"),
    # a removal before t
    "1.400000004": lambda: (pre_standard("1.400000004") + [
        md("1.400000004", OFF - timedelta(minutes=7), definition(OFF, [runner(1), runner(2, "REMOVED"), runner(3)]))],
        "standard"),
    # a tie
    "1.400000005": lambda: (pre_standard("1.400000005", {1: 2.5, 2: 2.5, 3: 6.0}), "standard"),
    # a missing price
    "1.400000006": lambda: (pre_standard("1.400000006", {1: 2.5, 2: 3.5}), "standard"),
    # unreadable inside the prefix (a line whose pt is unknown, after the last pt applied before t)
    "1.400000007": lambda: (pre_standard("1.400000007") + [b"{not json\n"], "standard"),
    # unreadable only after t: a segmented message (known pt) first after t, or a bad line later
    "1.400000008": lambda: (pre_standard("1.400000008"), "corrupt_after_t"),
    # excluded at t: another country (the altered form makes it GB later)
    "1.400000013": lambda: ([md("1.400000013", OFF - timedelta(hours=2), definition(OFF, three(), countryCode="US")),
                             prices("1.400000013", OFF - timedelta(minutes=10), {1: 3.0})], "standard"),
    # unclassifiable at t: no countryCode before t (every later definition carries one)
    "1.400000014": lambda: ([md("1.400000014", OFF - timedelta(hours=2), definition(OFF, three(), countryCode=DROP)),
                             prices("1.400000014", OFF - timedelta(minutes=10), {1: 3.0})], "standard"),
    # an early close before t
    "1.400000009": lambda: ([md("1.400000009", OFF - timedelta(hours=2), definition(OFF, three())),
                             md("1.400000009", OFF - timedelta(minutes=10),
                                definition(OFF, [runner(1, "WINNER", 2.0), runner(2, "LOSER", 3.0),
                                                 runner(3, "LOSER", 4.0)], status="CLOSED"))], "none"),
    # suspended at t
    "1.400000010": lambda: (pre_standard("1.400000010") + [
        md("1.400000010", OFF - timedelta(minutes=6), definition(OFF, three(), status="SUSPENDED"))], "standard"),
}
COMPETING = "1.400000011"       # its market file decides; an event file holding it too never does
LATE = "1.400000012"            # first published at off-4m: no off-5m decision


def scenario_members(form):
    """[(path, messages)] of every scenario under one form: full, cut, deleted or altered."""
    members = []
    for mid, make in SCENARIOS.items():
        pre, kind = make()
        post = {"standard": {"full": post_normal(mid), "cut": [], "deleted": post_deleted(mid),
                             "altered": post_altered(mid)},
                "corrupt_after_t": {"full": [segmented(mid)] + post_normal(mid),
                                    "cut": [], "deleted": post_deleted(mid) + [b"{not json\n"],
                                    "altered": [segmented(mid)] + post_altered(mid)},
                "none": {"full": [], "cut": [], "deleted": [], "altered": []}}[kind][form]
        members.append((path(mid), pre + post))
    competing_pre = pre_standard(COMPETING)
    competing_post = {"full": post_normal(COMPETING), "cut": [], "deleted": post_deleted(COMPETING),
                      "altered": post_altered(COMPETING)}[form]
    members.append((path(COMPETING), competing_pre + competing_post))
    event_ltps = {1: 1.5, 2: 9.0, 3: 9.0} if form == "altered" else {1: 9.0, 2: 9.0, 3: 1.5}
    event = pre_standard(COMPETING, event_ltps) + ([] if form == "cut" else post_normal(COMPETING, winner=3))
    members.append((f"BASIC/2015/Jun/1/{fx.EVENT}/{fx.EVENT}.bz2", event))
    late = [] if form == "cut" else [md(LATE, OFF - timedelta(minutes=4), definition(OFF, three()))] + post_normal(LATE)
    members.append((path(LATE), late))
    return members


class PrefixInvarianceTests(CohortCase):
    def test_decisions_do_not_depend_on_what_is_published_after_t(self):
        forms = {form: self.decide(scenario_members(form)) for form in ("full", "cut", "deleted", "altered")}
        full = forms["full"]
        decided = {mid for mid, d in full.items() if d.population not in (NO_VIEW, ANCHOR_NOT_KNOWN)}
        self.assertEqual(decided, set(SCENARIOS) | {COMPETING})
        self.assertEqual({mid: full[mid].population for mid in ("1.400000013", "1.400000014")},
                         {"1.400000013": EXCLUDED, "1.400000014": UNCLASSIFIABLE})
        for form, decisions in forms.items():
            with self.subTest(form=form):
                same = {mid for mid, d in decisions.items() if d.population not in (NO_VIEW, ANCHOR_NOT_KNOWN)}
                self.assertEqual(same, decided)
                for mid in decided:
                    self.assertEqual((decisions[mid].population, decisions[mid].reason),
                                     (full[mid].population, full[mid].reason), mid)
                    self.assertEqual([canonical_json(row) for row in decisions[mid].rows],
                                     [canonical_json(row) for row in full[mid].rows], mid)
                if LATE in decisions:
                    self.assertIn(decisions[LATE].population, (NO_VIEW, ANCHOR_NOT_KNOWN))
                self.assertEqual(ledger_lines(d for mid, d in decisions.items() if mid in decided),
                                 ledger_lines(d for mid, d in full.items() if mid in decided))
        self.assertEqual(full[LATE].population, ANCHOR_NOT_KNOWN)

    def test_the_comparison_is_not_vacuous(self):
        full = self.decide(scenario_members("full"))
        reasons = {(variant, row["variants"][variant]["outcome"], row["variants"][variant]["reason"])
                   for d in full.values() for row in d.rows for variant in VARIANTS}
        for expected in (("ltp_favourite", ISSUED, None), ("seeded_random_runner", ISSUED, None),
                         ("ltp_favourite", ABSTAINED, "ltp_tie"), ("ltp_favourite", UNSUPPORTED, "ltp"),
                         ("ltp_favourite", UNSUPPORTED, "decision_view_unreadable"),
                         ("ltp_favourite", ABSTAINED, "market_not_open_pre_off"),
                         ("ltp_favourite", NOT_SELECTED, None)):
            self.assertIn(expected, reasons)
        batch = [row for row in full["1.400000002"].rows if row["variants"]["ltp_favourite"]["outcome"] == ISSUED]
        self.assertEqual([row["selection_id"] for row in batch], [3])      # the whole batch at t was seen
        competing = [row for row in full[COMPETING].rows if row["variants"]["ltp_favourite"]["outcome"] == ISSUED]
        self.assertEqual([row["selection_id"] for row in competing], [2])  # the market file, not the event file
        imaged = full["1.400000003"]
        self.assertEqual([row["selection_id"] for row in imaged.rows], [1, 2])
        # A change at or before t does change the decision: the comparison above can fail.
        members = scenario_members("full")
        members[0] = (members[0][0], pre_standard("1.400000001", {1: 1.2, 2: 2.5, 3: 6.0})
                      + post_normal("1.400000001"))
        changed = self.decide(members)["1.400000001"]
        self.assertNotEqual(changed.rows, full["1.400000001"].rows)


if __name__ == "__main__":
    unittest.main()
