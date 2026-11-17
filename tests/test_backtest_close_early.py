"""close-early-v1's frozen files: the registration research/studies/close-early-v1.json, its two rule files
research/rules/close-early-v1-fav.json and close-early-v1-band.json, and split file version 3
(research/partitions-v3.json).

The four files are read from the repository and checked with the repository's own parsers (rulefile.parse_rule,
partition.parse_split, resting.parse_registration). Every ledger lives in a temporary folder that the test removes,
and its entries carry made-up hashes. Every market is synthetic, built with the fixture helpers of
tests/test_backtest_lp.py and tests/test_backtest_resting.py, and goes through the real ingest, index and replay; the
pass runner (resting_study) runs the real registration, unchanged, over such markets. These tests show that the
files are the reviewed ones in their one byte form, that version 3 gives every date the label and half the first
two give, that the registration parses with version 3 and its rule files only, that what it does not change is
close-v1's, what the ledger takes of this study, and that a blind look and an explore pass under the real gates open
explore-date files only and give counts alone on a small synthetic root. They say nothing about real Betfair files or
results.
"""

import copy
from datetime import date
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from betangel_research.betfair.backtest import partition, rulefile
from betangel_research.betfair.backtest.movement import MARKET_CODES, RegistrationError
from betangel_research.betfair.backtest.partition import parse_split
from betangel_research.betfair.backtest.resting import (EARLY_IDS, EARLY_TRD_COUNTS, NULL_LABEL, STATISTICS, Cell,
                                                        parse_registration, population_codes)
from betangel_research.betfair.backtest.rulefile import Market, Template, parse_rule
from betangel_research.betfair.backtest.study_ledger import LEDGER_FILE, verify

import test_backtest_lane as lane
import test_backtest_resting as rt
from test_backtest_early import V2_SHA256
from test_backtest_lp import BOOK, FAV, R2, lp_market, path

REPO = Path(__file__).resolve().parent.parent
RESEARCH = REPO / "research"
STUDY = EARLY_IDS[0]
FAV_RULE, BAND_RULE = "close-early-v1-fav", "close-early-v1-band"
# Each new file's sha256 as reviewed: any change to a file must change its line here too (a new file).
V3_SHA256 = "fd35425aab3717099f0be2ce497505ec57fc37549cd24d470e3d6961ff792b65"
FAV_SHA256 = "95684bd115997662782c8968363b52b51e76c382e12b2495d5e5d4567c46549b"
BAND_SHA256 = "0ef42617d1e8fa5465b23f46e69008058e58a7bc7de2af95f3e6b4f77944e7fb"
REGISTRATION_SHA256 = "45a180b9d86fb3d16b0e21fdfc26663bcb0ee49239bb5e8ec4e8a5b60738a299"
PINNED = {RESEARCH / "partitions-v3.json": V3_SHA256, RESEARCH / "rules" / f"{FAV_RULE}.json": FAV_SHA256,
          RESEARCH / "rules" / f"{BAND_RULE}.json": BAND_SHA256,
          RESEARCH / "studies" / f"{STUDY}.json": REGISTRATION_SHA256}

V1_RAW, V2_RAW = rt.SPLIT_RAW, (RESEARCH / "partitions-v2.json").read_bytes()
V3_RAW = (RESEARCH / "partitions-v3.json").read_bytes()
V1, V2, V3 = parse_split(V1_RAW), parse_split(V2_RAW), parse_split(V3_RAW)
RULES_RAW = {f"{name}.json": (RESEARCH / "rules" / f"{name}.json").read_bytes() for name in (FAV_RULE, BAND_RULE)}
RULE_FAV, RULE_BAND = (parse_rule(RULES_RAW[f"{name}.json"]) for name in (FAV_RULE, BAND_RULE))
REGISTRATION_RAW = (RESEARCH / "studies" / f"{STUDY}.json").read_bytes()
PLAN = parse_registration(REGISTRATION_RAW, V3_RAW, RULES_RAW)
VALUE, CLOSE = json.loads(REGISTRATION_RAW), json.loads(rt.REGISTRATION.read_bytes())
LAY_RAW = rt.RULES_RAW["close-v1-lay.json"]
TIMES = (-14400, -7200, -3600, -1800)
PULL = -600
# The codes the two rule files give (the spec's section 10.2), in evaluation order.
CODES = MARKET_CODES[:12] + (
    "partition_not_registered", "bsp_market_false", "ladder_malformed", "market_not_open", "too_few_active_runners",
    "no_priced_active_runner", "favourite_tie", "best_back_out_of_range", "depth_below_min", "no_best_lay",
    "gap_not_1_to_20_ticks", "no_active_runner", "no_best_back", "no_reference_price", "price_off_ladder")
