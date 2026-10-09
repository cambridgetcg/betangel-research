"""Feature-measurement and pre-decision chronology tests, not profit tests."""

import json
import tempfile
import unittest
from pathlib import Path

from betangel_research.microstructure import sample_features
from betangel_research.temporal import timestamp_ms


class MicrostructureTests(unittest.TestCase):
    start = timestamp_ms("2020-01-01T12:00:00Z")

    def definition(self, **changes):
        return dict(marketTime="2020-01-01T12:00:00Z", marketType="WIN",
                    countryCode="IE", status="OPEN", inPlay=False,
                    numberOfActiveRunners=2,
                    runners=[{"id": 1, "status": "ACTIVE"},
                             {"id": 2, "status": "ACTIVE"}], **changes)

    def msg(self, offset, **change):
        return {"op": "mcm", "pt": self.start + round(offset * 1000),
                "mc": [{"id": "1.1", **change}]}

    def runners(self, back=2.0, lay=2.02, volume=100.0):
        return [{"id": rid, "batb": [[0, back, 30], [1, back - 0.01, 99]],
                 "batl": [[0, lay, 10]], "tv": volume} for rid in (1, 2)]

    def initial(self, offset=-150):
        return self.msg(offset, marketDefinition=self.definition(), rc=self.runners())

    def sample(self, rows, **options):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            result = sample_features(path, **options)
        # Fail on NaN/Infinity rather than Python's permissive JSON extension.
        json.dumps(result, allow_nan=False)
        return result

    def test_level_zero_replaces_old_price_and_exact_time_updates_are_included(self):
        result = self.sample([
            self.initial(),
            self.msg(-120, rc=[{"id": 1, "batb": [[0, 1.98, 20]], "tv": 105}]),
            self.msg(-120, rc=[{"id": 1, "batl": [[0, 2.0, 20]], "tv": 107}]),
            self.msg(-119, rc=[{"id": 1, "batb": [[0, 1.5, 20]], "tv": 200}]),
        ])
        row = result["records"][0]
        self.assertTrue(row["eligible"])
        self.assertEqual(row["back"], 1.98)  # Old 2.0 must not survive by price key.
        self.assertEqual(row["lay"], 2.0)
        self.assertEqual(row["previous_back"], 2.0)
        self.assertEqual(row["volume_delta"], 7)
        self.assertEqual(row["book_imbalance"], 0)
        self.assertAlmostEqual(row["relative_spread"], 0.02 / 1.99)
        self.assertEqual(row["observed_ms"], row["decision_ms"])

    def test_volume_is_runner_total_not_market_or_moving_best_price_bucket(self):
        result = self.sample([
            self.msg(-150, marketDefinition=self.definition(), rc=[
                {**runner, "trd": [[2.0, 99], [1.99, 1]]} for runner in self.runners()
            ], tv=10000),
            self.msg(-120, rc=[{"id": 1, "batb": [[0, 1.99, 30]], "tv": 110,
                                "trd": [[1.99, 11]]}], tv=20000),
        ])
        self.assertEqual(result["records"][0]["volume_delta"], 10)
        self.assertEqual(result["records"][1]["volume_delta"], 0)

    def test_zero_deletes_level_without_promoting_cached_level_one(self):
        result = self.sample([self.initial(), self.msg(-120, rc=[
            {"id": 1, "batb": [[0, 0, 0]], "tv": 105}])])
        row = result["records"][0]
        self.assertIsNone(row["back"])
        self.assertIsNone(row["book_imbalance"])
        self.assertFalse(row["eligible"])
        self.assertIn("missing_top_book", row["invalid_reasons"])

    def test_volume_decrease_then_recovery_is_still_undefined(self):
        result = self.sample([self.initial(),
                              self.msg(-140, rc=[{"id": 1, "tv": 90}]),
                              self.msg(-120, rc=[{"id": 1, "tv": 110}])])
        row = result["records"][0]
        self.assertIsNone(row["volume_delta"])
        self.assertIn("runner_volume_decreased_or_invalid_in_window", row["invalid_reasons"])
        self.assertFalse(row["eligible"])

    def test_negative_or_nonfinite_volume_does_not_become_activity(self):
        for invalid in (-1, float("nan"), float("inf"), True):
            with self.subTest(invalid=invalid):
                row = self.sample([self.initial(), self.msg(-140, rc=[{"id": 1, "tv": invalid}]),
                                   self.msg(-120, rc=[{"id": 1, "tv": 110}])])["records"][0]
                self.assertIsNone(row["volume_delta"])
                self.assertFalse(row["eligible"])

    def test_missing_tv_never_falls_back_to_traded_ladder(self):
        runners = self.runners()
        del runners[0]["tv"]
        runners[0]["trd"] = [[2.0, 100]]
        row = self.sample([self.msg(-150, marketDefinition=self.definition(), rc=runners),
                           self.msg(-120, rc=[{"id": 1, "tv": 110}])])["records"][0]
        self.assertIsNone(row["volume_delta"])
        self.assertIn("missing_or_invalid_runner_volume", row["invalid_reasons"])

    def test_null_delta_fields_preserve_cache_without_refreshing_quote_time(self):
        row = self.sample([self.initial(), self.msg(-120, rc=[
            {"id": 1, "batb": None, "batl": None, "tv": None}])])["records"][0]
        self.assertTrue(row["eligible"])
        self.assertEqual(row["volume_delta"], 0)
        self.assertEqual(row["back_observed_ms"], self.start - 150000)
        self.assertEqual(row["observed_ms"], self.start - 120000)

    def test_image_clears_omitted_quotes_and_tv_and_breaks_interval(self):
        row = self.sample([self.initial(), self.msg(-120, img=True,
            marketDefinition=self.definition(), rc=[{"id": 1, "batb": [[0, 2.0, 50]]}])])["records"][0]
        self.assertIsNone(row["lay"])
        self.assertIsNone(row["runner_volume"])
        self.assertIsNone(row["volume_delta"])
        self.assertIn("image_reset_in_window", row["invalid_reasons"])
        restored = self.sample([self.initial(), self.msg(-130, img=True,
            marketDefinition=self.definition(), rc=self.runners(volume=105)),
            self.msg(-120, rc=self.runners(volume=110))])["records"][0]
        self.assertIsNone(restored["volume_delta"])
        self.assertFalse(restored["eligible"])

    def test_suspension_and_reopen_need_new_quotes_and_break_volume_window(self):
        rows = [self.initial(), self.msg(-140, marketDefinition={"status": "SUSPENDED"},
                                        rc=self.runners(volume=105)),
                self.msg(-120, marketDefinition={"status": "OPEN"})]
        row = self.sample(rows)["records"][0]
        self.assertIsNone(row["back"])
        self.assertIn("status_break_in_window", row["invalid_reasons"])
        self.assertIsNone(row["volume_delta"])
        row = self.sample(rows + [self.msg(-120, rc=self.runners(volume=110))])["records"][0]
        self.assertFalse(row["eligible"])
        self.assertIn("status_break_in_window", row["invalid_reasons"])

    def test_inplay_is_ineligible_and_does_not_carry_prices_back_to_preplay(self):
        row = self.sample([self.initial(), self.msg(-130, marketDefinition={"inPlay": True}),
                           self.msg(-120, marketDefinition={"inPlay": False})])["records"][0]
        self.assertIsNone(row["back"])
        self.assertIn("status_break_in_window", row["invalid_reasons"])

    def test_removal_excludes_survivors_and_does_not_emit_removed_runner(self):
        result = self.sample([self.initial(), self.msg(-120, marketDefinition={
            "numberOfActiveRunners": 1, "runners": [{"id": 2, "status": "REMOVED"}]})])
        self.assertEqual(len(result["records"]), 1)
        row = result["records"][0]
        self.assertEqual(row["selection_id"], "1")
        self.assertIn("runner_set_changed_in_window", row["invalid_reasons"])
        self.assertIsNone(row["volume_delta"])

    def test_initial_schedule_is_fixed_and_known_revisions_stay_invalid(self):
        row = self.sample([self.initial(), self.msg(-140, marketDefinition={
            "marketTime": "2020-01-01T12:01:00Z"}), self.msg(-120, marketDefinition={
            "marketTime": "2020-01-01T12:00:00Z"})])["records"][0]
        self.assertEqual(row["decision_ms"], self.start - 120000)
        self.assertEqual(row["start_ms"], self.start)
        self.assertIn("scheduled_start_revised", row["invalid_reasons"])

    def test_future_removal_schedule_and_even_pro_fields_do_not_veto_decision(self):
        prefix = [self.initial(), self.msg(-120, rc=self.runners(volume=110))]
        expected = self.sample(prefix)["records"]
        actual = self.sample(prefix + [self.msg(-119, marketDefinition={
            "marketTime": "2020-01-01T12:01:00Z", "inPlay": True,
            "runners": [{"id": 1, "status": "REMOVED"}]},
            rc=[{"id": 2, "atb": [[2.0, 9000]], "tv": 1}])])["records"]
        self.assertEqual(actual, expected)
        self.assertTrue(all(row["eligible"] for row in actual))

    def test_lookback_freshness_is_checked_separately_from_decision(self):
        result = self.sample([self.initial(-161), self.msg(-120, rc=self.runners(volume=110))])
        self.assertIn("stale_lookback_snapshot", result["records"][0]["invalid_reasons"])
        self.assertNotIn("stale_decision_snapshot", result["records"][0]["invalid_reasons"])
        result = self.sample([self.initial(-160), self.msg(-120, rc=self.runners(volume=110))])
        self.assertTrue(result["records"][0]["eligible"])

    def test_decision_freshness_cannot_be_refreshed_by_another_market(self):
        other = self.msg(-120, marketDefinition=self.definition(), rc=self.runners())
        other["mc"][0]["id"] = "1.2"
        row = self.sample([self.initial(), other])["records"][0]
        self.assertIn("stale_decision_snapshot", row["invalid_reasons"])

    def test_missing_lookback_is_retained_but_unobserved_decision_is_audited(self):
        row = self.sample([self.initial(-140), self.msg(-120, rc=self.runners())])["records"][0]
        self.assertIsNone(row["previous_observed_ms"])
        self.assertIn("missing_lookback_snapshot", row["invalid_reasons"])
        result = self.sample([self.initial()])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["audit"]["market_exclusions"]["1.1"],
                         ["missing_observable_decision_snapshot"])

    def test_basic_pro_and_virtual_are_reported_unsupported(self):
        for fields, reason in (({"ltp": 2.0}, "unsupported_basic_or_unknown_fields"),
                               ({"atb": [[2.0, 20]], "atl": [[2.02, 10]]},
                                "unsupported_pro_or_mixed_fields"),
                               ({"bdatb": [[0, 2.0, 20]]}, "unsupported_virtual_fields")):
            with self.subTest(fields=fields):
                rows = [self.msg(-150, marketDefinition=self.definition(), rc=[{"id": 1, **fields}]),
                        self.msg(-120, rc=[{"id": 1, **fields}])]
                result = self.sample(rows)
                self.assertEqual(result["audit"]["eligible_records"], 0)
                self.assertIn(reason, result["records"][0]["invalid_reasons"])
                self.assertEqual(result["audit"]["supported_package"], "ADV_ONLY")

    def test_crossed_book_is_not_imbalance_signal(self):
        row = self.sample([self.initial(), self.msg(-120, rc=self.runners(lay=1.99))])["records"][0]
        self.assertIn("locked_or_crossed_top_book", row["invalid_reasons"])
        self.assertIsNone(row["book_imbalance"])
        self.assertIsNone(row["relative_spread"])

    def test_win_scope_uses_only_market_type_known_at_decision(self):
        md = self.definition()
        md["marketType"] = "PLACE"
        result = self.sample([self.msg(-150, marketDefinition=md, rc=self.runners()),
                              self.msg(-120, rc=self.runners()),
                              self.msg(-119, marketDefinition={"marketType": "WIN"})])
        self.assertEqual(result["records"], [])
        self.assertEqual(result["audit"]["market_exclusions"]["1.1"], ["outside_win_market_scope"])

    def test_malformed_levels_and_parameters_fail_explicitly(self):
        for update in ([[2.0, 20]], [[3, 2.0, 20]], [[0, 2.0, -1]],
                       [[0, 2.0, 20], [0, 2.02, 20]]):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.sample([self.initial(), self.msg(-120, rc=[{"id": 1, "batb": update}])])
        for name, value in (("lookback_s", 0), ("decision_before_start_s", True),
                            ("max_snapshot_age_s", -1), ("lookback_s", 0.0001)):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                self.sample([self.initial()], **{name: value})

    def test_nonmonotonic_messages_are_rejected_by_existing_reader(self):
        with self.assertRaisesRegex(ValueError, "decreasing pt"):
            self.sample([self.initial(), self.msg(-120), self.msg(-130)])


if __name__ == "__main__":
    unittest.main()
