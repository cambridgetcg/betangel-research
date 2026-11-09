"""Pilot 1's unchanged code and Pilot 2's journal entries (docs/betfair-pilot-2-engineering.md §7), the parts that need
no gateway: an open or unsettled Pilot 2 leg is one of Pilot 1's open manual refs, so it halts a Pilot 1 open
(pilot_gate.py:318-334) and refuses Pilot 1's ARM (pilot_run.py:909-910); and a P2 ARM record is no pilot note, so
Pilot 1's journal readers ignore it. Synthetic events and core-fold records only; nothing is opened or sent. The fold
of a journal with Pilot 2's intents, submits, reports and ARM records against the fold without them needs the
gateway's journal and comes with PR-2. Pilot 1's files staying byte-identical is a merge-checklist item (git diff),
not a test here.
"""

import unittest

from betangel_research.betfair import p2_lp, pilot, pilot_gate
from betangel_research.betfair.receipts import utc_from_ns

import test_betfair_support_orders as so

DAY = "2027-01-04"
DAY_NS = pilot.parse_utc_ns(DAY + "T00:00:00Z")
ARM_NS = DAY_NS + 9 * 3600 * pilot.NS


def intent(seq, ref, source):
    return {"seq": seq, "kind": "intent", "customer_order_ref": ref, "recorded_at": utc_from_ns(ARM_NS + seq),
            "payload": {"source": source}}


def p2_leg(ref, **changes):
    return so.order_record(**{"customer_order_ref": ref, "customer_strategy_ref": "p2-lp", "mode": "live",
                              "submitted_at": utc_from_ns(ARM_NS + 60 * pilot.NS), **changes})


class PilotOneSeesPilotTwo(unittest.TestCase):
    def test_open_or_unsettled_p2_leg_is_a_pilot_1_manual_ref(self):
        events = [intent(1, "p2-back", "pilot_lp"), intent(2, "p2-lay", "pilot_lp"), intent(3, "p1", "pilot")]
        cases = {
            "resting": ({"state": "EXECUTABLE"}, ["p2-back"]),
            "unknown": ({"state": "UNKNOWN", "bet_id": None}, ["p2-back"]),
            "matched, not settled": ({"state": "EXECUTION_COMPLETE", "size_matched_p": 200}, ["p2-back"]),
            "pulled, nothing matched": ({"state": "EXECUTION_COMPLETE", "size_cancelled_p": 200}, []),
            "settled": ({"state": "SETTLED", "size_matched_p": 200}, []),
            "lapsed": ({"state": "LAPSED", "size_lapsed_p": 200}, []),
            "refused before its submit": ({"state": "REFUSED", "bet_id": None, "submitted_at": None}, []),
        }
        for label, (changes, expected) in cases.items():
            with self.subTest(case=label):
                orders = {"p2-back": p2_leg("p2-back", **changes),
                          "p1": so.order_record(customer_order_ref="p1", customer_strategy_ref="p1-fav-t5")}
                self.assertEqual(pilot_gate.open_manual_refs(events, orders), expected)
        both = {"p2-back": p2_leg("p2-back"), "p2-lay": p2_leg("p2-lay", side="LAY", price_h=305)}
        self.assertEqual(pilot_gate.open_manual_refs(events, both), ["p2-back", "p2-lay"])

    def test_p2_arm_record_is_no_pilot_note(self):
        payload = p2_lp.arm_payload(p2_lp.arm_line("c" * 12, DAY, "2000", 1), session_id="p2s-0a1b2c3d",
                                    config_sha256="c" * 64, utc_day=DAY, until_ns=DAY_NS + 20 * 3600 * pilot.NS,
                                    races=1, code_commit="d" * 40)
        events = [{"seq": 1, "kind": "operator_confirm", "recorded_at": utc_from_ns(ARM_NS), "payload": payload}]
        self.assertEqual(len(p2_lp.arm_records(events)), 1)
        self.assertNotIn("pilot", payload)
        self.assertIsNone(pilot_gate.latest_note(events, "session_start"))
        self.assertIsNone(pilot_gate.journal_contract_sha256(events))
        self.assertFalse(pilot_gate.session_window_open(events, now_ns=ARM_NS + pilot.NS))


if __name__ == "__main__":
    unittest.main()
