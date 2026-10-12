"""RiskGuard rules R1-R12, adversarial exposure and capability issue, on hand-computed synthetic cases."""

import copy
import unittest

from betangel_research.betfair import risk
from betangel_research.betfair.capability import OrderCapability
from betangel_research.betfair.errors import ConfigError, OrderRefused
from betangel_research.betfair.receipts import bytes_sha256, canonical_json, utc_from_ns
from betangel_research.betfair.ticks import wire_price, wire_size

import test_betfair_support as support
import test_betfair_support_orders as orders

NOW = orders.NOW_NS
SEL = 12345


def record(n, **changes):
    changes.setdefault("customer_order_ref", f"bro200101120000-{n:016x}")
    return orders.order_record(**changes)


def rules(decision):
    return [f["rule"] for f in decision["detail"]["failures"]]


def place_params(decision, *, ref=orders.REF, cref=orders.CREF, **overrides):
    ins = decision["instructions"][0]
    params = {"marketId": decision["market_id"], "customerRef": cref, "customerStrategyRef": "bar-manual",
              "marketVersion": {"version": decision["market_version"]},
              "instructions": [{"orderType": "LIMIT", "selectionId": ins["selection_id"], "handicap": 0,
                                "side": ins["side"], "customerOrderRef": ref,
                                "limitOrder": {"size": wire_size(ins["size_p"]), "price": wire_price(ins["price_h"]),
                                               "persistenceType": "LAPSE"}}]}
    params.update(overrides)
    return params