# The three sentences version 3 adds to version 2's prior_exposure.
ADDED = ("close-v1", "early-price-v1", "p0-basic-2015-16")
# The fields of the registration the spec's section 10.2 shows, as key paths; every other field is close-v1's.
SHOWN = (("id",), ("registered_on",), ("summary",), ("decided_by",), ("not_measured",), ("blind_pass",),
         ("registered_pass",), ("data", "partitions"), ("data", "rules"), ("data", "split"),
         ("data", "traded_volume"), ("diagnostics",), ("exposure",), ("family",), ("fill_models", "rule"), ("grid",),
         ("measurement", "deciding_net"), ("measurement", "mid"), ("measurement", "part_and_rest"),
         ("measurement", "removals"), ("statistics", "rule"), ("null", "rule"), ("population", "codes"),
         ("population", "codes_rule"), ("population", "source"), ("selection", "rule"), ("uncertainty", "rule"),
         ("verdicts", "prediction"), ("verdicts", "printed_with"), ("verdicts", "rule"))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def shown(trail):
    return any(trail[:len(prefix)] == prefix for prefix in SHOWN)


def changed_registration(change):
    """(the registration's bytes with change(value) applied, in the one byte form, version 3, the rule files)."""
    value = copy.deepcopy(VALUE)
    change(value)
    return rt.canonical(value), V3_RAW, RULES_RAW


class FrozenFiles(unittest.TestCase):
    def test_each_file_is_the_reviewed_one_in_its_one_byte_form(self):
        self.assertEqual((V3.sha256, RULE_FAV.sha256, RULE_BAND.sha256, PLAN.sha256),
                         (V3_SHA256, FAV_SHA256, BAND_SHA256, REGISTRATION_SHA256))
        for file, pinned in PINNED.items():
            raw = file.read_bytes()
            with self.subTest(file=file.name):
                self.assertEqual(digest(raw), pinned)
                raw.decode("ascii")
                self.assertEqual(raw, (json.dumps(json.loads(raw), sort_keys=True, indent=2, ensure_ascii=True)
                                       + "\n").encode("ascii"))
                self.assertEqual(raw, partition.file_bytes(json.loads(raw)))
                self.assertEqual(raw, rulefile.file_bytes(json.loads(raw)))
                self.assertTrue(raw.endswith(b"}\n") and not raw.endswith(b"\n\n"))

    def test_no_blank_is_left(self):
        for file in PINNED:
            raw = file.read_bytes()
            with self.subTest(file=file.name):
                self.assertNotIn(b"<<", raw)
                self.assertNotIn(b">>", raw)
                self.assertEqual([item for _, item in rt.leaves(json.loads(raw))
                                  if isinstance(item, str) and ("<<" in item or ">>" in item)], [])

    def test_the_registration_names_the_other_three_by_their_hashes(self):
        self.assertEqual(VALUE["data"]["split"], {"name": "partitions-v3.json", "sha256": V3_SHA256})
        self.assertEqual(VALUE["data"]["rules"], [{"name": f"{FAV_RULE}.json", "sha256": FAV_SHA256},
                                                  {"name": f"{BAND_RULE}.json", "sha256": BAND_SHA256}])


