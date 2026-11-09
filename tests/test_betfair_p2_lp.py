"""Pure Pilot 2 logic (betangel_research.betfair.p2_lp) against the Pilot 2 page and engineering note, synthetic.

These show the runner rule at its band and spread edges and its tie order, the leg prices at the worked tick edges
and the lay cap, the passive check with our own BACK resting, the schedule row, the screen, each leg's choice with
its window and codes, the race counts, the ARM record, every leg refusal in order, config-p2's pins (and that
load_config_p2 reads only its own path), the switched-off default (IN_FORCE False), the constants against the page's
Values and Money rows, the handoff, the summary, the copied rules against their originals, and the module's imports
and closure. Books are synthetic snapshots that pass schemas.validate_book_snapshot; orders are synthetic core-fold
records. No journal file, clock, network or Keychain is touched. They do not show that a caller fetched, journaled or
sent what these functions assume (PR-2), nor anything about Betfair's real responses.
"""

import ast
import copy
import inspect
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from betangel_research.betfair import config as cfg
from betangel_research.betfair import journal, p2_lp, pilot, pilot_gate, schemas
from betangel_research.betfair.errors import ConfigError
from betangel_research.betfair.paths import FORBID_NETWORK_ENV
from betangel_research.betfair.receipts import utc_from_ns
from betangel_research.betfair.ticks import LADDER_CLASSIC, tick_index

import test_betfair_backtest_boundaries as bb
import test_betfair_boundaries as boundaries
import test_betfair_pilot_2_docs as docs
import test_betfair_support as support
import test_betfair_support_orders as so

REPO = Path(__file__).resolve().parent.parent
PATH = REPO / "betangel_research" / "betfair" / "p2_lp.py"
P2_EXAMPLE = REPO / "research" / "betfair-config-p2.example.json"
S = p2_lp.NS
US = 1000
DAY = "2027-01-04"
NEXT_DAY = "2027-01-05"
DAY_NS = pilot.parse_utc_ns(DAY + "T00:00:00Z")
OFF = DAY_NS + 10 * 3600 * S                       # 10:00:00Z
ARM_NS = DAY_NS + 9 * 3600 * S                     # the ARM record, 09:00:00Z
UNTIL = DAY_NS + 20 * 3600 * S
NOW = OFF - 590 * S
MARKET = "1.234"
CFG = "c" * 64
SESSION = "p2s-0a1b2c3d"
LP = {"arm_seq": 5, "session_id": SESSION, "races": 5}
PAIR = {"market_id": MARKET, "selection_id": 11, "lay_price_h": 305}


def runner(back, lay, *, status="ACTIVE", size=15.0):
    return so.runner(back=[(back, size)] if back is not None else [], lay=[(lay, size)] if lay is not None else [],
                     ltp=None, status=status)


def runners(prices=(), statuses=()):
    """Runner 11 at 3.00/3.15 (3 ticks, in band) and 12-16 at 7.0/7.2 (out of band), with overrides."""
    table = {11: (3.0, 3.15), **{selection: (7.0, 7.2) for selection in range(12, 17)}}
    table.update(dict(prices))
    statuses = dict(statuses)
    return {str(s): runner(*table[s], status=statuses.get(s, "ACTIVE")) for s in sorted(table)}


def live_book(observed_ns=NOW, book_runners=None, **changes):
    """A send book as the gateway's own fetch gives it: live key, not delayed, off at OFF."""
    value = so.snapshot(market_id=MARKET, source="rest_listMarketBook", fetched_by="gateway_self_fetch",
                        key_type="live", app_key_slot="live", is_market_data_delayed=False,
                        observed_at=utc_from_ns(observed_ns), market_time=utc_from_ns(OFF),
                        runners=runners() if book_runners is None else book_runners)
    value.update(copy.deepcopy(changes))
    return schemas.validate_book_snapshot(value)


def delayed_book(**changes):
    """SCREEN's book: the delayed key, read by a read command at off -800 s."""
    return live_book(OFF - 800 * S, **{"fetched_by": "read_command", "key_type": "delayed",
                                       "app_key_slot": "delayed", "is_market_data_delayed": True, **changes})


def leg(ref, market_id=MARKET, *, side="BACK", submitted_ns=None, **changes):
    """A p2-lp order record of the core fold (EXECUTABLE, bet id set, by default)."""
    fields = {"customer_order_ref": ref, "intent_id": f"intent-{ref}", "customer_strategy_ref": "p2-lp",
              "mode": "live", "market_id": market_id, "selection_id": 11, "side": side,
              "price_h": 310 if side == "BACK" else 305,
              "submitted_at": None if submitted_ns is None else utc_from_ns(submitted_ns)}
    fields.update(changes)
    return so.order_record(**fields)


def by_ref(*records):
    return {record["customer_order_ref"]: record for record in records}


def arm_event(seq=5, *, races=5, until_ns=UNTIL, session_id=SESSION, config_sha256=CFG, day=DAY,
              recorded_ns=ARM_NS):
    line = p2_lp.arm_line(config_sha256[:12], day, "2000", races)
    return {"seq": seq, "kind": "operator_confirm", "recorded_at": utc_from_ns(recorded_ns),
            "payload": p2_lp.arm_payload(line, session_id=session_id, config_sha256=config_sha256, utc_day=day,
                                         until_ns=until_ns, races=races, code_commit="d" * 40)}


def intent_event(seq, ref, at_ns, source="pilot_lp"):
    return {"seq": seq, "kind": "intent", "customer_order_ref": ref, "recorded_at": utc_from_ns(at_ns),
            "payload": {"source": source}}


# ---------------------------------------------------------------------------------------------------------------

