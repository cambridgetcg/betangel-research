"""Engineering criteria (betangel_research.betfair.backtest.engineering) and the shared-fold refactors (task c2b3a).

Every archive is synthetic and goes through the real ingest and index (helpers from
tests/test_betfair_backtest_replay.py and tests/test_betfair_backtest_cohort.py). These show: the hand-worked
accounting answers pass and each perturbed settlement rule fails the cases it should; reconciliation catches
missing, duplicate and swapped bets, records and decisions, an edited ledger, an unmeasurable settlement and an
unexercised variant; the prefix check takes candidacy from the record, checks the member as replay does, compares
the whole anchor-by-t population (eligible, excluded, unclassifiable, stopped), keeps the batch at t and failures at
or before it, keeps the raw bytes, marks every unknown-pt reason before the boundary (the boundary line's own length
and running total included) and a full buffer unexercised, and reports a deliberate future leak in the run's
decisions as failed (so it never compares a state with itself); the tally takes every record once, only with the
decision its result checked and evidence of the shape its stage and status give it, leaves itself unchanged when it
refuses, never passes counts that do not reconcile, and digests in a fixed order; engineering_status needs exactly
the three criteria; and the extracted index fold gives exactly the old loop's output. They say nothing about real
files or results.
"""

import bz2
import dataclasses
from dataclasses import replace
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_DOWN, ROUND_HALF_UP, InvalidOperation
import hashlib
import json
import os
import unittest
from unittest import mock

from betangel_research.betfair.backtest import accounting, engineering, market_index, replay
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, ArchiveProblem, HashingReader, safe_target
from betangel_research.betfair.backtest.cohort import decide_records, ledger_lines
from betangel_research.betfair.backtest.engineering import (FAILED, INCOMPLETE, NOT_CANDIDATE, PASSED, UNEXERCISED,
                                                           PrefixTally, check_prefix, engineering_status,
                                                           issuance_exposure_reconciliation,
                                                           known_accounting_answers)
from betangel_research.betfair.backtest.market_index import _checked_changes, _Market, _messages, _Unreadable
from betangel_research.betfair.backtest.outcome import issued_bets, market_outcome
from betangel_research.betfair.backtest.replay import replay_view

import test_betfair_backtest_cohort as cohort_fx
import test_betfair_backtest_replay as fx
from test_betfair_backtest_replay import OFF, T, definition, md, ms, path, prices, runner

UNKNOWN = engineering.UNKNOWN_PT


def old_read_member(path, limits):
    """The index's member loop as it was before the extraction (task b), kept here as the reference."""
    markets, reason, complete = {}, None, None
    with open(path, "rb") as handle:
        reader = HashingReader(handle)
        try:
            with bz2.BZ2File(reader) as stream:
                for pt, message in _messages(stream, limits):
                    try:
                        checked = _checked_changes(message)
                    except _Unreadable as error:
                        raise _Unreadable(str(error), pt) from None
                    for market_id, change in checked:
                        market = markets.get(market_id)
                        if market is None:
                            market = markets[market_id] = _Market(market_id, pt)
                        market.change(pt, change)
                    complete = pt
        except _Unreadable as error:
            reason = str(error)
            if error.pt is not None:
                complete = error.pt
        except (OSError, EOFError, ValueError):
            reason = "not_bz2"
        digest = reader.drain()
    return markets, reason, digest, complete


class EngineeringCase(cohort_fx.CohortCase):
    def run_material(self, members, rules=None):
        """(root, manifest, records, decisions) of a fresh archive, decided as the runner would."""
        root, manifest, records = self.build(members)
        rules = rules or self.rules
        decisions = decide_records(rules, records.values(),
                                   lambda record: replay_view(root, manifest, record, rules.offset_s))
        return root, manifest, records, {d.market_id: d for d in decisions}

    def prefix(self, members, limits=DEFAULT_LIMITS, **kwargs):
        root, manifest, records, decisions = self.run_material(members)
        return {mid: check_prefix(self.rules, records[mid], decisions[mid], root, manifest, limits, **kwargs)
                for mid in records}, decisions

    def tally(self, results, decisions, order=sorted, keys=None, **kwargs):
        keys = [(results[mid]["plan"], mid) for mid in results] if keys is None else keys
        tally = PrefixTally(self.rules.variants, keys, **kwargs)
        for mid in order(results):
            tally.add(results[mid], decisions[mid])
        return tally.result()