class SplitV3(unittest.TestCase):
    def test_it_is_version_2_plus_exactly_three_sentences(self):
        second, third = json.loads(V2_RAW), json.loads(V3_RAW)
        self.assertEqual(sorted(set(third["prior_exposure"]) - set(second["prior_exposure"])), sorted(ADDED))
        self.assertEqual(set(second["prior_exposure"]) - set(third["prior_exposure"]), set())
        for key in ADDED:
            with self.subTest(key=key):
                text = third["prior_exposure"].pop(key)
                self.assertTrue(text.startswith(key) or text.startswith("P0's run of 2026-10-17"), text[:40])
                self.assertTrue(text.endswith(".") and "\n" not in text and text == text.strip())
        self.assertEqual(third, second)
        self.assertTrue(V3.prior_exposure["close-v1"].startswith("close-v1 read the whole 2015-16 PRO year"))

    def test_every_date_gets_the_same_label_and_half_under_versions_1_2_and_3(self):
        checked = 0
        for day in lane.days("2015-01-01", "2027-12-31"):
            self.assertEqual((V3.label(day), V3.half(day)), (V2.label(day), V2.half(day)), day)
            self.assertEqual((V3.label(day), V3.half(day)), (V1.label(day), V1.half(day)), day)
            checked += 1
        self.assertEqual(checked, (date(2028, 1, 1) - date(2015, 1, 1)).days)
        self.assertEqual((V3.ranges, V3.halves, V3.max_looks, V3.roles, V3.explore_budgets, V3.alpha_lifetimes),
                         (V2.ranges, V2.halves, V2.max_looks, V2.roles, V2.explore_budgets, V2.alpha_lifetimes))
        self.assertEqual(json.loads(V3_RAW)["schema"], "betangel.partitions/v1")       # "v3" names the file only

    def test_versions_1_and_2_keep_their_hashes(self):
        self.assertEqual((digest(V1_RAW), V1.sha256), (lane.SPLIT_SHA256, lane.SPLIT_SHA256))
        self.assertEqual((digest(V2_RAW), V2.sha256), (V2_SHA256, V2_SHA256))
        self.assertEqual(lane.SPLIT_SHA256, "dafa34416d704b24f66b540f754fa7681ca8fcb188a2327cb72fd40340ee3f2e")
        self.assertEqual(V2_SHA256, "e65ff6c06da47e272dbb6bb2a6a1e308f99258a7e8fd6b55ed8ffc3d855e1e83")


class RuleFiles(unittest.TestCase):
    MARKET_ROWS = [("market_open", "==", 1, "market_not_open"), ("active_runners", ">=", 5, "too_few_active_runners")]
    LATER_ROWS = [("best_back_size", ">=", Fraction(10), "depth_below_min"),
                  ("best_lay", ">=", Fraction("1.01"), "no_best_lay"),
                  ("spread_ticks", ">=", 1, "gap_not_1_to_20_ticks"),
                  ("spread_ticks", "<=", 20, "gap_not_1_to_20_ticks")]

    def test_the_favourite_file(self):
        self.assertEqual((RULE_FAV.id, RULE_FAV.lineage, RULE_FAV.select, RULE_FAV.parent_sha256),
                         (FAV_RULE, "close", "favourite", lane.RULE_SHA256["close-v1-lay"]))
        self.assertEqual(RULE_FAV.times, tuple((at_s, PULL) for at_s in TIMES))
        self.assertEqual(RULE_FAV.market, Market(("GB", "IE"), "7", "WIN", "ODDS", 1, True, None, None))
        self.assertEqual(RULE_FAV.intents, (Template("LAY", "best_back", None, Fraction(2), "lapse"),))
        self.assertEqual([tuple(row) for row in RULE_FAV.when], self.MARKET_ROWS + [
            ("best_back", ">=", Fraction("1.50"), "best_back_out_of_range"),
            ("best_back", "<=", Fraction("4.00"), "best_back_out_of_range")] + self.LATER_ROWS)

    def test_the_band_file(self):
        self.assertEqual((RULE_BAND.id, RULE_BAND.lineage, RULE_BAND.select, RULE_BAND.parent_sha256),
                         (BAND_RULE, "close", "each_active", FAV_SHA256))
        self.assertEqual((RULE_BAND.times, RULE_BAND.market, RULE_BAND.intents),
                         (RULE_FAV.times, RULE_FAV.market, RULE_FAV.intents))
        self.assertEqual([tuple(row) for row in RULE_BAND.when], self.MARKET_ROWS + [
            ("best_back", ">=", Fraction("1.01"), "no_best_back"),
            ("best_back", ">=", Fraction("4.00"), "best_back_out_of_range"),
            ("best_back", "<=", Fraction("10.00"), "best_back_out_of_range")] + self.LATER_ROWS)

    def test_the_favourite_file_is_close_v1s_lay_file_with_its_times_rows_and_sentences_changed(self):
        fav, lay = json.loads(RULES_RAW[f"{FAV_RULE}.json"]), json.loads(LAY_RAW)
        self.assertEqual(fav["parent_sha256"], digest(LAY_RAW))
        self.assertEqual({key for key in lay if fav[key] != lay[key]},
                         {"claim", "counter_claim", "id", "parent_sha256", "times", "when"})
        self.assertEqual(fav["when"][:5], lay["when"][:5])

    def test_the_band_file_is_the_favourite_file_but_for_its_selector_its_rows_and_its_sentences(self):
        fav, band = (json.loads(RULES_RAW[f"{name}.json"]) for name in (FAV_RULE, BAND_RULE))
        self.assertEqual(band["parent_sha256"], digest(RULES_RAW[f"{FAV_RULE}.json"]))
        self.assertEqual({key for key in fav if fav[key] != band[key]},
                         {"claim", "counter_claim", "id", "parent_sha256", "select", "when"})
        self.assertEqual(band["when"][:2] + band["when"][5:], fav["when"][:2] + fav["when"][4:])

    def test_each_claim_and_counter_claim_is_one_sentence_of_40_to_200_characters(self):
        texts = [text for rule in (RULE_FAV, RULE_BAND) for text in (rule.claim, rule.counter_claim)]
        self.assertEqual(len(set(texts)), 4)
        for text in texts:
            with self.subTest(text=text[:30]):
                self.assertTrue(text.endswith(".") and text.count(". ") == 0 and 40 <= len(text) <= 200)


