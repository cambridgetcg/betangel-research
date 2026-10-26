"""P1-10 S3a (Astra #2): OrderJournal.append(..., not_before_ns) checks the stamp actually inserted.

A frontier note whose trigger time F lies past the journal clock J replays clean only when max(J, stamp) >= F (rule
R-F2, design v4 S2c), so its writer passes F as not_before_ns, and the journal refuses the append
(journal_append_too_early, nothing written) when the event's recorded_at is below it. recorded_at keeps microseconds,
so the check is on that text, never on the wall clock. It is fixed before BEGIN IMMEDIATE, so the refusal is made
before the write lock is taken. journal.py is shared with the gateway: without the argument nothing changes, which the
journal and gateway test files show by passing unchanged. Synthetic: no writer passes not_before_ns yet (S3b).
"""

from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from betangel_research.betfair import journal as journal_module, pilot, pilot_run
from betangel_research.betfair.errors import JournalError
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.receipts import parse_utc_ns

import test_betfair_support as support

S = pilot.NS
CONTRACT = SimpleNamespace(sha256="c" * 64)          # a stop payload reads only the contract's digest


def stop_note():
    return {"pilot": pilot.stop_payload(CONTRACT, "manual-check-4", "review", "manual_check")}


class SteppingClock(support.FakeClock):
    """A FakeClock whose wall moves by step_ns after every wall reading (utc_now or time_ns), so no two agree."""

    step_ns = 0

    def time_ns(self):
        value = super().time_ns()
        self.wall_ns += self.step_ns
        return value

    def utc_now(self):
        value = super().utc_now()
        self.wall_ns += self.step_ns
        return value


class NotBeforeTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "orders" / "journal-live.sqlite"
        self.clock = support.FakeClock()

    def open(self, **options):
        journal = OrderJournal(self.path, mode="live", clock=self.clock, **options)
        self.addCleanup(journal.close)
        return journal

    def stamp(self):
        """The recorded_at the next append inserts, in ns: the wall clock floored to the microsecond."""
        return parse_utc_ns(self.clock.utc_now())

    def seen(self, journal):
        """What a refusal must leave as it was: the head (this connection's and a fresh one's), the rows, the fold
        cache."""
        return journal.anchor(), journal.head_fresh(), list(journal.events()), journal._fold_cache

    def assert_unchanged(self, journal, before):
        anchor, fresh, rows, cache = before
        self.assertEqual((journal.anchor(), journal.head_fresh(), list(journal.events())), (anchor, fresh, rows))
        self.assertIs(journal._fold_cache, cache)

    def test_a_stamp_below_the_bound_is_refused_and_nothing_changes(self):
        """The head, the rows and the fold cache are as they were, and the connection appends and folds as before."""
        journal = self.open()
        journal.append("note", {"reason": "first"})
        before = self.seen(journal)
        self.assertIsNotNone(before[3])
        for bound in (self.stamp() + 1, self.stamp() + 1000, self.stamp() + 3600 * S):
            with self.subTest(bound=bound), self.assertRaisesRegex(JournalError, "^journal_append_too_early$"):
                journal.append("note", {"reason": "early"}, not_before_ns=bound)
            self.assert_unchanged(journal, before)
        journal.append("note", {"reason": "next"})
        self.assertEqual([e["payload"]["reason"] for e in journal.events()], ["first", "next"])
        self.assertEqual(journal.verify()["status"], "ok")
        now = self.clock.time_ns()
        self.assertEqual(journal.fold(now_ns=now),
                         journal_module.fold_events(list(journal.events()), mode="live", now_ns=now))

    def test_a_stamp_at_or_above_the_bound_is_written(self):
        """Equality is written: R-F2 is max(J, stamp) >= F, so a stamp equal to F already reaches it."""
        journal = self.open()
        stamp = self.stamp()
        for bound in (stamp, stamp - 1, stamp - 3600 * S):
            with self.subTest(bound=bound):
                event = journal.append("note", {"reason": "in_time"}, not_before_ns=bound)
                self.assertEqual(parse_utc_ns(event["recorded_at"]), stamp)
        self.assertEqual([e["seq"] for e in journal.events()], [1, 2, 3])
        self.assertEqual(journal.verify()["status"], "ok")

    def test_the_inserted_stamp_is_checked_not_the_wall_clock(self):
        """At wall deadline + 500 ns the wall has passed deadline + 1, but the stamp keeps microseconds and reads the
        deadline itself, which the fold would see below the trigger: refused. From deadline + 1 µs it is written."""
        journal = self.open()
        deadline = self.stamp() + 60 * S
        self.clock.wall_ns = deadline + 500
        self.assertGreaterEqual(self.clock.time_ns(), deadline + 1)
        with self.assertRaisesRegex(JournalError, "^journal_append_too_early$"):
            journal.append("note", {"reason": "deadline"}, not_before_ns=deadline + 1)
        self.assertEqual((journal.anchor(), list(journal.events())), ({}, []))
        self.clock.wall_ns = deadline + 1000
        event = journal.append("note", {"reason": "deadline"}, not_before_ns=deadline + 1)
        self.assertEqual(parse_utc_ns(event["recorded_at"]), deadline + 1000)

    def test_only_the_stamp_inserted_decides_when_the_wall_moves_between_readings(self):
        """No other reading equals the stamp here. Moving forward, the stamp is below the bound and every later
        reading past it: refused. Moving back, the stamp is at the bound and every later reading below it: written,
        and the stamp inserted is at the bound. A check on a later reading gets both wrong; one on an earlier
        reading lets the stamp that moves back land below the bound."""
        self.clock = SteppingClock()
        journal = self.open()
        self.clock.step_ns = 1000
        with self.assertRaisesRegex(JournalError, "^journal_append_too_early$"):
            journal.append("note", {"reason": "forward"}, not_before_ns=self.clock.wall_ns + 1)
        self.assertEqual((journal.anchor(), list(journal.events())), ({}, []))
        self.clock.step_ns = -1000
        bound = self.clock.wall_ns
        event = journal.append("note", {"reason": "back"}, not_before_ns=bound)
        self.assertEqual(parse_utc_ns(event["recorded_at"]), bound)

    def test_a_bound_that_is_not_an_int_is_refused(self):
        """type() is checked, as for before_ns, so a bool (an int subclass) is refused too; nothing is written."""
        journal = self.open()
        journal.append("note", {"reason": "first"})
        before = self.seen(journal)
        for bound in (True, False, 1.5, float(self.stamp()), str(self.stamp())):
            with self.subTest(bound=bound), self.assertRaisesRegex(JournalError, "^not_before_ns_invalid$"):
                journal.append("note", {"reason": "typed"}, not_before_ns=bound)
            self.assert_unchanged(journal, before)

    def test_the_refusal_is_made_before_the_write_lock(self):
        """recorded_at is fixed before BEGIN IMMEDIATE, so a refusal that is already certain never waits for another
        writer: while another connection holds the write lock, the early append is refused at once, and one the bound
        admits waits for the lock and fails on it."""
        journal = self.open(busy_timeout_s=0.2)
        journal.append("note", {"reason": "first"})
        before = self.seen(journal)
        other = sqlite3.connect(str(self.path), isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaisesRegex(JournalError, "^journal_append_too_early$"):
                journal.append("note", {"reason": "early"}, not_before_ns=self.stamp() + 1)
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                journal.append("note", {"reason": "admitted"}, not_before_ns=self.stamp())
        finally:
            other.execute("ROLLBACK")
        self.assert_unchanged(journal, before)
        journal.append("note", {"reason": "next"})
        self.assertEqual(journal.verify()["status"], "ok")


class AppendPilotTests(unittest.TestCase):
    def test_not_before_ns_reaches_the_journal_only_when_set(self):
        """append_pilot passes it as it passes before_ns, only when it is not None, so a journal stand-in whose append
        takes no such argument is called exactly as before. A falsy value is still passed: 0 is a bound, and False must
        reach the journal's type check rather than be dropped (Astra, S3a review)."""
        calls = []

        class Recording:
            def append(self, kind, payload, **options):
                calls.append((kind, options))

        pilot_run.append_pilot(Recording(), stop_note())
        pilot_run.append_pilot(Recording(), stop_note(), not_before_ns=None)
        pilot_run.append_pilot(Recording(), stop_note(), not_before_ns=7)
        pilot_run.append_pilot(Recording(), stop_note(), not_before_ns=0)
        pilot_run.append_pilot(Recording(), stop_note(), not_before_ns=False)
        self.assertEqual(calls, [("note", {}), ("note", {}), ("note", {"not_before_ns": 7}),
                                 ("note", {"not_before_ns": 0}), ("note", {"not_before_ns": False})])

    def test_a_pilot_note_below_its_bound_is_refused(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        clock = support.FakeClock()
        journal = OrderJournal(Path(temp.name) / "journal-live.sqlite", mode="live", clock=clock)
        self.addCleanup(journal.close)
        stamp = parse_utc_ns(clock.utc_now())
        with self.assertRaisesRegex(JournalError, "^journal_append_too_early$"):
            pilot_run.append_pilot(journal, stop_note(), not_before_ns=stamp + 1)
        self.assertEqual(list(journal.events()), [])
        with self.assertRaisesRegex(JournalError, "^not_before_ns_invalid$"):
            pilot_run.append_pilot(journal, stop_note(), not_before_ns=False)
        self.assertEqual(list(journal.events()), [])
        event = pilot_run.append_pilot(journal, stop_note(), not_before_ns=stamp)
        self.assertEqual((event["seq"], parse_utc_ns(event["recorded_at"])), (1, stamp))


if __name__ == "__main__":
    unittest.main()
