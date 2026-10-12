"""Pre-trade validation (V0-V14, V9b) on synthetic books and orders; no order is ever sent."""

import unittest
from unittest.mock import patch

from betangel_research.betfair import validate
from betangel_research.betfair.constants import CUSTOMER_ORDER_REF_PATTERN, CUSTOMER_REF_PATTERN
from betangel_research.betfair.receipts import utc_from_ns, utc_ms_from_ns

import test_betfair_support as support
import test_betfair_support_orders as orders

NOW = orders.NOW_NS
MINUTE = 60 * 10**9


def rules(failures):
    return {f["rule"] for f in failures}


def reasons(failures):
    return {f["reason"] for f in failures}


class ValidatePlaceTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.config = orders.order_config()
        self.clock = support.FakeClock()

    def place(self, instructions=None, *, book=None, open_orders=(), config=None, mode="paper", now_ns=NOW,
              **kwargs):
        instructions = [orders.instruction()] if instructions is None else instructions
        return validate.validate_place(support.MARKET_ID, instructions,
                                       book=orders.snapshot() if book is None else book, open_orders=list(open_orders),
                                       config=config or self.config, mode=mode, now_ns=now_ns, **kwargs)

    def live(self, instructions=None, *, book=None, **kwargs):
        book = orders.live_book(self.clock) if book is None else book
        kwargs.setdefault("now_monotonic_ns", self.clock.mono_ns)
        return self.place(instructions, book=book, mode="live", now_ns=self.clock.time_ns(), **kwargs)

    def test_valid_paper_and_live_pass(self):
        self.assertEqual(self.place(), [])
        self.assertEqual(self.live(), [])

    def test_min_stake(self):
        self.assertIn("size_below_min_stake", reasons(self.place([orders.instruction(size_p=99)])))
        self.assertEqual(self.place([orders.instruction(size_p=100)]), [])
        low = orders.order_config({"orders": {"rules": {"min_stake": 0.5}}})
        self.assertIn("min_stake_below_1_00_unsupported", reasons(self.place(config=low)))
        high = orders.order_config({"orders": {"rules": {"min_stake": 2.0}}})
        self.assertIn("size_below_min_stake", reasons(self.place([orders.instruction(size_p=150)], config=high)))

    def test_two_dp_size(self):
        for bad in (200.0, 199.5, "200", True, 0, -100):
            with self.subTest(bad=bad):
                self.assertEqual(rules(self.place([orders.instruction(size_p=bad)])) & {"V5"}, {"V5"})

    def test_off_ladder_refused(self):
        for bad in (351, 201, 100, 100001, 3.5, "350", None):
            with self.subTest(bad=bad):
                self.assertIn("V4", rules(self.place([orders.instruction(price_h=bad)])))

    def test_self_match_request_open_and_unresolved(self):
        two = orders.order_config({"orders": {"caps": {"max_instructions_per_request": 2}}})
        back = orders.instruction(side="BACK", price_h=350)
        crossing_lay = orders.instruction(side="LAY", price_h=350)
        safe_lay = orders.instruction(side="LAY", price_h=345)
        self.assertIn("V8", rules(self.place([back, crossing_lay], config=two)))
        self.assertNotIn("V8", rules(self.place([back, safe_lay], config=two)))
        open_back = orders.order_record(side="BACK", price_h=350)
        self.assertIn("V8", rules(self.place([orders.instruction(side="LAY", price_h=355)], open_orders=[open_back])))
        self.assertNotIn("V8", rules(self.place([orders.instruction(side="LAY", price_h=345)],
                                                open_orders=[open_back])))
        unknown_lay = orders.order_record(side="LAY", price_h=340, state="UNKNOWN", bet_id=None,
                                          customer_order_ref="bro200101120000-00000000000000aa")
        self.assertIn("V8", rules(self.place([orders.instruction(side="BACK", price_h=340)],
                                             open_orders=[unknown_lay])))
        # Terminal orders, other selections and other markets cannot self-match.
        ignored = [orders.order_record(side="LAY", price_h=360, state="LAPSED", size_remaining_p=0, size_lapsed_p=200),
                   orders.order_record(side="LAY", price_h=360, selection_id=999,
                                       customer_order_ref="bro200101120000-00000000000000bb"),
                   orders.order_record(side="LAY", price_h=360, market_id="1.2",
                                       customer_order_ref="bro200101120000-00000000000000cc")]
        self.assertNotIn("V8", rules(self.place([orders.instruction(side="BACK", price_h=350)], open_orders=ignored)))

    def test_instruction_count(self):
        self.assertIn("no_instructions", reasons(self.place([])))
        self.assertIn("V7", rules(self.place([orders.instruction(), orders.instruction(price_h=345)])))
        ten = orders.order_config({"orders": {"caps": {"max_instructions_per_request": 10}}})
        self.assertNotIn("V7", rules(self.place([orders.instruction(), orders.instruction(price_h=345)], config=ten)))
        self.assertIn("V7", rules(self.place([orders.instruction(price_h=p) for p in
                                              (350, 345, 340, 335, 330, 325, 320, 315, 310, 305, 300)], config=ten)))

    def test_ref_formats_lengths_charsets(self):
        ref = validate.make_customer_ref(NOW, rng=lambda n: "0123456789abcdef")
        order_ref = validate.make_customer_order_ref(NOW, rng=lambda n: "0123456789abcdef")
        stamp = utc_from_ns(NOW)
        expected_stamp = stamp[2:4] + stamp[5:7] + stamp[8:10] + stamp[11:13] + stamp[14:16] + stamp[17:19]
        self.assertEqual(ref, "brq" + expected_stamp + "-0123456789abcdef")
        self.assertEqual(order_ref, "bro" + expected_stamp + "-0123456789abcdef")
        for value, pattern in ((ref, CUSTOMER_REF_PATTERN), (order_ref, CUSTOMER_ORDER_REF_PATTERN)):
            self.assertEqual(len(value), 32)
            self.assertRegex(value, pattern)
            self.assertRegex(value, r"^br[qo]\d{12}-[0-9a-f]{16}$")
        for bad in ("0123456789ABCDEF", "0123456789abcde", "0123456789abcdeg", "", 12, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate.make_customer_order_ref(NOW, rng=lambda n, bad=bad: bad)
        with self.assertRaises(ValueError):
            validate.make_customer_ref(-1)
        self.assertIn("customer_ref_invalid", reasons(self.place(customer_ref="x" * 33)))
        self.assertIn("customer_ref_invalid", reasons(self.place(customer_ref="has space")))
        self.assertIn("customer_order_ref_invalid", reasons(self.place(customer_order_refs=["bad_ref"])))
        self.assertIn("customer_order_refs_count_or_duplicate",
                      reasons(self.place(customer_order_refs=[order_ref, order_ref])))
        self.assertEqual(self.place(customer_ref=ref, customer_order_refs=[order_ref]), [])
        bad_strategy = orders.order_config({"orders": {"customer_strategy_ref": "far-too-long-strategy"}})
        self.assertIn("strategy_ref_invalid", reasons(self.place(config=bad_strategy)))

    def test_refs_default_rng_secrets(self):
        with patch.object(validate.secrets, "token_hex", return_value="fedcba9876543210") as token_hex:
            ref = validate.make_customer_ref(NOW)
            order_ref = validate.make_customer_order_ref(NOW)
        self.assertEqual(token_hex.call_count, 2)
        token_hex.assert_called_with(8)
        self.assertTrue(ref.endswith("-fedcba9876543210") and order_ref.endswith("-fedcba9876543210"))

    def test_refs_unique_across_10k_generations(self):
        refs = {validate.make_customer_order_ref(NOW) for _ in range(10_000)}
        refs |= {validate.make_customer_ref(NOW) for _ in range(10_000)}
        self.assertEqual(len(refs), 20_000)

    def test_profit_ratio_13p_at_1_06_and_149p_at_1_01(self):
        self.assertFalse(validate.profit_ratio_ok(13, 106))       # 0.78p "ought", 1p paid: +28%
        self.assertFalse(validate.profit_ratio_ok(149, 101))      # 1.49p "ought", 1p paid: -33%
        self.assertTrue(validate.profit_ratio_ok(100, 101))       # the live-probe LAY 1.00 @ 1.01
        self.assertTrue(validate.profit_ratio_ok(100, 102))
        self.assertTrue(validate.profit_ratio_ok(200, 106))
        self.assertTrue(validate.profit_ratio_ok(213, 106))
        self.assertTrue(validate.profit_ratio_ok(200, 350))
        for bad in ((0, 106), (13, 100), (13.0, 106), (13, 1.06)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate.profit_ratio_ok(*bad)
        failures = self.place([orders.instruction(size_p=149, price_h=101)])
        self.assertIn("V14", rules(failures))

    def test_market_gates_each(self):
        cases = {
            "market_not_open": {"status": "SUSPENDED"},
            "market_inplay": {"inplay": True},
            "market_version_missing": {"version": None},
            "betting_type_not_odds": {"betting_type": "ASIAN_HANDICAP_DOUBLE_LINE"},
            "number_of_winners_not_1": {"number_of_winners": 2},
            "market_type_not_allowed": {"market_type": "PLACE"},
            "event_type_not_allowed": {"event_type_id": "1"},
            "ladder_not_classic": {"price_ladder_type": "FINEST"},
            "book_invalid": {"invalid_reason": "non_finite_size"},
            "book_market_mismatch": {"market_id": "1.2"},
            "book_schema_invalid": {"runners": {"12345": {"status": "ACTIVE"}}},
            "runner_not_active": {"runners": {"12345": orders.runner(status="REMOVED")}},
            "selection_not_in_book": {"runners": {"999": orders.runner()}},
        }
        for reason, changes in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, reasons(self.place(book=orders.snapshot(**changes))))
        with self.assertRaises(ValueError):
            validate.validate_place("1.1", [orders.instruction()], book=orders.snapshot(), open_orders=[],
                                    config=self.config, mode="sandbox", now_ns=NOW)
        self.assertIn("market_id_invalid", reasons(validate.validate_place(
            "2.1", [orders.instruction()], book=orders.snapshot(), open_orders=[], config=self.config, mode="paper",
            now_ns=NOW)))
        gbp = orders.order_config({"account_currency": "EUR"})
        self.assertIn("V12", rules(self.place(config=gbp)))

    def test_catalogue_missing_refused(self):
        book = orders.snapshot(catalogue_present=False, invalid_reason="catalogue_missing", market_time=None,
                               betting_type=None, market_type=None, event_type_id=None, price_ladder_type=None)
        found = reasons(self.place(book=book))
        self.assertTrue({"catalogue_missing", "book_invalid", "betting_type_not_odds", "ladder_not_classic"} <= found)

    def test_v9b_minutes_before_off(self):
        now = self.clock.time_ns()
        at_limit = orders.live_book(self.clock, market_time=utc_ms_from_ns(now + 30 * MINUTE))
        self.assertEqual(self.live(book=at_limit), [])
        too_far = orders.live_book(self.clock, market_time=utc_ms_from_ns(now + 31 * MINUTE))
        self.assertIn("market_time_too_far", reasons(self.live(book=too_far)))
        passed = orders.live_book(self.clock, market_time=utc_ms_from_ns(now - 1_000_000))
        self.assertIn("market_time_passed", reasons(self.live(book=passed)))
        missing = orders.live_book(self.clock, market_time=None)
        self.assertIn("market_time_missing", reasons(self.live(book=missing)))
        # Paper does not apply V9b (a replayed market's off time is in the past of the wall clock).
        self.assertNotIn("V9b", rules(self.place(book=orders.snapshot(market_time="2019-01-01T00:00:00.000Z"))))

    def test_live_book_age_and_delayed_flag(self):
        cases = {
            "book_not_self_fetched": {"fetched_by": "read_command"},
            "book_not_live_key": {"app_key_slot": "delayed"},
            "market_data_delayed_or_unknown": {"is_market_data_delayed": True},
        }
        for reason, changes in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, reasons(self.live(book=orders.live_book(self.clock, **changes))))
        self.assertIn("market_data_delayed_or_unknown",
                      reasons(self.live(book=orders.live_book(self.clock, is_market_data_delayed=None))))
        book = orders.live_book(self.clock)
        self.clock.advance(2.0)
        self.assertEqual(self.live(book=book), [])
        self.clock.advance(0.001)
        self.assertIn("book_stale", reasons(self.live(book=book)))
        self.assertIn("book_age_unmeasurable", reasons(self.live(now_monotonic_ns=None)))
        self.assertIn("book_age_unmeasurable", reasons(self.live(book=orders.live_book(self.clock, monotonic_ns=None))))
        # Paper: age against simulation time with the paper max_snapshot_age_s (10 s).
        self.assertEqual(self.place(now_ns=NOW + 10 * 10**9), [])
        self.assertIn("book_stale", reasons(self.place(now_ns=NOW + 10 * 10**9 + 1000)))
        recording = orders.snapshot(source="recording", fetched_by="recording", pt=support.BASE_PT)
        self.assertIn("book_stale", reasons(self.place(book=recording, now_ns=support.BASE_PT * 10**6 + 11 * 10**9)))

    def test_negative_age_refused(self):
        self.assertIn("book_age_negative", reasons(self.place(now_ns=NOW - 1000)))
        future = orders.live_book(self.clock, monotonic_ns=self.clock.mono_ns + 1)
        self.assertIn("book_age_negative", reasons(self.live(book=future)))

    def test_price_deviation_and_probe_exempt(self):
        # Best back is 3.50: ten ticks away (4.00) passes, eleven (4.10) is refused.
        self.assertEqual(self.place([orders.instruction(price_h=400)]), [])
        far = [orders.instruction(price_h=410)]
        self.assertIn("price_deviation_too_large", reasons(self.place(far)))
        self.assertNotIn("V13", rules(self.place(far, probe=True)))
        empty = orders.snapshot(runners={"12345": orders.runner(back=(), lay=(), ltp=None)})
        self.assertIn("no_reference_price", reasons(self.place(book=empty)))
        self.assertNotIn("V13", rules(self.place(book=empty, probe=True)))
        lay = orders.instruction(side="LAY", price_h=355)
        self.assertEqual(self.place([lay]), [])
        ltp_only = orders.snapshot(runners={"12345": orders.runner(back=(), lay=(), ltp=5.0)})
        self.assertIn("price_deviation_too_large", reasons(self.place(book=ltp_only)))

    def test_lapse_only(self):
        for persistence in ("PERSIST", "MARKET_ON_CLOSE", None, "lapse"):
            with self.subTest(persistence=persistence):
                self.assertIn("persistence_not_lapse",
                              reasons(self.place([orders.instruction(persistence_type=persistence)])))

    def test_unsupported_order_features_refused(self):
        for extra in ({"time_in_force": "FILL_OR_KILL"}, {"min_fill_size": 100}, {"bet_target_type": "PAYOUT"},
                      {"order_type": "MARKET_ON_CLOSE"}, {"async": True}, {"customer_order_ref": "x"}):
            with self.subTest(extra=extra):
                self.assertIn("unsupported_order_feature_or_keys",
                              reasons(self.place([dict(orders.instruction(), **extra)])))
        missing = orders.instruction()
        del missing["persistence_type"]
        self.assertIn("V0", rules(self.place([missing])))
        self.assertIn("V0", rules(self.place(["not an instruction"])))
        self.assertIn("handicap_not_zero", reasons(self.place([orders.instruction(handicap=0.5)])))
        self.assertIn("V3", rules(self.place([orders.instruction(side="back")])))
        self.assertIn("market_inplay", reasons(self.place(book=orders.snapshot(inplay=True))))


class ValidateReplaceCancelTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.config = orders.order_config()
        self.parent = orders.order_record(side="BACK", price_h=350, size_remaining_p=200, bet_id="312345678901")

    def replace(self, replaces, open_orders=None, **kwargs):
        return validate.validate_replace(support.MARKET_ID, replaces, book=orders.snapshot(),
                                         open_orders=[self.parent] if open_orders is None else open_orders,
                                         config=self.config, mode="paper", now_ns=NOW, **kwargs)

    def test_replace_new_price_checked_as_new_order(self):
        self.assertEqual(self.replace([{"bet_id": "312345678901", "new_price_h": 345}]), [])
        self.assertIn("new_price_equals_current", reasons(self.replace([{"bet_id": "312345678901",
                                                                          "new_price_h": 350}])))
        self.assertIn("V4", rules(self.replace([{"bet_id": "312345678901", "new_price_h": 351}])))
        self.assertIn("V13", rules(self.replace([{"bet_id": "312345678901", "new_price_h": 410}])))
        self.assertIn("bet_not_ours_in_market", reasons(self.replace([{"bet_id": "999", "new_price_h": 345}])))
        self.assertIn("V0", rules(self.replace([{"bet_id": "312345678901", "new_price_h": 345, "x": 1}])))
        self.assertIn("no_instructions", reasons(self.replace([])))
        complete = dict(self.parent, state="EXECUTION_COMPLETE", size_remaining_p=0, size_matched_p=200,
                        average_price_matched_h=350)
        self.assertIn("bet_not_replaceable_state", reasons(self.replace(
            [{"bet_id": "312345678901", "new_price_h": 345}], open_orders=[complete])))
        # The parent itself is excluded from the self-match check; another of our orders is not.
        other_lay = orders.order_record(side="LAY", price_h=345, bet_id="312345678902",
                                        customer_order_ref="bro200101120000-00000000000000dd")
        self.assertIn("V8", rules(self.replace([{"bet_id": "312345678901", "new_price_h": 345}],
                                               open_orders=[self.parent, other_lay])))

    def test_cancel_rules(self):
        ok = validate.validate_cancel(support.MARKET_ID, ["312345678901"], open_orders=[self.parent])
        self.assertEqual(ok, [])
        self.assertEqual(validate.validate_cancel(support.MARKET_ID, [{"bet_id": "312345678901",
                                                                       "size_reduction_p": 50}],
                                                  open_orders=[self.parent]), [])
        cases = {
            "bet_not_ours_in_market": (support.MARKET_ID, ["999"]),
            "bet_id_invalid": (support.MARKET_ID, ["12a"]),
            "duplicate_bet_id": (support.MARKET_ID, ["312345678901", "312345678901"]),
            "too_many_instructions": (support.MARKET_ID, [str(10**11 + i) for i in range(61)]),
            "no_instructions": (support.MARKET_ID, []),
            "market_id_invalid": ("x", ["312345678901"]),
            "size_reduction_invalid": (support.MARKET_ID, [{"bet_id": "312345678901", "size_reduction_p": 200}]),
        }
        for reason, (market, cancels) in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, reasons(validate.validate_cancel(market, cancels, open_orders=[self.parent])))
        # Cancelling 1.87 of 2.00 @ 1.06 leaves 13p @ 1.06, which fails the F39 profit-ratio rule.
        short = dict(self.parent, price_h=106)
        self.assertIn("remainder_profit_ratio_out_of_bounds", reasons(validate.validate_cancel(
            support.MARKET_ID, [{"bet_id": "312345678901", "size_reduction_p": 187}], open_orders=[short])))
        # Another market's bet is not "ours in this market".
        elsewhere = dict(self.parent, market_id="1.2")
        self.assertIn("bet_not_ours_in_market", reasons(validate.validate_cancel(
            support.MARKET_ID, ["312345678901"], open_orders=[elsewhere])))
        # Regression: every own bet that may still be unmatched is cancellable, unresolved ones included
        # (the spec section 9.8 kill sweep must reach them); finished ones are not.
        for state in ("UNKNOWN", "ABSENT_UNCONFIRMED", "CANCEL_PENDING", "REPLACE_PENDING"):
            with self.subTest(state=state):
                unresolved = dict(self.parent, state=state, exposure_basis="adverse_unknown")
                self.assertEqual(validate.validate_cancel(support.MARKET_ID, ["312345678901"],
                                                          open_orders=[unresolved]), [])
                self.assertIn("size_reduction_needs_executable_order", reasons(validate.validate_cancel(
                    support.MARKET_ID, [{"bet_id": "312345678901", "size_reduction_p": 50}],
                    open_orders=[unresolved])))
        child = dict(self.parent, state="UNKNOWN", provisional_child=True, parent_ref=orders.REF,
                     customer_order_ref="bro200101120000-00000000000000c1", exposure_basis="adverse_unknown")
        self.assertEqual(validate.validate_cancel(support.MARKET_ID, ["312345678901"], open_orders=[child]), [])
        for state in ("EXECUTION_COMPLETE", "LAPSED", "REPLACED", "SETTLED"):
            with self.subTest(state=state):
                done = dict(self.parent, state=state, size_remaining_p=0)
                self.assertIn("bet_not_cancellable_state", reasons(validate.validate_cancel(
                    support.MARKET_ID, ["312345678901"], open_orders=[done])))
        drained = dict(self.parent, size_remaining_p=0)
        self.assertIn("bet_not_cancellable_state", reasons(validate.validate_cancel(
            support.MARKET_ID, ["312345678901"], open_orders=[drained])))
        # Bets the kill sweep found on the exchange are ours by its evidence, for full cancels only.
        target = {"bet_id": "312345678999", "market_id": support.MARKET_ID, "leg": "bet_id"}
        self.assertEqual(validate.validate_cancel(support.MARKET_ID, ["312345678999"], open_orders=[],
                                                  kill_targets=[target]), [])
        # A sourceIdKey leg proves nothing (amendment A3, D4): only our strategy refs and journal betIds do.
        self.assertEqual(validate.KILL_TARGET_LEGS, ("strategy_ref", "bet_id"))
        self.assertIn("kill_targets_invalid", reasons(validate.validate_cancel(
            support.MARKET_ID, ["312345678999"], open_orders=[], kill_targets=[dict(target, leg="source_id_key")])))
        done = dict(self.parent, state="EXECUTION_COMPLETE", size_remaining_p=0, bet_id="312345678999")
        self.assertEqual(validate.validate_cancel(support.MARKET_ID, ["312345678999"], open_orders=[done],
                                                  kill_targets=[target]), [])
        self.assertIn("bet_not_ours_in_market", reasons(validate.validate_cancel(
            support.MARKET_ID, [{"bet_id": "312345678999", "size_reduction_p": 50}], open_orders=[],
            kill_targets=[target])))
        self.assertIn("bet_not_ours_in_market", reasons(validate.validate_cancel(
            support.MARKET_ID, ["312345678999"], open_orders=[], kill_targets=[dict(target, market_id="1.2")])))
        self.assertIn("kill_targets_invalid", reasons(validate.validate_cancel(
            support.MARKET_ID, ["312345678999"], open_orders=[], kill_targets=[dict(target, leg="other")])))


if __name__ == "__main__":
    unittest.main()
