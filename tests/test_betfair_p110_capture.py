"""P1-10 S1: L is read for every bet whose market a capture showed CLOSED, including a CLOSED read captured after
off + 24 h, which never sets the BSP's closed_read (pilot_run.capture_entries used closed_markets_of(state)).

P1-10 S1b: the CLOSED book is read for every PLACE record's market, sent or not, so the decision-price series
(contract reporting) has each record's BSP. A record with no bet id gets a book entry once its off has passed, until
no read can change it: off + the BSP cutoff for an unsent record, its deadline for a sent one (a CLOSED read up to then
still decides void or removal; Astra S1b-1, Codex review on PR #40). The pending-captures line shows counts, never a
bet id or market id (note §12:185)."""
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from betangel_research.betfair import pilot, pilot_run

import test_betfair_pilot as tp

NS = pilot.NS
CUTOFF_S = 24 * 3600
DEADLINE_S = 7 * 86400
OFF = 100 * NS


def record(**changes):
    rec = dict(ref="ref-1", market_id="1.5", selection_id=7, session_id="s1", off_ns=OFF, unsent=False, l_rows=[],
               bsp={"decided": True, "closed_read": None}, deadline_ns=OFF + DEADLINE_S * NS)
    rec.update(changes)
    return SimpleNamespace(**rec)


def projection(*recs, closed_markets, orders=None, now_ns=OFF + 60 * NS):
    """recs default to one sent bet with bet id 999; orders maps ref -> order (None leaves the ref out)."""
    if orders is None:
        orders = {rec.ref: {"bet_id": "999"} for rec in recs}
    contract = SimpleNamespace(bsp_cutoff_s=CUTOFF_S)
    return {"state": SimpleNamespace(place_records=list(recs), now_ns=now_ns, contract=contract),
            "core": {"orders": {ref: order for ref, order in orders.items() if order is not None}},
            "pending_capture": [], "closed_markets": closed_markets}


def kinds(entries):
    return [kind for kind, _ in entries]


def closed_book(market_id="1.5", status="CLOSED"):
    return {"kind": "note", "market_id": market_id,
            "payload": {"pilot": {"type": "capture_book", "status": status}}}


class LGatingTests(unittest.TestCase):
    def test_a_late_closed_read_queues_l_and_no_book(self):
        """The BSP was decided "none" (CLOSED read after the cutoff), so closed_read is unset; a capture still
        showed the market CLOSED, so L is read and the book is not re-read on every run."""
        self.assertEqual(kinds(pilot_run.capture_entries(projection(record(), closed_markets={"1.5"}))), ["l"])

    def test_no_closed_capture_reads_the_book(self):
        self.assertEqual(kinds(pilot_run.capture_entries(projection(record(), closed_markets=set()))), ["book"])

    def test_a_settled_l_row_is_not_read_again(self):
        rec = record(l_rows=[{"row": {"betStatus": "SETTLED"}}])
        self.assertEqual(pilot_run.capture_entries(projection(rec, closed_markets={"1.5"})), [])

    def test_the_projection_carries_every_closed_capture(self):
        events = [closed_book("1.5"), closed_book("1.6", status="OPEN"), closed_book("1.7")]
        state = SimpleNamespace(due_stops=[], place_records=[])
        with patch.object(pilot_run.pilot, "fold", return_value=state), \
                patch.object(pilot_run, "fold_events", return_value={"orders": {}}), \
                patch.object(pilot_run.pilot, "session_armed", return_value=False):
            out = pilot_run.recovery_projection(events, contract=None, now_ns=0)
        self.assertEqual(out["closed_markets"], {"1.5", "1.7"})


