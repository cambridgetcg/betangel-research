"""Registered measurement early-price-v1 and the split file version 2 (research/partitions-v2.json).

Everything here is synthetic. The split tests read only the two split files of the repository. Every ledger lives
in a temporary folder that the test removes, and its entries carry made-up hashes. These tests show that the
second split file gives every date the label and half the first gives, that it differs from the first only by the
two BASIC budgets and one exposure sentence, and how one ledger keeps the budgets of studies that name different
split files. They say nothing about real Betfair files or results.
"""

from datetime import date
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import unittest

from betangel_research.betfair.backtest.partition import file_bytes, parse_split
from betangel_research.betfair.backtest.study_ledger import StudyLedger, head_text, verify

from test_backtest_lane import SPLIT_SHA256 as V1_SHA256
from test_backtest_lane import LedgerCase, days, sha

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


if __name__ == "__main__":
    unittest.main()
