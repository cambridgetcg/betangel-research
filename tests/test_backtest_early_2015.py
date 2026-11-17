"""The drift check's frozen file: the registration research/studies/early-price-2015-16-v1.json, with split file
version 3 (research/partitions-v3.json, merged with close-early-v1 and pinned in tests/test_backtest_close_early.py).

The two files are read from the repository and checked with the repository's own parsers (partition.parse_split,
early.parse_registration). Every ledger lives in a temporary folder that the test removes, and its entries carry
made-up hashes. Every market is synthetic, built with the fixture helpers of tests/test_backtest_early.py, and goes
through the real ingest, index and replay; the pass runner (early_study) runs the real registration, unchanged, over
such markets. These tests show that the registration is the reviewed file in its one byte form and names version 3
by its pinned hash, that version 3 gives every date the label and half the first two give, that the registration
parses with version 3 only, that its measurement is early-price-v1's, that its words are the ones lab-Claude signed
and quoted, what the ledger takes of this study, and that its blind look opens the two 2015-16 BASIC files and its
explore pass the explore-date file only, with counts alone on a small synthetic root. They say nothing about real
Betfair files or results.
"""

import copy
from datetime import date, datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair.backtest import early, early_study, partition, resting
from betangel_research.betfair.backtest.movement import RegistrationError
from betangel_research.betfair.backtest.partition import parse_split
from betangel_research.betfair.backtest.study_ledger import LEDGER_FILE, verify

import test_backtest_close_early as close_early
import test_backtest_lane as lane
import test_betfair_backtest_replay as fx
from test_backtest_early import (COMMIT, V2_SHA256, basic_market, build_root, cli, leaves_of, opened_ids,
                                 outputs_of, raw_members, walk_floats)
from test_backtest_early import REGISTRATION_SHA256 as V1_REGISTRATION_SHA256
from test_backtest_lp import markdown_columns, walk_items

REPO = Path(__file__).resolve().parent.parent
RESEARCH = REPO / "research"
STUDY = early.DRIFT_CHECK_ID
D, V = "PRO-D", "PRO-V"
# The registration's sha256 as reviewed: any change to the file must change this line too (a new registration).
REGISTRATION_SHA256 = "bc66167cecd5169183dcae39847b3748ebc09a385411b46788deb0f8e9c904b3"
# Split file version 3 as merged; tests/test_backtest_close_early.py pins the same hash.
V3_SHA256 = "fd35425aab3717099f0be2ce497505ec57fc37549cd24d470e3d6961ff792b65"
REGISTRATION_FILE = RESEARCH / "studies" / f"{STUDY}.json"
RAW = REGISTRATION_FILE.read_bytes()
V1_RAW, V2_RAW, V3_RAW = (lane.SPLIT_RAW, (RESEARCH / "partitions-v2.json").read_bytes(),
                          (RESEARCH / "partitions-v3.json").read_bytes())
V1, V2, V3 = parse_split(V1_RAW), parse_split(V2_RAW), parse_split(V3_RAW)
PLAN = early.parse_registration(RAW, V3_RAW)
VALUE = json.loads(RAW)
EARLY_RAW = (RESEARCH / "studies" / "early-price-v1.json").read_bytes()
EARLY, EARLY_PLAN = json.loads(EARLY_RAW), early.parse_registration(EARLY_RAW, V2_RAW)
# The ids version 3 adds to version 2's prior_exposure.
ADDED = ("close-v1", "early-price-v1", "p0-basic-2015-16")

# The words the registration quotes, as lab-Claude's go note of 2026-11-17 gives them, word for word.
OFFER = ("If you'd like me to merge my own offline research PRs after an independent review, and run the night data "
         "passes without asking each time, say \"research is yours\". That covers no live code and no money.")
STANDING_WORD = "ho lah follow what you recc and suggest :)"
LAY_OK = "yes lay is ok"
QUESTION = ("Is a LAY thinkable at all? If no, I drop the drift flag. If yes, I check it offline in small steps before "
            "asking for anything live.")
FIELDS_OK = "yes to both on git."
PREDICTION = ("Signed by lab-Claude on 2026-11-17, before the build and before any pass. (a) The blind look passes S1 "
              "to S3, with about three chances in four; the likeliest stop is the files not reaching back four hours. "
              "(b) The mean x of B from -14400: DRIFT on PRO-D at the BSP as written lies below -0.006, and more "
              "likely than not at or below -0.012. (c) Shows about one chance in two, absent about one in ten, "
              "unclear about two in five. (d) Band A most likely reads unclear (about six in ten), seen again about "
              "three in ten, not seen at that size about one in ten.")
