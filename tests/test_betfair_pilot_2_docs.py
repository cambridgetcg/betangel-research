"""Pilot 2 (P2-LP) docs stage: the draft contract research/pilots/p2-lp.json against its own money arithmetic and
the Pilot 2 page (docs/betfair-pilot-2-policy.md). Docs only: no network, no gateway; Pilot 1 is untouched.
"""

import hashlib
import json
from pathlib import Path
import unittest

from betangel_research.betfair import pilot
from betangel_research.betfair.pilot import PilotError

REPO = Path(__file__).resolve().parent.parent
P2_PATH = REPO / "research" / "pilots" / "p2-lp.json"
P1_PATH = REPO / "research" / "pilots" / "p1-fav-t5.json"
P1_SHA256 = "4dbccb5a45f7a4779e0263d4df5048f582f10b8dc3bd22c5f2e93e791155de92"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def at(document, dotted):
    value = document
    for key in dotted.split("."):
        value = value[key]
    return value


class DraftContractTests(unittest.TestCase):
    def setUp(self):
        self.c = load(P2_PATH)

    def test_identity_and_draft_status(self):
        c = self.c
        self.assertEqual(c["schema"], "betangel.pilot-contract-lp/v1")
        self.assertEqual((c["id"], c["rule"]["id"]), ("p2-lp", "P2-LP"))
        self.assertEqual(c["status"], "draft")
        self.assertIsNone(c["frozen_on"])
        self.assertEqual((c["customer_strategy_ref"], c["intent_source"], c["journal_note_key"]),
                         ("p2-lp", "pilot_lp", "pilot_lp"))
        p1 = load(P1_PATH)
        self.assertNotEqual(c["customer_strategy_ref"], p1["customer_strategy_ref"])
        self.assertNotEqual(c["intent_source"], p1["intent_source"])
        for key in ("policy", "engineering_note", "plan"):
            with self.subTest(key=key):
                self.assertTrue((REPO / c[key]).is_file())

    def test_money_follows_from_the_stake_and_the_lay_cap(self):
        m, caps = self.c["money"], self.c["caps"]
        stake, lay_cap = m["stake_per_leg_p"], m["max_lay_price_h"]
        self.assertEqual((stake, lay_cap), (200, 600))
        worst_lay = stake * (lay_cap - 100) // 100
        self.assertEqual(worst_lay, m["worst_race_p"])
        self.assertGreaterEqual(worst_lay, stake)                  # the race's worst case is the lay's, not their sum
        self.assertEqual(caps["max_stake_per_order_p"], stake)
        self.assertEqual(caps["max_liability_per_order_p"], worst_lay)
        self.assertEqual(caps["max_market_exposure_p"], max(worst_lay, stake))
        self.assertEqual(caps["max_daily_loss_p"], m["day_stop_p"])
        self.assertEqual(caps["max_total_exposure_p"], m["max_open_worst_case_p"])
        self.assertEqual(caps["external_exposure_tolerance_p"], 0)

    def test_the_pilot_stop_holds_by_count(self):
        m, limits = self.c["money"], self.c["placement_limits"]
        self.assertEqual(limits["lifetime_place_records"] * m["worst_race_p"], m["pilot_stop_p"])
        self.assertGreaterEqual(m["day_stop_p"], m["worst_race_p"])
        self.assertEqual(limits["races_in_flight"], 1)
        self.assertEqual(self.c["caps"]["max_orders_per_day"], 2 * limits["place_records_per_utc_day"])

    def test_pinned_ledger_examples_are_the_per_outcome_worst_case(self):
        # No hedge credit: in each outcome only the losing leg counts; the worst outcome is W.
        def worst(legs):
            outcomes = (True, False)                    # our runner wins, or loses
            loss = lambda side, size_p, price_h, wins: (
                (-size_p * (price_h - 100) // 100 if wins else 0) if side == "LAY" else (0 if wins else -size_p))
            return min(sum(loss(*leg, wins) for leg in legs) for wins in outcomes)

        examples = self.c["ledger"]["pinned_examples_p"]
        self.assertEqual(worst([("LAY", 200, 600)]), examples["pending_lone_lay_at_6_0"])
        self.assertEqual(worst([("BACK", 200, 510), ("LAY", 200, 500)]),
                         examples["unsettled_round_trip_back_5_1_lay_5_0"])
        self.assertEqual(examples["both_legs_checked_nothing_matched"], 0)
        self.assertEqual(examples["nothing_sent"], 0)

    def test_rule_values(self):
        r = self.c["rule"]
        self.assertEqual(r["legs"]["order"], ["BACK", "LAY"])
        self.assertEqual(r["legs"]["lay"]["max_price_h"], self.c["money"]["max_lay_price_h"])
        self.assertEqual((r["selection"]["best_back_from_h"], r["selection"]["best_back_to_h"]), (150, 590))
        self.assertEqual(r["selection"]["spread_ticks"], [3, 4])
        self.assertEqual(self.c["caps"]["max_price_deviation_ticks"], r["selection"]["spread_ticks"][-1] - 1)
        self.assertLess(r["prefilter"]["from_offset_s"], r["prefilter"]["to_offset_s"])
        self.assertLess(r["prefilter"]["to_offset_s"], r["window"]["decision_book_observed_from_offset_s"])
        self.assertLess(r["window"]["decision_book_observed_from_offset_s"], r["send_deadline_offset_s"])
        self.assertLess(r["send_deadline_offset_s"], r["cancel"]["offset_s"])
        self.assertLess(r["cancel"]["offset_s"], 0)

    def test_pending_paths_resolve(self):
        for path in self.c["pending_yu_at_build_consent"]:
            with self.subTest(path=path):
                at(self.c, path)

    def test_no_fractional_numbers(self):
        def numbers(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from numbers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from numbers(item)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                yield value

        self.assertTrue(all(isinstance(n, int) for n in numbers(self.c)))


class PilotOneIsUntouchedTests(unittest.TestCase):
    def test_pilot_1_contract_bytes_unchanged(self):
        self.assertEqual(hashlib.sha256(P1_PATH.read_bytes()).hexdigest(), P1_SHA256)

    def test_pilot_1_loader_refuses_the_draft(self):
        raw = P2_PATH.read_bytes()
        with self.assertRaises(PilotError):
            pilot.load_contract(json.loads(raw), raw_sha256=hashlib.sha256(raw).hexdigest())


class PageTests(unittest.TestCase):
    def test_page_states_the_draft_numbers(self):
        page = (REPO / "docs" / "betfair-pilot-2-policy.md").read_text(encoding="utf-8")
        for text in ("| Highest lay price | 6.0 |", "| Day stop | −£20 |", "| Pilot stop | −£100 |",
                     "| Worst race | −£10 |", "| Decisions in total | at most 10 |"):
            with self.subTest(text=text):
                self.assertIn(text, page)
        self.assertIn("are not in force", page)


if __name__ == "__main__":
    unittest.main()