class KnownAnswerTests(EngineeringCase):
    def test_the_hand_worked_answers_pass(self):
        self.assertEqual(known_accounting_answers(), {"status": PASSED, "cases": 13, "failed_cases": []})

    def test_each_perturbed_settlement_rule_fails_the_cases_it_touches(self):
        real_bet, real_market = accounting.bet_reasons, accounting.market_reasons
        unresolved = ["void_is_unresolved_with_exposure", "dead_heat_is_unresolved_with_exposure",
                      "own_runner_removed_after_decision_is_unresolved",
                      "other_runner_removed_after_decision_is_unresolved", "result_revised_after_close_is_unresolved",
                      "missing_bsp_is_unresolved"]
        resolved = ["s9_win_and_loss_in_two_markets", "s9_plus_and_minus_ten_in_one_market",
                    "s9_plus_and_minus_ten_in_two_markets", "penny_rounding_against_us",
                    "winnings_floor_and_commission_ceiling", "commission_half_up_rounding", "variants_are_never_netted"]
        real_settle = engineering.settle
        perturbations = {
            "no commission": (mock.patch.object(accounting, "commission_pence", return_value=0),
                              ["s9_win_and_loss_in_two_markets", "s9_plus_and_minus_ten_in_two_markets",
                               "penny_rounding_against_us", "winnings_floor_and_commission_ceiling",
                               "commission_half_up_rounding", "variants_are_never_netted"]),
            "commission rounded down": (mock.patch.dict(accounting.COMMISSION_ROUNDING,
                                                        {"per_market_ceil_penny": ROUND_FLOOR}),
                                        ["penny_rounding_against_us", "winnings_floor_and_commission_ceiling"]),
            "ceiling rounded half up": (mock.patch.dict(accounting.COMMISSION_ROUNDING,
                                                        {"per_market_ceil_penny": ROUND_HALF_UP}),
                                        ["winnings_floor_and_commission_ceiling"]),
            "half up rounded up": (mock.patch.dict(accounting.COMMISSION_ROUNDING,
                                                   {"per_market_half_up_penny": ROUND_CEILING}),
                                   ["commission_half_up_rounding"]),
            "winnings rounded up": (mock.patch.object(accounting, "WIN_ROUNDING", ROUND_CEILING),
                                    ["penny_rounding_against_us", "winnings_floor_and_commission_ceiling",
                                     "commission_half_up_rounding"]),
            "winnings to the nearest penny": (mock.patch.object(accounting, "WIN_ROUNDING", ROUND_HALF_DOWN),
                                              ["winnings_floor_and_commission_ceiling",
                                               "commission_half_up_rounding"]),
            "other removal ignored": (mock.patch.object(accounting, "bet_reasons", lambda o, b, c: [
                r for r in real_bet(o, b, c) if r != "other_runner_removed_after_decision"]),
                ["other_runner_removed_after_decision_is_unresolved"]),
            "revision ignored": (mock.patch.object(accounting, "market_reasons", lambda o: [
                r for r in real_market(o) if r != "result_revised_after_close"]),
                ["result_revised_after_close_is_unresolved"]),
            "an extra reason after the first": (mock.patch.object(accounting, "bet_reasons", lambda o, b, c: (
                real_bet(o, b, c) + ["removals_unknown"]) if real_bet(o, b, c) else []), unresolved),
            "another settlement case": (mock.patch.dict(accounting.SETTLEMENT_CASE, {"bsp_missing": "market_void"}),
                                        ["missing_bsp_is_unresolved"]),
            "a settlement that drops its bets": (mock.patch.object(
                engineering, "settle", lambda *args: replace(real_settle(*args), bets=())),
                [case["id"] for case in engineering.KAT_CASES]),
            "a settlement that cannot compute": (mock.patch.object(accounting, "win_gross_pence",
                                                                   side_effect=InvalidOperation), resolved),
            "a settlement that repeats a bet": (mock.patch.object(
                engineering, "settle", lambda *args: (lambda s: replace(s, bets=s.bets + s.bets[:1]))(
                    real_settle(*args))), [case["id"] for case in engineering.KAT_CASES]),
            "a settlement that repeats a market": (mock.patch.object(
                engineering, "settle", lambda *args: (lambda s: replace(s, markets=s.markets + s.markets[-1:]))(
                    real_settle(*args))), [case["id"] for case in engineering.KAT_CASES]),
        }
        for name, (patch, expected) in perturbations.items():
            with self.subTest(perturbation=name), patch:
                result = known_accounting_answers()
                self.assertEqual((result["status"], result["failed_cases"]), (FAILED, expected))

    def test_the_expected_constants_are_compared_whole(self):
        void = next(case for case in engineering.KAT_CASES if case["id"] == "void_is_unresolved_with_exposure")
        exposure = {**void["expected_markets"], ("kat", "1.000000006"): (None, None, 99)}
        for change in ({"expected_markets": exposure}, {"expected_net": 1},
                       {"expected_bets": [("unresolved", ("market_void", "dead_heat"), "market_void", None)]}):
            with self.subTest(change=sorted(change)), mock.patch.dict(void, change):
                self.assertEqual(known_accounting_answers()["failed_cases"], ["void_is_unresolved_with_exposure"])