# The line's five numbers (Yu's OK of 2026-11-16 on the fields), each with the comparison it is read by.
LINE = ("mean <= -0.012 and ci_high < 0", "absent when mean >= -0.006 and ci_low > -0.018",
        "its mean is at least 0.009 below FLAT's mean", "mean <= -0.015 and ci_high < 0", "ci_low > -0.015")
# The fields the spec's section 5.1 lists as changed, as key paths (a list's entries by index); every other field
# is early-price-v1's, byte for byte.
CHANGED = (("id",), ("registered_on",), ("summary",), ("decided_by",), ("data", "start_month"),
           ("data", "end_month"), ("data", "partitions", "explore"), ("data", "partitions", "test"),
           ("data", "partitions", "rule"), ("data", "halves"), ("data", "prices", "rule"), ("data", "split"),
           ("blind_pass", "releases", 7), ("blind_pass", "rule"), ("blind_pass", "withholds", 2),
           ("blind_pass", "withholds", 3), ("exposure", "blind_pass_releases"), ("exposure", "cites"),
           ("exposure", "fresh_on_test"), ("exposure", "lines"), ("exposure", "not_fresh_on_test"),
           ("exposure", "printed_with_every_label"), ("family", "alpha"), ("family", "rule"),
           ("measurement", "part_and_rest"), ("registered_pass", "releases"), ("registered_pass", "rule"),
           ("selection", "rule"), ("verdicts", "prediction"), ("verdicts", "printed_with"),
           ("verdicts", "printed_with_label"), ("verdicts", "rule"))
