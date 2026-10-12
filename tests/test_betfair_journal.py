"""Append-only, hash-chained SQLite journal tests on synthetic events in temporary directories."""

import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from betangel_research.betfair import journal as journal_module
from betangel_research.betfair.errors import JournalError, SecretLeak
from betangel_research.betfair.journal import OrderJournal, fold_events
from betangel_research.betfair.receipts import canonical_sha256, parse_utc_ns
from betangel_research.betfair.secret import Secret

import test_betfair_support as support
import test_betfair_support_orders as orders

REF2 = "bro200101120000-00000000000000a2"


class JournalTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.path = self.dir / "orders" / "journal-paper.sqlite"
        self.clock = support.FakeClock()

    def open(self, mode="paper", path=None):
        journal = OrderJournal(path or self.path, mode=mode, clock=self.clock)
        self.addCleanup(journal.close)
        return journal

    def place(self, journal, ref=orders.REF, *, intent_id="intent-1", market_id=support.MARKET_ID, **order):
        journal.append("intent", orders.intent_payload(orders.instruction(**order)), intent_id=intent_id,
                       customer_order_ref=ref, customer_ref=orders.CREF, market_id=market_id, state_after="RECORDED")
        journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                  "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                       intent_id=intent_id, market_id=market_id)

    def raw(self):
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        self.addCleanup(connection.close)
        return connection

    def test_update_delete_blocked(self):
        journal = self.open()
        self.place(journal)
        raw = self.raw()
        for statement in ("UPDATE events SET kind='note'", "DELETE FROM events", "UPDATE events SET payload_json='{}'",
                          "UPDATE journal_meta SET value='live' WHERE key='mode'", "DELETE FROM journal_meta"):
            with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                raw.execute(statement)
        self.assertEqual(journal.verify()["status"], "ok")
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    def test_meta_append_only(self):
        journal = self.open()
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw().execute("INSERT INTO journal_meta (key, value) VALUES ('mode', 'live')")
        self.assertEqual(journal.meta()["mode"], "paper")
        self.assertEqual(journal.meta()["schema_version"], "1")
        self.assertEqual(journal.meta()["created_at"], self.clock.utc_now())

    def test_hash_chain_verify_and_tamper_detect(self):
        journal = self.open()
        self.place(journal)
        journal.append("note", {"text": "synthetic"})
        events = list(journal.events())
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertEqual(events[0]["prev_sha256"], "0" * 64)
        for previous, current in zip(events, events[1:]):
            self.assertEqual(current["prev_sha256"], previous["event_sha256"])
        for event in events:
            body = {k: v for k, v in event.items() if k != "event_sha256"}
            self.assertEqual(event["event_sha256"], canonical_sha256(body))
        self.assertEqual(journal.verify(), {"status": "ok", "events": 3, "first_bad_seq": None})
        self.assertEqual([e["seq"] for e in journal.events(since_seq=2)], [3])
        raw = self.raw()
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET payload_json='{\"text\":\"edited\"}' WHERE seq=3")
        self.assertEqual(journal.verify(), {"status": "broken", "events": 3, "first_bad_seq": 3})
        raw.execute("UPDATE events SET payload_json='{\"text\":\"synthetic\"}' WHERE seq=3")
        self.assertEqual(journal.verify()["status"], "ok")
        raw.execute("UPDATE events SET payload_json='{\"text\": \"synthetic\"}' WHERE seq=3")     # non-canonical
        self.assertEqual(journal.verify()["first_bad_seq"], 3)
        raw.execute("UPDATE events SET payload_json='{\"text\":\"synthetic\"}' WHERE seq=3")
        raw.execute("UPDATE events SET prev_sha256='%s' WHERE seq=2" % ("b" * 64))
        self.assertEqual(journal.verify()["first_bad_seq"], 2)
        raw.execute("UPDATE events SET prev_sha256=? WHERE seq=2", (events[0]["event_sha256"],))
        raw.execute("DROP TRIGGER events_no_delete")
        raw.execute("DELETE FROM events WHERE seq=2")
        self.assertEqual(journal.verify()["first_bad_seq"], 3)

    def test_begin_immediate_two_connections_no_fork(self):
        first, second = self.open(), self.open()
        errors = []

        def writer(label):
            # sqlite3 connections are bound to their creating thread, so each thread opens its own.
            journal = OrderJournal(self.path, mode="paper", clock=self.clock)
            try:
                for n in range(15):
                    journal.append("note", {"writer": label, "n": n})
            except Exception as exc:          # recorded, then asserted below
                errors.append(exc)
            finally:
                journal.close()

        threads = [threading.Thread(target=writer, args=(label,)) for label in ("a", "b")]
        for thread in threads:
            thread.start()
        for n in range(5):
            first.append("note", {"writer": "main-1", "n": n})
            second.append("note", {"writer": "main-2", "n": n})
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        result = first.verify()
        self.assertEqual(result, {"status": "ok", "events": 40, "first_bad_seq": None})
        self.assertEqual([e["seq"] for e in second.events()], list(range(1, 41)))

    def test_concurrent_appends_from_two_connections_keep_one_chain(self):
        """Amendment A3, D4, verified rather than assumed: the lock-free kill appends from its own connection
        while a lock holder appends order moves from another. append() runs BEGIN IMMEDIATE, reads the head
        and folds inside that write transaction, so the two never fork the chain. Also when one side is a
        fresh connection whose first append folds a journal of several thousand events (it may wait up to
        the 30 s busy timeout for the other's commit; nothing on the kill path depends on that latency)."""
        path = self.dir / "orders" / "journal-live.sqlite"
        errors = []

        def holder(count, offset):
            journal = OrderJournal(path, mode="live", clock=self.clock)
            try:
                for n in range(count):
                    ref = f"bro200101120000-{offset + n:016x}"
                    intent_id = f"int-{offset + n}"
                    journal.append("intent", orders.intent_payload(orders.instruction()), intent_id=intent_id,
                                   customer_order_ref=ref, customer_ref=f"brq200101120000-{offset + n:016x}",
                                   market_id=support.MARKET_ID, state_after="RECORDED")
                    journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                              "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                                   intent_id=intent_id, market_id=support.MARKET_ID)
                    journal.append("report", {"action": "place", "transitions": [
                        {"customer_order_ref": ref, "state": "LAPSED", "fields": {
                            "bet_id": str(300000000000 + offset + n), "size_remaining_p": 0, "size_lapsed_p": 200}}]},
                                   intent_id=intent_id, market_id=support.MARKET_ID)
            except Exception as exc:          # recorded, then asserted below
                errors.append(exc)
            finally:
                journal.close()

        def kill_like(count):
            journal = OrderJournal(path, mode="live", clock=self.clock)
            try:
                journal.append("kill", {"reason": "operator kill", "scope": "own"})
                journal.append("halt", {"reason": "kill", "scope": "global"})
                for n in range(count):
                    journal.append("note", {"reason": f"kill_sweep_{n}"})
            except Exception as exc:
                errors.append(exc)
            finally:
                journal.close()

        def race(*targets):
            threads = [threading.Thread(target=target, args=args) for target, args in targets]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(120)

        OrderJournal(path, mode="live", clock=self.clock).close()
        race((holder, (10, 0)), (kill_like, (12,)))
        self.assertEqual(errors, [])
        journal = self.open(mode="live", path=path)
        self.assertEqual(journal.verify(), {"status": "ok", "events": 44, "first_bad_seq": None})
        self.assertEqual([e["seq"] for e in journal.events()], list(range(1, 45)))
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(len(state["orders"]), 10)
        self.assertTrue(state["halted"])
        # A journal of several thousand events: each racer's first append folds all of it inside its write.
        head = journal.anchor()
        rows, previous = [], head["last_sha256"]
        for seq in range(head["seq"] + 1, head["seq"] + 4001):
            event = {"seq": seq, "event_id": f"ev-{seq:032x}", "recorded_at": self.clock.utc_now(),
                     "monotonic_ns": self.clock.mono_ns, "kind": "note", "mode": "live", "intent_id": None,
                     "customer_order_ref": None, "customer_ref": None, "bet_id": None, "market_id": None,
                     "state_after": None, "payload": {"reason": "bulk"}, "prev_sha256": previous}
            event["event_sha256"] = journal_module._event_sha256(event)
            rows.append((seq, event["event_id"], event["recorded_at"], event["monotonic_ns"], "note", "live", None,
                         None, None, None, None, None, '{"reason":"bulk"}', previous, event["event_sha256"]))
            previous = event["event_sha256"]
        raw = sqlite3.connect(str(path), isolation_level=None)
        try:
            raw.execute("BEGIN IMMEDIATE")
            raw.executemany(journal_module._INSERT_SQL, rows)
            raw.execute("COMMIT")
        finally:
            raw.close()
        race((holder, (5, 100)), (kill_like, (5,)))
        self.assertEqual(errors, [])
        result = journal.verify()
        self.assertEqual((result["status"], result["events"]), ("ok", 44 + 4000 + 15 + 7))
        self.assertEqual([e["seq"] for e in journal.events(since_seq=head["seq"] + 4000)],
                         list(range(head["seq"] + 4001, head["seq"] + 4001 + 22)))
        self.assertEqual(len(journal.fold(now_ns=self.clock.time_ns())["orders"]), 15)

    def test_a_writer_held_between_its_head_read_and_insert_never_forks_the_chain(self):
        """Amendment A3, D4 ("verify, don't assume"), made deterministic (review): the free-running race above
        catches a head read outside the write transaction only when its threads happen to interleave. Here
        connection A's append is held inside its write transaction, right after it read the head and caught
        its fold up, until connection B's append (its own thread) has reached its BEGIN; only then does A
        insert and commit. Reading the head inside BEGIN IMMEDIATE makes B wait for A and then take A's event
        as its head: both commit, the seqs are contiguous and the chain verifies. A head read before the
        BEGIN would give both the same seq (journal_integrity_error); a deferred BEGIN would let them race."""
        path = self.dir / "orders" / "journal-live.sqlite"
        a = self.open(mode="live", path=path)
        a.append("note", {"reason": "seed"})
        b_at_begin, b_result = threading.Event(), {}

        class WatchedConnection:
            """B's sqlite3 connection, saying when B is about to begin its write transaction."""

            def __init__(self, db):
                self._wrapped = db

            def execute(self, sql, *args):
                if sql.lstrip().upper().startswith("BEGIN"):
                    b_at_begin.set()
                return self._wrapped.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        def run_b():
            b = OrderJournal(path, mode="live", clock=self.clock)          # opening reads only: never blocked
            b._db = WatchedConnection(b._db)
            try:
                b_result["event"] = b.append("kill", {"reason": "operator kill", "scope": "own"})
            except Exception as exc:          # recorded, then asserted below
                b_result["error"] = exc
            finally:
                b_at_begin.set()
                b.close()

        b_thread = threading.Thread(target=run_b, name="journal-writer-b")
        original = a._committed_folder
        held = []

        def hold_until_b_begins(head):
            folder = original(head)
            if not held:                          # A's first append only: inside its transaction, head read
                held.append(head)
                b_thread.start()
                held.append(b_at_begin.wait(20))
            return folder

        a._committed_folder = hold_until_b_begins
        a_event = a.append("note", {"reason": "holder_report"})
        b_thread.join(60)
        self.assertEqual(held[1:], [True], "B never reached its BEGIN while A held its transaction")
        self.assertNotIn("error", b_result)
        self.assertEqual((a_event["seq"], b_result["event"]["seq"]), (2, 3))
        self.assertEqual(b_result["event"]["prev_sha256"], a_event["event_sha256"])
        self.assertEqual(a.verify(), {"status": "ok", "events": 3, "first_bad_seq": None})
        self.assertTrue(a.fold(now_ns=self.clock.time_ns())["halted"])

    def test_fullfsync_pragmas_set(self):
        journal = self.open()
        db = journal._db
        self.assertEqual(db.execute("PRAGMA fullfsync").fetchone()[0], 1)
        self.assertEqual(db.execute("PRAGMA checkpoint_fullfsync").fetchone()[0], 1)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)        # FULL

    def test_submit_committed_before_transport_call(self):
        journal = self.open()
        journal.append("intent", orders.intent_payload(), intent_id="intent-1", customer_order_ref=orders.REF,
                       customer_ref=orders.CREF, market_id=support.MARKET_ID, state_after="RECORDED")
        seen = []

        def fake_send():
            # A different connection (as another process would) must already see the committed submit.
            other = sqlite3.connect(str(self.path))
            try:
                seen.extend(other.execute("SELECT kind, payload_json FROM events WHERE kind='submit'").fetchall())
            finally:
                other.close()

        event = journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "c" * 64,
                                          "transitions": [{"customer_order_ref": orders.REF, "state": "SUBMITTING"}]},
                               intent_id="intent-1")
        self.assertFalse(journal._db.in_transaction)
        fake_send()
        self.assertEqual(len(seen), 1)
        self.assertIn("c" * 64, seen[0][1])
        self.assertEqual(event["seq"], 2)

    def test_order_ref_unique(self):
        journal = self.open()
        self.place(journal)
        with self.assertRaisesRegex(JournalError, "customer_order_ref_reused"):
            journal.append("intent", orders.intent_payload(), intent_id="intent-2", customer_order_ref=orders.REF,
                           customer_ref=orders.CREF, market_id=support.MARKET_ID)
        journal.append("note", {"about": "same ref"}, customer_order_ref=orders.REF)      # non-intent: allowed
        self.assertEqual(journal.verify()["events"], 3)

    def test_mode_mismatch_refused(self):
        self.open("paper")
        with self.assertRaisesRegex(JournalError, "journal_mode_mismatch"):
            OrderJournal(self.path, mode="live", clock=self.clock)
        with self.assertRaises(JournalError):
            OrderJournal(self.dir / "x.sqlite", mode="sandbox", clock=self.clock)
        live = self.open("live", self.dir / "journal-live.sqlite")
        event = live.append("note", {})
        with self.assertRaisesRegex(JournalError, "mode_mismatch"):
            fold_events([event], mode="paper", now_ns=self.clock.time_ns())

    def test_account_fingerprint_set_once(self):
        journal = self.open()
        self.assertIsNone(journal.account_fingerprint())
        journal.set_account_fingerprint("0123456789abcdef")
        journal.set_account_fingerprint("0123456789abcdef")
        with self.assertRaisesRegex(JournalError, "account_fingerprint_mismatch"):
            journal.set_account_fingerprint("fedcba9876543210")
        for bad in ("ABC", "0123456789ABCDEF", None, "0123456789abcdef0"):
            with self.subTest(bad=bad), self.assertRaises(JournalError):
                journal.set_account_fingerprint(bad)
        self.assertEqual(self.open().account_fingerprint(), "0123456789abcdef")

    def test_schema_checked(self):
        self.open().close()
        tampered = [("DROP TRIGGER events_no_update", "journal_schema_incomplete"),
                    ("PRAGMA user_version=2", "journal_schema_version_mismatch"),
                    ("DROP INDEX one_intent_per_order_ref", "journal_schema_incomplete")]
        for statement, code in tampered:
            with self.subTest(statement=statement):
                path = self.dir / f"t-{abs(hash(statement))}.sqlite"
                OrderJournal(path, mode="paper", clock=self.clock).close()
                raw = sqlite3.connect(str(path), isolation_level=None)
                raw.execute(statement)
                raw.close()
                with self.assertRaisesRegex(JournalError, code):
                    OrderJournal(path, mode="paper", clock=self.clock)
        foreign = self.dir / "foreign.sqlite"
        raw = sqlite3.connect(str(foreign), isolation_level=None)
        raw.execute("CREATE TABLE something (x)")
        raw.close()
        with self.assertRaises(JournalError):
            OrderJournal(foreign, mode="paper", clock=self.clock)

    def test_forbidden_payload_keys_recursive(self):
        journal = self.open()
        for payload in ({"a": [{"Session-Token": "x"}]}, {"deep": {"deeper": {"x_application": "y"}}},
                        {"list": [[{"errorDetails": "z"}]]}, {"value": Secret("synthetic-journal-secret")},
                        {"text": "prefix " + support.PLANTED_SECRET}):
            with self.subTest(payload=str(payload)[:30]), self.assertRaises(SecretLeak):
                if "text" in payload:
                    Secret(support.PLANTED_SECRET)
                journal.append("note", payload)
        with self.assertRaises(SecretLeak):
            Secret(support.PLANTED_SECRET)
            journal.append("note", {}, intent_id=support.PLANTED_SECRET)
        for payload in ({"x": float("nan")}, {"x": float("inf")}, {"x": {1, 2}}, ["not", "an", "object"]):
            with self.subTest(payload=str(payload)), self.assertRaises(JournalError):
                journal.append("note", payload)
        self.assertEqual(journal.verify()["events"], 0)

    def test_append_validates_columns_and_shapes(self):
        journal = self.open()
        cases = [
            ("bogus_kind", {}, {}),
            ("note", {}, {"market_id": "2.1"}),
            ("note", {}, {"bet_id": "12a"}),
            ("note", {}, {"state_after": "RECORDED"}),
            ("note", {}, {"customer_order_ref": orders.REF, "state_after": "MATCHED"}),
            ("intent", {"order": orders.instruction()}, {"intent_id": "i", "customer_order_ref": orders.REF,
                                                         "customer_ref": orders.CREF, "market_id": "1.1"}),
            ("intent", orders.intent_payload(), {"customer_order_ref": orders.REF}),
            ("intent", orders.intent_payload(orders.instruction(price_h=351)),
             {"intent_id": "i", "customer_order_ref": orders.REF, "customer_ref": orders.CREF, "market_id": "1.1"}),
            ("intent", orders.intent_payload(provisional_child=True),
             {"intent_id": "i", "customer_order_ref": orders.REF, "customer_ref": orders.CREF, "market_id": "1.1"}),
            ("submit", {"action": "fire", "instruction_count": 1}, {}),
            ("submit", {"action": "place", "instruction_count": 0}, {}),
            ("submit", {"action": "place", "instruction_count": 1, "transactions": 1}, {}),
            ("note", {"transitions": [{"customer_order_ref": orders.REF}]}, {}),
            ("note", {"transactions": -1}, {}),
        ]
        for kind, payload, columns in cases:
            with self.subTest(kind=kind, columns=columns), self.assertRaises(JournalError):
                journal.append(kind, payload, **columns)
        self.assertEqual(journal.verify()["events"], 0)

    def test_fold_reconstructs_states(self):
        journal = self.open()
        self.place(journal)
        journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTABLE", "fields": {"bet_id": "312345678901"}}]})
        self.clock.advance(5)
        journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTABLE",
             "fields": {"size_matched_p": 50, "average_price_matched_h": 350}}]})
        self.place(journal, REF2, intent_id="intent-2", market_id="1.2", side="LAY", price_h=300, size_p=100)
        journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": REF2, "state": "UNKNOWN"}]})
        journal.append("submit", {"action": "cancel", "instruction_count": 1, "transitions": [
            {"customer_order_ref": orders.REF, "state": "CANCEL_PENDING"}]})
        journal.append("report", {"action": "cancel", "transactions": 1, "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTABLE"}]})
        journal.append("funds_check", {"funds": {"availableToBetBalance": 100.0, "exposure": -2.0}})
        journal.append("halt", {"reason": "unattributed_candidate_order"})
        journal.append("halt", {"reason": "unresolved_replace", "scope": "market"}, market_id="1.3")
        journal.append("ref_roundtrip_verified", {})
        journal.append("startup_reconcile", {"exchange_orders_today": 7, "exchange_realized_today_pence": -150})
        now = self.clock.time_ns()
        state = journal.fold(now_ns=now)
        self.assertEqual(set(state), journal_module.FOLD_KEYS)
        first, second = state["orders"][orders.REF], state["orders"][REF2]
        self.assertEqual((first["state"], first["size_matched_p"], first["size_remaining_p"], first["bet_id"]),
                         ("EXECUTABLE", 50, 150, "312345678901"))
        self.assertEqual(first["exposure_basis"], "adverse_unmatched")
        self.assertEqual(first["submitted_at"], support.BASE_UTC)
        self.assertEqual(first["body_sha256"], "a" * 64)
        self.assertEqual((second["state"], second["exposure_basis"], second["size_remaining_p"]),
                         ("UNKNOWN", "adverse_unknown", 100))
        self.assertIsNotNone(second["outcome_observed_at"])
        self.assertEqual(state["unresolved_markets"], ["1.2", "1.3"])
        self.assertEqual(len(state["submitted_instruction_wall_ns"]), 2)       # place + place, not the cancel
        self.assertEqual(sum(state["transactions_by_clock_hour"].values()), 3)  # 2 placements + 1 failed cancel
        self.assertEqual(state["orders_today"], 2)
        self.assertEqual(state["strategy_refs_seen"], ["bar-manual"])
        self.assertTrue(state["halted"])
        self.assertEqual(state["halt_reasons"], ["unattributed_candidate_order"])
        self.assertTrue(state["ref_roundtrip_verified"])
        self.assertEqual(state["funds"], {"availableToBetBalance": 100.0, "exposure": -2.0})
        self.assertEqual(state["exchange_floor"], {"orders_today": 7, "realized_today_pence": -150})
        self.assertEqual(state["consecutive_unknown"], 0)
        self.assertEqual(parse_utc_ns(state["funds_checked_at"]), now)
        self.assertFalse(state["clock_regression"])
        self.assertTrue(journal.fold(now_ns=now - 6 * 10**9)["clock_regression"])
        self.assertIsNone(journal.fold(now_ns=now + 86400 * 10**9)["exchange_floor"])
        journal.append("resume", {"scope": "global"})
        journal.append("resume", {"scope": "market"}, market_id="1.3")
        state = journal.fold(now_ns=now)
        self.assertFalse(state["halted"])
        self.assertEqual(state["unresolved_markets"], ["1.2"])
        # Folding is pure: the same events give the same state.
        self.assertEqual(state, fold_events(list(journal.events()), mode="paper", now_ns=now))

    def test_fold_settlement_and_realized_today(self):
        journal = self.open()
        self.place(journal)
        journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTION_COMPLETE",
             "fields": {"bet_id": "1", "size_matched_p": 200, "average_price_matched_h": 350}}]})
        now = self.clock.time_ns()
        state = journal.fold(now_ns=now)
        self.assertEqual(state["unsettled_matched_liability_today_pence"], 200)    # a BACK risks its stake
        journal.append("settle", {"commission_p": 25, "transitions": [
            {"customer_order_ref": orders.REF, "state": "SETTLED",
             "fields": {"settled_profit_own_p": 500, "settled_profit_betfair_p": 499}}]})
        state = journal.fold(now_ns=now)
        self.assertEqual(state["realized_today_pence"], 499 - 25)
        self.assertEqual(state["unsettled_matched_liability_today_pence"], 0)
        self.assertEqual(state["orders"][orders.REF]["exposure_basis"], "none")
        self.assertEqual(journal.fold(now_ns=now + 86400 * 10**9)["realized_today_pence"], 0)
        # Unknown settled profit counts as the worst case (the matched liability).
        self.place(journal, REF2, intent_id="intent-2", side="LAY", price_h=400, size_p=100)
        journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": REF2, "state": "EXECUTION_COMPLETE",
             "fields": {"bet_id": "2", "size_matched_p": 100, "average_price_matched_h": 400}}]})
        journal.append("settle", {"commission_p": 0, "transitions": [{"customer_order_ref": REF2, "state": "SETTLED"}]})
        folded = journal.fold(now_ns=now)
        self.assertEqual(folded["realized_today_pence"], 474 - 300)
        # Matched worst-case loss of today's settled orders: BACK 2.00 stake + LAY 1.00 @ 4.00 liability.
        self.assertEqual(folded["settled_matched_liability_today_pence"], 200 + 300)

    def test_settled_worst_case_counts_the_remaining_size(self):
        """Amendment A3 (D3): settle-market moves an EXECUTABLE order to SETTLED with its remaining size as
        recorded and no profit, so the fold books its matched worst case plus its remainder at the limit
        (BACK: stake; LAY: liability, rounded up) as realised loss; the order carries no exposure after."""
        from betangel_research.betfair.journal import settled_worst_loss_pence
        from betangel_research.betfair.risk import exposure_by_market
        journal = self.open(mode="live", path=self.dir / "journal-live.sqlite")
        ref3 = "bro200101120000-00000000000000a3"
        self.place(journal)                                                       # BACK 2.00 @ 3.50, nothing matched
        self.place(journal, REF2, intent_id="intent-2", side="LAY", price_h=410, size_p=100)
        self.place(journal, ref3, intent_id="intent-3", side="LAY", price_h=300, size_p=200)
        journal.append("report", {"action": "place", "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTABLE", "fields": {"bet_id": "1"}},
            {"customer_order_ref": REF2, "state": "EXECUTABLE", "fields": {"bet_id": "2"}},
            {"customer_order_ref": ref3, "state": "EXECUTABLE",
             "fields": {"bet_id": "3", "size_matched_p": 100, "average_price_matched_h": 290}}]})
        now = self.clock.time_ns()
        before = journal.fold(now_ns=now)
        worst = {ref: settled_worst_loss_pence(before["orders"][ref]) for ref in (orders.REF, REF2, ref3)}
        # BACK remaining 2.00 -> 200; LAY 1.00 @ 4.10 -> 310; LAY matched 1.00 (limit 3.00) + 1.00 left.
        self.assertEqual(worst, {orders.REF: 200, REF2: 310, ref3: 200 + 200})
        self.assertEqual(settled_worst_loss_pence(dict(before["orders"][REF2], size_remaining_p=33)), 103)
        self.assertGreater(exposure_by_market(list(before["orders"].values()))["1.1"], 0)
        journal.append("settle", {"basis": "operator_settled_worst_case", "echo": "SETTLE MARKET 1.1",
                                  "shown_sha256": "c" * 64, "market_status": "CLOSED", "commission_p": 0,
                                  "transitions": [
                                      {"customer_order_ref": ref, "state": "SETTLED",
                                       "reason": "operator_settle_market",
                                       "fields": {"settled_profit_own_p": None, "settled_profit_betfair_p": None}}
                                      for ref in (orders.REF, REF2, ref3)]}, market_id=support.MARKET_ID)
        after = journal.fold(now_ns=now)
        for ref in (orders.REF, REF2, ref3):
            order = after["orders"][ref]
            self.assertEqual((order["state"], order["exposure_basis"]), ("SETTLED", "none"))
            self.assertEqual(order["size_remaining_p"], before["orders"][ref]["size_remaining_p"])
        self.assertEqual(after["realized_today_pence"], -sum(worst.values()))
        self.assertEqual(after["settled_matched_liability_today_pence"], sum(worst.values()))
        self.assertEqual(exposure_by_market(list(after["orders"].values())), {"1.1": 0})
        self.assertEqual(after["unresolved_markets"], [])
        # A stated profit still wins over the worst case (paper settlement is unchanged).
        stated = dict(after["orders"][orders.REF], settled_profit_own_p=150, settled_profit_betfair_p=140)
        self.assertEqual(journal_module._settled_profit(stated), 140)

    def test_settle_payload_accepts_echo_and_shown_sha256(self):
        """settle-market's typed line rides in its settle event: the fold accepts basis, echo, shown_sha256 and
        market_status on settle, and refuses malformed ones before anything is written."""
        journal = self.open(mode="live", path=self.dir / "journal-live.sqlite")
        good = {"basis": "operator_settled_worst_case", "echo": "SETTLE MARKET 1.1", "shown_sha256": "d" * 64,
                "market_status": "CLOSED", "commission_p": 0, "transitions": []}
        for change, code in (({"basis": "betfair_profit"}, "settle_basis_invalid"),
                             ({"echo": ""}, "settle_echo_invalid"), ({"echo": 7}, "settle_echo_invalid"),
                             ({"shown_sha256": "D" * 64}, "settle_shown_sha256_invalid"),
                             ({"shown_sha256": None}, "settle_shown_sha256_invalid"),
                             ({"market_status": "OPEN"}, "settle_market_status_invalid")):
            with self.subTest(change=change), self.assertRaisesRegex(JournalError, code):
                journal.append("settle", dict(good, **change), market_id=support.MARKET_ID)
        self.assertEqual(journal.verify()["events"], 0)
        event = journal.append("settle", good, market_id=support.MARKET_ID)
        self.assertEqual({k: event["payload"][k] for k in ("basis", "echo", "shown_sha256", "market_status")},
                         {k: good[k] for k in ("basis", "echo", "shown_sha256", "market_status")})

    def test_settle_commission_must_be_stated(self):
        """Regression: a settle event's commission_p is a non-negative integer; never silently zero."""
        journal = self.open()
        for payload in ({"transitions": []}, {"commission_p": None, "transitions": []},
                        {"commission_p": "25", "transitions": []}, {"commission_p": 2.5, "transitions": []},
                        {"commission_p": -1, "transitions": []}, {"commission_p": True, "transitions": []}):
            with self.subTest(payload=payload), self.assertRaisesRegex(JournalError, "commission_p"):
                journal.append("settle", payload, market_id=support.MARKET_ID)
        journal.append("settle", {"commission_p": 0, "transitions": []}, market_id=support.MARKET_ID)
        self.assertEqual(journal.verify()["events"], 1)

    def test_resume_scope_fails_closed(self):
        """Regression: only an explicit global resume clears the HALT; a malformed resume is refused
        at append, and a raw unrecognised one folds as a no-op, never a global clear."""
        journal = self.open(mode="live", path=self.dir / "journal-live.sqlite")
        journal.append("kill", {"reason": "operator", "scope": "own"})
        journal.append("halt", {"reason": "kill"})
        for payload, columns in (({"scope": "market"}, {}), ({"scope": "markte"}, {"market_id": "1.1"}),
                                 ({}, {}), ({"scope": None}, {})):
            with self.subTest(payload=payload), self.assertRaisesRegex(JournalError, "resume"):
                journal.append("resume", payload, **columns)
        self.assertTrue(journal.fold(now_ns=self.clock.time_ns())["halted"])
        journal.append("resume", {"scope": "market"}, market_id="1.1")
        self.assertTrue(journal.fold(now_ns=self.clock.time_ns())["halted"])
        raw = journal_module.JournalFolder("live")
        for event in journal.events():
            raw.apply(event)
        raw.apply(dict(event, kind="resume", payload={"scope": "markte"}, market_id=None))
        raw.apply(dict(event, kind="resume", payload={}, market_id=None))
        self.assertTrue(raw.state(now_ns=self.clock.time_ns())["halted"])
        journal.append("resume", {"scope": "global"})
        self.assertFalse(journal.fold(now_ns=self.clock.time_ns())["halted"])

    def test_append_refuses_unfoldable_moves(self):
        """Regression: an illegal or inconsistent move is refused at append, so it is never committed
        and the append-only journal always stays foldable."""
        journal = self.open()
        self.place(journal)
        bad_moves = [
            ("illegal_transition", {"customer_order_ref": orders.REF, "state": "SETTLED"}),
            ("final_state_size_unaccounted", {"customer_order_ref": orders.REF, "state": "EXECUTION_COMPLETE",
                                              "fields": {"size_matched_p": 999, "average_price_matched_h": 350}}),
            ("invalid_order_record", {"customer_order_ref": orders.REF, "state": "EXECUTABLE",
                                      "fields": {"size_matched_p": 999, "average_price_matched_h": 350}}),
            ("final_state_size_unaccounted", {"customer_order_ref": orders.REF, "state": "EXECUTION_COMPLETE"}),
            ("not_mutable", {"customer_order_ref": orders.REF, "state": "EXECUTABLE", "fields": {"price_h": 345}}),
            ("reason_invalid", {"customer_order_ref": orders.REF, "state": "EXECUTABLE", "reason": "has space"}),
            ("unknown_order", {"customer_order_ref": REF2, "state": "UNKNOWN"}),
            ("journal_event_unfoldable", {"customer_order_ref": orders.REF, "state": "EXECUTABLE",
                                          "fields": {"size_matched_p": "50"}}),
        ]
        for code, move in bad_moves:
            with self.subTest(code=code), self.assertRaisesRegex(JournalError, code):
                journal.append("report", {"transitions": [move]}, market_id=support.MARKET_ID)
        # A legal move followed by an illegal one in the same event: the whole event is refused.
        with self.assertRaisesRegex(JournalError, "illegal_transition"):
            journal.append("report", {"transitions": [
                {"customer_order_ref": orders.REF, "state": "EXECUTABLE", "fields": {"bet_id": "1"}},
                {"customer_order_ref": orders.REF, "state": "ABSENT_ACCEPTED"}]})
        with self.assertRaisesRegex(JournalError, "illegal_transition"):
            journal.append("note", {"reason": "x"}, customer_order_ref=orders.REF, state_after="SETTLED")
        self.assertEqual(journal.verify(), {"status": "ok", "events": 2, "first_bad_seq": None})
        journal.append("report", {"transitions": [{"customer_order_ref": orders.REF, "state": "EXECUTABLE",
                                                   "fields": {"bet_id": "1"}}]})
        self.assertEqual(journal.fold(now_ns=self.clock.time_ns())["orders"][orders.REF]["state"], "EXECUTABLE")
        # Another connection's appends are caught up before validating (no stale cached fold).
        other = self.open()
        other.append("submit", {"action": "cancel", "instruction_count": 1, "transitions": [
            {"customer_order_ref": orders.REF, "state": "CANCEL_PENDING"}]})
        with self.assertRaisesRegex(JournalError, "illegal_transition:CANCEL_PENDING->REPLACE_PENDING"):
            journal.append("submit", {"action": "replace", "instruction_count": 1, "transitions": [
                {"customer_order_ref": orders.REF, "state": "REPLACE_PENDING"}]})
        journal.append("report", {"action": "cancel", "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTION_COMPLETE",
             "fields": {"size_cancelled_p": 200, "size_remaining_p": 0}}]})
        self.assertEqual(other.fold(now_ns=self.clock.time_ns())["orders"][orders.REF]["state"], "EXECUTION_COMPLETE")
        # Raw events that were never validated (tampering) still make the pure fold raise.
        events = list(journal.events())
        illegal = dict(events[-1], payload={"transitions": [{"customer_order_ref": orders.REF, "state": "SUBMITTING"}]})
        with self.assertRaisesRegex(JournalError, "illegal_transition"):
            fold_events(events + [illegal], mode="paper", now_ns=self.clock.time_ns())

    def test_unfoldable_prefix_still_accepts_kill_and_halt(self):
        """A journal whose committed events cannot be folded (tampered, or written by older code)
        refuses order moves but still records kill, halt and note events."""
        journal = self.open()
        self.place(journal)
        raw = self.raw()
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET payload_json=? WHERE seq=2", (
            '{"action":"place","body_sha256":"' + "a" * 64 + '","instruction_count":1,"transitions":'
            '[{"customer_order_ref":"' + orders.REF + '","state":"SETTLED"}]}',))
        raw.execute(next(statement for statement in journal_module.SCHEMA_STATEMENTS if "events_no_update" in statement))
        fresh = self.open()
        with self.assertRaisesRegex(JournalError, "journal_unfoldable"):
            fresh.append("report", {"transitions": [{"customer_order_ref": orders.REF, "state": "UNKNOWN"}]})
        with self.assertRaisesRegex(JournalError, "journal_unfoldable"):
            fresh.append("resume", {"scope": "global"})
        fresh.append("kill", {"reason": "operator", "scope": "own"})
        fresh.append("halt", {"reason": "kill"})
        fresh.append("note", {"text": "journal broken"})
        kinds = [event["kind"] for event in fresh.events()]
        self.assertEqual(kinds[-3:], ["kill", "halt", "note"])
        # A committed row of the wrong type (not a JournalError inside the fold) is handled the same way.
        raw.execute("DROP TRIGGER events_no_update")
        raw.execute("UPDATE events SET payload_json=? WHERE seq=1", ('{"order":7}',))
        raw.execute(next(statement for statement in journal_module.SCHEMA_STATEMENTS if "events_no_update" in statement))
        typed = self.open()
        with self.assertRaisesRegex(JournalError, "journal_unfoldable"):
            typed.append("report", {"transitions": [{"customer_order_ref": orders.REF, "state": "UNKNOWN"}]})
        typed.append("kill", {"reason": "operator", "scope": "own"})
        # The kill sweep's own cancel events (no order moves) are recorded too; place submits are not.
        typed.append("cancel_intent", {"approved": True, "bet_ids": ["300000000001"]})
        typed.append("submit", {"action": "cancel", "instruction_count": 1, "body_sha256": "b" * 64,
                                "transitions": []})
        typed.append("report", {"action": "cancel", "transitions": []})
        for kind, payload in (("submit", {"action": "place", "instruction_count": 1, "transitions": []}),
                              ("submit", {"action": "cancel", "instruction_count": 1, "transitions": [
                                  {"customer_order_ref": orders.REF, "state": "CANCEL_PENDING"}]}),
                              ("risk_decision", {"approved": True})):
            with self.subTest(kind=kind, payload=payload), self.assertRaisesRegex(JournalError, "journal_unfoldable"):
                typed.append(kind, payload)

    def test_compare_and_append_on_the_expected_head(self):
        """append(expect_head=...) commits only if the head inside the write transaction is the one
        the caller saw (the resume prompt and every place or replace submit rely on it)."""
        journal = self.open()
        with self.assertRaisesRegex(JournalError, "journal_head_moved"):
            journal.append("note", {"reason": "x"}, expect_head={"seq": 1, "last_sha256": "a" * 64})
        first = journal.append("note", {"reason": "first"}, expect_head={})
        seen = journal.anchor()
        other = self.open()
        other.append("halt", {"reason": "elsewhere", "scope": "global"})
        with self.assertRaisesRegex(JournalError, "journal_head_moved"):
            journal.append("resume", {"scope": "global", "cleared_through_seq": seen["seq"]}, expect_head=seen)
        self.assertTrue(journal.fold(now_ns=self.clock.time_ns())["halted"])
        head = journal.anchor()
        journal.append("note", {"reason": "next"}, expect_head=head)
        self.assertEqual(first["seq"], 1)
        for bad in ({"seq": 0, "last_sha256": "a" * 64}, {"seq": "1"}, [], {"seq": 1, "last_sha256": "A" * 64}):
            with self.subTest(bad=bad), self.assertRaisesRegex(JournalError, "expect_head_invalid"):
                journal.append("note", {"reason": "y"}, expect_head=bad)

    def test_bounded_resume_clears_only_what_was_shown(self):
        """A global resume carrying cleared_through_seq clears the halts at or below that seq only."""
        journal = self.open(mode="live", path=self.dir / "journal-live.sqlite")
        journal.append("halt", {"reason": "seen", "scope": "global"})
        seen = journal.anchor()["seq"]
        journal.append("kill", {"reason": "later", "scope": "own"})
        journal.append("halt", {"reason": "kill", "scope": "global"})
        journal.append("resume", {"scope": "global", "cleared_through_seq": seen})
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual((state["halted"], state["halt_reasons"]), (True, ["kill", "kill"]))
        journal.append("resume", {"scope": "global", "cleared_through_seq": journal.anchor()["seq"]})
        self.assertFalse(journal.fold(now_ns=self.clock.time_ns())["halted"])
        for bad in ({"scope": "global", "cleared_through_seq": -1}, {"scope": "global", "cleared_through_seq": "3"},
                    {"scope": "global", "cleared_through_seq": True}):
            with self.subTest(bad=bad), self.assertRaisesRegex(JournalError, "cleared_through_seq"):
                journal.append("resume", bad)
        with self.assertRaisesRegex(JournalError, "cleared_through_seq"):
            journal.append("resume", {"scope": "global", "cleared_through_seq": journal.anchor()["seq"] + 5})

    def test_payload_halts_fold_atomically(self):
        """R3#13: a report or risk_decision may carry payload "halts"; the fold applies each one as a global HALT
        with that event's seq (so a bounded resume treats it like a halt event), in the same event as its moves.
        Any other kind, or a malformed list, is refused before anything is written."""
        journal = self.open(mode="live", path=self.dir / "journal-live.sqlite")
        self.place(journal)
        event = journal.append("report", {"action": "place", "halts": ["terminal_account"], "transitions": [
            {"customer_order_ref": orders.REF, "state": "FAILED", "fields": {"error_code": "LOSS_LIMIT_EXCEEDED"}}]})
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(state["orders"][orders.REF]["state"], "FAILED")
        self.assertEqual((state["halted"], state["halt_reasons"]), (True, ["terminal_account"]))
        self.assertEqual(fold_events(list(journal.events())[:-1], mode="live", now_ns=self.clock.time_ns())["halted"],
                         False)                                       # the HALT exists exactly when the move does
        journal.append("risk_decision", {"approved": False, "rule": "unexplained_account_exposure",
                                         "halts": ["unexplained_account_exposure"]})
        journal.append("resume", {"scope": "global", "cleared_through_seq": event["seq"]})
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual((state["halted"], state["halt_reasons"]), (True, ["unexplained_account_exposure"]))
        journal.append("resume", {"scope": "global", "cleared_through_seq": journal.anchor()["seq"]})
        self.assertFalse(journal.fold(now_ns=self.clock.time_ns())["halted"])
        head = journal.anchor()
        for kind, halts in (("note", ["x"]), ("halt", ["x"]), ("settle", ["x"]), ("report", []), ("report", "x"),
                            ("report", ["a", "a"]), ("report", ["has space"]), ("report", [1]), ("report", None)):
            with self.subTest(kind=kind, halts=halts), self.assertRaisesRegex(JournalError, "journal_halts_invalid"):
                payload = {"halts": halts, "reason": "x", "scope": "global", "commission_p": 0}
                journal.append(kind, payload)
        self.assertEqual(journal.anchor(), head)

    def test_paper_state_payload_round_trips(self):
        """R3#12: a paper report, settle or note may carry payload "paper_state"; the latest one comes back from
        last_paper_state() exactly as written (and the fold ignores it). Any other kind, a malformed state, or a
        live journal is refused before anything is written."""
        journal = self.open()
        self.assertIsNone(journal.last_paper_state())
        self.place(journal)
        state = {"schema_version": 1, "queues": {orders.REF: None}, "books": {"1.1": orders.snapshot()},
                 "exchange": {"taken": {"1.1": {"12345:back:350": 100}}, "activated_ms": {orders.REF: 7},
                              "traded_hwm": {"1.1": {"12345:350": "100.0"}}, "activation": {orders.REF: 1},
                              "sequence": 1}}
        journal.append("report", {"action": "place", "paper_state": state, "transitions": [
            {"customer_order_ref": orders.REF, "state": "EXECUTABLE", "fields": {"bet_id": "1"}}]})
        self.assertEqual(journal.last_paper_state(), state)
        later = dict(state, queues={orders.REF: 0})
        journal.append("note", {"reason": "paper_advance_state", "paper_state": later})
        journal.append("halt", {"reason": "x", "scope": "global"})       # events without one do not hide it
        self.assertEqual(journal.last_paper_state(), later)
        self.assertEqual(journal.fold(now_ns=self.clock.time_ns())["orders"][orders.REF]["state"], "EXECUTABLE")
        # Re-review R2: schema_version 2 adds the closed-market markers {"markets": {id: ms}, "floor_ms"}.
        marked = dict(state, schema_version=2, closed={"markets": {"1.2": 1577880000000}, "floor_ms": None})
        journal.append("note", {"reason": "paper_advance_state", "paper_state": marked})
        floored = dict(marked, closed={"markets": {}, "floor_ms": 1577880000000})
        journal.append("note", {"reason": "paper_advance_state", "paper_state": floored})
        self.assertEqual(journal.last_paper_state(), floored)
        head = journal.anchor()
        bad_closed = ({"markets": {}}, {"markets": {}, "floor_ms": None, "x": 1}, {"markets": [], "floor_ms": None},
                      {"markets": {}, "floor_ms": -1}, {"markets": {}, "floor_ms": True},
                      {"markets": {}, "floor_ms": 1.5}, {"markets": {"2.1": 1}, "floor_ms": None},
                      {"markets": {"1.2": "1"}, "floor_ms": None}, {"markets": {"1.2": True}, "floor_ms": None},
                      {"markets": {"1.2": -1}, "floor_ms": None}, None)
        for kind, value in (("halt", state), ("kill", state), ("report", dict(state, schema_version=2)),
                            ("report", dict(marked, schema_version=1)), ("report", dict(state, schema_version=True)),
                            ("report", dict(state, schema_version=3)),
                            ("report", {k: v for k, v in state.items() if k != "books"}), ("note", None),
                            ("settle", dict(state, queues=[])), ("note", dict(state, extra={})),
                            *(("note", dict(marked, closed=closed)) for closed in bad_closed)):
            with self.subTest(kind=kind, value=value and value.get("closed")), \
                    self.assertRaisesRegex(JournalError, "paper_state_invalid"):
                journal.append(kind, {"paper_state": value, "reason": "x", "scope": "global", "commission_p": 0})
        self.assertEqual(journal.anchor(), head)
        live = self.open(mode="live", path=self.dir / "journal-live.sqlite")
        with self.assertRaisesRegex(JournalError, "paper_state_in_live_journal"):
            live.append("note", {"reason": "x", "paper_state": state})
        self.assertEqual(live.anchor(), {})

    def test_replace_child_intent_must_match_parent(self):
        """Regression: a provisional child must be the parent's selection, side and handicap, no larger
        than its remaining size, and its parent must be live (EXECUTABLE or REPLACE_PENDING)."""
        journal = self.open()
        self.place(journal)
        child = "bro200101120000-00000000000000c1"

        def child_intent(**order):
            journal.append("intent", orders.intent_payload(orders.instruction(**order), parent_ref=orders.REF,
                                                           provisional_child=True),
                           intent_id="intent-c", customer_order_ref=child, customer_ref=orders.CREF,
                           market_id=support.MARKET_ID)

        with self.assertRaisesRegex(JournalError, "parent_not_replaceable"):
            child_intent(price_h=345)                       # the parent is still SUBMITTING
        journal.append("report", {"transitions": [{"customer_order_ref": orders.REF, "state": "EXECUTABLE",
                                                   "fields": {"bet_id": "1", "size_matched_p": 50,
                                                              "average_price_matched_h": 350}}]})
        for order, code in (({"price_h": 345, "side": "LAY"}, "differs_from_parent"),
                            ({"price_h": 345, "selection_id": 999}, "differs_from_parent"),
                            ({"price_h": 345, "size_p": 151}, "parent_not_replaceable")):
            with self.subTest(order=order), self.assertRaisesRegex(JournalError, code):
                child_intent(**order)
        child_intent(price_h=345, size_p=150)
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(state["orders"][orders.REF]["child_ref"], child)
        self.assertTrue(state["orders"][child]["provisional_child"])

    def test_crash_submitting_becomes_unknown(self):
        journal = self.open()
        self.place(journal)
        state = journal.fold(now_ns=self.clock.time_ns())
        order = state["orders"][orders.REF]
        self.assertEqual((order["state"], order["error_code"], order["exposure_basis"]),
                         ("UNKNOWN", "crash_between_submit_and_report", "adverse_unknown"))
        self.assertEqual(state["unresolved_markets"], [support.MARKET_ID])
        kept = journal.fold(now_ns=self.clock.time_ns(), submitting_as_unknown=False)
        self.assertEqual(kept["orders"][orders.REF]["state"], "SUBMITTING")

    def test_reconcile_after_crash_moves_through_unknown(self):
        """Regression: after a crash the fold shows a stored SUBMITTING order as UNKNOWN; the moves
        reconcile then writes from UNKNOWN (LAPSED, ABSENT_UNCONFIRMED) are legal against the stored
        state and the journal stays foldable."""
        journal = self.open(mode="live", path=self.dir / "journal-live.sqlite")
        journal.append("intent", orders.intent_payload(source="cli_manual"), intent_id="i1",
                       customer_order_ref=orders.REF, customer_ref=orders.CREF, market_id=support.MARKET_ID)
        journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "0" * 64,
                                  "transitions": [{"customer_order_ref": orders.REF, "state": "SUBMITTING"}]},
                       market_id=support.MARKET_ID)
        self.assertEqual(journal.fold(now_ns=self.clock.time_ns())["orders"][orders.REF]["state"], "UNKNOWN")
        # Found in listClearedOrders LAPSED: the lapsed size must be stated (nothing vanishes by default).
        with self.assertRaisesRegex(JournalError, "final_state_size_unaccounted"):
            journal.append("reconcile", {"transitions": [{"customer_order_ref": orders.REF, "state": "LAPSED",
                                                          "reason": "found_cleared_lapsed"}]})
        self.clock.advance(1)
        journal.append("reconcile", {"transitions": [{"customer_order_ref": orders.REF, "state": "LAPSED",
                                                      "reason": "found_cleared_lapsed",
                                                      "fields": {"size_lapsed_p": 200}}]})
        order = journal.fold(now_ns=self.clock.time_ns())["orders"][orders.REF]
        self.assertEqual((order["state"], order["size_lapsed_p"], order["reconcile_attempts"]), ("LAPSED", 200, 1))
        self.assertEqual(order["outcome_observed_at"], self.clock.utc_now())
        second = "bro200101120000-00000000000000a3"
        journal.append("intent", orders.intent_payload(source="cli_manual"), intent_id="i2",
                       customer_order_ref=second, customer_ref=orders.CREF, market_id=support.MARKET_ID)
        journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "1" * 64,
                                  "transitions": [{"customer_order_ref": second, "state": "SUBMITTING"}]})
        journal.append("reconcile", {"transitions": [{"customer_order_ref": second, "state": "ABSENT_UNCONFIRMED"}]})
        state = journal.fold(now_ns=self.clock.time_ns())
        self.assertEqual(state["orders"][second]["state"], "ABSENT_UNCONFIRMED")
        self.assertEqual(state["orders"][second]["exposure_basis"], "adverse_unknown")
        self.assertEqual(state, fold_events(list(journal.events()), mode="live", now_ns=self.clock.time_ns()))

    def test_anchor_values(self):
        journal = self.open()
        self.assertEqual(journal.anchor(), {})
        self.place(journal)
        events = list(journal.events())
        self.assertEqual(journal.anchor(), {"genesis_sha256": events[0]["event_sha256"], "seq": 2,
                                            "last_sha256": events[1]["event_sha256"]})
        journal.append("note", {})
        anchor = journal.anchor()
        self.assertEqual((anchor["genesis_sha256"], anchor["seq"]), (events[0]["event_sha256"], 3))


if __name__ == "__main__":
    unittest.main()
