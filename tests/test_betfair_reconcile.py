"""Reconciliation of unknown order outcomes, startup reconciliation and settle-market (settling a CLOSED
market by hand), against a simulated exchange. Offline and synthetic: these show the decision rules (a
read error is never "absent", nothing is resent, exchange evidence never lowers a live order's counted
worst case), not Betfair's real visibility timings."""

import contextlib
import io
import json
import time
import unittest
from unittest.mock import patch

from betangel_research.betfair import reconcile
from betangel_research.betfair.arming import dynamic_arming
from betangel_research.betfair.errors import ApiError, LockBusy, NotArmed, OrderRefused, TransportError
from betangel_research.betfair.lifecycle import transition
from betangel_research.betfair.paths import file_lock
from betangel_research.betfair.reconcile import reconcile_unknown, startup_reconcile
from betangel_research.betfair.receipts import canonical_sha256, parse_utc_ns, utc_from_ns
from betangel_research.betfair.risk import exposure_by_market
from betangel_research.betfair.secret import Secret

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_orders as orders

REF = orders.REF
CFG = {"orders": {"reconcile": {"poll_interval_s": 1.0, "window_s": 15, "grace_s": 5, "min_successful_polls": 3,
                                "cleared_lookback_s": 3600}}}


def moved(order, result):
    """The order record after a reconcile result's move (as the journal fold applies it)."""
    state, fields = result["transition"]
    return transition(order, state, reason=result["reason"], fields=fields)


