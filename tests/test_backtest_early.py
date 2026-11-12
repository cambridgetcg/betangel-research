"""Registered measurement early-price-v1 (betangel_research.betfair.backtest.early) and the split file version 2
(research/partitions-v2.json).

Everything here is synthetic. The split tests read only the two split files of the repository. Every ledger lives
in a temporary folder that the test removes, and its entries carry made-up hashes. Every market is a BASIC-shaped
synthetic file (last traded prices only) built here with the fixture helpers of
tests/test_betfair_backtest_replay.py, and goes through the real ingest, index and replay. These tests show that the
second split file gives every date the label and half the first gives, that it differs from the first only by the
two BASIC budgets and one exposure sentence, how one ledger keeps the budgets of studies that name different split
files, the registration's checks, and the codes, prices, cells and classes early.assess gives on these synthetic
shapes. They say nothing about real Betfair files or results.
"""

import bz2
import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from betangel_research.betfair.backtest import early, movement, resting
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, safe_target
from betangel_research.betfair.backtest.early import (CLASSES, CODES, DRIFT, FLAT, NO_LTP_KINDS, PRICED, RUNNER_CODES,
                                                      STEAM, UNDEFINED, VIEWED, Value, assess, changes,
                                                      changes_between, fold, move_cell, move_of, parse_registration,
                                                      record_code, split_named)
from betangel_research.betfair.backtest.lp import BACK, LAY
from betangel_research.betfair.backtest.movement import RegistrationError, exact, replay_market
from betangel_research.betfair.backtest.partition import file_bytes, parse_split
from betangel_research.betfair.backtest.replay import ReplayProblem, _member
from betangel_research.betfair.backtest.study_ledger import StudyLedger, head_text, verify

import test_betfair_backtest_replay as fx
from test_backtest_lane import SPLIT_SHA256 as V1_SHA256
from test_backtest_lane import LedgerCase, days, sha
from test_betfair_backtest_replay import definition, md, mcm, ms, prices, runner, settle

REPO = Path(__file__).resolve().parent.parent
V1_RAW = (REPO / "research" / "partitions-v1.json").read_bytes()
V2_FILE = REPO / "research" / "partitions-v2.json"
# The second split file's sha256 as reviewed: any change to the file must change this line too (a new split).
V2_SHA256 = "e65ff6c06da47e272dbb6bb2a6a1e308f99258a7e8fd6b55ed8ffc3d855e1e83"
V2_RAW = V2_FILE.read_bytes()
V1, V2 = parse_split(V1_RAW), parse_split(V2_RAW)
P0_BASIC = ("P0's run of 2026-10-17 and the index build read every 2023-25 BASIC file mechanically, results "
            "included; no strategy, settlement or price figure of those years was evaluated or released, only "
            "counts and digests.")


class SplitV2(unittest.TestCase):
    def test_the_file_is_the_reviewed_one_in_its_one_byte_form(self):
        self.assertEqual(hashlib.sha256(V2_RAW).hexdigest(), V2_SHA256)
        self.assertEqual(V2.sha256, V2_SHA256)
        V2_RAW.decode("ascii")
        self.assertEqual(V2_RAW, file_bytes(json.loads(V2_RAW)))
        self.assertTrue(V2_RAW.endswith(b"}\n") and not V2_RAW.endswith(b"\n\n"))
        self.assertEqual(json.loads(V2_RAW)["schema"], "betangel.partitions/v1")     # "v2" names the file only
        self.assertEqual(json.loads(V2_RAW)["ranges"][0]["salt"], json.loads(V1_RAW)["ranges"][0]["salt"])

    def test_the_first_split_file_is_unchanged(self):
        self.assertEqual(hashlib.sha256(V1_RAW).hexdigest(), V1_SHA256)
        self.assertEqual(V1.sha256, V1_SHA256)

    def test_it_is_the_first_file_with_three_edits(self):
        first, second = json.loads(V1_RAW), json.loads(V2_RAW)
        first["labels"]["BASIC-D"]["explore_budget"] = 10
        first["labels"]["BASIC-V"]["alpha_lifetime"] = "0.10"
        first["prior_exposure"]["p0-basic-2023-25"] = P0_BASIC
        self.assertEqual(second, first)
        self.assertEqual(file_bytes(first), V2_RAW)

    def test_every_date_gets_the_same_label_and_half(self):
        checked = 0
        for day in days("2015-01-01", "2027-12-31"):
            self.assertEqual((V2.label(day), V2.half(day)), (V1.label(day), V1.half(day)), day)
            checked += 1
        self.assertEqual(checked, (date(2028, 1, 1) - date(2015, 1, 1)).days)
        self.assertEqual((V2.ranges, V2.halves, V2.max_looks, V2.roles), (V1.ranges, V1.halves, V1.max_looks,
                                                                          V1.roles))

    def test_the_basic_budgets_and_what_a_registration_may_name(self):
        self.assertEqual(V2.explore_budgets, {"PRO-D": 20, "BASIC-D": 10})
        self.assertEqual(V2.alpha_lifetimes, {"PRO-V": Fraction(1, 10), "BASIC-V": Fraction(1, 10)})
        self.assertEqual({label: V2.label_problem(label) for label in V2.roles},
                         {"PRO-D": None, "PRO-V": None, "BASIC-D": None, "BASIC-V": None,
                          "BASIC-T": "role_sealed", "SEALED": "role_never"})
        self.assertEqual((V1.label_problem("BASIC-D"), V1.label_problem("BASIC-V")), ("no_budget", "no_budget"))

    def test_one_new_exposure_sentence(self):
        self.assertEqual(sorted(V2.prior_exposure),
                         ["lp-v1", "movement-v1", "p0", "p0-basic-2023-25", "suite-counts"])
        self.assertEqual(V2.prior_exposure["p0-basic-2023-25"], P0_BASIC)
        self.assertEqual({key: text for key, text in V2.prior_exposure.items() if key != "p0-basic-2023-25"},
                         V1.prior_exposure)