class Rule(unittest.TestCase):
    """The runner rule, the leg prices and the passive check (note §4), as the parked draft's Predicates and Legs
    tests, on snapshots."""

    def select(self, prices=(), statuses=()):
        return p2_lp.select_runner(live_book(book_runners=runners(prices, statuses)))

    def test_band_edges_150_590_spread_3_4_tie_break(self):
        for (back, lay), code in {(1.49, 1.52): "no_runner_in_band", (1.50, 1.53): None, (5.90, 6.4): None,
                                  (6.0, 6.6): "no_runner_in_band"}.items():
            with self.subTest(back=back):
                self.assertEqual(self.select({11: (back, lay)})["code"], code)
                band = p2_lp.runner_band(runner(back, lay))
                self.assertEqual((band["in_band"], band["eligible"], band["spread_ticks"]), (code is None,) * 2 + (3,))
        for lay, code in ((3.05, "spread_out_of_band"), (3.10, "spread_out_of_band"), (3.15, None), (3.20, None),
                          (3.25, "spread_out_of_band")):
            with self.subTest(lay=lay):
                self.assertEqual(self.select({11: (3.0, lay)})["code"], code)
        # Ticks are counted on the ladder across band edges: 1.98 to 2.04 is 4 ticks.
        for back, lay, spread in ((1.98, 2.04, 4), (1.98, 2.02, 3), (2.96, 3.10, 4), (3.90, 4.2, 4), (5.90, 6.6, 4)):
            with self.subTest(back=back, lay=lay):
                self.assertEqual(p2_lp.runner_band(runner(back, lay))["spread_ticks"], spread)
                self.assertIsNone(self.select({11: (back, lay)})["code"])
        # Off the ladder, equal, crossed, missing or not whole hundredths: out of band.
        for back, lay in ((3.03, 3.20), (3.0, 3.17), (3.0, 3.0), (3.15, 3.0), (3.005, 3.15), (None, 3.15),
                          (3.0, None)):
            with self.subTest(back=back, lay=lay):
                self.assertEqual(self.select({11: (back, lay)})["code"], "no_runner_in_band")
        self.assertEqual(self.select({12: (3.0, 3.15)}, {11: "SUSPENDED"})["selection_id"], 12)
        self.assertEqual(self.select({11: (3.0, 3.15)}, {11: "REMOVED"})["code"], "no_runner_in_band")
        for first, second, chosen in (((3.0, 3.20), (3.0, 3.15), 12), ((3.0, 3.15), (3.0, 3.20), 11),
                                      ((3.0, 3.15), (2.98, 3.10), 12), ((3.0, 3.15), (3.0, 3.15), 11)):
            with self.subTest(first=first, second=second):
                self.assertEqual(self.select({11: first, 12: second})["selection_id"], chosen)
        self.assertEqual(self.select({11: (3.0, 3.15), 12: (2.50, 2.54)}),      # 2 ticks loses to the eligible 11
                         {"code": None, "selection_id": 11, "best_back_h": 300, "best_lay_h": 315})
        self.assertEqual(self.select({11: (7.0, 7.2)}), {"code": "no_runner_in_band", "selection_id": None,
                                                         "best_back_h": None, "best_lay_h": None})
        for size in (0.01, 0.0, 1000000.0):                                    # no depth predicate
            book = live_book(book_runners={**runners(), "11": runner(3.0, 3.15, size=size)})
            self.assertIsNone(p2_lp.select_runner(book)["code"])

    def test_leg_prices_every_band_and_lay_cap_600(self):
        for back, lay, back_leg, lay_leg in ((150, 153, 152, 151), (150, 154, 153, 151), (198, 202, 200, 199),
                                             (198, 204, 202, 199), (296, 305, 300, 298), (296, 310, 305, 298),
                                             (390, 410, 400, 395), (390, 420, 410, 395), (590, 640, 620, 600),
                                             (590, 660, 640, 600), (300, 315, 310, 305), (300, 320, 315, 305)):
            with self.subTest(back=back, lay=lay):
                found = p2_lp.leg_prices(back, lay)
                self.assertEqual(found, {"BACK": back_leg, "LAY": lay_leg})
                self.assertEqual(tick_index(found["BACK"]), tick_index(lay) - 1)
                self.assertEqual(tick_index(found["LAY"]), tick_index(back) + 1)
                self.assertTrue(back < found["LAY"] < found["BACK"] < lay)
        for back in (p for p in LADDER_CLASSIC if p2_lp.BAND_H[0] <= p <= p2_lp.BAND_H[1]):
            for spread in range(p2_lp.SPREAD_TICKS[0], p2_lp.SPREAD_TICKS[1] + 1):
                lay = LADDER_CLASSIC[tick_index(back) + spread]
                self.assertLessEqual(p2_lp.leg_prices(back, lay)["LAY"], p2_lp.MAX_LAY_H)
        self.assertEqual(p2_lp.leg_prices(590, 640)["LAY"], p2_lp.MAX_LAY_H)
        for back, lay, code in ((600, 660, "lay_price_above_cap"), (300, 300, "leg_prices_invalid"),
                                (305, 300, "leg_prices_invalid"), (300, 305, "leg_prices_invalid"),
                                (300, 310, "leg_prices_invalid"), (303, 315, "leg_prices_invalid"),
                                (None, 315, "leg_prices_invalid"), (True, 315, "leg_prices_invalid"),
                                (300, 315.0, "leg_prices_invalid"), ("300", 315, "leg_prices_invalid")):
            with self.subTest(back=back, lay=lay), self.assertRaises(pilot.PilotError) as caught:
                p2_lp.leg_prices(back, lay)
            self.assertEqual(caught.exception.code, code)

    def test_passive_strict_counting_own_back(self):
        """The BACK at l - 1 is passive on its book; once it rests it is the best lay the LAY sees, and b + 1 is still
        strictly inside."""
        for back, lay in ((300, 315), (198, 204), (590, 660)):
            legs = p2_lp.leg_prices(back, lay)
            self.assertIsNone(p2_lp.passive_check(best_back_h=back, best_lay_h=lay, side="BACK", price_h=legs["BACK"]))
            self.assertIsNone(p2_lp.passive_check(best_back_h=back, best_lay_h=legs["BACK"], side="LAY",
                                                  price_h=legs["LAY"]))
        for back, lay, side, price in ((300, 310, "BACK", 310), (300, 305, "BACK", 310), (315, 310, "BACK", 310),
                                       (305, 310, "LAY", 305), (310, 315, "LAY", 305), (300, 300, "LAY", 300)):
            with self.subTest(back=back, lay=lay, side=side):
                self.assertEqual(p2_lp.passive_check(best_back_h=back, best_lay_h=lay, side=side, price_h=price),
                                 "leg_not_passive")
        for kw in ({"best_back_h": None, "best_lay_h": 315}, {"best_back_h": 300, "best_lay_h": None},
                   {"best_back_h": True, "best_lay_h": 315}, {"best_back_h": 300.0, "best_lay_h": 315},
                   {"best_back_h": 300, "best_lay_h": 315, "side": "SIDEWAYS"},
                   {"best_back_h": 300, "best_lay_h": 315, "price_h": True},
                   {"best_back_h": 300, "best_lay_h": 315, "price_h": 310.0}):
            with self.subTest(kw=kw):
                self.assertEqual(p2_lp.passive_check(**{"side": "BACK", "price_h": 310, **kw}), "leg_not_passive")
        found = {p2_lp.passive_check(best_back_h=b, best_lay_h=l, side=side, price_h=p)
                 for b in (None, 298, 300, 305) for l in (None, 305, 310, 315) for p in (300, 305, 310, 315)
                 for side in p2_lp.SIDES}
        self.assertEqual(found, {None, "leg_not_passive"})
        self.assertEqual(set(inspect.signature(p2_lp.passive_check).parameters),
                         {"best_back_h", "best_lay_h", "side", "price_h"})