def matched_place(exchange, *, price=3.5):
    """A place handler that fills every instruction in full at price (and reports it so)."""
    def handler(params):
        exchange.place_handler = None
        try:
            result = exchange.place(params)
        finally:
            exchange.place_handler = handler
        for report in result["instructionReports"]:
            order = exchange.orders[report["betId"]]
            size = order["priceSize"]["size"]
            order.update(status="EXECUTION_COMPLETE", sizeMatched=size, sizeRemaining=0.0, averagePriceMatched=price)
            report.update(sizeMatched=size, averagePriceMatched=price, orderStatus="EXECUTION_COMPLETE")
        return result
    return handler


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.clock = support.FakeClock()
        self.exchange = gw.SimExchange(self.clock)
        self.read = gw.FakeReadClient(self.exchange)

    def unknown(self, **changes):
        now = self.clock.utc_now()
        values = {"state": "UNKNOWN", "bet_id": None, "mode": "live", "submitted_at": now, "outcome_observed_at": now,
                  "price_h": 350, "size_p": 200, "selection_id": gw.SELECTION}
        values.update(changes)
        return orders.order_record(**values)

    def run_reconcile(self, order, **kw):
        return reconcile_unknown(self.read, order, cfg=CFG, clock=self.clock, strategy_refs=["bar-manual"], **kw)

    def poll_calls(self):
        return [c for c in self.exchange.calls if c[0] == "listCurrentOrders" and c[1].get("customer_order_refs")]

    def test_found_executable_in_window(self):
        self.exchange.add_order(ref=REF, strategy_ref="bar-manual", price=3.5, size=2.0, matched=0.5, average=3.5)
        result = self.run_reconcile(self.unknown())
        self.assertEqual(result["final_state"], "EXECUTABLE")
        self.assertEqual(result["reason"], "found_by_ref")
        state, fields = result["transition"]
        self.assertEqual(state, "EXECUTABLE")
        self.assertEqual(fields["size_matched_p"], 50)
        self.assertEqual(fields["size_remaining_p"], 150)
        self.assertEqual(fields["average_price_matched_h"], 350)
        self.assertEqual(len(self.poll_calls()), 1)
        payload = result["events"][0]["payload"]
        self.assertEqual(payload["transitions"][0]["state"], "EXECUTABLE")
        self.assertIsNone(result["halt"])

    def test_found_complete(self):
        self.exchange.add_order(ref=REF, price=3.5, size=2.0, matched=2.0, average=3.6, status="EXECUTION_COMPLETE")
        result = self.run_reconcile(self.unknown())
        self.assertEqual(result["final_state"], "EXECUTION_COMPLETE")
        self.assertEqual(result["transition"][1]["average_price_matched_h"], 360)
        # A LAY's odd average is rounded up (against us); a BACK's down.
        self.assertEqual(reconcile.average_price_h(3.4567, "LAY"), 346)
        self.assertEqual(reconcile.average_price_h(3.4567, "BACK"), 345)
        # Sizes that do not add up, or other terms, are never taken: the order stays UNKNOWN.
        self.exchange.orders.clear()
        self.exchange.add_order(ref=REF, price=3.55, size=2.0)
        mismatch = self.run_reconcile(self.unknown())
        self.assertEqual(mismatch["final_state"], "UNKNOWN")
        self.assertEqual(mismatch["halt"], {"reason": "found_order_inconsistent", "scope": "market"})

    def test_cleared_lapsed(self):
        """Amendment A3 (D3, the live evidence rule): a cleared LAPSED row only establishes that the bet
        exists. The order gets its betId and stays EXECUTABLE at its worst case; it is never LAPSED."""
        self.exchange.cleared["LAPSED"].append({"betId": "300000000099", "marketId": "1.1", "customerOrderRef": REF,
                                                "sizeCancelled": 2.0})
        result = self.run_reconcile(self.unknown())
        self.assertEqual((result["final_state"], result["reason"]), ("EXECUTABLE", "cleared_row_bet_exists_worst_case"))
        state, fields = result["transition"]
        self.assertEqual({k: fields[k] for k in ("bet_id", "size_remaining_p", "error_code")},
                         {"bet_id": "300000000099", "size_remaining_p": 200,
                          "error_code": "cleared_row_bet_exists_worst_case"})
        self.assertNotIn("size_lapsed_p", fields)
        self.assertFalse(result["clean"])
        self.assertGreaterEqual(len(self.poll_calls()), 3)
        cleared = [c[1]["bet_status"] for c in self.exchange.calls if c[0] == "listClearedOrders"]
        self.assertEqual(cleared, ["LAPSED", "CANCELLED", "SETTLED", "VOIDED"])
        # A matched order found only in cleared LAPSED keeps its matched part and its unmatched worst case.
        matched = self.unknown(size_matched_p=50, average_price_matched_h=350)
        result = self.run_reconcile(matched)
        self.assertEqual(result["final_state"], "EXECUTABLE")
        after = moved(matched, result)
        self.assertEqual((after["size_matched_p"], after["size_remaining_p"], after["size_lapsed_p"]), (50, 150, 0))
        self.assertEqual(exposure_by_market([after]), exposure_by_market([matched]))

    def test_cleared_rows_never_turn_a_matched_part_into_zero(self):
        """Partial, unsized, SETTLED or mixed cleared rows all only establish the bet (EXECUTABLE, worst case);
        rows that disagree with each other or with the order are never used (still UNKNOWN)."""
        self.exchange.cleared["LAPSED"].append({"betId": "300000000099", "marketId": "1.1", "customerOrderRef": REF,
                                                "sizeCancelled": 1.0})
        partial = self.run_reconcile(self.unknown())
        self.assertEqual(partial["final_state"], "EXECUTABLE")
        self.assertEqual(partial["transition"][1]["size_remaining_p"], 200)
        self.exchange.cleared["LAPSED"][0].pop("sizeCancelled")
        self.assertEqual(self.run_reconcile(self.unknown())["final_state"], "EXECUTABLE")
        self.exchange.cleared["LAPSED"][0]["sizeCancelled"] = 1.0
        self.exchange.cleared["SETTLED"].append({"betId": "300000000099", "marketId": "1.1", "customerOrderRef": REF,
                                                 "sizeSettled": 1.0, "priceMatched": 3.5, "profit": -1.0})
        sized = self.run_reconcile(self.unknown())
        self.assertEqual(sized["final_state"], "EXECUTABLE")
        fields = sized["transition"][1]
        self.assertNotIn("size_matched_p", fields)                # sizes are untouched: worst case kept
        self.assertEqual(fields["size_remaining_p"], 200)
        # A fully matched order whose market settled before reconciliation is found, never "absent".
        self.exchange.cleared = {"SETTLED": [{"betId": "300000000099", "marketId": "1.1", "customerOrderRef": REF,
                                              "sizeSettled": 2.0, "priceMatched": 3.5, "profit": 5.0}],
                                 "VOIDED": [], "LAPSED": [], "CANCELLED": []}
        settled = self.run_reconcile(self.unknown())
        self.assertEqual(settled["final_state"], "EXECUTABLE")
        self.assertFalse(settled["clean"])
        # Rows that disagree with each other or with the order are never used.
        self.exchange.cleared["VOIDED"].append({"betId": "300000000098", "marketId": "1.1", "customerOrderRef": REF,
                                                "sizeSettled": 2.0})
        mixed = self.run_reconcile(self.unknown())
        self.assertEqual((mixed["final_state"], mixed["reason"]), ("UNKNOWN", "cleared_rows_inconsistent"))
        row = {"betId": "300000000099", "marketId": "1.1", "customerOrderRef": REF}
        self.assertEqual(reconcile._cleared_bet_id(self.unknown(bet_id="300000000097"), {"SETTLED": [row]}),
                         (None, "cleared_rows_inconsistent"))
        self.assertEqual(reconcile._cleared_bet_id(self.unknown(), {"LAPSED": [dict(row, betId="12a")]}),
                         (None, "cleared_rows_inconsistent"))
        self.assertEqual(reconcile._cleared_bet_id(self.unknown(), {"SETTLED": [row], "LAPSED": [row]}),
                         ("300000000099", None))
        self.assertEqual(reconcile._cleared_bet_id(self.unknown(), {}), (None, None))

    def test_float_artefact_sizes_are_their_penny(self):
        """Regression (audit): every response size had to be an exact 2-dp double, but Betfair sends raw
        doubles (its order-placement example: "sizeMatched":3.4400000000000004), so a routine multi-fill match
        left the order UNKNOWN on every path that could resolve it."""
        from betangel_research.betfair.gateway import normalise_cancel_report, normalise_place_report
        self.assertEqual(reconcile.pence(3.4400000000000004), 344)
        self.assertEqual(reconcile.pence(1.5599999999999996), 156)
        self.assertEqual(reconcile.pence(0.1 + 0.2), 30)
        for not_a_penny in (1.005, 3.441, -0.01, float("nan"), True, "3.44"):
            self.assertIsNone(reconcile.pence(not_a_penny))
        self.assertEqual(reconcile.profit_pence(-3.4400000000000004), -344)
        self.assertEqual(reconcile.profit_pence(-0.005), -1)                 # not a penny: floored against us
        # The documented FILL_OR_KILL example response, with its orderStatus.
        report = {"status": "SUCCESS", "marketId": "1.1", "instructionReports": [{
            "status": "SUCCESS", "instruction": {"selectionId": gw.SELECTION, "handicap": 0, "side": "BACK",
                                                 "orderType": "LIMIT", "customerOrderRef": REF,
                                                 "limitOrder": {"size": 5.0, "price": 21.0, "persistenceType": "LAPSE"}},
            "betId": "300000000011", "placedDate": "2020-01-01T12:00:00.000Z",
            "averagePriceMatched": 21.32267441860465, "sizeMatched": 3.4400000000000004,
            "orderStatus": "EXECUTABLE"}]}
        normalised = normalise_place_report(report, market_id="1.1", sent=[{"customer_order_ref": REF, "side": "BACK"}])
        self.assertIsNotNone(normalised)
        self.assertEqual(normalised["instructions"][0]["size_matched_p"], 344)
        order = orders.order_record(mode="live", state="UNKNOWN", bet_id="300000000011", price_h=2100, size_p=500)
        item = {"betId": "300000000011", "marketId": "1.1", "selectionId": gw.SELECTION, "side": "BACK",
                "priceSize": {"price": 21.0, "size": 5.000000000000001}, "status": "EXECUTABLE",
                "averagePriceMatched": 21.32267441860465, "sizeMatched": 3.4400000000000004,
                "sizeRemaining": 1.5599999999999996, "sizeLapsed": 0.0, "sizeCancelled": 0.0, "sizeVoided": 0.0}
        state, fields = reconcile.found_move(item, order)
        self.assertEqual((state, fields["size_matched_p"], fields["size_remaining_p"]), ("EXECUTABLE", 344, 156))
        self.assertEqual(reconcile.found_move(dict(item, sizeMatched=5.000000000000001, sizeRemaining=0.0,
                                                   status="EXECUTION_COMPLETE"), order)[0], "EXECUTION_COMPLETE")
        cancel = normalise_cancel_report({"status": "SUCCESS", "marketId": "1.1", "instructionReports": [{
            "status": "SUCCESS", "instruction": {"betId": "300000000011"}, "sizeCancelled": 1.5599999999999996,
            "cancelledDate": "2020-01-01T12:00:01.000Z"}]}, market_id="1.1")
        self.assertEqual(cancel["instructions"][0]["size_cancelled_p"], 156)

    def test_voided_fragment_is_part_of_the_size_identity(self):
        """Regression (audit): found_move left sizeVoided out of matched + remaining + lapsed + cancelled ==
        size, so any current order with a voided part (Betfair's VAR example: sm 2, sv 0 becomes sm 0, sv 2)
        was inconsistent forever."""
        order = orders.order_record(mode="live", state="UNKNOWN", bet_id="300000000012", size_p=200,
                                    side="LAY", price_h=200)
        base = {"betId": "300000000012", "marketId": "1.1", "selectionId": gw.SELECTION, "side": "LAY",
                "priceSize": {"price": 2.0, "size": 2.0}, "status": "EXECUTION_COMPLETE", "averagePriceMatched": 2.0,
                "sizeRemaining": 0.0, "sizeLapsed": 0.0, "sizeCancelled": 0.0}
        before = reconcile.found_move(dict(base, sizeMatched=2.0, sizeVoided=0.0), order)
        self.assertEqual(before[0], "EXECUTION_COMPLETE")
        # A void the journal did not know is withheld until settle-market (amendment A3, D3): the voided
        # size is never taken from the exchange and the order stays EXECUTABLE at its worst case.
        state, fields = reconcile.found_move(dict(base, sizeMatched=0.0, sizeVoided=2.0), order)
        self.assertEqual((state, fields["size_matched_p"], fields["size_voided_p"], fields["size_remaining_p"]),
                         ("EXECUTABLE", 0, 0, 200))
        self.assertEqual(fields["error_code"], "exchange_reduction_withheld_until_settle")
        state, fields = reconcile.found_move(dict(base, sizeMatched=1.0, sizeVoided=1.0), order)
        self.assertEqual((state, fields["size_matched_p"], fields["size_voided_p"], fields["size_remaining_p"]),
                         ("EXECUTABLE", 100, 0, 100))
        self.assertIsNone(reconcile.found_move(dict(base, sizeMatched=2.0, sizeVoided=1.0), order))
        after = transition(order, state, fields=fields, reason="found_by_bet_id")
        self.assertEqual(after["size_voided_p"], 0)
        self.assertEqual(exposure_by_market([after]), exposure_by_market([order]))   # LAY 1.00 @ 2.00: still 200
        # A void the journal already knows is no new reduction: the exchange state is taken as it is.
        known = dict(order, size_voided_p=100, size_remaining_p=100)
        state, fields = reconcile.found_move(dict(base, sizeMatched=1.0, sizeVoided=1.0), known)
        self.assertEqual((state, fields["size_matched_p"], fields["size_voided_p"]), ("EXECUTION_COMPLETE", 100, 100))

    def test_found_order_takes_no_stale_unknown_outcome_code(self):
        """Regression (operator review): a full cancel of a bet that part-matched just before the cancel arrived
        (Betfair: matched 0.50, cancelled 1.50) was reconciled to EXECUTION_COMPLETE but kept
        cancel_size_short_refresh_needed, so the CLI exited 5 telling the operator to run reconcile, which never
        touches that order. An order the exchange resolves takes no stale code; a withheld reduction keeps its own."""
        order = orders.order_record(mode="live", state="UNKNOWN", bet_id="300000000015", size_p=200, price_h=350,
                                    size_cancelled_p=150, size_remaining_p=50,
                                    error_code="cancel_size_short_refresh_needed")
        item = {"betId": "300000000015", "marketId": "1.1", "selectionId": gw.SELECTION, "side": "BACK",
                "priceSize": {"price": 3.5, "size": 2.0}, "status": "EXECUTION_COMPLETE", "averagePriceMatched": 3.5,
                "sizeMatched": 0.5, "sizeRemaining": 0.0, "sizeLapsed": 0.0, "sizeCancelled": 1.5, "sizeVoided": 0.0}
        state, fields = reconcile.found_move(item, order)
        self.assertEqual((state, fields["size_matched_p"], fields["size_remaining_p"], fields["error_code"]),
                         ("EXECUTION_COMPLETE", 50, 0, None))
        after = transition(order, state, fields=fields, reason="found_by_bet_id")
        self.assertIsNone(after["error_code"])
        resting = dict(item, status="EXECUTABLE", sizeMatched=0.0, sizeRemaining=0.5)
        self.assertEqual(reconcile.found_move(resting, order)[1]["error_code"], None)
        withheld = dict(item, sizeMatched=0.0, sizeCancelled=2.0)          # more cancelled than the journal knows
        self.assertEqual(reconcile.found_move(withheld, order)[1]["error_code"],
                         "exchange_reduction_withheld_until_settle")

    def test_known_cancel_is_never_counted_twice_as_a_lapse(self):
        """Regression (audit): a CANCELLED row restating a known partial cancel was read as the rest lapsing.
        Under the live evidence rule no cleared row sizes anything: the known cancel stays, the open
        remainder stays at its worst case, and the order only learns its betId."""
        partial = self.unknown(bet_id="300000000013", side="LAY", size_p=1000, size_cancelled_p=500)
        self.assertEqual(partial["size_remaining_p"], 500)
        self.exchange.cleared["CANCELLED"].append({"betId": "300000000013", "marketId": "1.1", "sizeCancelled": 5.0})
        self.exchange.cleared["LAPSED"].append({"betId": "300000000013", "marketId": "1.1", "sizeCancelled": 5.0})
        result = self.run_reconcile(partial)
        self.assertEqual(result["final_state"], "EXECUTABLE")
        after = moved(partial, result)
        self.assertEqual((after["size_cancelled_p"], after["size_lapsed_p"], after["size_remaining_p"]), (500, 0, 500))
        # A current row showing more cancelled than the journal knows is withheld the same way.
        item = {"betId": "300000000013", "marketId": "1.1", "selectionId": gw.SELECTION, "side": "LAY",
                "priceSize": {"price": 3.5, "size": 10.0}, "status": "EXECUTION_COMPLETE", "averagePriceMatched": 0.0,
                "sizeMatched": 0.0, "sizeRemaining": 0.0, "sizeLapsed": 0.0, "sizeCancelled": 10.0, "sizeVoided": 0.0}
        state, fields = reconcile.found_move(item, partial)
        self.assertEqual((state, fields["size_cancelled_p"], fields["size_remaining_p"]), ("EXECUTABLE", 500, 500))
        # Less cancelled on the exchange than our own reports established is contradictory: nothing is taken.
        self.assertIsNone(reconcile.found_move(dict(item, sizeCancelled=4.0, sizeLapsed=0.0, sizeRemaining=6.0,
                                                    status="EXECUTABLE"), partial))

    def test_partial_void_never_zeroes_a_standing_match(self):
        """Regression (audit): a VOIDED row once moved a matched order to terminal VOIDED (zero exposure). No
        cleared row of any size moves a live order to VOIDED now: it only records the betId."""
        unknown = self.unknown(bet_id="300000000014", side="LAY", size_matched_p=50, average_price_matched_h=350)
        for voided in (2.0, 0.5, None):
            with self.subTest(voided=voided):
                row = {"betId": "300000000014", "marketId": "1.1"}
                if voided is not None:
                    row["sizeSettled"] = voided
                self.exchange.cleared["VOIDED"] = [row]
                result = self.run_reconcile(unknown)
                self.assertEqual(result["final_state"], "EXECUTABLE")
                after = moved(unknown, result)
                self.assertEqual((after["size_matched_p"], after["size_voided_p"], after["size_remaining_p"]),
                                 (50, 0, 150))
                self.assertEqual(exposure_by_market([after]), exposure_by_market([unknown]))

    def test_known_matched_size_is_never_absent(self):
        """Regression (audit): an UNKNOWN order with a known matched size reached ABSENT_UNCONFIRMED (clean)
        when nothing was found, and --accept then dropped the unknown remainder to zero exposure."""
        matched = self.unknown(size_matched_p=50, average_price_matched_h=350)
        result = self.run_reconcile(matched)
        self.assertEqual((result["final_state"], result["reason"]), ("UNKNOWN", "matched_size_known_never_absent"))
        self.assertFalse(result["clean"])
        self.assertEqual(result["halt"], {"reason": "matched_size_known_never_absent", "scope": "market"})
        absent = orders.order_record(mode="live", state="ABSENT_UNCONFIRMED", bet_id=None, size_matched_p=50,
                                     average_price_matched_h=350, submitted_at=self.clock.utc_now(),
                                     outcome_observed_at=self.clock.utc_now())
        self.assertFalse(self.run_reconcile(absent)["clean"])

    def test_startup_flags_matched_size_the_journal_does_not_have(self):
        """Regression (audit): startup_reconcile only compared EXECUTABLE status, so a bet the exchange
        shows matched passed A17 behind a LAPSED, VOIDED or zero-matched journal order."""
        since = utc_from_ns(self.clock.time_ns() - self.clock.time_ns() % (86400 * 10**9))
        lapsed = orders.order_record(mode="live", state="LAPSED", bet_id="300000000008", size_p=1000,
                                     size_cancelled_p=500, size_lapsed_p=500,
                                     customer_order_ref="bro200101120000-00000000000000c1")
        lagging = orders.order_record(mode="live", state="EXECUTABLE", bet_id="300000000009",
                                      customer_order_ref="bro200101120000-00000000000000c2")
        complete = orders.order_record(mode="live", state="EXECUTION_COMPLETE", bet_id="300000000010",
                                       size_matched_p=200, average_price_matched_h=350,
                                       customer_order_ref="bro200101120000-00000000000000c3")
        state = orders.state_with([lapsed, lagging, complete], mode="live")
        self.exchange.add_order(bet_id="300000000008", ref=lapsed["customer_order_ref"], strategy_ref="bar-manual",
                                size=10.0, matched=5.0, cancelled=5.0, status="EXECUTION_COMPLETE", average=3.5)
        self.exchange.add_order(bet_id="300000000009", ref=lagging["customer_order_ref"], strategy_ref="bar-manual",
                                matched=1.0, average=3.5)                  # the journal lags an open bet: fine
        self.exchange.add_order(bet_id="300000000010", ref=complete["customer_order_ref"], strategy_ref="bar-manual",
                                matched=2.0, status="EXECUTION_COMPLETE", average=3.5)
        result = startup_reconcile(self.read, state, strategy_refs=["bar-manual"], live_app_key=Secret(gw.LIVE_KEY),
                                   since_utc=since, clock=self.clock)
        self.assertEqual(result["contradictions"], [{"bet_id": "300000000008", "market_id": "1.1",
                                                     "journal_state": "LAPSED",
                                                     "reason": "exchange_matched_size_exceeds_journal"}])
        self.exchange.orders["300000000010"]["sizeMatched"] = 2.5
        self.exchange.orders["300000000010"]["priceSize"]["size"] = 2.5
        result = startup_reconcile(self.read, state, strategy_refs=["bar-manual"], live_app_key=Secret(gw.LIVE_KEY),
                                   since_utc=since, clock=self.clock)
        self.assertEqual(sorted(c["bet_id"] for c in result["contradictions"]), ["300000000008", "300000000010"])
        dynamic = dynamic_arming(orders.order_config(), command="place", session=None, account_details=None,
                                 journal_state=orders.state_with([], mode="live"), startup=dict(result, foreign=[]),
                                 book=None)
        self.assertIn("unresolved_or_foreign_orders", dynamic["failures"])

    def test_window_anchored_at_outcome_observed(self):
        submitted = utc_from_ns(self.clock.time_ns() - 100 * 10**9)
        observed_ns = self.clock.time_ns()
        result = self.run_reconcile(self.unknown(submitted_at=submitted))
        self.assertEqual(result["final_state"], "ABSENT_UNCONFIRMED")
        self.assertGreaterEqual(self.clock.time_ns() - observed_ns, 15 * 10**9)
        # The ref-independent search starts 5 s before the submit.
        search = [c[1] for c in self.exchange.calls if c[0] == "listCurrentOrders" and c[1].get("date_range")][0]
        expected_from = parse_utc_ns(submitted) - 5 * 10**9
        self.assertEqual(parse_utc_ns(search["date_range"]["from"]), expected_from - expected_from % 1_000_000)
        self.assertEqual(search["customer_strategy_refs"], ["bar-manual"])
        self.assertEqual(search["order_by"], "BY_PLACE_TIME")

    def test_http_timeout_after_window_still_polls(self):
        failures = [TransportError("deadline_exceeded", phase="receive") for _ in range(25)]
        self.exchange.read_failures["listCurrentOrders"] = failures
        result = self.run_reconcile(self.unknown())
        # 25 failed polls take the clock past the deadline; polling continues until enough succeed.
        self.assertEqual(result["final_state"], "ABSENT_UNCONFIRMED")
        self.assertEqual(result["successful_polls"], result["polls"] - 25)
        self.assertGreaterEqual(result["successful_polls"], 3)

    def test_read_error_never_absent(self):
        self.exchange.read_failures["listCurrentOrders"] = [ApiError("TOO_MANY_REQUESTS", surface="aping")
                                                            for _ in range(500)]
        result = self.run_reconcile(self.unknown())
        self.assertEqual(result["final_state"], "UNKNOWN")
        self.assertEqual(result["reason"], "reconcile_window_incomplete")
        self.assertEqual(result["successful_polls"], 0)
        self.assertEqual(result["transition"], ["UNKNOWN", {"successful_polls": 0}])
        for method, position in (("listCurrentOrders", "search"), ("listClearedOrders", "cleared")):
            with self.subTest(failing=position):
                self.exchange.read_failures = {}
                self.exchange.calls.clear()
                original = self.exchange.current if method == "listCurrentOrders" else self.exchange.cleared_orders

                def failing(*args, _original=original, **kw):
                    if position == "search" and kw.get("date_range") is None:
                        return _original(*args, **kw)
                    raise TransportError("connection_lost", phase="receive")

                with patch.object(self.exchange, "current" if method == "listCurrentOrders" else "cleared_orders",
                                  failing):
                    result = self.run_reconcile(self.unknown())
                self.assertEqual(result["final_state"], "UNKNOWN")
                self.assertFalse(result["clean"])

    def test_absent_unconfirmed_needs_three_polls_span(self):
        cfg = {"orders": {"reconcile": dict(CFG["orders"]["reconcile"], poll_interval_s=10.0)}}
        start = self.clock.time_ns()
        result = reconcile_unknown(self.read, self.unknown(), cfg=cfg, clock=self.clock, strategy_refs=[])
        self.assertEqual(result["final_state"], "ABSENT_UNCONFIRMED")
        polls = self.poll_calls()
        self.assertGreaterEqual(len(polls), 3)
        self.assertGreaterEqual(self.clock.time_ns() - start, 20 * 10**9)
        self.assertTrue(result["clean"])
        # One failure inside the final five seconds postpones the decision.
        self.exchange.calls.clear()
        clock_points = []
        original = self.exchange.current

        def flaky(**kw):
            clock_points.append(self.clock.time_ns())
            if len(clock_points) == 20:
                raise TransportError("connection_lost", phase="receive")
            return original(**kw)

        with patch.object(self.exchange, "current", flaky):
            later = self.run_reconcile(self.unknown())
        self.assertEqual(later["final_state"], "ABSENT_UNCONFIRMED")
        self.assertGreaterEqual(later["polls"], 26)

    def test_unattributed_candidate_halts(self):
        self.exchange.add_order(ref=None, strategy_ref="bar-manual", price=3.5, size=2.0)
        result = self.run_reconcile(self.unknown())
        self.assertEqual(result["final_state"], "UNKNOWN")
        self.assertEqual(result["halt"], {"reason": "unattributed_candidate_order", "scope": "global"})
        halt_events = [e for e in result["events"] if e["kind"] == "halt"]
        self.assertEqual(halt_events[0]["payload"], {"reason": "unattributed_candidate_order", "scope": "global"})
        # A known bet with the same terms is not a candidate.
        known = list(self.exchange.orders)
        result = self.run_reconcile(self.unknown(), known_bet_ids=known)
        self.assertEqual(result["final_state"], "ABSENT_UNCONFIRMED")

    def test_absent_never_resubmitted(self):
        env = gw.build_live_gateway(self, clock=self.clock)
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        gw.open_live(self, env, "place", instructions=[gw.instruction(), gw.instruction()])
        result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual(result["orders"][0]["state"], "ABSENT_UNCONFIRMED")
        self.assertEqual(len(env.router.order_requests), 1)
        again = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual(again["decision"], "refused")
        self.assertEqual(len(env.router.order_requests), 1)
        state = env.journal.fold(now_ns=self.clock.time_ns())
        absent = state["orders"][result["orders"][0]["customer_order_ref"]]
        self.assertEqual(absent["exposure_basis"], "adverse_unknown")
        self.assertIn("1.1", state["unresolved_markets"])

    def test_accept_requires_tty_and_clean_search(self):
        env = gw.build_live_gateway(self, clock=self.clock)
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        placed = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        env.gateway.close()
        ref = placed["orders"][0]["customer_order_ref"]
        from betangel_research.betfair.errors import NotArmed, NotPresent, OrderRefused
        # No terminal: the phrase cannot even be typed, and a gateway opened without one is not armed.
        env.confirmer.present = False
        with self.assertRaises(NotPresent):
            env.gateway.preconfirm("reconcile-accept", accept_ref=ref)
        with self.assertRaises(NotArmed) as caught:
            env.gateway.open(command="reconcile-accept", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "operator_tty_required")
        env.confirmer.present = True
        # A mistyped phrase is refused at the terminal, before the lock.
        env.confirmer.lines = ["ACCEPT ABSENT nope"]
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.preconfirm("reconcile-accept", accept_ref=ref)
        self.assertEqual(caught.exception.rule, "operator_echo_mismatch")
        env.confirmer.lines = None
        gw.open_live(self, env, "reconcile-accept", accept_ref=ref)
        with self.assertRaises(NotArmed):
            env.gateway.reconcile(accept_ref=ref, confirm_token="wrong")
        env.exchange.add_order(ref=None, strategy_ref="bar-manual", price=3.5, size=2.0)
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.reconcile(accept_ref=ref, confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "accept_final_search_not_clean")
        env.exchange.orders.clear()
        state = env.journal.fold(now_ns=self.clock.time_ns())
        self.assertNotEqual(state["orders"][ref]["state"], "ABSENT_ACCEPTED")
        # The typed phrase was used by that attempt: another needs typing again.
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.reconcile(accept_ref=ref, confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "operator_confirmation_missing")
        env.gateway.close()
        # The unattributed candidate halted the journal; accepting is still possible once it is gone.
        gw.open_live(self, env, "reconcile-accept", accept_ref=ref)
        accepted = env.gateway.reconcile(accept_ref=ref, confirm_token=env.token)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(env.confirmer.prompts[-1]["expected"], f"ACCEPT ABSENT {ref}")
        state = env.journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(state["orders"][ref]["state"], "ABSENT_ACCEPTED")
        self.assertEqual([e["kind"] for e in env.journal.events()][-1], "operator_accept")

    def test_reconcile_clears_market_halt_only_when_resolved(self):
        env = gw.build_live_gateway(self, clock=self.clock)
        env.journal.append("halt", {"reason": "found_order_inconsistent", "scope": "market"}, market_id="1.7")
        env.journal.append("halt", {"reason": "found_order_inconsistent", "scope": "market"}, market_id="1.8")
        ref, cref = "bro200101120000-0000000000000def", "brq200101120000-0000000000000def"
        env.journal.append("intent", orders.intent_payload(gw.instruction(), source="cli_manual"), intent_id="int-u",
                           customer_order_ref=ref, customer_ref=cref, market_id="1.8", state_after="RECORDED")
        env.journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "b" * 64,
                                      "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                           intent_id="int-u", market_id="1.8")
        env.journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": ref, "state": "UNKNOWN", "reason": "test"}]}, market_id="1.8")
        env.exchange.read_failures["listCurrentOrders"] = [TransportError("connection_lost", phase="receive")
                                                           for _ in range(500)]
        env.gateway.open(command="reconcile")
        self.addCleanup(env.gateway.close)
        result = env.gateway.reconcile()
        self.assertEqual(result["market_halts_cleared"], ["1.7"])
        state = env.journal.fold(now_ns=self.clock.time_ns())
        self.assertNotIn("1.7", state["unresolved_markets"])
        self.assertIn("1.8", state["unresolved_markets"])
        self.assertFalse(state["halted"])

    def test_accept_needs_a_reconcile_accept_gateway(self):
        env = gw.build_live_gateway(self, clock=self.clock)
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        ref = env.gateway.place("1.1", [gw.instruction()], source="cli_manual",
                                confirm_token=env.token)["orders"][0]["customer_order_ref"]
        env.gateway.close()
        gw.open_live(self, env, "reconcile")          # no A1, A2 or A11 behind this gateway
        from betangel_research.betfair.errors import OrderRefused
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.reconcile(accept_ref=ref, confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "gateway_opened_for_another_command")
        state = env.journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(state["orders"][ref]["state"], "ABSENT_UNCONFIRMED")

    def test_startup_foreign_order_halts(self):
        mine = orders.order_record(mode="live", bet_id="300000000005")
        state = orders.state_with([mine], mode="live")
        self.exchange.add_order(bet_id="300000000005", ref=orders.REF, strategy_ref="bar-manual")
        self.exchange.add_order(ref="bro200101120000-ffffffffffffffff", strategy_ref="bar-manual", source_key=None)
        self.exchange.add_order(ref=None, strategy_ref=None, source_key=gw.LIVE_KEY)
        self.exchange.add_order(ref=None, strategy_ref=None, source_key="bet-angel-vendor-key")
        since = utc_from_ns(self.clock.time_ns() - self.clock.time_ns() % (86400 * 10**9))
        result = startup_reconcile(self.read, state, strategy_refs=["bar-manual"], live_app_key=Secret(gw.LIVE_KEY),
                                   since_utc=since, clock=self.clock)
        self.assertEqual(sorted(f["leg"] for f in result["foreign"]), ["source_id_key", "strategy_ref"])
        self.assertNotIn("sourceIdKey", repr(result))
        self.assertNotIn(gw.LIVE_KEY, repr(result))
        # Through the gateway: a foreign order halts and fails A17.
        env = gw.build_live_gateway(self)
        env.exchange.add_order(ref=None, strategy_ref="bar-manual")
        report = env.gateway.open(command="place", confirm_token=env.token)
        self.addCleanup(env.gateway.close)
        self.assertIn("unresolved_or_foreign_orders", report["failures"])
        fold = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertTrue(fold["halted"])
        self.assertIn("foreign_strategy_order", fold["halt_reasons"])
        # A read failure leaves foreign unknown (None), which A17 also refuses.
        self.exchange.read_failures["listClearedOrders"] = [TransportError("connection_lost", phase="receive")]
        failed = startup_reconcile(self.read, state, strategy_refs=["bar-manual"], live_app_key=None, since_utc=since,
                                   clock=self.clock)
        self.assertIsNone(failed["foreign"])
        self.assertTrue(failed["read_failures"])

    def test_startup_owns_a_bet_by_bet_id_not_by_a_closed_ref(self):
        """Regression (audit): a bet counted as ours whenever its customerOrderRef matched any journal
        order, whatever that order's state or betId, so a live bet behind an ABSENT_ACCEPTED (zero
        exposure) order, or a replacement reusing a closed parent's ref, passed A17 at zero exposure."""
        since = utc_from_ns(self.clock.time_ns() - self.clock.time_ns() % (86400 * 10**9))
        accepted = orders.order_record(mode="live", state="ABSENT_ACCEPTED", bet_id=None,
                                       customer_order_ref="bro200101120000-00000000000000a1")
        parent = orders.order_record(mode="live", state="REPLACED", bet_id="300000000070",
                                     customer_order_ref="bro200101120000-00000000000000a2", size_remaining_p=0)
        pending = orders.order_record(mode="live", state="UNKNOWN", bet_id=None,
                                      customer_order_ref="bro200101120000-00000000000000a3")
        complete = orders.order_record(mode="live", state="EXECUTION_COMPLETE", bet_id="300000000072",
                                       customer_order_ref="bro200101120000-00000000000000a4", size_matched_p=200,
                                       average_price_matched_h=350)
        state = orders.state_with([accepted, parent, pending, complete], mode="live")
        behind_accepted = self.exchange.add_order(ref=accepted["customer_order_ref"], strategy_ref="bar-manual")
        reusing_parent = self.exchange.add_order(ref=parent["customer_order_ref"], strategy_ref=None)
        self.exchange.add_order(ref=pending["customer_order_ref"], strategy_ref="bar-manual")     # ours, unresolved
        self.exchange.add_order(bet_id="300000000072", ref=complete["customer_order_ref"], strategy_ref="bar-manual")
        result = startup_reconcile(self.read, state, strategy_refs=["bar-manual"], live_app_key=Secret(gw.LIVE_KEY),
                                   since_utc=since, clock=self.clock)
        self.assertEqual(sorted(f["bet_id"] for f in result["foreign"]), sorted([behind_accepted, reusing_parent]))
        # The journal says fully matched; the exchange shows the same bet still EXECUTABLE.
        self.assertEqual(result["contradictions"], [{"bet_id": "300000000072", "market_id": "1.1",
                                                     "journal_state": "EXECUTION_COMPLETE"}])
        clean = dict(result, foreign=[])
        dynamic = dynamic_arming(orders.order_config(), command="cancel", session=None, account_details=None,
                                 journal_state=orders.state_with([], mode="live"), startup=clean, book=None)
        self.assertNotIn("A17", dynamic["checked"])          # cancel does not check A17; place does
        dynamic = dynamic_arming(orders.order_config(), command="place", session=None, account_details=None,
                                 journal_state=orders.state_with([], mode="live"), startup=clean, book=None)
        self.assertIn("unresolved_or_foreign_orders", dynamic["failures"])
        # Through the gateway: a bet that turns up after its order was accepted absent halts and fails A17.
        env = gw.build_live_gateway(self)
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        ref = env.gateway.place("1.1", [gw.instruction()], source="cli_manual",
                                confirm_token=env.token)["orders"][0]["customer_order_ref"]
        env.gateway.close()
        env.exchange.place_handler = None
        gw.open_live(self, env, "reconcile-accept", accept_ref=ref)
        self.assertEqual(env.gateway.reconcile(accept_ref=ref, confirm_token=env.token)["status"], "accepted")
        env.gateway.close()
        env.exchange.add_order(ref=ref, strategy_ref="bar-manual", price=3.5, size=2.0)
        report = gw.open_live(self, env, "place", instructions=[gw.instruction(price_h=370)])
        self.assertIn("unresolved_or_foreign_orders", report["failures"])
        self.assertIn("foreign_strategy_order", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])
        with self.assertRaises(NotArmed):
            env.gateway.place("1.1", [gw.instruction(price_h=370)], source="cli_manual", confirm_token=env.token)
        self.assertEqual(len(env.router.order_requests), 1)

    def test_contradicting_bet_halts_through_the_gateway(self):
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        bet = env.gateway.place("1.1", [gw.instruction()], source="cli_manual",
                                confirm_token=env.token)["orders"][0]["bet_id"]
        env.gateway.cancel("1.1", [bet], source="cli_manual")
        env.gateway.close()
        env.exchange.orders[bet].update(status="EXECUTABLE", sizeRemaining=2.0, sizeCancelled=0.0)
        report = gw.open_live(self, env, "place", instructions=[gw.instruction()])
        self.assertEqual(env.gateway._startup["contradictions"][0]["bet_id"], bet)
        self.assertIn("unresolved_or_foreign_orders", report["failures"])
        self.assertIn("exchange_journal_contradiction", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])

    def test_wall_clock_step_while_polling_concludes_nothing(self):
        """Regression (audit): polling ran on the wall clock; a step back blocked one call for the size of
        the step (an hour), and a step forward concluded ABSENT after 2 s instead of the 15 s window."""
        for step_s in (-3600, 30):
            with self.subTest(step_s=step_s):
                clock = support.FakeClock()
                exchange = gw.SimExchange(clock)
                read = gw.FakeReadClient(exchange)
                now = clock.utc_now()
                order = orders.order_record(state="UNKNOWN", mode="live", submitted_at=now, outcome_observed_at=now,
                                            bet_id=None)
                calls = []
                original = exchange.current

                def current(_calls=calls, _clock=clock, _original=original, _step=step_s, **kw):
                    _calls.append(1)
                    if len(_calls) == 2:
                        _clock.step_wall(_step)
                    return _original(**kw)

                exchange.current = current
                started = clock.mono_ns
                result = reconcile_unknown(read, order, cfg=CFG, clock=clock, strategy_refs=["bar-manual"])
                self.assertEqual((result["final_state"], result["reason"]), ("UNKNOWN", "clock_step_during_reconcile"))
                self.assertFalse(result["clean"])
                self.assertLessEqual(clock.mono_ns - started, reconcile.MAX_BLOCK_S * 10**9)

    def test_polling_window_is_monotonic(self):
        """The window span, the final-5-s rule and the hard stop run on the monotonic clock."""
        started = self.clock.mono_ns
        result = self.run_reconcile(self.unknown())
        self.assertEqual(result["final_state"], "ABSENT_UNCONFIRMED")
        self.assertGreaterEqual(self.clock.mono_ns - started, 20 * 10**9)    # window 15 + grace 5 from submit
        self.assertLessEqual(self.clock.mono_ns - started, reconcile.MAX_BLOCK_S * 10**9)

    def test_source_id_key_leg_finds_unattributed_bets(self):
        """A bet placed with our key but without our strategy ref is a candidate too (F33 unverified)."""
        plain = self.unknown()
        self.exchange.add_order(ref=None, strategy_ref=None, price=3.5, size=2.0)          # our key, our terms
        result = self.run_reconcile(plain, live_app_key=Secret(gw.LIVE_KEY))
        self.assertEqual((result["final_state"], result["reason"]), ("UNKNOWN", "unattributed_candidate_order"))
        without_key = self.run_reconcile(plain)
        self.assertIn("source_id_key_leg_skipped_no_live_key", without_key["flags"])
        # Live replace was removed in v1: there is no parent argument and no inferred child match.
        with self.assertRaises(TypeError):
            self.run_reconcile(plain, parent=plain)

    def test_exchange_floor_used(self):
        since = utc_from_ns(self.clock.time_ns() - self.clock.time_ns() % (86400 * 10**9))
        for _ in range(3):
            self.exchange.add_order(ref=None, strategy_ref="bar-manual")
        self.exchange.cleared["SETTLED"] += [
            {"betId": "300000000090", "marketId": "1.5", "customerStrategyRef": "bar-manual", "profit": -4.0,
             "placedDate": since},
            {"betId": "300000000091", "marketId": "1.6", "customerStrategyRef": "bar-manual", "profit": 3.0,
             "placedDate": since}]
        result = startup_reconcile(self.read, orders.empty_state("live"), strategy_refs=["bar-manual"],
                                   live_app_key=None, since_utc=since, clock=self.clock)
        self.assertEqual(result["exchange_orders_today"], 5)
        # The winning market's commission is unknown, so it counts as 0, never as +3.00.
        self.assertEqual(result["exchange_realized_today_pence"], -400)
        self.exchange.cleared["SETTLED"].append({"marketId": "1.6", "customerStrategyRef": "bar-manual",
                                                 "commission": 0.15, "_group": "MARKET"})
        result = startup_reconcile(self.read, orders.empty_state("live"), strategy_refs=["bar-manual"],
                                   live_app_key=None, since_utc=since, clock=self.clock)
        self.assertEqual(result["exchange_realized_today_pence"], -400 + 285)
        self.exchange.cleared["SETTLED"].append({"betId": "300000000092", "marketId": "1.7",
                                                 "customerStrategyRef": "bar-manual", "placedDate": since})
        unknown = startup_reconcile(self.read, orders.empty_state("live"), strategy_refs=["bar-manual"],
                                    live_app_key=None, since_utc=since, clock=self.clock)
        self.assertIsNone(unknown["exchange_realized_today_pence"])
        # The journaled floor is what R6 uses: a floor at the day cap refuses the next place.
        env = gw.build_live_gateway(self, overrides={"orders": {"caps": {"max_orders_per_day": 3}}})
        env.clock.step_wall(-86400)
        for i in range(3):
            # Ours by betId (submitted, then lapsed, in a journal dated yesterday), but placed today as far
            # as the exchange says: only the exchange floor counts them for today.
            ref, bet = f"bro200101120000-{i:016x}", str(400000000000 + i)
            env.journal.append("intent", orders.intent_payload(gw.instruction(), source="cli_manual"),
                               intent_id=f"int-{i}", customer_order_ref=ref, customer_ref=orders.CREF, market_id="1.9",
                               state_after="RECORDED")
            env.journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": None,
                                          "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                               intent_id=f"int-{i}", market_id="1.9")
            env.journal.append("report", {"action": "place", "transitions": [
                {"customer_order_ref": ref, "state": "EXECUTABLE", "fields": {"bet_id": bet, "size_remaining_p": 200}},
                {"customer_order_ref": ref, "state": "LAPSED", "fields": {"size_remaining_p": 0,
                                                                          "size_lapsed_p": 200}}]}, market_id="1.9")
            env.exchange.cleared["LAPSED"].append({"betId": bet, "marketId": "1.9", "customerStrategyRef": "bar-manual",
                                                   "customerOrderRef": ref, "sizeCancelled": 2.0, "placedDate": since})
        env.clock.step_wall(86400)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        fold = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertEqual(fold["exchange_floor"]["orders_today"], 3)
        result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual(result["decision"], "refused")
        self.assertIn("rate_day", [f["rule"] for f in result["refusal"]["detail"]["failures"]])

    def test_startup_source_id_key_leg_through_the_gateway(self):
        env = gw.build_live_gateway(self)
        bet = env.exchange.add_order(ref=None, strategy_ref=None, source_key=gw.LIVE_KEY)
        report = gw.open_live(self, env, "place")
        self.assertIn("unresolved_or_foreign_orders", report["failures"])
        startup = [e for e in env.journal.events() if e["kind"] == "startup_reconcile"][-1]
        self.assertEqual(startup["payload"]["foreign"], [{"bet_id": bet, "market_id": "1.1", "leg": "source_id_key"}])
        self.assertIn("foreign_strategy_order", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])

    def test_unknown_found_lapsed_keeps_worst_case_executable(self):
        """Amendment A3 (D3): a current row showing the bet lapsed (EXECUTION_COMPLETE, nothing matched) is
        a reduction the journal did not know: withheld until settle-market, so the order is EXECUTABLE with
        its whole size remaining (worst case), never LAPSED."""
        self.exchange.add_order(ref=REF, strategy_ref="bar-manual", price=3.5, size=2.0, status="EXECUTION_COMPLETE",
                                lapsed=2.0)
        unknown = self.unknown()
        result = self.run_reconcile(unknown)
        self.assertEqual((result["final_state"], result["reason"]), ("EXECUTABLE", "found_by_ref"))
        after = moved(unknown, result)
        self.assertEqual((after["size_lapsed_p"], after["size_remaining_p"], after["error_code"]),
                         (0, 200, "exchange_reduction_withheld_until_settle"))
        self.assertEqual(after["exposure_basis"], "adverse_unmatched")
        self.assertEqual(exposure_by_market([after]), exposure_by_market([unknown]))

    def test_unknown_found_partly_cancelled_keeps_worst_case(self):
        """A cancelled part the journal did not know (say, Bet Angel or the website cancelled it) is
        withheld: the matched part is taken (it only raises what is known), the rest stays remaining."""
        self.exchange.add_order(ref=REF, strategy_ref="bar-manual", price=3.5, size=2.0, matched=0.5, average=3.5,
                                cancelled=0.5)
        unknown = self.unknown()
        result = self.run_reconcile(unknown)
        after = moved(unknown, result)
        self.assertEqual((after["state"], after["size_matched_p"], after["average_price_matched_h"],
                          after["size_cancelled_p"], after["size_remaining_p"]), ("EXECUTABLE", 50, 350, 0, 150))
        self.assertEqual(after["error_code"], "exchange_reduction_withheld_until_settle")
        self.assertEqual(exposure_by_market([after]), exposure_by_market([unknown]))

    def test_unknown_found_partly_voided_matched_bet_is_executable_with_nothing_remaining(self):
        """Journal matched 2.00 (the place report said so); the exchange now shows matched 1.00 and voided
        1.00. The void is withheld and the larger matched size kept: EXECUTABLE, remaining 0, matched_exact
        (a valid record, never an assert or a None)."""
        bet = "300000000016"
        self.exchange.add_order(bet_id=bet, ref=REF, strategy_ref="bar-manual", price=3.5, size=2.0, matched=1.0,
                                average=3.5, status="EXECUTION_COMPLETE")
        self.exchange.orders[bet]["sizeVoided"] = 1.0
        unknown = self.unknown(bet_id=bet, size_matched_p=200, average_price_matched_h=350)
        result = self.run_reconcile(unknown)
        self.assertEqual((result["final_state"], result["reason"]), ("EXECUTABLE", "found_by_bet_id"))
        after = moved(unknown, result)
        self.assertEqual((after["size_matched_p"], after["size_voided_p"], after["size_remaining_p"],
                          after["exposure_basis"]), (200, 0, 0, "matched_exact"))
        self.assertEqual(exposure_by_market([after]), exposure_by_market([unknown]))

    def test_cleared_rows_only_establish_the_bet(self):
        """Regression R3#3 (moot by D3): a VOIDED row compared with a stale journal size left the order
        UNKNOWN (or EXECUTABLE) for good. Now any cleared row (VOIDED with a stale size, SETTLED, LAPSED)
        only records the betId: EXECUTABLE at worst case, never LAPSED, VOIDED or SETTLED, and the order is
        no longer unresolved, so A17 and R3 do not stay blocked (settle-market then closes it)."""
        rows = {"VOIDED": {"sizeSettled": 2.0}, "SETTLED": {"sizeSettled": 2.0, "priceMatched": 3.5, "profit": 5.0},
                "LAPSED": {"sizeCancelled": 2.0}}
        for status, extra in rows.items():
            with self.subTest(status=status):
                self.exchange.cleared = {"SETTLED": [], "VOIDED": [], "LAPSED": [], "CANCELLED": []}
                self.exchange.cleared[status].append(dict(extra, betId="300000000099", marketId="1.1",
                                                          customerOrderRef=REF))
                unknown = self.unknown()
                result = self.run_reconcile(unknown)
                self.assertEqual((result["final_state"], result["halt"]), ("EXECUTABLE", None))
                after = moved(unknown, result)
                self.assertEqual((after["bet_id"], after["size_matched_p"], after["size_remaining_p"]),
                                 ("300000000099", 0, 200))
                self.assertEqual(exposure_by_market([after]), exposure_by_market([unknown]))
                state = orders.state_with([after], mode="live", ref_roundtrip_verified=True)
                self.assertEqual(state["unresolved_markets"], [])
                dynamic = dynamic_arming(orders.order_config(), command="place", session=None, account_details=None,
                                         journal_state=state, startup={"foreign": [], "contradictions": []},
                                         book=None)
                self.assertNotIn("unresolved_or_foreign_orders", dynamic["failures"])
        # Through the gateway: a lost place response whose bet was voided and settled before anyone looked.
        env = gw.build_live_gateway(self)

        def lost_then_voided(params):
            ref = params["instructions"][0]["customerOrderRef"]
            env.exchange.cleared["VOIDED"].append({"betId": "300000000077", "marketId": "1.1", "customerOrderRef": ref,
                                                   "customerStrategyRef": "bar-manual", "sizeSettled": 2.0})
            raise TransportError("connection_lost", phase="receive")

        env.exchange.place_handler = lost_then_voided
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        placed = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        env.gateway.close()
        order = env.journal.fold(now_ns=env.clock.time_ns())["orders"][placed["orders"][0]["customer_order_ref"]]
        self.assertEqual((order["state"], order["bet_id"], order["size_remaining_p"], order["error_code"]),
                         ("EXECUTABLE", "300000000077", 200, "cleared_row_bet_exists_worst_case"))
        self.assertFalse(any(e["kind"] == "settle" for e in env.journal.events()))
        env.exchange.place_handler = None
        report = gw.open_live(self, env, "place", instructions=[gw.instruction(price_h=360)])
        self.assertTrue(report["armed"], report)

    def test_live_reconcile_writes_no_settle_or_close_moves(self):
        """Amendment A3 (D3): live reconcile refreshes, closes and settles nothing. With the market CLOSED,
        our bets gone from current orders and SETTLED and VOIDED rows waiting, it journals no settle event,
        moves no order and leaves the counted exposure as it was."""
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "place", instructions=[gw.instruction(), gw.instruction(price_h=360)])
        env.exchange.place_handler = matched_place(env.exchange)
        complete = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        env.exchange.place_handler = None
        resting = env.gateway.place("1.1", [gw.instruction(price_h=360)], source="cli_manual",
                                    confirm_token=env.token)
        env.gateway.close()
        bets = [complete["orders"][0]["bet_id"], resting["orders"][0]["bet_id"]]
        env.exchange.market_status = "CLOSED"
        env.exchange.orders.clear()
        for bet in bets:
            env.exchange.cleared["SETTLED"].append({"betId": bet, "marketId": "1.1",
                                                    "customerStrategyRef": "bar-manual", "sizeSettled": 2.0,
                                                    "priceMatched": 3.5, "profit": -2.0})
        env.exchange.cleared["VOIDED"].append({"betId": bets[1], "marketId": "1.1", "customerStrategyRef": "bar-manual",
                                               "sizeSettled": 2.0})
        before = env.journal.fold(now_ns=env.clock.time_ns())
        gw.open_live(self, env, "reconcile")
        env.exchange.calls.clear()
        result = env.gateway.reconcile()
        self.assertEqual(set(result), {"mode", "status", "reconciled", "market_halts_cleared", "unresolved",
                                       "journal_seq"})
        self.assertEqual((result["status"], result["reconciled"]), ("reconciled", []))
        after = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertFalse(any(e["kind"] == "settle" for e in env.journal.events()))
        self.assertEqual({r: o["state"] for r, o in after["orders"].items()},
                         {r: o["state"] for r, o in before["orders"].items()})
        self.assertEqual(exposure_by_market(list(after["orders"].values())),
                         exposure_by_market(list(before["orders"].values())))
        self.assertEqual(env.exchange.calls, [])                    # no refresh, cleared or book read at all

    def test_startup_bounds_a_settled_order_by_matched_plus_remaining(self):
        """Amendment A3 (K3): settle-market books matched + remaining at worst case, so the same-day startup
        reconcile compares a SETTLED order's exchange sizeSettled with that size, not its stale matched size."""
        since = utc_from_ns(self.clock.time_ns() - self.clock.time_ns() % (86400 * 10**9))
        settled = orders.order_record(mode="live", state="SETTLED", bet_id="300000000031", size_remaining_p=200,
                                      customer_order_ref="bro200101120000-00000000000000d1")
        self.assertEqual((settled["size_matched_p"], settled["size_remaining_p"]), (0, 200))
        state = orders.state_with([settled], mode="live")
        row = {"betId": "300000000031", "marketId": "1.1", "customerStrategyRef": "bar-manual", "sizeSettled": 2.0,
               "profit": -2.0, "placedDate": since}
        self.exchange.cleared["SETTLED"].append(row)
        result = startup_reconcile(self.read, state, strategy_refs=["bar-manual"], live_app_key=None,
                                   since_utc=since, clock=self.clock)
        self.assertEqual((result["contradictions"], result["foreign"]), ([], []))
        row["sizeSettled"] = 2.01
        result = startup_reconcile(self.read, state, strategy_refs=["bar-manual"], live_app_key=None,
                                   since_utc=since, clock=self.clock)
        self.assertEqual(result["contradictions"], [{"bet_id": "300000000031", "market_id": "1.1",
                                                     "journal_state": "SETTLED",
                                                     "reason": "exchange_matched_size_exceeds_journal"}])
        # A paper-style SETTLED order (nothing remaining) is still bounded by its matched size alone.
        row["sizeSettled"] = 2.0
        matched_only = dict(settled, size_matched_p=100, average_price_matched_h=350, size_remaining_p=0,
                            size_lapsed_p=100)
        result = startup_reconcile(self.read, orders.state_with([matched_only], mode="live"),
                                   strategy_refs=["bar-manual"], live_app_key=None, since_utc=since, clock=self.clock)
        self.assertEqual(len(result["contradictions"]), 1)

    def test_fake_clock_no_real_sleep(self):
        started = time.monotonic()
        with patch("time.sleep", side_effect=AssertionError("real sleep in reconcile")):
            result = self.run_reconcile(self.unknown())
        self.assertEqual(result["final_state"], "ABSENT_UNCONFIRMED")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(self.clock.sleeps)
        self.assertTrue(all(s == 1.0 for s in self.clock.sleeps))