class LedgerAcrossSplits(LedgerCase):
    """close-v1 names the first split file and early-price-v1 the second; both write to one ledger file."""

    def close(self, kind="look", run=0, **changes):
        return self.request("close-v1", kind=kind, run=run, **changes)

    def early(self, kind="look", run=0, **changes):
        values = dict(lineage="early-price", partitions=("BASIC-D", "BASIC-V"), partitions_sha256=V2_SHA256)
        if kind == "explore":
            values.update(partitions=("BASIC-D",))
        values.update(changes)
        return self.request("early-price-v1", kind=kind, run=run, **values)

    def refused(self, code, request, split):
        before = self.path.read_bytes() if self.path.exists() else None
        ledger = self.open()
        self.assertRefused(code, ledger.check, request, split)
        self.assertRefused(code, ledger.record, request, split, sha("t"), "completed", lambda: "x")
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    def test_one_ledger_holds_both_and_neither_spends_the_others_budget(self):
        self.run_pass(self.close())
        self.run_pass(self.early(), split=V2_RAW)
        self.run_pass(self.close(run=1))
        self.run_pass(self.early(run=1), split=V2_RAW)
        self.refused("ledger:looks_spent", self.close(run=2), V1_RAW)
        self.refused("ledger:looks_spent", self.early(run=2), V2_RAW)
        self.run_pass(self.close(kind="registered"))
        self.run_pass(self.early(kind="registered"), split=V2_RAW)
        entries = verify(self.path)
        self.assertEqual([(e["study"], e["kind"], e["partitions_sha256"]) for e in entries],
                         [("close-v1", "look", V1_SHA256), ("early-price-v1", "look", V2_SHA256),
                          ("close-v1", "look", V1_SHA256), ("early-price-v1", "look", V2_SHA256),
                          ("close-v1", "registered", V1_SHA256), ("early-price-v1", "registered", V2_SHA256)])
        self.assertEqual(self.open().counts("close"), {"looks": 2, "registered": 1})
        self.assertEqual(self.open().counts("early-price"), {"looks": 2, "registered": 1})

    def test_the_explore_budget_of_basic_d_is_ten_per_lineage(self):
        self.run_pass(self.early(kind="registered"), split=V2_RAW)          # the registered pass counts as one
        for run in range(9):
            self.run_pass(self.early(kind="explore", run=run), split=V2_RAW)
        self.refused("ledger:explore_budget_spent", self.early(kind="explore", run=9), V2_RAW)
        self.run_pass(self.close(kind="explore", partitions=("PRO-D",)))    # PRO-D's budget is untouched
        self.run_pass(self.request("other-v1", kind="explore", partitions=("BASIC-D",), partitions_sha256=V2_SHA256),
                      split=V2_RAW)                                         # and so is another lineage's

    def test_a_second_early_price_test_refuses_under_any_study_id(self):
        self.run_pass(self.early(kind="registered"), split=V2_RAW)
        self.refused("ledger:lineage_already_tested", self.early(kind="registered", run=1), V2_RAW)
        self.refused("ledger:lineage_already_tested",
                     self.request("early-price-v2", kind="registered", lineage="early-price",
                                  partitions=("BASIC-D", "BASIC-V"), partitions_sha256=V2_SHA256), V2_RAW)
        self.run_pass(self.close(kind="registered"))                        # close-v1 may still test PRO-V

    def test_an_early_price_request_with_the_first_split_refuses(self):
        self.refused("ledger:entry:partition_no_budget", self.early(partitions_sha256=V1_SHA256), V1_RAW)
        self.refused("ledger:entry:partition_no_budget",
                     self.early(kind="registered", partitions_sha256=V1_SHA256), V1_RAW)
        self.refused("ledger:entry:split_not_the_requests", self.early(), V1_RAW)
        self.refused("ledger:entry:split_not_the_requests", self.close(), V2_RAW)
        self.assertFalse(self.path.exists())

    def test_alpha_on_pro_v_is_one_sum_across_the_two_files(self):
        for lineage in "abcd":                                              # 4 x 0.02 under the first file
            self.run_pass(self.request(f"{lineage}-v1", kind="registered"))
        self.run_pass(self.request("e-v1", kind="registered", partitions_sha256=V2_SHA256), split=V2_RAW)
        self.refused("ledger:alpha_spent", self.request("f-v1", kind="registered", partitions_sha256=V2_SHA256,
                                                        alpha="0.000001"), V2_RAW)
        self.refused("ledger:alpha_spent", self.request("f-v1", kind="registered", alpha="0.000001"), V1_RAW)
        self.run_pass(self.early(kind="registered", alpha="0.10"), split=V2_RAW)    # BASIC-V is a budget of its own
        self.refused("ledger:alpha_spent",
                     self.request("g-v1", kind="registered", lineage="g", partitions=("BASIC-D", "BASIC-V"),
                                  partitions_sha256=V2_SHA256, alpha="0.000001"), V2_RAW)

    def test_a_pass_typed_with_an_old_head_refuses(self):
        self.run_pass(self.close())
        old = self.head
        self.run_pass(self.early(), split=V2_RAW)                           # an early-price pass moves the head
        self.assertRefused("ledger:head_not_expected", StudyLedger.open, self.directory, head_text(old))
        self.run_pass(self.close(kind="registered"))                        # the newest head is taken


# ---------------------------------------------------------------------------------------------------------------
# The registration

REGISTRATION = REPO / "research" / "studies" / "early-price-v1.json"
# The registration's sha256 as reviewed: any change to the file must change this line too (a new registration).
REGISTRATION_SHA256 = "75519f19bc27b9e8930f235be4372d151c967e6325ecf8b9fbebc96ae60ec465"
REGISTRATION_RAW = REGISTRATION.read_bytes()
PLAN = parse_registration(REGISTRATION_RAW, V2_RAW)
TIMES = (-14400, -7200, -3600, -1800, -600)
DECIDED_BY = ("lab-Claude proposed this study on 2026-11-12 after a design round and its reviews. Yu, 2026-11-12, "
              "after lab-Claude wrote that it read Yu's message as a go to build the study as a pull request: "
              "\"go\". Yu chose none of the numbers here. Merging this registration and the split file version 2 "
              "is Yu's decision, and each pass over real data needs Yu's OK for that pass.")
PREDICTION = ("Signed by lab-Claude on 2026-11-12, before any pass: the band-A level cells at T-30 and T-10 lie "
              "within 0.5 points of 0 on BASIC-V; each primary's gross mean for its side on BASIC-V is between -0.5 "
              "and +1.5 points; the labels are kill_free_fill or unresolved, about even; about one chance in five "
              "of a signal_candidate and under one in twenty of a cost_clearing_candidate.")


def P(value):
    return exact(value)


def reg_files(change=None, split=None):
    """(registration bytes, split bytes) of the repository's files, changed: split and change are change(value).
    The registration names the changed split's sha256 before its own change runs."""
    value, split_raw = json.loads(REGISTRATION_RAW), V2_RAW
    if split is not None:
        parsed = json.loads(split_raw)
        split(parsed)
        split_raw = file_bytes(parsed)
    value["data"]["split"]["sha256"] = hashlib.sha256(split_raw).hexdigest()
    if change is not None:
        change(value)
    return file_bytes(value), split_raw


def plan_of(change=None, split=None):
    return parse_registration(*reg_files(change, split))


def setting(*path_and_value):
    """change(value) that sets value[path...] = the last argument."""
    *path, item = path_and_value

    def change(value):
        for key in path[:-1]:
            value = value[key]
        value[path[-1]] = item
    return change