class Screen(unittest.TestCase):
    def test_schedule_row_turn_in_play(self):
        def row(**description):
            return {"marketId": MARKET, "description": {"bettingType": "ODDS", "turnInPlayEnabled": True,
                                                        **description}}

        self.assertIsNone(p2_lp.schedule_row_ok(row()))
        for value in (False, None, "true", 1):
            with self.subTest(turn_in_play=value):
                self.assertEqual(p2_lp.schedule_row_ok(row(turnInPlayEnabled=value)), "turn_in_play_not_enabled")
        no_flag = row()
        del no_flag["description"]["turnInPlayEnabled"]
        self.assertEqual(p2_lp.schedule_row_ok(no_flag), "turn_in_play_not_enabled")
        for bad in (row(bettingType="ASIAN_HANDICAP_DOUBLE_LINE"), {"marketId": MARKET}, {"description": None},
                    None, []):
            with self.subTest(row=bad):
                self.assertEqual(p2_lp.schedule_row_ok(bad), "betting_type_not_odds")
        self.assertEqual(p2_lp.schedule_row_ok(row(bettingType="LINE", turnInPlayEnabled=False)),
                         "betting_type_not_odds")

    def test_screen_codes(self):
        """Every screen problem at once, then mended one at a time: each step shows the next code."""
        self.assertIsNone(p2_lp.screen(delayed_book()))
        self.assertIsNone(p2_lp.screen(delayed_book(bet_delay=5)))                 # only a live send book needs 0
        four = runners(statuses={15: "REMOVED", 16: "REMOVED"})
        five = runners(statuses={16: "REMOVED"})
        self.assertIsNone(p2_lp.screen(delayed_book(runners=five)))
        mends = [("not_delayed", {"is_market_data_delayed": True}), ("market_not_open", {"status": "OPEN"}),
                 ("market_inplay", {"inplay": False}),
                 ("too_few_active_runners", {"runners": runners({11: (7.0, 7.2)})}),
                 ("no_runner_in_band", {"runners": runners({11: (3.0, 3.05)})}),
                 ("spread_out_of_band", {"runners": runners()})]
        current = {"is_market_data_delayed": None, "status": "SUSPENDED", "inplay": True, "runners": four}
        for code, mend in mends:
            self.assertEqual(p2_lp.screen(delayed_book(**current)), code)
            current.update(mend)
        self.assertIsNone(p2_lp.screen(delayed_book(**current)))
        self.assertEqual(p2_lp.screen(live_book()), "not_delayed")                   # a live-key book never screens


class Choose(unittest.TestCase):
    def test_choose_back_window_and_codes(self):
        instruction, pending, code = p2_lp.choose_back(live_book(), country="GB")
        self.assertEqual(instruction, {"selection_id": 11, "handicap": 0, "side": "BACK", "price_h": 310,
                                       "size_p": 200, "persistence_type": "LAPSE"})
        self.assertEqual((pending, code), (PAIR, None))
        schemas.validate_instruction_v1(instruction)
        self.assertEqual(p2_lp.choose_back(live_book(), country="IE")[2], None)
        for country in ("FR", None, "gb", ""):
            with self.subTest(country=country):
                self.assertEqual(p2_lp.choose_back(live_book(), country=country), (None, None, "p2_country"))
        for observed, code in ((OFF - 590 * S, None), (OFF - 575 * S, None), (OFF - 582 * S, None),
                               (OFF - 590 * S - US, "p2_window"), (OFF - 575 * S + US, "p2_window"),
                               (OFF - 300 * S, "p2_window")):
            with self.subTest(observed=(observed - OFF) / S):
                self.assertEqual(p2_lp.choose_back(live_book(observed), country="GB")[2], code)
        self.assertEqual(p2_lp.choose_back(live_book(market_time=None), country="GB")[2], "p2_window")
        for changes, code in (({"is_market_data_delayed": True}, "book_delayed"),
                              ({"is_market_data_delayed": None}, "book_delayed"),
                              ({"bet_delay": 1}, "bet_delay_not_zero"),
                              ({"status": "SUSPENDED"}, "market_not_open"), ({"inplay": True}, "market_inplay"),
                              ({"runners": runners(statuses={15: "REMOVED", 16: "REMOVED"})}, "too_few_active_runners"),
                              ({"runners": runners({11: (7.0, 7.2)})}, "no_runner_in_band"),
                              ({"runners": runners({11: (3.0, 3.10)})}, "spread_out_of_band")):
            with self.subTest(changes=changes):
                self.assertEqual(p2_lp.choose_back(live_book(**changes), country="GB"), (None, None, code))
        # The order: the country, then the window, then the book.
        late_delayed = live_book(OFF - 300 * S, is_market_data_delayed=True)
        self.assertEqual(p2_lp.choose_back(late_delayed, country="FR")[2], "p2_country")
        self.assertEqual(p2_lp.choose_back(late_delayed, country="GB")[2], "p2_window")

    def test_choose_lay_deadline_and_codes(self):
        resting = runners({11: (3.0, 3.10)})                      # our BACK at 3.10 is now the best lay
        instruction, code = p2_lp.choose_lay(live_book(OFF - 560 * S, resting), PAIR)
        self.assertEqual(instruction, {"selection_id": 11, "handicap": 0, "side": "LAY", "price_h": 305,
                                       "size_p": 200, "persistence_type": "LAPSE"})
        self.assertIsNone(code)
        schemas.validate_instruction_v1(instruction)
        for observed, found in ((OFF - 532 * S, None), (OFF - 590 * S, None), (OFF - 532 * S + US, "p2_send_deadline"),
                                (OFF - 300 * S, "p2_send_deadline")):
            with self.subTest(observed=(observed - OFF) / S):
                self.assertEqual(p2_lp.choose_lay(live_book(observed, resting), PAIR)[1], found)
        self.assertEqual(p2_lp.choose_lay(live_book(OFF - 560 * S, resting, market_time=None), PAIR),
                         (None, "p2_send_deadline"))
        for pending in (None, dict(PAIR, market_id="1.999")):
            with self.subTest(pending=pending):
                self.assertEqual(p2_lp.choose_lay(live_book(OFF - 560 * S, resting), pending),
                                 (None, "p2_lay_without_back"))
        cases = (({"bet_delay": 1}, "bet_delay_not_zero"), ({"is_market_data_delayed": True}, "book_delayed"),
                 ({"inplay": True}, "market_inplay"),
                 ({"runners": runners({11: (3.0, 3.10)}, {11: "SUSPENDED"})}, "runner_not_active"),
                 ({"runners": {k: v for k, v in resting.items() if k != "11"} | {"17": runner(7.0, 7.2)}},
                  "runner_not_active"),
                 ({"runners": runners({11: (3.05, 3.10)})}, "leg_not_passive"),     # someone backs at our price
                 ({"runners": runners({11: (3.0, 3.05)})}, "leg_not_passive"),      # a lay at our price
                 ({"runners": runners({11: (3.0, None)})}, "leg_not_passive"))
        for changes, found in cases:
            with self.subTest(changes=changes):
                self.assertEqual(p2_lp.choose_lay(live_book(OFF - 560 * S, resting, **changes), PAIR), (None, found))


