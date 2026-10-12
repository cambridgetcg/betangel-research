"""Lifecycle state machine and report classification on synthetic normalised reports."""

import unittest

from betangel_research.betfair import lifecycle
from betangel_research.betfair.errors import ApiError, JournalError, OrderRefused, TransportError

import test_betfair_support as support
import test_betfair_support_orders as orders

REF_A = "bro200101120000-000000000000000a"
REF_B = "bro200101120000-000000000000000b"
CHILD = "bro200101120000-00000000000000c1"


def item(ref, *, code=None, **changes):
    """One normalised place instruction report; code is its error_code."""
    value = orders.place_report(ref)["instructions"][0]
    value["error_code"] = code
    value.update(changes)
    return value


def states(result):
    return {key: value[0] for key, value in result.items()}


class TransitionTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_transition_table_complete(self):
        self.assertEqual(set(lifecycle.TRANSITIONS), set(lifecycle.STATES))
        for source, targets in lifecycle.TRANSITIONS.items():
            self.assertLessEqual(set(targets), set(lifecycle.STATES))
        for terminal in lifecycle.TERMINAL_STATES - {"REPLACED"}:
            self.assertEqual(lifecycle.TRANSITIONS[terminal], frozenset(), terminal)
        # Only the matched part of a replaced bet may still settle.
        self.assertEqual(lifecycle.TRANSITIONS["REPLACED"], {"SETTLED", "VOIDED"})
        expected = {
            ("RECORDED", "REFUSED"), ("RECORDED", "SUBMITTING"), ("SUBMITTING", "EXECUTABLE"),
            ("SUBMITTING", "EXECUTION_COMPLETE"), ("SUBMITTING", "FAILED"), ("SUBMITTING", "UNKNOWN"),
            ("SUBMITTING", "REFUSED"), ("UNKNOWN", "EXECUTABLE"), ("UNKNOWN", "EXECUTION_COMPLETE"),
            ("UNKNOWN", "LAPSED"), ("UNKNOWN", "ABSENT_UNCONFIRMED"), ("UNKNOWN", "UNKNOWN"),
            ("ABSENT_UNCONFIRMED", "EXECUTABLE"), ("ABSENT_UNCONFIRMED", "EXECUTION_COMPLETE"),
            ("ABSENT_UNCONFIRMED", "LAPSED"), ("ABSENT_UNCONFIRMED", "ABSENT_ACCEPTED"),
            ("ABSENT_UNCONFIRMED", "UNKNOWN"),
            ("EXECUTABLE", "EXECUTABLE"), ("EXECUTABLE", "EXECUTION_COMPLETE"), ("EXECUTABLE", "LAPSED"),
            ("EXECUTABLE", "CANCEL_PENDING"), ("EXECUTABLE", "REPLACE_PENDING"), ("EXECUTABLE", "SETTLED"),
            ("CANCEL_PENDING", "EXECUTABLE"),
            ("CANCEL_PENDING", "EXECUTION_COMPLETE"), ("CANCEL_PENDING", "UNKNOWN"), ("REPLACE_PENDING", "REPLACED"),
            ("REPLACE_PENDING", "EXECUTION_COMPLETE"), ("REPLACE_PENDING", "EXECUTABLE"),
            ("REPLACE_PENDING", "UNKNOWN"), ("EXECUTION_COMPLETE", "SETTLED"), ("EXECUTION_COMPLETE", "VOIDED"),
            ("REPLACED", "SETTLED"), ("REPLACED", "VOIDED")}
        actual = {(s, t) for s, targets in lifecycle.TRANSITIONS.items() for t in targets}
        self.assertEqual(actual, expected)
        self.assertEqual(lifecycle.UNRESOLVED_STATES,
                         {"SUBMITTING", "UNKNOWN", "ABSENT_UNCONFIRMED", "CANCEL_PENDING", "REPLACE_PENDING"})
        self.assertEqual(lifecycle.TERMINAL_STATES,
                         {"REFUSED", "FAILED", "ABSENT_ACCEPTED", "LAPSED", "REPLACED", "SETTLED", "VOIDED"})

    def test_executable_to_settled_keeps_remaining_and_counts_nothing(self):
        """Amendment A3 (D3): settle-market's EXECUTABLE -> SETTLED keeps the remaining size as recorded (it is
        booked at its worst case by the fold) and leaves no exposure; nothing leaves SETTLED."""
        from betangel_research.betfair.risk import adverse_contrib, exact_contrib, exposure_by_market
        partly = orders.order_record(mode="live", side="LAY", price_h=300, size_p=200, size_matched_p=50,
                                     average_price_matched_h=290)
        settled = lifecycle.transition(partly, "SETTLED", reason="operator_settle_market",
                                       fields={"settled_profit_own_p": None, "settled_profit_betfair_p": None})
        self.assertEqual((settled["state"], settled["size_matched_p"], settled["size_remaining_p"]),
                         ("SETTLED", 50, 150))
        self.assertEqual(settled["exposure_basis"], "none")
        for outcome in (12345, "other"):
            self.assertEqual((exact_contrib(settled, outcome), adverse_contrib(settled, outcome)), (0, 0))
        self.assertEqual(exposure_by_market([settled]), {"1.1": 0})
        for target in lifecycle.STATES:
            with self.subTest(target=target), self.assertRaises(JournalError):
                lifecycle.transition(settled, target, reason="test")

    def test_illegal_transition_raises(self):
        recorded = orders.order_record(state="RECORDED", size_remaining_p=0, bet_id=None)
        with self.assertRaisesRegex(JournalError, "illegal_transition"):
            lifecycle.transition(recorded, "EXECUTABLE", reason="test")
        failed = orders.order_record(state="FAILED", size_remaining_p=0, bet_id=None)
        for target in lifecycle.STATES:
            with self.subTest(target=target), self.assertRaises(JournalError):
                lifecycle.transition(failed, target, reason="test")
        executable = orders.order_record()
        for bad in ({"price_h": 345}, {"side": "LAY"}, {"customer_order_ref": "x"}, {"exposure_basis": "none"}):
            with self.subTest(bad=bad), self.assertRaises(JournalError):
                lifecycle.transition(executable, "EXECUTABLE", reason="test", fields=bad)
        for reason in ("", "has space", None, "x" * 81):
            with self.subTest(reason=reason), self.assertRaises(JournalError):
                lifecycle.transition(executable, "EXECUTABLE", reason=reason)
        with self.assertRaises(JournalError):
            lifecycle.transition(executable, "MATCHED", reason="test")
        # A field update that would break the record (more matched than size) is refused.
        with self.assertRaises(JournalError):
            lifecycle.transition(executable, "EXECUTABLE", reason="test", fields={"size_matched_p": 500})

    def test_transition_defaults_keep_unknown_adverse(self):
        recorded = orders.order_record(state="RECORDED", size_remaining_p=0, bet_id=None)
        submitting = lifecycle.transition(recorded, "SUBMITTING", reason="submit")
        self.assertEqual((submitting["size_remaining_p"], submitting["exposure_basis"]), (200, "adverse_unknown"))
        unknown = lifecycle.transition(submitting, "UNKNOWN", reason="report_ref_mismatch",
                                       fields={"size_matched_p": 50, "average_price_matched_h": 350})
        self.assertEqual(unknown["size_remaining_p"], 150)
        self.assertEqual(unknown["exposure_basis"], "adverse_unknown")
        self.assertEqual(unknown["error_code"], "report_ref_mismatch")
        complete = lifecycle.transition(unknown, "EXECUTION_COMPLETE", reason="reconcile",
                                        fields={"size_matched_p": 200})
        self.assertEqual((complete["size_remaining_p"], complete["exposure_basis"]), (0, "matched_exact"))
        lapsed = lifecycle.transition(orders.order_record(), "LAPSED", reason="cleared")
        self.assertEqual((lapsed["size_lapsed_p"], lapsed["size_remaining_p"], lapsed["exposure_basis"]),
                         (200, 0, "none"))
        child = orders.order_record(state="SUBMITTING", provisional_child=True, parent_ref=REF_B,
                                    customer_order_ref=CHILD)
        self.assertTrue(lifecycle.transition(child, "UNKNOWN", reason="t")["provisional_child"])
        self.assertFalse(lifecycle.transition(child, "EXECUTABLE", reason="t")["provisional_child"])
        self.assertFalse(lifecycle.transition(child, "FAILED", reason="t")["provisional_child"])

    def test_final_states_account_for_whole_size(self):
        """Regression: a move to EXECUTION_COMPLETE, LAPSED or REPLACED never lets unaccounted size
        vanish to zero exposure; out of an unresolved state the sizes must be stated."""
        sources = {"UNKNOWN": ("EXECUTION_COMPLETE", "LAPSED"), "ABSENT_UNCONFIRMED": ("EXECUTION_COMPLETE", "LAPSED"),
                   "CANCEL_PENDING": ("EXECUTION_COMPLETE",), "REPLACE_PENDING": ("EXECUTION_COMPLETE", "REPLACED"),
                   "SUBMITTING": ("EXECUTION_COMPLETE",), "EXECUTABLE": ("EXECUTION_COMPLETE",)}
        for source, targets in sources.items():
            order = orders.order_record(state=source, side="LAY", price_h=1000, size_p=500, bet_id="777")
            for target in targets:
                with self.subTest(source=source, target=target), self.assertRaisesRegex(
                        JournalError, "final_state_size_unaccounted"):
                    lifecycle.transition(order, target, reason="found")
        unknown = orders.order_record(state="UNKNOWN", size_p=500, size_matched_p=100, average_price_matched_h=350,
                                      bet_id="777")
        with self.assertRaisesRegex(JournalError, "final_state_size_unaccounted"):
            lifecycle.transition(unknown, "EXECUTION_COMPLETE", reason="found", fields={"size_matched_p": 300})
        complete = lifecycle.transition(unknown, "EXECUTION_COMPLETE", reason="found",
                                        fields={"size_matched_p": 300, "size_lapsed_p": 200})
        self.assertEqual((complete["size_remaining_p"], complete["exposure_basis"]), (0, "matched_exact"))
        nothing_matched = dict(unknown, size_matched_p=0, average_price_matched_h=None)
        lapsed = lifecycle.transition(nothing_matched, "LAPSED", reason="found_cleared", fields={"size_lapsed_p": 500})
        self.assertEqual((lapsed["size_lapsed_p"], lapsed["size_remaining_p"]), (500, 0))
        # The known remainder of an EXECUTABLE order may lapse by default; final states keep nothing open.
        self.assertEqual(lifecycle.transition(orders.order_record(), "LAPSED", reason="r")["size_lapsed_p"], 200)
        with self.assertRaisesRegex(JournalError, "final_state_size_unaccounted"):
            lifecycle.transition(orders.order_record(), "EXECUTION_COMPLETE", reason="r",
                                 fields={"size_matched_p": 100, "average_price_matched_h": 350,
                                         "size_remaining_p": 100})

    def test_lapsed_never_carries_matched_size(self):
        """Regression: LAPSED cannot settle, so a partly matched order finishes as EXECUTION_COMPLETE."""
        partly = orders.order_record(size_matched_p=100, average_price_matched_h=350)
        with self.assertRaisesRegex(JournalError, "lapsed_with_matched_size"):
            lifecycle.transition(partly, "LAPSED", reason="cleared")
        done = lifecycle.transition(partly, "EXECUTION_COMPLETE", reason="cleared",
                                    fields={"size_lapsed_p": 100, "size_remaining_p": 0})
        self.assertEqual(lifecycle.transition(done, "SETTLED", reason="settled")["state"], "SETTLED")
        unknown = orders.order_record(state="UNKNOWN", size_matched_p=100, average_price_matched_h=350)
        with self.assertRaisesRegex(JournalError, "lapsed_with_matched_size"):
            lifecycle.transition(unknown, "LAPSED", reason="cleared", fields={"size_lapsed_p": 100})


class PlaceReportTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def classify(self, items, *, sent=(REF_A,), status="SUCCESS", code=None, source="betfair"):
        report = orders.report_envelope(items, status=status, code=code, source=source)
        return lifecycle.classify_place_report(report, sent_refs=list(sent))

    def test_timeout_unknown(self):
        self.assertEqual(states(self.classify([item(REF_A, status="TIMEOUT", order_status=None, bet_id=None)],
                                             status="TIMEOUT")), {REF_A: "UNKNOWN"})
        self.assertEqual(states(self.classify([], sent=(REF_A, REF_B), status="TIMEOUT")),
                         {REF_A: "UNKNOWN", REF_B: "UNKNOWN"})
        self.assertEqual(states(self.classify([], status="SUCCESS")), {REF_A: "UNKNOWN"})
        self.assertEqual(states(lifecycle.classify_place_report({"status": "SUCCESS"}, sent_refs=[REF_A])),
                         {REF_A: "UNKNOWN"})

    def test_processed_with_errors_per_instruction(self):
        result = self.classify([item(REF_A), item(REF_B, status="FAILURE", order_status=None, bet_id=None,
                                                  code="INVALID_ODDS")],
                               sent=(REF_A, REF_B), status="PROCESSED_WITH_ERRORS")
        self.assertEqual(states(result), {REF_A: "EXECUTABLE", REF_B: "FAILED"})
        self.assertEqual(result[REF_B][1]["error_code"], "INVALID_ODDS")
        self.assertEqual(result[REF_A][1]["bet_id"], "312345678901")

    def test_matched_size_never_failed(self):
        result = self.classify([item(REF_A, status="FAILURE", order_status="EXECUTABLE",
                                     size_matched_p=50, average_price_matched_h=350, code="INSUFFICIENT_FUNDS")],
                               status="FAILURE")
        self.assertEqual(states(result), {REF_A: "EXECUTABLE"})
        self.assertEqual(result[REF_A][1]["size_matched_p"], 50)
        result = self.classify([item(REF_A, status="SUCCESS", order_status=None, size_matched_p=50,
                                     average_price_matched_h=350)])
        self.assertEqual(states(result), {REF_A: "UNKNOWN"})
        self.assertEqual(result[REF_A][1]["size_matched_p"], 50)       # known minimum kept
        complete = self.classify([item(REF_A, order_status="EXECUTION_COMPLETE", size_matched_p=200,
                                       average_price_matched_h=355)])
        self.assertEqual(states(complete), {REF_A: "EXECUTION_COMPLETE"})

    def test_bet_id_echoed_on_failure_unknown(self):
        result = self.classify([item(REF_A, status="FAILURE", order_status=None, code="BET_TAKEN_OR_LAPSED")],
                               status="FAILURE", code="BET_ACTION_ERROR")
        self.assertEqual(states(result), {REF_A: "UNKNOWN"})
        self.assertEqual(result[REF_A][1]["bet_id"], "312345678901")
        no_code = self.classify([item(REF_A, status="FAILURE", order_status=None)], status="FAILURE")
        self.assertEqual(no_code[REF_A], ("UNKNOWN", {"bet_id": "312345678901", "size_matched_p": 0,
                                                      "average_price_matched_h": None,
                                                      "error_code": "bet_id_echoed_on_failure"}))

    def test_success_unexpected_order_status_unknown(self):
        for status in (None, "PENDING", "EXPIRED", "SOMETHING_NEW"):
            with self.subTest(status=status):
                result = self.classify([item(REF_A, order_status=status)])
                self.assertEqual(states(result), {REF_A: "UNKNOWN"})
        # Complete but with no matched size stated: possibly fully matched, so never counted as zero.
        missing = self.classify([item(REF_A, order_status="EXECUTION_COMPLETE", size_matched_p=None)])
        self.assertEqual(missing[REF_A][0], "UNKNOWN")
        self.assertEqual(missing[REF_A][1]["error_code"], "matched_size_missing")
        # Complete with nothing matched does not say where the size went: UNKNOWN until reconciled.
        zero = self.classify([item(REF_A, order_status="EXECUTION_COMPLETE", size_matched_p=0)])
        self.assertEqual(zero[REF_A][0], "UNKNOWN")
        self.assertEqual(zero[REF_A][1]["error_code"], "complete_size_unaccounted")

    def test_success_without_bet_id_unknown(self):
        """Regression: a success (or matched size) with no betId could never be cancelled or confirmed
        by betId, so it is UNKNOWN and reconciled by customerOrderRef."""
        result = self.classify([item(REF_A, bet_id=None)])
        self.assertEqual(result[REF_A][0], "UNKNOWN")
        self.assertEqual(result[REF_A][1]["error_code"], "bet_id_missing_on_success")
        matched = self.classify([item(REF_A, bet_id=None, size_matched_p=50, average_price_matched_h=350)])
        self.assertEqual(matched[REF_A][0], "UNKNOWN")
        self.assertEqual(matched[REF_A][1]["size_matched_p"], 50)      # known minimum kept
        complete = self.classify([item(REF_A, bet_id=None, order_status="EXECUTION_COMPLETE", size_matched_p=200,
                                       average_price_matched_h=350)])
        self.assertEqual(complete[REF_A][0], "UNKNOWN")

    def test_complete_must_match_whole_order_when_known(self):
        submitting = orders.order_record(customer_order_ref=REF_A, state="SUBMITTING", bet_id=None)
        report = orders.report_envelope([item(REF_A, order_status="EXECUTION_COMPLETE", size_matched_p=120,
                                              average_price_matched_h=350)])
        short = lifecycle.classify_place_report(report, sent_refs=[REF_A], orders={REF_A: submitting})
        self.assertEqual(short[REF_A][0], "UNKNOWN")
        self.assertEqual(short[REF_A][1]["error_code"], "complete_size_unaccounted")
        moved = lifecycle.transition(submitting, *short[REF_A][:1], reason="place_report", fields=short[REF_A][1])
        self.assertEqual((moved["size_matched_p"], moved["size_remaining_p"]), (120, 80))
        full = orders.report_envelope([item(REF_A, order_status="EXECUTION_COMPLETE", size_matched_p=200,
                                             average_price_matched_h=350)])
        self.assertEqual(lifecycle.classify_place_report(full, sent_refs=[REF_A], orders={REF_A: submitting})
                         [REF_A][0], "EXECUTION_COMPLETE")
        over = orders.report_envelope([item(REF_A, size_matched_p=500, average_price_matched_h=350)])
        result = lifecycle.classify_place_report(over, sent_refs=[REF_A], orders={REF_A: submitting})
        self.assertEqual(result[REF_A], ("UNKNOWN", {"bet_id": "312345678901",
                                                     "error_code": "matched_size_exceeds_order"}))
        lifecycle.transition(submitting, "UNKNOWN", reason="place_report", fields=result[REF_A][1])

    def test_failure_without_instruction_reports(self):
        for code in ("MARKET_SUSPENDED", "INSUFFICIENT_FUNDS", "INVALID_MARKET_VERSION"):
            with self.subTest(code=code):
                result = self.classify([], sent=(REF_A, REF_B), status="FAILURE", code=code)
                self.assertEqual(states(result), {REF_A: "FAILED", REF_B: "FAILED"})
                self.assertEqual(result[REF_A][1]["error_code"], code)
        for code in ("SOMETHING_NEW", "ERROR_IN_MATCHER", None):
            with self.subTest(code=code):
                self.assertEqual(states(self.classify([], status="FAILURE", code=code)), {REF_A: "UNKNOWN"})

    def test_market_version_lapse_failed_market_state(self):
        result = self.classify([item(REF_A, status="FAILURE", order_status=None, bet_id=None,
                                     code="BET_TAKEN_OR_LAPSED")], status="FAILURE", code="BET_ACTION_ERROR")
        self.assertEqual(result[REF_A][0], "FAILED")
        self.assertEqual(result[REF_A][1]["error_code"], "BET_TAKEN_OR_LAPSED")
        # A failed instruction without its own code takes a definitive execution code.
        result = self.classify([item(REF_A, status="FAILURE", order_status=None, bet_id=None)],
                               status="FAILURE", code="MARKET_NOT_OPEN_FOR_BETTING")
        self.assertEqual(result[REF_A], ("FAILED", {"bet_id": None, "size_matched_p": 0,
                                                    "average_price_matched_h": None,
                                                    "error_code": "MARKET_NOT_OPEN_FOR_BETTING"}))
        # A non-definitive instruction code keeps it UNKNOWN even under a definitive execution code.
        result = self.classify([item(REF_A, status="FAILURE", order_status=None, bet_id=None,
                                     code="ERROR_IN_ORDER")], status="FAILURE", code="MARKET_SUSPENDED")
        self.assertEqual(result[REF_A][0], "UNKNOWN")

    def test_report_matched_by_echoed_ref(self):
        result = self.classify([item(REF_B, bet_id="2"), item(REF_A, bet_id="1")], sent=(REF_A, REF_B))
        self.assertEqual(result[REF_A][1]["bet_id"], "1")
        self.assertEqual(result[REF_B][1]["bet_id"], "2")
        positional = self.classify([item(None, bet_id="1"), item(None, bet_id="2")], sent=(REF_A, REF_B))
        self.assertEqual((positional[REF_A][1]["bet_id"], positional[REF_B][1]["bet_id"]), ("1", "2"))

    def test_ref_mismatch_all_unknown(self):
        cases = {"unknown echo": [item("bro200101120000-00000000000000ff"), item(REF_B)],
                 "count": [item(REF_A)],
                 "duplicate echo": [item(REF_A), item(REF_A)],
                 "echo collides with position": [item(None), item(REF_A)]}
        for name, items in cases.items():
            with self.subTest(name=name):
                result = self.classify(items, sent=(REF_A, REF_B))
                self.assertEqual(states(result), {REF_A: "UNKNOWN", REF_B: "UNKNOWN"})
                self.assertEqual(result[REF_A][1]["error_code"], "report_ref_mismatch")

    def test_duplicate_transaction_unknown(self):
        self.assertEqual(states(self.classify([], status="FAILURE", code="DUPLICATE_TRANSACTION")), {REF_A: "UNKNOWN"})
        for code in ("ERROR_IN_MATCHER", "BET_IN_PROGRESS", "TIMEOUT", "SOMETHING_NEW"):
            with self.subTest(code=code):
                result = self.classify([item(REF_A, status="FAILURE", order_status=None, bet_id=None, code=code)],
                                       status="FAILURE")
                self.assertEqual(states(result), {REF_A: "UNKNOWN"})

    def test_paper_codes_only_from_paper_reports(self):
        failure = item(REF_A, status="FAILURE", order_status=None, bet_id=None, code="paper_book_stale")
        self.assertEqual(states(self.classify([failure], status="FAILURE", source="paper-v1")), {REF_A: "REFUSED"})
        self.assertEqual(states(self.classify([failure], status="FAILURE", source="betfair")), {REF_A: "UNKNOWN"})
        market = dict(failure, error_code="paper_rejected_market_state")
        self.assertEqual(states(self.classify([market], status="FAILURE", source="paper-v1")), {REF_A: "FAILED"})


class CancelReplaceReportTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.order = orders.order_record(state="CANCEL_PENDING", size_remaining_p=200, bet_id="5")

    def cancel(self, items, *, status="SUCCESS", code=None, with_orders=True):
        report = orders.report_envelope(items, status=status, code=code)
        return lifecycle.classify_cancel_report(report, sent_bet_ids=["5"],
                                                orders={"5": self.order} if with_orders else None)

    def test_cancel_bet_taken_or_lapsed_refresh(self):
        self.assertEqual(self.cancel([orders.cancel_item("5", status="FAILURE", code="BET_TAKEN_OR_LAPSED")],
                                     status="FAILURE")["5"][0], "UNKNOWN")
        full = self.cancel([orders.cancel_item("5", cancelled=200)])
        self.assertEqual(full["5"], ("EXECUTION_COMPLETE", {"size_cancelled_p": 200, "size_remaining_p": 0,
                                                            "error_code": None}))
        partial = self.cancel([orders.cancel_item("5", cancelled=50, reduction=50)])
        self.assertEqual(partial["5"], ("EXECUTABLE", {"size_cancelled_p": 50, "size_remaining_p": 150,
                                                       "error_code": None}))
        short = self.cancel([orders.cancel_item("5", cancelled=120)])
        self.assertEqual(short["5"][0], "UNKNOWN")
        failed = self.cancel([orders.cancel_item("5", status="FAILURE", code="INVALID_BET_ID")], status="FAILURE")
        self.assertEqual(failed["5"], ("EXECUTABLE", {"size_remaining_p": 200, "error_code": "INVALID_BET_ID"}))
        self.assertEqual(self.cancel([orders.cancel_item("5", status="TIMEOUT")], status="TIMEOUT")["5"][0],
                         "UNKNOWN")
        self.assertEqual(self.cancel([orders.cancel_item("5", cancelled=200)], with_orders=False)["5"][0], "UNKNOWN")
        self.assertEqual(self.cancel([], status="FAILURE", code="MARKET_SUSPENDED")["5"][0], "EXECUTABLE")
        self.assertEqual(self.cancel([], status="TIMEOUT")["5"][0], "UNKNOWN")
        self.assertEqual(self.cancel([orders.cancel_item("6", cancelled=200)])["5"][1]["error_code"],
                         "report_ref_mismatch")
        # Regression: more cancelled than was open is inconsistent; the move carries no sizes at all.
        over = self.cancel([orders.cancel_item("5", cancelled=250)])
        self.assertEqual(over["5"], ("UNKNOWN", {"error_code": "cancel_size_inconsistent_refresh_needed"}))
        lifecycle.transition(self.order, *over["5"][:1], reason="cancel_report", fields=over["5"][1])
        lifecycle.transition(self.order, *short["5"][:1], reason="cancel_report", fields=short["5"][1])
        # Transactions: a successful cancel is free, a failed or unknown one counts.
        ok = orders.report_envelope([orders.cancel_item("5", cancelled=200)])
        bad = orders.report_envelope([orders.cancel_item("5", status="FAILURE", code="INVALID_BET_ID")],
                                     status="FAILURE")
        self.assertEqual(lifecycle.report_transactions("cancel", ok), 0)
        self.assertEqual(lifecycle.report_transactions("cancel", bad), 1)
        self.assertEqual(lifecycle.report_transactions("cancel", None, sent_count=3), 3)
        self.assertEqual(lifecycle.report_transactions("place", bad), 0)

    def test_sweep_cancel_of_unresolved_order_stays_unknown(self):
        """A kill-sweep cancel of an order that was already unresolved never sizes it from the report:
        every outcome is UNKNOWN, which is a legal move from each unresolved state."""
        for state in ("UNKNOWN", "ABSENT_UNCONFIRMED", "REPLACE_PENDING"):
            unresolved = orders.order_record(state=state, size_remaining_p=200, bet_id="5")
            for cancel_item, status in ((orders.cancel_item("5", cancelled=200), "SUCCESS"),
                                        (orders.cancel_item("5", status="FAILURE", code="INVALID_BET_ID"), "FAILURE")):
                with self.subTest(state=state, status=status):
                    report = orders.report_envelope([cancel_item], status=status)
                    result = lifecycle.classify_cancel_report(report, sent_bet_ids=["5"], orders={"5": unresolved})
                    self.assertEqual(result["5"][0], "UNKNOWN")
                    moved = lifecycle.transition(unresolved, "UNKNOWN", reason="sweep_cancel", fields=result["5"][1])
                    self.assertEqual(moved["exposure_basis"], "adverse_unknown")
        report = orders.report_envelope([], status="FAILURE", code="MARKET_SUSPENDED")
        unknown = orders.order_record(state="UNKNOWN", size_remaining_p=200, bet_id="5")
        self.assertEqual(lifecycle.classify_cancel_report(report, sent_bet_ids=["5"], orders={"5": unknown})["5"][0],
                         "UNKNOWN")

    def replace(self, item_changes=None, *, status="SUCCESS", cancel=None, place=None):
        parent = orders.order_record(state="REPLACE_PENDING", size_remaining_p=200, bet_id="5", child_ref=CHILD)
        report_item = {"bet_id": "5", "new_price_h": 345, "status": status, "error_code": None,
                       "cancel": orders.cancel_item("5", cancelled=200) if cancel is None else cancel,
                       "place": item(None, bet_id="6") if place is None else place}
        report_item.update(item_changes or {})
        report = orders.report_envelope([report_item], status=status)
        return lifecycle.classify_replace_report(report, sent=[{"bet_id": "5", "new_price_h": 345, "order": parent}],
                                                 provisional_children={orders.REF: CHILD})

    def test_cancelled_not_placed(self):
        success = self.replace()
        self.assertEqual(states(success), {orders.REF: "REPLACED", CHILD: "EXECUTABLE"})
        self.assertEqual(success[CHILD][1]["bet_id"], "6")
        self.assertIs(success[CHILD][1]["provisional_child"], False)
        not_placed = self.replace({"error_code": "CANCELLED_NOT_PLACED"}, status="FAILURE",
                                  place=item(None, status="FAILURE", order_status=None, bet_id=None,
                                             code="CANCELLED_NOT_PLACED"))
        self.assertEqual(states(not_placed), {orders.REF: "EXECUTION_COMPLETE", CHILD: "FAILED"})
        self.assertEqual(not_placed[orders.REF][1]["size_cancelled_p"], 200)
        cancel_failed = self.replace({"error_code": "INVALID_BET_ID"}, status="FAILURE",
                                     cancel=orders.cancel_item("5", status="FAILURE", code="INVALID_BET_ID"),
                                     place=item(None, status="FAILURE", order_status=None, bet_id=None))
        self.assertEqual(states(cancel_failed), {orders.REF: "EXECUTABLE", CHILD: "FAILED"})
        timeout = self.replace({"cancel": None, "place": None}, status="TIMEOUT")
        self.assertEqual(states(timeout), {orders.REF: "UNKNOWN", CHILD: "UNKNOWN"})
        short_cancel = self.replace(cancel=orders.cancel_item("5", cancelled=150))
        self.assertEqual(states(short_cancel), {orders.REF: "UNKNOWN", CHILD: "UNKNOWN"})
        placed_but_failed = self.replace({"error_code": "CANCELLED_NOT_PLACED"}, status="FAILURE",
                                         place=item(None, status="FAILURE", order_status=None, bet_id="7",
                                                    code="CANCELLED_NOT_PLACED"))
        self.assertEqual(states(placed_but_failed), {orders.REF: "UNKNOWN", CHILD: "UNKNOWN"})
        complete_unsized = self.replace(place=item(None, bet_id="6", order_status="EXECUTION_COMPLETE",
                                                   size_matched_p=None))
        self.assertEqual(states(complete_unsized), {orders.REF: "UNKNOWN", CHILD: "UNKNOWN"})
        wrong_price = self.replace({"new_price_h": 340})
        self.assertEqual(states(wrong_price), {orders.REF: "UNKNOWN", CHILD: "UNKNOWN"})
        complete_short = self.replace(place=item(None, bet_id="6", order_status="EXECUTION_COMPLETE",
                                                 size_matched_p=150, average_price_matched_h=345))
        self.assertEqual(states(complete_short), {orders.REF: "UNKNOWN", CHILD: "UNKNOWN"})

    def test_replace_without_provisional_child_never_replaced(self):
        """Regression: a successful replace whose parent has no provisional child would leave the new
        live bet untracked; the parent stays UNKNOWN instead of REPLACED."""
        parent = orders.order_record(state="REPLACE_PENDING", size_remaining_p=200, bet_id="5")
        report = orders.report_envelope([{"bet_id": "5", "new_price_h": 345, "status": "SUCCESS", "error_code": None,
                                          "cancel": orders.cancel_item("5", cancelled=200),
                                          "place": item(None, bet_id="6")}])
        result = lifecycle.classify_replace_report(report, sent=[{"bet_id": "5", "new_price_h": 345,
                                                                  "order": parent}], provisional_children={})
        self.assertEqual(result, {orders.REF: ("UNKNOWN", {"error_code": "replace_child_untracked"})})
        moved = lifecycle.transition(parent, "UNKNOWN", reason="replace_report", fields=result[orders.REF][1])
        self.assertEqual(moved["exposure_basis"], "adverse_unknown")
        self.assertEqual(lifecycle.report_transactions("replace", orders.report_envelope(
            [{"bet_id": "5", "cancel": orders.cancel_item("5", status="FAILURE", code="X")}])), 1)


class ExceptionOutcomeTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_outcome_from_exception(self):
        self.assertEqual(lifecycle.outcome_from_exception(TransportError("x", phase="connect")), "FAILED_NOT_SENT")
        self.assertEqual(lifecycle.outcome_from_exception(TransportError("x", phase="preflight")), "FAILED_NOT_SENT")
        self.assertEqual(lifecycle.outcome_from_exception(TransportError("x", phase="send")), "UNKNOWN")
        self.assertEqual(lifecycle.outcome_from_exception(TransportError("x", phase="receive")), "UNKNOWN")
        self.assertEqual(lifecycle.outcome_from_exception(
            TransportError("x", phase="send", may_have_arrived=False)), "FAILED_NOT_SENT")
        for code, surface in (("NO_SESSION", "aping"), ("INVALID_APP_KEY", "aping"), ("TOO_MANY_REQUESTS", "aping"),
                              ("TOO_MUCH_DATA", "aping"), ("ACCESS_DENIED", "aping"), ("-32602", "jsonrpc")):
            with self.subTest(code=code):
                self.assertEqual(lifecycle.outcome_from_exception(ApiError(code, surface=surface)), "FAILED_REJECTED")
        for code, surface in (("TIMEOUT_ERROR", "aping"), ("ANGX-0003", "angx"), ("HTTP_503", "http"),
                              ("-32603", "jsonrpc"), ("NEW_CODE", "aping")):
            with self.subTest(code=code):
                self.assertEqual(lifecycle.outcome_from_exception(ApiError(code, surface=surface)), "UNKNOWN")
        self.assertEqual(lifecycle.outcome_from_exception(OrderRefused("capability_expired")), "FAILED_NOT_SENT")
        # api.send_order's structural refusals both come before the transport (amendment A3, C2): nothing sent.
        for rule in ("capability_send_bound_required", "send_bound_not_for_this_method"):
            self.assertEqual(lifecycle.outcome_from_exception(OrderRefused(rule)), "FAILED_NOT_SENT")
        self.assertEqual(lifecycle.outcome_from_exception(OrderRefused("something_else")), "UNKNOWN")
        self.assertEqual(lifecycle.outcome_from_exception(RuntimeError("x")), "UNKNOWN")
        self.assertEqual(lifecycle.halt_reason("terminal_input"), "validator_gap")
        self.assertEqual(lifecycle.halt_reason("planner_bug"), "validator_gap")
        self.assertEqual(lifecycle.halt_reason("unrecognized"), "unrecognized")
        self.assertIsNone(lifecycle.halt_reason("market_state"))


if __name__ == "__main__":
    unittest.main()