class Registration(unittest.TestCase):
    def assertRefused(self, reason, change=None, raw=None, split_raw=V2_RAW):
        with self.assertRaises(RegistrationError) as caught:
            if raw is None:
                plan_of(change)
            else:
                parse_registration(raw, split_raw)
        self.assertEqual(str(caught.exception), f"registration:{reason}")

    def test_the_repository_registration_parses_and_is_named_by_its_bytes(self):
        self.assertEqual(hashlib.sha256(REGISTRATION_RAW).hexdigest(), REGISTRATION_SHA256)
        self.assertEqual(PLAN.sha256, REGISTRATION_SHA256)
        self.assertEqual(REGISTRATION_RAW, file_bytes(json.loads(REGISTRATION_RAW)))
        self.assertTrue(all(byte < 128 for byte in REGISTRATION_RAW))
        self.assertEqual((PLAN.study, PLAN.lineage, PLAN.labels, PLAN.tiers), (
            "early-price-v1", "early-price", ("BASIC-D", "BASIC-V"), ("BASIC",)))
        self.assertEqual((PLAN.months[0], PLAN.months[-1], len(PLAN.months)), ("2023-01", "2024-12", 24))
        self.assertEqual(PLAN.halves, (("H1", "2024-01-01", "2024-06-30"), ("H2", "2024-07-01", "2024-12-31")))
        self.assertEqual((PLAN.countries, PLAN.event_type_id, PLAN.market_type, PLAN.betting_type,
                          PLAN.number_of_winners, PLAN.min_active_runners), (("GB", "IE"), "7", "WIN", "ODDS", 1, 5))
        self.assertEqual((PLAN.times_s, PLAN.fresh_s), (TIMES, (3600, 3600, 1800, 900, 300)))
        self.assertEqual(PLAN.bands, (("A", Fraction(3, 2), Fraction(4)), ("B", Fraction(4), Fraction(10))))
        self.assertEqual((PLAN.starts_s, PLAN.end_s, PLAN.entry_s, PLAN.threshold),
                         ((-14400, -3600), -1800, -600, Fraction(5, 100)))
        self.assertEqual((PLAN.sides, PLAN.commission, PLAN.family_m, PLAN.alpha), (
            (BACK, LAY), Fraction(2, 100), 2, Fraction(2, 100)))
        self.assertEqual((PLAN.bsp_step, PLAN.bsp_whole_share), (Fraction(1, 100), Fraction(99, 100)))
        self.assertEqual((PLAN.min_values, PLAN.min_tested_values, PLAN.min_tested_race_dates, PLAN.min_clusters,
                          PLAN.half_year_min_values), (100, 200, 15, 15, 100))
        self.assertEqual((PLAN.confidence_level, PLAN.kill_level, PLAN.lower_bound_level, PLAN.power,
                          PLAN.detectable_level), (Fraction(95, 100), Fraction(975, 1000), Fraction(98, 100),
                                                   Fraction(8, 10), Fraction(99, 100)))
        self.assertEqual((PLAN.max_detectable, PLAN.underpowered_above), (Fraction(25, 1000), Fraction(25, 1000)))
        self.assertEqual(PLAN.half_gap_allowance, {"A": Fraction(1, 100), "B": Fraction(2, 100)})
        self.assertEqual(PLAN.quantiles, tuple(Fraction(q) for q in ("0.05", "0.1", "0.25", "0.5", "0.75", "0.9",
                                                                     "0.95")))
        self.assertEqual(len(PLAN.level_cells), 10)
        self.assertEqual(len(PLAN.move_cells), 12)
        self.assertEqual(PLAN.move_cells[:3], (early.MoveCell(-14400, DRIFT, "A"), early.MoveCell(-14400, DRIFT, "B"),
                                               early.MoveCell(-14400, FLAT, "A")))
        self.assertEqual(PLAN.test_label_move_means, "primaries_only")
        self.assertEqual([PLAN.fresh(at_s) for at_s in TIMES], [3600, 3600, 1800, 900, 300])

    def test_every_token_list_is_the_modules_own(self):
        value = json.loads(REGISTRATION_RAW)
        population, move = value["population"], value["cells"]["move"]
        self.assertEqual(tuple(population["codes"]), CODES)
        self.assertEqual(CODES[:13], movement.MARKET_CODES[:13])
        self.assertEqual(CODES[13:], ("partition_not_registered", "bsp_market_false", "market_not_open",
                                      "price_change_malformed"))
        self.assertEqual(tuple(population["runner_codes"]), RUNNER_CODES)
        self.assertEqual(RUNNER_CODES, ("no_ltp", "ltp_out_of_bands", "ltp_never_changed", "ltp_before_last_removal",
                                        "ltp_stale"))
        self.assertNotIn(VIEWED, CODES)
        self.assertNotIn(PRICED, RUNNER_CODES)
        self.assertEqual(tuple(value["measurement"]["classes"]), CLASSES)
        self.assertEqual(CLASSES, movement.CLASSES[:5] + (resting.SCORED,))
        self.assertEqual((tuple(move["names"]), tuple(move["undefined_reasons"]), tuple(move["flags"]),
                          tuple(move["changes_between"])), (early.MOVES, UNDEFINED, early.FLAGS,
                                                            early.CHANGES_BETWEEN))
        self.assertEqual(early.MOVES, ("DRIFT", "FLAT", "STEAM"))
        self.assertEqual(len(UNDEFINED), 8)
        self.assertEqual(tuple(value["verdicts"]["names"]), early.VERDICTS)
        self.assertEqual(tuple(value["diagnostics"]["no_ltp_kinds"]), NO_LTP_KINDS)
        self.assertEqual(tuple(value["measurement"]["bsp_ends"]["names"]), resting.BSP_ENDS)
        self.assertEqual(tuple(value["exposure"]["cites"]),
                         ("p0", "movement-v1", "lp-v1", "suite-counts", "p0-basic-2023-25"))
        self.assertFalse(any("edge" in name for name in early.VERDICTS))

    def test_the_words_lab_claude_signed_and_the_printed_texts(self):
        value = json.loads(REGISTRATION_RAW)
        self.assertEqual(value["decided_by"], DECIDED_BY)
        self.assertEqual(value["verdicts"]["prediction"], PREDICTION)
        self.assertEqual(value["registered_on"], "2026-11-12")
        self.assertEqual(PLAN.label_note, "A last traded price is not a price anyone could have taken. This can flag "
                                          "or kill; it cannot show an edge.")
        self.assertEqual(PLAN.exposure_note, "Before this study, nothing of 2024 had been evaluated. Its blind pass "
                                             "showed counts and spreads.")
        self.assertEqual(sorted(PLAN.label_texts), sorted(early.VERDICTS))
        self.assertTrue(PLAN.label_texts["cost_clearing_candidate"].startswith(PLAN.label_texts["signal_candidate"]))
        self.assertEqual((PLAN.label_texts["underpowered"], PLAN.label_texts["unresolved"]),
                         ("This says nothing either way.",) * 2)
        self.assertIn("0.3 points at 1.50", value["measurement"]["bsp_ends"]["limit"])
        self.assertIn("Values in markets with a later removal are not scored.", value["measurement"]["removals"])
        self.assertEqual(value["measurement"]["bsp_ends"]["switch_rows"], "level_cells_at_entry_both_labels")
        self.assertEqual(value["registered_pass"]["test_label_move_means"], "primaries_only")

    def test_it_names_the_second_split_file(self):
        self.assertEqual(split_named(REGISTRATION_RAW), ("partitions-v2.json", V2_SHA256))
        self.assertEqual(PLAN.split.sha256, V2_SHA256)
        self.assertRefused("data.split:sha256_mismatch", raw=REGISTRATION_RAW, split_raw=V1_RAW)
        value = json.loads(REGISTRATION_RAW)
        value["data"]["split"] = {"name": "partitions-v1.json", "sha256": V1_SHA256}
        self.assertRefused("data.partitions.explore:no_budget", raw=file_bytes(value), split_raw=V1_RAW)

    def test_each_check_refuses_on_its_own(self):
        def times(*items):
            return setting("timing", "times_s", list(items))

        def fresh(*items):
            return setting("timing", "fresh_s", list(items))

        def without_cite(value):
            value["exposure"]["cites"].remove("p0-basic-2023-25")

        def extra_key(value):
            value["note"] = "x"

        cases = (
            ("timing.times_s:not_sorted_and_unique", times(-14400, -3600, -7200, -1800, -600)),
            ("timing.times_s:not_sorted_and_unique", times(-14400, -7200, -7200, -1800, -600)),
            ("timing.times_s:not_an_int_in_range", times(-14400, -7200, -3600, -1800, 0)),
            ("timing.fresh_s:not_one_per_time", fresh(3600, 3600, 1800, 900)),
            ("timing.fresh_s:not_an_int_in_range", fresh(3600, 3600, 1800, 900, 0)),
            ("timing.fresh_s:windows_overlap", fresh(3600, 7300, 1800, 900, 300)),
            ("timing.fresh_s:windows_overlap", fresh(3600, 3600, 1800, 1800, 300)),
            ("cells.move:entry_fresh_not_below_the_gap", fresh(3600, 3600, 1800, 900, 1200)),
            ("cells.move:not_starts_then_end_then_entry", setting("cells", "move", "starts_s", [-14400, -1800])),
            ("cells.move:not_starts_then_end_then_entry", setting("cells", "move", "starts_s", [-3600, -14400])),
            ("cells.move:not_starts_then_end_then_entry", setting("cells", "move", "end_s", -600)),
            ("cells.move:time_not_registered", setting("cells", "move", "starts_s", [-14400, -5400])),
            ("cells.bands_h:not_contiguous", setting("cells", "bands_h", [["A", 150, 400], ["B", 410, 1000]])),
            ("cells.bands_h:name_used_twice", setting("cells", "bands_h", [["A", 150, 400], ["A", 400, 1000]])),
            ("cells.bands_h:malformed_band", setting("cells", "bands_h", [["A", 400, 150], ["B", 400, 1000]])),
            ("cells.level.times_s:not_the_registered_times", setting("cells", "level", "times_s", list(TIMES[1:]))),
            ("cells.level.count:not_times_by_bands", setting("cells", "level", "count", 12)),
            ("cells.move.count:not_starts_by_moves_by_bands", setting("cells", "move", "count", 6)),
            ("data.halves:not_the_test_labels_dates", setting("data", "halves", "H2", "last", "2024-12-30")),
            ("data.halves:not_the_test_labels_dates", setting("data", "halves", "H2", "first", "2024-07-02")),
            ("data.halves:not_the_test_labels_dates", setting("data", "halves", "H1", "first", "2023-12-31")),
            ("data.halves.H1:not_a_date", setting("data", "halves", "H1", "last", "2024-02-30")),
            ("data.halves.H2:ends_before_it_starts", setting("data", "halves", "H2", "last", "2024-06-30")),
            ("data.partitions.explore:role_not_explore", setting("data", "partitions", "explore", "BASIC-V")),
            ("data.partitions.test:role_not_holdout", setting("data", "partitions", "test", "BASIC-D")),
            ("data.partitions.test:role_sealed", setting("data", "partitions", "test", "BASIC-T")),
            ("exposure.cites:not_the_splits_prior_exposure", without_cite),
            ("family.m:not_one_primary_per_side", setting("family", "m", 1)),
            ("selection.max_detectable:not_the_underpowered_line", setting("selection", "max_detectable", "0.03")),
            ("population.codes:not_the_implemented_list",
             setting("population", "codes", list(CODES[:13]) + [CODES[14], CODES[13]] + list(CODES[15:]))),
            ("population.runner_codes:not_the_implemented_list",
             setting("population", "runner_codes", list(RUNNER_CODES) + ["priced"])),
            ("cells.move.names:not_the_implemented_list", setting("cells", "move", "names", ["STEAM", "FLAT",
                                                                                              "DRIFT"])),
            ("cells.move.undefined_reasons:not_the_implemented_list",
             setting("cells", "move", "undefined_reasons", list(UNDEFINED[::-1]))),
            ("measurement.classes:not_the_implemented_list", setting("measurement", "classes", list(CLASSES[:5]))),
            ("measurement.sides:not_the_implemented_list", setting("measurement", "sides", ["LAY", "BACK"])),
            ("verdicts.names:not_the_implemented_list",
             setting("verdicts", "names", list(early.VERDICTS[1:]) + [early.VERDICTS[0]])),
            ("verdicts.half_gap_allowance:keys_not_the_registered_set",
             setting("verdicts", "half_gap_allowance", {"A": "0.010"})),
            ("verdicts.printed_with_label:keys_not_the_registered_set",
             setting("verdicts", "printed_with_label", {"unresolved": "x"})),
            ("population.bsp_market:not_supported", setting("population", "bsp_market", False)),
            ("registered_pass.test_label_move_means:not_supported",
             setting("registered_pass", "test_label_move_means", "all_cells")),
            ("measurement.bsp_ends.switch_rows:not_supported",
             setting("measurement", "bsp_ends", "switch_rows", "every_scored_row")),
            ("data.tiers:not_supported", setting("data", "tiers", ["PRO"])),
            ("data.prices.key:not_supported", setting("data", "prices", "key", "trd")),
            ("lineage:not_supported", setting("lineage", "close")),
            ("id:not_supported", setting("id", "close-v1")),
            ("top:keys_not_the_registered_set", extra_key),
        )
        for reason, change in cases:
            with self.subTest(reason=reason):
                self.assertRefused(reason, change)

    def test_only_the_one_byte_form_is_taken(self):
        self.assertRefused("file:not_canonical", raw=REGISTRATION_RAW + b"\n")
        self.assertRefused("file:not_canonical", raw=json.dumps(json.loads(REGISTRATION_RAW)).encode("ascii"))
        self.assertRefused("file:not_json", raw=b"{")
        self.assertRefused("file:not_bytes_or_too_large", raw=REGISTRATION_RAW.decode("ascii"))