class ReconciliationTests(EngineeringCase):
    def material(self):
        """[records, decisions, ledger bytes, bets, settlement] of a consistent two-market run."""
        members = [(path(mid), fx.standard_market(mid)) for mid in ("1.100000001", "1.100000002")]
        _, _, records, decisions = self.run_material(members)
        decisions = list(decisions.values())
        bets = [bet for d in decisions for bet in issued_bets(d)]
        self.outcomes = [market_outcome(records[d.market_id], d) for d in decisions if issued_bets(d)]
        self.accounting_rules = accounting.AccountingRules.from_contract(self.contract)
        return [list(records.values()), decisions, b"".join(ledger_lines(decisions)), bets,
                accounting.settle(bets, self.outcomes, self.accounting_rules)]

    def reconcile(self, material, **changes):
        names = ("records", "decisions", "ledger", "bets", "settlement")
        values = dict(zip(names, material), **changes)
        return issuance_exposure_reconciliation(self.rules, *(values[name] for name in names))

    def test_a_consistent_run_passes(self):
        result = self.reconcile(self.material())
        self.assertEqual((result["status"], result["problems"]), (PASSED, []))
        self.assertEqual({v: result["by_variant"][v]["issued"] for v in self.rules.variants},
                         {"ltp_favourite": 2, "seeded_random_runner": 2})

    def test_missing_duplicate_and_swapped_material_fails(self):
        material = self.material()
        records, decisions, ledger, bets, settlement = material
        swapped = [replace(bets[0], selection_id=99)] + bets[1:]
        short = accounting.settle(bets[1:], self.outcomes, self.accounting_rules)
        rules = self.accounting_rules
        one_variant = accounting.AccountingRules(commission_rate=rules.commission_rate, rounding=rules.rounding,
                                                 settlement=dict(rules.settlement), stake_pence=rules.stake_pence,
                                                 variants=("ltp_favourite",))
        unmeasurable = accounting.settle([b for b in bets if b.variant == "ltp_favourite"], self.outcomes,
                                         one_variant)
        first = decisions[0]
        other_rules = accounting.AccountingRules(commission_rate=rules.commission_rate, rounding=rules.rounding,
                                                 settlement=dict(rules.settlement), stake_pence=rules.stake_pence)

        def rows_of_first(change, only_first_row=False):
            rows = [dict(row, **change(row)) if index == 0 or not only_first_row else row
                    for index, row in enumerate(first.rows)]
            return [replace(first, rows=tuple(rows))] + decisions[1:]

        cases = {
            "rows on a market that is not eligible": (
                {"decisions": [replace(first, population="excluded", reason="plan_not_contract_tier")] + decisions[1:]},
                "decisions:1_not_eligible_with_rows"),
            "rows on another plan": ({"decisions": rows_of_first(lambda row: {"plan": "PRO"})},
                                     f"rows:{len(first.rows)}_not_their_decisions"),
            "a row under another decision id": (
                {"decisions": rows_of_first(lambda row: {"decision_id": "0" * 64}, only_first_row=True)},
                "rows:1_not_their_decisions"),
            "a row with another variant": (
                {"decisions": rows_of_first(lambda row: {"variants": dict(row["variants"], zzz=None)},
                                            only_first_row=True)}, "rows:1_without_exactly_the_contract_variants"),
            "a settlement for other variants": ({"settlement": accounting.settle(bets, self.outcomes, other_rules)},
                                                "settlement:other_variants"),
            "a missing bet": ({"bets": bets[1:]}, "bets:not_the_issued_rows"),
            "a duplicate bet": ({"bets": bets + bets[:1]}, "bets:duplicate"),
            "a swapped bet": ({"bets": swapped}, "bets:not_the_issued_rows"),
            "a settlement short of a bet": ({"settlement": short}, "settled:not_the_issued_rows"),
            "no settlement": ({"settlement": None}, "settlement:not_a_settlement"),
            "a settlement that cannot be measured": ({"settlement": unmeasurable}, "metrics:ValueError"),
            "a swapped record": ({"records": [dict(records[0], market_id="1.100000009")] + records[1:]},
                                 "decisions:missing_for_1_records"),
            "a record without a decision": ({"records": records + [dict(records[0], market_id="1.100000009")]},
                                            "decisions:missing_for_1_records"),
            "a decision without a record": ({"records": records[1:]}, "decisions:1_without_an_input_record"),
            "a duplicate record": ({"records": records + records[:1]}, "records:duplicate"),
            "a duplicate decision": ({"decisions": decisions + decisions[:1]}, "issuance:CohortProblem"),
            "an edited ledger": ({"ledger": ledger.replace(b"ltp_favourite", b"ltp_favourita", 1)},
                                 "ledger:not_the_decisions"),
            "a ledger short of a line": ({"ledger": ledger[:ledger.index(b"\n") + 1]}, "ledger:not_the_decisions"),
            "a ledger as text": ({"ledger": ledger.decode()}, "ledger:not_the_decisions"),
        }
        for name, (changes, problem) in cases.items():
            with self.subTest(case=name):
                result = self.reconcile(material, **changes)
                self.assertEqual(result["status"], FAILED, result)
                self.assertTrue(any(p.startswith(problem) for p in result["problems"]), result)
        both = self.reconcile(material, records=[dict(records[0], market_id="1.100000009")] + records[1:])
        self.assertIn("decisions:1_without_an_input_record", both["problems"])

    def test_issued_must_be_resolved_plus_unresolved_in_count_and_stake(self):
        material = self.material()
        real = engineering.metrics_by_variant
        for field, problem in (("resolved_bets", "issued_not_resolved_plus_unresolved"),
                               ("resolved_stake_pence", "issued_stake_not_s_r_plus_s_u")):
            def off_by_one(settlement, variants, field=field):
                return tuple(replace(m, **{field: getattr(m, field) + 1}) if m.variant == "ltp_favourite" else m
                             for m in real(settlement, variants))
            with self.subTest(field=field), mock.patch.object(engineering, "metrics_by_variant", off_by_one):
                result = self.reconcile(material)
                self.assertEqual((result["status"], result["problems"]), (FAILED, [f"ltp_favourite:{problem}"]))

    def test_a_variant_that_issued_nothing_is_incomplete(self):
        tie = cohort_fx.VariantTests.ltp_market(self, {1: 2.5, 2: 2.5, 3: 6.0})
        _, _, records, decisions = self.run_material([(path(cohort_fx.M), tie)])
        decision = decisions[cohort_fx.M]
        bets = list(issued_bets(decision))
        settlement = accounting.settle(bets, [market_outcome(records[cohort_fx.M], decision)],
                                       accounting.AccountingRules.from_contract(self.contract))
        result = issuance_exposure_reconciliation(self.rules, list(records.values()), [decision],
                                                  b"".join(ledger_lines([decision])), bets, settlement)
        self.assertEqual((result["status"], result["unexercised_variants"]), (INCOMPLETE, ["ltp_favourite"]))