class SettleMarketTests(unittest.TestCase):
    """settle-market (amendment A3, D3): the operator settles one CLOSED market by hand, typed before the
    lock; nothing settles a live order automatically."""

    def setUp(self):
        support.block_network(self)

    def fixture(self, **kw):
        """A live gateway (closed) with two of our bets in market 1.1: BACK 2.00 @ 3.50 fully matched
        (EXECUTION_COMPLETE) and BACK 2.00 @ 3.60 resting (EXECUTABLE, nothing matched as far as we know)."""
        env = gw.build_live_gateway(self, **kw)
        gw.open_live(self, env, "place", instructions=[gw.instruction(), gw.instruction(price_h=360)])
        env.exchange.place_handler = matched_place(env.exchange)
        complete = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        env.exchange.place_handler = None
        resting = env.gateway.place("1.1", [gw.instruction(price_h=360)], source="cli_manual", confirm_token=env.token)
        env.gateway.close()
        complete, resting = complete["orders"][0], resting["orders"][0]
        self.assertEqual((complete["state"], resting["state"]), ("EXECUTION_COMPLETE", "EXECUTABLE"))
        return env, complete, resting

    @staticmethod
    def settled_on_exchange(env):
        env.exchange.market_status = "CLOSED"
        env.exchange.orders.clear()          # Betfair stops listing a settled market's bets (F77, inferred)

    def fold(self, env):
        return env.journal.fold(now_ns=env.clock.time_ns())

    def attempt(self, env, market_id="1.1", token=None):
        """Type the line, open for settle-market and settle: the OrderRefused raised, or the result."""
        env.gateway.close()
        gw.open_live(self, env, "settle-market", market_id=market_id, settle=True)
        try:
            return env.gateway.settle_market(market_id, confirm_token=token or env.token)
        except OrderRefused as exc:
            return exc
        finally:
            env.gateway.close()

    def test_settle_market_has_no_mode_flag(self):
        """Checked at the parser only: if a regression ever accepted --mode, nothing here could run the command
        against the operator's real state root (cli.main without deps uses the passwd home)."""
        from betangel_research.betfair.cli import build_parser
        parsed = build_parser().parse_args(["settle-market", "--market", "1.1"])
        self.assertFalse(hasattr(parsed, "mode"))
        for argv in (["settle-market", "--market", "1.1", "--mode", "live"],
                     ["settle-market", "--market", "1.1", "--mode", "paper"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
                    build_parser().parse_args(argv)
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("unrecognized arguments: --mode", err.getvalue())

    def test_settle_market_needs_the_anchor_written_first(self):
        """Amendment A3 section 3.1: settle-market removes counted live exposure, so it is a risk command: open()
        refuses (journal_anchor_write_failed) when the Keychain anchor cannot be written, and nothing settles."""
        from betangel_research.betfair import gateway as gateway_module
        self.assertIn("settle-market", gateway_module.RISK_COMMANDS)
        env, complete, resting = self.fixture()
        self.settled_on_exchange(env)
        env.runner.add_fails = True
        with self.assertRaises(NotArmed) as caught:
            gw.open_live(self, env, "settle-market", settle=True)
        self.assertEqual(caught.exception.rule, "journal_anchor_write_failed")
        with self.assertRaises(OrderRefused):
            env.gateway.settle_market("1.1", confirm_token=env.token)     # never opened
        self.assertFalse(any(e["kind"] == "settle" for e in env.journal.events()))
        orders = self.fold(env)["orders"]
        self.assertEqual((orders[complete["customer_order_ref"]]["state"],
                          orders[resting["customer_order_ref"]]["state"]), ("EXECUTION_COMPLETE", "EXECUTABLE"))

    def test_settle_market_uses_a_normal_login(self):
        """Amendment A3 section 3.1: settle-market logs in normally (it is not an emergency command, so it never
        skips the successful-login cap); the login ledger records emergency false."""
        from betangel_research.betfair import gateway as gateway_module
        self.assertNotIn("settle-market", gateway_module.EMERGENCY_COMMANDS)
        env, _, _ = self.fixture()
        self.settled_on_exchange(env)
        gw.open_live(self, env, "settle-market", settle=True)
        self.assertEqual(env.gateway.login_kind, "normal")
        rows = [json.loads(line) for line in env.paths.login_ledger.read_text().splitlines() if line.strip()]
        attempts = [row["emergency"] for row in rows if row["kind"] == "login_attempt"]
        self.assertEqual(attempts[-1], False)
        self.assertEqual(env.gateway.settle_market("1.1", confirm_token=env.token)["status"], "settled")

    def test_settle_market_shows_orders_and_worst_case_then_settles(self):
        env, complete, resting = self.fixture()
        self.settled_on_exchange(env)
        before = self.fold(env)
        self.assertEqual(exposure_by_market(list(before["orders"].values()))["1.1"], 400)
        gw.open_live(self, env, "settle-market", settle=True)
        prompt = env.confirmer.prompts[-1]
        self.assertEqual(prompt["expected"], "SETTLE MARKET 1.1")
        banner = prompt["banner"]
        self.assertTrue(banner.startswith("MODE=LIVE: SETTLE MARKET BY HAND. No order is sent."))
        for text in ("check this market's settled bets on the Betfair website (or in Bet Angel) first",
                     f"{complete['customer_order_ref']} bet {complete['bet_id']} BACK sel {gw.SELECTION} @ 3.50 "
                     "size 2.00 matched 2.00 remaining 0.00 EXECUTION_COMPLETE worst-case loss \u00a32.00",
                     f"{resting['customer_order_ref']} bet {resting['bet_id']} BACK sel {gw.SELECTION} @ 3.60 "
                     "size 2.00 matched 0.00 remaining 2.00 EXECUTABLE worst-case loss \u00a32.00",
                     "Worst-case exposure counted now in this market (no hedge credit): \u00a34.00",
                     "Booked as today's (UTC) realised loss for the daily cap: \u00a34.00",
                     "Orders the kill cancelled, or whose cancel report was lost, are still counted here",
                     "Betfair's settled P&L is not used", "It cannot be undone."):
            self.assertIn(text, banner)
        self.assertNotIn("Type exactly", banner)
        methods = len(env.router.rpc_methods)
        result = env.gateway.settle_market("1.1", confirm_token=env.token)
        self.assertEqual(env.router.rpc_methods[methods:], ["listMarketBook", "listCurrentOrders", "listCurrentOrders"])
        self.assertNotIn("listClearedOrders", env.router.rpc_methods[methods:])
        self.assertEqual(env.router.order_requests[2:], [])          # no order is sent
        self.assertEqual((result["mode"], result["status"], result["market_id"]), ("live", "settled", "1.1"))
        self.assertEqual(sorted(o["state"] for o in result["orders"]), ["SETTLED", "SETTLED"])
        self.assertEqual((result["worst_case_exposure_before_pence"], result["booked_realized_loss_pence"]), (400, 400))
        settles = [e for e in env.journal.events() if e["kind"] == "settle"]
        self.assertEqual(len(settles), 1)
        payload = settles[0]["payload"]
        shown, _ = env.gateway._settle_listing(before, "1.1")
        self.assertEqual({k: payload[k] for k in ("basis", "echo", "shown_sha256", "market_status", "commission_p")},
                         {"basis": "operator_settled_worst_case", "echo": "SETTLE MARKET 1.1",
                          "shown_sha256": canonical_sha256(shown), "market_status": "CLOSED", "commission_p": 0})
        self.assertEqual(sorted((t["state"], t["reason"]) for t in payload["transitions"]),
                         [("SETTLED", "operator_settle_market")] * 2)
        self.assertEqual(settles[0]["market_id"], "1.1")
        self.assertFalse(any(e["kind"] == "operator_confirm" and "SETTLE" in json.dumps(e["payload"])
                             for e in env.journal.events()))
        after = self.fold(env)
        self.assertEqual(exposure_by_market(list(after["orders"].values()))["1.1"], 0)
        self.assertEqual(after["realized_today_pence"], -400)       # the EXECUTABLE remainder booked at worst case
        self.assertEqual(after["orders"][resting["customer_order_ref"]]["size_remaining_p"], 200)
        self.assertEqual(env.gateway.status()["exposure_pence_total"], 0)
        env.gateway.close()
        # Settling again: nothing left to settle, refused before anything is typed.
        prompts = len(env.confirmer.prompts)
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.preconfirm("settle-market", market_id="1.1")
        self.assertEqual(caught.exception.rule, "settle_market_nothing_to_settle")
        self.assertEqual(len(env.confirmer.prompts), prompts)

    def test_settle_market_line_is_typed_before_the_lock(self):
        env, _, _ = self.fixture()
        self.settled_on_exchange(env)
        seen = []

        class LockProbingConfirmer(gw.FakeConfirmer):
            def confirm_line(inner, expected, *, banner):
                try:
                    with file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock):
                        seen.append("lock_free")
                except LockBusy:
                    seen.append("lock_held")
                return super().confirm_line(expected, banner=banner)

        env.gateway.confirmer = LockProbingConfirmer()
        gw.open_live(self, env, "settle-market", settle=True)
        self.assertEqual(seen, ["lock_free"])
        # Typing after open() would hold the lock: refused.
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.preconfirm("settle-market", market_id="1.1")
        self.assertEqual(caught.exception.rule, "preconfirm_after_open")
        with self.assertRaises(NotArmed) as caught:
            env.gateway.settle_market("1.1", confirm_token="live-wrong")
        self.assertEqual(caught.exception.rule, "confirm_token_mismatch")
        self.assertEqual(env.gateway.settle_market("1.1", confirm_token=env.token)["status"], "settled")
        env.gateway.close()
        # A mistyped line is refused at the terminal and journals nothing; an untyped settle is refused.
        env2, _, _ = self.fixture()
        self.settled_on_exchange(env2)
        events = len(list(env2.journal.events()))
        env2.gateway.confirmer = gw.FakeConfirmer(["SETTLE MARKET 1.2"])
        with self.assertRaises(OrderRefused) as caught:
            env2.gateway.preconfirm("settle-market", market_id="1.1")
        self.assertEqual(caught.exception.rule, "operator_echo_mismatch")
        self.assertEqual(len(list(env2.journal.events())), events)
        gw.open_live(self, env2, "settle-market")
        with self.assertRaises(OrderRefused) as caught:
            env2.gateway.settle_market("1.1", confirm_token=env2.token)
        self.assertEqual(caught.exception.rule, "operator_confirmation_missing")
        self.assertFalse(any(e["kind"] == "settle" for e in env2.journal.events()))
        env2.gateway.close()
        with self.assertRaises(ValueError):
            env2.gateway.preconfirm("settle-market", market_id="2.1")
        # Paper has nothing to settle by hand (paper-step settles on a CLOSED book).
        paper = gw.build_paper_gateway(self)
        self.assertEqual(paper.gateway.preconfirm("settle-market", market_id="1.1"), [])
        paper.gateway.open(command="place")
        self.addCleanup(paper.gateway.close)
        with self.assertRaises(OrderRefused) as caught:
            paper.gateway.settle_market("1.1", confirm_token="x")
        self.assertEqual(caught.exception.rule, "settle_market_is_live_only")

    def test_settle_market_refused_unless_closed(self):
        env, _, _ = self.fixture()
        env.exchange.orders.clear()
        refused = self.attempt(env)                                     # the market is still OPEN
        self.assertEqual((refused.rule, refused.detail), ("settle_market_not_closed", {"status": "OPEN"}))
        env.exchange.market_status = "SUSPENDED"
        self.assertEqual(self.attempt(env).detail, {"status": "SUSPENDED"})
        env.exchange.market_status = "CLOSED"
        with patch.object(env.read, "list_market_book", return_value=[]):
            missing = self.attempt(env)
        self.assertEqual(missing.rule, "settle_market_book_missing")
        with patch.object(env.read, "list_market_book",
                          side_effect=TransportError("connection_lost", phase="receive")):
            failed = self.attempt(env)
        self.assertEqual((failed.rule, failed.detail), ("settle_market_book_read_failed", {"code": "connection_lost"}))
        with patch.object(env.read, "list_market_book", return_value=[env.exchange.book_row("1.2")]):
            other = self.attempt(env)
        self.assertEqual(other.rule, "settle_market_not_closed")
        self.assertFalse(any(e["kind"] == "settle" for e in env.journal.events()))
        self.assertEqual({o["state"] for o in self.fold(env)["orders"].values()}, {"EXECUTION_COMPLETE", "EXECUTABLE"})

    def test_settle_market_refused_while_our_bets_are_current(self):
        env, complete, resting = self.fixture()
        env.exchange.market_status = "CLOSED"                          # but Betfair still lists our resting bet
        del env.exchange.orders[complete["bet_id"]]
        refused = self.attempt(env)
        self.assertEqual(refused.rule, "settle_market_bets_still_current")
        self.assertEqual(refused.detail["bet_ids"], [resting["bet_id"]])
        self.assertGreaterEqual(refused.detail["count"], 1)
        # A finished (EXECUTION_COMPLETE) bet still listed refuses too: Betfair has not settled it yet.
        env.exchange.orders.clear()
        env.exchange.add_order(bet_id=complete["bet_id"], ref=complete["customer_order_ref"], strategy_ref="bar-manual",
                               matched=2.0, average=3.5, status="EXECUTION_COMPLETE")
        self.assertEqual(self.attempt(env).detail["bet_ids"], [complete["bet_id"]])
        # A bet carrying our strategy ref in this market, unknown to the journal, refuses as well.
        env.exchange.orders.clear()
        stray = env.exchange.add_order(strategy_ref="bar-manual", ref=None)
        self.assertEqual(self.attempt(env).detail["bet_ids"], [stray])
        self.assertFalse(any(e["kind"] == "settle" for e in env.journal.events()))

    def test_settle_market_refused_with_unresolved_orders(self):
        env, _, _ = self.fixture()
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        gw.open_live(self, env, "place", instructions=[gw.instruction(price_h=370)])
        lost = env.gateway.place("1.1", [gw.instruction(price_h=370)], source="cli_manual",
                                 confirm_token=env.token)["orders"][0]
        env.gateway.close()
        self.settled_on_exchange(env)
        self.assertIn(lost["state"], ("UNKNOWN", "ABSENT_UNCONFIRMED"))
        prompts = len(env.confirmer.prompts)
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.preconfirm("settle-market", market_id="1.1")
        self.assertEqual((caught.exception.rule, caught.exception.detail),
                         ("settle_market_unresolved_orders", {"refs": [lost["customer_order_ref"]]}))
        self.assertEqual(len(env.confirmer.prompts), prompts)           # refused before anything is typed
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.preconfirm("settle-market", market_id="1.9")
        self.assertEqual(caught.exception.rule, "settle_market_no_orders_in_market")

    def test_settle_market_read_failures_refuse(self):
        env, _, _ = self.fixture()
        self.settled_on_exchange(env)
        with patch.object(env.read, "iter_current_orders", side_effect=ApiError("TOO_MANY_REQUESTS", surface="aping")):
            refused = self.attempt(env)
        self.assertEqual(refused.rule, "settle_market_current_read_failed")
        self.assertTrue(refused.detail["code"])
        calls = []
        original = env.read.iter_current_orders

        def second_leg_fails(**kw):
            calls.append(kw)
            if kw.get("bet_ids"):
                raise TransportError("connection_lost", phase="receive")
            return original(**kw)

        with patch.object(env.read, "iter_current_orders", side_effect=second_leg_fails):
            refused = self.attempt(env)
        self.assertEqual((refused.rule, refused.detail),
                         ("settle_market_current_read_failed", {"code": "connection_lost"}))
        self.assertEqual([sorted(c) for c in calls], [["customer_strategy_refs", "market_ids", "order_projection"],
                                                      ["bet_ids", "order_projection"]])
        self.assertEqual(calls[0]["market_ids"], ["1.1"])
        self.assertFalse(any(e["kind"] == "settle" for e in env.journal.events()))

    def test_settle_market_refused_when_the_journal_moved_after_typing(self):
        env, _, resting = self.fixture()
        self.settled_on_exchange(env)
        # (a) Another process moved an order of this market after the operator typed: the listing changed.
        env.gateway.preconfirm("settle-market", market_id="1.1")
        env.journal.append("reconcile", {"reason": "test", "transitions": [
            {"customer_order_ref": resting["customer_order_ref"], "state": "EXECUTABLE",
             "fields": {"size_matched_p": 100, "average_price_matched_h": 360, "size_remaining_p": 100}}]},
            market_id="1.1")
        gw.open_live(self, env, "settle-market")
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.settle_market("1.1", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "settle_market_journal_changed")
        env.gateway.close()
        self.assertFalse(any(e["kind"] == "settle" for e in env.journal.events()))
        # (b) Anything journaled between the fold and the settle append (a concurrent kill's events, say)
        # fails the compare-and-append: nothing is settled.
        gw.open_live(self, env, "settle-market", settle=True)
        real_status = env.gateway._market_status

        def status_then_concurrent_note(market_id):
            env.journal.append("note", {"reason": "concurrent_writer"})
            return real_status(market_id)

        with patch.object(env.gateway, "_market_status", side_effect=status_then_concurrent_note):
            with self.assertRaises(OrderRefused) as caught:
                env.gateway.settle_market("1.1", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "settle_market_journal_changed")
        env.gateway.close()
        self.assertFalse(any(e["kind"] == "settle" for e in env.journal.events()))
        self.assertEqual(self.fold(env)["orders"][resting["customer_order_ref"]]["state"], "EXECUTABLE")
        # Typed again on the current journal, it settles.
        self.assertEqual(self.attempt(env)["status"], "settled")

    def test_settle_market_then_place_same_day_arms(self):
        """Regression R3#3 / K3: the resting bet matched after the journal last looked (its matched size is
        stale, 0) and settled. settle-market books matched + remaining at worst case, so the same-day
        startup reconcile's SETTLED row (sizeSettled 2.00) is no contradiction: no halt, A17 passes."""
        env, complete, resting = self.fixture()
        self.settled_on_exchange(env)
        today = utc_from_ns(env.clock.time_ns() - env.clock.time_ns() % (86400 * 10**9))
        for order in (complete, resting):
            env.exchange.cleared["SETTLED"].append({
                "betId": order["bet_id"], "marketId": "1.1", "customerStrategyRef": "bar-manual",
                "customerOrderRef": order["customer_order_ref"], "sizeSettled": 2.0, "priceMatched": 3.5,
                "profit": -2.0, "placedDate": today, "settledDate": env.clock.utc_now()})
        self.assertEqual(self.attempt(env)["status"], "settled")
        env.exchange.market_status = "OPEN"
        report = gw.open_live(self, env, "place", instructions=[gw.instruction()], market_id="1.2")
        self.assertTrue(report["armed"], report)
        self.assertEqual(env.gateway._startup["contradictions"], [])
        fold = self.fold(env)
        self.assertFalse(fold["halted"], fold["halt_reasons"])
        placed = env.gateway.place("1.2", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual(placed["decision"], "accepted", placed["refusal"])


if __name__ == "__main__":
    unittest.main()