class Registration(unittest.TestCase):
    def assertRefused(self, reason, given):
        with self.assertRaises(RegistrationError) as caught:
            parse_registration(*given)
        self.assertEqual(str(caught.exception), f"registration:{reason}")

    def test_it_parses_with_version_3_and_its_two_rule_files(self):
        self.assertEqual((PLAN.study, PLAN.lineage, PLAN.labels, PLAN.labels_read), (STUDY, "close", ("PRO-D", "PRO-V"),
                                                                                    ("PRO-D",)))
        self.assertEqual((PLAN.hour_rises_counted, PLAN.explore_only, PLAN.trd_counts), (False, True, EARLY_TRD_COUNTS))
        self.assertEqual((PLAN.split.sha256, [rule.sha256 for rule in PLAN.rules]), (V3_SHA256, [FAV_SHA256,
                                                                                               BAND_SHA256]))
        self.assertEqual([rule.select for rule in PLAN.rules], ["favourite", "each_active"])
        self.assertEqual(PLAN.cells, tuple(Cell(rule_id, at_s, PULL) for rule_id in (FAV_RULE, BAND_RULE)
                                           for at_s in TIMES))
        self.assertEqual((PLAN.family_m, PLAN.alpha, PLAN.after_the_hour_s), (2, Fraction(0), 60))

    def test_it_is_refused_with_version_2_or_version_1(self):
        for raw in (V2_RAW, V1_RAW):
            with self.subTest(split=digest(raw)[:8]):
                self.assertRefused("data.split:sha256_mismatch", (REGISTRATION_RAW, raw, RULES_RAW))
        # The same text naming version 2 (or 1) by its name and hash: its cites are not that file's prior exposure.
        for name, raw in (("partitions-v2.json", V2_RAW), ("partitions-v1.json", V1_RAW)):
            with self.subTest(split=name):
                value = copy.deepcopy(VALUE)
                value["data"]["split"] = {"name": name, "sha256": digest(raw)}
                self.assertRefused("exposure.cites:not_the_splits_prior_exposure", (rt.canonical(value), raw,
                                                                                     RULES_RAW))

    def test_it_takes_only_its_own_rule_files(self):
        for index, (name, raw) in enumerate(((f"{FAV_RULE}.json", LAY_RAW),
                                             (f"{BAND_RULE}.json", RULES_RAW[f"{FAV_RULE}.json"]))):
            with self.subTest(name=name):
                self.assertRefused(f"data.rules[{index}]:sha256_mismatch",
                                   (REGISTRATION_RAW, V3_RAW, {**RULES_RAW, name: raw}))
        # The same files under close-v1's id, without the new keys, are refused at the each_active file.
        def as_close(value):
            value["id"] = "close-v1"
            del value["data"]["traded_volume"]["hour_rises"], value["data"]["partitions"]["read"]
            value["diagnostics"]["trd_counts"].remove(rt.LEFT_OUT)

        self.assertRefused("data.rules[1]:select_not_favourite", changed_registration(as_close))

    def test_population_codes_are_the_rule_files_own(self):
        self.assertEqual(tuple(VALUE["population"]["codes"]), CODES)
        self.assertEqual((PLAN.codes, population_codes(PLAN.rules), population_codes((RULE_FAV, RULE_BAND))),
                         (CODES,) * 3)
        self.assertRefused("population.codes:not_the_implemented_list",
                           changed_registration(lambda value: value["population"]["codes"].remove("no_active_runner")))

    def test_the_values_the_parser_checks(self):
        self.assertEqual((VALUE["id"], VALUE["registered_on"], VALUE["status"]), (STUDY, "2026-11-17", "registered"))
        self.assertEqual((VALUE["grid"]["cells"], VALUE["family"]["alpha"], VALUE["family"]["m"]), (8, "0.0", 2))
        self.assertEqual(sorted(VALUE["exposure"]["cites"]), sorted(V3.prior_exposure))
        self.assertEqual(len(VALUE["exposure"]["cites"]), len(set(VALUE["exposure"]["cites"])))
        self.assertEqual(VALUE["exposure"]["cites"], ["p0", "movement-v1", "lp-v1", "suite-counts", "p0-basic-2023-25",
                                                      "close-v1", "early-price-v1", "p0-basic-2015-16"])
        self.assertEqual(sorted(VALUE["exposure"]["lines"]), sorted(V3.prior_exposure))
        self.assertEqual(VALUE["null"]["label"], NULL_LABEL)
        self.assertEqual((VALUE["data"]["traded_volume"]["hour_rises"], VALUE["data"]["partitions"]["read"]),
                         ("not_counted", "explore_only"))
        self.assertEqual(VALUE["diagnostics"]["trd_counts"], list(EARLY_TRD_COUNTS))

    def test_every_field_the_spec_does_not_show_is_close_v1s(self):
        new, old = dict(rt.leaves(VALUE)), dict(rt.leaves(CLOSE))
        kept = sorted(trail for trail in old if not shown(trail))
        self.assertGreater(len(kept), 40)
        self.assertEqual(sorted(trail for trail in new if not shown(trail)), kept)
        self.assertEqual([trail for trail in kept if new[trail] != old[trail]], [])
        # Of the fields the spec shows, these keys are new; every other key is close-v1's.
        self.assertEqual(set(new) - set(old), {
            ("data", "partitions", "read"), ("data", "traded_volume", "hour_rises"),
            ("exposure", "lines", "close-v1"), ("exposure", "lines", "early-price-v1"),
            ("exposure", "lines", "p0-basic-2015-16"), ("exposure", "lines", "p0-basic-2023-25")})
        self.assertEqual(set(old) - set(new), set())
        self.assertEqual(sorted(VALUE), sorted(CLOSE))

    def test_the_two_texts_that_begin_with_close_v1s_words(self):
        old = CLOSE["fill_models"]["rule"]
        self.assertTrue(VALUE["fill_models"]["rule"].startswith(old + " Over a window of hours: "))
        ending = "of the first-published off."
        old = CLOSE["uncertainty"]["rule"]
        self.assertEqual(old.count(ending), 1)
        kept = old[:old.index(ending) + len(ending)]
        self.assertTrue(VALUE["uncertainty"]["rule"].startswith(kept + " The four times of one market, "))
        self.assertNotIn(old[len(kept):].strip(), VALUE["uncertainty"]["rule"])     # close-v1's last sentence is not

    def test_decided_by_holds_no_quotation_mark(self):
        for mark in ('"', "“", "”", "‘", "’", "«", "»"):                      # the apostrophe of Yu's is allowed
            self.assertNotIn(mark, VALUE["decided_by"])
        self.assertIn("2026-11-16", VALUE["decided_by"])
        self.assertTrue(VALUE["decided_by"].endswith("the record is in the lab's notes."))

    def test_the_signed_prediction_is_words_and_counts_of_cells(self):
        prediction = VALUE["verdicts"]["prediction"]
        self.assertTrue(prediction.startswith("(1) The four cells at 30 minutes and at 1 hour pass the stop rules"))
        self.assertTrue(prediction.endswith("the filled orders score below the unfilled ones."))
        self.assertNotIn("point", prediction)