class PrefixTests(EngineeringCase):
    def test_the_whole_anchor_by_t_population_is_compared(self):
        results, decisions = self.prefix(cohort_fx.scenario_members("full"))
        statuses = {mid: (r["status"], r["reason"]) for mid, r in results.items()}
        compared = {mid for mid, (status, _) in statuses.items() if status in (PASSED, FAILED)}
        self.assertEqual({mid: s for mid, s in statuses.items() if s[0] == FAILED}, {})
        self.assertEqual(statuses[cohort_fx.LATE], (NOT_CANDIDATE, "anchor_not_known_by_decision"))
        self.assertEqual(statuses["1.400000007"], (UNEXERCISED, f"{UNKNOWN}:not_json"))
        for mid in ("1.400000001", "1.400000002", "1.400000003", "1.400000008", "1.400000009", "1.400000013",
                    "1.400000014", cohort_fx.COMPETING):
            self.assertIn(mid, compared, mid)
        self.assertEqual({decisions[mid].population for mid in compared}, {"eligible", "excluded", "unclassifiable"})
        boundary = results["1.400000002"]["prefix"]["boundary"]
        self.assertEqual(boundary, "first_message_after_t")
        self.assertEqual(results["1.400000009"]["status"], PASSED)            # stopped by an early close
        self.assertEqual(results["1.400000008"]["prefix"]["boundary"], "first_message_after_t")
        self.assertEqual({r["prefix_limit_bytes"] for r in results.values()}, {engineering.PREFIX_LIMIT_BYTES})
        self.assertTrue(all(r["read_limits"] == dataclasses.asdict(DEFAULT_LIMITS) for r in results.values()))
        self.assertEqual({mid: r["stage"] for mid, r in results.items() if r["stage"] != "prefix"},
                         {cohort_fx.LATE: "record"})

    def test_candidacy_comes_from_the_record_not_the_decision(self):
        mid = "1.100000001"
        root, manifest, records, decisions = self.run_material([(path(mid), fx.standard_market(mid))])
        decision = decisions[mid]
        for name, wrong in (("no view", replace(decision, population="no_view", rows=())),
                            ("late anchor", replace(decision, population="anchor_not_known_by_decision")),
                            ("another market", replace(decision, market_id="1.100000009")),
                            ("another plan", replace(decision, plan="PRO")),
                            ("another reason", replace(decision, reason="plan_not_contract_tier")),
                            ("another anchor", replace(decision, anchor_pt=decision.anchor_pt - 1)),
                            ("another t", replace(decision, decision_ms=decision.decision_ms + 1))):
            with self.subTest(case=name):
                result = check_prefix(self.rules, records[mid], wrong, root, manifest, DEFAULT_LIMITS)
                self.assertEqual((result["status"], result["stage"], result["reason"]),
                                 (FAILED, "record", "decision_not_the_records_classification"))
                self.assertEqual(result["rows"], len(wrong.rows))

    def test_a_market_without_a_decision_at_t_must_have_no_rows(self):
        late = "1.100000002"
        members = [(path("1.100000001"), fx.standard_market("1.100000001")),
                   (path(late), [md(late, OFF - timedelta(minutes=4), definition(OFF, cohort_fx.three()))]
                    + cohort_fx.post_normal(late))]
        root, manifest, records, decisions = self.run_material(members)
        self.assertEqual(decisions[late].population, "anchor_not_known_by_decision")
        borrowed = tuple(dict(row, market_id=late) for row in decisions["1.100000001"].rows)
        result = check_prefix(self.rules, records[late], replace(decisions[late], rows=borrowed), root, manifest,
                              DEFAULT_LIMITS)
        self.assertEqual((result["status"], result["stage"], result["reason"]),
                         (FAILED, "record", "decision_differs:rows"))

    def test_a_difference_only_canonical_json_sees_is_named(self):
        mid = "1.100000001"
        root, manifest, records, decisions = self.run_material([(path(mid), fx.standard_market(mid))])
        decision = decisions[mid]
        first = decision.rows[0]
        self.assertEqual(first["input"]["ltp"], 3.0)
        integral = dict(first, input=dict(first["input"], ltp=3))            # equal in Python, not as JSON
        result = check_prefix(self.rules, records[mid], replace(decision, rows=(integral,) + decision.rows[1:]),
                              root, manifest, DEFAULT_LIMITS)
        self.assertEqual((result["status"], result["stage"], result["reason"]),
                         (FAILED, "prefix", "decision_differs:rows"))

    def test_the_member_is_checked_as_replay_checks_it(self):
        mid = "1.100000001"
        root, manifest, records, decisions = self.run_material([(path(mid), fx.standard_market(mid))])
        record = records[mid]

        def check(provenance=None):
            item = record if provenance is None else dict(record, provenance=dict(record["provenance"],
                                                                                  **provenance))
            return check_prefix(self.rules, item, decisions[mid], root, manifest, DEFAULT_LIMITS)

        for change, problem in (({"archive_sha256": "0" * 64}, "archive_mismatch"),
                                ({"member_sha256": "0" * 64}, "member_sha256_mismatch"),
                                ({"path_date": "2015-06-02"}, "provenance_mismatch"),
                                ({"member_path": path("1.100000009")}, "member_not_in_manifest")):
            with self.subTest(change=change), self.assertRaisesRegex(replay.ReplayProblem, problem):
                check(change)
        with mock.patch.object(engineering, "safe_target", side_effect=ArchiveProblem("raw:x:symlink")), \
                self.assertRaisesRegex(replay.ReplayProblem, f"prefix:{mid}:raw:x:symlink"):
            check()
        raw = safe_target(root, manifest["archive"]["sha256"], path(mid))
        if os.geteuid() != 0:
            raw.chmod(0)
            with self.assertRaisesRegex(replay.ReplayProblem, "member_unreadable_file"):
                check()
            raw.chmod(0o600)
        raw.unlink()
        with self.assertRaisesRegex(replay.ReplayProblem, "member_missing"):
            check()

    def test_a_known_failure_at_or_before_t_is_kept_and_compared(self):
        mid = "1.100000001"
        segmented = {"op": "mcm", "clk": "1", "pt": ms(T) - 1000, "segmentType": "SEG_START", "mc": [{"id": mid}]}
        messages = fx.standard_market(mid)[:3] + [segmented, b"{not json\n"] + fx.standard_market(mid)[3:]
        results, decisions = self.prefix([(path(mid), messages)])
        self.assertEqual((results[mid]["status"], results[mid]["prefix"]["boundary"]),
                         (PASSED, "known_failure_at_or_before_t"))
        self.assertEqual(decisions[mid].rows[0]["variants"]["ltp_favourite"]["reason"], "decision_view_unreadable")

    def test_a_batch_cut_by_a_failure_at_its_pt_is_dropped_as_the_index_drops_it(self):
        mid = "1.100000001"
        at = ms(T) - 60000
        closed = md(mid, at, definition(OFF, [runner(1), runner(2), runner(3)], status="CLOSED"))
        unreadable = {"op": "mcm", "clk": "1", "pt": at, "mc": "not a list"}    # the index cannot read past it
        messages = fx.standard_market(mid)[:2] + [closed, unreadable] + fx.standard_market(mid)[2:]
        root, manifest, records, decisions = self.run_material([(path(mid), messages)])
        self.assertEqual(records[mid]["decision_time"]["view_ends"], {"pt": at, "reason": "member_unreadable_from_pt"})
        self.assertEqual(decisions[mid].rows[0]["variants"]["ltp_favourite"]["reason"], "decision_view_unreadable")
        result = check_prefix(self.rules, records[mid], decisions[mid], root, manifest, DEFAULT_LIMITS)
        self.assertEqual((result["status"], result["prefix"]["boundary"], result["prefix"]["lines"]),
                         (PASSED, "known_failure_at_or_before_t", 4))

    def test_a_refused_redecision_or_a_market_absent_from_the_prefix_is_failed(self):
        mid = "1.100000001"
        root, manifest, records, decisions = self.run_material([(path(mid), fx.standard_market(mid))])

        def check():
            return check_prefix(self.rules, records[mid], decisions[mid], root, manifest, DEFAULT_LIMITS)

        for error in (replay.ReplayProblem("replay:x"), engineering.CohortProblem("cohort:y"), ValueError("z")):
            with self.subTest(error=error), mock.patch.object(engineering, "decide_market", side_effect=error):
                self.assertEqual((check()["status"], check()["reason"]), (FAILED, f"redecision:{error}"))
        with mock.patch.object(engineering, "fold_member_stream", return_value=({}, None, None)):
            self.assertEqual((check()["status"], check()["reason"]), (FAILED, "market_absent_from_prefix"))

    def test_the_prefix_is_the_raw_bytes_and_the_running_total_counts_the_boundary_line(self):
        mid = "1.100000001"
        messages = fx.standard_market(mid)
        root, manifest, records, decisions = self.run_material([(path(mid), messages)])

        def check(limits):
            return check_prefix(self.rules, records[mid], decisions[mid], root, manifest, limits)

        full = check(DEFAULT_LIMITS)
        raw_lines = fx.lines(messages).splitlines(keepends=True)
        kept = raw_lines[:full["prefix"]["lines"]]
        self.assertEqual(full["prefix"]["bytes"], sum(map(len, kept)))
        self.assertEqual(full["prefix"]["sha256"],
                         hashlib.sha256(engineering.PREFIX_DIGEST_TAG + b"".join(kept)).hexdigest())
        boundary = raw_lines[full["prefix"]["lines"]]
        total = full["prefix"]["bytes"] + len(boundary)
        exact = check(DEFAULT_LIMITS.stricter(max_member_decompressed_bytes=total))
        over = check(DEFAULT_LIMITS.stricter(max_member_decompressed_bytes=total - 1))
        self.assertEqual((exact["status"], exact["prefix"]["sha256"]), (PASSED, full["prefix"]["sha256"]))
        self.assertEqual((over["status"], over["reason"], over["prefix"]["sha256"]),
                         (UNEXERCISED, f"{UNKNOWN}:decompressed_too_large", None))

    def test_a_long_boundary_line_is_unexercised(self):
        mid = "1.100000001"
        after = dict(prices(mid, ms(T) + 1000, {1: 2.0}), pad="x" * 5000)
        messages = cohort_fx.pre_standard(mid) + [after] + cohort_fx.post_normal(mid)
        results, _ = self.prefix([(path(mid), messages)], limits=DEFAULT_LIMITS.stricter(max_line_bytes=4000))
        self.assertEqual((results[mid]["status"], results[mid]["reason"]), (UNEXERCISED, f"{UNKNOWN}:line_too_long"))
        results, _ = self.prefix([(path(mid), messages)])
        self.assertEqual(results[mid]["status"], PASSED)

    def test_a_full_buffer_is_unexercised_never_passed(self):
        results, _ = self.prefix([(path("1.100000001"), fx.standard_market())], prefix_limit=300)
        self.assertEqual((results["1.100000001"]["status"], results["1.100000001"]["reason"],
                          results["1.100000001"]["prefix_limit_bytes"]), (UNEXERCISED, "prefix_buffer_limit", 300))

    def test_the_limit_is_checked_before_each_append(self):
        members = [(path("1.100000001"), fx.standard_market())]
        full, _ = self.prefix(members)
        size = full["1.100000001"]["prefix"]["bytes"]
        exact, _ = self.prefix(members, prefix_limit=size)
        short, _ = self.prefix(members, prefix_limit=size - 1)
        self.assertEqual(exact["1.100000001"]["status"], PASSED)
        self.assertEqual((short["1.100000001"]["status"], short["1.100000001"]["reason"]),
                         (UNEXERCISED, "prefix_buffer_limit"))

    def test_a_decreasing_pt_before_the_boundary_is_unexercised(self):
        mid = "1.100000001"
        back = prices(mid, ms(T) - 600000, {1: 2.0})               # published before the previous message
        messages = fx.standard_market(mid)[:3] + [back] + fx.standard_market(mid)[3:]
        results, _ = self.prefix([(path(mid), messages)])
        self.assertEqual((results[mid]["status"], results[mid]["reason"]),
                         (UNEXERCISED, f"{UNKNOWN}:invalid_or_decreasing_pt"))

    def test_a_line_that_is_not_a_message_before_the_boundary_is_unexercised(self):
        mid = "1.100000001"
        messages = fx.standard_market(mid)[:3] + [b"[1, 2]\n"] + fx.standard_market(mid)[3:]
        results, _ = self.prefix([(path(mid), messages)])
        self.assertEqual((results[mid]["status"], results[mid]["reason"]),
                         (UNEXERCISED, f"{UNKNOWN}:malformed_message"))

    def test_a_stream_that_fails_before_the_boundary_is_unexercised(self):
        data = bz2.compress(fx.lines(cohort_fx.pre_standard("1.100000001")))[:-20]     # nothing published after t
        raw = self.tmp / "cut.bz2"
        raw.write_bytes(data)
        member = {"path": "cut.bz2", "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        _, digest, _, boundary, reason = engineering._capture(raw, member, ms(T), DEFAULT_LIMITS, 1 << 20)
        self.assertEqual((boundary, reason), (None, f"{UNKNOWN}:not_bz2"))
        with self.assertRaisesRegex(replay.ReplayProblem, "member_hash_mismatch"):
            engineering._capture(raw, dict(member, size_bytes=len(data) + 1), ms(T), DEFAULT_LIMITS, 1 << 20)

    def test_the_prefix_reader(self):
        reader = engineering._PrefixReader(bytearray(b"ab\ncd\nef"))
        self.assertEqual([reader.readline(), reader.readline(1), reader.readline(-1), reader.readline(),
                          reader.readline()], [b"ab\n", b"c", b"d\n", b"ef", b""])

    def test_a_changed_member_is_an_integrity_failure(self):
        root, manifest, records, decisions = self.run_material([(path("1.100000001"), fx.standard_market())])
        raw = safe_target(root, manifest["archive"]["sha256"], path("1.100000001"))
        raw.chmod(0o600)
        raw.write_bytes(raw.read_bytes() + b"x")
        with self.assertRaisesRegex(replay.ReplayProblem, "member_hash_mismatch"):
            check_prefix(self.rules, records["1.100000001"], decisions["1.100000001"], root, manifest,
                         DEFAULT_LIMITS)

    def test_a_future_leak_in_the_runs_decisions_is_reported_as_failed(self):
        members = [(path(mid), cohort_fx.pre_standard(mid) + cohort_fx.post_normal(mid))   # no corruption after t
                   for mid in ("1.400000001", "1.400000002")]
        real_fold, real_upto = replay._fold, replay._upto

        def leaky_fold(raw, market_id, t, limits):          # the run's replay reads 3 minutes past t
            return real_fold(raw, market_id, t + 180000, limits)

        with mock.patch.object(replay, "_fold", leaky_fold), \
                mock.patch.object(replay, "_upto", lambda timeline, t: real_upto(timeline, t + 180000)):
            root, manifest, records, decisions = self.run_material(members)
        results = {mid: check_prefix(self.rules, records[mid], decisions[mid], root, manifest, DEFAULT_LIMITS)
                   for mid in records}
        failed = sorted(mid for mid, r in results.items() if r["status"] == FAILED)
        self.assertEqual(failed, ["1.400000001", "1.400000002"])        # off-2m prices leaked into both
        self.assertTrue(results["1.400000001"]["reason"].startswith("decision_differs:"))
        summary = self.tally(results, decisions)
        self.assertEqual((summary["status"], summary["counts"][FAILED]), (FAILED, 2))
        self.assertEqual([item[1] for item in summary["failures"]], ["1.400000001", "1.400000002"])
        self.assertEqual(summary["counts"]["compared_rows"], sum(len(d.rows) for d in decisions.values()))
        self.assertGreater(summary["issued_among_compared"]["ltp_favourite"], 0)


class TallyTests(EngineeringCase):
    def test_the_tally_reconciles_its_counts(self):
        results, decisions = self.prefix(cohort_fx.scenario_members("full"))
        summary = self.tally(results, decisions)
        counts = summary["counts"]
        self.assertEqual(summary["status"], INCOMPLETE)                     # one unknown-pt market was unexercised
        self.assertEqual(summary["unexercised_by_reason"], {f"{UNKNOWN}:not_json": 1})
        self.assertEqual(summary["not_candidate_by_reason"], {"anchor_not_known_by_decision": 1})
        self.assertEqual((counts["records"], counts["missing"]), (len(results), 0))
        self.assertEqual(counts["records"], counts[NOT_CANDIDATE] + counts["failed_against_record"]
                         + counts["candidates"] + counts["missing"])
        self.assertEqual(counts["candidates"], counts[UNEXERCISED] + counts["assessable"])
        self.assertEqual((counts["assessable"], counts["compared"]), (counts[PASSED] + counts[FAILED],) * 2)
        self.assertEqual(counts["compared_rows"], sum(len(decisions[mid].rows) for mid, r in results.items()
                                                      if r["status"] in (PASSED, FAILED)))
        self.assertGreater(summary["issued_among_compared"]["ltp_favourite"], 0)
        self.assertEqual(self.tally(results, decisions, order=lambda r: sorted(r, reverse=True))["results_sha256"],
                         summary["results_sha256"])
        competing = results[cohort_fx.COMPETING]
        changed = dict(results, **{cohort_fx.COMPETING: dict(competing, prefix=dict(competing["prefix"], lines=0))})
        self.assertNotEqual(self.tally(changed, decisions)["results_sha256"], summary["results_sha256"])

    def test_a_failure_against_the_record_is_counted_apart(self):
        mid = "1.100000001"
        root, manifest, records, decisions = self.run_material(
            [(path(m), fx.standard_market(m)) for m in (mid, "1.100000002")])
        results = {m: check_prefix(self.rules, records[m], decisions[m], root, manifest, DEFAULT_LIMITS)
                   for m in records}
        wrong = replace(decisions[mid], decision_ms=decisions[mid].decision_ms + 1)
        results[mid] = check_prefix(self.rules, records[mid], wrong, root, manifest, DEFAULT_LIMITS)
        summary = self.tally(results, dict(decisions, **{mid: wrong}))
        self.assertEqual((summary["status"], summary["failures"]),
                         (FAILED, [["BASIC", mid, "record", "decision_not_the_records_classification"]]))
        counts = summary["counts"]
        self.assertEqual((counts["failed_against_record"], counts["candidates"], counts["compared"], counts[FAILED]),
                         (1, 1, 1, 0))

    def test_a_clean_run_passes_and_a_missing_record_is_incomplete(self):
        members = [(path(mid), fx.standard_market(mid)) for mid in ("1.100000001", "1.100000002")]
        results, decisions = self.prefix(members)
        self.assertEqual(self.tally(results, decisions)["status"], PASSED)
        keys = [(results[mid]["plan"], mid) for mid in results]
        partial = self.tally({"1.100000001": results["1.100000001"]}, decisions, keys=keys)
        self.assertEqual((partial["status"], partial["counts"]["missing"], partial["missing_records"]),
                         (INCOMPLETE, 1, [["BASIC", "1.100000002"]]))
        nothing = self.tally({}, decisions, keys=[])
        self.assertEqual((nothing["status"], nothing["counts"]["compared"]), (INCOMPLETE, 0))

    def test_a_variant_without_issuance_is_incomplete(self):
        tie = cohort_fx.VariantTests.ltp_market(self, {1: 2.5, 2: 2.5, 3: 6.0})
        results, decisions = self.prefix([(path(cohort_fx.M), tie)])
        summary = self.tally(results, decisions)
        self.assertEqual((summary["status"], summary["unissued_variants"]), (INCOMPLETE, ["ltp_favourite"]))

    def test_the_tally_refuses_what_it_cannot_count(self):
        results, decisions = self.prefix([(path("1.100000001"), fx.standard_market())])
        result, decision = results["1.100000001"], decisions["1.100000001"]
        keys = [("BASIC", "1.100000001")]
        with self.assertRaisesRegex(ValueError, "no_variants"):
            PrefixTally((), keys)
        with self.assertRaisesRegex(ValueError, "duplicate_record_key"):
            PrefixTally(self.rules.variants, keys * 2)
        stricter = DEFAULT_LIMITS.stricter(max_line_bytes=1000)
        for name, (args, problem, kwargs) in {
                "an unknown record": ((dict(result, market_id="1.100000009"), decision), "not_an_input_record", {}),
                "another record's decision": ((result, replace(decision, market_id="1.100000009")),
                                              "decision_for_another_record", {}),
                "another prefix limit": ((result, decision), "other_limits", {"prefix_limit": 300}),
                "other read limits": ((result, decision), "other_limits", {"limits": stricter}),
                "a malformed result": (({k: v for k, v in result.items() if k != "stage"}, decision),
                                       "malformed_result", {}),
                "an unknown status": ((dict(result, status="bogus"), decision), "malformed_status", {}),
                "an unknown stage": ((dict(result, stage="bogus"), decision), "malformed_status", {}),
                "a not-candidate compared": ((dict(result, status=NOT_CANDIDATE), decision), "malformed_status", {}),
                "a pass against the record": ((dict(result, stage="record", prefix=None), decision),
                                              "malformed_status", {})}.items():
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, problem):
                PrefixTally(self.rules.variants, keys, **kwargs).add(*args)
        tally = PrefixTally(self.rules.variants, keys)
        tally.add(result, decision)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            tally.add(result, decision)
        self.assertEqual(tally.result()["counts"]["candidates"], 1)

    def test_a_result_is_bound_to_the_decision_it_checked(self):
        mid = "1.100000001"
        root, manifest, records, decisions = self.run_material([(path(mid), fx.standard_market(mid))])
        decision = decisions[mid]
        result = check_prefix(self.rules, records[mid], decision, root, manifest, DEFAULT_LIMITS)
        self.assertEqual((result["status"], result["decision_sha256"]),
                         (PASSED, engineering.decision_sha256(decision)))
        rows = [dict(row) for row in decision.rows]
        rows[0] = dict(rows[0], input=dict(rows[0]["input"], ltp=999.0))
        changed = replace(decision, rows=tuple(rows))
        keys = [("BASIC", mid)]
        for name, (given, paired) in {"a changed decision under the same key": (result, changed),
                                      "another row count": (dict(result, rows=result["rows"] + 1), decision)}.items():
            with self.subTest(case=name):
                tally = PrefixTally(self.rules.variants, keys)
                with self.assertRaisesRegex(ValueError, "decision_not_the_one_checked"):
                    tally.add(given, paired)
                self.assertEqual((tally.result()["status"], tally.result()["counts"]["missing"]), (INCOMPLETE, 1))
        # Astra's probe: with the changed decision every other criterion passes, so the binding is what refuses it.
        bets = issued_bets(changed)
        settlement = accounting.settle(bets, [market_outcome(records[mid], changed)],
                                       accounting.AccountingRules.from_contract(self.contract))
        reconciliation = issuance_exposure_reconciliation(self.rules, records.values(), [changed],
                                                          b"".join(ledger_lines([changed])), bets, settlement)
        self.assertEqual((known_accounting_answers()["status"], reconciliation["status"]), (PASSED, PASSED))
        self.assertEqual(check_prefix(self.rules, records[mid], changed, root, manifest, DEFAULT_LIMITS)["reason"],
                         "decision_differs:rows")
        tally = PrefixTally(self.rules.variants, keys)
        tally.add(result, decision)
        self.assertEqual(tally.result()["status"], PASSED)

    def test_the_digest_binds_the_decision_even_when_nothing_was_compared(self):
        mid = "1.100000001"
        messages = fx.standard_market(mid)[:3] + [b"[1, 2]\n"] + fx.standard_market(mid)[3:]
        root, manifest, records, decisions = self.run_material([(path(mid), messages)])
        decision = decisions[mid]
        other = replace(decision, rows=(dict(decision.rows[0], input={"ltp": 1.5, "ltp_pt": 1}),))
        digests = []
        for given in (decision, other):
            result = check_prefix(self.rules, records[mid], given, root, manifest, DEFAULT_LIMITS)
            self.assertEqual(result["status"], UNEXERCISED)              # the prefix never compares a decision
            tally = PrefixTally(self.rules.variants, [("BASIC", mid)])
            tally.add(result, given)
            digests.append(tally.result()["results_sha256"])
        self.assertNotEqual(digests[0], digests[1])

    def test_a_refused_result_leaves_the_tally_unchanged_and_retryable(self):
        mids = ("1.100000001", "1.100000002")
        results, decisions = self.prefix([(path(mid), fx.standard_market(mid)) for mid in mids])
        tally = PrefixTally(self.rules.variants, [("BASIC", mid) for mid in mids])
        tally.add(results[mids[0]], decisions[mids[0]])
        before = tally.result()
        good = results[mids[1]]
        for name, bad in {
                "a pass claimed against the record": dict(good, stage="record", prefix=None),
                "a pass without evidence": dict(good, prefix=None, parent=None, rows=-17)}.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(ValueError, "malformed_"):
                    tally.add(bad, decisions[mids[1]])
                after = tally.result()
                self.assertEqual(after, before)
                self.assertEqual((after["status"], after["counts"]["missing"], after["counts_reconcile"]),
                                 (INCOMPLETE, 1, True))
        tally.add(good, decisions[mids[1]])
        self.assertEqual((tally.result()["status"], tally.result()["counts"]["missing"]), (PASSED, 0))

    def test_malformed_evidence_is_refused_for_each_stage_and_status(self):
        results, decisions = self.prefix(cohort_fx.scenario_members("full"))
        passed, unexercised, late = results["1.400000001"], results["1.400000007"], results[cohort_fx.LATE]
        self.assertEqual([r["status"] for r in (passed, unexercised, late)], [PASSED, UNEXERCISED, NOT_CANDIDATE])
        failed_record = dict(late, status=FAILED, reason="decision_not_the_records_classification")
        sha = "0" * 64
        cases = {
            "no parent": (dict(passed, parent=None), "parent"),
            "a parent without a member sha": (dict(passed, parent=dict(passed["parent"], member_sha256="x")), "parent"),
            "negative rows": (dict(passed, rows=-1), "decision"),
            "rows as a bool": (dict(passed, rows=True), "decision"),
            "no decision digest": (dict(passed, decision_sha256=None), "decision"),
            "a pass without a prefix": (dict(passed, prefix=None), "prefix"),
            "a pass without a prefix digest": (dict(passed, prefix=dict(passed["prefix"], sha256=None)), "prefix"),
            "a pass with an unknown boundary": (dict(passed, prefix=dict(passed["prefix"], boundary="x")), "prefix"),
            "a pass with negative bytes": (dict(passed, prefix=dict(passed["prefix"], bytes=-1)), "prefix"),
            "a prefix with another key": (dict(passed, prefix=dict(passed["prefix"], extra=1)), "prefix"),
            "a pass with a reason": (dict(passed, reason="x"), "reason"),
            "a comparison failure without a reason": (dict(passed, status=FAILED), "reason"),
            "an unexercised prefix with a digest": (dict(unexercised, prefix=dict(unexercised["prefix"], sha256=sha)),
                                                   "prefix"),
            "an unexercised prefix with a boundary": (
                dict(unexercised, prefix=dict(unexercised["prefix"], boundary="end_of_stream")), "prefix"),
            "an unexercised reason outside the set": (dict(unexercised, reason="x"), "reason"),
            "a not-candidate with another reason": (dict(late, reason="x"), "reason"),
            "a not-candidate with a prefix": (dict(late, prefix=passed["prefix"]), "prefix"),
            "a record failure without a reason": (dict(failed_record, reason=None), "status"),
            "an unexercised record": (dict(late, status=UNEXERCISED), "status"),
        }
        keys = [(r["plan"], mid) for mid, r in results.items()]
        for name, (bad, problem) in cases.items():
            mid = bad["market_id"]
            with self.subTest(case=name):
                tally = PrefixTally(self.rules.variants, keys)
                with self.assertRaisesRegex(ValueError, f"malformed_{problem}$"):
                    tally.add(bad, decisions[mid])
                self.assertEqual(tally.result()["counts"]["missing"], len(keys))
        for good in (passed, unexercised, late, failed_record):
            PrefixTally(self.rules.variants, keys).add(good, decisions[good["market_id"]])

    def test_counts_that_do_not_reconcile_never_pass(self):
        mid = "1.100000001"
        results, decisions = self.prefix([(path(mid), fx.standard_market(mid))])
        tally = PrefixTally(self.rules.variants, [("BASIC", mid)])
        tally.add(results[mid], decisions[mid])
        self.assertEqual((tally.result()["status"], tally.result()["counts_reconcile"]), (PASSED, True))
        tally.counts["assessable"] += 1
        self.assertEqual((tally.result()["status"], tally.result()["counts_reconcile"]), (FAILED, False))