class PlanFitsTheSharedHelpers(unittest.TestCase):
    """The helpers of resting.py this study imports read only the named attributes of the plan they are given; an
    early.Plan carries those names with the same meaning."""

    def rows(self, count, days=20, label="BASIC-D", half=None, bsp=2.5, ltp=2.6):
        return [Value(at_s=-600, code=PRICED, no_ltp=None, band="A", age_ms=60000, ltp=P(ltp), label=label,
                      half=half, race_date=f"2023-06-{index % days + 1:02d}", klass=resting.SCORED, bsp=P(bsp),
                      moves=()) for index in range(count)]

    def series(self, rows, side=BACK):
        return [(row.race_date, float(resting.score(PLAN, side, row.ltp, row.bsp))) for row in rows]

    def test_the_gate_names_and_the_market_code(self):
        gate = resting._Gate(*(getattr(PLAN, name) for name in resting._Gate._fields))
        self.assertEqual(gate.min_active_runners, 5)
        self.assertTrue(all(hasattr(PLAN, name) for name in (
            "min_values", "min_tested_values", "min_tested_race_dates", "confidence_level", "min_clusters",
            "quantiles", "commission", "bsp_step", "bsp_whole_share", "kill_level", "lower_bound_level", "power",
            "detectable_level")))

    def test_score_ends_and_the_switch(self):
        self.assertEqual(resting.score(PLAN, BACK, P(2.5), P(2)), Fraction(1, 4))
        self.assertEqual(resting.score(PLAN, LAY, P(2.5), P(2)), Fraction(-1, 4))
        self.assertEqual(resting.score(PLAN, BACK, P(2.5), P(2), resting.NET_HELD), Fraction(1, 4) - Fraction(3, 200))
        self.assertEqual(resting.score(PLAN, LAY, P(2.5), P(2), resting.NET_HELD), Fraction(-1, 4) - Fraction(1, 100))
        rows = self.rows(99) + self.rows(1, bsp=2.505) + self.rows(1, label="BASIC-V", half="H1")
        rows.append(self.rows(1)[0]._replace(klass="missing_bsp", bsp=None))
        whole, of = resting.whole_hundredths(PLAN, rows)
        self.assertEqual((whole, of), (100, 101))
        self.assertTrue(resting.ends_apart(PLAN, whole, of))
        self.assertFalse(resting.ends_apart(PLAN, 98, 100))
        self.assertEqual(len(resting.scored(rows)), 101)
        self.assertEqual(len(resting.scored(rows, "BASIC-V", "H1")), 1)
        self.assertEqual(resting.deciding_end(BACK, resting.KILL_END, True), resting.AS_WRITTEN)
        self.assertEqual(resting.deciding_end(LAY, resting.KILL_END, True), resting.PLUS_STEP)

    def test_sizes_part_and_rest_and_the_statistics(self):
        self.assertTrue(resting.gate(PLAN, 200, 15))
        self.assertFalse(resting.gate(PLAN, 199, 15) or resting.gate(PLAN, 200, 14))
        self.assertEqual(resting.part_and_rest(PLAN, list(range(250)), list(range(150))), (list(range(150)), False))
        self.assertEqual(resting.part_and_rest(PLAN, list(range(249)), list(range(150))), (list(range(249)), True))
        self.assertEqual(resting.given_together(PLAN, 300, [("a", set(range(100))), ("b", set(range(150)))]),
                         ["a"])
        small = self.series(self.rows(99))
        self.assertEqual(resting.described(PLAN, small)["reason"], resting.COUNTS_ONLY)
        values = [(day, x + index / 1000) for index, (day, x) in enumerate(self.series(self.rows(200)))]
        figure = resting.described(PLAN, values)
        self.assertIsNotNone(figure["sd"])
        self.assertEqual(resting.withheld(figure)["reason"], resting.REST_NOT_GIVEN)
        tested = resting.cr1_test(PLAN, values)
        self.assertIsNotNone(tested["p_one_sided"])
        self.assertGreater(resting.bound(tested, PLAN.kill_level, 1), tested["mean"])
        self.assertLess(resting.bound(tested, PLAN.lower_bound_level, -1), tested["mean"])
        self.assertEqual(resting.gated(PLAN, values[:199])["reason"], resting.UNDERPOWERED)
        self.assertEqual(len(resting.quantile_table(PLAN, [x for _, x in values])), 7)
        self.assertEqual(sorted(resting.spread(PLAN, values)["ranges"]), ["0.75-0.25", "0.9-0.1", "0.95-0.05"])
        self.assertIsNone(resting.spread(PLAN, small)["sd"])
        self.assertEqual(resting.rounded({"x": 1 / 3}), {"x": 0.333333})


# ---------------------------------------------------------------------------------------------------------------
# Synthetic BASIC markets

