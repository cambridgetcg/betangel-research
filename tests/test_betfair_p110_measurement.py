"""P1-10 S4: the reduction and race cases through the fake exchange, pilot-run to the fold (tests only; design v4 S4,
Astra's v1 review #5).

pilot-run places one bet on the first market. A second market an hour later is outside the contract's countries, so
its decision is a SKIP and the session stays open past the first off: pilot-run reads the bet's C row and the CLOSED
book there, and offers the settle point, which the gateway refuses (settle_market_bets_still_current, a skip_offer
code: nothing is journaled) because the matched bet is still listed current. settle_bet then moves the bet to
Betfair's cleared SETTLED list with the L fields under test, pilot-capture reads L, and the fold classes the record.

priceReduced true is post_decision_removal whatever the prices (void_or_removal; finalize(removal_classed=True)): L
only gives row 4 at L's size; C and L of equal size give row 1, with no P even though L's price differs. An exact
price mismatch with priceReduced false is row 2, measurement_unresolved. An L row counts only once a PT1 or PT4 receipt
has verified its fields (note §11), so every case where L must count journals a verifying PT1 receipt after the
registration; with none, the record waits for its deadline and C decides (row 3). An unverified or refuted L with
priceReduced true is not pinned here: Yu's Q7 answer (b) makes it measurement_unresolved in a later slice (D1-b).

The race cases: more of the bet matches before the remainder cancel lands, and the rest lapses before it lands (as at
the off; the fake clock is not moved). The lapse is shown by a reconcile read, never by a report of our own request,
so it is withheld until settle (the live evidence rule, reconcile.py) and the run hands the bet off: that case alone
ends on remainder_left_executable, not session_end, and needs two continued captures.

Every value is synthetic. These show the fold over the fake exchange's rows, not Betfair's behaviour.
"""

from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from betangel_research.betfair import pilot_run
from betangel_research.betfair.api import ReadClient
from betangel_research.betfair.receipts import utc_ms_from_ns

import test_betfair_support_pilot as sp
import test_betfair_support_run as sr

S = sr.S
OFF = sr.OFF


def pt1_receipt(env):
    """A PT1 receipt that verifies the L fields, naming the registration note before it (the fold adopts no receipt
    of another registration). It does not change arming: only a PT4 receipt with a matched settled bet does."""
    (registration,) = sr.notes(env, "registration")
    sp.note(env, "pt_receipt", kind="PT1", receipt_sha256="1" * 64,
            registration_receipt_sha256=registration["registration_receipt_sha256"], l_fields_verified=True,
            matched_settled_bets=0, profit_basis="gross")


def settle_bet(env, bet_id, *, size, price, reduced):
    """Betfair settles the bet: listCurrentOrders stops listing it and listClearedOrders lists it SETTLED, with its own
    ids, settled now (after the off) and L's fields as given; priceReduced is a JSON bool."""
    order = env.exchange.orders.pop(bet_id)
    env.exchange.cleared["SETTLED"].append({
        "betId": bet_id, "marketId": order["marketId"], "selectionId": order["selectionId"],
        "customerOrderRef": order["customerOrderRef"], "settledDate": utc_ms_from_ns(env.clock.time_ns()),
        "sizeSettled": size, "priceMatched": price, "priceReduced": reduced})


def cancel_race(env, *, matched, lapse=False):
    """When the remainder cancel arrives the bet has matched `matched` (£) in all, more than its place report showed;
    with lapse, the rest has lapsed before the cancel lands (the market treated as turned in play; the fake clock is
    not moved), so the cancel finds nothing open. Once only."""
    exchange = env.exchange

    def handler(params):
        exchange.cancel_handler = None
        for instruction in params["instructions"]:
            order = exchange.orders[instruction["betId"]]
            rest = round(order["priceSize"]["size"] - matched, 2)
            order.update(sizeMatched=matched, sizeRemaining=rest, averagePriceMatched=order["priceSize"]["price"])
            if lapse:
                order.update(sizeLapsed=rest, sizeRemaining=0.0, status="EXECUTION_COMPLETE")
        return exchange.cancel(params)

    exchange.cancel_handler = handler


def record(env, now_ns=None):
    (rec,) = sr.fold(env, now_ns).place_records
    return rec


def outcome(rec):
    return {"row": rec.execution["row"], "size": rec.execution["size"], "P": rec.execution["P"],
            "class": rec.measurement["class"]}


def ledger(rec):
    return rec.ledger["part"], rec.ledger["value_p"], rec.ledger["reason"]