class LedgerOfThisStudy(lane.LedgerCase):
    """Made-up hashes; version 3's bytes: what the ledger takes of close-early-v1 and what it refuses."""

    def early(self, kind="look", **changes):
        values = dict(partitions=("PRO-D",), partitions_sha256=V3_SHA256, registration_sha256=REGISTRATION_SHA256)
        values.update(changes)
        return self.request(STUDY, kind=kind, **values)

    def refused(self, code, request):
        before = self.path.read_bytes() if self.path.exists() else None
        ledger = self.open()
        self.assertRefused(code, ledger.check, request, V3_RAW)
        self.assertRefused(code, ledger.record, request, V3_RAW, lane.sha("t"), "completed", lambda: lane.CLOCK)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    def test_a_look_and_an_explore_on_pro_d_are_taken(self):
        self.assertEqual(self.early().lineage, "close")
        self.assertIsNone(self.run_pass(self.early(), split=V3_RAW))
        self.assertIsNone(self.run_pass(self.early(kind="explore"), split=V3_RAW))
        entries = verify(self.path)
        self.assertEqual([(e["kind"], e["study"], e["lineage"], e["partitions"], e["partitions_sha256"], e["m"],
                           e["alpha"]) for e in entries],
                         [("look", STUDY, "close", ["PRO-D"], V3_SHA256, 0, "0"),
                          ("explore", STUDY, "close", ["PRO-D"], V3_SHA256, 0, "0")])
        self.assertEqual(self.open().counts("close"), {"looks": 1, "registered": 0})     # an explore line is neither

    def test_an_explore_that_names_pro_v_and_a_registered_pass_are_refused(self):
        self.run_pass(self.early(), split=V3_RAW)
        for labels in (("PRO-D", "PRO-V"), ("PRO-V",)):
            with self.subTest(labels=labels):
                self.refused("ledger:entry:explore_touches_another_role", self.early(kind="explore", partitions=labels))
        # A registered request with this registration's family: alpha "0.0" is not a rate a test may spend.
        family = VALUE["family"]
        self.refused("ledger:entry:m_or_alpha_malformed",
                     self.early(kind="registered", partitions=("PRO-D", "PRO-V"), m=family["m"], alpha=family["alpha"]))
        self.refused("ledger:entry:registered_without_holdout",
                     self.early(kind="registered", m=family["m"], alpha="0.02"))
        self.assertEqual([e["kind"] for e in verify(self.path)], ["look"])