OFF_D = datetime(2023, 6, 1, 14, 0, tzinfo=timezone.utc)                   # a BASIC-D race date
OFF_V = datetime(2024, 3, 1, 14, 0, tzinfo=timezone.utc)                   # a BASIC-V race date in H1
M = "1.210000001"
SIDS = (51000001, 51000002, 51000003, 51000004, 51000005, 51000006)
A1, A2, B1, B2, HIGH, LOW = SIDS
N7 = 51000007
OPENING_S = 21600
# Each runner's price at its first sight (off - 6 h) and then 60 s before each registered time.
TICK_S = (21600, 14460, 7260, 3660, 1860, 660)
PATHS = {A1: (2.0, 2.1, 2.2, 2.3, 2.4, 2.5), A2: (3.0, 3.5, 3.2, 3.0, 2.8, 2.9), B1: (5.0, 5.2, 5.4, 5.6, 6.0, 6.2),
         B2: (8.0, 9.0, 8.8, 8.6, 8.4, 8.2), HIGH: (20, 21, 22, 23, 24, 25),
         LOW: (1.2, 1.25, 1.3, 1.35, 1.4, 1.45)}
BSPS = {A1: 2.52, A2: 3.1, B1: 6.4, B2: 8.0, HIGH: 30.0, LOW: 1.5, N7: 12.0}


def t_of(at_s, off=OFF_D):
    return ms(off) + at_s * 1000


def ticks_of(paths=PATHS, seconds=TICK_S):
    """[(ms before the off, {runner: price})]: one tick per time in seconds; a None price skips that runner."""
    return [(before * 1000, {rid: path[index] for rid, path in paths.items() if path[index] is not None})
            for index, before in enumerate(seconds)]


def basic_market(market_id=M, off=OFF_D, ticks=None, runners=SIDS, bsps=None, statuses=None, extra=(),
                 opening_s=OPENING_S, tail=(), **fields):
    """A BASIC market's messages: its definition at off - opening_s with fields changed, an ltp tick per entry of
    ticks [(ms before the off, {runner: price})] (default: PATHS), extra messages in pt order, then BSP reconciled
    and CLOSED after the off (the first runner wins unless statuses says otherwise), then tail."""
    start = ms(off)
    messages = [md(market_id, start - opening_s * 1000, definition(off, [runner(s) for s in runners], **fields))]
    messages += [prices(market_id, start - before, ltps) for before, ltps in (ticks_of() if ticks is None else ticks)
                 if ltps]
    messages = sorted(messages + list(extra), key=lambda message: message["pt"])
    statuses = statuses or {sid: "WINNER" if sid == runners[0] else "LOSER" for sid in runners}
    bsps = {**BSPS, **(bsps or {})}
    return messages + settle(market_id, off, statuses, {sid: bsps[sid] for sid in statuses}) + list(tail)


def status_change(at_ms, status, off=OFF_D, runners=SIDS, in_play=False, market_id=M):
    """A definition at pt at_ms that sets the market's status (and inPlay)."""
    return md(market_id, at_ms, definition(off, [runner(s) for s in runners], status=status, in_play=in_play))


def removal(sid, at_ms, off=OFF_D, runners=SIDS, market_id=M):
    """A definition at pt at_ms with runner sid REMOVED."""
    return md(market_id, at_ms, definition(off, [runner(s, "REMOVED" if s == sid else "ACTIVE") for s in runners]))


def statuses_with(**changes):
    """The default final statuses (A1 wins) with {name: status} changes for named runners."""
    named = {"A1": A1, "A2": A2, "B1": B1, "B2": B2, "HIGH": HIGH, "LOW": LOW}
    found = {sid: "WINNER" if sid == A1 else "LOSER" for sid in SIDS}
    found.update({named[name]: status for name, status in changes.items()})
    return found


def member_stream(root, manifest, record):
    return bz2.open(safe_target(root, manifest["archive"]["sha256"], _member(manifest, record)["path"]), "rb")


class Unread:
    """A stream that must not be read."""

    def readline(self, size):
        raise AssertionError("the member was read")


class EarlyCase(fx.Workspace):
    def record(self, messages, market_id=M, when=OFF_D, plan="BASIC"):
        self.root, self.manifest, records = self.build([(fx.path(market_id, when, plan=plan), messages)])
        return records[market_id]

    def assessed(self, messages, plan=PLAN, **kwargs):
        record = self.record(messages, **kwargs)
        with member_stream(self.root, self.manifest, record) as stream:
            return assess(stream, record, plan, DEFAULT_LIMITS)

    def runners(self, assessed, at_s, sids=SIDS):
        """{runner: Value} at one time: the rows of a time come in runner id order (the active runners, sorted)."""
        rows = [value for value in assessed.values if value.at_s == at_s]
        self.assertEqual(len(rows), len(sids))
        return dict(zip(sids, rows))


# ---------------------------------------------------------------------------------------------------------------
# The fold and the prices

class OwnFoldMatchesMovement(EarlyCase):
    def both(self, messages, until_s=-600):
        record = self.record(messages)
        until = t_of(until_s)
        with member_stream(self.root, self.manifest, record) as stream:
            ours, kept = fold(stream, record, TIMES, until, DEFAULT_LIMITS)
        with member_stream(self.root, self.manifest, record) as stream:
            theirs = replay_market(stream, record, ("atb", "atl"), TIMES, until, DEFAULT_LIMITS)
        return record, ours, kept, theirs

    def test_the_views_are_movements_at_all_five_times(self):
        image = md(M, t_of(-650), definition(OFF_D, [runner(s) for s in SIDS]), img=True,
                   rc=[{"id": sid, "ltp": PATHS[sid][4]} for sid in SIDS if sid != HIGH])
        for name, messages in (("standard", basic_market()), ("image", basic_market(extra=[image])),
                               ("removal", basic_market(extra=[removal(HIGH, t_of(-5000))],
                                                        statuses=statuses_with(HIGH="REMOVED")))):
            with self.subTest(market=name):
                _, ours, kept, theirs = self.both(messages)
                self.assertEqual(set(ours.moments), set(TIMES))
                for at_s in TIMES:                                          # BASIC has no ladder: movement's quotes
                    mine, its = ours.moments[at_s], theirs.moments[at_s]    # are (id, None, None) and ours are ()
                    self.assertEqual(mine, replace(its, quotes=()))
                    self.assertEqual({(back, lay) for _, back, lay in its.quotes}, {(None, None)} if its.quotes
                                     else set())
                    self.assertIsNotNone(mine.view)
                self.assertEqual((ours.unreadable_from, ours.market.fields, ours.market.runners),
                                 (theirs.unreadable_from, theirs.market.fields, theirs.market.runners))
                self.assertEqual(ours.market.removals(), theirs.market.removals())
                self.assertIsInstance(kept, early.PriceFold)
                if name == "image":
                    self.assertEqual(kept.ltp[HIGH][-1], [t_of(-650), None])     # the image reset HIGH's price

    def test_nothing_after_until_is_applied(self):
        messages = basic_market(extra=[prices(M, t_of(-300), {A1: 2.7})])
        at = next(index for index, message in enumerate(messages) if message["pt"] == t_of(-300))
        messages.insert(at + 1, b"{not json\n")                            # never read: the fold stops before it
        _, ours, kept, _ = self.both(messages)
        self.assertIsNone(ours.unreadable_from)
        self.assertLessEqual(max(pt for timeline in kept.ltp.values() for pt, _ in timeline), t_of(-600))
        self.assertLessEqual(ours.market.last_pt, t_of(-600))
        self.assertEqual(kept.ltp[A1][-1], [t_of(-660), 2.5])

    def test_a_tampered_index_record_refuses(self):
        record = self.record(basic_market())
        tampered = copy.deepcopy(record)
        winners = tampered["decision_time"]["definition"]["numberOfWinners"]
        winners[0][1] = 2
        with member_stream(self.root, self.manifest, record) as stream, self.assertRaises(ReplayProblem) as caught:
            fold(stream, tampered, TIMES, t_of(-600), DEFAULT_LIMITS)
        self.assertEqual(str(caught.exception), f"replay:{M}:index_mismatch")