def prepared(method, params, rpc_id=7):
    body = canonical_json({"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id})
    return {"method": method, "body": body, "body_sha256": bytes_sha256(body), "rpc_id": rpc_id}


class ExposureTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_worst_case_hand_computed_back_lay(self):
        back = record(1, side="BACK", price_h=350, size_p=200)
        lay = record(2, side="LAY", price_h=350, size_p=200)
        self.assertEqual(risk.worst_case_market_loss([back], [SEL]), 200)        # stake lost if another wins
        self.assertEqual(risk.worst_case_market_loss([lay], [SEL]), 500)         # 2.00 x 2.50 if SEL wins
        self.assertEqual(risk.adverse_contrib(lay, SEL), -500)
        self.assertEqual(risk.adverse_contrib(lay, risk.OTHER), 0)
        self.assertEqual(risk.adverse_contrib(back, SEL), 0)
        matched_back = record(3, side="BACK", price_h=350, state="EXECUTION_COMPLETE", size_matched_p=200,
                              average_price_matched_h=350, size_remaining_p=0)
        matched_lay = record(4, side="LAY", price_h=300, state="EXECUTION_COMPLETE", size_matched_p=200,
                             average_price_matched_h=300, size_remaining_p=0)
        self.assertEqual(risk.exact_contrib(matched_back, SEL), 500)
        self.assertEqual(risk.exact_contrib(matched_back, risk.OTHER), -200)
        # With hedge credit (paper): +500 - 400 if SEL wins, -200 + 200 otherwise -> no loss anywhere.
        self.assertEqual(risk.worst_case_market_loss([matched_back, matched_lay], [SEL], hedge_credit=True), 0)
        # Without it (the default; live, amendment A3 C1) each matched leg counts its loss only:
        # the lay's -400 if SEL wins, the back's -200 otherwise -> 400.
        self.assertEqual(risk.worst_case_market_loss([matched_back, matched_lay], [SEL]), 400)
        # A resting lay is counted adversely only: matched back +500, resting lay -400 if SEL wins; -200 otherwise.
        resting_lay = record(5, side="LAY", price_h=300, size_p=200)
        self.assertEqual(risk.worst_case_market_loss([matched_back, resting_lay], [SEL], hedge_credit=True), 200)
        self.assertEqual(risk.worst_case_market_loss([matched_back, resting_lay], [SEL]), 400)
        # 1.01 @ 1.05 laid: 5.05p is rounded up to 6p.
        self.assertEqual(risk.worst_case_market_loss([record(6, side="LAY", price_h=105, size_p=101)], []), 6)
        # A matched lay with no known average price is valued at its (worse) limit price.
        no_avg = dict(matched_lay, average_price_matched_h=None, price_h=320)
        self.assertEqual(risk.worst_case_market_loss([no_avg], []), 440)
        settled = dict(matched_lay, state="SETTLED")
        self.assertEqual(risk.worst_case_market_loss([settled], [SEL]), 0)

    def test_unknown_absent_counted_full_adverse(self):
        for state in ("UNKNOWN", "ABSENT_UNCONFIRMED", "SUBMITTING", "CANCEL_PENDING", "REPLACE_PENDING"):
            with self.subTest(state=state):
                lay = record(1, side="LAY", price_h=350, size_p=200, state=state, size_remaining_p=0)
                self.assertEqual(lay["exposure_basis"], "adverse_unknown")
                self.assertEqual(risk.worst_case_market_loss([lay], [SEL]), 500)
        unknown = record(2, side="LAY", price_h=350, state="UNKNOWN", size_matched_p=50,
                         average_price_matched_h=350, size_remaining_p=150)
        self.assertEqual(risk.worst_case_market_loss([unknown], [SEL]), 500)      # 125 exact + 375 adverse
        # Remaining is never trusted below what is unaccounted for.
        understated = dict(unknown, size_remaining_p=0)
        self.assertEqual(risk.worst_case_market_loss([understated], [SEL]), 500)

    def test_outcomes_include_selection_missing_from_book(self):
        lay = record(1, side="LAY", price_h=1000, size_p=100, selection_id=999)
        self.assertEqual(risk.worst_case_market_loss([lay], [SEL]), 900)
        guard = risk.RiskGuard(orders.order_config({"orders": {"caps": {"max_market_exposure": 9.5}}}), mode="paper")
        state = orders.state_with([lay])
        decision = guard.evaluate_place(support.MARKET_ID, [orders.instruction(side="LAY", price_h=355, size_p=100)],
                                        book=orders.snapshot(), state=state, arming=orders.ARMED, now_ns=NOW,
                                        kill_engaged=False)
        # 999 is absent from the book, yet "999 wins" (-900) is an outcome: the new lay only adds when SEL wins.
        self.assertEqual(decision["worst_case"]["market_pence"], 900)
        self.assertTrue(decision["approved"])

    def test_provisional_child_worse_of_two(self):
        parent = record(1, side="LAY", price_h=350, state="REPLACE_PENDING", size_remaining_p=200, child_ref=None)
        child = record(2, side="LAY", price_h=400, state="SUBMITTING", provisional_child=True,
                       parent_ref=parent["customer_order_ref"], bet_id=None)
        # Parent alone 500, child alone 600: together they count 600 (one of them, never both).
        self.assertEqual(risk.worst_case_market_loss([parent], [SEL]), 500)
        self.assertEqual(risk.worst_case_market_loss([child], [SEL]), 600)
        self.assertEqual(risk.worst_case_market_loss([parent, child], [SEL]), 600)
        both_unknown = [dict(parent, state="UNKNOWN"), dict(child, state="UNKNOWN")]
        self.assertEqual(risk.worst_case_market_loss(both_unknown, [SEL]), 600)
        backs = [dict(parent, side="BACK"), dict(child, side="BACK")]
        self.assertEqual(risk.worst_case_market_loss(backs, [SEL]), 200)
        # A child that resolved is no longer paired: both count.
        resolved = dict(child, state="EXECUTABLE", provisional_child=False, bet_id="9")
        self.assertEqual(risk.worst_case_market_loss([dict(parent, state="EXECUTABLE"), resolved], [SEL]), 1100)


class GuardTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.clock = support.FakeClock()
        self.config = orders.order_config()

    def paper(self, instructions=None, *, state=None, book=None, config=None, kill_engaged=False, now_ns=NOW,
              market_id=support.MARKET_ID, **kwargs):
        guard = risk.RiskGuard(config or self.config, mode="paper")
        return guard.evaluate_place(market_id, instructions or [orders.instruction()],
                                    book=book or orders.snapshot(), state=state or orders.empty_state(),
                                    arming=orders.ARMED, now_ns=now_ns, kill_engaged=kill_engaged, **kwargs)

    def live_guard(self, config=None):
        return risk.RiskGuard(config or self.config, mode="live", clock=self.clock)

    def funds(self, available=100.0, exposure=0.0):
        return {"availableToBetBalance": available, "exposure": exposure, "retainedCommission": 0.0,
                "exposureLimit": -1000.0, "discountRate": 0.0, "pointsBalance": 0}

    def live(self, instructions=None, *, guard=None, state=None, funds="default", arming=orders.ARMED, **kwargs):
        """A live evaluate_place with refs bound and funds just fetched, unless the test says otherwise."""
        guard = guard or self.live_guard()
        state = state or orders.empty_state("live", self.clock.time_ns())
        instructions = instructions or [orders.instruction()]
        kwargs.setdefault("customer_ref", orders.CREF)
        kwargs.setdefault("customer_order_refs", [f"bro200101120000-{i:016x}" for i in range(len(instructions))])
        if funds is not None:
            kwargs.setdefault("funds_checked_at", self.clock.utc_now())
        return guard.evaluate_place(support.MARKET_ID, instructions,
                                    book=kwargs.pop("book", None) or orders.live_book(self.clock), state=state,
                                    arming=arming, funds=self.funds() if funds == "default" else funds,
                                    now_ns=self.clock.time_ns(), now_monotonic_ns=self.clock.mono_ns,
                                    kill_engaged=kwargs.pop("kill_engaged", False), **kwargs)

    def test_baseline_approved_and_shape(self):
        decision = self.paper()
        self.assertTrue(decision["approved"], decision["detail"]["failures"])
        self.assertIsNone(decision["rule"])
        self.assertEqual(decision["worst_case"], {"market_pence": 200, "total_pence": 200, "daily_used_pence": 200})
        self.assertEqual(decision["evaluated_at"], utc_from_ns(NOW))
        self.assertRegex(decision["decision_id"], r"^dec-[0-9a-f]{24}$")
        self.assertTrue(self.live()["approved"])

    def test_stake_and_liability_caps(self):
        self.assertIn("stake_cap", rules(self.paper([orders.instruction(size_p=201)])))
        ok = self.paper([orders.instruction(side="LAY", price_h=600, size_p=200)],
                        book=orders.snapshot(runners={"12345": orders.runner(back=((5.9, 9.0),), lay=((6.0, 9.0),),
                                                                              ltp=6.0)}))
        self.assertTrue(ok["approved"], ok["detail"]["failures"])           # liability exactly 10.00
        high = self.paper([orders.instruction(side="LAY", price_h=620, size_p=200)],
                          book=orders.snapshot(runners={"12345": orders.runner(back=((6.0, 9.0),), lay=((6.2, 9.0),),
                                                                                ltp=6.2)}))
        self.assertEqual(high["rule"], "stake_cap")
        self.assertIn("liability_above_max_liability_per_order",
                      [f["reason"] for f in high["detail"]["failures"]])

    def test_market_and_total_caps(self):
        existing = record(1, side="LAY", price_h=1000, size_p=211, selection_id=11)       # 1899p if 11 wins
        state = orders.state_with([existing])
        self.assertTrue(self.paper([orders.instruction(size_p=100)], state=state)["approved"])
        self.assertTrue(self.paper([orders.instruction(size_p=101)], state=state)["approved"])   # exactly 2000
        refused = self.paper([orders.instruction(size_p=102)], state=state)
        self.assertEqual(refused["rule"], "market_exposure")                   # 1899 + 102 > 2000 if 11 wins
        other = record(10, side="LAY", price_h=1000, size_p=522, market_id="1.20", selection_id=11)   # 4698p
        loose = orders.order_config({"orders": {"caps": {"max_daily_loss": 200.0}}})

        def with_back(size):
            return orders.state_with([other, record(3, side="BACK", price_h=350, size_p=size, selection_id=12346)])

        # Market 1.1 then loses 100 + 200 if neither of our selections wins: 4698 + 300 = 4998.
        self.assertTrue(self.paper([orders.instruction(size_p=200)], state=with_back(100), config=loose)["approved"])
        over = self.paper([orders.instruction(size_p=200)], state=with_back(104), config=loose)
        self.assertEqual(over["rule"], "total_exposure")
        self.assertEqual(over["worst_case"]["total_pence"], 4698 + 304)

    def test_daily_loss_profits_do_not_extend(self):
        state = orders.state_with([], realized_today_pence=-1850)
        self.assertTrue(self.paper([orders.instruction(size_p=150)], state=state)["approved"])
        self.assertEqual(self.paper([orders.instruction(size_p=151)], state=state)["rule"], "daily_loss")
        lays = [record(1, side="LAY", price_h=1000, size_p=201, selection_id=11, market_id="1.30")]
        rich = orders.state_with(lays, realized_today_pence=50_000)
        decision = self.paper([orders.instruction(size_p=200)], state=rich)
        self.assertEqual(decision["rule"], "daily_loss")                        # 1809 + 200 > 2000 despite profit
        floor = orders.state_with([], realized_today_pence=0,
                                  exchange_floor={"orders_today": 0, "realized_today_pence": -1900})
        self.assertEqual(self.paper(state=floor)["rule"], "daily_loss")         # the worse of journal and exchange
        unknown = orders.state_with([], realized_today_pence=0, unsettled_matched_liability_today_pence=1900,
                                    exchange_floor={"orders_today": 0, "realized_today_pence": None})
        self.assertEqual(self.paper(state=unknown)["rule"], "daily_loss")

    def test_daily_loss_unknown_realised_counts_settled_liability(self):
        """Regression: an unknown exchange realised value counts as -(matched liability of today's
        settled orders), so a journal win cannot hide it (spec section 10 R10)."""
        guard = self.live_guard()
        # Today a LAY 1.00 @ 20.00 settled as a +1.00 win per the journal; its matched liability was 19.00.
        settled = record(1, side="LAY", price_h=2000, size_p=100, state="SETTLED", size_matched_p=100,
                         average_price_matched_h=2000, settled_profit_own_p=100, mode="live",
                         submitted_at=support.BASE_UTC, market_id="1.50")
        base = dict(realized_today_pence=100, unsettled_matched_liability_today_pence=0,
                    settled_matched_liability_today_pence=1900)
        known = orders.state_with([settled], mode="live", now_ns=self.clock.time_ns(),
                                  exchange_floor={"orders_today": 1, "realized_today_pence": 100}, **base)
        self.assertTrue(self.live(guard=guard, state=known)["approved"])
        unknown = orders.state_with([settled], mode="live", now_ns=self.clock.time_ns(),
                                    exchange_floor={"orders_today": 1, "realized_today_pence": None}, **base)
        decision = self.live(guard=guard, state=unknown)
        self.assertEqual(decision["rule"], "daily_loss")
        self.assertEqual(decision["worst_case"]["daily_used_pence"], 1900 + 200)
        # A state built without the fold's settled figure counts the whole daily cap as used (amendment A3, A26).
        bare = copy.deepcopy(unknown)
        del bare["settled_matched_liability_today_pence"]
        self.assertEqual(self.live(guard=guard, state=bare)["rule"], "daily_loss")
        # Through the real fold: the settled order's liability is reported for today only.
        journal = orders.MemoryJournal("live", self.clock)
        journal.append("intent", orders.intent_payload(orders.instruction(side="LAY", price_h=2000, size_p=100),
                                                       source="cli_manual"),
                       intent_id="i1", customer_order_ref=orders.REF, customer_ref=orders.CREF,
                       market_id="1.50")
        journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                  "transitions": [{"customer_order_ref": orders.REF, "state": "SUBMITTING"}]})
        journal.append("report", {"transitions": [{"customer_order_ref": orders.REF, "state": "EXECUTION_COMPLETE",
                                                   "fields": {"bet_id": "1", "size_matched_p": 100,
                                                              "average_price_matched_h": 2000}}]})
        journal.append("settle", {"commission_p": 0, "transitions": [
            {"customer_order_ref": orders.REF, "state": "SETTLED", "fields": {"settled_profit_own_p": 100}}]})
        journal.append("startup_reconcile", {"exchange_orders_today": 1, "exchange_realized_today_pence": None})
        folded = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual((folded["realized_today_pence"], folded["settled_matched_liability_today_pence"]), (100, 1900))
        self.assertEqual(self.live(guard=guard, state=folded)["rule"], "daily_loss")
        tomorrow = journal.fold(now_ns=self.clock.time_ns() + 86400 * 10**9)
        self.assertEqual(tomorrow["settled_matched_liability_today_pence"], 0)

    def test_realized_loss_without_fold_fields_counts_the_cap_used(self):
        """Amendment A3 (section 2b, A26): with an unknown realised value, R10 needs the fold's
        settled_matched_liability_today_pence and unsettled_matched_liability_today_pence. When either is missing or
        invalid the guard no longer recomputes them from the orders (the SETTLED recompute undercounted an
        EXECUTABLE -> SETTLED remainder): the whole daily cap counts as used, so even a 1.00 place is refused."""
        guard = self.live_guard()
        settled = record(1, side="BACK", price_h=350, size_p=100, state="SETTLED", size_remaining_p=100, mode="live",
                         submitted_at=support.BASE_UTC, market_id="1.50")
        base = dict(realized_today_pence=0, unsettled_matched_liability_today_pence=0,
                    settled_matched_liability_today_pence=100,
                    exchange_floor={"orders_today": 1, "realized_today_pence": None})
        state = orders.state_with([settled], mode="live", now_ns=self.clock.time_ns(), **base)
        self.assertEqual(guard._realized_loss(state), 100)
        self.assertTrue(self.live([orders.instruction(size_p=100)], guard=guard, state=state)["approved"])
        cap = guard.caps_p["max_daily_loss"]
        for key in ("settled_matched_liability_today_pence", "unsettled_matched_liability_today_pence"):
            for value in (None, -1, "100", 1.5, "absent"):
                with self.subTest(key=key, value=value):
                    broken = copy.deepcopy(state)
                    if value == "absent":
                        del broken[key]
                    else:
                        broken[key] = value
                    self.assertEqual(guard._realized_loss(broken), cap)
                    decision = self.live([orders.instruction(size_p=100)], guard=guard, state=broken)
                    self.assertEqual(decision["rule"], "daily_loss")
                    self.assertEqual(decision["worst_case"]["daily_used_pence"], cap + 100)
        # Known realised values need neither field.
        known = copy.deepcopy(state)
        known["exchange_floor"] = {"orders_today": 1, "realized_today_pence": 0}
        del known["settled_matched_liability_today_pence"]
        self.assertEqual(guard._realized_loss(known), 0)

    def test_rate_windows_count_submitted_instructions(self):
        recent = [NOW - i * 10**9 for i in range(5)]
        self.assertTrue(self.paper(state=orders.state_with([], submitted_instruction_wall_ns=recent))["approved"])
        six = recent + [NOW - 30 * 10**9]
        self.assertEqual(self.paper(state=orders.state_with([], submitted_instruction_wall_ns=six))["rule"],
                         "rate_minute")
        old = [NOW - 61 * 10**9 - i * 10**9 for i in range(59)]
        self.assertTrue(self.paper(state=orders.state_with([], submitted_instruction_wall_ns=old))["approved"])
        self.assertEqual(self.paper(state=orders.state_with([], submitted_instruction_wall_ns=old + [NOW - 10**9]))
                         ["rule"], "rate_hour")
        floor = orders.state_with([], exchange_floor={"orders_today": 200, "realized_today_pence": 0})
        self.assertEqual(self.paper(state=floor)["rule"], "rate_day")
        today = orders.state_with([], orders_today=200)
        self.assertEqual(self.paper(state=today)["rule"], "rate_day")
        # Refused intents are never submitted, so they never count: built through the real fold.
        journal = orders.MemoryJournal("paper", self.clock)
        for n in range(8):
            ref = f"bro200101120000-{n:016x}"
            journal.append("intent", orders.intent_payload(), intent_id=f"i{n}", customer_order_ref=ref,
                           customer_ref=orders.CREF, market_id=support.MARKET_ID)
            journal.append("risk_decision", {"transitions": [{"customer_order_ref": ref, "state": "REFUSED"}]})
        self.assertTrue(self.paper(state=journal.fold(now_ns=NOW))["approved"])

    def test_clock_step_back_does_not_reopen_window(self):
        six = [NOW - i * 10**9 for i in range(6)]
        state = orders.state_with([], submitted_instruction_wall_ns=six)
        stepped = NOW - 120 * 10**9
        decision = self.paper(state=state, now_ns=stepped, book=orders.snapshot(observed_at=utc_from_ns(stepped)))
        self.assertIn("rate_minute", rules(decision))
        # Through the fold, the same step back also raises clock_regression, which halts (R1).
        journal = orders.MemoryJournal("paper", self.clock)
        journal.append("note", {})
        self.assertTrue(journal.fold(now_ns=self.clock.time_ns() - 6 * 10**9)["clock_regression"])
        self.assertEqual(self.paper(state=journal.fold(now_ns=NOW - 6 * 10**9), now_ns=NOW - 6 * 10**9,
                                    book=orders.snapshot(observed_at=utc_from_ns(NOW - 6 * 10**9)))["rule"], "halted")

    def test_transactions_clock_hour_failed_cancel_counts(self):
        journal = orders.MemoryJournal("paper", self.clock)
        journal.append("note", {"transactions": 299})
        state = journal.fold(now_ns=NOW)
        self.assertEqual(sum(state["transactions_by_clock_hour"].values()), 299)
        self.assertTrue(self.paper(state=state)["approved"])
        # Regression: a replace reserves 2 transactions (1, plus 1 if its cancel half fails).
        parent = record(1, side="LAY", price_h=300, size_p=200, bet_id="312345678901")
        replace_book = orders.snapshot(runners={"12345": orders.runner(back=((2.98, 50.0),), lay=((3.0, 50.0),),
                                                                       ltp=3.0)})
        guard = risk.RiskGuard(self.config, mode="paper")

        def replace(transactions):
            return guard.evaluate_replace(support.MARKET_ID, [{"bet_id": "312345678901", "new_price_h": 305}],
                                          book=replace_book, now_ns=NOW, arming=orders.ARMED, kill_engaged=False,
                                          state=orders.state_with([parent], transactions_by_clock_hour={
                                              utc_from_ns(NOW)[:13]: transactions}))

        self.assertTrue(replace(298)["approved"])
        self.assertEqual(replace(299)["rule"], "transactions_hour")
        journal.append("report", {"action": "cancel", "transactions": 1})
        self.assertEqual(self.paper(state=journal.fold(now_ns=NOW))["rule"], "transactions_hour")
        next_hour = NOW + 3600 * 10**9
        decision = self.paper(state=journal.fold(now_ns=next_hour), now_ns=next_hour,
                              book=orders.snapshot(observed_at=utc_from_ns(next_hour)))
        self.assertTrue(decision["approved"], decision["detail"]["failures"])

    def test_cancel_allowed_when_halted(self):
        bet = record(1, bet_id="312345678901")
        state = orders.state_with([bet], halted=True, halt_reasons=["kill"], clock_regression=True)
        guard = risk.RiskGuard(self.config, mode="live", clock=self.clock)
        decision = guard.evaluate_cancel(support.MARKET_ID, ["312345678901"], state=state, now_ns=NOW)
        self.assertTrue(decision["approved"], decision["detail"]["failures"])
        self.assertEqual(decision["action"], "cancel")
        foreign = guard.evaluate_cancel(support.MARKET_ID, ["999999"], state=state, now_ns=NOW)
        self.assertEqual(foreign["rule"], "validation:V2")
        account = guard.evaluate_cancel(None, [], state=state, now_ns=NOW, account_wide=True)
        self.assertTrue(account["approved"])
        self.assertEqual(account["action"], "cancel_all")
        self.assertFalse(guard.evaluate_cancel(support.MARKET_ID, [], state=state, now_ns=NOW,
                                               account_wide=True)["approved"])
        self.assertEqual(self.paper(state=state)["rule"], "halted")

    def test_kill_sweep_can_cancel_every_unresolved_own_bet(self):
        """Regression: the guard approves cancels of our UNKNOWN, ABSENT_UNCONFIRMED, CANCEL_PENDING,
        REPLACE_PENDING orders and provisional children (spec section 9.8 target set), and of bets the
        sweep proved ours on the exchange, but never of a bet nobody proved ours."""
        guard = risk.RiskGuard(self.config, mode="live", clock=self.clock)
        unresolved = [record(1, state="UNKNOWN", bet_id="312345678901", mode="live"),
                      record(2, state="ABSENT_UNCONFIRMED", bet_id="312345678902", mode="live"),
                      record(3, state="CANCEL_PENDING", bet_id="312345678903", mode="live"),
                      record(4, state="REPLACE_PENDING", bet_id="312345678904", mode="live"),
                      record(5, state="UNKNOWN", bet_id="312345678905", mode="live", price_h=360,
                             parent_ref=record(4)["customer_order_ref"], provisional_child=True)]
        bets = [o["bet_id"] for o in unresolved]
        killed = orders.state_with(unresolved, mode="live", halted=True, halt_reasons=["kill"])
        decision = guard.evaluate_cancel(support.MARKET_ID, bets, state=killed, now_ns=NOW, customer_ref=orders.CREF)
        self.assertTrue(decision["approved"], decision["detail"]["failures"])
        # Manual cancels of the same unresolved bets are allowed too (a cancel only reduces exposure).
        open_state = orders.state_with(unresolved, mode="live")
        self.assertTrue(guard.evaluate_cancel(support.MARKET_ID, bets, state=open_state, now_ns=NOW)["approved"])
        # Bets found only on the exchange by the strategy-ref or journal-betId leg need the sweep's evidence.
        found = "312345678999"
        self.assertEqual(guard.evaluate_cancel(support.MARKET_ID, [found], state=killed, now_ns=NOW)["rule"],
                         "validation:V2")
        targets = [{"bet_id": found, "market_id": support.MARKET_ID, "leg": "strategy_ref"},
                   {"bet_id": "312345678998", "market_id": support.MARKET_ID, "leg": "bet_id"}]
        swept = guard.evaluate_cancel(support.MARKET_ID, [found, "312345678998"], state=killed, now_ns=NOW,
                                      kill_targets=targets)
        self.assertTrue(swept["approved"], swept["detail"]["failures"])
        self.assertTrue(swept["detail"]["kill_sweep"])
        # A sourceIdKey leg is no evidence any more (amendment A3, D4): such a bet is never a kill target.
        keyed = [{"bet_id": found, "market_id": support.MARKET_ID, "leg": "source_id_key"}]
        self.assertFalse(guard.evaluate_cancel(support.MARKET_ID, [found], state=killed, now_ns=NOW,
                                               kill_targets=keyed)["approved"])
        # Evidence for another market, a malformed target, or a journal that is not halted proves nothing.
        elsewhere = [dict(targets[0], market_id="1.2")]
        self.assertFalse(guard.evaluate_cancel(support.MARKET_ID, [found], state=killed, now_ns=NOW,
                                               kill_targets=elsewhere)["approved"])
        for bad in ([dict(targets[0], leg="guess")], [{"bet_id": found}], "312345678999"):
            with self.subTest(bad=bad):
                self.assertFalse(guard.evaluate_cancel(support.MARKET_ID, [found], state=killed, now_ns=NOW,
                                                       kill_targets=bad)["approved"])
        not_halted = guard.evaluate_cancel(support.MARKET_ID, [found], state=open_state, now_ns=NOW,
                                           kill_targets=targets)
        self.assertIn("kill_targets_need_journal_halt", [f["reason"] for f in not_halted["detail"]["failures"]])
        # Still one market and at most 60 per request; a partial reduction needs a known (EXECUTABLE) remainder.
        many = [str(10**11 + i) for i in range(61)]
        self.assertFalse(guard.evaluate_cancel(support.MARKET_ID, many, state=killed, now_ns=NOW, kill_targets=[
            {"bet_id": b, "market_id": support.MARKET_ID, "leg": "strategy_ref"} for b in many])["approved"])
        partial = guard.evaluate_cancel(support.MARKET_ID, [{"bet_id": "312345678901", "size_reduction_p": 50}],
                                        state=killed, now_ns=NOW)
        self.assertIn("size_reduction_needs_executable_order", [f["reason"] for f in partial["detail"]["failures"]])

    def test_place_refused_kill_or_halt(self):
        self.assertEqual(self.paper(kill_engaged=True)["rule"], "halted")
        unknown_kill = self.paper(kill_engaged=None)
        self.assertEqual(unknown_kill["rule"], "halted")
        self.assertIn("kill_state_unknown", [f["reason"] for f in unknown_kill["detail"]["failures"]])
        self.assertEqual(self.paper(state=orders.state_with([], halted=True))["rule"], "halted")
        self.assertEqual(self.paper(state=orders.state_with([], clock_regression=True))["rule"], "halted")
        paths = support.temp_state_paths(self)
        guard = risk.RiskGuard(self.config, mode="paper", paths=paths)
        kwargs = dict(book=orders.snapshot(), state=orders.empty_state(), arming=orders.ARMED, now_ns=NOW)
        self.assertTrue(guard.evaluate_place(support.MARKET_ID, [orders.instruction()], **kwargs)["approved"])
        before = guard.evaluate_place(support.MARKET_ID, [orders.instruction()], **kwargs)
        paths.kill_file.write_text("{}")
        after = guard.evaluate_place(support.MARKET_ID, [orders.instruction()], **kwargs)
        self.assertEqual(after["rule"], "halted")
        # The kill state read from disk is part of the decision's identity.
        self.assertNotEqual(before["decision_id"], after["decision_id"])
        # Regression: a caller's stale kill_engaged=False never hides an engaged kill file.
        stale = guard.evaluate_place(support.MARKET_ID, [orders.instruction()], kill_engaged=False, **kwargs)
        self.assertEqual(stale["rule"], "halted")
        self.assertIn("kill_engaged", [f["reason"] for f in stale["detail"]["failures"]])
        paths.kill_file.unlink()
        self.assertTrue(guard.evaluate_place(support.MARKET_ID, [orders.instruction()], kill_engaged=False,
                                             **kwargs)["approved"])
        self.assertEqual(guard.evaluate_place(support.MARKET_ID, [orders.instruction()], kill_engaged=True,
                                              **kwargs)["rule"], "halted")
        self.assertEqual(self.paper(kill_engaged="no")["rule"], "halted")          # not a bool: fail closed

    def test_unresolved_blocks_market_and_global_in_live(self):
        unknown = record(1, state="UNKNOWN", market_id="1.2", bet_id=None)
        paper_state = orders.state_with([unknown])
        self.assertTrue(self.paper(state=paper_state)["approved"])
        self.assertEqual(self.paper(state=paper_state, market_id="1.2",
                                    book=orders.snapshot(market_id="1.2"))["rule"], "unresolved_orders")
        live_state = orders.state_with([unknown], mode="live", now_ns=self.clock.time_ns())
        decision = self.live(state=live_state)
        self.assertEqual(decision["rule"], "unresolved_orders")
        self.assertIn("unresolved_orders_anywhere_live", [f["reason"] for f in decision["detail"]["failures"]])
        halted_market = orders.state_with([], unresolved_markets=["1.1"])
        self.assertEqual(self.paper(state=halted_market)["rule"], "unresolved_orders")

    def test_not_armed_in_live_only(self):
        self.assertEqual(self.live(arming={"armed": False, "failures": ["not_armed"]})["rule"], "not_armed")
        self.assertEqual(self.live(arming={"armed": True, "failures": ["confirm_token_mismatch"]})["rule"], "not_armed")
        self.assertEqual(self.live(arming=None)["rule"], "not_armed")
        guard = risk.RiskGuard(self.config, mode="paper")
        decision = guard.evaluate_place(support.MARKET_ID, [orders.instruction()], book=orders.snapshot(),
                                        state=orders.empty_state(), arming={"armed": False, "failures": ["x"]},
                                        now_ns=NOW, kill_engaged=False)
        self.assertTrue(decision["approved"])

    def test_replace_runs_r1_r4_on_new_price(self):
        parent = record(1, side="LAY", price_h=300, size_p=200, bet_id="312345678901")
        state = orders.state_with([parent])
        book = orders.snapshot(runners={"12345": orders.runner(back=((2.98, 50.0),), lay=((3.0, 50.0),), ltp=3.0)})
        guard = risk.RiskGuard(self.config, mode="paper")

        def replace(new_price, **kwargs):
            kwargs.setdefault("kill_engaged", False)
            return guard.evaluate_replace(support.MARKET_ID, [{"bet_id": "312345678901", "new_price_h": new_price}],
                                          book=book, state=kwargs.pop("state", state), arming=orders.ARMED,
                                          now_ns=NOW, **kwargs)

        self.assertTrue(replace(305)["approved"], replace(305)["detail"]["failures"])
        self.assertEqual(replace(305, kill_engaged=True)["rule"], "halted")
        self.assertEqual(replace(306)["rule"], "validation:V4")
        self.assertEqual(replace(400)["rule"], "validation:V13")
        self.assertEqual(replace(305, state=orders.state_with([parent], halted=True))["rule"], "halted")
        unresolved = orders.state_with([parent, record(2, state="UNKNOWN", bet_id=None)])
        self.assertEqual(replace(305, state=unresolved)["rule"], "unresolved_orders")
        decision = replace(305)
        self.assertEqual(decision["instructions"], [{"bet_id": "312345678901", "new_price_h": 305}])
        self.assertEqual(decision["action"], "replace")

    def test_replace_max_of_both_outcomes(self):
        parent = record(1, side="LAY", price_h=300, size_p=200, bet_id="312345678901")      # liability 400
        state = orders.state_with([parent])
        book = orders.snapshot(runners={"12345": orders.runner(back=((2.98, 50.0),), lay=((3.0, 50.0),), ltp=3.0)})
        tight = orders.order_config({"orders": {"caps": {"max_market_exposure": 4.2,
                                                         "max_price_deviation_ticks": 20}}})
        guard = risk.RiskGuard(tight, mode="paper")
        kwargs = dict(book=book, state=state, arming=orders.ARMED, now_ns=NOW, kill_engaged=False)
        up = guard.evaluate_replace(support.MARKET_ID, [{"bet_id": "312345678901", "new_price_h": 320}], **kwargs)
        self.assertEqual(up["worst_case"]["market_pence"], 440)                  # outcome (b): 2.00 x 2.20
        self.assertEqual(up["rule"], "market_exposure")
        down = guard.evaluate_replace(support.MARKET_ID, [{"bet_id": "312345678901", "new_price_h": 290}], **kwargs)
        self.assertEqual(down["worst_case"]["market_pence"], 400)                # outcome (a): the original stays
        self.assertTrue(down["approved"], down["detail"]["failures"])
        self.assertEqual(guard.evaluate_replace(support.MARKET_ID, [{"bet_id": "312345678901", "new_price_h": 320}],
                                                **dict(kwargs, state=orders.state_with([parent]))), up)

    def test_replace_mixed_outcomes_counted(self):
        """Regression: each replace instruction succeeds or fails on its own, so a two-instruction
        replace is judged on the post-submit pairs (worse of parent and child per pair per outcome),
        not just on all-fail or all-succeed."""
        config = orders.order_config({"orders": {"caps": {"max_market_exposure": 11.0,
                                                          "max_instructions_per_request": 2,
                                                          "max_total_exposure": 50.0, "max_daily_loss": 20.0}}})
        guard = risk.RiskGuard(config, mode="paper")
        first = record(1, side="LAY", price_h=330, size_p=200, bet_id="100000000001")
        second = record(2, side="LAY", price_h=400, size_p=200, bet_id="100000000002")
        replaces = [{"bet_id": "100000000001", "new_price_h": 380}, {"bet_id": "100000000002", "new_price_h": 350}]
        decision = guard.evaluate_replace(support.MARKET_ID, replaces, book=orders.snapshot(),
                                          state=orders.state_with([first, second]), arming=orders.ARMED, now_ns=NOW,
                                          kill_engaged=False)
        # All fail: 460 + 600 = 1060; all succeed: 560 + 500 = 1060; first succeeds, second fails: 560 + 600.
        self.assertEqual(decision["worst_case"]["market_pence"], 1160)
        self.assertEqual(decision["rule"], "market_exposure")
        # The post-submit state the decision describes: both parents REPLACE_PENDING with provisional children.
        parents = [dict(first, state="REPLACE_PENDING", exposure_basis="adverse_unknown"),
                   dict(second, state="REPLACE_PENDING", exposure_basis="adverse_unknown")]
        children = [record(3, side="LAY", price_h=380, size_p=200, state="SUBMITTING", bet_id=None,
                           parent_ref=first["customer_order_ref"], provisional_child=True),
                    record(4, side="LAY", price_h=350, size_p=200, state="SUBMITTING", bet_id=None,
                           parent_ref=second["customer_order_ref"], provisional_child=True)]
        self.assertEqual(risk.worst_case_market_loss(parents + children, [SEL]), 1160)
        # An unhashable bet id is a validation refusal, not an exception.
        odd = guard.evaluate_replace(support.MARKET_ID, [{"bet_id": ["x"], "new_price_h": 345}],
                                     book=orders.snapshot(), state=orders.empty_state(), arming=orders.ARMED,
                                     now_ns=NOW, kill_engaged=False)
        self.assertEqual(odd["rule"], "validation:V2")

    def test_funds_policy(self):
        self.assertTrue(self.live(funds=self.funds(available=2.0))["approved"])
        self.assertEqual(self.live(funds=self.funds(available=1.99))["rule"], "funds")
        reserve = orders.order_config({"orders": {"caps": {"funds_reserve": 0.5}}})
        self.assertEqual(self.live(guard=self.live_guard(reserve), funds=self.funds(available=2.49))["rule"], "funds")
        lay = [orders.instruction(side="LAY", price_h=355, size_p=200)]          # needs 2.00 x 2.55 = 5.10
        self.assertEqual(self.live(lay, funds=self.funds(available=5.09))["rule"], "funds")
        self.assertTrue(self.live(lay, funds=self.funds(available=5.10))["approved"])
        state = orders.empty_state("live", self.clock.time_ns())
        state.update(funds=self.funds(), funds_checked_at=utc_from_ns(self.clock.time_ns() - 60 * 10**9))
        self.assertTrue(self.live(state=state, funds=None)["approved"])
        state["funds_checked_at"] = utc_from_ns(self.clock.time_ns() - 61 * 10**9)
        stale = self.live(state=state, funds=None)
        self.assertIn("funds_stale_refresh_required", [f["reason"] for f in stale["detail"]["failures"]])
        missing = self.live(funds=None)
        self.assertIn("funds_unknown_refresh_required", [f["reason"] for f in missing["detail"]["failures"]])
        # Regression: funds handed in are never assumed fresh. Without a fetch time they are refused,
        # including the fold's own stored funds passed back in without funds_checked_at.
        undated = self.live(funds=self.funds(), funds_checked_at=None)
        self.assertEqual(undated["rule"], "funds")
        self.assertIn("funds_age_unknown_refresh_required", [f["reason"] for f in undated["detail"]["failures"]])
        stored = orders.empty_state("live", self.clock.time_ns())
        stored.update(funds=self.funds(), funds_checked_at=utc_from_ns(self.clock.time_ns() - 3600 * 10**9))
        self.assertEqual(self.live(state=stored, funds=stored["funds"], funds_checked_at=None)["rule"], "funds")
        old = utc_from_ns(self.clock.time_ns() - 61 * 10**9)
        self.assertIn("funds_stale_refresh_required", [f["reason"] for f in self.live(
            funds=self.funds(), funds_checked_at=old)["detail"]["failures"]])
        self.assertTrue(self.live(funds=self.funds(),
                                  funds_checked_at=utc_from_ns(self.clock.time_ns() - 59 * 10**9))["approved"])
        self.assertEqual(self.live(funds=self.funds(), funds_checked_at="yesterday")["rule"], "funds")
        # A "checked_at" key on the document is accepted as its fetch time.
        dated = dict(self.funds(), checked_at=old)
        self.assertIn("funds_stale_refresh_required",
                      [f["reason"] for f in self.live(funds=dated, funds_checked_at=None)["detail"]["failures"]])
        dated["checked_at"] = utc_from_ns(self.clock.time_ns() - 59 * 10**9)
        self.assertTrue(self.live(funds=dated, funds_checked_at=None)["approved"])
        dated["checked_at"] = "yesterday"
        self.assertEqual(self.live(funds=dated, funds_checked_at=None)["rule"], "funds")
        for bad in (float("nan"), None, "100", True):
            with self.subTest(bad=bad):
                self.assertEqual(self.live(funds=self.funds(available=bad))["rule"], "funds")

    def test_unexplained_exposure_halts_above_tolerance(self):
        mine = record(1, side="BACK", price_h=350, size_p=200, mode="live", market_id="1.40")   # 200p
        state = orders.state_with([mine], mode="live", now_ns=self.clock.time_ns())
        ok = self.live(state=state, funds=self.funds(exposure=-2.0))
        self.assertTrue(ok["approved"], ok["detail"]["failures"])
        self.assertEqual(ok["detail"]["external_exposure_pence"], 0)
        over = self.live(state=state, funds=self.funds(exposure=-2.01))
        self.assertEqual(over["rule"], "unexplained_account_exposure")
        self.assertTrue(over["detail"]["halt"])
        self.assertEqual(over["detail"]["halt_reason"], "unexplained_account_exposure")
        self.assertEqual(over["detail"]["external_exposure_pence"], 1)
        positive = self.live(state=state, funds=self.funds(exposure=2.01))          # sign unverified: abs()
        self.assertEqual(positive["rule"], "unexplained_account_exposure")
        tolerant = self.live_guard(orders.order_config({"orders": {"rules": {"external_exposure_tolerance": 1.0}}}))
        self.assertTrue(self.live(guard=tolerant, state=state, funds=self.funds(exposure=-3.0))["approved"])
        self.assertEqual(self.live(state=state, funds=self.funds(exposure=None))["rule"], "funds")

    def matched(self, n, side, price_h, *, average_price_matched_h=None, mode="paper"):
        """A fully matched 2.00 order record."""
        return record(n, side=side, price_h=price_h, size_p=200, state="EXECUTION_COMPLETE", size_matched_p=200,
                      average_price_matched_h=average_price_matched_h or price_h, size_remaining_p=0, mode=mode)

    def test_live_guard_gives_no_hedge_credit(self):
        """Astra C1 (amendment A3): with market and total caps of 2.00, a matched BACK 2.00 @ 3.50 cannot pay for a
        LAY 2.00 @ 3.50 in live. Without credit the market loses 5.00 if the selection wins (the LAY) and 2.00
        otherwise (the BACK), so R8 refuses at 500p; the figure is the same whatever average matched price the
        journal holds (3.50 as reported, or the 2.00 Betfair moved it to after a non-runner)."""
        caps = orders.order_config({"orders": {"caps": {"max_market_exposure": 2.0, "max_total_exposure": 2.0}}})
        guard = self.live_guard(caps)
        self.assertIs(guard.hedge_credit, False)
        for average in (350, 200):
            with self.subTest(average_price_matched_h=average):
                state = orders.state_with([self.matched(1, "BACK", 350, average_price_matched_h=average, mode="live")],
                                          mode="live", now_ns=self.clock.time_ns())
                refused = self.live([orders.instruction(side="LAY")], guard=guard, state=state,
                                    funds=self.funds(exposure=-2.0))
                self.assertEqual(refused["rule"], "market_exposure")
                self.assertIn("total_exposure", rules(refused))
                self.assertEqual((refused["worst_case"]["market_pence"], refused["worst_case"]["total_pence"]),
                                 (500, 500))
        # A cancel's informational worst case is counted the same way: a matched BACK 2.00 @ 3.50 and LAY 2.00 @ 3.10
        # lose 4.20 if the selection wins (0 with credit, as paper counts it below).
        legs = [self.matched(1, "BACK", 350, mode="live"), self.matched(2, "LAY", 310, mode="live")]
        hedged = orders.state_with(legs, mode="live", now_ns=self.clock.time_ns())
        cancel = guard.evaluate_cancel(support.MARKET_ID, [], state=hedged, now_ns=self.clock.time_ns())
        self.assertEqual(cancel["worst_case"]["market_pence"], 420)
        # A single order is unaffected, and a hedge whose two losses fit the caps is still accepted.
        self.assertTrue(self.live([orders.instruction(side="LAY")], guard=self.live_guard())["approved"])
        state = orders.state_with([self.matched(1, "BACK", 350, mode="live")], mode="live", now_ns=self.clock.time_ns())
        hedge = self.live([orders.instruction(side="LAY")], state=state, funds=self.funds(exposure=-2.0))
        self.assertTrue(hedge["approved"], hedge["detail"]["failures"])
        self.assertEqual(hedge["worst_case"]["market_pence"], 500)

    def test_paper_keeps_hedge_credit(self):
        """Amendment A3, D5: paper keeps the credited model, so the same hedge fits the 2.00 caps there (the BACK's
        3.50 payoff offsets the LAY's loss: 2.00 is lost only if another runner wins)."""
        caps = orders.order_config({"orders": {"caps": {"max_market_exposure": 2.0, "max_total_exposure": 2.0}}})
        guard = risk.RiskGuard(caps, mode="paper")
        self.assertIs(guard.hedge_credit, True)
        state = orders.state_with([self.matched(1, "BACK", 350)])
        decision = self.paper([orders.instruction(side="LAY")], state=state, config=caps)
        self.assertTrue(decision["approved"], decision["detail"]["failures"])
        self.assertEqual(decision["worst_case"]["market_pence"], 200)
        hedged = orders.state_with([self.matched(1, "BACK", 350), self.matched(2, "LAY", 310)])
        cancel = guard.evaluate_cancel(support.MARKET_ID, [], state=hedged, now_ns=NOW)
        self.assertEqual(cancel["worst_case"]["market_pence"], 0)

    def test_r12_uses_the_credited_figure(self):
        """Amendment A3, A11: R12 compares Betfair's account exposure (which nets a hedge) with our explained
        exposure, so the credited (smaller) figure is the strict one. A matched BACK 2.00 @ 3.50 and a matched LAY
        2.00 @ 3.10 explain 0p with credit (420p without); an account exposure of 0.01 is then unexplained and
        halts. Had R12 used the no-credit 420p, 1p - 420p would have passed."""
        state = orders.state_with([self.matched(1, "BACK", 350, mode="live"), self.matched(2, "LAY", 310, mode="live")],
                                  mode="live", now_ns=self.clock.time_ns())
        self.assertEqual(risk.exposure_by_market(list(state["orders"].values())), {support.MARKET_ID: 420})
        self.assertEqual(risk.exposure_by_market(list(state["orders"].values()), hedge_credit=True),
                         {support.MARKET_ID: 0})
        ok = self.live([orders.instruction(size_p=100)], state=state, funds=self.funds(exposure=0.0))
        self.assertTrue(ok["approved"], ok["detail"]["failures"])
        self.assertEqual(ok["detail"]["external_exposure_pence"], 0)
        over = self.live([orders.instruction(size_p=100)], state=state, funds=self.funds(exposure=-0.01))
        self.assertEqual((over["rule"], over["detail"]["external_exposure_pence"], over["detail"]["halt"]),
                         ("unexplained_account_exposure", 1, True))
        # R8-R10 still use the no-credit figure: 420p if the selection wins (the new BACK only adds 100p to the
        # 200p lost when another runner wins).
        self.assertEqual(ok["worst_case"]["market_pence"], 420)

    def test_hedge_credit_defaults_to_none(self):
        """Amendment A3, C1: the module functions default to no credit (a call site that forgets the argument is
        conservative); RiskGuard._exposure defaults to None, meaning the guard's own setting (paper only)."""
        import inspect
        for function in (risk.worst_case_market_loss, risk.exposure_by_market):
            with self.subTest(function=function.__name__):
                self.assertIs(inspect.signature(function).parameters["hedge_credit"].default, False)
        self.assertIsNone(inspect.signature(risk.RiskGuard._exposure).parameters["hedge_credit"].default)
        state = orders.state_with([self.matched(1, "BACK", 350), self.matched(2, "LAY", 310)])
        live, paper = self.live_guard(), risk.RiskGuard(self.config, mode="paper")
        self.assertEqual(live._exposure(state, support.MARKET_ID, None)["market"], 420)
        self.assertEqual(paper._exposure(state, support.MARKET_ID, None)["market"], 0)
        self.assertEqual(live._exposure(state, support.MARKET_ID, None, hedge_credit=True)["market"], 0)
        self.assertEqual(paper._exposure(state, support.MARKET_ID, None, hedge_credit=False)["market"], 420)
        self.assertEqual(risk.STATUS_EXPOSURE_BASIS, {"paper": "worst_case_with_hedge_credit",
                                                      "live": "worst_case_no_hedge_credit"})

    def test_decision_pure_deterministic(self):
        state = orders.state_with([record(1, side="LAY", price_h=400, size_p=150, selection_id=11)])
        inputs = (copy.deepcopy(state), orders.snapshot())
        first = self.paper(state=state)
        second = self.paper(state=state)
        self.assertEqual(first, second)
        self.assertEqual((state, orders.snapshot()), inputs)        # nothing mutated
        rebuilt = risk.RiskGuard(copy.deepcopy(self.config), mode="paper")
        third = rebuilt.evaluate_place(support.MARKET_ID, [orders.instruction()], book=orders.snapshot(),
                                       state=copy.deepcopy(state), arming=orders.ARMED, now_ns=NOW,
                                       kill_engaged=False)
        self.assertEqual(first, third)
        self.assertNotEqual(first["decision_id"], self.paper(state=state, now_ns=NOW + 1)["decision_id"])
        live_a = self.live()
        live_b = self.live()
        self.assertEqual(live_a, live_b)

    def test_config_ceilings_rechecked(self):
        bad = orders.order_config({"orders": {"caps": {"max_stake_per_order": 50.01}}})
        with self.assertRaises(ConfigError):
            risk.RiskGuard(bad, mode="paper")
        with self.assertRaises(ConfigError):
            risk.RiskGuard(self.config, mode="sandbox")

    def test_issue_refuses_unapproved_and_paper(self):
        paper_guard = risk.RiskGuard(self.config, mode="paper")
        paper_decision = self.paper()
        with self.assertRaisesRegex(OrderRefused, "capability_mode"):
            paper_guard.issue(paper_decision, prepared(orders.PLACE_METHOD, place_params(paper_decision)),
                              now_monotonic_ns=1)
        guard = self.live_guard()
        refused = self.live([orders.instruction(size_p=500)], guard=guard)
        self.assertFalse(refused["approved"])
        with self.assertRaisesRegex(OrderRefused, "capability_unapproved"):
            guard.issue(refused, prepared(orders.PLACE_METHOD, {}), now_monotonic_ns=1)
        forged = dict(refused, approved=True, rule=None)
        with self.assertRaisesRegex(OrderRefused, "capability_decision_unknown"):
            guard.issue(forged, prepared(orders.PLACE_METHOD, place_params(forged)), now_monotonic_ns=1)
        decision = self.live(guard=guard, customer_ref=orders.CREF, customer_order_refs=[orders.REF])
        self.assertTrue(decision["approved"], decision["detail"]["failures"])
        widened = copy.deepcopy(decision)
        widened["instructions"][0]["size_p"] = 5000
        with self.assertRaisesRegex(OrderRefused, "capability_decision_unknown"):
            guard.issue(widened, prepared(orders.PLACE_METHOD, place_params(widened)), now_monotonic_ns=1)
        good = prepared(orders.PLACE_METHOD, place_params(decision))
        cap = guard.issue(decision, good, now_monotonic_ns=self.clock.mono_ns)
        self.assertIsInstance(cap, OrderCapability)
        self.assertEqual((cap.decision_id, cap.method, cap.body_sha256), (decision["decision_id"],
                                                                           orders.PLACE_METHOD, good["body_sha256"]))
        self.assertEqual(cap.consume(method=orders.PLACE_METHOD, body_sha256=good["body_sha256"],
                                     now_monotonic_ns=self.clock.mono_ns), good["body_sha256"])
        with self.assertRaisesRegex(OrderRefused, "capability_reused"):
            cap.consume(method=orders.PLACE_METHOD, body_sha256=good["body_sha256"],
                        now_monotonic_ns=self.clock.mono_ns)
        with self.assertRaisesRegex(OrderRefused, "capability_decision_reused"):
            guard.issue(decision, good, now_monotonic_ns=self.clock.mono_ns)
        # A decision older than max_book_age_s (2 s) on the monotonic clock is never minted.
        self.clock.advance(0.5)
        late = self.live(guard=guard, customer_ref=orders.CREF, customer_order_refs=[orders.REF])
        self.clock.advance(2.001)
        with self.assertRaisesRegex(OrderRefused, "capability_decision_stale"):
            guard.issue(late, prepared(orders.PLACE_METHOD, place_params(late)), now_monotonic_ns=self.clock.mono_ns)
        early = self.live(guard=guard, customer_ref=orders.CREF, customer_order_refs=[orders.REF])
        with self.assertRaisesRegex(OrderRefused, "capability_decision_stale"):
            guard.issue(early, prepared(orders.PLACE_METHOD, place_params(early)),
                        now_monotonic_ns=self.clock.mono_ns - 1)

    def test_live_decisions_must_bind_refs(self):
        """Regression: in live mode a decision that does not bind customerRef (and, for a place,
        customerOrderRefs) is refused at evaluation (V11) and can never be minted."""
        guard = self.live_guard()
        for missing in ({"customer_ref": None}, {"customer_order_refs": None}):
            with self.subTest(missing=missing):
                decision = self.live(guard=guard, **missing)
                self.assertEqual(decision["rule"], "validation:V11")
        paper_guard = risk.RiskGuard(self.config, mode="paper")
        self.assertTrue(paper_guard.evaluate_place(support.MARKET_ID, [orders.instruction()], book=orders.snapshot(),
                                                   state=orders.empty_state(), arming=orders.ARMED, now_ns=NOW,
                                                   kill_engaged=False)["approved"])
        parent = record(1, side="LAY", price_h=300, size_p=200, bet_id="312345678901", mode="live")
        book = orders.live_book(self.clock, runners={"12345": orders.runner(back=((2.98, 50.0),), lay=((3.0, 50.0),),
                                                                          ltp=3.0)})
        # Live replace was removed in v1 (amendment A3, D2): a live guard refuses to evaluate one at all.
        with self.assertRaisesRegex(OrderRefused, "live_replace_removed_v1"):
            guard.evaluate_replace(support.MARKET_ID, [{"bet_id": "312345678901", "new_price_h": 305}],
                                   book=book, state=orders.state_with([parent], mode="live"),
                                   arming=orders.ARMED, funds=self.funds(), funds_checked_at=self.clock.utc_now(),
                                   now_ns=self.clock.time_ns(), now_monotonic_ns=self.clock.mono_ns,
                                   kill_engaged=False, customer_ref=orders.CREF)
        # A cancel decision evaluated without customer_ref is approved but cannot be minted.
        cancel = guard.evaluate_cancel(support.MARKET_ID, ["312345678901"],
                                       state=orders.state_with([parent], mode="live"), now_ns=NOW)
        self.assertTrue(cancel["approved"])
        body = {"marketId": support.MARKET_ID, "customerRef": orders.CREF,
                "instructions": [{"betId": "312345678901", "sizeReduction": None}]}
        with self.assertRaisesRegex(OrderRefused, "capability_refs_unbound"):
            guard.issue(cancel, prepared(orders.CANCEL_METHOD, body), now_monotonic_ns=1)
        bound = guard.evaluate_cancel(support.MARKET_ID, ["312345678901"],
                                      state=orders.state_with([parent], mode="live"), now_ns=NOW + 1,
                                      customer_ref=orders.CREF)
        self.assertIsInstance(guard.issue(bound, prepared(orders.CANCEL_METHOD, body), now_monotonic_ns=1),
                              OrderCapability)

    def test_no_capability_for_a_replace_body(self):
        """Amendment A3 (D2): replace is not in ACTION_VERBS, so even an approved decision whose action is
        replace (none can be evaluated live any more) can never be minted for a replaceOrders body."""
        self.assertNotIn("replace", risk.ACTION_VERBS)
        self.assertFalse(hasattr(risk, "PROBE_REPLACE_PRICE_H"))
        guard = self.live_guard()
        decision = guard._decide("replace", support.MARKET_ID, [], inputs={"now_monotonic_ns": self.clock.mono_ns},
                                 worst_case={}, now_ns=NOW, detail={},
                                 instructions=[{"bet_id": "312345678901", "new_price_h": 305}],
                                 market_version=1, customer_ref=orders.CREF)
        self.assertTrue(decision["approved"])
        body = {"marketId": support.MARKET_ID, "customerRef": orders.CREF, "marketVersion": {"version": 1},
                "instructions": [{"betId": "312345678901", "newPrice": 3.05}]}
        with self.assertRaisesRegex(OrderRefused, "capability_body_mismatch"):
            guard.issue(decision, prepared(orders.REPLACE_METHOD, body), now_monotonic_ns=self.clock.mono_ns)

    def test_issue_binds_exact_body(self):
        guard = self.live_guard()
        base = None

        def fresh():
            self.clock.advance(0.001)
            return self.live(guard=guard, customer_ref=orders.CREF, customer_order_refs=[orders.REF])

        base = fresh()
        ins = base["instructions"][0]
        tampered = {
            "size": place_params(base, instructions=[dict(place_params(base)["instructions"][0],
                                                          limitOrder={"size": 2.01, "price": 3.5,
                                                                      "persistenceType": "LAPSE"})]),
            "price": place_params(base, instructions=[dict(place_params(base)["instructions"][0],
                                                           limitOrder={"size": 2.0, "price": 3.55,
                                                                       "persistenceType": "LAPSE"})]),
            "persist": place_params(base, instructions=[dict(place_params(base)["instructions"][0],
                                                             limitOrder={"size": 2.0, "price": 3.5,
                                                                         "persistenceType": "PERSIST"})]),
            "extra_async": dict(place_params(base), **{"async": True}),
            "time_in_force": place_params(base, instructions=[dict(place_params(base)["instructions"][0],
                                                                   timeInForce="FILL_OR_KILL")]),
            "version": place_params(base, marketVersion={"version": 1}),
            "market": place_params(base, marketId="1.2"),
            "strategy": place_params(base, customerStrategyRef="other"),
            "order_ref": place_params(base, ref="bro200101120000-ffffffffffffffff"),
            "customer_ref": place_params(base, cref="brq200101120000-ffffffffffffffff"),
            "selection": place_params(base, instructions=[dict(place_params(base)["instructions"][0],
                                                               selectionId=float(ins["selection_id"]))]),
            "two_instructions": place_params(base, instructions=place_params(base)["instructions"] * 2),
        }
        for name, params in tampered.items():
            with self.subTest(name=name):
                decision = fresh()
                with self.assertRaisesRegex(OrderRefused, "capability_body_mismatch"):
                    guard.issue(decision, prepared(orders.PLACE_METHOD, params), now_monotonic_ns=self.clock.mono_ns)
                # The attempt burned the decision: even the correct body is now refused.
                with self.assertRaisesRegex(OrderRefused, "capability_decision_reused"):
                    guard.issue(decision, prepared(orders.PLACE_METHOD, place_params(decision)),
                                now_monotonic_ns=self.clock.mono_ns)
        decision = fresh()
        body = prepared(orders.PLACE_METHOD, place_params(decision))
        with self.assertRaisesRegex(OrderRefused, "capability_body_mismatch"):
            guard.issue(decision, dict(body, body_sha256="0" * 64), now_monotonic_ns=self.clock.mono_ns)
        decision = fresh()
        with self.assertRaisesRegex(OrderRefused, "capability_body_mismatch"):
            guard.issue(decision, prepared(orders.CANCEL_METHOD, place_params(decision)),
                        now_monotonic_ns=self.clock.mono_ns)
        for method in ("SportsAPING/v1.0/listMarketBook", orders.REPLACE_METHOD, "SportsAPING/v1.0/Orders",
                       "AccountAPING/v1.0/placeOrders"):
            with self.subTest(method=method):
                decision = fresh()
                with self.assertRaisesRegex(OrderRefused, "capability_body_mismatch"):
                    guard.issue(decision, prepared(method, place_params(decision)), now_monotonic_ns=self.clock.mono_ns)
        # Cancels bind bet ids and reductions the same way.
        bet = record(1, bet_id="312345678901", mode="live")
        state = orders.state_with([bet], mode="live")
        cancel = guard.evaluate_cancel(support.MARKET_ID, [{"bet_id": "312345678901", "size_reduction_p": 50}],
                                       state=state, now_ns=NOW, customer_ref=orders.CREF)
        wrong = {"marketId": support.MARKET_ID, "customerRef": orders.CREF,
                 "instructions": [{"betId": "312345678901", "sizeReduction": None}]}
        with self.assertRaisesRegex(OrderRefused, "capability_body_mismatch"):
            guard.issue(cancel, prepared(orders.CANCEL_METHOD, wrong), now_monotonic_ns=1)
        cancel = guard.evaluate_cancel(support.MARKET_ID, [{"bet_id": "312345678901", "size_reduction_p": 50}],
                                       state=state, now_ns=NOW + 1, customer_ref=orders.CREF)
        right = dict(wrong, instructions=[{"betId": "312345678901", "sizeReduction": 0.5}])
        self.assertIsInstance(guard.issue(cancel, prepared(orders.CANCEL_METHOD, right), now_monotonic_ns=1),
                              OrderCapability)


if __name__ == "__main__":
    unittest.main()