class StatusTests(unittest.TestCase):
    def test_engineering_status_needs_exactly_the_three_criteria(self):
        names = engineering.ENGINEERING_CRITERIA

        def status(*statuses, criteria=names, extra=None):
            given = {name: {"status": value} for name, value in zip(criteria, statuses)}
            return engineering_status(dict(given, **(extra or {})))

        self.assertEqual(status(PASSED, PASSED, PASSED), PASSED)
        for statuses, kwargs, expected in (
                ((PASSED, INCOMPLETE, PASSED), {}, INCOMPLETE), ((INCOMPLETE, FAILED, PASSED), {}, FAILED),
                ((PASSED, "bogus", PASSED), {}, INCOMPLETE), ((PASSED, None, PASSED), {}, INCOMPLETE),
                ((PASSED, PASSED), {}, INCOMPLETE), ((), {}, INCOMPLETE),
                ((PASSED, PASSED, PASSED), {"extra": {"another": {"status": PASSED}}}, INCOMPLETE),
                ((PASSED, PASSED, PASSED), {"extra": {names[0]: "passed"}}, INCOMPLETE),
                ((PASSED, PASSED), {"extra": {"another": {"status": FAILED}}}, FAILED)):
            with self.subTest(statuses=statuses, kwargs=kwargs):
                self.assertEqual(status(*statuses, **kwargs), expected)


