"""P1-10 S1: L is read for every bet whose market a capture showed CLOSED, including a CLOSED read captured after
off + 24 h, which never sets the BSP's closed_read (pilot_run.capture_entries used closed_markets_of(state))."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from betangel_research.betfair import pilot_run


def record(**changes):
    rec = dict(ref="ref-1", market_id="1.5", selection_id=7, session_id="s1", off_ns=100, unsent=False, l_rows=[],
               bsp={"decided": True, "closed_read": None})
    rec.update(changes)
    return SimpleNamespace(**rec)


def projection(rec, *, closed_markets):
    return {"state": SimpleNamespace(place_records=[rec]),
            "core": {"orders": {rec.ref: {"bet_id": "999"}}},
            "pending_capture": [], "closed_markets": closed_markets}


def closed_book(market_id="1.5", status="CLOSED"):
    return {"kind": "note", "market_id": market_id,
            "payload": {"pilot": {"type": "capture_book", "status": status}}}


class LGatingTests(unittest.TestCase):
    def test_a_late_closed_read_queues_l_and_no_book(self):
        """The BSP was decided "none" (CLOSED read after the cutoff), so closed_read is unset; a capture still
        showed the market CLOSED, so L is read and the book is not re-read on every run."""
        kinds = [kind for kind, _ in pilot_run.capture_entries(projection(record(), closed_markets={"1.5"}))]
        self.assertEqual(kinds, ["l"])

    def test_no_closed_capture_reads_the_book(self):
        kinds = [kind for kind, _ in pilot_run.capture_entries(projection(record(), closed_markets=set()))]
        self.assertEqual(kinds, ["book"])

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


if __name__ == "__main__":
    unittest.main()
