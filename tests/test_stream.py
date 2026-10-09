import copy
import json
from pathlib import Path
import tempfile
import unittest
from betangel_research.stream import replay_snapshots, build_records, fixed_forecasts
from betangel_research.temporal import timestamp_ms


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.start = timestamp_ms("2020-01-01T12:00:00Z")
        self.config = dict(countries=["IE"], market_type="WIN", decision_before_start_s=120,
                           lookback_s=60, horizon_s=30, delays_s=[0], max_snapshot_age_s=90)
        self.md = dict(marketTime="2020-01-01T12:00:00Z", marketType="WIN", countryCode="IE",
                       status="OPEN", inPlay=False, numberOfActiveRunners=2,
                       runners=[{"id": 1, "status": "ACTIVE"}, {"id": 2, "status": "ACTIVE"}])
        self.data = [self.msg(-240, {"marketDefinition": self.md, "rc": [{"id": 1, "ltp": 2}, {"id": 2, "ltp": 2}]}),
                     self.msg(-180, {"rc": [{"id": 1, "ltp": 2.5}]}),
                     self.msg(-120, {"rc": [{"id": 1, "ltp": 3}]}),
                     self.msg(-90, {"rc": [{"id": 1, "ltp": 4}]}),
                     self.msg(-89, {"rc": [{"id": 1, "ltp": 100}]})]

    def msg(self, offset, change):
        return {"op": "mcm", "pt": self.start + offset * 1000, "mc": [{"id": "1.1", **change}]}

    def run_data(self, data):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stream.jsonl"
            path.write_text("\n".join(json.dumps(x) for x in data))
            states, _ = replay_snapshots(path, self.config)
        return states, build_records(states, self.config)

    def test_exact_cutoff_carry_forward_and_no_future_label(self):
        states, (rows, _) = self.run_data(self.data)
        self.assertEqual(len(rows["0"]), 2)
        label = states["1.1"]["snapshots"][self.start - 90000]
        self.assertEqual(label["ltp"], {"1": 4, "2": 2})
        one = next(r for r in rows["0"] if r["selection_id"] == "1")
        self.assertAlmostEqual(one["actual_change"], 1 / 3 - 0.4)
        self.assertEqual(one["predictions"]["no_change"], 0)

    def test_image_removes_previous_prices(self):
        data = copy.deepcopy(self.data)
        data[2]["mc"][0].update(img=True, marketDefinition=self.md)
        _, (rows, exclusions) = self.run_data(data)
        self.assertEqual(rows["0"], [])
        self.assertEqual(exclusions["0"]["1.1"], "missing_or_invalid_active_runner_prices")

    def test_zero_ltp_is_not_a_valid_price(self):
        data = copy.deepcopy(self.data)
        data[2]["mc"][0]["rc"][0]["ltp"] = 0
        _, (rows, exclusions) = self.run_data(data)
        self.assertEqual(rows["0"], [])
        self.assertIn("invalid", exclusions["0"]["1.1"])

    def test_schedule_revision_and_runner_removal_excluded(self):
        data = copy.deepcopy(self.data)
        data[2]["mc"][0]["marketDefinition"] = {"marketTime": "2020-01-01T12:01:00Z"}
        _, (_, exclusions) = self.run_data(data)
        self.assertEqual(exclusions["0"]["1.1"], "scheduled_start_revised")
        data = copy.deepcopy(self.data)
        data[3]["mc"][0]["marketDefinition"] = {"numberOfActiveRunners": 1, "runners": [{"id": 2, "status": "REMOVED"}]}
        _, (rows, _) = self.run_data(data)
        self.assertEqual(rows["0"], [])

    def test_reversed_time_rejected(self):
        with self.assertRaises(ValueError):
            self.run_data([self.data[0], self.data[2], self.data[1]])

    def test_no_carry_forward_across_suspension(self):
        data = self.data[:2] + [self.msg(-150, {"marketDefinition": {"status": "SUSPENDED"}}),
                              self.msg(-140, {"marketDefinition": {"status": "OPEN"}})] + self.data[2:]
        _, (rows, exclusions) = self.run_data(data)
        self.assertEqual(rows["0"], [])
        self.assertEqual(exclusions["0"]["1.1"], "missing_or_invalid_active_runner_prices")

    def test_forecasts_normalized_and_labels_not_inputs(self):
        forecasts = fixed_forecasts({"1": 0.4, "2": 0.6}, {"1": 0.6, "2": 0.4}, 60, 30)
        for values in forecasts.values():
            self.assertAlmostEqual(sum(values.values()), 1)
            self.assertTrue(all(0 < p < 1 for p in values.values()))
        self.assertAlmostEqual(forecasts["momentum"]["1"], 0.7)