# ---------------------------------------------------------------------------------------------------------------
# The real files on synthetic markets

class RealFilesWalk(rt.RestingCase):
    def test_the_real_files_walk_from_four_hours_out(self):
        fav, each = FAV_RULE, BAND_RULE
        # Backs from 2 hours out, lays from 1 hour out, the definition from 5 hours out.
        backs = {sid: (back, None) for sid, (back, _) in BOOK.items()}
        walked = self.walked(lp_market(off=rt.DAY_OFF, opening_minutes=300, books={7200: backs, 3600: BOOK}),
                             plan=PLAN, when=rt.DAY_OFF)
        found = {(cell.rule_id, cell.at_s): outcome for cell, outcome in walked.outcomes.items()}
        counted = {(cell.rule_id, cell.at_s): codes for cell, codes in walked.runner_codes.items()}
        self.assertEqual((found[fav, -14400], found[each, -14400]), ("no_priced_active_runner", "no_best_back"))
        self.assertEqual((found[fav, -7200], found[each, -7200]), ("no_best_lay", "best_back_out_of_range"))
        self.assertEqual(counted, {(each, -14400): {"no_best_back": 6},
                                   (each, -7200): {"best_back_out_of_range": 2, "no_best_lay": 4},
                                   (each, -3600): {"issued": 4, "best_back_out_of_range": 2},
                                   (each, -1800): {"issued": 4, "best_back_out_of_range": 2}})
        for at_s in (-3600, -1800):
            self.assertEqual((found[fav, at_s].price, found[fav, at_s].gap_ticks), (rt.P(2.5), 1))
            self.assertEqual([(row.price, row.gap_ticks) for row in found[each, at_s]],
                             [(rt.P(4.0), 1), (rt.P(6.0), 3), (rt.P(8.0), 5), (rt.P(10.0), 1)])
            self.assertEqual({fill.ended for row in rt.orders_of(found[each, at_s]) + [found[fav, at_s]]
                              for fill in row.fills.values()}, {"pull"})
        # A file whose definition is first published two hours out (or 1 minute later) is too late for the earlier
        # time (or the two earlier times).
        for minutes, late in ((120, {-14400}), (119, {-14400, -7200})):
            with self.subTest(minutes=minutes):
                walked = self.walked(lp_market(off=rt.DAY_OFF, opening_minutes=minutes), plan=PLAN, when=rt.DAY_OFF)
                self.assertEqual({cell.at_s for cell, outcome in walked.outcomes.items()
                                  if outcome == "anchor_not_known_by_decision"}, late)


def real_members():
    """[(member path, messages)] and the market ids of a small archive: four markets on explore dates and four on
    test dates, two in each half-year, each opening 5 hours before the off with the standard book from 17,000 s out
    and one trade through the favourite's and R2's best back prices 1,000 s before the off."""
    offs = (rt.offs_of(rt.D, rt.OFF, 2) + rt.offs_of(rt.D, rt.WINTER, 2) + rt.offs_of(rt.V, rt.OFF, 2)
            + rt.offs_of(rt.V, rt.WINTER, 2))
    trades = [(1000, FAV, [[2.48, 1.0]]), (1000, R2, [[3.95, 1.0]])]
    members = []
    for index, off in enumerate(offs):
        market_id = f"1.1830{index:02d}001"
        members.append((path(market_id, off), lp_market(market_id, off, books={17000: BOOK}, trades=trades,
                                                        opening_minutes=300)))
    return members, [name.rsplit("/", 1)[1][:-4] for name, _ in members]


