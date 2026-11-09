"""Pilot 2 (P2-LP) docs stage: the plain capped bot's page (docs/betfair-pilot-2-policy.md). Its Values and Money
tables are the only copy of the rule's numbers besides the p2_lp constants, which the code's own test pins to the same
rows. Docs only: no network, no gateway; Pilot 1 is untouched.
"""

import hashlib
from pathlib import Path
import re
import unittest

from betangel_research.betfair import ticks

REPO = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO / "docs" / "betfair-pilot-2-policy.md"
P1_PATH = REPO / "research" / "pilots" / "p1-fav-t5.json"
P1_SHA256 = "4dbccb5a45f7a4779e0263d4df5048f582f10b8dc3bd22c5f2e93e791155de92"
# A Values or Money row that names a p2_lp constant: | Value | `NAME` or `NAME[i]` | integer | unit | ...
CODE_ROW = re.compile(r"^\| [^|]+ \| `([A-Z][A-Z0-9_]*)(?:\[([01])\])?` \| (-?[0-9]+) "
                      r"\| (pence|hundredths|ticks|seconds|count) \|", re.M)
PAGE_VALUES = {
    ("STAKE_P", None): (200, "pence"), ("MAX_LAY_H", None): (600, "hundredths"),
    ("PAIR_WORST_P", None): (1000, "pence"), ("RACES_DAY", None): (5, "count"), ("RACES_TOTAL", None): (10, "count"),
    ("BAND_H", 0): (150, "hundredths"), ("BAND_H", 1): (590, "hundredths"),
    ("SPREAD_TICKS", 0): (3, "ticks"), ("SPREAD_TICKS", 1): (4, "ticks"), ("MIN_ACTIVE", None): (5, "count"),
    ("SCREEN_S", 0): (-840, "seconds"), ("SCREEN_S", 1): (-780, "seconds"), ("OPEN_S", None): (-740, "seconds"),
    ("DECIDE_S", 0): (-590, "seconds"), ("DECIDE_S", 1): (-575, "seconds"),
    ("LAY_BOOK_BY_S", None): (-532, "seconds"), ("PULL_S", None): (-300, "seconds"),
    ("SLICE_S", None): (1, "seconds"), ("LATE_WAKE_S", None): (5, "seconds"), ("SKIP_STREAK", None): (5, "count"),
}


def page():
    return PAGE_PATH.read_text(encoding="utf-8")


def code_rows(text):
    return [((name, int(index) if index else None), (int(number), unit))
            for name, index, number, unit in CODE_ROW.findall(text)]


def row(text, first_cell):
    """The cells of the one table row whose first cell is first_cell."""
    found = [line.strip("|").split(" | ") for line in text.splitlines() if line.startswith(f"| {first_cell} | ")]
    assert len(found) == 1, (first_cell, len(found))
    return [cell.strip() for cell in found[0]]


class PageTableTests(unittest.TestCase):
    def setUp(self):
        self.text = page()
        self.v = {key: number for key, (number, _) in code_rows(self.text)}

    def test_each_constant_once_with_its_value_and_unit(self):
        rows = code_rows(self.text)
        self.assertEqual(len(rows), len({key for key, _ in rows}), "a constant named twice")
        self.assertEqual(dict(rows), PAGE_VALUES)

    def test_money_follows_from_the_stake_and_the_lay_cap(self):
        v = self.v
        worst_lay = v["STAKE_P", None] * (v["MAX_LAY_H", None] - 100) // 100
        self.assertEqual(worst_lay, v["PAIR_WORST_P", None])
        self.assertGreaterEqual(worst_lay, v["STAKE_P", None])       # the race's worst case is the lay's, not their sum
        self.assertEqual(row(self.text, "Pilot stop")[1:4], ["—", str(v["RACES_TOTAL", None] * worst_lay), "pence"])
        self.assertEqual(row(self.text, "Day stop")[1:4], ["—", "2000", "pence"])
        self.assertEqual(row(self.text, "Open at once")[1:4], ["—", str(worst_lay), "pence"])

    def test_rule_values(self):
        v = self.v
        self.assertEqual(ticks.tick_offset(v["BAND_H", 1], 1), v["MAX_LAY_H", None])   # b + 1 tick never above 6.0
        self.assertLess(v["BAND_H", 0], v["BAND_H", 1])
        # The widest spread's BACK sits one tick under the best lay, so SPREAD_TICKS[1] - 1 ticks over the best back.
        self.assertIn(f"**V13 up to {v['SPREAD_TICKS', 1] - 1} ticks.**", self.text)

    def test_timings_in_order(self):
        v = self.v
        order = [v["SCREEN_S", 0], v["SCREEN_S", 1], v["OPEN_S", None], v["DECIDE_S", 0], v["DECIDE_S", 1],
                 v["LAY_BOOK_BY_S", None], v["PULL_S", None], 0]
        self.assertEqual(order, sorted(set(order)))
        self.assertEqual(v["DECIDE_S", 0] - v["OPEN_S", None], 150)      # as Pilot 1's open lead
        self.assertEqual(v["DECIDE_S", 1] - v["DECIDE_S", 0], 15)        # as Pilot 1's window
        self.assertGreater(v["DECIDE_S", 0], -600)                      # inside max_minutes_before_off 10 (V9b)

    def test_page_states_the_bot_and_its_line(self):
        for text in ("`p2-run`", "`p2-summary`", "`config-p2.json`", "`p2-lp`", "`pilot_lp`",
                     "`P2 ARM <cfg12> <YYYYMMDD> UNTIL <HHMM>Z RACES <N>`", "are not in force",
                     "Each cut protection needs Yu's yes in plain words at the build consent."):
            with self.subTest(text=text):
                self.assertIn(text, self.text)


class PilotOneIsUntouchedTests(unittest.TestCase):
    def test_pilot_1_contract_bytes_unchanged(self):
        self.assertEqual(hashlib.sha256(P1_PATH.read_bytes()).hexdigest(), P1_SHA256)


if __name__ == "__main__":
    unittest.main()