class LtpAsOf(EarlyCase):
    def test_the_batch_at_t_is_seen_and_one_ms_later_is_not(self):
        extra = [prices(M, t_of(-600), {A1: 2.6}), prices(M, t_of(-600), {A1: 2.7}),
                 prices(M, t_of(-600) + 1, {A2: 3.3})]
        found = self.runners(self.assessed(basic_market(extra=extra)), -600)
        self.assertEqual((found[A1].ltp, found[A1].age_ms, found[A1].code), (P(2.7), 0, PRICED))
        self.assertEqual((found[A2].ltp, found[A2].age_ms), (P(2.9), 600000 - 540000))

    def test_what_counts_as_a_change(self):
        timeline = [[1, 5], [2, 5.0], [3, None], [4, 5], [5, 0], [6, 5], [7, 6], [8, None], [9, 7], [10, True],
                    [11, "8"], [12, 7.0]]
        self.assertEqual(changes(timeline, 12), [7, 9])
        self.assertEqual(changes(timeline, 8), [7])
        self.assertEqual(changes(timeline, 6), [])
        self.assertEqual(changes([[1, 5]], 1), [])                          # a first sight is not a change
        self.assertEqual(changes([[1, 5], [2, None], [3, 6]], 3), [3])      # a reset then a new price is one

    def test_first_sight_a_reset_and_5_then_5_0_are_no_change(self):
        runners = SIDS + (N7,)
        reset = md(M, t_of(-650), definition(OFF_D, [runner(s) for s in runners]), img=True,
                   rc=[{"id": A1, "ltp": 2.0}, {"id": A2, "ltp": 3.0}, {"id": B1, "ltp": 5.2}, {"id": B2, "ltp": 8.0},
                       {"id": LOW, "ltp": 0}])
        ticks = [(21600_000, {A1: 2.0, A2: 3, B1: 5.0, HIGH: 20.0}), (1000_000, {A1: None, B1: None}),
                 (700_000, {A1: 2.0, A2: 3.0, B1: 5.2, B2: 8.0, LOW: 0})]
        found = self.runners(self.assessed(basic_market(ticks=ticks, runners=runners, extra=[reset])), -600,
                             sorted(runners))
        self.assertEqual({sid: (found[sid].code, found[sid].no_ltp) for sid in runners}, {
            A1: ("ltp_never_changed", None),                                # a reset, then the same price
            A2: ("ltp_never_changed", None),                                # 3, then 3.0
            B1: (PRICED, None),                                             # a reset, then a new price
            B2: ("ltp_never_changed", None),                                # first sight
            HIGH: ("no_ltp", "reset_not_sent_again"),                       # the image left it out
            LOW: ("no_ltp", "not_a_usable_number"),
            N7: ("no_ltp", "never_sent")})
        self.assertEqual((found[B1].age_ms, found[B1].ltp), (100_000, P(5.2)))
        self.assertEqual({found[sid].ltp for sid in (HIGH, LOW, N7)}, {None})

    def test_a_malformed_runner_id_codes_the_market(self):
        bad = mcm(t_of(-5000), [{"id": M, "rc": [{"id": "x", "ltp": 2.0}]}])
        found = self.assessed(basic_market(extra=[bad]))
        self.assertEqual(found.codes, {-14400: VIEWED, -7200: VIEWED, -3600: "price_change_malformed",
                                       -1800: "price_change_malformed", -600: "price_change_malformed"})
        self.assertEqual({value.at_s for value in found.values}, {-14400, -7200})


class Freshness(EarlyCase):
    def test_an_age_of_exactly_fresh_s_is_fresh_and_one_ms_more_is_stale(self):
        others = ticks_of({sid: PATHS[sid] for sid in (B1, B2, HIGH, LOW)})
        ticks = others + [(21600_000, {A1: 2.0, A2: 3.0})]
        for index, (at_s, fresh) in enumerate(zip(TIMES, PLAN.fresh_s)):
            before = (fresh - at_s) * 1000
            ticks += [(before, {A1: (2.1, 2.2, 2.3, 2.4, 2.5)[index]}), (before + 1, {A2: (3.1, 3.2, 3.3, 3.4,
                                                                                         3.5)[index]})]
        found = self.assessed(basic_market(ticks=ticks))
        for at_s, fresh in zip(TIMES, PLAN.fresh_s):
            with self.subTest(at_s=at_s):
                row = self.runners(found, at_s)
                self.assertEqual((row[A1].code, row[A1].age_ms), (PRICED, fresh * 1000))
                self.assertEqual((row[A2].code, row[A2].age_ms), ("ltp_stale", fresh * 1000 + 1))

    def test_a_price_from_before_the_last_removal_is_refused_however_fresh(self):
        extra = [removal(HIGH, t_of(-630)), prices(M, t_of(-620), {B1: 6.3})]
        found = self.assessed(basic_market(extra=extra, statuses=statuses_with(HIGH="REMOVED")))
        row = self.runners(found, -600, (A1, A2, B1, B2, LOW))
        self.assertEqual((row[A1].code, row[A1].age_ms, row[A1].band), ("ltp_before_last_removal", 60_000, "A"))
        self.assertEqual((row[B1].code, row[B1].age_ms), (PRICED, 20_000))
        self.assertIsNone(row[A1].klass)
        self.assertEqual(self.runners(found, -1800)[A1].code, PRICED)        # the removal came later


class Bands(EarlyCase):
    def test_where_each_price_falls(self):
        for price, name in ((1.49, None), (1.5, "A"), (3.95, "A"), (3.99, "A"), (4.0, "B"), (9.8, "B"),
                            (10.0, None), (10.5, None)):
            with self.subTest(price=price):
                self.assertEqual(PLAN.band(P(price)), name)

    def test_the_runner_codes_read_the_band(self):
        ticks = ticks_of()[:-1] + [(660_000, {A1: 1.5, A2: 3.95, B1: 4.0, B2: 9.8, HIGH: 10.0, LOW: 1.49})]
        found = self.runners(self.assessed(basic_market(ticks=ticks)), -600)
        self.assertEqual({sid: (found[sid].code, found[sid].band) for sid in SIDS}, {
            A1: (PRICED, "A"), A2: (PRICED, "A"), B1: (PRICED, "B"), B2: (PRICED, "B"),
            HIGH: ("ltp_out_of_bands", None), LOW: ("ltp_out_of_bands", None)})


# ---------------------------------------------------------------------------------------------------------------
# Cells

def moves_of(row):
    """{start: cell or reason} of one entry member, and its flags and k."""
    return {item.start_s: item.cell or item.reason for item in row.moves}