_RUN = {}


def real_run():
    """One blind look and one explore pass through main() with the repository's registration (no --registration:
    the runner reads research/studies/close-early-v1.json, version 3 beside it and the rule files in rules/), over
    real_members(), with a ledger of its own and _walk wrapped (opening). Run once for this module."""
    if not _RUN:
        folder = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(folder.cleanup)
        base = Path(folder.name).resolve()
        members, ids = real_members()
        root = rt.build_root(base, members)
        ledger = base / "ledger"
        ledger.mkdir(mode=0o700)
        common = ["--study", STUDY, "--registration-sha256", REGISTRATION_SHA256, "--archive-root", str(root),
                  "--code-commit", rt.COMMIT, "--ledger", str(ledger)]
        opened = {"blind": [], "explore": []}
        with rt.opening(opened["blind"]):
            blind = rt.cli(*common, "--pass", "blind", "--out", str(base / "blind"), "--ledger-head", "none")
        head = json.loads(blind[1]).get("ledger_head", "none")
        with rt.opening(opened["explore"]):
            explore = rt.cli(*common, "--pass", "explore", "--out", str(base / "explore"), "--ledger-head", head,
                             "--blind-receipt", str(base / "blind" / "receipt.json"))
        _RUN["value"] = dict(base=base, root=root, ids=ids, ledger=ledger, blind=blind, explore=explore,
                             opened=opened)
    return _RUN["value"]


