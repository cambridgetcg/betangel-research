import json
from pathlib import Path
import tempfile
import unittest
import test_stream
from betangel_research.__main__ import run_replay


class CliTests(unittest.TestCase):
    def test_delay_metrics_provenance_and_existing_run_protection(self):
        fixture = test_stream.StreamTests()
        fixture.setUp()
        config = {**fixture.config, "experiment_id": "synthetic-unit-test", "delays_s": [0, 2],
                  "candidates": ["no_change", "momentum", "mean_reversion", "rival_linkage"]}
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "synthetic.jsonl"
            source.write_text("\n".join(json.dumps(x) for x in fixture.data))
            config_path = base / "config.json"
            config_path.write_text(json.dumps(config))
            output = base / "run"
            report = run_replay(source, config_path, output)
            self.assertEqual(report["scores_by_delay_seconds"]["2"]["counts"]["records"], 2)
            self.assertIsNone(report["scores_by_delay_seconds"]["2"]["models"]["momentum"]["directional_accuracy"])
            self.assertIsNotNone(report["scores_by_delay_seconds"]["0"]["models"]["momentum"]["directional_accuracy"])
            self.assertIsNone(report["economic_result"])
            self.assertEqual(report["available_chronological_folds"], [])
            self.assertEqual(len(report["source"]["sha256"]), 64)
            self.assertTrue((output / "predictions.csv").is_file())
            self.assertTrue((base / "run-ledger.jsonl").is_file())
            with self.assertRaises(ValueError):
                run_replay(source, config_path, output)