class MoveCells(EarlyCase):
    def test_price_up_is_drift_and_both_thresholds_are_inside(self):
        self.assertEqual([move_cell(PLAN, m) for m in (0.05, 0.3, -0.05, -0.3, 0.0499999, -0.0499999, 0.0)],
                         [DRIFT, DRIFT, STEAM, STEAM, FLAT, FLAT, FLAT])
        share_rule = SimpleNamespace(move_threshold=Fraction(5, 100))
        self.assertEqual(movement.late_move_cell(share_rule, 0.05), movement.STEAM)    # a share up is a steam

    def test_the_standard_markets_cells(self):
        found = self.runners(self.assessed(basic_market()), -600)
        self.assertEqual({sid: moves_of(found[sid]) for sid in SIDS}, {
            A1: {-14400: DRIFT, -3600: FLAT}, A2: {-14400: STEAM, -3600: STEAM}, B1: {-14400: DRIFT, -3600: DRIFT},
            B2: {-14400: STEAM, -3600: FLAT}, HIGH: {}, LOW: {}})
        self.assertEqual({(item.flagged, item.k) for sid in (A1, A2, B1, B2) for item in found[sid].moves},
                         {(False, 1)})
        self.assertEqual([found[sid].band for sid in (A1, A2, B1, B2)], ["A", "A", "B", "B"])
        early_rows = [value for value in self.assessed(basic_market()).values if value.at_s != -600]
        self.assertEqual({value.moves for value in early_rows}, {()})        # only the entry time has moves

    def test_each_undefined_reason_in_order(self):
        # A1 changes at off - 19000 s and not again before off - 7260 s: stale at off - 4 h.
        stale_start = [(before, {sid: price for sid, price in ltps.items() if (sid, before) != (A1, 14460_000)})
                       for before, ltps in ticks_of()] + [(19000_000, {A1: 2.05})]
        cases = {
            "removal_in_window": (dict(extra=[removal(HIGH, t_of(-5000))], statuses=statuses_with(HIGH="REMOVED")),
                                  (A1, A2, B1, B2, LOW), {-14400: "removal_in_window", -3600: FLAT}),
            "removal_first": (dict(ticks=stale_start, extra=[removal(HIGH, t_of(-5000))],
                                   statuses=statuses_with(HIGH="REMOVED")),
                              (A1, A2, B1, B2, LOW), {-14400: "removal_in_window", -3600: FLAT}),
            "no_view_at_start": (dict(opening_s=10000, ticks=[(10000_000, {s: p[0] for s, p in PATHS.items()})]
                                      + ticks_of(PATHS)[2:]), SIDS, {-14400: "no_view_at_start", -3600: FLAT}),
            "no_start_price": (dict(ticks=[(21600_000, {s: p[0] for s, p in PATHS.items() if s != A1})]
                                    + ticks_of(PATHS)[1:]), SIDS, {-14400: "no_start_price", -3600: FLAT}),
            "start_price_stale": (dict(ticks=stale_start), SIDS, {-14400: "start_price_stale", -3600: FLAT}),
            "no_view_at_end": (dict(extra=[status_change(t_of(-1900), "SUSPENDED"), status_change(t_of(-1700),
                                                                                                 "OPEN")]),
                               SIDS, {-14400: "no_view_at_end", -3600: "no_view_at_end"}),
            "no_end_price": (dict(extra=[prices(M, t_of(-1850), {A1: None})]), SIDS,
                             {-14400: "no_end_price", -3600: "no_end_price"}),
            "end_price_stale": (dict(ticks=ticks_of({**PATHS, A1: (2.0, 2.1, 2.2, 2.3, None, 2.5)})), SIDS,
                                {-14400: "end_price_stale", -3600: "end_price_stale"}),
        }
        for name, (given, sids, expected) in cases.items():
            with self.subTest(case=name):
                found = self.runners(self.assessed(basic_market(**given)), -600, sids)
                self.assertEqual(found[A1].code, PRICED)
                self.assertEqual(moves_of(found[A1]), expected)
                self.assertEqual({item.k for item in found[A1].moves if item.reason is not None}, {None})
        self.assertEqual(set(cases) - {"removal_first"} | {"window_unknown"}, set(UNDEFINED))   # the next test

    def test_window_unknown_from_a_hand_built_replay(self):
        record = self.record(basic_market())
        with member_stream(self.root, self.manifest, record) as stream:
            replay, kept = fold(stream, record, TIMES, t_of(-600), DEFAULT_LIMITS)
        codes = dict.fromkeys(TIMES, VIEWED)
        self.assertEqual(move_of(PLAN, record, replay, codes, kept.ltp[A1], A1, -3600).cell, FLAT)
        cut = replace(replay, unreadable_from=t_of(-600))
        self.assertEqual(move_of(PLAN, record, cut, codes, kept.ltp[A1], A1, -3600),
                         early.Move(-3600, None, "window_unknown", False, None))

    def test_reasons_and_cells_add_up_to_the_level_cells_members(self):
        found = self.assessed(basic_market(ticks=ticks_of({**PATHS, A1: (2.0, 2.1, 2.2, 2.3, None, 2.5)})))
        members = [value for value in found.values if value.at_s == -600 and value.code == PRICED]
        for band in ("A", "B"):
            in_band = [value for value in members if value.band == band]
            for start in PLAN.starts_s:
                cells = [item.cell for value in in_band for item in value.moves if item.start_s == start and item.cell]
                reasons = [item.reason for value in in_band for item in value.moves
                           if item.start_s == start and item.reason]
                self.assertEqual(len(cells) + len(reasons), len(in_band))
                self.assertTrue(all((item.cell is None) != (item.reason is None) for value in in_band
                                    for item in value.moves))
        self.assertEqual(sum(1 for value in found.values if value.at_s == -600 and value.moves), 4)

    def test_a_suspension_flags_and_keeps_the_move(self):
        extra = [status_change(t_of(-3000), "SUSPENDED"), status_change(t_of(-2950), "OPEN")]
        found = self.runners(self.assessed(basic_market(extra=extra)), -600)
        standard = self.runners(self.assessed(basic_market()), -600)
        for sid in (A1, A2, B1, B2):
            self.assertEqual(moves_of(found[sid]), moves_of(standard[sid]))
            self.assertEqual({item.flagged for item in found[sid].moves}, {True})
        self.assertEqual({item.flagged for item in standard[A1].moves}, {False})

    def test_k_counts_the_changes_between_the_end_and_the_entry(self):
        extra = [prices(M, t_of(-1200), {A1: 2.45}), prices(M, t_of(-900), {A1: 2.42, A2: 2.85}),
                 prices(M, t_of(-800), {B2: 8.4})]                         # B2's -800 price is its -1860 one again
        found = self.runners(self.assessed(basic_market(extra=extra)), -600)
        self.assertEqual({sid: {item.k for item in found[sid].moves} for sid in (A1, A2, B1, B2)},
                         {A1: {3}, A2: {2}, B1: {1}, B2: {1}})
        self.assertEqual([changes_between(k) for k in (1, 2, 3, 7)], ["1", "2", "3_or_more", "3_or_more"])

    def test_one_price_spanning_t30_and_t10_is_in_no_move_cell(self):
        found = self.runners(self.assessed(basic_market(ticks=ticks_of({**PATHS, A1: (2.0, 2.1, 2.2, 2.3, 2.4,
                                                                                      None)}))), -600)
        self.assertEqual((found[A1].code, found[A1].age_ms, found[A1].moves), ("ltp_stale", 1_260_000, ()))


class NoFieldAfterT(EarlyCase):
    def test_changing_every_message_after_t_moves_only_the_class_and_the_bsp(self):
        base = self.assessed(basic_market())
        for at_s in TIMES:
            with self.subTest(at_s=at_s):
                ticks = [(before, ltps if before >= -at_s * 1000 else {sid: round(price * 1.1, 2) for sid, price
                                                                       in ltps.items()})
                         for before, ltps in ticks_of()]
                ticks.append((-at_s * 1000 - 1, {sid: 7.0 for sid in SIDS}))
                changed = basic_market(ticks=ticks, extra=[removal(LOW, t_of(at_s) + 1000)],
                                       bsps={sid: price + 0.5 for sid, price in BSPS.items()},
                                       statuses=statuses_with(LOW="REMOVED", A1="LOSER", A2="WINNER"))
                after = self.assessed(changed)

                def upto(found):
                    return ({key: code for key, code in found.codes.items() if key <= at_s},
                            [value._replace(klass=None, bsp=None) for value in found.values if value.at_s <= at_s])

                self.assertEqual(upto(after), upto(base))
                classes = {value.klass for value in after.values if value.at_s == at_s and value.code == PRICED}
                self.assertEqual(classes, {"post_decision_removal"})        # the class did move