class RealFilesRun(unittest.TestCase):
    def setUp(self):
        self.given = real_run()

    def results(self):
        return {name: json.loads(self.given[name][1]) for name in ("blind", "explore")}

    def test_the_blind_look_and_the_explore_pass_run_the_real_files(self):
        self.assertEqual((self.given["blind"][0], self.given["explore"][0]), (0, 0),
                         (self.given["blind"][1], self.given["explore"][1]))
        cells = [f"{cell.rule_id}@{cell.at_s}" for cell in PLAN.cells]
        for name, result in self.results().items():
            with self.subTest(name=name):
                self.assertEqual((result["status"], result["study"], result["pass"], result["records"],
                                  result["replayed_markets"]), ("completed", STUDY, name, 8, 4))
                self.assertEqual(result["issued"], dict.fromkeys(cells, 4))
                self.assertEqual(result["scored"], {cell: 16 if cell.startswith(BAND_RULE) else 4 for cell in cells})
        look, entry = verify(self.given["ledger"] / LEDGER_FILE)
        self.assertEqual([(item["kind"], item["study"], item["lineage"], item["partitions"], item["partitions_sha256"],
                           item["registration_sha256"], item["m"], item["alpha"]) for item in (look, entry)],
                         [(kind, STUDY, "close", ["PRO-D"], V3_SHA256, REGISTRATION_SHA256, 0, "0")
                          for kind in ("look", "explore")])

    def test_only_explore_date_files_are_opened(self):
        self.assertEqual(self.given["opened"], {"blind": [rt.D] * 4, "explore": [rt.D] * 4})
        opened = sum(member.stat().st_size for member in rt.raw_members(self.given["root"])
                     if member.name[:-4] in self.given["ids"][:4])
        for name in ("blind", "explore"):
            value = json.loads(rt.outputs_of(self.given["base"] / name)["summary.json"])
            receipt = json.loads(rt.outputs_of(self.given["base"] / name)["receipt.json"])
            with self.subTest(name=name):
                self.assertEqual(receipt["bytes_walked"] + receipt["bytes_drained"], opened)
                self.assertEqual(value["partitions"], {"explore_label": rt.D, "test_label": rt.V, "read": [rt.D]})
                for cell, tables in value["cells"].items():
                    self.assertEqual({code: count for code, count in tables["codes"].items() if count},
                                     {"partition_not_registered": 4, "issued": 4})
                    self.assertEqual([set(tables[key]) for key in ("classes", "fills", "fills_not_scored")],
                                     [{rt.D}] * 3)
                    # The trade 1,000 s out fills the favourite's order, and of the band's only R2's, at every time.
                    pessimistic = tables["fills"][rt.D][rt.PESSIMISTIC]
                    self.assertEqual((pessimistic["fills"], pessimistic["by_kind"][rt.THROUGH]), (4, 4), cell)

    def test_with_the_real_gates_nothing_but_counts_prints(self):
        """Under 100 values a group gives counts only, and under 200 pessimistic fills on 15 race dates a cell gives
        counts only in E1 and E2: so on this small root no SD, range, quantile, mean or interval is given, and the
        only floats are fill rates (counts of fills over counts of scored orders) and the tables' sqrt(2 ln c)."""
        for name, result in self.results().items():
            value = json.loads(rt.outputs_of(self.given["base"] / name)["summary.json"])
            with self.subTest(name=name):
                self.assertEqual(list(rt.walk_floats(result)), [])                  # the printed line
                self.assertEqual({trail[-1] for trail, _ in rt.walk_floats(value)},
                                 {"fill_rate"} | ({"sqrt_2_ln_c"} if name == "explore" else set()))
                statistics = [item for _, item in rt.walk_items(value)
                              if isinstance(item, dict) and ("mean" in item or "estimate" in item)]
                self.assertEqual(name == "explore", bool(statistics))
                for item in statistics:
                    self.assertEqual({key: item[key] for key in STATISTICS if key in item},
                                     {key: None for key in STATISTICS if key in item})
                spreads = [item for key, item in rt.walk_items(value) if key in ("sd", "ranges", "quantiles",
                                                                                 "queue_quantiles",
                                                                                 "seconds_quantiles")]
                self.assertGreater(len(spreads), 40)
                self.assertEqual(set(map(repr, spreads)), {"None"})
        explore = json.loads(rt.outputs_of(self.given["base"] / "explore")["summary.json"])
        self.assertEqual({cell: item[rt.D]["figures_given"] for cell, item in explore["figures"]["size_gate"].items()},
                         {f"{cell.rule_id}@{cell.at_s}": False for cell in PLAN.cells})
        text = rt.outputs_of(self.given["base"] / "explore")["summary.md"].decode()
        self.assertEqual((text.count("figures given: yes"), text.count("figures given: no")), (0, len(PLAN.cells)))

    def test_the_registrations_words_on_b9_and_on_the_exploratory_tables_are_what_prints(self):
        """registered_pass.rule: B9, the table by gap, is in both passes and gives counts only. registered_pass.releases
        and statistics.rule: of the explore pass, E1, E2 and E3 alone are headed exploratory with c and sqrt(2 ln c)."""
        self.assertTrue(VALUE["registered_pass"]["rule"].endswith(
            "No outcome figure is given by gap, or by price within a cell; the table by gap (B9) gives counts only."))
        self.assertIn("each of the explore pass's tables E1, E2 and E3 prints its number of cells c and sqrt(2 ln c).",
                      VALUE["statistics"]["rule"])
        self.assertEqual([item[:3] for item in VALUE["registered_pass"]["releases"][1:]], ["E1:", "E2:", "E3:"])
        for name in ("blind", "explore"):
            value = json.loads(rt.outputs_of(self.given["base"] / name)["summary.json"])
            text = rt.outputs_of(self.given["base"] / name)["summary.md"].decode()
            with self.subTest(name=name):
                self.assertIn("## B9 ", text)
                for cell, tables in value["cells"].items():
                    self.assertEqual({type(count) for counts in tables["by_gap_at_t"].values()
                                      for count in counts.values()}, {int}, cell)
                headed = {trail[:-1] for trail, _ in rt.walk_floats(value) if trail[-1] == "sqrt_2_ln_c"}
                self.assertEqual(headed, {("figures",), ("half_years",), ("bsp_book",)} if name == "explore" else set())
                for key in ("figures", "half_years", "bsp_book"):
                    self.assertEqual(value[key]["heading"] if name == "explore" else value.get(key),
                                     "exploratory" if name == "explore" else None)


class NoOtherFrozenFile(unittest.TestCase):
    def test_the_registrations_and_split_files_are_the_reviewed_ones(self):
        # A stray registration (a fallback id's, say) or a fourth split file would otherwise pass every test.
        self.assertEqual(sorted(path.name for path in (RESEARCH / "studies").glob("*.json")),
                         ["close-early-v1.json", "close-v1.json", "early-price-2015-16-v1.json",
                          "early-price-v1.json", "lp-v1.json", "movement-v1.json"])
        self.assertEqual(sorted(path.name for path in RESEARCH.glob("partitions-*.json")),
                         ["partitions-v1.json", "partitions-v2.json", "partitions-v3.json"])
        for study in EARLY_IDS[1:]:                                  # the two fallback ids have no file
            with self.subTest(study=study):
                self.assertFalse((RESEARCH / "studies" / f"{study}.json").exists())


if __name__ == "__main__":
    unittest.main()
