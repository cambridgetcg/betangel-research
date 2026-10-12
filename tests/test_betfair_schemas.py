"""Exact-key schema tests on synthetic records (market 1.1, invented prices)."""

import copy
import unittest

from betangel_research.betfair import schemas

import test_betfair_support as support


def snapshot(**changes):
    value = {
        "market_id": support.MARKET_ID, "source": "fixture", "fetched_by": "fixture", "key_type": "synthetic",
        "app_key_slot": None, "observed_at": support.BASE_UTC, "monotonic_ns": 5_000_000_000,
        "pt": support.BASE_PT, "status": "OPEN", "inplay": False, "version": 4249587130, "bet_delay": 0,
        "is_market_data_delayed": None, "catalogue_present": True, "market_time": "2020-01-01T12:20:00.000Z",
        "number_of_winners": 1, "betting_type": "ODDS", "market_type": "WIN", "event_type_id": "7",
        "price_ladder_type": "CLASSIC", "invalid_reason": None,
        "runners": {"12345": {"status": "ACTIVE", "handicap": 0.0, "ltp": 3.5,
                              "back": [[3.5, 120.0], [3.45, 40.0]], "lay": [[3.55, 80.0]],
                              "traded": {"3.5": 1520.4}, "total_matched": None}},
    }
    value.update(changes)
    return value


def instruction(**changes):
    value = {"selection_id": 12345, "handicap": 0, "side": "LAY", "price_h": 350, "size_p": 200,
             "persistence_type": "LAPSE"}
    value.update(changes)
    return value


def place_report(source):
    return {"status": "SUCCESS", "error_code": None, "market_id": support.MARKET_ID, "source": source,
            "instructions": [{"customer_order_ref": "bro200101120000-0123456789abcdef", "status": "SUCCESS",
                              "error_code": None, "order_status": "EXECUTABLE", "bet_id": "312345678901",
                              "placed_date": "2020-01-01T12:00:00.000Z", "average_price_matched_h": None,
                              "size_matched_p": 0}]}


def order_record(**changes):
    value = {key: None for key in schemas.ORDER_RECORD_KEYS}
    value.update({
        "intent_id": "intent-1", "customer_order_ref": "bro200101120000-0123456789abcdef",
        "customer_ref": "brq200101120000-0123456789abcdef", "customer_strategy_ref": "bar-manual", "mode": "paper",
        "market_id": support.MARKET_ID, "selection_id": 12345, "handicap": 0, "side": "BACK", "price_h": 350,
        "size_p": 200, "persistence_type": "LAPSE", "state": "EXECUTABLE", "bet_id": None, "size_matched_p": 0,
        "size_remaining_p": 200, "size_cancelled_p": 0, "size_lapsed_p": 0, "size_voided_p": 0,
        "successful_polls": 0, "provisional_child": None, "is_probe": False, "reconcile_attempts": 0,
        "exposure_basis": "adverse_unmatched"})
    value.update(changes)
    return value


class SchemaTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_book_snapshot_exact_keys(self):
        valid = snapshot()
        self.assertEqual(schemas.validate_book_snapshot(valid), valid)
        extra = snapshot(extra=1)
        with self.assertRaisesRegex(ValueError, "exact"):
            schemas.validate_book_snapshot(extra)
        missing = snapshot()
        del missing["pt"]
        with self.assertRaisesRegex(ValueError, "exact"):
            schemas.validate_book_snapshot(missing)
        runner_extra = snapshot()
        runner_extra["runners"]["12345"]["atb"] = []
        with self.assertRaisesRegex(ValueError, "exact"):
            schemas.validate_book_snapshot(runner_extra)
        bad = [
            snapshot(observed_at="2020-01-01T12:00:00"),          # naive time
            snapshot(inplay=0),                                     # bool required
            snapshot(source="somewhere"),
            snapshot(catalogue_present=False),                      # must say why it is invalid
            snapshot(status="RUNNING"),
            snapshot(market_id="2.1"),
        ]
        unsorted = snapshot()
        unsorted["runners"]["12345"]["back"] = [[3.45, 40.0], [3.5, 120.0]]
        bad.append(unsorted)
        null_size = snapshot()
        null_size["runners"]["12345"]["lay"] = [[3.55, None]]
        bad.append(null_size)
        for item in bad:
            with self.subTest(item=item):
                with self.assertRaises(ValueError):
                    schemas.validate_book_snapshot(item)
        flagged = copy.deepcopy(null_size)
        flagged["invalid_reason"] = "non_finite_size"
        schemas.validate_book_snapshot(flagged)
        schemas.validate_book_snapshot(snapshot(catalogue_present=False, invalid_reason="catalogue_missing",
                                                market_time=None, betting_type=None, market_type=None,
                                                event_type_id=None, price_ladder_type=None))

    def test_instruction_v1_exact_keys(self):
        self.assertEqual(schemas.validate_instruction_v1(instruction()), instruction())
        for bad in (dict(instruction(), order_type="LIMIT"), instruction(price_h=351), instruction(size_p=0),
                    instruction(persistence_type="PERSIST"), instruction(side="lay"), instruction(handicap=True),
                    instruction(selection_id="12345"), instruction(price_h=3.5)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    schemas.validate_instruction_v1(bad)
        missing = instruction()
        del missing["handicap"]
        with self.assertRaises(ValueError):
            schemas.validate_instruction_v1(missing)

    def test_place_report_shape_paper_equals_live(self):
        live, paper = place_report("betfair"), place_report("paper-v1")
        schemas.validate_place_report(live)
        schemas.validate_place_report(paper)
        self.assertEqual(set(live), set(paper))
        self.assertEqual(set(live["instructions"][0]), set(paper["instructions"][0]))
        self.assertEqual(set(live), schemas.PLACE_REPORT_KEYS)
        odd = place_report("betfair")
        odd["instructions"][0]["order_status"] = "SOMETHING_NEW"     # kept for the lifecycle rules (-> UNKNOWN)
        schemas.validate_place_report(odd)
        with self.assertRaises(ValueError):
            schemas.validate_place_report(dict(place_report("betfair"), source="simulator"))
        with self.assertRaises(ValueError):
            schemas.validate_place_report(dict(place_report("betfair"), customerRef="x"))
        cancel = {"status": "SUCCESS", "error_code": None, "market_id": "1.1", "source": "paper-v1",
                  "instructions": [{"bet_id": "312345678901", "size_reduction_p": None, "status": "SUCCESS",
                                    "error_code": None, "size_cancelled_p": 200,
                                    "cancelled_date": "2020-01-01T12:00:01.000Z"}]}
        schemas.validate_cancel_report(cancel)
        replace = {"status": "PROCESSED_WITH_ERRORS", "error_code": None, "market_id": "1.1", "source": "betfair",
                   "instructions": [{"bet_id": "312345678901", "new_price_h": 345, "status": "FAILURE",
                                     "error_code": "CANCELLED_NOT_PLACED",
                                     "cancel": cancel["instructions"][0], "place": None}]}
        schemas.validate_replace_report(replace)

    def test_order_record_exact_keys(self):
        record = order_record()
        self.assertEqual(schemas.validate_order_record(record), record)
        self.assertEqual(len(schemas.ORDER_RECORD_KEYS), 35)
        with self.assertRaisesRegex(ValueError, "exact"):
            schemas.validate_order_record(dict(record, session="x"))
        for bad in (order_record(state="DONE"), order_record(exposure_basis="zero"), order_record(size_p=-1),
                    order_record(customer_strategy_ref="much-too-long-strategy"), order_record(is_probe=None),
                    order_record(body_sha256="ABC")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    schemas.validate_order_record(bad)
        schemas.validate_order_record(order_record(state="UNKNOWN", exposure_basis="adverse_unknown",
                                                   body_sha256="0" * 64, submitted_at=support.BASE_UTC,
                                                   settled_profit_own_p=-150))
        # Spec section 3 marks customer_ref as not nullable.
        with self.assertRaisesRegex(ValueError, "customer_ref"):
            schemas.validate_order_record(order_record(customer_ref=None))

    def test_order_record_unknown_never_zero_exposure(self):
        # Regression (spec section 0.3 item 6): unresolved states and provisional children count adversarially.
        for state in sorted(schemas.UNRESOLVED_STATES):
            for basis in ("none", "matched_exact", "adverse_unmatched"):
                with self.subTest(state=state, basis=basis):
                    with self.assertRaisesRegex(ValueError, "adverse_unknown"):
                        schemas.validate_order_record(order_record(state=state, exposure_basis=basis,
                                                                   size_remaining_p=0))
            schemas.validate_order_record(order_record(state=state, exposure_basis="adverse_unknown"))
        with self.assertRaisesRegex(ValueError, "adverse_unknown"):
            schemas.validate_order_record(order_record(state="EXECUTABLE", provisional_child=True,
                                                       exposure_basis="adverse_unmatched"))
        schemas.validate_order_record(order_record(state="SUBMITTING", provisional_child=True,
                                                   parent_ref="bro200101120000-00000000000000aa",
                                                   exposure_basis="adverse_unknown"))
        refused = [
            order_record(state="EXECUTABLE", exposure_basis="none"),                       # 200 remaining
            order_record(state="EXECUTABLE", exposure_basis="matched_exact"),              # remaining ignored
            order_record(state="EXECUTION_COMPLETE", size_matched_p=200, size_remaining_p=0,
                         exposure_basis="none"),
            order_record(state="EXECUTABLE", size_matched_p=500, size_remaining_p=0,
                         exposure_basis="matched_exact"),                                  # matched > size
            order_record(size_matched_p=150, size_remaining_p=100),                        # parts > size
            order_record(state="VOIDED", size_remaining_p=0, size_voided_p=201, exposure_basis="none"),
        ]
        for index, record in enumerate(refused):
            with self.subTest(refused=index):
                with self.assertRaises(ValueError):
                    schemas.validate_order_record(record)
        accepted = [
            order_record(state="EXECUTION_COMPLETE", size_matched_p=200, size_remaining_p=0,
                         exposure_basis="matched_exact"),
            order_record(state="EXECUTABLE", size_matched_p=50, size_remaining_p=150),
            order_record(state="FAILED", size_remaining_p=0, exposure_basis="none"),
            order_record(state="REFUSED", size_remaining_p=0, exposure_basis="none"),
            order_record(state="RECORDED", size_remaining_p=0, exposure_basis="none"),
            order_record(state="LAPSED", size_remaining_p=0, size_lapsed_p=200, exposure_basis="none"),
            order_record(state="SETTLED", size_matched_p=200, size_remaining_p=0, exposure_basis="matched_exact"),
            order_record(state="VOIDED", size_matched_p=200, size_remaining_p=0, size_voided_p=200,
                         exposure_basis="none"),
        ]
        for index, record in enumerate(accepted):
            with self.subTest(accepted=index):
                schemas.validate_order_record(record)

    def test_identifiers_ascii_digits_only_and_numbers_never_overflow(self):
        arabic = "\u0661\u0662\u0663"
        bad = [
            lambda: schemas.validate_order_record(order_record(market_id="1." + arabic)),
            lambda: schemas.validate_order_record(order_record(bet_id=arabic)),
            lambda: schemas.validate_book_snapshot(snapshot(market_id="1." + arabic)),
            lambda: schemas.validate_book_snapshot(snapshot(observed_at="2020-01-01T12:00:00." + arabic + "Z")),
            lambda: schemas.validate_place_report(dict(place_report("betfair"), market_id="1." + arabic)),
        ]
        relabelled = snapshot()
        relabelled["runners"] = {"\u0667": relabelled["runners"]["12345"]}
        bad.append(lambda: schemas.validate_book_snapshot(relabelled))
        huge = snapshot()
        huge["runners"]["12345"]["back"] = [[3.5, 10 ** 400]]
        bad.append(lambda: schemas.validate_book_snapshot(huge))
        bad.append(lambda: schemas.validate_book_snapshot(snapshot(runners={"12345": dict(
            snapshot()["runners"]["12345"], handicap=-10 ** 400)})))
        for index, call in enumerate(bad):
            with self.subTest(case=index):
                with self.assertRaises(ValueError):
                    call()


if __name__ == "__main__":
    unittest.main()