class EveryPlaceRecordBookTests(unittest.TestCase):
    """S1b: which book entries a record with no usable bet id gets."""

    def test_an_unsent_record_after_its_off_gets_one_book_and_never_c_or_l(self):
        rec = record(unsent=True)
        for orders in ({rec.ref: None}, {rec.ref: {"bet_id": None}}):
            with self.subTest(orders=orders):
                entries = pilot_run.capture_entries(projection(rec, closed_markets=set(), orders=orders))
                self.assertEqual(entries, [("book", {"ref": "ref-1", "bet_id": None, "market_id": "1.5",
                                                     "selection_id": 7, "off_ns": OFF})])
                self.assertEqual(pilot_run.capture_entries(projection(rec, closed_markets={"1.5"}, orders=orders)),
                                 [])

    def test_no_book_before_the_off_or_after_the_cutoff(self):
        rec = record(unsent=True)
        for now, expected in ((OFF - NS, []), (OFF, []), (OFF + 1, ["book"]),
                              (OFF + CUTOFF_S * NS, ["book"]), (OFF + CUTOFF_S * NS + 1, [])):
            with self.subTest(now=now - OFF):
                out = pilot_run.capture_entries(projection(rec, closed_markets=set(), orders={rec.ref: None},
                                                           now_ns=now))
                self.assertEqual(kinds(out), expected)

    def test_a_sent_record_with_no_bet_id_is_read_until_its_deadline(self):
        """Astra S1b-1: after the cutoff a CLOSED read still decides void or removal for a sent record; after its
        deadline the record is final and nothing is read (Codex review, PR #40)."""
        rec = record()
        for now, expected in ((OFF - NS, []), (OFF + NS, ["book"]), (OFF + CUTOFF_S * NS + 1, ["book"]),
                              (OFF + DEADLINE_S * NS, ["book"]), (OFF + DEADLINE_S * NS + 1, []),
                              (OFF + 8 * 86400 * NS, [])):
            with self.subTest(now=now - OFF):
                out = pilot_run.capture_entries(projection(rec, closed_markets=set(),
                                                           orders={rec.ref: {"bet_id": None}}, now_ns=now))
                self.assertEqual(kinds(out), expected)

    def test_a_bet_keeps_s1s_rule(self):
        """A record with a bet id is read as before: its book before the off too, and after the cutoff."""
        for now in (OFF - NS, OFF + CUTOFF_S * NS + 1):
            with self.subTest(now=now - OFF):
                self.assertEqual(kinds(pilot_run.capture_entries(projection(record(), closed_markets=set(),
                                                                            now_ns=now))), ["book"])

    def test_one_book_per_market(self):
        bet, unsent = record(), record(ref="ref-2", unsent=True)
        for recs in ((bet, unsent), (unsent, bet)):
            with self.subTest(first=recs[0].ref):
                out = pilot_run.capture_entries(projection(*recs, closed_markets=set(),
                                                           orders={"ref-1": {"bet_id": "999"}, "ref-2": None}))
                self.assertEqual(kinds(out), ["book"])

    def test_in_a_session_only_that_sessions_markets_after_their_off(self):
        mine = record(ref="ref-1", market_id="1.5", unsent=True)
        other = record(ref="ref-2", market_id="1.6", session_id="s0", unsent=True)
        later = record(ref="ref-3", market_id="1.7", unsent=True, off_ns=OFF + 3600 * NS)
        proj = projection(mine, other, later, closed_markets=set(), orders={"ref-1": None, "ref-2": None,
                                                                            "ref-3": None})
        out = pilot_run.capture_entries(proj, queue={}, session_id="s1", now_ns=OFF + 60 * NS)
        self.assertEqual([(kind, item["market_id"]) for kind, item in out], [("book", "1.5")])
        self.assertEqual(pilot_run.capture_entries(proj, queue={}, session_id="s1", now_ns=None), [])


class SentNoBetIdFold(unittest.TestCase):
    """Why a sent record with no bet id is read past the cutoff and up to its deadline, and not after it: a CLOSED read
    after off + 24 h leaves the BSP "none" but still decides void up to the deadline; after it the class is final."""

    def removed_record(self, read_at=None):
        journal_ = tp.Pilot(pt4=True)
        session = journal_.arm(0)
        p = journal_.place(session)
        journal_.intent(p)
        journal_.submit(p)                                     # sent, never reported: no bet id
        journal_.reserve("1.299999", journal_.now + 300 * tp.S, session=session, open_id="open-later")
        if read_at is not None:
            journal_.closed(p, ours="REMOVED", sp="3.10", at=p.off + read_at)
        rec = journal_.fold(now=p.off + 9 * tp.DAY_NS).place_records[0]
        self.assertFalse(rec.unsent)
        return rec.measurement["class"]

    def test_a_closed_read_up_to_the_deadline_decides_void_and_not_after(self):
        deadline = 7 * tp.DAY_NS
        self.assertEqual({at: self.removed_record(at) for at in (None, 2 * tp.DAY_NS, deadline, deadline + 1000)},
                         {None: "measurement_unresolved", 2 * tp.DAY_NS: "void", deadline: "void",
                          deadline + 1000: "measurement_unresolved"})

    def test_a_late_closed_read_still_decides_void(self):
        outcomes = {}
        for with_read in (False, True):
            journal_ = tp.Pilot(pt4=True)
            session = journal_.arm(0)
            p = journal_.place(session)
            journal_.intent(p)
            journal_.submit(p)                                 # sent, never reported: no bet id
            journal_.reserve("1.299999", journal_.now + 300 * tp.S, session=session, open_id="open-later")
            if with_read:
                journal_.closed(p, ours="REMOVED", sp="3.10", at=p.off + 2 * tp.DAY_NS)
            rec = journal_.fold(now=p.off + 8 * tp.DAY_NS).place_records[0]
            self.assertFalse(rec.unsent)
            outcomes[with_read] = (rec.measurement["class"], rec.bsp["decided"], rec.bsp["bsp"])
        self.assertEqual(outcomes, {False: ("measurement_unresolved", True, None), True: ("void", True, None)})


