"""paper-v1 fill model and paper replay tests on invented snapshots; nothing here is execution evidence."""

import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from betangel_research.betfair import lifecycle, paper
from betangel_research.betfair.receipts import file_sha256, strict_json_loads, utc_from_ns

import test_betfair_support as support
import test_betfair_support_orders as orders
import test_betfair_support_stream as fx

PARAMS = orders.order_config()["orders"]["paper"]
BOOK_MS = support.BASE_PT + 123          # BASE_UTC carries .123457 s
REF_A = "bro200101120000-000000000000000a"
REF_B = "bro200101120000-000000000000000b"


def paper_order(ref=REF_A, **changes):
    value = {"customer_order_ref": ref, "market_id": support.MARKET_ID, "selection_id": 12345, "side": "BACK",
             "price_h": 350, "size_p": 200}
    value.update(changes)
    return value


def book(back=((3.5, 10.0),), lay=((3.55, 10.0),), traded=None, **changes):
    return orders.snapshot(runners={"12345": orders.runner(back=back, lay=lay, traded=traded)}, **changes)


def open_record(ref=REF_A, **changes):
    changes.setdefault("bet_id", paper.paper_bet_id(ref))
    return orders.order_record(customer_order_ref=ref, **changes)


def at_ms(ms):
    """observed_at for a REST-style book at ms."""
    return utc_from_ns(ms * 1_000_000)


def resting(queues, *, activated_ms=BOOK_MS):
    """An exchange holding resting orders with known queues, active since activated_ms."""
    return paper.PaperExchange(PARAMS, queues=queues, state={"activated_ms": {ref: activated_ms for ref in queues}})


class FillModelTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.exchange = paper.PaperExchange(PARAMS)

    def place(self, order=None, *, book_=None, now_ms=BOOK_MS, exchange=None):
        report = (exchange or self.exchange).place([order or paper_order()], book=book_ or book(), now_ms=now_ms)
        return report["instructions"][0], report

    def test_immediate_back_best_first_level_price(self):
        levels = ((3.6, 2.0), (3.55, 4.0), (3.5, 10.0), (3.45, 50.0))
        item, report = self.place(paper_order(size_p=500), book_=book(back=levels))
        # 1.00 @ 3.60, 2.00 @ 3.55, then 2.00 of the 5.00 displayed at 3.50; 3.45 is never reached.
        self.assertEqual((item["size_matched_p"], item["average_price_matched_h"]), (500, 354))
        self.assertEqual(item["order_status"], "EXECUTION_COMPLETE")
        self.assertEqual((report["status"], report["source"]), ("SUCCESS", "paper-v1"))
        # A separate exchange: on this one REF_A already took the displayed size of that snapshot.
        partial, _ = self.place(paper_order(REF_B, size_p=500, price_h=355), book_=book(back=levels),
                                exchange=paper.PaperExchange(PARAMS))
        self.assertEqual((partial["size_matched_p"], partial["order_status"]), (300, "EXECUTABLE"))
        self.assertEqual(partial["average_price_matched_h"], (100 * 360 + 200 * 355) // 300)   # floored for a BACK
        self.assertEqual(lifecycle.classify_place_report(report, sent_refs=[REF_A])[REF_A][0], "EXECUTION_COMPLETE")

    def test_lay_symmetric(self):
        levels = ((3.4, 2.0), (3.45, 4.0), (3.5, 10.0), (3.55, 50.0))
        item, _ = self.place(paper_order(side="LAY", size_p=500), book_=book(lay=levels))
        self.assertEqual((item["size_matched_p"], item["average_price_matched_h"]), (500, 346))
        uneven, _ = self.place(paper_order(REF_B, side="LAY", size_p=150, price_h=345),
                               book_=book(lay=((3.4, 1.0), (3.45, 4.0))), exchange=paper.PaperExchange(PARAMS))
        # 0.50 @ 3.40 and 1.00 @ 3.45: 517000 / 150 = 343.33 rounded up for a LAY.
        self.assertEqual((uneven["size_matched_p"], uneven["average_price_matched_h"]), (150, 344))

    def test_displayed_fraction_floor_pence(self):
        item, _ = self.place(paper_order(size_p=200), book_=book(back=((3.5, 2.01),)))
        self.assertEqual(item["size_matched_p"], 100)                  # 2.01 x 0.5 = 1.005 -> 1.00
        tiny, _ = self.place(paper_order(REF_B, size_p=200), book_=book(back=((3.5, 0.03),)),
                             exchange=paper.PaperExchange(PARAMS))
        self.assertEqual(tiny["size_matched_p"], 1)                    # 1.5p -> 1p
        third = paper.PaperExchange(dict(PARAMS, displayed_size_fraction=0.3))
        item, _ = self.place(paper_order("bro200101120000-000000000000000c", size_p=200),
                             book_=book(back=((3.5, 3.33),)), exchange=third)
        self.assertEqual(item["size_matched_p"], 99)                   # 0.999 -> 0.99

    def test_our_orders_share_displayed_size(self):
        """Regression: orders against one snapshot (in one call or several) together take at most
        floor(displayed x fraction) per level, and what they took stays taken in later snapshots."""
        refs = [f"bro200101120000-{i:016x}" for i in range(4)]
        single = book(back=((3.5, 4.0),))
        report = self.exchange.place([paper_order(ref, size_p=200) for ref in refs], book=single, now_ms=BOOK_MS)
        self.assertEqual([i["size_matched_p"] for i in report["instructions"]], [200, 0, 0, 0])
        later = self.exchange.place([paper_order("bro200101120000-00000000000000ff", size_p=200)], book=single,
                                    now_ms=BOOK_MS)
        self.assertEqual(later["instructions"][0]["size_matched_p"], 0)       # same snapshot, separate call
        two_levels = paper.PaperExchange(PARAMS).place(
            [paper_order(refs[0], size_p=150), paper_order(refs[1], size_p=150)],
            book=book(back=((3.6, 2.0), (3.5, 4.0))), now_ms=BOOK_MS)
        self.assertEqual([(i["size_matched_p"], i["average_price_matched_h"]) for i in two_levels["instructions"]],
                         [(150, (100 * 360 + 50 * 350) // 150), (150, 350)])
        # Regression (audit): a later snapshot with the same level, or any change elsewhere in the market,
        # used to reset the takes, so successive orders filled 4.00 x 0.5 again and again.
        later_book = book(back=((3.5, 4.0),), observed_at="2020-01-01T12:00:00.223457Z")
        fresh = self.exchange.place([paper_order("bro200101120000-00000000000000fe", size_p=200)],
                                    book=later_book, now_ms=BOOK_MS + 100)
        self.assertEqual(fresh["instructions"][0]["size_matched_p"], 0)
        # A bigger level brings new size: 10.00 x 0.5 = 5.00, less the 2.00 already taken there.
        bigger = self.exchange.place([paper_order("bro200101120000-00000000000000fd", size_p=500)],
                                     book=book(back=((3.5, 10.0),), observed_at="2020-01-01T12:00:00.323457Z"),
                                     now_ms=BOOK_MS + 200)
        self.assertEqual(bigger["instructions"][0]["size_matched_p"], 300)

    def test_takes_persist_across_exchanges(self):
        """Regression (audit): the CLI rebuilds the exchange per process; the takes travel in its state."""
        first = paper.PaperExchange(PARAMS)
        single = book(back=((3.5, 4.0),))
        self.assertEqual(first.place([paper_order(size_p=200)], book=single, now_ms=BOOK_MS)
                         ["instructions"][0]["size_matched_p"], 200)
        state = json.loads(json.dumps(first.export_state()))
        second = paper.PaperExchange(PARAMS, queues=first.queue_state(), state=state)
        self.assertEqual(second.place([paper_order(REF_B, size_p=200)], book=single, now_ms=BOOK_MS)
                         ["instructions"][0]["size_matched_p"], 0)
        self.assertEqual(second.export_state()["taken"], {support.MARKET_ID: {"12345:back:350": 200}})
        for bad in ({"taken": {support.MARKET_ID: {"12345:middle:350": 1}}}, {"activated_ms": {REF_A: "x"}},
                    {"traded_hwm": {support.MARKET_ID: {"12345:350": "NaN"}}}, {"other": {}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                paper.PaperExchange(PARAMS, state=bad)

    def test_activation_order_survives_a_process_boundary(self):
        """Regression R3#9: passive-fill priority (one step's traded volume goes to our earlier orders at a
        price first) used an in-process sequence that export_state() dropped, so an order from an earlier
        process sorted after every order of the current one: X 10.00 then Y 10.00 at 3.55 gave X=10.00,
        Y=10.00 in one process but X=2.00, Y=10.00 across a process boundary."""
        refs = {"X": "bro200101120000-00000000000000a1", "Y": "bro200101120000-00000000000000b2"}

        def snap(ms, lay, traded):
            return book(back=((3.5, 10.0),), lay=lay, traded={"3.55": traded}, observed_at=at_ms(ms))

        def run(split):
            exchange = paper.PaperExchange(PARAMS)
            exchange.place([paper_order(refs["X"], price_h=355, size_p=1000)],
                           book=snap(BOOK_MS, ((3.55, 8.0),), 100.0), now_ms=BOOK_MS)     # X queues behind 8.00
            if split:                                                         # a new process, through JSON
                exchange = paper.PaperExchange(PARAMS, queues=exchange.queue_state(),
                                               state=json.loads(json.dumps(exchange.export_state())))
            y_book = snap(BOOK_MS + 1000, ((3.6, 5.0),), 100.0)
            exchange.place([paper_order(refs["Y"], price_h=355, size_p=1000)], book=y_book, now_ms=BOOK_MS + 1000)
            matched = {name: 0 for name in refs}
            prev = snap(BOOK_MS + 2000, ((3.6, 5.0),), 100.0)
            exchange.advance([], prev_book=y_book, book=prev, now_ms=BOOK_MS + 2000)
            for step, traded in enumerate((120.0, 140.0), 1):              # 20.00 traded per step, 10.00 halved
                now = snap(BOOK_MS + 2000 + step * 1000, ((3.6, 5.0),), traded)
                records = [open_record(ref, price_h=355, size_p=1000, size_matched_p=matched[name],
                                       size_remaining_p=1000 - matched[name],
                                       average_price_matched_h=355 if matched[name] else None)
                           for name, ref in refs.items() if matched[name] < 1000]
                for update in exchange.advance(records, prev_book=prev, book=now, now_ms=BOOK_MS + 2000 + step * 1000):
                    name = "X" if update["customer_order_ref"] == refs["X"] else "Y"
                    matched[name] = update["fields"].get("size_matched_p", matched[name])
                prev = now
            return matched, exchange

        one, _ = run(False)
        split, exchange = run(True)
        self.assertEqual(one, {"X": 1000, "Y": 1000})
        self.assertEqual(split, one)
        state = exchange.export_state()
        self.assertEqual((state["activation"], state["sequence"]), ({}, 2))     # finished orders are forgotten
        # States written before R3#9 still load; a broken order is refused.
        old_state = paper.PaperExchange(PARAMS, state={"taken": {}, "activated_ms": {}})
        self.assertEqual(old_state.export_state()["sequence"], 0)
        for bad in ({"activation": {REF_A: 2}, "sequence": 1}, {"activation": {REF_A: 0}, "sequence": 1},
                    {"sequence": -1}, {"activation": [REF_A], "sequence": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                paper.PaperExchange(PARAMS, state=bad)

    def test_our_resting_orders_share_traded_volume(self):
        """Regression: one traded delta is consumed by the queue ahead and then by our resting orders at
        that price in activation order; it never fills each of them independently."""
        refs = [f"bro200101120000-{i:016x}" for i in range(1, 3)]
        exchange = paper.PaperExchange(PARAMS)
        lay = ((3.55, 10.0), (3.6, 0.0), (3.7, 20.0))
        for ref in reversed(refs):                    # activation order: refs[1] first, then refs[0]
            exchange.place([paper_order(ref, price_h=360)], book=book(lay=lay), now_ms=BOOK_MS)
        self.assertEqual(exchange.queue_state(), {refs[0]: 0, refs[1]: 0})
        records = [open_record(ref, price_h=360) for ref in refs]
        # 4.00 traded at 3.60, halved: 200p for both of our 2.00 orders together (both books after activation).
        updates = exchange.advance(records, prev_book=book(traded={"3.6": 100.0}, observed_at=at_ms(BOOK_MS + 500)),
                                   book=book(traded={"3.6": 104.0}, observed_at=at_ms(BOOK_MS + 1000)),
                                   now_ms=BOOK_MS + 1000)
        self.assertEqual([(u["customer_order_ref"], u["state"]) for u in updates],
                         [(refs[1], "EXECUTION_COMPLETE")])
        self.assertEqual(sum(u["fields"]["size_matched_p"] for u in updates), 200)
        # With a queue ahead, only what is left after the queue reaches us, earliest first.
        queued = resting({refs[0]: 100, refs[1]: 100})
        updates = queued.advance(records, prev_book=book(traded={"3.6": 100.0}), book=book(traded={"3.6": 105.0}),
                                 now_ms=BOOK_MS + 1000)
        self.assertEqual(sum(u["fields"].get("size_matched_p", 0) for u in updates), 150)
        self.assertEqual([u["fields"]["size_matched_p"] for u in updates], [150])

    def test_resting_back_queues_on_lay_ladder(self):
        lay = ((3.55, 10.0), (3.6, 7.5), (3.7, 20.0))
        item, _ = self.place(paper_order(price_h=360), book_=book(back=((3.5, 10.0),), lay=lay))
        self.assertEqual((item["size_matched_p"], item["order_status"]), (0, "EXECUTABLE"))
        self.assertEqual(self.exchange.queue_state()[REF_A], 750)
        self.place(paper_order(REF_B, price_h=365), book_=book(back=((3.5, 10.0),), lay=lay))
        self.assertEqual(self.exchange.queue_state()[REF_B], 0)        # inside the shown range, nobody at 3.65
        lay_ref = "bro200101120000-000000000000000c"
        self.place(paper_order(lay_ref, side="LAY", price_h=340),
                   book_=book(back=((3.5, 10.0), (3.45, 3.0), (3.4, 6.0)), lay=lay))
        self.assertEqual(self.exchange.queue_state()[lay_ref], 600)    # a resting LAY queues on the back ladder

    def advance(self, order, prev_traded, now_traded, **book_changes):
        prev = book(traded=prev_traded)
        now = book(traded=now_traded, **book_changes)
        return self.exchange.advance([order], prev_book=prev, book=now, now_ms=BOOK_MS + 1000)

    def test_resting_needs_trd_beyond_queue(self):
        exchange = resting({REF_A: 750})
        self.exchange = exchange
        order = open_record(price_h=360)
        # 10.00 traded at 3.60 = 1000p, halved = 500p: all of it goes to the 7.50 queue ahead.
        self.assertEqual(self.advance(order, {"3.6": 100.0}, {"3.6": 110.0}), [])
        self.assertEqual(exchange.queue_state()[REF_A], 250)
        # Another 6.00 = 300p: 250p finishes the queue, 50p fills us at our own price.
        updates = self.advance(order, {"3.6": 110.0}, {"3.6": 116.0})
        self.assertEqual(updates, [{"customer_order_ref": REF_A, "state": "EXECUTABLE",
                                    "fields": {"size_matched_p": 50, "average_price_matched_h": 360,
                                               "size_remaining_p": 150}, "flags": ["passive_fill"]}])
        self.assertEqual(exchange.queue_state()[REF_A], 0)
        # Volume at another price never fills us (no trade-through).
        filled = dict(order, size_matched_p=50, average_price_matched_h=360, size_remaining_p=150)
        self.assertEqual(self.advance(filled, {"3.6": 116.0, "3.65": 0.0}, {"3.6": 116.0, "3.65": 900.0}), [])
        done = self.advance(filled, {"3.6": 116.0}, {"3.6": 200.0})
        self.assertEqual(done[0]["state"], "EXECUTION_COMPLETE")
        self.assertEqual(done[0]["fields"]["size_matched_p"], 200)
        self.assertNotIn(REF_A, exchange.queue_state())

    def test_queue_unknown_beyond_depth_never_fills(self):
        lay = ((3.55, 10.0), (3.6, 7.5), (3.7, 20.0))
        item, _ = self.place(paper_order(price_h=400), book_=book(back=((3.5, 10.0),), lay=lay),
                             now_ms=BOOK_MS - 500)                   # active from BOOK_MS
        self.assertIsNone(self.exchange.queue_state()[REF_A])
        updates = self.advance(open_record(price_h=400), {"4.0": 0.0}, {"4.0": 10000.0})
        self.assertEqual(updates, [{"customer_order_ref": REF_A, "state": "EXECUTABLE", "fields": {},
                                    "flags": ["passive_fill_blocked_queue_unknown"]}])
        empty = paper.PaperExchange(PARAMS)
        empty.place([paper_order(REF_B, price_h=360)], book=book(back=(), lay=()), now_ms=BOOK_MS)
        self.assertIsNone(empty.queue_state()[REF_B])                  # empty ladder: queue unknown, conservative
        fresh = paper.PaperExchange(PARAMS)                            # no carried queue state: unknown
        self.exchange = fresh
        # Nor any activation time: the first interval never counts, then the unknown queue blocks fills.
        self.assertEqual(self.advance(open_record(price_h=360), {"3.6": 0.0}, {"3.6": 500.0})[0]["flags"],
                         ["passive_fill_skipped_pre_activation_interval"])
        self.assertEqual(self.advance(open_record(price_h=360), {"3.6": 500.0}, {"3.6": 900.0})[0]["flags"],
                         ["passive_fill_blocked_queue_unknown"])

    def test_no_trd_no_passive_label(self):
        self.exchange = resting({REF_A: 0})
        updates = self.advance(open_record(price_h=360), None, {"3.6": 500.0})
        self.assertEqual(updates[0]["flags"], ["passive_fill_unavailable_no_trd"])
        self.assertEqual(self.advance(open_record(price_h=360), {"3.6": 1.0}, None)[0]["flags"],
                         ["passive_fill_unavailable_no_trd"])

    def test_negative_and_settlement_trd_ignored(self):
        self.exchange = resting({REF_A: 0})
        order = open_record(price_h=360)
        self.assertEqual(self.advance(order, {"3.6": 50.0}, {"3.6": 40.0})[0]["flags"],
                         ["negative_traded_delta_ignored"])
        self.assertEqual(self.advance(order, {"3.6": 50.0}, {})[0]["flags"], ["settlement_zeroing_ignored"])
        self.assertEqual(self.advance(order, {"3.6": 50.0}, {"3.6": 0.0})[0]["flags"], ["settlement_zeroing_ignored"])
        self.assertEqual(self.advance(order, {"3.6": 50.0}, {"3.333": 60.0})[0]["flags"],
                         ["traded_volume_invalid_no_passive_fill"])
        self.assertEqual(self.exchange.queue_state()[REF_A], 0)

    def test_lapse_inplay_and_version_bump(self):
        order = open_record(price_h=360)
        inplay = self.exchange.advance([order], prev_book=book(), book=book(inplay=True), now_ms=BOOK_MS)
        self.assertEqual(inplay[0]["state"], "LAPSED")
        self.assertEqual(inplay[0]["flags"], ["lapsed_inplay"])
        partial = dict(order, size_matched_p=50, average_price_matched_h=360, size_remaining_p=150)
        partly = self.exchange.advance([partial], prev_book=book(), book=book(inplay=True), now_ms=BOOK_MS)
        self.assertEqual((partly[0]["state"], partly[0]["fields"]), ("EXECUTION_COMPLETE",
                                                                     {"size_lapsed_p": 150, "size_remaining_p": 0}))
        bumped = self.exchange.advance([order], prev_book=book(),
                                       book=book(status="SUSPENDED", version=4249587131, back=(), lay=()),
                                       now_ms=BOOK_MS)
        self.assertEqual(bumped[0]["flags"], ["lapsed_version_change_while_suspended"])
        plain = self.exchange.advance([order], prev_book=book(), book=book(status="SUSPENDED", back=(), lay=()),
                                      now_ms=BOOK_MS)
        self.assertEqual(plain, [])                                    # suspension blocks fills, keeps the order
        # Regression: a version change first seen on the reopening frame also happened while SUSPENDED.
        reopened = self.exchange.advance([order], prev_book=book(status="SUSPENDED", back=(), lay=()),
                                         book=book(version=4249587131), now_ms=BOOK_MS)
        self.assertEqual(reopened[0]["flags"], ["lapsed_version_change_while_suspended"])
        self.assertEqual(self.exchange.advance([order], prev_book=book(status="SUSPENDED", back=(), lay=()),
                                               book=book(), now_ms=BOOK_MS), [])
        open_to_open = self.exchange.advance([order], prev_book=book(), book=book(version=4249587131),
                                             now_ms=BOOK_MS)            # OPEN to OPEN: no suspension seen
        # Regression (audit): the order is kept, but no fill comes from an interval whose version changed.
        self.assertEqual([(u["state"], u["flags"]) for u in open_to_open],
                         [("EXECUTABLE", ["passive_fill_blocked_version_change"])])
        removed = self.exchange.advance([order], prev_book=book(),
                                        book=orders.snapshot(runners={"12345": orders.runner(status="REMOVED")}),
                                        now_ms=BOOK_MS)
        self.assertEqual(removed[0]["flags"], ["lapsed_runner_removed"])
        # The lapse applies to the record through the lifecycle unchanged.
        lifecycle.transition(order, inplay[0]["state"], reason="paper_advance", fields=inplay[0]["fields"])

    def test_unobserved_suspension_lapses(self):
        """Regression (audit): with conflated data or a recording gap the SUSPENDED frame is never seen,
        so the market goes OPEN(v1) -> OPEN(v2) with another runner removed. Betfair lapses unmatched
        bets on a removal; the order used to stay and fill from that interval's traded volume."""
        exchange = resting({REF_A: 0})
        order = open_record(price_h=360)

        def two(other_status, traded, version=4249587130):
            return orders.snapshot(version=version, runners={
                "12345": orders.runner(back=((3.5, 10.0),), lay=((3.55, 10.0),), traded=traded),
                "23456": orders.runner(status=other_status, back=(), lay=(), ltp=None)})

        conflated = exchange.advance([order], prev_book=two("ACTIVE", {"3.6": 100.0}),
                                     book=two("REMOVED", {"3.6": 300.0}, version=4249587131), now_ms=BOOK_MS)
        self.assertEqual([(u["state"], u["flags"]) for u in conflated],
                         [("LAPSED", ["lapsed_version_change_runner_removed"])])
        same_version = resting({REF_A: 0}).advance([order], prev_book=two("ACTIVE", {"3.6": 100.0}),
                                                   book=two("REMOVED", {"3.6": 300.0}), now_ms=BOOK_MS)
        self.assertEqual(same_version[0]["state"], "LAPSED")
        already = resting({REF_A: 0}).advance([order], prev_book=two("REMOVED", {"3.6": 100.0}),
                                              book=two("REMOVED", {"3.6": 300.0}), now_ms=BOOK_MS)
        self.assertEqual(already[0]["flags"], ["passive_fill"])          # a removal seen before is no change

    def test_passive_fills_count_only_volume_after_activation(self):
        """Regression (audit): an order active for the last 0.5 s of a 10 s interval was credited with the
        whole interval's traded volume, including trading before it existed."""
        for submit_ms, expected in ((BOOK_MS - 500, ["passive_fill"]),
                                    (BOOK_MS + 9000, ["passive_fill_skipped_pre_activation_interval"])):
            with self.subTest(submit_ms=submit_ms):
                exchange = paper.PaperExchange(PARAMS)
                first = book(back=((1.98, 30.0),), lay=((2.02, 10.0),), traded={"2.0": 100.0})
                exchange.place([paper_order(price_h=200, size_p=1000)], book=first, now_ms=submit_ms)
                self.assertEqual(exchange.queue_state()[REF_A], 0)
                later = book(back=((1.98, 30.0),), lay=((2.02, 10.0),), traded={"2.0": 200.0},
                             observed_at=at_ms(BOOK_MS + 10_000))
                updates = exchange.advance([open_record(price_h=200, size_p=1000, size_remaining_p=1000)],
                                           prev_book=first, book=later, now_ms=BOOK_MS + 10_000)
                self.assertEqual(updates[0]["flags"], expected)

    def test_traded_volume_counted_once(self):
        """Regression (audit): a baseline that moved back (an older book) or a dip and recovery in traded
        volume credited the same volume twice."""
        exchange = resting({REF_A: 100})
        order = open_record(price_h=355)

        def traded(volume, ms):
            return book(lay=((3.55, 1.0),), traded={"3.55": volume}, observed_at=at_ms(ms))

        self.assertEqual(exchange.advance([order], prev_book=traded(100.0, BOOK_MS), book=traded(102.0, BOOK_MS + 1000),
                                          now_ms=BOOK_MS + 1000), [])
        self.assertEqual(exchange.queue_state()[REF_A], 0)                 # 2.00 traded, halved: the queue
        dip = exchange.advance([order], prev_book=traded(102.0, BOOK_MS + 1000), book=traded(101.0, BOOK_MS + 2000),
                               now_ms=BOOK_MS + 2000)
        self.assertEqual(dip[0]["flags"], ["negative_traded_delta_ignored"])
        recovered = exchange.advance([order], prev_book=traded(101.0, BOOK_MS + 2000),
                                     book=traded(102.0, BOOK_MS + 3000), now_ms=BOOK_MS + 3000)
        self.assertEqual(recovered[0]["flags"], ["traded_volume_already_counted_ignored"])
        new = exchange.advance([order], prev_book=traded(102.0, BOOK_MS + 3000), book=traded(104.0, BOOK_MS + 4000),
                               now_ms=BOOK_MS + 4000)
        self.assertEqual(new[0]["fields"]["size_matched_p"], 100)        # only the 2.00 above 102 counts

    def test_stale_or_invalid_book_refused(self):
        cases = {
            "paper_book_stale": (book(), BOOK_MS + 10_000 - 500 + 1),
            "paper_book_after_activation": (book(), BOOK_MS - 501),
            "paper_book_invalid": (book(invalid_reason="non_finite_size"), BOOK_MS),
            "paper_rejected_market_state": (book(status="SUSPENDED"), BOOK_MS),
        }
        for code, (snapshot, now_ms) in cases.items():
            with self.subTest(code=code):
                item, report = self.place(book_=snapshot, now_ms=now_ms)
                self.assertEqual((item["status"], item["error_code"]), ("FAILURE", code))
                expected = "FAILED" if code == "paper_rejected_market_state" else "REFUSED"
                self.assertEqual(lifecycle.classify_place_report(report, sent_refs=[REF_A])[REF_A][0], expected)
        self.assertEqual(self.place(book_=book(), now_ms=BOOK_MS + 10_000 - 500)[0]["status"], "SUCCESS")
        none = self.exchange.place([paper_order()], book=None, now_ms=BOOK_MS)
        self.assertEqual(none["instructions"][0]["error_code"], "paper_book_stale")
        inplay = self.place(book_=book(inplay=True))[0]
        self.assertEqual(inplay["error_code"], "paper_rejected_market_state")
        with self.assertRaises(ValueError):
            self.exchange.place([paper_order(size_p=99)], book=book(), now_ms=BOOK_MS)

    def closed(self, statuses, number_of_winners=1):
        return orders.snapshot(status="CLOSED", number_of_winners=number_of_winners,
                               runners={sel: orders.runner(status=s, back=(), lay=(), ltp=None)
                                        for sel, s in statuses.items()})

    def matched(self, ref=REF_A, **changes):
        changes.setdefault("state", "EXECUTION_COMPLETE")
        changes.setdefault("average_price_matched_h", 350)
        return open_record(ref, size_matched_p=200, size_remaining_p=0, **changes)

    def test_removal_excludes_settlement(self):
        ours = self.exchange.settle([self.matched()], market_definition=self.closed({"12345": "REMOVED",
                                                                                    "23456": "WINNER"}))
        self.assertEqual(ours["excluded_reason"], "rule4_adjustment_not_modelled")
        self.assertEqual(ours["orders"][REF_A], {"state": "VOIDED", "settled_profit_own_p": None})
        other = self.exchange.settle([self.matched()], market_definition=self.closed({"12345": "WINNER",
                                                                                     "23456": "REMOVED"}))
        self.assertEqual(other["excluded_reason"], "rule4_adjustment_not_modelled")
        self.assertEqual(other["orders"][REF_A], {"state": "SETTLED", "settled_profit_own_p": None})
        self.assertIsNone(other["net_pence"])
        with self.assertRaises(ValueError):
            self.exchange.settle([self.matched()], market_definition=dict(self.closed({"12345": "WINNER"}),
                                                                          status="OPEN"))

    def test_dead_heat_excluded(self):
        result = self.exchange.settle([self.matched()], market_definition=self.closed({"12345": "WINNER",
                                                                                      "23456": "WINNER"}))
        self.assertEqual(result["excluded_reason"], "dead_heat_not_modelled")
        two = self.exchange.settle([self.matched()], market_definition=self.closed({"12345": "WINNER"},
                                                                                  number_of_winners=2))
        self.assertEqual(two["excluded_reason"], "number_of_winners_not_1")
        stream_style = {"id": "1.1", "status": "CLOSED", "numberOfWinners": 1,
                        "runners": [{"id": 12345, "status": "WINNER"}, {"id": 23456, "status": "WINNER"}]}
        self.assertEqual(self.exchange.settle([self.matched()], market_definition=stream_style)["excluded_reason"],
                         "dead_heat_not_modelled")

    def test_commission_source_echoed(self):
        result = self.exchange.settle([self.matched(), self.matched(REF_B, side="LAY", price_h=300,
                                                                    average_price_matched_h=300)],
                                      market_definition=self.closed({"12345": "WINNER", "23456": "LOSER"}))
        # BACK 2.00 @ 3.50 wins 500; LAY 2.00 @ 3.00 on the winner loses 400: gross 100, 5% commission 5.
        self.assertEqual((result["gross_pence"], result["commission_pence"], result["net_pence"]), (100, 5, 95))
        self.assertEqual(result["commission_source"], PARAMS["commission_source"])
        self.assertEqual(result["orders"][REF_A], {"state": "SETTLED", "settled_profit_own_p": 500})
        self.assertEqual(result["orders"][REF_B], {"state": "SETTLED", "settled_profit_own_p": -400})

    def test_cancel_and_replace_reports_classify(self):
        order = open_record(price_h=360)
        report = self.exchange.cancel([order], [order["bet_id"]], now_ms=BOOK_MS)
        pending = dict(order, state="CANCEL_PENDING")
        self.assertEqual(lifecycle.classify_cancel_report(report, sent_bet_ids=[order["bet_id"]],
                                                          orders={order["bet_id"]: pending})[order["bet_id"]][0],
                         "EXECUTION_COMPLETE")
        partial = self.exchange.cancel([order], [{"bet_id": order["bet_id"], "size_reduction_p": 50}], now_ms=BOOK_MS)
        self.assertEqual(partial["instructions"][0]["size_cancelled_p"], 50)
        unknown = self.exchange.cancel([order], ["999"], now_ms=BOOK_MS)
        self.assertEqual(unknown["instructions"][0]["error_code"], "INVALID_BET_ID")
        child = "bro200101120000-00000000000000c1"
        replaced = self.exchange.replace([order], [{"bet_id": order["bet_id"], "new_price_h": 350, "child_ref": child}],
                                         book=book(), now_ms=BOOK_MS)
        parent = dict(order, state="REPLACE_PENDING")
        moves = lifecycle.classify_replace_report(replaced, sent=[{"bet_id": order["bet_id"], "new_price_h": 350,
                                                                   "order": parent}],
                                                  provisional_children={REF_A: child})
        self.assertEqual({ref: m[0] for ref, m in moves.items()}, {REF_A: "REPLACED", child: "EXECUTION_COMPLETE"})
        stale = self.exchange.replace([order], [{"bet_id": order["bet_id"], "new_price_h": 350, "child_ref": child}],
                                      book=book(), now_ms=BOOK_MS + 60_000)
        moves = lifecycle.classify_replace_report(stale, sent=[{"bet_id": order["bet_id"], "new_price_h": 350,
                                                                "order": parent}],
                                                  provisional_children={REF_A: child})
        self.assertEqual({ref: m[0] for ref, m in moves.items()}, {REF_A: "EXECUTABLE", child: "FAILED"})

    def test_paper_exchange_has_no_transport(self):
        exchange = paper.PaperExchange(PARAMS)
        names = {name.lower() for name in vars(exchange)}
        for forbidden in ("transport", "rpc", "socket", "session", "client", "connection"):
            self.assertFalse(any(forbidden in name for name in names), forbidden)
        tree = ast.parse(Path(paper.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
        for forbidden in ("transport", "api", "auth", "esa", "gateway", "socket", "http", "urllib", "ssl",
                          "credentials", "capability", "risk"):
            self.assertNotIn(forbidden, imported)
        with self.assertRaises(ValueError):
            paper.PaperExchange(dict(PARAMS, model="paper-v2"))
        with self.assertRaises(ValueError):
            paper.PaperExchange({k: v for k, v in PARAMS.items() if k != "commission_source"})


def write_recording(path, snapshots):
    path.write_text("".join(json.dumps({"pt": pt, "snapshot": snap}, sort_keys=True) + "\n" for pt, snap in snapshots))
    return path


def jsonl_reader(path, *, key_type):
    for line in Path(path).read_text().splitlines():
        row = json.loads(line)
        snapshot = dict(row["snapshot"], key_type=key_type)
        yield row["pt"], snapshot


def recorded(pt, **changes):
    """A recording-sourced snapshot at publish time pt (two runners)."""
    runners = changes.pop("runners", None) or {
        "12345": orders.runner(back=((3.5, 10.0),), lay=((3.55, 10.0),), traded={"3.5": 100.0}),
        "23456": orders.runner(back=((2.98, 5.0), (2.96, 5.0)), lay=((3.05, 5.0),), ltp=3.0,
                               traded={"3.0": 50.0, "2.98": 10.0})}
    return orders.snapshot(source="recording", fetched_by="recording", key_type="historical", pt=pt,
                           observed_at=utc_from_ns(pt * 1_000_000), monotonic_ns=None, runners=runners, **changes)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "repo").mkdir()
        (self.root / "outputs").mkdir()
        (self.root / "rec").mkdir()
        t0 = support.BASE_PT
        # 12.00 more traded at 2.98: 600p after halving, 500p of it is the queue ahead of our LAY.
        lay_side = orders.runner(back=((2.98, 5.0), (2.96, 5.0)), lay=((3.05, 5.0),), ltp=3.0,
                                 traded={"3.0": 60.0, "2.98": 22.0})
        snapshots = [
            (t0, recorded(t0)),
            (t0 + 1000, recorded(t0 + 1000, runners={"12345": orders.runner(traded={"3.5": 100.0}),
                                                     "23456": lay_side})),
            (t0 + 2000, recorded(t0 + 2000, inplay=True)),
            (t0 + 60000, recorded(t0 + 60000, status="CLOSED", inplay=True, runners={
                "12345": orders.runner(status="WINNER", back=(), lay=(), ltp=None),
                "23456": orders.runner(status="LOSER", back=(), lay=(), ltp=None)})),
        ]
        self.recording = write_recording(self.root / "rec" / "seg-000.jsonl", snapshots)
        self.orders_path = self.root / "orders.json"
        self.orders_path.write_text(json.dumps({"schema_version": 1, "orders": [
            # Active at t0 (latency 500 ms): the t0 snapshot is the baseline of their first interval.
            {"submit_pt": t0 - 500, "market_id": "1.1", "selection_id": 12345, "side": "BACK", "price": "3.50",
             "size": "2.00"},
            {"submit_pt": t0 - 500, "market_id": "1.1", "selection_id": 23456, "side": "LAY", "price": "2.98",
             "size": "2.00"},
            {"submit_pt": t0 + 61000, "market_id": "1.1", "selection_id": 12345, "side": "BACK", "price": "3.50",
             "size": "1.00"},
            {"submit_pt": t0, "market_id": "1.9", "selection_id": 1, "side": "BACK", "price": "2.00",
             "size": "1.00"}]}))

    def replay(self, name, **kwargs):
        kwargs.setdefault("snapshot_reader", jsonl_reader)
        return paper.run_paper_replay([self.recording], self.orders_path, PARAMS, self.root / "outputs" / name,
                                      repo_root=self.root / "repo", **kwargs)

    def test_replay_outcomes(self):
        report = self.replay("run-a")
        results = report["simulated_execution"]["orders"]
        self.assertEqual([r["final_state"] for r in results], ["SETTLED", "SETTLED", "FAILED", "REFUSED"])
        back, lay, late, elsewhere = results
        self.assertEqual((back["size_matched_p"], back["average_price_matched_h"]), (200, 350))
        # The resting LAY at 2.98 queued behind the 5.00 shown on the back ladder; 12.00 traded at 2.98
        # (600p after halving) cleared that queue and filled 1.00; the other 1.00 lapsed at in-play.
        self.assertEqual((lay["size_matched_p"], lay["average_price_matched_h"], lay["size_lapsed_p"]), (100, 298, 100))
        self.assertIn("passive_fill", lay["flags"])
        self.assertEqual(late["refusal"], "paper_rejected_market_state")
        self.assertEqual(elsewhere["refusal"], "paper_book_stale")
        market = report["simulated_execution"]["markets"]["1.1"]
        # BACK 2.00 @ 3.50 on the winner +500; LAY 1.00 on the loser +100; 5% commission on 600.
        self.assertEqual((market["status"], market["gross_pence"], market["commission_pence"]), ("settled", 600, 30))
        self.assertEqual(report["simulated_execution"]["markets"]["1.9"], {"status": "not_closed_in_recording"})
        self.assertEqual(report["simulated_execution"]["totals"]["net_pence"], 570)
        self.assertIsNone(report["economic_result"])
        self.assertEqual(report["journal"]["verify"], "ok")
        self.assertEqual(report["commission_source"], PARAMS["commission_source"])
        self.assertEqual(report["assumptions"], list(paper.PAPER_ASSUMPTIONS))
        self.assertEqual(report["status"], "paper_simulation_not_execution_evidence")
        out = self.root / "outputs" / "run-a"
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["journal.sqlite", "report.json", "summary.md"])
        self.assertIn("not execution evidence", (out / "summary.md").read_text())

    def test_activation_waits_for_every_message_at_its_time(self):
        """Regression (audit): two messages share pt X and the second adds traded volume at our price.
        An order active at X must use the LAST snapshot at X, so that volume is its baseline, not a fill;
        an order active between two snapshots skips the interval it became active in."""
        t0 = support.BASE_PT
        lay_side = orders.runner(back=((2.98, 5.0),), lay=((3.05, 5.0),), ltp=3.0, traded={"3.0": 60.0})
        busy = orders.runner(back=((2.98, 5.0),), lay=((3.05, 5.0),), ltp=3.0, traded={"3.0": 60.0, "2.98": 500.0})
        recording = write_recording(self.root / "rec" / "seg-eq.jsonl", [
            (t0, recorded(t0, runners={"12345": orders.runner(traded={"3.5": 100.0}), "23456": lay_side})),
            (t0, recorded(t0, runners={"12345": orders.runner(traded={"3.5": 100.0}), "23456": busy})),
            (t0 + 1000, recorded(t0 + 1000, runners={"12345": orders.runner(traded={"3.5": 100.0}), "23456": busy})),
            (t0 + 9000, recorded(t0 + 9000, runners={"12345": orders.runner(traded={"3.5": 100.0}), "23456": busy})),
            (t0 + 10000, recorded(t0 + 10000, runners={"12345": orders.runner(traded={"3.5": 100.0}),
                                                       "23456": busy}))])
        orders_path = self.root / "orders-eq.json"
        orders_path.write_text(json.dumps({"schema_version": 1, "orders": [
            {"submit_pt": t0 - 500, "market_id": "1.1", "selection_id": 23456, "side": "LAY", "price": "2.98",
             "size": "2.00"},
            {"submit_pt": t0 + 8999, "market_id": "1.1", "selection_id": 23456, "side": "LAY", "price": "2.98",
             "size": "2.00"}]}))
        report = paper.run_paper_replay([recording], orders_path, PARAMS, self.root / "outputs" / "run-eq",
                                        repo_root=self.root / "repo", snapshot_reader=jsonl_reader)
        at_x, late = report["simulated_execution"]["orders"]
        self.assertEqual(at_x["activation_book_pt"], t0)
        self.assertEqual((at_x["size_matched_p"], at_x["final_state"]), (0, "EXECUTABLE"))
        self.assertNotIn("passive_fill", at_x["flags"])
        # The late order (active at t0 + 9499) skips the 9000 -> 10000 interval it became active in.
        self.assertIn("passive_fill_skipped_pre_activation_interval", late["flags"])
        self.assertEqual(late["size_matched_p"], 0)

    def test_bit_identical_reports(self):
        self.replay("run-a")
        self.replay("run-b")
        a, b = self.root / "outputs" / "run-a", self.root / "outputs" / "run-b"
        self.assertEqual((a / "report.json").read_bytes(), (b / "report.json").read_bytes())
        self.assertEqual((a / "summary.md").read_bytes(), (b / "summary.md").read_bytes())
        self.assertNotIn(str(self.root), (a / "report.json").read_text())       # no absolute paths

    def test_delayed_label_propagates(self):
        (self.root / "rec" / "receipt.json").write_text(json.dumps({"key_type": "delayed"}))
        report = self.replay("run-delayed")
        self.assertEqual(report["key_type"], "delayed")
        self.assertIn("delayed_data_paper_fills_unreliable", report["limitations"])
        self.assertEqual(report["data_freshness"]["label"], "delayed_data_paper_fills_unreliable")
        self.assertEqual(report["inputs"]["resolved"]["key_type_source"], "receipt")
        (self.root / "rec" / "receipt.json").unlink()
        inferred = self.replay("run-inferred")
        self.assertEqual(inferred["key_type"], "historical")
        self.assertEqual(inferred["inputs"]["resolved"]["key_type_source"], "inferred_historical")
        self.assertIn("key_type_inferred_historical_no_receipt", inferred["limitations"])
        self.assertNotIn("delayed_data_paper_fills_unreliable", inferred["limitations"])

    def test_paper_replay_ledger_row_exact_keys(self):
        report = self.replay("run-a")
        ledger = (self.root / "outputs" / "run-ledger.jsonl").read_text().splitlines()
        self.assertEqual(len(ledger), 1)
        row = strict_json_loads(ledger[0])
        self.assertEqual(set(row), {"created_at", "experiment_id", "source_sha256", "config_sha256", "code_sha256",
                                    "report", "report_sha256", "status"})
        orders_sha = file_sha256(self.orders_path)
        self.assertEqual(row["experiment_id"], "paper-replay:" + orders_sha[:12])
        self.assertEqual(row["status"], "paper_simulation_not_execution_evidence")
        self.assertEqual(row["report_sha256"], file_sha256(self.root / "outputs" / "run-a" / "report.json"))
        self.assertEqual(row["code_sha256"], report["code_sha256"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.replay("run-a")
        self.assertEqual(len((self.root / "outputs" / "run-ledger.jsonl").read_text().splitlines()), 1)
        bad = self.root / "bad-orders.json"
        for payload in ({"schema_version": 1, "orders": [{"submit_pt": 1, "market_id": "1.1", "selection_id": 1,
                                                         "side": "BACK", "price": "3.51", "size": "2.00"}]},
                        {"schema_version": 1, "orders": [{"submit_pt": 1, "market_id": "1.1", "selection_id": 1,
                                                         "side": "BACK", "price": "3.50", "size": "0.50"}]},
                        {"schema_version": 1, "orders": [{"submit_pt": 1, "market_id": "1.1", "selection_id": 1,
                                                         "side": "BACK", "price": "3.50", "size": "nan"}]},
                        {"schema_version": 2, "orders": []}):
            with self.subTest(payload=str(payload)[:60]):
                bad.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    paper.run_paper_replay([self.recording], bad, PARAMS, self.root / "outputs" / "never",
                                           repo_root=self.root / "repo", snapshot_reader=jsonl_reader)
                self.assertFalse((self.root / "outputs" / "never").exists())
        self.assertEqual([p.name for p in (self.root / "outputs").iterdir() if p.name.startswith(".")], [])

    def write_segment(self, name, messages, *, directory="stream"):
        folder = self.root / directory
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_bytes(b"".join(fx.line(m, b"\n") for m in messages))
        return path

    def test_recording_gap_is_never_forward_filled(self):
        """Regression (audit): an order active after a segment ended (connection lost) and before the next
        image matched the last pre-gap book, and passive fills counted volume traded in the unobserved gap."""
        seg0 = self.write_segment("seg-000.jsonl", [
            fx.image(1, fx.pt_at(-300)), fx.delta(1, fx.pt_at(-102), [{"id": 11, "ltp": 2.0}]),
            fx.delta(1, fx.pt_at(-100), [{"id": 11, "ltp": 2.0}])])
        seg1 = self.write_segment("seg-001.jsonl", [
            fx.image(1, fx.pt_at(-60), rc=[{"id": 11, "batb": [[0, 2.0, 30]], "batl": [[0, 2.02, 10]],
                                            "trd": [[2.0, 100], [2.02, 300]]}]),
            fx.delta(1, fx.pt_at(-50), [{"id": 12, "tv": 500}])])
        (seg0.parent / "receipt.json").write_text(json.dumps({"key_type": "synthetic", "segments": [
            {"index": 0, "path": "seg-000.jsonl", "first_pt": fx.pt_at(-300), "last_pt": fx.pt_at(-100),
             "market_ids": ["1.1"], "close_reason": "connection_lost"},
            {"index": 1, "path": "seg-001.jsonl", "first_pt": fx.pt_at(-60), "last_pt": fx.pt_at(-50),
             "market_ids": ["1.1"], "close_reason": "stopped"}],
            "gaps": [{"after_segment": 0, "reason": "connection_lost"}]}))
        orders_path = self.root / "orders-gap.json"
        orders_path.write_text(json.dumps({"schema_version": 1, "orders": [
            {"submit_pt": fx.pt_at(-102.4), "market_id": "1.1", "selection_id": 11, "side": "BACK", "price": "2.02",
             "size": "2.00"},
            {"submit_pt": fx.pt_at(-95), "market_id": "1.1", "selection_id": 11, "side": "BACK", "price": "2.02",
             "size": "2.00"}]}))
        report = paper.run_paper_replay([seg0, seg1], orders_path, PARAMS, self.root / "outputs" / "run-gap",
                                        repo_root=self.root / "repo")
        resting, in_gap = report["simulated_execution"]["orders"]
        self.assertEqual((in_gap["final_state"], in_gap["refusal"], in_gap["size_matched_p"]),
                         ("REFUSED", "paper_book_in_recording_gap", 0))
        # 300.00 traded at 2.02 shows up only after the gap: none of it fills the resting order (queue 10.00).
        self.assertEqual((resting["final_state"], resting["size_matched_p"]), ("EXECUTABLE", 0))
        self.assertIn("passive_fill_blocked_recording_gap", resting["flags"])
        self.assertTrue(any("not forward-filled" in item for item in report["limitations"]))
        without = paper.run_paper_replay([self.recording], self.orders_path, PARAMS, self.root / "outputs" / "nr",
                                         repo_root=self.root / "repo", snapshot_reader=jsonl_reader)
        self.assertIn("recording_gaps_unknown_no_receipt", without["limitations"])
        self.assertEqual([r["inputs"]["resolved"]["windows_status"] for r in (report, without)],
                         ["known", "unknown_no_receipt"])

    def gap_case(self, directory, segments, orders_, *, name, inputs=None, receipt=True):
        """Writes segments {file: messages} and a receipt listing [(file, first_pt|None, last_pt|None, market_ids)],
        then replays inputs (default: every segment) with orders_; returns the report's orders."""
        paths = {file: self.write_segment(file, messages, directory=directory) for file, messages in segments.items()}
        if receipt:
            (self.root / directory / "receipt.json").write_text(json.dumps({"key_type": "synthetic", "segments": [
                {"index": i, "path": file, "first_pt": first, "last_pt": last, "lines": len(segments[file]),
                 "market_ids": ids} for i, (file, first, last, ids) in enumerate(receipt)]}))
        orders_path = self.root / f"orders-{name}.json"
        orders_path.write_text(json.dumps({"schema_version": 1, "orders": orders_}))
        report = paper.run_paper_replay(inputs or list(paths.values()), orders_path, PARAMS,
                                        self.root / "outputs" / name, repo_root=self.root / "repo")
        return report["simulated_execution"]["orders"], report

    def test_empty_segment_keeps_the_gap_guard(self):
        """Regression R3#7: an empty segment (lines 0, first_pt null: the connection dropped after the
        subscription, before the image) switched the whole gap guard off, so replaying every segment of a
        verified session forward-filled the pre-gap book and counted volume traded in the gap."""
        segments = {
            "seg-000.jsonl": [fx.image(1, fx.pt_at(-300)), fx.delta(1, fx.pt_at(-102), [{"id": 11, "ltp": 2.0}]),
                              fx.delta(1, fx.pt_at(-100), [{"id": 11, "ltp": 2.0}])],
            "seg-001.jsonl": [],
            "seg-002.jsonl": [fx.image(1, fx.pt_at(-60), rc=[{"id": 11, "batb": [[0, 2.0, 30]], "batl": [[0, 2.02, 10]],
                                                            "trd": [[2.0, 100], [2.02, 300]]}]),
                              fx.delta(1, fx.pt_at(-50), [{"id": 12, "tv": 500}])]}
        receipt = [("seg-000.jsonl", fx.pt_at(-300), fx.pt_at(-100), ["1.1"]), ("seg-001.jsonl", None, None, []),
                   ("seg-002.jsonl", fx.pt_at(-60), fx.pt_at(-50), ["1.1"])]
        orders_ = [{"submit_pt": fx.pt_at(-102.4), "market_id": "1.1", "selection_id": 11, "side": "BACK",
                    "price": "2.02", "size": "2.00"},
                   {"submit_pt": fx.pt_at(-95), "market_id": "1.1", "selection_id": 11, "side": "BACK",
                    "price": "2.02", "size": "2.00"}]
        (resting, in_gap), report = self.gap_case("stream-empty", segments, orders_, name="empty", receipt=receipt)
        self.assertEqual((in_gap["final_state"], in_gap["refusal"], in_gap["size_matched_p"]),
                         ("REFUSED", "paper_book_in_recording_gap", 0))
        self.assertEqual((resting["final_state"], resting["size_matched_p"]), ("EXECUTABLE", 0))
        self.assertIn("passive_fill_blocked_recording_gap", resting["flags"])
        resolved = report["inputs"]["resolved"]
        self.assertEqual((resolved["windows_status"], resolved["windows"]), ("known", [
            [fx.pt_at(-300), fx.pt_at(-100), ["1.1"]], [fx.pt_at(-60), fx.pt_at(-50), ["1.1"]]]))
        self.assertNotIn("recording_gaps_unknown_no_receipt", report["limitations"])

    def test_mixed_receipt_and_no_receipt_inputs_refused(self):
        """R3#7: a recording no receipt lists would switch the gap guard off for the others; mixing is refused.
        A receipt that does not list the recording is labelled as such, not as 'no receipt'."""
        listed = self.write_segment("seg-000.jsonl", [fx.image(1, fx.pt_at(-300))], directory="mix-a")
        (listed.parent / "receipt.json").write_text(json.dumps({"key_type": "synthetic", "segments": [
            {"path": "seg-000.jsonl", "first_pt": fx.pt_at(-300), "last_pt": fx.pt_at(-300), "market_ids": ["1.1"]}]}))
        bare = self.write_segment("seg-000.jsonl", [fx.image(1, fx.pt_at(-200))], directory="mix-b")
        orders_path = self.root / "orders-mix.json"
        orders_path.write_text(json.dumps({"schema_version": 1, "orders": [
            {"submit_pt": fx.pt_at(-250), "market_id": "1.1", "selection_id": 11, "side": "BACK", "price": "2.02",
             "size": "2.00"}]}))
        with self.assertRaisesRegex(ValueError, "recordings_mix_known_and_unknown_gap_windows"):
            paper.run_paper_replay([listed, bare], orders_path, PARAMS, self.root / "outputs" / "mix",
                                   repo_root=self.root / "repo")
        self.assertFalse((self.root / "outputs" / "mix").exists())
        (bare.parent / "receipt.json").write_text(json.dumps({"key_type": "synthetic", "segments": [
            {"path": "seg-009.jsonl", "first_pt": fx.pt_at(-200), "last_pt": fx.pt_at(-200), "market_ids": ["1.1"]}]}))
        report = paper.run_paper_replay([bare], orders_path, PARAMS, self.root / "outputs" / "unlisted",
                                        repo_root=self.root / "repo")
        self.assertIn("recording_gaps_unknown_segment_not_in_receipt", report["limitations"])
        self.assertEqual(report["inputs"]["resolved"]["windows_status"], "unknown_segment_not_in_receipt")

    def test_gap_window_is_per_market_and_same_window(self):
        """Regression R3#8: the gap check asked only whether the activation time fell in ANY segment's window,
        the union over markets and recordings. An order on 1.1 inside a window of 1.2 only, or with a second
        recording passed in --input, or whose activation book was last seen in an earlier window, filled
        against 1.1's last book from before the gap."""
        def order(submit_s, market="1.1"):
            return {"submit_pt": fx.pt_at(submit_s), "market_id": market, "selection_id": 11, "side": "BACK",
                    "price": "2.00", "size": "2.00"}

        # (1) seg-000 carries 1.1 only, seg-001 1.2 only (an explicit-mode refresh dropped 1.1).
        (between, inside), _ = self.gap_case("gap-r1", {
            "seg-000.jsonl": [fx.image(1, fx.pt_at(-300), market_id="1.1"),
                              fx.delta(1, fx.pt_at(-100), [{"id": 11, "ltp": 2.0}], market_id="1.1")],
            "seg-001.jsonl": [fx.image(2, fx.pt_at(-95), market_id="1.2"),
                              fx.delta(2, fx.pt_at(-50), [{"id": 12, "tv": 500}], market_id="1.2")]},
            [order(-98), order(-95)], name="r1",
            receipt=[("seg-000.jsonl", fx.pt_at(-300), fx.pt_at(-100), ["1.1"]),
                     ("seg-001.jsonl", fx.pt_at(-95), fx.pt_at(-50), ["1.2"])])
        for result in (between, inside):
            self.assertEqual((result["final_state"], result["refusal"], result["size_matched_p"]),
                             ("REFUSED", "paper_book_in_recording_gap", 0))
        # (2) recording A (1.1) lost its connection from -100 s to -90 s; recording B (1.2) never did.
        segments_a = {"seg-000.jsonl": [fx.image(1, fx.pt_at(-300), market_id="1.1"),
                                        fx.delta(1, fx.pt_at(-100), [{"id": 11, "ltp": 2.0}], market_id="1.1")],
                      "seg-001.jsonl": [fx.image(1, fx.pt_at(-90), market_id="1.1"),
                                        fx.delta(1, fx.pt_at(-50), [{"id": 11, "ltp": 2.0}], market_id="1.1")]}
        receipt_a = [("seg-000.jsonl", fx.pt_at(-300), fx.pt_at(-100), ["1.1"]),
                     ("seg-001.jsonl", fx.pt_at(-90), fx.pt_at(-50), ["1.1"])]
        (alone,), _ = self.gap_case("gap-a", segments_a, [order(-96)], name="a-alone", receipt=receipt_a)
        b_path = self.write_segment("seg-000.jsonl", directory="gap-b", messages=[
            fx.image(1, fx.pt_at(-300), market_id="1.2"),
            fx.delta(1, fx.pt_at(-50), [{"id": 12, "tv": 1}], market_id="1.2")])
        (b_path.parent / "receipt.json").write_text(json.dumps({"key_type": "synthetic", "segments": [
            {"path": "seg-000.jsonl", "first_pt": fx.pt_at(-300), "last_pt": fx.pt_at(-50), "market_ids": ["1.2"]}]}))
        a_paths = [self.root / "gap-a" / name for name in segments_a]
        (with_b,), _ = self.gap_case("gap-a", {}, [order(-96)], name="a-with-b", receipt=None,
                                     inputs=a_paths + [b_path])
        for result in (alone, with_b):
            self.assertEqual((result["final_state"], result["refusal"]), ("REFUSED", "paper_book_in_recording_gap"))
        # (3) 1.1 is listed in seg-001 too, but its first book there comes at -95 s: an order active at -97 s
        # would use the -100 s book of seg-000 (3 s old, inside max_snapshot_age_s), never observed after -100 s.
        (late,), _ = self.gap_case("gap-c", {
            "seg-000.jsonl": [fx.image(1, fx.pt_at(-300), market_id="1.1"),
                              fx.delta(1, fx.pt_at(-100), [{"id": 11, "ltp": 2.0}], market_id="1.1")],
            "seg-001.jsonl": [fx.image(2, fx.pt_at(-99), market_id="1.2"),
                              fx.mcm(2, fx.pt_at(-95), [{"id": "1.1", "img": True, "rc": fx.runners_adv(),
                                                         "marketDefinition": fx.market_definition()}])]},
            [order(-97.5)], name="c",
            receipt=[("seg-000.jsonl", fx.pt_at(-300), fx.pt_at(-100), ["1.1"]),
                     ("seg-001.jsonl", fx.pt_at(-99), fx.pt_at(-95), ["1.1", "1.2"])])
        self.assertEqual((late["activation_book_pt"], late["final_state"], late["refusal"]),
                         (fx.pt_at(-100), "REFUSED", "paper_book_in_recording_gap"))
        windows = [(fx.pt_at(-300), fx.pt_at(-100), ("1.1",)), (fx.pt_at(-99), fx.pt_at(-95), ("1.1", "1.2"))]
        self.assertTrue(paper._same_window(windows, "1.1", fx.pt_at(-97), fx.pt_at(-96)))
        self.assertFalse(paper._same_window(windows, "1.1", fx.pt_at(-100), fx.pt_at(-97)))
        self.assertFalse(paper._same_window(windows, "1.3", fx.pt_at(-200)))

    def astra_503_case(self, name, messages):
        """Astra C3: image at t0 (back 2.00 / lay 2.04, 100.00 traded at 2.02), a resting BACK 2.00 @ 2.02 with
        zero latency (queue 0), then 4.00 more traded at 2.02 at t0 + 2 s: 2.00 fills unless something between
        makes the interval unobserved."""
        t0 = fx.pt_at(-100)
        seg = self.write_segment("seg-000.jsonl", directory=f"astra-{name}", messages=[
            fx.image(1, t0, rc=[{"id": 11, "batb": [[0, 2.0, 10]], "batl": [[0, 2.04, 10]], "trd": [[2.02, 100.0]]}],
                     extra_markets=[("1.2", fx.market_definition(), fx.runners_adv())]),
            *messages(t0),
            fx.delta(1, t0 + 2000, [{"id": 11, "trd": [[2.02, 104.0]]}])])
        orders_path = self.root / f"orders-astra-{name}.json"
        orders_path.write_text(json.dumps({"schema_version": 1, "orders": [
            {"submit_pt": t0, "market_id": "1.1", "selection_id": 11, "side": "BACK", "price": "2.02",
             "size": "2.00"}]}))
        report = paper.run_paper_replay([seg], orders_path, dict(PARAMS, submit_latency_ms=0),
                                        self.root / "outputs" / f"astra-{name}", repo_root=self.root / "repo",
                                        key_type="synthetic")
        return report["simulated_execution"]["orders"][0]

    def test_no_passive_fill_across_a_503_without_mc(self):
        """Regression C3 (Astra): a 503 heartbeat with no mc (or a 503 that changes another market only) yielded
        nothing for this market, so the healthy delta after it looked like contiguous history and 2.00 filled
        from volume traded while the stream was flagged stale. Every cached market is now unusable from the
        503 to the next healthy message, so no fill interval spans it."""
        control = self.astra_503_case("control", lambda t0: [])
        self.assertEqual((control["size_matched_p"], "passive_fill" in control["flags"]), (200, True))
        cases = {"no-mc": lambda t0: [dict(fx.heartbeat(1, t0 + 1000), status=503)],
                 "other-market": lambda t0: [dict(fx.delta(1, t0 + 1000, [{"id": 12, "ltp": 2.1}], market_id="1.2"),
                                                  status=503)]}
        for name, messages in cases.items():
            with self.subTest(case=name):
                result = self.astra_503_case(name, messages)
                self.assertEqual((result["final_state"], result["size_matched_p"]), ("EXECUTABLE", 0))
                self.assertNotIn("passive_fill", result["flags"])
                self.assertIn("book_invalid_no_fills", result["flags"])

    def test_provenance_hashes_receipts_sidecars_and_assumptions(self):
        """Regression C4 (Astra): the ledger's source_sha256 hashed only the recordings and the orders, yet the
        receipt beside a recording decides the gap guard: one recording, one orders file, one code and params
        gave a 2.00 fill without the receipt and a refusal with it, under identical source and config hashes."""
        t0 = fx.pt_at(-100)
        seg = self.write_segment("seg-000.jsonl", directory="prov", messages=[
            fx.image(1, t0, rc=[{"id": 11, "batb": [[0, 2.0, 30], [1, 1.98, 99]], "batl": [[0, 2.02, 10]], "tv": 100.0,
                                 "ltp": 2.0, "trd": [[2.0, 100.0]]}]),
            fx.delta(1, t0 + 2000, [{"id": 11, "ltp": 2.0}])])
        orders_path = self.root / "orders-prov.json"
        orders_path.write_text(json.dumps({"schema_version": 1, "orders": [
            {"submit_pt": t0 + 2500, "market_id": "1.1", "selection_id": 11, "side": "BACK", "price": "2.00",
             "size": "2.00"}]}))
        receipt = seg.parent / "receipt.json"

        def run(name):
            report = paper.run_paper_replay([seg], orders_path, PARAMS, self.root / "outputs" / name,
                                            repo_root=self.root / "repo", key_type="synthetic")
            ledger = (self.root / "outputs" / "run-ledger.jsonl").read_text().splitlines()
            return report, strict_json_loads(ledger[-1])

        plain, plain_row = run("prov-a")
        receipt.write_text(json.dumps({"key_type": "synthetic", "segments": [
            {"path": "seg-000.jsonl", "first_pt": t0, "last_pt": t0 + 2000}]}))           # Astra's receipt, verbatim
        bounded, bounded_row = run("prov-b")
        self.assertEqual(plain["simulated_execution"]["orders"][0]["size_matched_p"], 200)
        self.assertEqual(bounded["simulated_execution"]["orders"][0]["refusal"], "paper_book_in_recording_gap")
        self.assertEqual(plain_row["config_sha256"], bounded_row["config_sha256"])
        self.assertNotEqual(plain_row["source_sha256"], bounded_row["source_sha256"])
        self.assertEqual(plain["inputs"]["dependencies"], [{
            "recording": "seg-000.jsonl", "receipt_json": {"present": False, "sha256": None},
            "receipt_partial_json": {"present": False, "sha256": None}, "receipt_used": None,
            "recv_sidecar": {"name": "seg-000.recv.jsonl", "present": False, "sha256": None}}])
        self.assertEqual(plain["inputs"]["resolved"], {"key_type": "synthetic", "key_type_source": "explicit",
                                                       "windows": None, "windows_status": "unknown_no_receipt"})
        used = bounded["inputs"]["dependencies"][0]
        self.assertEqual((used["receipt_json"], used["receipt_used"]),
                         ({"present": True, "sha256": file_sha256(receipt)}, "receipt.json"))
        # Astra's receipt lists no market_ids: its window covers no market.
        self.assertEqual(bounded["inputs"]["resolved"]["windows"], [[t0, t0 + 2000, []]])
        # Each consumed file moves the hash on its own: the market list, a receipt.partial.json, the sidecar.
        rows = [bounded_row]
        receipt.write_text(json.dumps({"key_type": "synthetic", "segments": [
            {"path": "seg-000.jsonl", "first_pt": t0, "last_pt": t0 + 2000, "market_ids": ["1.1"]}]}))
        rows.append(run("prov-c")[1])
        (seg.parent / "receipt.partial.json").write_text(json.dumps({"key_type": "synthetic", "segments": []}))
        rows.append(run("prov-d")[1])
        sidecar = [{"line": n, "pt": pt, "received_at": utc_from_ns(pt * 1_000_000), "monotonic_ns": n,
                    "byte_offset": 0, "bytes": 0, "sha256": "0" * 64, "clk": None}
                   for n, pt in enumerate((t0, t0 + 2000), 1)]
        (seg.parent / "seg-000.recv.jsonl").write_text("".join(json.dumps(row) + "\n" for row in sidecar))
        last, last_row = run("prov-e")
        rows.append(last_row)
        self.assertEqual(len({row["source_sha256"] for row in rows}), 4)
        dependency = last["inputs"]["dependencies"][0]
        self.assertEqual((dependency["receipt_partial_json"]["present"], dependency["receipt_used"],
                          dependency["recv_sidecar"]),
                         (True, "receipt.json", {"name": "seg-000.recv.jsonl", "present": True,
                                                 "sha256": file_sha256(seg.parent / "seg-000.recv.jsonl")}))

    def test_change_under_a_503_refuses_orders_in_and_after_it(self):
        """Regression (audit): an in-play transition carried by a status-503 message was never yielded when
        the market did not change again, so an order activated after it filled against the pre-play book."""
        seg = self.write_segment("seg-000.jsonl", directory="stream-503", messages=[
            fx.image(1, fx.pt_at(-300)), fx.delta(1, fx.pt_at(-101), [{"id": 11, "ltp": 2.0}]),
            fx.mcm(1, fx.pt_at(-100), [{"id": "1.1", "marketDefinition": fx.market_definition(inPlay=True,
                                                                                              version=2)}],
                   status=503),
            fx.heartbeat(1, fx.pt_at(-99)), fx.delta(1, fx.pt_at(-50), [{"id": 12, "tv": 500}])])
        orders_path = self.root / "orders-503.json"
        orders_path.write_text(json.dumps({"schema_version": 1, "orders": [
            {"submit_pt": fx.pt_at(-99.9), "market_id": "1.1", "selection_id": 11, "side": "BACK", "price": "2.00",
             "size": "2.00"},
            {"submit_pt": fx.pt_at(-98), "market_id": "1.1", "selection_id": 11, "side": "BACK", "price": "2.00",
             "size": "2.00"}]}))
        report = paper.run_paper_replay([seg], orders_path, PARAMS, self.root / "outputs" / "run-503",
                                        repo_root=self.root / "repo", key_type="synthetic")
        during, after = report["simulated_execution"]["orders"]
        self.assertEqual((during["size_matched_p"], during["refusal"]), (0, "paper_book_invalid"))
        self.assertEqual((after["size_matched_p"], after["refusal"]), (0, "paper_rejected_market_state"))

    def test_divisor_limitation_follows_the_configured_divisor(self):
        """Regression (audit): the report always claimed divisor 2 (the PRO basis), whatever was configured."""
        for divisor, below in ((1, True), (5, False)):
            report = paper.run_paper_replay(
                [self.recording], self.orders_path, dict(PARAMS, traded_volume_divisor=divisor),
                self.root / "outputs" / f"run-d{divisor}", repo_root=self.root / "repo", snapshot_reader=jsonl_reader)
            text = " ".join(report["limitations"])
            self.assertIn(f"Traded volume divisor {divisor} ", text)
            self.assertNotIn("Traded volume divisor 2 follows", text)
            self.assertEqual("traded_volume_divisor_below_2_less_conservative_than_pro_basis" in report["limitations"],
                             below)

    @unittest.skipUnless(importlib.util.find_spec("betangel_research.betfair.book"),
                         "betfair.book (U3) not present yet; the injected reader covers the replay")
    def test_replay_through_real_recording_reader(self):
        from betangel_research.temporal import timestamp_ms
        t0 = timestamp_ms("2020-01-01T12:00:00Z")
        definition = {"marketTime": "2020-01-01T12:20:00.000Z", "status": "OPEN", "inPlay": False, "betDelay": 0,
                      "bettingType": "ODDS", "marketType": "WIN", "eventTypeId": "7", "numberOfWinners": 1,
                      "version": 1, "priceLadderDefinition": {"type": "CLASSIC"},
                      "runners": [{"id": 12345, "status": "ACTIVE"}]}
        lines = [{"op": "mcm", "pt": t0, "mc": [{"id": "1.1", "img": True, "marketDefinition": definition,
                                                 "rc": [{"id": 12345, "batb": [[0, 3.5, 10.0]],
                                                         "batl": [[0, 3.55, 10.0]]}]}]}]
        segment = self.root / "rec" / "seg-real.jsonl"
        segment.write_text("".join(json.dumps(line) + "\n" for line in lines))
        report = paper.run_paper_replay([segment], self.orders_path, PARAMS, self.root / "outputs" / "real",
                                        repo_root=self.root / "repo", key_type="historical")
        self.assertEqual(report["status"], "paper_simulation_not_execution_evidence")
        first = report["simulated_execution"]["orders"][0]
        self.assertEqual((first["activation_book_pt"], first["size_matched_p"]), (t0, 200))
        self.assertEqual(report["simulated_execution"]["markets"]["1.1"], {"status": "not_closed_in_recording"})


if __name__ == "__main__":
    unittest.main()
