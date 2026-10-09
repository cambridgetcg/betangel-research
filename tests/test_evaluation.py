import copy
import unittest

from betangel_research.evaluation import (
    chronological_folds, evaluate_records, settled_market_costs,
)


def record(market="a", selection="1", actual=0, prediction=0, day="2026-10-01"):
    return {"market_id": market, "selection_id": selection, "event_date": day,
            "actual_change": actual, "predictions": {"baseline": prediction}}


class EvaluationTests(unittest.TestCase):
    def test_unequal_fields_and_days(self):
        rows = [record(actual=1), record(selection="2", actual=1),
                record(market="b", actual=9, day="2026-10-02")]
        original = copy.deepcopy(rows)
        result = evaluate_records(rows)
        self.assertAlmostEqual(result["models"]["baseline"]["mae"], 11 / 3)
        self.assertEqual(result["models"]["baseline"]["race_balanced_mae"], 5)
        self.assertEqual(result["counts"], {"records": 3, "markets": 2, "days": 2})
        self.assertEqual(result["per_day"]["2026-10-02"]["models"]["baseline"]["mae"], 9)
        self.assertEqual(rows, original)

    def test_three_way_direction_and_tolerance(self):
        pairs = [(-1, -2), (0, 1e-12), (1e-12, -1e-12), (1, 0), (-1, 1)]
        rows = [record(selection=str(i), actual=a, prediction=p) for i, (a, p) in enumerate(pairs)]
        self.assertAlmostEqual(evaluate_records(rows)["models"]["baseline"]["directional_accuracy"], 3 / 5)
        rows.append(record(market="b", actual=1, prediction=-1))
        self.assertAlmostEqual(evaluate_records(rows)["models"]["baseline"]["race_balanced_directional_accuracy"], .3)

    def test_invalid_records(self):
        bad = [[], [record(), record()], [record(day="20261001")],
               [record(actual=float("nan"))], [record(prediction=float("inf"))],
               [record(actual=True)], [record(actual=10 ** 400)], [record(market="")],
               [record(actual=-1e308, prediction=1e308)],
               [record(), record(selection="2", day="2026-10-02")],
               [record(), {**record(selection="2"), "predictions": {"other": 0}}],
               [{**record(), "predictions": {}}], [{}], [None]]
        for rows in bad:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                evaluate_records(rows)

    def test_models_keep_separate_scores(self):
        rows = [record(actual=2), record(selection="2", actual=-1)]
        for row in rows:
            row["predictions"]["perfect"] = row["actual_change"]
        scores = evaluate_records(rows)["models"]
        self.assertEqual(scores["baseline"]["mae"], 1.5)
        self.assertEqual(scores["perfect"]["mae"], 0)
        self.assertEqual(scores["perfect"]["directional_accuracy"], 1)

    def test_expanding_chronology_and_partial_block(self):
        days = [f"2026-10-{i:02d}" for i in range(1, 8)]
        folds = chronological_folds(list(reversed(days)) + days[:1], 2, 2)
        self.assertEqual([f["test_dates"] for f in folds], [days[2:4], days[4:6], days[6:]])
        self.assertEqual(folds[-1]["train_dates"], days[:6])
        for fold in folds:
            self.assertLess(max(fold["train_dates"]), min(fold["test_dates"]))
        self.assertEqual(chronological_folds(days[:2]), [])
        self.assertEqual(chronological_folds([]), [])
        for args in [(days, 0, 1), (days, 2, True), (days, 2, 1.5), (["bad"], 2, 1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                chronological_folds(*args)

    def test_commission_is_per_positive_market(self):
        result = settled_market_costs({"win": 10, "loss": -10, "flat": 0}, .02, 3, 1)
        self.assertEqual(result["gross_total"], 0)
        self.assertAlmostEqual(result["commission_total"], .2)
        self.assertAlmostEqual(result["net_after_all_costs"], -4.2)
        self.assertEqual(result["markets"]["loss"]["commission"], 0)
        self.assertEqual(settled_market_costs({}, 0, 2)["net_after_all_costs"], -2)

    def test_invalid_costs(self):
        for args in [({"x": float("inf")}, .02), ({"x": True}, .02), ({"": 1}, .02),
                     ({}, -.1), ({}, 1.1), ({}, True), ({}, .02, -1),
                     ({}, .02, 0, float("nan")), ({}, .02, 0, -1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                settled_market_costs(*args)


if __name__ == "__main__":
    unittest.main()