class SharedFoldTests(EngineeringCase):
    def test_the_extracted_fold_gives_the_old_loops_output(self):
        mid = "1.100000001"
        segmented = {"op": "mcm", "clk": "1", "pt": ms(T) - 1000, "segmentType": "SEG_START", "mc": [{"id": mid}]}
        streams = {
            "standard": fx.standard_market(mid),
            "unknown pt": fx.standard_market(mid)[:3] + [b"{not json\n"] + fx.standard_market(mid)[3:],
            "known pt": fx.standard_market(mid)[:3] + [segmented] + fx.standard_market(mid)[3:],
            "image": cohort_fx.pre_standard(mid) + [md(mid, OFF - timedelta(minutes=8),
                                                       definition(OFF, [runner(1)]), img=True)],
            "equal pt": cohort_fx.pre_standard(mid) + [prices(mid, ms(T), {1: 2.0}), prices(mid, ms(T), {3: 1.5})],
            "event file": cohort_fx.pre_standard(mid) + cohort_fx.pre_standard("1.100000002"),
        }
        for name, messages in streams.items():
            with self.subTest(stream=name):
                root, manifest, records = self.build([(path(mid), messages)])
                raw = safe_target(root, manifest["archive"]["sha256"], path(mid))
                new, old = market_index._read_member(raw, DEFAULT_LIMITS), old_read_member(raw, DEFAULT_LIMITS)
                self.assertEqual(new[1:], old[1:])
                self.assertEqual(sorted(new[0]), sorted(old[0]))
                provenance = records[mid]["provenance"]
                for key in new[0]:
                    a, b = new[0][key], old[0][key]
                    if new[1]:
                        a.truncate(new[3])
                        b.truncate(old[3])
                    unreadable = (new[1], new[3]) if new[1] else None
                    self.assertEqual(json.dumps(a.record(provenance, unreadable), sort_keys=True),
                                     json.dumps(b.record(provenance, unreadable), sort_keys=True))
        truncated = bz2.compress(fx.lines(fx.standard_market(mid)))[:-20]
        root, manifest, _ = self.build([(path(mid), fx.standard_market(mid))])
        raw = safe_target(root, manifest["archive"]["sha256"], path(mid))
        raw.chmod(0o600)
        raw.write_bytes(truncated)
        new, old = market_index._read_member(raw, DEFAULT_LIMITS), old_read_member(raw, DEFAULT_LIMITS)
        self.assertEqual((new[1], new[3]), (old[1], old[3]))
        self.assertEqual(new[1], "not_bz2")
        with mock.patch.object(bz2, "BZ2File", side_effect=OSError("cannot open")):     # the stream never opens
            new, old = market_index._read_member(raw, DEFAULT_LIMITS), old_read_member(raw, DEFAULT_LIMITS)
        self.assertEqual((new[0], new[1], new[2], new[3]), (old[0], old[1], old[2], old[3]))
        self.assertEqual((new[1], new[3]), ("not_bz2", None))


if __name__ == "__main__":
    unittest.main()