class MeasurementCase(unittest.TestCase):
    def build(self, *, receipt=True, complete=True):
        env = sr.build_run(self)
        code, out, err = sr.register(env)
        self.assertEqual(code, 0, err)
        if receipt:
            pt1_receipt(env)
        self.market = sr.add_market(env, off_ns=OFF)
        sr.add_market(env, off_ns=OFF + 3600 * S, country="FR")
        if complete:
            sr.fills(env, complete=True)
        sr.closes_after_off(env, self.market)
        return env

    def run_session(self, env):
        """pilot-run: one PLACE record (a PT4 one: the PT1 receipt arms nothing), the SKIP, and a refused settle offer
        that journals nothing."""
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertEqual((summary["place_records"], summary["pt4_records"], summary["skips"]),
                         (1, 1, {"country_not_allowed": 1}))
        self.assertIn(f"settle {self.market.market_id}: settle_market_bets_still_current", err)
        self.assertEqual([e for e in sr.events(env) if e["kind"] == "settle"], [])

    def c_price(self, env):
        """The captured C row's matched price, from which each case derives L's."""
        (c,) = sr.notes(env, "capture_c")
        return Decimal(c["row"]["averagePriceMatched"])

    def settled(self, env, *extra, size, price, reduced, codes=None):
        """Betfair settles the bet after the race, then pilot-capture reads L (and C when still owed) -> the record,
        folded now."""
        env.clock.advance(600)
        (bet_id,) = env.exchange.orders
        settle_bet(env, bet_id, size=size, price=price, reduced=reduced)
        code, out, err = sr.capture(env, *extra)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["written"], result["codes"]), (1, codes or {}))
        self.assertEqual(len(sr.notes(env, "capture_l")), 1)
        return record(env)

    def no_review_stop(self, env, now_ns=None, *, session_stop="session_end"):
        """Nothing due, and the one active stop is the session's own (design S4: every case but sizes that differ)."""
        state = sr.fold(env, now_ns)
        self.assertEqual(state.due_stops, ())
        self.assertEqual([(s["scope"], s["reason"]) for s in state.active_stops], [("session", session_stop)])
        # Once S3 has merged: also no finalized_mismatch and no bsp_decided_mismatch here (design v4 S4).


class ReductionTests(MeasurementCase):
    """priceReduced true decides post_decision_removal before any price comparison (note §11 class 4)."""

    def capturing(self):
        """A flag that is set while a capture C read (ReadClient.raw_current_order) runs, as in test_betfair_pilot_run's
        CaptureTests."""
        flag = []
        original = ReadClient.raw_current_order

        def raw(client, bet_id, **kw):
            flag.append(True)
            try:
                return original(client, bet_id, **kw)
            finally:
                flag.clear()

        patcher = patch.object(ReadClient, "raw_current_order", raw)
        patcher.start()
        self.addCleanup(patcher.stop)
        return flag

    def test_reduction_l_only(self):
        """1.50 of the £2 matches and the rest is cancelled, so L's size is not the stake. No C row is ever captured:
        in pilot-run every C read names another selection (c_row_differs), and by pilot-capture the settled bet is no
        longer listed current (c_bet_not_listed). L alone: row 4 at L's 1.50, no P, W at -150 for the removal."""
        env = self.build(complete=False)
        sr.fills(env, matched=1.5)
        flag = self.capturing()
        original = env.exchange.current
        env.exchange.current = lambda **kw: [dict(r, selectionId=99) for r in original(**kw)] if flag else \
            original(**kw)
        codes, capture = [], pilot_run.capture

        def spy(*args, **kw):
            out = capture(*args, **kw)
            codes.extend(out["codes"])
            return out

        with patch.object(pilot_run, "capture", spy):
            self.run_session(env)
        self.assertIn("c_row_differs", codes)
        (price,) = {order["averagePriceMatched"] for order in env.exchange.orders.values()}    # with no C row
        rec = self.settled(env, size=1.5, price=round(price - 0.20, 2), reduced=True, codes={"c_bet_not_listed": 1})
        self.assertEqual(sr.notes(env, "capture_c"), [])
        self.assertEqual(rec.c_reads, ())
        self.assertEqual(outcome(rec), {"row": 4, "size": Decimal("1.5"), "P": None, "class": "post_decision_removal"})
        self.assertEqual(ledger(rec), ("W", -150, "removal"))
        self.no_review_stop(env)

    def test_reduction_c_and_l(self):
        """C and L of equal size: row 1 at L's size with no P, although L's price is below C's (Betfair reports the
        reduced price in L)."""
        env = self.build()
        self.run_session(env)
        price = self.c_price(env)
        rec = self.settled(env, size=2.0, price=float(price - Decimal("0.20")), reduced=True)
        self.assertEqual(outcome(rec), {"row": 1, "size": Decimal("2.0"), "P": None, "class": "post_decision_removal"})
        self.assertEqual(ledger(rec), ("W", -200, "removal"))
        self.no_review_stop(env)

    def test_reduction_with_a_smaller_l_size(self):
        """Optional in the design, existing fail-closed behaviour: L settles less than C matched. The sizes differ (row
        2, size unresolved), the removal still classes it, and L contradicts the C size basis: W at -200 for good and a
        ledger_contradiction review stop."""
        env = self.build()
        self.run_session(env)
        price = self.c_price(env)
        rec = self.settled(env, size=1.5, price=float(price - Decimal("0.20")), reduced=True)
        self.assertEqual(outcome(rec), {"row": 2, "size": None, "P": None, "class": "post_decision_removal"})
        self.assertEqual(ledger(rec), ("W", -200, "contradiction"))
        state = sr.fold(env)
        self.assertEqual(state.due_stops, ())
        self.assertEqual([s["reason"] for s in state.active_stops if s["scope"] == "review"], ["ledger_contradiction"])
        # Once S3 has merged: also no finalized_mismatch and no bsp_decided_mismatch here (design v4 S4).