class PopulationCodes(EarlyCase):
    def test_the_standard_market(self):
        found = self.assessed(basic_market())
        self.assertEqual((found.codes, found.bsp_unknown, found.replayed),
                         (dict.fromkeys(TIMES, VIEWED), dict.fromkeys(TIMES, False), True))
        for at_s in TIMES:
            row = self.runners(found, at_s)
            self.assertEqual([row[sid].code for sid in SIDS], [PRICED] * 4 + ["ltp_out_of_bands"] * 2)
        self.assertEqual({(value.label, value.half, value.race_date) for value in found.values},
                         {("BASIC-D", None, "2023-06-01")})
        for off, half in ((OFF_V, "H1"), (datetime(2024, 9, 2, 14, 0, tzinfo=timezone.utc), "H2")):
            found = self.assessed(basic_market(off=off), when=off)
            self.assertEqual({(value.label, value.half) for value in found.values}, {("BASIC-V", half)})
        self.assertEqual(Value._fields, ("at_s", "code", "no_ltp", "band", "age_ms", "ltp", "label", "half",
                                         "race_date", "klass", "bsp", "moves"))
        self.assertEqual(early.Move._fields, ("start_s", "cell", "reason", "flagged", "k"))

    def test_a_row_holds_no_id(self):
        text = repr(self.assessed(basic_market()))
        for token in (M, M.split(".")[1], fx.EVENT, *map(str, SIDS), "Synthetic", "Horse"):
            self.assertNotIn(token, text)

    def test_the_codes_of_the_record_alone_never_open_the_member(self):
        winter = datetime(2022, 12, 31, 12, 0, tzinfo=timezone.utc)
        late = datetime(2025, 3, 1, 14, 0, tzinfo=timezone.utc)
        no_definition = [message for message in basic_market()
                         if not any("marketDefinition" in change for change in message["mc"])]
        revised = md(M, t_of(-20000), definition(OFF_D + timedelta(minutes=5), [runner(s) for s in SIDS]))
        cases = {
            "plan_not_registered": dict(messages=basic_market(), plan="PRO"),
            "no_view": dict(messages=no_definition),
            "month_not_registered": dict(messages=basic_market(off=late), when=late),
            "partition_not_registered": dict(messages=basic_market(off=winter), when=winter,
                                             registration=plan_of(setting("data", "start_month", "2022-12"))),
            "schedule_revised_before_decision": dict(messages=basic_market(extra=[revised])),
            "country_not_allowed": dict(messages=basic_market(countryCode="FR")),
            "event_type_not_allowed": dict(messages=basic_market(eventTypeId="4339")),
            "market_type_not_allowed": dict(messages=basic_market(marketType="PLACE")),
            "betting_type_not_odds": dict(messages=basic_market(bettingType="ASIAN_HANDICAP_DOUBLE_LINE")),
            "number_of_winners_not_1": dict(messages=basic_market(numberOfWinners=2)),
            "too_few_active_runners": dict(messages=basic_market(runners=SIDS[:4])),
            "bsp_market_false": dict(messages=basic_market(bspMarket=False)),
            "market_not_open": dict(messages=basic_market(status="SUSPENDED")),
        }
        self.assertEqual(sorted(cases), sorted(code for code in CODES if code not in (
            "anchor_not_known_by_decision", "view_ended_before_decision", "member_unreadable_before_decision",
            "price_change_malformed")))
        for code, given in cases.items():
            with self.subTest(code=code):
                plan = given.get("registration", PLAN)
                record = self.record(given["messages"], when=given.get("when", OFF_D), plan=given.get("plan",
                                                                                                    "BASIC"))
                found = assess(Unread(), record, plan, DEFAULT_LIMITS)
                self.assertEqual((found.codes, found.values, found.replayed), (dict.fromkeys(TIMES, code), (), False))
                self.assertEqual({record_code(plan, record, at_s) for at_s in TIMES}, {(code, False)})
                self.assertIn(code, early.RECORD_CODES)

    def test_codes_at_some_times_only(self):
        unreadable = basic_market(extra=[prices(M, t_of(-1000), {A1: 2.45})])
        at = next(index for index, message in enumerate(unreadable) if message["pt"] == t_of(-1000))
        unreadable.insert(at + 1, b"{not json\n")
        cases = {
            "anchor_not_known_by_decision": (dict(opening_s=10000, ticks=[(10000_000, {s: p[0] for s, p in
                                                                                        PATHS.items()})]
                                                  + ticks_of()[2:]), {-14400}),
            "view_ended_before_decision": (dict(extra=[md(M, t_of(-1000), definition(
                OFF_D, [runner(s) for s in SIDS], bspReconciled=True))]), {-600}),
            "market_not_open": (dict(extra=[status_change(t_of(-1900), "SUSPENDED"),
                                            status_change(t_of(-1700), "OPEN"),
                                            status_change(t_of(-700), "OPEN", in_play=True)]), {-1800, -600}),
        }
        for code, (given, coded) in cases.items():
            with self.subTest(code=code):
                found = self.assessed(basic_market(**given))
                self.assertEqual(found.codes, {at_s: code if at_s in coded else VIEWED for at_s in TIMES})
                self.assertEqual({value.at_s for value in found.values}, set(TIMES) - coded)
        found = self.assessed(unreadable)
        self.assertEqual(found.codes, {**dict.fromkeys(TIMES, VIEWED), -600: "member_unreadable_before_decision"})

    def test_a_missing_bsp_market_flag_passes_and_is_reported(self):
        for flag in (fx.DROP, None, "false"):
            with self.subTest(flag=flag):
                found = self.assessed(basic_market(bspMarket=flag))
                self.assertEqual((found.codes, found.bsp_unknown), (dict.fromkeys(TIMES, VIEWED),
                                                                    dict.fromkeys(TIMES, True)))

    def test_codes_add_up_over_records(self):
        found = [self.assessed(messages) for messages in (
            basic_market(), basic_market(countryCode="FR"),
            basic_market(opening_s=10000, ticks=[(10000_000, {A1: 2.0})]),
            basic_market(extra=[status_change(t_of(-1900), "SUSPENDED"), status_change(t_of(-1700), "OPEN")]))]
        for at_s in TIMES:
            counted = {}
            for item in found:
                counted[item.codes[at_s]] = counted.get(item.codes[at_s], 0) + 1
            self.assertEqual(sum(counted.values()), len(found))
            self.assertTrue(set(counted) <= set(CODES) | {VIEWED})

    def test_the_first_runner_code_wins(self):
        ticks = [(21600_000, {s: p[0] for s, p in PATHS.items() if s not in (HIGH, LOW)})] + [
            (before, {s: p for s, p in ltps.items() if s not in (HIGH, LOW)}) for before, ltps in ticks_of()[1:]]
        ticks.append((660_000, {HIGH: 20.0, LOW: 0}))                     # out of bands and first sight; unusable
        found = self.runners(self.assessed(basic_market(ticks=ticks)), -600)
        self.assertEqual((found[HIGH].code, found[LOW].code, found[LOW].no_ltp),
                         ("ltp_out_of_bands", "no_ltp", "not_a_usable_number"))


class Classes(EarlyCase):
    def classes(self, messages, at_s=-600, sids=SIDS):
        found = self.runners(self.assessed(messages), at_s, sids)
        return {sid: found[sid].klass for sid in sids if found[sid].code == PRICED}, found

    def test_each_class(self):
        settled = settle(M, OFF_D, statuses_with(A1="LOSER", A2="WINNER"), BSPS)[-1]
        settled["pt"] += 120_000                                            # a result revised after CLOSED
        cases = {
            "scored": (basic_market(), {A1: "scored", A2: "scored", B1: "scored", B2: "scored"}),
            "outcome_unknown": (basic_market(tail=[b"{not json\n"]), dict.fromkeys((A1, A2, B1, B2),
                                                                                   "outcome_unknown")),
            "void_own_runner_removed": (basic_market(extra=[removal(A1, t_of(-300))],
                                                     statuses=statuses_with(A1="REMOVED", A2="WINNER")),
                                        {A1: "void_own_runner_removed", A2: "post_decision_removal",
                                         B1: "post_decision_removal", B2: "post_decision_removal"}),
            "result_revised_after_close": (basic_market(tail=[settled]),
                                           dict.fromkeys((A1, A2, B1, B2), "result_revised_after_close")),
            "missing_bsp": (basic_market(bsps={A2: None}), {A1: "scored", A2: "missing_bsp", B1: "scored",
                                                            B2: "scored"}),
        }
        for name, (messages, expected) in cases.items():
            with self.subTest(case=name):
                classes, found = self.classes(messages)
                self.assertEqual(classes, expected)
                self.assertEqual({found[sid].klass for sid in (HIGH, LOW)}, {None})
                for sid, klass in expected.items():
                    self.assertEqual(found[sid].bsp, P(BSPS[sid]) if klass == "scored" else None)
        self.assertEqual(sorted(list(cases) + ["post_decision_removal"]), sorted(CLASSES))


if __name__ == "__main__":
    unittest.main()