class Counts(unittest.TestCase):
    def test_races_submitted_only_utc_day_and_session(self):
        orders = by_ref(
            leg("a1", "1.1", submitted_ns=ARM_NS + 10 * S), leg("a2", "1.1", side="LAY", submitted_ns=ARM_NS + 40 * S),
            leg("r1", "1.2", state="REFUSED", bet_id=None),                              # refused before its submit
            leg("p1", "1.3", submitted_ns=ARM_NS + 60 * S, customer_strategy_ref="p1-fav-t5"),
            leg("m1", "1.4", submitted_ns=ARM_NS + 70 * S, customer_strategy_ref="bar-manual"),
            leg("late", "1.5", submitted_ns=DAY_NS + pilot.DAY_NS - US),             # 23:59:59.999999Z
            leg("next", "1.6", submitted_ns=DAY_NS + pilot.DAY_NS),                  # 00:00:00Z the next day
            leg("early", "1.7", submitted_ns=ARM_NS - S))
        self.assertEqual(p2_lp.races(orders), {"1.1", "1.5", "1.6", "1.7"})
        self.assertEqual(p2_lp.races(orders, day=DAY), {"1.1", "1.5", "1.7"})
        self.assertEqual(p2_lp.races(orders, day=NEXT_DAY), {"1.6"})
        self.assertEqual(p2_lp.races(orders, since_ns=ARM_NS + 10 * S), {"1.1", "1.5", "1.6"})   # at or after
        self.assertEqual(p2_lp.races(orders, since_ns=ARM_NS + 11 * S), {"1.1", "1.5", "1.6"})   # its LAY counts
        self.assertEqual(p2_lp.races(orders, since_ns=ARM_NS, day=NEXT_DAY), {"1.6"})
        self.assertEqual(p2_lp.races({}), set())

    def test_arm_problem_each_code(self):
        event = arm_event()
        self.assertEqual(event["payload"]["echo"], f"P2 ARM {CFG[:12]} 20270104 UNTIL 2000Z RACES 5")
        self.assertEqual(p2_lp.arm_line(CFG[:12], DAY, "2000", 5),
                         p2_lp.ARM_LINE.format(cfg12=CFG[:12], day="20270104", hhmm="2000", n=5))
        self.assertIs(event["payload"]["typed_before_lock"], True)
        journal.prepare_event("operator_confirm", event["payload"])                   # a valid core payload
        ok = {"config_sha256": CFG, "session_id": SESSION, "now_ns": NOW}
        self.assertIsNone(p2_lp.arm_problem(event, **ok))
        self.assertIsNone(p2_lp.arm_problem(event, **dict(ok, now_ns=UNTIL - 1)))

        def changed(path, value):
            found = copy.deepcopy(event)
            target = found
            for key in path[:-1]:
                target = target[key]
            if value is None:
                del target[path[-1]]
            else:
                target[path[-1]] = value
            return found

        missing = [None, {}, changed(("kind",), "note"), changed(("payload", "p2_arm"), None),
                   changed(("payload",), None), changed(("payload", "p2_arm", "code_commit"), None),
                   changed(("payload", "p2_arm", "extra"), 1), changed(("payload", "p2_arm", "until_ns"), str(UNTIL)),
                   changed(("payload", "p2_arm", "races"), True), changed(("recorded_at",), "2027-01-04 09:00"),
                   changed(("recorded_at",), None)]
        for bad in missing:
            with self.subTest(event=bad):
                self.assertEqual(p2_lp.arm_problem(bad, **ok), "p2_arm_missing")
        self.assertEqual(p2_lp.arm_problem(event, **dict(ok, config_sha256="e" * 64)), "p2_arm_other_config")
        self.assertEqual(p2_lp.arm_problem(event, **dict(ok, session_id="p2s-ffffffff")), "p2_arm_other_session")
        self.assertEqual(p2_lp.arm_problem(event, **dict(ok, now_ns=DAY_NS + pilot.DAY_NS)), "p2_arm_not_today")
        self.assertEqual(p2_lp.arm_problem(event, **dict(ok, now_ns=UNTIL)), "p2_arm_expired")
        # The order: config, session, day, then UNTIL.
        self.assertEqual(p2_lp.arm_problem(event, config_sha256="e" * 64, session_id="x", now_ns=UNTIL + pilot.DAY_NS),
                         "p2_arm_other_config")
        self.assertEqual(p2_lp.arm_problem(event, config_sha256=CFG, session_id="x", now_ns=UNTIL + pilot.DAY_NS),
                         "p2_arm_other_session")
        self.assertEqual(p2_lp.arm_problem(event, **dict(ok, now_ns=UNTIL + pilot.DAY_NS)), "p2_arm_not_today")
        events = [event, arm_event(9, day=NEXT_DAY, recorded_ns=ARM_NS + pilot.DAY_NS),
                  {"seq": 6, "kind": "operator_confirm", "recorded_at": utc_from_ns(ARM_NS),
                   "payload": {"echo": "ACCEPT ABSENT", "typed_before_lock": True}}, intent_event(7, "a1", ARM_NS)]
        self.assertEqual([e["seq"] for e in p2_lp.arm_records(events)], [5, 9])
        self.assertEqual([e["seq"] for e in p2_lp.arm_records(events, day=NEXT_DAY)], [9])

    def context(self, side, broken=()):
        """leg_refusal's keywords for a leg that passes, with the named codes' causes added."""
        broken = set(broken)
        arm = arm_event(races=1 if "p2_races_session" in broken else 5,
                        until_ns=OFF - 301 * S if "p2_until_before_pull" in broken else UNTIL)
        records = []
        if side == "BACK":
            instruction = {"selection_id": 11, "handicap": 0, "side": "BACK", "price_h": 310, "size_p": 200,
                           "persistence_type": "LAPSE"}
            if "p2_market_used" in broken:                                    # refused ones count too
                records.append(leg("used", state="REFUSED", bet_id=None, error_code="R5"))
            if "p2_races_total" in broken:
                records += [leg(f"t{i}", f"1.10{i}", submitted_ns=DAY_NS - (i + 1) * pilot.DAY_NS) for i in range(10)]
            if "p2_races_today" in broken:
                records += [leg(f"d{i}", f"1.20{i}", submitted_ns=ARM_NS - 3600 * S + i * S) for i in range(5)]
            if "p2_races_session" in broken:
                records.append(leg("s0", "1.300", submitted_ns=ARM_NS + 60 * S))
            pair = None
        else:
            instruction = {"selection_id": 11, "handicap": 0, "side": "LAY", "price_h": 305, "size_p": 200,
                           "persistence_type": "LAPSE"}
            records.append(leg("back", submitted_ns=NOW))
            if "p2_lay_already_sent" in broken:
                records.append(leg("lay0", side="LAY", state="REFUSED", bet_id=None))
            if "p2_lay_not_pair" in broken:
                instruction["price_h"] = 300
            pair = None if "p2_lay_without_back" in broken else PAIR
        if "p2_leg_shape" in broken:
            instruction["size_p"] = 100
        return {"side": side, "market_id": MARKET, "instruction": instruction, "orders": by_ref(*records),
                "others": ["bro-other"] if "p2_other_bet_open" in broken else [],
                "p1_day": {"utc_day": DAY} if "p2_pilot_1_day" in broken else None,
                "arm_event": None if "p2_arm_missing" in broken else arm,
                "arm_events_today": [arm_event(2), arm] if "p2_arm_already_today" in broken else [arm], "lp": LP,
                "pair": pair, "off_ns": OFF, "config_sha256": CFG, "now_ns": NOW}

    def test_leg_refusal_each_code_in_order(self):
        """Each code alone, then every cause at once, mended one at a time in the note's order."""
        orders = {"BACK": ("p2_arm_missing", "p2_arm_already_today", "p2_pilot_1_day", "p2_market_used",
                           "p2_until_before_pull", "p2_races_total", "p2_races_today", "p2_races_session",
                           "p2_other_bet_open", "p2_leg_shape"),
                  "LAY": ("p2_arm_missing", "p2_arm_already_today", "p2_pilot_1_day", "p2_lay_without_back",
                          "p2_lay_already_sent", "p2_lay_not_pair", "p2_other_bet_open", "p2_leg_shape")}
        for side, codes in orders.items():
            self.assertIsNone(p2_lp.leg_refusal(**self.context(side)))
            for code in codes:
                with self.subTest(side=side, alone=code):
                    self.assertEqual(p2_lp.leg_refusal(**self.context(side, [code])), code)
            for index, code in enumerate(codes):
                with self.subTest(side=side, first=code):
                    self.assertEqual(p2_lp.leg_refusal(**self.context(side, codes[index:])), code)

    def test_leg_refusal_edges(self):
        ok = self.context("BACK")
        at_pull = arm_event(until_ns=OFF - 300 * S)                         # UNTIL exactly at the pull
        self.assertIsNone(p2_lp.leg_refusal(**dict(ok, arm_event=at_pull, arm_events_today=[at_pull])))
        self.assertEqual(p2_lp.leg_refusal(**dict(ok, off_ns=None)), "p2_until_before_pull")
        self.assertEqual(p2_lp.leg_refusal(**dict(ok, arm_event=arm_event(config_sha256="e" * 64))),
                         "p2_arm_other_config")
        self.assertIsNone(p2_lp.leg_refusal(**dict(ok, arm_events_today=[ok["arm_event"], arm_event(9)])))
        # Another market's legs, Pilot 1's and manual orders in this market never use it up.
        self.assertIsNone(p2_lp.leg_refusal(**dict(ok, orders=by_ref(
            leg("x", "1.999", submitted_ns=ARM_NS + S), leg("p", customer_strategy_ref="p1-fav-t5"),
            leg("m", customer_strategy_ref="bar-manual")))))
        nine = by_ref(*[leg(f"t{i}", f"1.10{i}", submitted_ns=DAY_NS - (i + 1) * pilot.DAY_NS) for i in range(9)])
        self.assertIsNone(p2_lp.leg_refusal(**dict(ok, orders=nine)))
        four = by_ref(*[leg(f"d{i}", f"1.20{i}", submitted_ns=ARM_NS + (i + 1) * S) for i in range(4)])
        self.assertIsNone(p2_lp.leg_refusal(**dict(ok, orders=four)))
        self.assertEqual(p2_lp.leg_refusal(**dict(ok, orders=four, arm_event=arm_event(races=4))), "p2_races_session")
        for changes in ({"persistence_type": "PERSIST"}, {"side": "LAY"}, {"handicap": 1}):
            with self.subTest(changes=changes):
                self.assertEqual(p2_lp.leg_refusal(**dict(ok, instruction=dict(ok["instruction"], **changes))),
                                 "p2_leg_shape")
        self.assertEqual(p2_lp.leg_refusal(**dict(ok, side="SIDEWAYS")), "p2_leg_shape")
        lay = self.context("LAY")
        back = lay["orders"]["back"]
        for label, changes in (("other market", {"pair": dict(PAIR, market_id="1.999")}),
                               ("back failed", {"orders": by_ref(dict(back, state="FAILED", bet_id=None))}),
                               ("back unknown", {"orders": by_ref(dict(back, state="UNKNOWN", bet_id=None))}),
                               # Lapsed or cancelled with nothing matched (an in-line reconcile can find it so): no
                               # BACK rests beside the LAY.
                               ("back complete, nothing matched", {"orders": by_ref(dict(
                                   back, state="EXECUTION_COMPLETE", size_remaining_p=0, size_lapsed_p=200))}),
                               ("back other runner", {"orders": by_ref(dict(back, selection_id=12))}),
                               ("no back", {"orders": {}})):
            with self.subTest(case=label):
                self.assertEqual(p2_lp.leg_refusal(**dict(lay, **changes)), "p2_lay_without_back")
        self.assertIsNone(p2_lp.leg_refusal(**dict(lay, orders=by_ref(dict(back, state="EXECUTION_COMPLETE",
                                                                           size_matched_p=200)))))
        self.assertEqual(p2_lp.leg_refusal(**dict(lay, instruction=dict(lay["instruction"], selection_id=12))),
                         "p2_lay_not_pair")
        self.assertIsNone(p2_lp.leg_refusal(**dict(lay, orders=dict(lay["orders"], other=leg(
            "other", "1.999", side="LAY", submitted_ns=ARM_NS + S)))))