# The Plan's fields that the changed text moves; every other field is early-price-v1's Plan's.
PLAN_MOVED = {"sha256", "study", "split", "explore_label", "test_label", "halves", "start_month", "end_month",
              "alpha", "label_note", "label_texts", "exposure_note"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def at(value, trail):
    for key in trail:
        value = value[key]
    return value


def fields(value, trail=()):
    """(key path, value) of every field of a registration: objects are walked, and the two lists of the blind pass
    by entry; every other list is one field."""
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from fields(item, trail + (key,))
    elif trail in (("blind_pass", "releases"), ("blind_pass", "withholds")):
        for index, item in enumerate(value):
            yield trail + (index,), item
    else:
        yield trail, value


def changed(trail):
    return any(trail[:len(prefix)] == prefix for prefix in CHANGED)


# A number of the text, for the kinds-only test: a sha256 is dropped first; a date, a month, a span of years or a time
# is one token; a sign belongs to a number only when no letter, digit, point or hyphen comes before it (so the
# hyphens of an id or a file name are not minus signs); thousands may be grouped by commas, and a percent sign is
# kept.
HASH = re.compile(rb"[0-9a-f]{64}")
NUMBER = re.compile(rb"\d{4}-\d{2}-\d{2}|\d{4}-\d{2}(?!\d)|\d{2}:\d{2}:\d{2}"
                    rb"|(?:(?<![\w.-])[-+])?\d+(?:,\d{3})*(?:\.\d+)?%?")
# Every number the registration may hold beyond early-price-v1's file. The line's five (Yu's OK on the fields,
# 2026-11-16); alpha 0.0; the section number 10.1; the 1.2 and 65% of the known release (an upper limit set by the
# share of dates on PRO-D, not a figure of any study); S3's 1500 values (and its 120 race dates, which early-price-v1's
# file already holds), this study's own stop gates; and dates and the one time of decided_by.
NUMBERS_ALLOWED = {b"-0.012", b"-0.006", b"-0.018", b"0.009", b"-0.015", b"0.0", b"10.1", b"1.2", b"65%", b"1500",
                   b"120"}
DATES_ALLOWED = {b"2015-05", b"2016-04", b"2015-05-01", b"2015-10-31", b"2015-11-01", b"2016-04-30", b"2026-10-17",
                 b"2026-11-14", b"2026-11-16", b"2026-11-17", b"18:43:51"}


def numbers(raw):
    return set(NUMBER.findall(HASH.sub(b"", raw)))


# ---------------------------------------------------------------------------------------------------------------
# D1 to D3: the two files

class TheSplitFile(unittest.TestCase):
    # D1
    def test_the_registration_names_version_3_as_merged_by_its_pinned_hash(self):
        self.assertEqual(V3_SHA256, close_early.V3_SHA256)
        self.assertEqual((digest(V3_RAW), V3.sha256, PLAN.split.sha256), (V3_SHA256,) * 3)
        self.assertEqual(VALUE["data"]["split"], {"name": "partitions-v3.json", "sha256": V3_SHA256})
        self.assertEqual(early.split_named(RAW), ("partitions-v3.json", V3_SHA256))
        self.assertEqual(V3_RAW, partition.file_bytes(json.loads(V3_RAW)))
        # The runner reads the split file beside the studies/ folder: that is research/partitions-v3.json.
        self.assertEqual(REGISTRATION_FILE.parent.parent / VALUE["data"]["split"]["name"],
                         RESEARCH / "partitions-v3.json")

    # D2
    def test_every_date_gets_the_same_label_and_half_under_versions_1_2_and_3(self):
        checked = 0
        for day in lane.days("2015-01-01", "2027-12-31"):
            self.assertEqual((V3.label(day), V3.half(day)), (V2.label(day), V2.half(day)), day)
            self.assertEqual((V3.label(day), V3.half(day)), (V1.label(day), V1.half(day)), day)
            checked += 1
        self.assertEqual(checked, (date(2028, 1, 1) - date(2015, 1, 1)).days)
        self.assertEqual((V3.ranges, V3.halves, V3.max_looks, V3.roles, V3.explore_budgets, V3.alpha_lifetimes),
                         (V2.ranges, V2.halves, V2.max_looks, V2.roles, V2.explore_budgets, V2.alpha_lifetimes))
        # The dates the end-to-end run below relies on.
        self.assertEqual([V3.label(day) for day in ("2015-05-01", "2015-05-02", "2023-06-01")], [D, V, "BASIC-D"])

    def test_the_five_old_sentences_are_unchanged_and_versions_1_and_2_keep_their_hashes(self):
        self.assertEqual(sorted(V3.prior_exposure), sorted(set(V2.prior_exposure) | set(ADDED)))
        self.assertEqual({key: text for key, text in V3.prior_exposure.items() if key not in ADDED},
                         V2.prior_exposure)
        self.assertEqual((digest(V1_RAW), V1.sha256), (lane.SPLIT_SHA256, lane.SPLIT_SHA256))
        self.assertEqual((digest(V2_RAW), V2.sha256), (V2_SHA256, V2_SHA256))
        self.assertEqual((lane.SPLIT_SHA256, V2_SHA256),
                         ("dafa34416d704b24f66b540f754fa7681ca8fcb188a2327cb72fd40340ee3f2e",
                          "e65ff6c06da47e272dbb6bb2a6a1e308f99258a7e8fd6b55ed8ffc3d855e1e83"))


class TheRegistration(unittest.TestCase):
    def assertRefused(self, reason, raw, split_raw):
        with self.assertRaises(RegistrationError) as caught:
            early.parse_registration(raw, split_raw)
        self.assertEqual(str(caught.exception), f"registration:{reason}")

    # D3
    def test_it_is_the_reviewed_file_in_its_one_byte_form(self):
        self.assertEqual((digest(RAW), PLAN.sha256), (REGISTRATION_SHA256, REGISTRATION_SHA256))
        RAW.decode("ascii")
        self.assertEqual(RAW, (json.dumps(VALUE, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii"))
        self.assertEqual(RAW, partition.file_bytes(VALUE))
        self.assertTrue(RAW.endswith(b"}\n") and not RAW.endswith(b"\n\n"))

    def test_no_blank_is_left(self):
        self.assertNotIn(b"<<", RAW)
        self.assertNotIn(b">>", RAW)
        self.assertEqual([trail for trail, item in leaves_of(VALUE) if isinstance(item, str)
                          and ("<<" in item or ">>" in item or "BLANK" in item)], [])

    def test_it_parses_with_version_3(self):
        self.assertEqual((PLAN.study, PLAN.lineage, PLAN.labels, PLAN.explore_label, PLAN.test_label),
                         (STUDY, "early-price", (D, V), D, V))
        self.assertEqual(PLAN.months, ("2015-05", "2015-06", "2015-07", "2015-08", "2015-09", "2015-10", "2015-11",
                                       "2015-12", "2016-01", "2016-02", "2016-03", "2016-04"))
        self.assertEqual(PLAN.halves, (("H1", "2015-05-01", "2015-10-31"), ("H2", "2015-11-01", "2016-04-30")))
        self.assertEqual(PLAN.halves, V3.halves)                    # the split file's own half-years
        self.assertEqual((PLAN.alpha, PLAN.family_m, PLAN.tiers), (Fraction(0), 2, ("BASIC",)))
        self.assertEqual((VALUE["id"], VALUE["lineage"], VALUE["status"], VALUE["registered_on"]),
                         (STUDY, "early-price", "registered", "2026-11-17"))

    def test_it_is_refused_with_version_2_or_version_1(self):
        for raw in (V2_RAW, V1_RAW):
            with self.subTest(split=digest(raw)[:8]):
                self.assertRefused("data.split:sha256_mismatch", RAW, raw)
        # The same text naming version 2 (or 1) by its name and hash: its cites are not that file's prior exposure.
        for name, raw in (("partitions-v2.json", V2_RAW), ("partitions-v1.json", V1_RAW)):
            with self.subTest(split=name):
                value = copy.deepcopy(VALUE)
                value["data"]["split"] = {"name": name, "sha256": digest(raw)}
                self.assertRefused("exposure.cites:not_the_splits_prior_exposure", partition.file_bytes(value), raw)


# ---------------------------------------------------------------------------------------------------------------
# D4 and D5: the same measurement, and the words

class TheSameMeasurement(unittest.TestCase):
    # D4
    def test_the_measurement_blocks_are_early_price_v1s(self):
        for key in ("cells", "timing", "population", "statistics", "uncertainty", "diagnostics", "not_measured"):
            with self.subTest(key=key):
                self.assertEqual(VALUE[key], EARLY[key])
        self.assertEqual({key: item for key, item in VALUE["measurement"].items() if key != "part_and_rest"},
                         {key: item for key, item in EARLY["measurement"].items() if key != "part_and_rest"})
        numbers = ("half_gap_allowance", "half_year_min_values", "kill_level", "lower_bound_level",
                   "underpowered_above")
        self.assertEqual({key: VALUE["verdicts"][key] for key in numbers},
                         {key: EARLY["verdicts"][key] for key in numbers})
        self.assertEqual({key: item for key, item in VALUE["selection"].items() if key != "rule"},
                         {key: item for key, item in EARLY["selection"].items() if key != "rule"})

    def test_every_field_the_spec_does_not_list_as_changed_is_early_price_v1s(self):
        new, old = dict(fields(VALUE)), dict(fields(EARLY))
        kept = sorted(trail for trail in old if not changed(trail))
        self.assertGreater(len(kept), 80)
        self.assertEqual(sorted(trail for trail in new if not changed(trail)), kept)
        self.assertEqual([trail for trail in kept if new[trail] != old[trail]], [])
        # And each field listed as changed does change.
        self.assertEqual([trail for trail in CHANGED if at(VALUE, trail) == at(EARLY, trail)], [])
        self.assertEqual(sorted(VALUE), sorted(EARLY))
        self.assertEqual({trail for trail, _ in leaves_of(VALUE) if trail[:2] != ("exposure", "lines")},
                         {trail for trail, _ in leaves_of(EARLY) if trail[:2] != ("exposure", "lines")})

    def test_the_plan_is_early_price_v1s_but_for_the_year_the_labels_and_the_texts(self):
        for name in sorted(set(early.Plan.__dataclass_fields__) - PLAN_MOVED):
            with self.subTest(field=name):
                self.assertEqual(getattr(PLAN, name), getattr(EARLY_PLAN, name))
        self.assertEqual({name for name in PLAN_MOVED if getattr(PLAN, name) == getattr(EARLY_PLAN, name)}, set())
        self.assertEqual((PLAN.move_cells, PLAN.level_cells), (EARLY_PLAN.move_cells, EARLY_PLAN.level_cells))
        self.assertEqual((PLAN.bsp_limit, PLAN.removal_note), (EARLY_PLAN.bsp_limit, EARLY_PLAN.removal_note))


class TheWords(unittest.TestCase):
    # D5
    def test_decided_by_quotes_the_offer_the_answers_and_the_question_exactly(self):
        text = VALUE["decided_by"]
        for quoted in (OFFER, STANDING_WORD, LAY_OK, QUESTION, FIELDS_OK):
            with self.subTest(quoted=quoted[:20]):
                self.assertEqual(text.count(f'"{quoted}"'), 1)
        self.assertIn("Items 2 and 3 of that message answered other questions", text)
        # Of Yu's reply of 2026-11-14 only item 1 is quoted, and of 2026-11-16 only its last words.
        for other in ("2.yes", "can you show them here", "1.yes"):
            self.assertNotIn(other, text)
        self.assertIn("2026-11-16 (18:43:51 UTC)", text)
        self.assertTrue(text.endswith("the cells, the passes and the line in verdicts.rule are lab-Claude's."))
        self.assertNotIn("/Users/", text)
        # Each of the three readings is labelled as lab-Claude's, not Yu's words.
        self.assertIn("lab-Claude reads that as yes to the offer as worded; the reading is lab-Claude's, not Yu's "
                      "words", text)
        self.assertIn("lab-Claude reads it as covering this much, and the reading is lab-Claude's, not Yu's words: a "
                      "LAY is thinkable", text)
        self.assertIn("That this covers these fields is lab-Claude's reading", text)
        self.assertNotIn("It covers this much", text)

    def test_the_known_releases_say_the_explore_pass_names_the_moved_cells(self):
        """The blind look lists the two moved cells unnamed; the explore pass names them on PRO-D with the same
        counts, so the two passes together name the blind look's PRO-V counts of DRIFT and STEAM."""
        text = VALUE["exposure"]["blind_pass_releases"]
        self.assertIn("The explore pass names DRIFT and STEAM on PRO-D with the counts the blind look gave unnamed, so "
                      "after the explore pass the blind look's PRO-V counts and pooled spreads (B7) of the two moved "
                      "cells are known by name", text)
        self.assertTrue(text.endswith("no mean, sign or interval of x on PRO-V."))

    def test_the_signed_prediction_and_the_line(self):
        self.assertEqual(VALUE["verdicts"]["prediction"], PREDICTION)
        rule = VALUE["verdicts"]["rule"]
        for words in LINE + ("THE CELL: B from -14400: DRIFT on PRO-D", "the mean of x of B from -14400: FLAT",
                             "BAND A: A from -14400: DRIFT"):
            with self.subTest(words=words):
                self.assertEqual(rule.count(words), 1)
        self.assertIn("The numbers in this rule were set by lab-Claude with early-price-v1's results in view", rule)
        self.assertEqual(VALUE["family"]["alpha"], "0.0")

    def test_exposure_cites_version_3s_eight_ids(self):
        cites = VALUE["exposure"]["cites"]
        self.assertEqual(cites, EARLY["exposure"]["cites"] + list(ADDED))
        self.assertEqual((sorted(cites), sorted(VALUE["exposure"]["lines"])), (sorted(V3.prior_exposure),) * 2)
        self.assertEqual(len(cites), 8)

    def test_kinds_only_every_number_beyond_early_price_v1s_is_on_the_list(self):
        """The registration may carry, of the figures behind it, the five numbers of the line and nothing else
        (Yu's OK on the fields, 2026-11-16). So every number not in early-price-v1's file, an integer or a decimal,
        is on the list: one of the five, a design number of this study, a date or the time of decided_by."""
        extra = numbers(RAW) - numbers(EARLY_RAW)
        self.assertEqual(sorted(extra - NUMBERS_ALLOWED - DATES_ALLOWED), [])
        self.assertLessEqual({b"-0.012", b"-0.006", b"-0.018", b"0.009", b"-0.015"}, extra)
        # The tokens: a grouped count and a percentage are numbers, the parts of a date or an id are not.
        self.assertEqual(sorted(numbers(b"held 12,345 values (67890) at -0.25% from -14400 on 2026-11-16 at 18:43:51 "
                                        b"in p0-basic-2015-16 and B@-600, YU-ANSWERS-2026-11-16.md")),
                         sorted({b"12,345", b"67890", b"-0.25%", b"-14400", b"2026-11-16", b"18:43:51", b"2015-16",
                                 b"0", b"-600"}))


# ---------------------------------------------------------------------------------------------------------------
# D6: the ledger, with made-up hashes and version 3's bytes

class LedgerOfThisStudy(lane.LedgerCase):
    def drift(self, kind="look", **changes):
        values = dict(lineage="early-price", partitions=(D, V), partitions_sha256=V3_SHA256,
                      registration_sha256=REGISTRATION_SHA256)
        if kind == "explore":
            values.update(partitions=(D,))
        values.update(changes)
        return self.request(STUDY, kind=kind, **values)

    def refused(self, code, request):
        before = self.path.read_bytes() if self.path.exists() else None
        ledger = self.open()
        self.assertRefused(code, ledger.check, request, V3_RAW)
        self.assertRefused(code, ledger.record, request, V3_RAW, lane.sha("t"), "completed", lambda: lane.CLOCK)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)

    def test_a_look_and_an_explore_on_pro_d_are_taken(self):
        self.assertIsNone(self.run_pass(self.drift(), split=V3_RAW))
        self.assertIsNone(self.run_pass(self.drift(kind="explore"), split=V3_RAW))
        self.assertEqual([(e["kind"], e["study"], e["lineage"], e["partitions"], e["partitions_sha256"], e["m"],
                           e["alpha"]) for e in verify(self.path)],
                         [("look", STUDY, "early-price", [D, V], V3_SHA256, 0, "0"),
                          ("explore", STUDY, "early-price", [D], V3_SHA256, 0, "0")])
        self.assertEqual(self.open().counts("early-price"), {"looks": 1, "registered": 0})

    def test_an_explore_that_names_pro_v_and_a_registered_pass_are_refused(self):
        self.run_pass(self.drift(), split=V3_RAW)
        for labels in ((D, V), (V,)):
            with self.subTest(labels=labels):
                self.refused("ledger:entry:explore_touches_another_role", self.drift(kind="explore", partitions=labels))
        # A registered request with this registration's family: alpha "0.0" is not a rate a test may spend.
        family = VALUE["family"]
        self.refused("ledger:entry:m_or_alpha_malformed",
                     self.drift(kind="registered", m=family["m"], alpha=family["alpha"]))
        self.refused("ledger:entry:registered_without_holdout",
                     self.drift(kind="registered", partitions=(D,), m=family["m"], alpha="0.02"))
        self.assertEqual([e["kind"] for e in verify(self.path)], ["look"])

    def test_the_lineages_explore_count_on_pro_d_is_its_own(self):
        """PRO-D's budget is 20 explore passes per lineage. Another lineage's passes on PRO-D (close-early-v1's
        lineage close) and early-price's on BASIC-D do not count against early-price's on PRO-D, nor the reverse."""
        budget = V3.explore_budget(D)
        self.assertEqual((budget, V3.explore_budget("BASIC-D")), (20, 10))
        close = dict(lineage="close", partitions=(D,), partitions_sha256=V3_SHA256)
        for run in range(budget - 1):
            self.run_pass(self.request("close-early-v1", kind="explore", run=run, **close), split=V3_RAW)
        for run in range(budget):
            self.run_pass(self.drift(kind="explore", run=run), split=V3_RAW)
        self.refused("ledger:explore_budget_spent", self.drift(kind="explore", run=budget))
        self.run_pass(self.request("close-early-v1", kind="explore", run=budget, **close), split=V3_RAW)
        self.run_pass(self.drift(kind="explore", partitions=("BASIC-D",), run=budget + 1), split=V3_RAW)
        kinds = [(e["lineage"], e["partitions"]) for e in verify(self.path)]
        self.assertEqual((kinds.count(("close", [D])), kinds.count(("early-price", [D])),
                          kinds.count(("early-price", ["BASIC-D"]))), (budget, budget, 1))


# ---------------------------------------------------------------------------------------------------------------
# D7: the real file, end to end, on synthetic BASIC markets

OFF = datetime(2015, 5, 1, 14, 0, tzinfo=timezone.utc)                     # a PRO-D race date
# One BASIC market on a PRO-D date, one on a PRO-V date, one BASIC market of 2023 and one market on the PRO path.
MARKETS = {"explore": ("1.230100001", OFF, "BASIC"),
           "locked": ("1.230200001", datetime(2015, 5, 2, 14, 0, tzinfo=timezone.utc), "BASIC"),
           "2023": ("1.230300001", datetime(2023, 6, 1, 14, 0, tzinfo=timezone.utc), "BASIC"),
           "pro": ("1.230400001", OFF, "PRO")}
STATISTICS = {"mean", "median", "estimate", "se", "t_critical", "ci_low", "ci_high", "sd", "ranges", "effect",
              "seconds_quantiles", "quantiles"}

_RUN = {}


def real_run():
    """One blind look and one explore pass of the drift check through main() with the repository's registration
    (no --registration: the runner reads research/studies/early-price-2015-16-v1.json and version 3 beside it) over
    the four markets, with a ledger of their own and _walk wrapped; then early-price-v1's blind pass over the same root
    with its own registration and another ledger. Run once for this module."""
    if not _RUN:
        folder = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(folder.cleanup)
        base = Path(folder.name).resolve()
        root = build_root(base, [(fx.path(market_id, off, plan=plan), basic_market(market_id, off))
                                 for market_id, off, plan in MARKETS.values()])
        ids = {name: market_id for name, (market_id, _, _) in MARKETS.items()}
        found = dict(base=base, root=root, ids=ids, opened={})
        for study, sha, passes in ((STUDY, REGISTRATION_SHA256, ("blind", "explore")),
                                   (early.STUDY_ID, V1_REGISTRATION_SHA256, ("blind",))):
            ledger = base / f"ledger-{study}"
            ledger.mkdir(mode=0o700)
            (base / study).mkdir()                                  # the parent of each pass's --out
            common = ["--study", study, "--registration-sha256", sha, "--archive-root", str(root), "--code-commit",
                      COMMIT, "--ledger", str(ledger)]
            head = "none"
            for name in passes:
                more = ["--blind-receipt", str(base / study / "blind" / "receipt.json")] if name != "blind" else []
                with mock.patch.object(early_study, "_walk", wraps=early_study._walk) as walked:
                    result = cli(*common, "--pass", name, "--out", str(base / study / name), "--ledger-head", head,
                                 *more)
                found[study, name], found["opened"][study, name] = result, opened_ids(walked)
                head = json.loads(result[1]).get("ledger_head", "none")
            found["ledger", study] = ledger
        _RUN["value"] = found
    return _RUN["value"]


class RealFileRun(unittest.TestCase):
    def setUp(self):
        self.given = real_run()

    def results(self):
        return {name: json.loads(self.given[STUDY, name][1]) for name in ("blind", "explore")}

    def summary(self, name):
        return json.loads(outputs_of(self.given["base"] / STUDY / name)["summary.json"])

    def test_the_blind_look_and_the_explore_pass_run_the_real_file(self):
        self.assertEqual([self.given[STUDY, name][0] for name in ("blind", "explore")], [0, 0],
                         [self.given[STUDY, name][1] for name in ("blind", "explore")])
        for name, result in self.results().items():
            with self.subTest(name=name):
                self.assertEqual((result["status"], result["study"], result["pass"], result["registration_sha256"],
                                  result["records"], result["replayed_markets"], result["ledger_repeat"]),
                                 ("completed", STUDY, name, REGISTRATION_SHA256, 4, 2 if name == "blind" else 1,
                                  False))
                self.assertEqual(result["ledger_counts"], {"looks": 1, "registered": 0})
                receipt = json.loads(outputs_of(self.given["base"] / STUDY / name)["receipt.json"])
                self.assertEqual((receipt["registration_sha256"], receipt["partitions_sha256"]),
                                 (REGISTRATION_SHA256, V3_SHA256))
        entries = verify(self.given["ledger", STUDY] / LEDGER_FILE)
        self.assertEqual([(e["kind"], e["study"], e["lineage"], e["partitions"], e["partitions_sha256"],
                           e["registration_sha256"], e["m"], e["alpha"], e["result"]) for e in entries],
                         [("look", STUDY, "early-price", [D, V], V3_SHA256, REGISTRATION_SHA256, 0, "0", "completed"),
                          ("explore", STUDY, "early-price", [D], V3_SHA256, REGISTRATION_SHA256, 0, "0",
                           "completed")])

    def test_the_blind_look_opens_both_2015_files_and_the_explore_pass_the_explore_dates_alone(self):
        ids, opened = self.given["ids"], self.given["opened"]
        self.assertEqual(opened[STUDY, "blind"], sorted([ids["explore"], ids["locked"]]))
        self.assertEqual(opened[STUDY, "explore"], [ids["explore"]])
        # The fact that flips: early-price-v1 over the same root opens its own year's file and no 2015-16 one.
        self.assertEqual(self.given[early.STUDY_ID, "blind"][0], 0, self.given[early.STUDY_ID, "blind"][1])
        self.assertEqual(opened[early.STUDY_ID, "blind"], [ids["2023"]])
        sizes = {member.name[:-4]: member.stat().st_size for member in raw_members(self.given["root"])}
        for name, read in (("blind", ("explore", "locked")), ("explore", ("explore",))):
            receipt = json.loads(outputs_of(self.given["base"] / STUDY / name)["receipt.json"])
            with self.subTest(name=name):
                self.assertEqual(receipt["bytes_walked"] + receipt["bytes_drained"],
                                 sum(sizes[ids[market]] for market in read))

    def test_the_codes_and_the_locked_labels_counts(self):
        blind, explore = self.summary("blind"), self.summary("explore")
        self.assertEqual(explore["partitions"], blind["partitions"])
        self.assertEqual(explore["partitions"], {"explore_label": D, "test_label": V})
        self.assertEqual(explore["coverage"], blind["coverage"])
        self.assertEqual(blind["coverage"]["by_month"]["2015-05"], {D: 1, V: 1, "no_registered_label": 0})
        self.assertEqual(blind["coverage"]["by_month"]["outside_registered_months"], {D: 0, V: 0,
                                                                                    "no_registered_label": 1})
        for name, value in (("blind", blind), ("explore", explore)):
            for at_s, codes in value["codes"].items():
                with self.subTest(name=name, at_s=at_s):
                    self.assertEqual({code: count for code, count in codes.items() if count},
                                     {"viewed": 2, "month_not_registered": 1, "plan_not_registered": 1}
                                     if name == "blind" else
                                     {"viewed": 1, "partition_not_registered": 1, "month_not_registered": 1,
                                      "plan_not_registered": 1})
        # In the explore pass every count under the locked label is 0 but B1's; in the blind look it is not.
        under = [(trail, item) for trail, item in leaves_of(explore) if V in trail and trail[0] != "coverage"]
        self.assertGreater(len(under), 100)
        self.assertEqual({item for _, item in under if not isinstance(item, (dict, list))}, {0})
        self.assertGreater(blind["runners"]["-600"][V]["priced"], 0)
        self.assertEqual(explore["runners"]["-600"][D], blind["runners"]["-600"][D])
        text = outputs_of(self.given["base"] / STUDY / "explore")["summary.md"].decode()
        self.assertIn(f"\n- {V}: {early_study.LOCKED_NOT_READ}\n", text)

    def test_with_the_real_gates_nothing_but_counts_prints(self):
        """Under 100 values a group gives counts only: so on this small root no SD, range, quantile, mean or interval
        is given, and the only floats are the explore tables' sqrt(2 ln c)."""
        for name, result in self.results().items():
            value = self.summary(name)
            with self.subTest(name=name):
                self.assertEqual(list(walk_floats(result)), [])                     # the printed line
                self.assertEqual({trail[-1] for trail, _ in walk_floats(value)},
                                 {"sqrt_2_ln_c"} if name == "explore" else set())
                self.assertEqual([(key, item) for key, item in walk_items(value) if key in STATISTICS
                                  and item is not None and not isinstance(item, (dict, list))], [])
                self.assertEqual([key for key, item in walk_items(value) if key in STATISTICS and item], [])
                spreads = value["spreads"]["by_cell"]
                self.assertEqual({(item["sd"], item["ranges"], item["detectable"]["effect"])
                                  for item in spreads.values()}, {(None, None, None)})
                self.assertLessEqual({item["reason"] for item in spreads.values()},
                                     {resting.COUNTS_ONLY, resting.REST_NOT_GIVEN})
        explore = self.summary("explore")
        self.assertEqual((list(explore["level_figures"]["by_cell"]), sorted(explore["move_figures"]["by_cell"])),
                         (["A@-600", "B@-600"], sorted(f"{band} from -14400: {move}" for band in ("A", "B")
                                                       for move in early.MOVES)))
        self.assertEqual({item["x"][end]["reason"] for table in ("level_figures", "move_figures")
                          for by_label in explore[table]["by_cell"].values() for item in by_label.values()
                          for end in resting.BSP_ENDS}, {resting.COUNTS_ONLY})
        self.assertEqual(explore["bsp_book"], {D: {"markets": 1, "quantiles": None}})
        self.assertEqual(explore["explore_notes"], [VALUE["verdicts"]["printed_with"],
                                                    VALUE["exposure"]["printed_with_every_label"],
                                                    VALUE["measurement"]["bsp_ends"]["limit"],
                                                    VALUE["measurement"]["removals"]])
        shown = {column.split(" (")[0]: cells for column, cells in markdown_columns(
            outputs_of(self.given["base"] / STUDY / "explore")["summary.md"].decode()).items()}
        self.assertEqual({column: cells for column, cells in shown.items()
                          if column in {"SD", "mean", "CI low", "CI high", "0.5"} and set(cells) - {"-"}}, {})


if __name__ == "__main__":
    unittest.main()
