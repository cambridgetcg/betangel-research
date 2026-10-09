import copy
import json
from pathlib import Path
import tempfile
import unittest

from betangel_research.motifs import evaluate_policies, run_motif_lab

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "research/experiments/motif-prototypes-v1.json"
FIXTURE = ROOT / "research/fixtures/motif-scenarios.json"


class MotifTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG.read_text())
        self.fixture = json.loads(FIXTURE.read_text())
        self.record = self.fixture["records"][0]
        self.context = self.fixture["context"][0]

    def evaluate(self, records=None, events=None):
        return evaluate_policies(records if records is not None else [self.record],
                                 events if events is not None else [self.context], self.config)

    def decision(self, rows, policy="P01", delay=0):
        return next(d for d in rows if d["policy"] == policy and d["processing_delay_s"] == delay)

    def test_no_trade_cannot_become_directional_volume(self):
        self.record["volume_delta"] = 0
        self.record["book_imbalance"] = .99
        result = self.decision(self.evaluate())
        self.assertEqual(result["status"], "abstain")
        self.assertIn("matched_activity_not_established", result["reasons"])

    def test_hypothesis_and_context_unknown_are_separate(self):
        rows = self.evaluate(events=[])
        self.assertEqual(self.decision(rows)["status"], "hypothesis")
        self.assertEqual(self.decision(rows)["odds_direction"], "shorten")
        self.assertEqual(self.decision(rows, "P03")["reasons"], ["context_unknown"])

    def test_future_context_and_hidden_outcomes_do_not_change_decisions(self):
        before = self.evaluate()
        future = copy.deepcopy(self.context)
        future.update(id="later-correction", assessment="contradicted",
                      analysis_completed_ms=self.record["decision_ms"] + 1)
        self.record["future_winner"] = True
        self.record["future_price"] = 1000
        self.assertEqual(before, self.evaluate(events=[future, self.context]))

    def test_all_availability_clocks_are_enforced(self):
        for key in ("published_ms", "received_ms", "link_known_ms", "analysis_completed_ms"):
            context = copy.deepcopy(self.context)
            context[key] = self.record["decision_ms"] + 1
            with self.subTest(key=key):
                self.assertIn("context_unknown", self.decision(self.evaluate(events=[context]), "P03")["reasons"])
        context = copy.deepcopy(self.context)
        del context["received_ms"]
        self.assertIn("context_unknown", self.decision(self.evaluate(events=[context]), "P03")["reasons"])

    def test_source_hash_and_counterexplanation_required(self):
        for key in ("content_sha256", "source", "alternative_explanation", "coverage_scope"):
            context = copy.deepcopy(self.context)
            del context[key]
            with self.subTest(key=key):
                self.assertIn("context_unknown", self.decision(self.evaluate(events=[context]), "P03")["reasons"])

    def test_malformed_context_links_and_scopes_are_unknown(self):
        for value in (True, {"x": "y"}, [], [None], [""]):
            context = {**self.context, "coverage_scope": value}
            with self.subTest(value=value):
                self.assertIn("context_unknown", self.decision(self.evaluate(events=[context]), "P03")["reasons"])
        for kind, extra in (("public_tip", {"selection_id": []}),
                            ("previous_race_finish", {"related_market_id": {"x": "y"},
                                                      "event_status": "confirmed"})):
            context = {**self.context, "kind": kind, **extra}
            self.assertEqual(self.decision(self.evaluate(events=[context]), "P02")["status"], "abstain")

    def test_conflicting_context_abstains_independent_of_input_order(self):
        conflict = copy.deepcopy(self.context)
        conflict.update(id="separate-contradicting-evidence", assessment="contradicted")
        a = self.evaluate(events=[self.context, conflict])
        b = self.evaluate(events=[conflict, self.context])
        self.assertEqual(a, b)
        self.assertEqual(self.decision(a, "P01")["status"], "hypothesis")
        self.assertIn("context_contradicted", self.decision(a, "P03")["reasons"])

    def test_ambiguous_immutable_context_and_duplicate_records_rejected(self):
        conflict = {**self.context, "assessment": "contradicted"}
        with self.assertRaisesRegex(ValueError, "conflicting immutable"):
            self.evaluate(events=[self.context, conflict])
        with self.assertRaisesRegex(ValueError, "duplicate feature"):
            self.evaluate(records=[self.record, self.record])

    def test_latency_does_not_grant_future_context_access(self):
        late = {**self.context, "analysis_completed_ms": self.record["decision_ms"] + 1000}
        rows = self.evaluate(events=[late])
        self.assertIn("context_unknown", self.decision(rows, "P03", 2)["reasons"])
        self.assertEqual(self.decision(rows, delay=2)["status"], "hypothesis")
        self.assertEqual(self.decision(rows)["expires_ms"], self.record["observed_ms"] + 10000)
        self.assertIn("feature_too_old_at_issue", self.decision(rows, delay=10)["reasons"])
        self.assertIn("signal_expired", self.decision(rows, delay=30)["reasons"])

    def test_feature_and_context_age_deadlines_are_exclusive_expiries(self):
        self.config["processing_delays_s"] = [0, 9]
        rows = self.evaluate()
        self.assertEqual(self.decision(rows, delay=9)["status"], "abstain")
        self.assertIn("feature_too_old_at_issue", self.decision(rows, delay=9)["reasons"])
        self.config["processing_delays_s"] = [0, 2]
        self.context["event_ms"] = self.record["decision_ms"] - 119000
        rows = self.evaluate()
        self.assertEqual(self.decision(rows, "P03")["expires_ms"], self.record["decision_ms"] + 1000)
        self.assertIn("context_expired_at_issue", self.decision(rows, "P03", 2)["reasons"])

    def test_context_expiration_is_checked_at_issue(self):
        self.context["expires_ms"] = self.record["decision_ms"] + 1000
        rows = self.evaluate()
        self.assertEqual(self.decision(rows, "P03")["status"], "hypothesis")
        self.assertEqual(self.decision(rows, "P03")["expires_ms"], self.context["expires_ms"])
        self.assertIn("context_expired_at_issue", self.decision(rows, "P03", 2)["reasons"])

    def test_attention_watch_has_no_direction_and_one_row_per_market(self):
        self.context.update(kind="public_tip")
        second = {**self.record, "selection_id": "runner-B"}
        rows = self.evaluate(records=[self.record, second])
        watches = [r for r in rows if r["policy"] == "P02" and r["processing_delay_s"] == 0]
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0]["status"], "watch")
        self.assertIsNone(watches[0]["odds_direction"])

    def test_feature_time_cannot_be_in_future(self):
        self.record["observed_ms"] = self.record["decision_ms"] + 1
        self.assertIn("future_feature", self.decision(self.evaluate())["reasons"])

    def test_reversed_feature_chronology_cannot_create_momentum(self):
        self.record["previous_observed_ms"] = self.record["observed_ms"] + 1
        self.assertIn("invalid_previous_feature_time", self.decision(self.evaluate())["reasons"])

    def test_malformed_inputs_fail_with_a_clear_validation_error(self):
        for records, events in (([None], []), ([self.record], [None]), ({}, [])):
            with self.subTest(records=records, events=events), self.assertRaises(ValueError):
                self.evaluate(records, events)
        self.record["start_ms"] = None
        with self.assertRaisesRegex(ValueError, "start_ms"):
            self.evaluate()

    def test_unlinked_runner_and_unconfirmed_race_finish_are_not_attention_evidence(self):
        self.context.update(kind="public_tip", selection_id="unknown-runner")
        self.assertEqual(self.decision(self.evaluate(), "P02")["status"], "abstain")
        self.context.update(kind="previous_race_finish", selection_id=None,
                            related_market_id="prior-market", event_status="provisional")
        self.assertEqual(self.decision(self.evaluate(), "P02")["status"], "abstain")
        self.context["event_status"] = "confirmed"
        self.assertEqual(self.decision(self.evaluate(), "P02")["status"], "watch")

    def test_provenance_outputs_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "run"
            report = run_motif_lab(FIXTURE, CONFIG, output)
            self.assertIsNone(report["economic_result"])
            self.assertIsNone(report["forecast_score"])
            self.assertEqual(report["audit"]["data_kind"], "synthetic")
            self.assertEqual(len(report["input_sha256"]), 64)
            self.assertEqual(len((output / "decisions.jsonl").read_text().splitlines()), 9 * 3 * 4)
            self.assertTrue((Path(temp) / "run-ledger.jsonl").is_file())
            with self.assertRaisesRegex(ValueError, "already exists"):
                run_motif_lab(FIXTURE, CONFIG, output)


if __name__ == "__main__":
    unittest.main()