def in_force(test):
    """p2_lp.IN_FORCE True for this test, as only the go-live commit sets it (the pins below are tested in force)."""
    patcher = patch.object(p2_lp, "IN_FORCE", True)
    patcher.start()
    test.addCleanup(patcher.stop)


class ConfigP2(unittest.TestCase):
    def setUp(self):
        in_force(self)
        support.block_network(self)
        # No git child process: the worktree check (paths.is_inside_git_worktree) is faked; the roots are temporary.
        worktree = patch("betangel_research.betfair.paths.is_inside_git_worktree", return_value=False)
        worktree.start()
        self.addCleanup(worktree.stop)
        self.paths = support.temp_state_paths(self)

    def example(self):
        value = json.loads(P2_EXAMPLE.read_text(encoding="utf-8"))
        value["profile"] = self.paths.profile
        return value

    def test_load_config_p2_reads_only_its_own_path(self):
        with self.assertRaisesRegex(ConfigError, "config_missing"):
            cfg.load_config_p2(paths=self.paths)
        support.write_test_config(self.paths)                                  # Pilot 1's config.json
        with self.assertRaisesRegex(ConfigError, "config_missing"):
            cfg.load_config_p2(paths=self.paths)
        self.paths.config_p2_path.write_text(json.dumps(self.example()), encoding="utf-8")
        loaded = cfg.load_config_p2(paths=self.paths)
        self.assertEqual(self.paths.config_p2_path, self.paths.profile_dir / "config-p2.json")
        self.assertEqual(loaded["orders"]["customer_strategy_ref"], "p2-lp")
        self.assertEqual(cfg.order_strategy_ref(loaded, "pilot_lp"), "p2-lp")
        self.assertNotEqual(cfg.config_sha256(loaded), cfg.config_sha256(cfg.load_config(self.paths.config_path,
                                                                                          paths=self.paths)))
        # Outside the harness load_config refuses config-p2.json (so no manual command loads it); load_config_p2
        # reads its own path only.
        with patch.dict(os.environ):
            os.environ.pop(FORBID_NETWORK_ENV, None)
            with self.assertRaisesRegex(ConfigError, "config_override_not_allowed"):
                cfg.load_config(self.paths.config_p2_path, paths=self.paths)
            self.assertEqual(cfg.load_config_p2(paths=self.paths), loaded)
        above = self.example()
        above["orders"]["caps"]["max_stake_per_order"] = 60
        self.paths.config_p2_path.write_text(json.dumps(above), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "max_stake_per_order_above_absolute_ceiling"):
            cfg.load_config_p2(paths=self.paths)

    def test_config_problems_pins_every_cap(self):
        self.paths.config_p2_path.write_text(json.dumps(self.example()), encoding="utf-8")
        loaded = cfg.load_config_p2(paths=self.paths)
        self.assertEqual(p2_lp.config_problems(loaded), [])
        self.assertEqual(len(p2_lp.CONFIG_PINS), 18)
        # Pilot 1's example config breaks the ref and every cap the bot narrows.
        self.assertEqual(p2_lp.config_problems(cfg.load_config(support.write_test_config(self.paths),
                                                               paths=self.paths)),
                         ["p2_config_customer_strategy_ref", "p2_config_max_market_exposure",
                          "p2_config_max_total_exposure", "p2_config_max_orders_per_minute",
                          "p2_config_max_orders_per_hour", "p2_config_max_orders_per_day",
                          "p2_config_max_price_deviation_ticks", "p2_config_max_minutes_before_off"])
        loosened = {"customer_strategy_ref": "p2-lq", "max_stake_per_order": 2.5, "max_liability_per_order": 12.0,
                    "max_market_exposure": 20.0, "max_total_exposure": 30.0, "max_daily_loss": 25.0,
                    "max_orders_per_minute": 3, "max_orders_per_hour": 13, "max_orders_per_day": 11,
                    "max_instructions_per_request": 2, "max_price_deviation_ticks": 4, "max_book_age_s": 3.0,
                    "max_minutes_before_off": 11, "market_types": ["WIN", "PLACE"], "event_type_ids": ["7", "4339"],
                    "external_exposure_tolerance": 0.01, "heartbeat_api_enabled": True, "app_key_slot": "live"}
        for path, _ in p2_lp.CONFIG_PINS:
            with self.subTest(key=path[-1]):
                changed = copy.deepcopy(loaded)
                target = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = loosened[path[-1]]
                cfg.validate_config(changed, paths=self.paths)                 # still a valid config
                self.assertEqual(p2_lp.config_problems(changed), [f"p2_config_{path[-1]}"])
                del target[path[-1]]
                self.assertEqual(p2_lp.config_problems(changed), [f"p2_config_{path[-1]}"])
        for key, value in (("max_stake_per_order", True), ("max_instructions_per_request", True),
                           ("max_book_age_s", "2.0")):
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(loaded)
                changed["orders"]["caps"][key] = value
                self.assertEqual(p2_lp.config_problems(changed), [f"p2_config_{key}"])
        stake_2 = copy.deepcopy(loaded)
        stake_2["orders"]["caps"]["max_stake_per_order"] = 2                  # 2 and 2.0 are the same pound value
        self.assertEqual(p2_lp.config_problems(stake_2), [])
        with_pilot = copy.deepcopy(loaded)
        with_pilot["orders"]["pilot"] = {"contract_path": "research/pilots/p1-fav-t5.json",
                                         "contract_sha256": "a" * 64, "evidence_deadline_s": 2}
        cfg.validate_config(with_pilot, paths=self.paths)
        self.assertEqual(p2_lp.config_problems(with_pilot), ["p2_config_pilot"])
        self.assertEqual(cfg.order_strategy_ref(with_pilot, "pilot_lp"), "bar-manual")   # why it is refused

    def test_example_is_the_main_example_with_the_bots_orders_values(self):
        main = json.loads(support.EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        p2 = json.loads(P2_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual({key: value for key, value in p2.items() if key != "orders"},
                         {key: value for key, value in main.items() if key != "orders"})
        changed = ("customer_strategy_ref", "caps")
        self.assertEqual({key: value for key, value in p2["orders"].items() if key not in changed},
                         {key: value for key, value in main["orders"].items() if key not in changed})
        self.assertIs(p2["orders"]["live"]["armed"], False)
        self.assertNotIn("pilot", p2["orders"])
        text = P2_EXAMPLE.read_text(encoding="utf-8")
        for literal in boundaries.NEVER_ANYWHERE:
            self.assertNotIn(literal.lower(), text.lower().replace("heartbeat_api_enabled", ""))


class SwitchedOff(unittest.TestCase):
    """The code ships switched off (note §1): IN_FORCE is False in the committed file, and config_problems then refuses
    every config, config-p2 exactly as pinned included, with p2_not_in_force first."""

    def test_in_force_false_until_the_go_live_commit(self):
        self.assertIs(p2_lp.IN_FORCE, False)
        self.assertEqual(PATH.read_text(encoding="utf-8").count("\nIN_FORCE = False\n"), 1)
        example = json.loads(P2_EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(p2_lp.config_problems(example), ["p2_not_in_force"])
        with_pilot = copy.deepcopy(example)
        with_pilot["orders"]["pilot"] = {}
        self.assertEqual(p2_lp.config_problems(with_pilot), ["p2_not_in_force", "p2_config_pilot"])
        self.assertEqual(p2_lp.config_problems({})[0], "p2_not_in_force")
        with patch.object(p2_lp, "IN_FORCE", True):
            self.assertEqual(p2_lp.config_problems(example), [])
            self.assertEqual(p2_lp.config_problems(with_pilot), ["p2_config_pilot"])


class Page(unittest.TestCase):
    def test_constants_match_policy_money_table(self):
        """Every code row of the page's Values and Money tables (the docs test's regex) equals its constant."""
        text = docs.page()
        rows = docs.code_rows(text)
        self.assertEqual(len(rows), 20)
        self.assertEqual(len(dict(rows)), 20)
        for (name, index), (number, unit) in rows:
            with self.subTest(constant=name, index=index):
                value = getattr(p2_lp, name)
                self.assertEqual(value if index is None else value[index], number)
                self.assertIs(type(value), int if index is None else tuple)
        for name in ("BAND_H", "SPREAD_TICKS", "SCREEN_S", "DECIDE_S"):
            self.assertEqual(len(getattr(p2_lp, name)), 2)
        line = p2_lp.ARM_LINE.format(cfg12="<cfg12>", day="<YYYYMMDD>", hhmm="<HHMM>", n="<N>")
        for literal in (line, p2_lp.STRATEGY_REF, p2_lp.SOURCE):
            self.assertIn(f"`{literal}`", text)
        self.assertEqual(p2_lp.PAIR_WORST_P, p2_lp.STAKE_P * (p2_lp.MAX_LAY_H - 100) // 100)
        self.assertEqual(p2_lp.CONFIG_PINS[10], (("orders", "caps", "max_price_deviation_ticks"),
                                                 p2_lp.SPREAD_TICKS[1] - 1))


class Handoff(unittest.TestCase):
    def test_handoff_names_phone_app_first(self):
        checkout = "/Users/someone/betangel-research-p2"
        legs = {"bro-a": leg("bro-a", submitted_ns=NOW, bet_id="312345678901"),
                "bro-b": leg("bro-b", side="LAY", submitted_ns=NOW + S, state="UNKNOWN", bet_id=None),
                "bro-c": None}
        lines = p2_lp.handoff_lines(legs, checkout=checkout, market_id=MARKET)
        self.assertEqual(lines[:3], [
            "p2-lp leg 312345678901: BACK £2.00 @ 3.10 on runner 11 in 1.234, state EXECUTABLE",
            "p2-lp leg bro-b: LAY £2.00 @ 3.05 on runner 11 in 1.234, state UNKNOWN",
            "p2-lp leg bro-c in 1.234: its side, price and state could not be read"])
        text = "\n".join(lines)
        self.assertLess(text.index("phone app"), text.index("website"))
        self.assertLess(text.index("website"), text.index("kill"))
        self.assertIn(f'cd "{checkout}" && python3 -m betangel_research.betfair kill --reason "p2 handoff"', text)
        self.assertNotIn("account", text)
        self.assertIn("A kill from Pilot 1's checkout misses these legs when the journal cannot be read.", text)
        self.assertEqual(p2_lp.handoff_lines({"bro-c": None}, checkout=checkout)[0],
                         "p2-lp leg bro-c in unknown market: its side, price and state could not be read")


class Summary(unittest.TestCase):
    def journal(self):
        day2 = pilot.DAY_NS
        legs = [
            # Day 1, race 1.101: a round trip, the LAY part cancelled at the pull.
            leg("a1", "1.101", submitted_ns=ARM_NS + 10 * S, state="EXECUTION_COMPLETE", size_matched_p=200,
                average_price_matched_h=310),
            leg("a2", "1.101", side="LAY", submitted_ns=ARM_NS + 40 * S, state="EXECUTION_COMPLETE",
                size_matched_p=100, size_cancelled_p=100, average_price_matched_h=305, bet_id="312345678902"),
            # Day 1, race 1.102: the BACK pulled unmatched, the LAY refused by R5 before its submit.
            leg("b1", "1.102", submitted_ns=ARM_NS + 900 * S, state="EXECUTION_COMPLETE", size_cancelled_p=200,
                bet_id="312345678903"),
            leg("b2", "1.102", side="LAY", state="REFUSED", bet_id=None, error_code="R5"),
            # Day 2: 1.103 rejected by Betfair; 1.104 a lone BACK settled, its LAY lapsed; 1.105 unknown.
            leg("c1", "1.103", submitted_ns=ARM_NS + day2 + 10 * S, state="FAILED", bet_id=None,
                error_code="INSUFFICIENT_FUNDS"),
            leg("d1", "1.104", submitted_ns=ARM_NS + day2 + 900 * S, state="SETTLED", size_matched_p=200,
                average_price_matched_h=310, bet_id="312345678904"),
            leg("d2", "1.104", side="LAY", submitted_ns=ARM_NS + day2 + 930 * S, state="LAPSED", size_lapsed_p=200,
                bet_id="312345678905"),
            leg("e1", "1.105", submitted_ns=ARM_NS + day2 + 1800 * S, state="UNKNOWN", bet_id=None),
            # Pilot 1's and manual orders are never counted.
            leg("p1", "1.106", submitted_ns=ARM_NS + 20 * S, customer_strategy_ref="p1-fav-t5", size_matched_p=200,
                state="EXECUTION_COMPLETE"),
            leg("m1", "1.107", submitted_ns=ARM_NS + 30 * S, customer_strategy_ref="bar-manual")]
        events, seq = [arm_event(1), arm_event(20, day=NEXT_DAY, recorded_ns=ARM_NS + day2)], 30
        for order in legs:
            at = pilot.parse_utc_ns(order["submitted_at"]) - S if order["submitted_at"] else ARM_NS + 41 * S
            events.append(intent_event(seq, order["customer_order_ref"], at))
            seq += 1
        return events, by_ref(*legs)

    def test_summary_counts(self):
        events, orders = self.journal()
        found = p2_lp.summary(events, orders)
        total = found["total"]
        self.assertEqual(total, {
            "sessions": 2, "races": 5, "sent": {"BACK": 5, "LAY": 2}, "placed": {"BACK": 3, "LAY": 2},
            "refused": {"BACK": 1, "LAY": 1}, "codes": {"INSUFFICIENT_FUNDS": 1, "R5": 1}, "matched": 3,
            "matched_full": 2, "round_trips": 1, "lone_backs": 1, "lone_lays": 0, "cancelled": 2, "lapsed": 1,
            "unresolved": 1, "settled_worst_p": 200})
        self.assertEqual(list(found["days"]), [DAY, NEXT_DAY])
        day1, day2 = found["days"][DAY], found["days"][NEXT_DAY]
        self.assertEqual((day1["sessions"], day1["races"], day1["sent"], day1["refused"], day1["round_trips"]),
                         (1, 2, {"BACK": 2, "LAY": 1}, {"BACK": 0, "LAY": 1}, 1))
        self.assertEqual((day2["sessions"], day2["races"], day2["lone_backs"], day2["settled_worst_p"],
                          day2["unresolved"], day2["codes"]), (1, 3, 1, 200, 1, {"INSUFFICIENT_FUNDS": 1}))
        for key in ("matched", "cancelled", "lapsed"):
            self.assertEqual(day1[key] + day2[key], total[key])
        self.assertEqual(found["races_left"], 5)
        self.assertEqual([order["customer_order_ref"] for order in found["open"]], ["a1", "a2", "e1"])
        self.assertEqual(found["matched_markets"], ["1.101", "1.104"])
        empty = p2_lp.summary([], {})
        self.assertEqual((empty["total"]["races"], empty["races_left"], empty["days"], empty["open"]), (0, 10, {}, []))

    def test_summary_lines_and_cleared(self):
        events, orders = self.journal()
        found = p2_lp.summary(events, orders)
        lines = p2_lp.summary_lines(found, checkout="/Users/someone/p2")
        self.assertEqual(lines[0], (
            "total: sessions 2, races 5; legs sent BACK 5, LAY 2; placed BACK 3, LAY 2; refused or failed BACK 1, "
            "LAY 1 (codes: INSUFFICIENT_FUNDS 1, R5 1); matched 3 (2 in full); round trips 1, lone BACKs 1, lone LAYs "
            "0; cancelled 2, lapsed 1, unresolved 1; booked at settle -£2.00; races left 5"))
        self.assertTrue(lines[1].startswith(f"{DAY}: sessions 1, races 2;"))
        self.assertTrue(lines[2].startswith(f"{NEXT_DAY}: sessions 1, races 3;"))
        self.assertIn("3 p2-lp legs are open or unsettled", lines[3])
        self.assertEqual(lines[4:7], p2_lp.handoff_lines({o["customer_order_ref"]: o for o in found["open"]},
                                                         checkout="/Users/someone/p2")[:3])
        self.assertFalse(any("cleared" in line for line in lines))
        cleared = [{"marketId": "1.104", "profit": 1.96, "commission": 0.04}, {"marketId": "1.999", "profit": -2.0},
                   {"marketId": "1.998", "profit": "x"}, "not a row"]
        tail = p2_lp.summary_lines(found, cleared, checkout="/Users/someone/p2")[len(lines):]
        self.assertEqual(tail, [
            "cleared 1.104: profit £1.96, commission £0.04",
            "cleared 1.999: profit -£2.00, commission not given",
            "check the website: 1.999 is cleared but has no matched p2-lp leg in the journal",
            "check the website: a cleared row for 1.998 has no readable profit",
            "check the website: a cleared row for a market has no readable profit",
            "check the website: 1.101 has a matched p2-lp leg but no cleared figure",
            "cleared total: profit -£0.04, commission £0.04"])
        quiet = p2_lp.summary_lines(p2_lp.summary([], {}), [], checkout="/Users/someone/p2")
        self.assertEqual(quiet[-1], "cleared total: profit £0.00, commission £0.00")
        self.assertEqual(len(quiet), 2)


class Copies(unittest.TestCase):
    """The rules copied because their modules may not be imported here, each against its original."""

    def test_copied_rules_equal_originals(self):
        self.assertEqual(p2_lp.UNRESOLVED_STATES, schemas.UNRESOLVED_STATES)
        self.assertEqual(p2_lp.TERMINAL_STATES, schemas.TERMINAL_STATES)
        records = []
        for state in sorted(schemas.ORDER_STATES):
            for side in p2_lp.SIDES:
                for matched, remaining in ((0, 0), (0, 200), (100, 100), (200, 0)):
                    for average in (None, 305, 320):
                        records.append(so.order_record(
                            customer_order_ref=f"r{len(records)}", customer_strategy_ref="p2-lp", state=state,
                            side=side, price_h=310, size_matched_p=matched, size_remaining_p=remaining,
                            average_price_matched_h=average if matched else None))
        for record in records:
            with self.subTest(record=record["customer_order_ref"]):
                self.assertEqual(p2_lp._settled_worst_p(record), journal.settled_worst_loss_pence(record))
        events = [intent_event(i, record["customer_order_ref"], ARM_NS) for i, record in enumerate(records)]
        self.assertEqual(sorted(r["customer_order_ref"] for r in records if p2_lp._open_or_unsettled(r)),
                         pilot_gate.open_manual_refs(events, by_ref(*records)))


class Boundaries(unittest.TestCase):
    """The module is pure: its imports, closure, calls and text, as tests/test_betfair_pilot.py holds pilot.py."""

    def test_imports_only_pilot_and_ticks(self):
        imports = {(module, level, tuple(names)) for module, level, names in boundaries.imported_modules(PATH)}
        self.assertEqual(imports, {("", 1, ("pilot",)),
                                   ("ticks", 1, ("is_on_ladder", "tick_index", "tick_offset", "to_hundredths",
                                                 "to_pence"))})
        module = f"{bb.BETFAIR}.p2_lp"
        self.assertIn("p2_lp", bb.ALLOWED_BETFAIR)
        self.assertNotIn("p2_lp", bb.FORBIDDEN_BETFAIR)
        self.assertEqual(set(bb.REAL.closure([module])), {bb.TOP, bb.BETFAIR, module, bb.PILOT,
                                                          f"{bb.BETFAIR}.ticks", f"{bb.BETFAIR}.constants"})
        # pilot_violations' rule (the backtest rule) from p2_lp instead of pilot.
        found = bb.REAL.violations([module], *bb.backtest_rules(bb.BETFAIR, bb.FORBIDDEN_BETFAIR,
                                                                 bb.shadowing_names(bb.REAL, [module])))
        for reached in sorted(bb.REAL.closure([module])):
            found += [(reached, site, "binds an internal package object outside the backtest package")
                      for site in bb.REAL.packages[reached] if not bb.under(site.partition(": ")[2],
                                                                            f"{bb.BETFAIR}.backtest")]
        found += bb.stdlib_shadow_violations(bb.REAL, [module])
        self.assertEqual(found, [], bb.describe(found))
        tree = boundaries.tree(PATH)
        # pilot.py's list (tests/test_betfair_pilot.py).
        banned = {"open", "write", "write_text", "write_bytes", "read_text", "read_bytes", "mkdir", "touch", "unlink",
                  "rename", "replace", "chmod", "now", "utcnow", "today", "time", "time_ns", "monotonic",
                  "monotonic_ns", "perf_counter", "sleep", "getenv", "urandom", "random", "randint"}
        self.assertEqual([ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)
                          and boundaries.called_name(n) in banned], [])
        text = PATH.read_text(encoding="utf-8")
        for banned_text in (*bb.LIVE_STATE_TEXT, *boundaries.ORDER_LITERALS, *boundaries.NEVER_ANYWHERE):
            self.assertNotIn(banned_text.lower(), text.lower())


if __name__ == "__main__":
    unittest.main()