class UnreducedTests(MeasurementCase):
    """priceReduced false: C and L are compared exactly."""

    def test_unreduced_exact_c_l_mismatch(self):
        """Equal sizes and prices unequal only below a penny: row 2 (finalize compares exactly), measurement_unresolved,
        W at -200 worst case, and no stop."""
        env = self.build()
        self.run_session(env)
        price = self.c_price(env)
        rec = self.settled(env, size=2.0, price=float(price + Decimal("0.004")), reduced=False)
        self.assertEqual(outcome(rec), {"row": 2, "size": None, "P": None, "class": "measurement_unresolved"})
        self.assertEqual(ledger(rec), ("W", -200, "worst_case"))
        self.no_review_stop(env)

    def test_unreduced_equal_control(self):
        """Equal sizes and prices: row 1 priced from L; missing_bsp, because the fake CLOSED book carries no SP."""
        env = self.build()
        self.run_session(env)
        price = self.c_price(env)
        rec = self.settled(env, size=2.0, price=float(price), reduced=False)
        self.assertEqual(outcome(rec), {"row": 1, "size": Decimal("2.0"), "P": price, "class": "missing_bsp"})
        self.no_review_stop(env)

    def test_mismatch_needs_the_receipt(self):
        """With no PT receipt the L row does not count (note §11): the mismatch above leaves the record open, at -200
        by ledger rule d on its checked C read; after its deadline C alone decides (row 3)."""
        env = self.build(receipt=False)
        self.run_session(env)
        price = self.c_price(env)
        rec = self.settled(env, size=2.0, price=float(price + Decimal("0.004")), reduced=False)
        self.assertFalse(rec.execution["final"])
        self.assertEqual((rec.ledger["basis"]["rule"], ledger(rec)), ("d", ("W", -200, "checked_current_read")))
        self.no_review_stop(env)
        later = record(env, rec.deadline_ns + 1)
        self.assertEqual(outcome(later), {"row": 3, "size": Decimal("2.0"), "P": price, "class": "missing_bsp"})
        self.assertEqual((later.execution["c_seq"], later.execution["l_seq"]), (rec.c_reads[0]["seq"], None))
        self.no_review_stop(env, rec.deadline_ns + 1)


class RaceTests(MeasurementCase):
    """The remainder cancel races the exchange (the pure cases are Finalization.test_cancel_race_and_lapse)."""

    def test_cancel_race(self):
        """The place report shows 0.50 matched; 1.20 has matched when the cancel lands, so it cancels 0.80, short of
        the 1.50 the journal had open, and a read by bet id resolves the bet. C and L give row 1 at 1.20."""
        env = self.build(complete=False)
        sr.fills(env, matched=0.5)
        cancel_race(env, matched=1.2)
        self.run_session(env)
        (c,) = sr.notes(env, "capture_c")
        self.assertEqual((c["row"]["sizeMatched"], c["row"]["sizeCancelled"]), ("1.2", "0.8"))
        price = self.c_price(env)
        rec = self.settled(env, size=1.2, price=float(price), reduced=False)
        self.assertEqual(outcome(rec), {"row": 1, "size": Decimal("1.2"), "P": price, "class": "missing_bsp"})
        self.assertFalse(rec.execution["consistency_failed"])
        self.no_review_stop(env)

    def test_lapse_at_the_off(self):
        """Unmatched at placement; 1.00 has matched and 1.00 lapsed before the cancel lands, so it fails
        (BET_TAKEN_OR_LAPSED). The measurement pins: row 1 at 1.00, consistency not failed, no review stop.

        The rest is handoff context, today's fail-closed path rather than a measurement rule: a lapse that only a
        reconcile read shows is withheld until settle (reconcile.py), so pilot-run hands the bet off (exit 5) and ends
        on the remainder_left_executable session stop, keeping admission, and two continued pilot-captures read C and
        the CLOSED book, then L. A change to the handoff updates these lines, not the measurement."""
        env = self.build(complete=False)
        cancel_race(env, matched=1.0, lapse=True)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(sr.notes(env, "capture_c"), [])
        generation = str(sr.admission_view(env)["admission"]["generation"])
        env.clock.advance(600)                                       # past the off: the book reads CLOSED
        code, out, err = sr.capture(env, "--continue-admission", generation)
        self.assertEqual((code, json.loads(out)["written"]), (0, 2), err)
        (c,) = sr.notes(env, "capture_c")
        self.assertEqual((c["row"]["sizeMatched"], c["row"]["sizeLapsed"]), ("1.0", "1.0"))
        price = self.c_price(env)
        rec = self.settled(env, "--continue-admission", generation, size=1.0, price=float(price), reduced=False)
        self.assertEqual(outcome(rec), {"row": 1, "size": Decimal("1.0"), "P": price, "class": "missing_bsp"})
        self.assertFalse(rec.execution["consistency_failed"])
        self.no_review_stop(env, session_stop="remainder_left_executable")


if __name__ == "__main__":
    unittest.main()