class PendingLineTests(unittest.TestCase):
    def test_a_mixed_batch_prints_counts_only(self):
        """bet_id None in a book entry once raised TypeError in sorted(); the line now names no id at all."""
        pending = [("c", {"bet_id": "5001", "market_id": "1.5"}), ("book", {"bet_id": None, "market_id": "1.6"}),
                   ("book", {"bet_id": "5002", "market_id": "1.7"}), ("l", {"bet_id": "5003", "market_id": "1.8"})]
        for abnormal, tail in ((None, pilot_run.PENDING_CAPTURE_LINE), ("kill", pilot_run.KILL_CAPTURE_LINE)):
            with self.subTest(abnormal=abnormal):
                line = pilot_run.Run.pending_line(SimpleNamespace(abnormal=abnormal), pending)
                self.assertEqual(line, f"pending captures: c 1, book 2, l 1; {tail}")
                self.assertIsNone(re.search(r"500[123]|1\.[5-8]", line))


class UnsentClosedReadFold(unittest.TestCase):
    """The fold rules S1b feeds (unchanged): an unsent record's CLOSED read within the cutoff decides its BSP and makes
    it final there, instead of at the cutoff."""

    def unsent_with_closed_read(self, contract=tp.C):
        journal_ = tp.Pilot(pt4=True)
        session = journal_.arm(0)
        _, unsent, _ = journal_.classify(session)              # decided PLACE, never submitted
        bet = journal_.place(session)                          # its reservation ends the first open
        journal_.lose(bet)
        return journal_, unsent, bet

    def test_the_closed_read_freezes_bsp_and_finality(self):
        journal_, p, _ = self.unsent_with_closed_read()
        before = journal_.fold().place_records[0]
        self.assertEqual((before.unsent, before.measurement["class"], before.bsp["decided"]),
                         (True, "definitely_unsent", False))
        self.assertFalse(before.measurement["final"])
        read_at = journal_.now + tp.MIN
        seq = journal_.closed(p, sp="3.10", at=read_at)
        for state in (journal_.fold(), tp.replayed(journal_)):
            rec = state.place_records[0]
            self.assertEqual((rec.bsp["decided"], rec.bsp["read_seq"], rec.bsp["decided_at_ns"]), (True, seq, read_at))
            self.assertEqual(rec.measurement, {"class": "definitely_unsent", "final": True, "final_at_ns": read_at})
            self.assertEqual((rec.ledger["value_p"], rec.ledger["reason"]), (0, "not_sent"))
            self.assertEqual(state.due_stops, ())

    def test_n_is_reached_at_the_unsent_records_closed_read(self):
        journal_, p, _ = self.unsent_with_closed_read()
        without = pilot.n_status(journal_.fold(contract=tp.C_ONE), now_ns=journal_.now)
        self.assertFalse(without["reached"])
        read_at = journal_.now + tp.MIN
        journal_.closed(p, sp="3.10", at=read_at)
        state = journal_.fold(contract=tp.C_ONE)
        self.assertEqual({k: pilot.n_status(state, now_ns=journal_.now)[k] for k in ("reached", "at_ns")},
                         {"reached": True, "at_ns": read_at})


if __name__ == "__main__":
    unittest.main()
