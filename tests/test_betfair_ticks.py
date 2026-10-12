"""CLASSIC ladder and money parsing tests on synthetic values (integer hundredths and pence)."""

from decimal import Decimal
import json
import unittest

from betangel_research.betfair import ticks
from betangel_research.betfair.constants import CLASSIC_BANDS

import test_betfair_support as support


class TickTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_count_and_bounds(self):
        self.assertEqual(len(ticks.LADDER_CLASSIC), 350)
        self.assertEqual(ticks.LADDER_CLASSIC[0], 101)
        self.assertEqual(ticks.LADDER_CLASSIC[-1], 100000)
        self.assertEqual(list(ticks.LADDER_CLASSIC), sorted(set(ticks.LADDER_CLASSIC)))
        # Every documented band boundary is on the ladder; steps inside each band are uniform.
        for low, high, step in CLASSIC_BANDS:
            self.assertTrue(ticks.is_on_ladder(low))
            inside = [p for p in ticks.LADDER_CLASSIC if low <= p < high]
            self.assertEqual(inside, list(range(low, high, step)))

    def test_on_off_ladder(self):
        for price in (101, 199, 200, 202, 300, 305, 410, 620, 1050, 2100, 3200, 5500, 11000, 100000):
            with self.subTest(price=price):
                self.assertTrue(ticks.is_on_ladder(price))
        for price in (100, 201, 301, 303, 405, 610, 1020, 2050, 3100, 5200, 10500, 100010, 0, -101):
            with self.subTest(price=price):
                self.assertFalse(ticks.is_on_ladder(price))
        for bad in (3.5, "350", True, None, Decimal(350)):
            with self.subTest(bad=bad):
                self.assertFalse(ticks.is_on_ladder(bad))
        with self.assertRaises(ValueError):
            ticks.tick_index(201)

    def test_tick_offset_distance(self):
        self.assertEqual(ticks.tick_offset(199, 1), 200)
        self.assertEqual(ticks.tick_offset(200, 1), 202)
        self.assertEqual(ticks.tick_offset(200, -1), 199)
        self.assertEqual(ticks.tick_offset(1000, 1), 1050)
        self.assertEqual(ticks.tick_offset(101, 349), 100000)
        self.assertEqual(ticks.ticks_between(101, 100000), 349)
        self.assertEqual(ticks.ticks_between(350, 345), 1)
        self.assertEqual(ticks.ticks_between(345, 350), 1)          # absolute, never negative
        self.assertEqual(ticks.ticks_between(195, 210), 10)
        for bad in ((101, -1), (100000, 1), (350, 1.0)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ticks.tick_offset(*bad)
        with self.assertRaises(ValueError):
            ticks.ticks_between(350, 351)

    def test_snap_up_down(self):
        self.assertEqual(ticks.snap(201, "down"), 200)
        self.assertEqual(ticks.snap(201, "up"), 202)
        self.assertEqual(ticks.snap(350, "up"), 350)
        self.assertEqual(ticks.snap(350, "down"), 350)
        self.assertEqual(ticks.snap(10500, "down"), 10000)
        self.assertEqual(ticks.snap(10500, "up"), 11000)
        for bad in ((100, "down"), (100001, "up"), (350, "nearest"), (3.5, "up")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ticks.snap(*bad)
        # Snapping is never implicit: parse_price refuses rather than snapping.
        with self.assertRaises(ValueError):
            ticks.parse_price("2.01")

    def test_integer_hundredths_no_float_drift(self):
        for price in ticks.LADDER_CLASSIC:
            text = f"{price // 100}.{price % 100:02d}"
            self.assertEqual(ticks.parse_price(text), price)
            self.assertEqual(ticks.to_hundredths(price / 100), price)
            self.assertEqual(ticks.to_hundredths(text), price)
        # Floats whose binary value is not the decimal still map exactly through repr.
        self.assertEqual(ticks.to_hundredths(1.1), 110)
        # Float arithmetic drift (0.07 * 3 == 0.21000000000000002) is refused, never rounded away.
        with self.assertRaises(ValueError):
            ticks.to_pence(0.07 * 3)
        self.assertEqual(ticks.to_pence(19.99), 1999)
        self.assertEqual(ticks.to_pence(Decimal("19.99")), 1999)
        self.assertEqual(ticks.to_pence(3), 300)
        for bad in (1.005, "1.005", 0.001, float("nan"), float("inf"), True, None, "1e1", [1]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ticks.to_pence(bad)

    def test_money_regex_rejects_nan_inf_exponent_nonpositive(self):
        self.assertEqual(ticks.parse_money("2"), 200)
        self.assertEqual(ticks.parse_money("2.5"), 250)
        self.assertEqual(ticks.parse_money("0.01"), 1)
        self.assertEqual(ticks.parse_money("999999.99"), 99999999)
        bad = ["nan", "NaN", "inf", "-inf", "Infinity", "1e1", "1E2", "0", "0.00", "-1", "+1", " 1", "1 ", "1.",
               ".5", "1.001", "1,00", "1000000", "", "1\n", "١", "１", "0x10", "1_000"]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                ticks.parse_money(text)
        for value in (2, 2.0, None, b"2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ticks.parse_money(value)
        self.assertTrue(ticks.MONEY_RE.startswith("^") and ticks.MONEY_RE.endswith("$"))
        self.assertNotIn("\\d", ticks.MONEY_RE)        # [0-9] only: \d would admit non-ASCII digits

    def test_wire_values_round_trip_2dp(self):
        for price in ticks.LADDER_CLASSIC:
            wire = ticks.wire_price(price)
            self.assertEqual(ticks.to_hundredths(json.loads(json.dumps(wire))), price)
            self.assertLessEqual(len(repr(wire).split(".")[1]), 2)
        for size in (1, 99, 100, 101, 199, 200, 1999, 5000, 123456):
            wire = ticks.wire_size(size)
            self.assertEqual(ticks.to_pence(json.loads(json.dumps(wire))), size)
        for bad in (201, 3.5, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ticks.wire_price(bad)
        for bad in (0, -1, 1.5, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ticks.wire_size(bad)


if __name__ == "__main__":
    unittest.main()
