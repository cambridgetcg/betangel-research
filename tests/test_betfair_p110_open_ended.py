"""P1-10 S2a: the journal records the end of an open (design v4 S2a; Astra #1; IR-2; Astra v2 #2; Yu, Q1).

Only another open's reservation ended an open in the journal. finish() ends the rest, an open that is not live, as the
present's knowledge only, so a frontier note resting on that end is premature at its own position (Nuance R3-1). An
open_ended note, {"type": "open_ended", "open_id"} with no column, makes the end a journal fact. A valid one (its shape,
this contract, an open the journal knows and has not ended) ends its open at the fold clock and evaluates that open's
records there, under the previous event's label, as finish() does; only then does its stamp move the clock (rule 7, a
deliberate choice with no necessity claim: Astra v2 #2). Any other is an invalid note, a repeat included (Astra, Q2).
The fold marks what only the present knows: RecordState.unsent_by_present and PilotState.present_ended_opens, beside
PilotState.live_open_id. Disclosed, and accepted by Yu in plain words (Q1): once the note is journaled, a record with
no submit counts £0, not the -200p placeholder, so a pause that would fire only because of that placeholder does not.

On ec18c4f, before this slice, an open_ended note is pilot_record_invalid and the new state fields do not exist. The
note is written out here rather than built by pilot_note, so that ec18c4f journals and folds it too.

The rule 7 pin compares the fold after the note with the fold its writer read, at every prefix of fold_fresh's pinned,
generated and drained journals where a drain would write one: for a stamp an hour before J (a wall stepped back), one
just past the next pending trigger of a record of another open, and J + 30 days; and probe_adv's shapes also just past
A's deadline and at t0. It checks that equality, not rule 7's order, which one test pins inside the pass
(RuleSevenTests). Synthetic: nothing here is P1-10's drain, which writes the note (S2c, S3b, S3c).
"""

import copy
import dataclasses
import unittest

from betangel_research.betfair import pilot
from betangel_research.betfair.pilot import DAY_NS, NS, PilotError

from test_betfair_pilot import C, HOUR, MIN, S, Pilot, bodies, codes, only, replayed, sha, stop_ids
import test_betfair_p110_fold_fresh as fresh

C400 = dataclasses.replace(C, pause_step_p=400)      # the design's pause probe: two placeholders' worth reach a level
DEADLINE = C.deadline_s * NS
CUTOFF = C.bsp_cutoff_s * NS


def open_ended_note(open_id, **changes):
    """An open_ended note's payload, written out rather than built by pilot_note, so that ec18c4f, which has no such
    type, journals and folds it too (as pilot_record_invalid)."""
    return {"pilot": dict({"v": 1, "type": "open_ended", "contract_sha256": C.sha256, "open_id": open_id}, **changes)}


def end(journal_, open_id, *, at=None, kind="note", **columns):
    """An open_ended note naming open_id, stamped at `at` (a second after the builder's clock by default)."""
    return journal_.add(kind, open_ended_note(open_id), at=at, **columns)


def last_open(*, closed=False):
    """One session whose one open, the last, reserved and was placed by the gateway's classify, then closed with no
    submit; with closed, its market's CLOSED book is read (S1b). -> (journal, placed, session)"""
    journal_ = Pilot(pt4=True)
    session = journal_.arm(0)
    _, p, _ = journal_.classify(session)
    if closed:
        journal_.closed(p, sp="3.10")
    return journal_, p, session


def invalid_at(seq):
    return [(f"pilot-record-invalid-{seq}", "review")]


def as_written(state):
    """What the rule 7 pin compares: equity, the pause, the due stops, the violations and the sessions, and each
    record's measurement, ledger and execution."""
    return {"equity": state.equity_p, "pause_fired": state.pause_fired, "due_stops": state.due_stops,
            "violations": state.violations, "sessions": state.sessions,
            "records": [(rec.ref, rec.measurement, rec.ledger, rec.execution) for rec in state.place_records]}


def drain_would_end(folder):
    """The opens a drain would end after this prefix, read off the journal order: one that no journal event has
    ended, with a record that has no submit, no FAILED(transport_not_sent) and no evidence of sending, which finish()
    with no live open then marks unsent_by_present (S2c's open_ended_problem)."""
    return [open_id for open_id in folder.opens if open_id not in folder.ended_opens and any(
        rec.open_id == open_id and rec.submit_seq is None and rec.failed_seq is None and rec.sent_seq is None
        for rec in folder.records)]


def last_acts(events):
    """{open id: the seq of the last intent or submit of one of its records}. Until then the open is still acting, so
    no drain runs: the builder writes a decision and its send inside one open."""
    opens, found = {}, {}
    for e in events:
        inner = (e["payload"].get("pilot") or {}) if e["kind"] == "note" else {}
        if inner.get("type") == "decision" and inner["outcome"] == "PLACE":
            opens[inner["customer_order_ref"]] = inner["open_id"]
        if e["kind"] == "intent":
            refs = [e["customer_order_ref"]]
        elif e["kind"] == "submit":
            refs = [move.get("customer_order_ref") for move in e["payload"].get("transitions") or ()]
        else:
            refs = []
        found.update({opens[ref]: e["seq"] for ref in refs if ref in opens})
    return found


def with_note(events, open_id, at):
    """The events, then an open_ended note naming open_id at `at`, written by the builder after them."""
    journal_ = Pilot()
    journal_.events, journal_.now = list(events), at
    end(journal_, open_id, at=at)
    return journal_.events


# ---------------------------------------------------------------------------------------------------------------

class NoteTests(unittest.TestCase):
    """Rules 1, 2 and 7: which notes end an open, and what follows from them."""

    def test_a_note_stamped_past_the_deadline_gives_the_present_class_and_final_time(self):
        """X with no submit and no CLOSED read: at its deadline + 2 s the present has it definitely unsent, final at the
        cutoff + 1. The note stamped there gives the same, and a finalized note after it matches."""
        journal_, p, _ = last_open()
        present = only(journal_.fold(now=p.off + DEADLINE + 2 * S)).measurement
        self.assertEqual(present, {"class": "definitely_unsent", "final": True, "final_at_ns": p.off + CUTOFF + 1})
        end(journal_, p.open_id, at=p.off + DEADLINE + 2 * S)
        rec = only(journal_.fold())
        self.assertEqual((rec.measurement, rec.unsent, rec.unsent_by_present), (present, True, False))
        journal_.note("finalized", ref_col=p.ref, final_at_ns=present["final_at_ns"], row=None,
                      **{"class": "definitely_unsent"})
        state = journal_.fold()
        self.assertEqual((state.due_stops, state.invalid_records), ((), ()))
        self.assertEqual(state, replayed(journal_))

    def test_an_unknown_open_an_ended_open_or_a_repeat_is_an_invalid_note(self):
        """A note naming an open the journal does not know, one another open's reservation already ended, or one an
        earlier note ended is pilot-record-invalid (Astra, Q2: never a silent repeat). It takes the normal path, so its
        stop is the note's own and ends the session at its seq. Only the first note ends X."""
        def unknown(j, p, session):
            return end(j, "open-nobody")

        def reserved_since(j, p, session):
            j.reserve("1.299999", j.now + 300 * S, session=session, open_id="open-later")
            return end(j, p.open_id)

        def repeat(j, p, session):
            end(j, p.open_id)
            return end(j, p.open_id)

        for case, still_present in ((unknown, True), (reserved_since, False), (repeat, False)):
            with self.subTest(case=case.__name__):
                journal_, p, session = last_open(closed=True)
                seq = case(journal_, p, session)
                state = journal_.fold()
                self.assertEqual((state.invalid_records, stop_ids(state.due_stops)), ((seq,), invalid_at(seq)))
                self.assertEqual((state.sessions[0]["ended_seq"], state.sessions[0]["ended_reason"]),
                                 (seq, "pilot_record_invalid"))
                self.assertEqual((only(state).unsent, only(state).unsent_by_present), (True, still_present))
                self.assertEqual(state, replayed(journal_))

    def test_the_note_needs_no_column_and_any_other_shape_goes_another_way(self):
        """Rule 1: the note has no column, and one carrying a market_id, a customer_order_ref or both still ends its
        open, as a stop, an ack or a look carrying them is valid (the drain writes none: S3b). A malformed one (an open
        id that is no id, an extra key, a state_after) is an invalid note at its own seq. Under another contract a
        well-formed one is refused as any other note under it is. A KILL carrying its payload is a KILL, and ends no
        open."""
        for columns in ({"market_id": "1.100001"}, {"customer_order_ref": "ref-1"},
                        {"market_id": "1.100001", "customer_order_ref": "ref-1"}):
            with self.subTest(columns=columns):
                journal_, p, _ = last_open()
                end(journal_, p.open_id, **columns)
                state = journal_.fold()
                self.assertEqual((state.invalid_records, state.present_ended_opens), ((), ()))
                self.assertEqual(state, replayed(journal_))
        malformed = {"open id": (open_ended_note("not an id"), {}), "extra key": (open_ended_note("open-1", at=1), {}),
                     "state_after": (open_ended_note("open-1"), {"state_after": "RECORDED"})}
        for name, (payload, columns) in malformed.items():
            with self.subTest(malformed=name):
                journal_, p, _ = last_open()
                seq = journal_.add("note", payload, **columns)
                state = journal_.fold()
                self.assertEqual((state.invalid_records, stop_ids(state.due_stops)), ((seq,), invalid_at(seq)))
                self.assertTrue(only(state).unsent_by_present)
        journal_, p, _ = last_open()
        journal_.add("note", open_ended_note(p.open_id, contract_sha256=sha("another contract")))
        with self.assertRaises(PilotError) as caught:
            journal_.fold()
        self.assertEqual(caught.exception.code, "replacement_contract_not_supported_v1")
        journal_, p, _ = last_open()
        seq = end(journal_, p.open_id, kind="kill")
        state = journal_.fold()
        self.assertEqual((state.sessions[0]["ended_seq"], state.sessions[0]["ended_reason"]), (seq, "kill"))
        self.assertEqual((state.invalid_records, state.present_ended_opens), ((), (p.open_id,)))

    def test_every_later_act_of_an_ended_open_is_refused_or_a_violation(self):
        """Why S2a is safe (design): after the note, a reservation by X is reservation_open_ended, and its record's
        intent and submit are intent_after_open_ended and submit_after_open_ended. The send authority refuses X's
        record even with X live (record_open_ended), where it gives one without the note, and a second decision
        journaled for X is decision_open_ended (classify refuses one as reservation_already_decided, as before S2a).
        An open that reserved and stopped before deciding, once a note ended it, is refused by classify even as the
        live open (reservation_interrupted), where it places without the note; the record journaled anyway is
        decision_open_ended."""
        def reservation(j, p, session):
            j.reserve("1.299999", j.now + 300 * S, session=session, open_id=p.open_id)

        def intent(j, p, session):
            j.intent(p, at=j.now + S)

        def submit(j, p, session):
            j.intent(p, at=j.now + S)
            j.submit(p, at=j.now + S)

        for act, code in ((reservation, "reservation_open_ended"), (intent, "intent_after_open_ended"),
                          (submit, "submit_after_open_ended")):
            with self.subTest(act=act.__name__):
                journal_, p, session = last_open()
                end(journal_, p.open_id)
                act(journal_, p, session)
                self.assertIn(code, codes(journal_.fold()))
        journal_, p, _ = last_open()
        send = {"phase": "send", "now_ns": journal_.now + S, "decision_seq": p.seq}
        self.assertIsNone(pilot.placement_authority(journal_.fold(live_open_id=p.open_id), **send))
        end(journal_, p.open_id)
        self.assertEqual(pilot.placement_authority(journal_.fold(live_open_id=p.open_id), **send), "record_open_ended")
        journal_.inner(p.record, market_id=p.market)
        self.assertIn("decision_open_ended", codes(journal_.fold()))
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = journal_.next_off(session)
        res = journal_.reserve("1.100001", off, session=session, open_id="open-stopped")
        row_body, book_body = bodies("1.100001", off)

        def classified():
            state = journal_.fold(now=off - 295 * S, live_open_id="open-stopped")
            return pilot.classify(C, state, reservation_seq=res, open_id="open-stopped", session_id=session,
                                  registration_receipt_sha256=journal_.registration, customer_order_ref="ref-stopped",
                                  row_body=row_body, book_body=book_body, observed_wall_ns=off - 295 * S,
                                  observed_monotonic_ns=7)

        placed = classified()
        self.assertEqual((placed["refusal"], placed["record"]["outcome"]), (None, "PLACE"))
        end(journal_, "open-stopped")
        self.assertEqual(classified()["refusal"], "reservation_interrupted")
        journal_.inner(placed["record"], market_id="1.100001")
        self.assertIn("decision_open_ended", codes(journal_.fold()))


class PresentEndTests(unittest.TestCase):
    """Rules 3-5: unsent_by_present and present_ended_opens name what only finish() ended."""

    def test_unsent_by_present_is_the_record_only_finish_found_unsent(self):
        """True for the last open's record with no submit, at the fold with no live open. False after the note (unsent
        by the journal), for FAILED(transport_not_sent) (unsent by its own evidence, after a submit or, synthetic,
        with none), with evidence of sending (a send, or, synthetic, a C read with no submit, whose open finish() still
        ends), and with the open live (not unsent)."""
        journal_, p, _ = last_open(closed=True)
        noted, failed, failed_only, sent, read = (copy.deepcopy(journal_) for _ in range(5))
        end(noted, p.open_id)
        failed.intent(p, at=failed.now + S)
        failed.submit(p, at=failed.now + S)
        failed.failed(p, at=failed.now + S)
        failed_only.failed(p, at=failed_only.now + S)
        sent.send(p)
        read.c_read(p, at=read.now + S)
        self.assertEqual(read.fold().present_ended_opens, (p.open_id,))
        for name, state, unsent, by_present in (
                ("no submit", journal_.fold(), True, True),
                ("no submit, live", journal_.fold(live_open_id=p.open_id), False, False),
                ("the note", noted.fold(), True, False),
                ("the note, live", noted.fold(live_open_id=p.open_id), True, False),
                ("FAILED(transport_not_sent)", failed.fold(), True, False),
                ("FAILED(transport_not_sent), no submit", failed_only.fold(), True, False),
                ("sent", sent.fold(), False, False),
                ("a C read, no submit", read.fold(), False, False)):
            with self.subTest(name):
                self.assertEqual((only(state).unsent, only(state).unsent_by_present), (unsent, by_present))
        self.assertEqual(noted.fold(), replayed(noted))

    def test_present_ended_opens_are_the_opens_only_finish_ended(self):
        """(X,) with no live open, and () with X live or after the note. After another open reserves, X is the
        journal's and that open is the present's unless it is the live one. finish() also ends an open whose record
        was sent or that only reserved, so S2c's predicate asks for a record with unsent_by_present as well. The state
        keeps the live_open_id it was folded with."""
        journal_, p, session = last_open()
        noted, later = copy.deepcopy(journal_), copy.deepcopy(journal_)
        end(noted, p.open_id)
        later.reserve("1.299999", later.now + 300 * S, session=session, open_id="open-later")
        for name, state, ended in (("no live open", journal_.fold(), (p.open_id,)),
                                   ("X live", journal_.fold(live_open_id=p.open_id), ()),
                                   ("the note", noted.fold(), ()),
                                   ("a later reservation", later.fold(), ("open-later",)),
                                   ("a later reservation, live", later.fold(live_open_id="open-later"), ())):
            with self.subTest(name):
                self.assertEqual(state.present_ended_opens, ended)
        self.assertEqual((journal_.fold().live_open_id, later.fold(live_open_id="open-later").live_open_id),
                         (None, "open-later"))
        sent = copy.deepcopy(journal_)
        sent.send(p)
        state = sent.fold()
        self.assertEqual((state.present_ended_opens, only(state).unsent_by_present), ((p.open_id,), False))
        self.assertEqual((later.fold().present_ended_opens, [r.unsent_by_present for r in later.fold().place_records]),
                         (("open-later",), [False]))


class PauseTests(unittest.TestCase):
    """The disclosed behaviour change (design S2a; note §10; Yu, Q1)."""

    def test_a_placeholder_pause_does_not_fire_once_the_note_is_journaled(self):
        """R is sent with nothing matched, so its checked C read counts it at 0; then X is placed, and its open closes
        with no submit; then a C read on R contradicts that read (-200). With pause_step_p 400, X's -200 placeholder
        takes equity to the first level, and pause-400 is due at that C read. With the note before it, X counts 0 and
        only R's ledger stop is due. The present ends X either way, so its equity is the same."""
        folds = {}
        for noted in (False, True):
            journal_ = Pilot(pt4=True)
            session = journal_.arm(0)
            r = journal_.place(session)
            journal_.send(r, 0)
            journal_.c_read(r, matched="0.00", cancelled="2.00", price=None)
            x = journal_.place(session)
            if noted:
                end(journal_, x.open_id)
            c2 = journal_.c_read(r, matched="1.00", cancelled="1.00", at=journal_.now + S)
            state = journal_.fold(contract=C400)
            self.assertEqual(state, replayed(journal_, contract=C400))
            folds[noted] = state, (f"ledger-contradiction-{r.seq}-{c2}", "review"), c2
        (without, contradiction, c2), (with_note_, contradiction_, _) = folds[False], folds[True]
        self.assertEqual(without.pause_fired, {-400: c2})
        self.assertEqual(stop_ids(without.due_stops),
                         sorted([contradiction, ("pause-400", "review"), ("pause-400-session", "session")]))
        self.assertEqual((with_note_.pause_fired, stop_ids(with_note_.due_stops)), ({}, [contradiction_]))
        self.assertEqual((without.equity_p, with_note_.equity_p), (-200, -200))


class StampTests(unittest.TestCase):
    """The note carries no time: what a frontier note after it may claim is fixed by that note's own stamp."""

    def test_a_finalized_note_after_the_note_is_judged_at_its_own_stamp(self):
        """Stamp pin: X has no CLOSED read by the cutoff, so it is final at the cutoff + 1 once the clock passes it.
        After the note, a finalized note stamped at the cutoff mismatches; one stamped 1 µs later, the journal's
        resolution, matches."""
        journal_, p, _ = last_open()
        end(journal_, p.open_id)
        cutoff = p.off + CUTOFF
        claim = {"final_at_ns": cutoff + 1, "row": None, "class": "definitely_unsent"}
        self.assertEqual(only(journal_.fold(now=cutoff + 1000)).measurement,
                         {"class": "definitely_unsent", "final": True, "final_at_ns": cutoff + 1})
        for stamp, matches in ((cutoff, False), (cutoff + 1000, True)):
            with self.subTest(after_cutoff_ns=stamp - cutoff):
                stamped = copy.deepcopy(journal_)
                seq = stamped.note("finalized", at=stamp, ref_col=p.ref, **claim)
                state = stamped.fold()
                mismatch = [(f"finalized-mismatch-{seq}", "review")]
                self.assertEqual(stop_ids(state.due_stops), [] if matches else mismatch)
                self.assertEqual(state, replayed(stamped))

    def test_the_v2_cases_lower_no_ledger_at_a_deadline_the_note_crosses(self):
        """Astra v2 #2's two cases, with X's note stamped 2 s past R's deadline. v2's witness (place report 1.00, C
        1.00 / 0.50 / 0 / 0) fails the capture check, so rule d never applies: -200 worst_case before the deadline and
        after it. A valid partial (C 1.00 / 1.00 / 0 / 0) is -100 under rule d before and -100 under rule c after. The
        note replays as the present at its stamp."""
        for name, cancelled, before, after in (
                ("witness", "0.50", (-200, "worst_case", None), (-200, "worst_case", None)),
                ("partial", "1.00", (-100, "checked_current_read", "d"), (-100, "result_unknown", "c"))):
            with self.subTest(name):
                journal_ = Pilot(pt4=True)
                r = journal_.place(journal_.arm(0))
                journal_.send(r, 100)
                journal_.c_read(r, matched="1.00", cancelled=cancelled)
                journal_.journal_due(journal_.fold())
                for stop in journal_.fold().active_stops:
                    journal_.ack(stop["stop_id"], cause=True)
                deadline = r.off + DEADLINE
                x = journal_.place(journal_.arm(7), off=deadline - 10 * MIN)
                self.assertLess(journal_.now, deadline)
                states = {"before": journal_.fold(now=deadline), "after": journal_.fold(now=deadline + 2 * S)}
                end(journal_, x.open_id, at=deadline + 2 * S)
                states["the note"] = journal_.fold()
                for when, state in states.items():
                    ledger = state.place_records[0].ledger
                    self.assertEqual((ledger["value_p"], ledger["reason"], (ledger["basis"] or {}).get("rule")),
                                     before if when == "before" else after, when)
                    self.assertEqual(state.violations, ())
                self.assertEqual(as_written(states["the note"]), as_written(states["after"]))


class RuleSevenTests(unittest.TestCase):
    """The rule 7 pin: a valid note appended at t, past other records' triggers in (J, t] or not, folds as the fold its
    writer read, fold(prefix, now=t) with no live open: equity, the pause, due stops, violations and sessions, and each
    record's measurement, ledger and execution. The two differ only where S2a says they do: the note's open is the
    journal's, not the present's, so its records lose unsent_by_present.

    These pins check that equality, not rule 7's order. Every ledger change their stamps cross is a rise, as no trigger
    lowers a ledger after S2a-0, and a rise fires no pause and changes nothing else compared here in either order, so
    they pass with the note's open ended after its stamp moves the clock too. Only
    test_the_note_ends_its_open_before_its_stamp_crosses_a_trigger, inside the pass, pins the order."""

    def written_as_read(self, events, open_id, at):
        read = pilot.fold(list(events), contract=C, now_ns=at)
        self.assertIn(open_id, read.present_ended_opens)
        self.assertTrue(any(rec.open_id == open_id and rec.unsent_by_present for rec in read.place_records))
        written = pilot.fold(with_note(events, open_id, at), contract=C, now_ns=at)
        self.assertEqual(written.invalid_records, read.invalid_records)
        self.assertNotIn(open_id, written.present_ended_opens)
        self.assertFalse(any(rec.open_id == open_id and rec.unsent_by_present for rec in written.place_records))
        return as_written(written) == as_written(read)

    def test_the_note_ends_its_open_before_its_stamp_crosses_a_trigger(self):
        """Rule 7's order, seen inside the pass, since no journal is known where the other order folds otherwise
        (design S2a; after S2a-0): probe_rise's X, ended by a note at J + 30 days, is already the journal's unsent
        record at £0 at every pause check the note's apply() makes, the first at R's deadline + 1, the trigger that
        changes R's ledger, as in the fold its writer read."""
        shape = fresh.probe_rise()
        events = list(shape.journal.events)
        folder = fresh.through(events, len(events))
        r_deadline = shape.r.off + DEADLINE + 1
        self.assertLess(folder.clock, r_deadline)
        events = pilot._check_events(with_note(events, shape.x.open_id, folder.clock + 30 * DAY_NS))
        x = next(rec for rec in folder.records if rec.ref == shape.x.ref)
        label, clock = folder.last_seq, folder.clock      # the fold's place before the note
        steps, pause_check, evaluate = [], folder.pause_check, folder.evaluate

        def observed(seq):
            steps.append(("pause", folder.at_ns, x.open_end, x.value_p))
            pause_check(seq)

        def evaluated(rec, at_seq, at_ns):
            if rec is x:
                steps.append(("x", at_seq, at_ns, folder.at_seq, folder.at_ns))
            evaluate(rec, at_seq, at_ns)

        folder.pause_check, folder.evaluate = observed, evaluated
        folder.apply(events[-1])
        checks = [step[1:] for step in steps if step[0] == "pause"]
        # X is evaluated first, at the saved fold clock under the previous event's label, before any pause check or
        # trigger: not at the note's seq or stamp (Astra, S2a review).
        self.assertEqual(steps[0], ("x", label, clock, label, clock))
        self.assertEqual(checks[0][0], r_deadline)
        self.assertEqual({(open_end, value) for _, open_end, value in checks}, {("journal", 0)})
        self.assertGreater(len(checks), 2)                # R's deadline, X's own triggers and the note itself

    def test_probe_adv_with_the_note_past_a_deadline_folds_as_its_writer_read(self):
        """probe_adv's shapes before Y2 reserves: X, placed with no submit just before A's deadline, is ended by a note
        stamped just past that deadline, at t0, or at J + 30 days, past every trigger."""
        for variant in ("late_c", "second_c", "second_closed", "control"):
            shape = fresh.probe_adv(variant)
            events = list(shape.journal.events)
            folder = fresh.through(events, len(events))
            self.assertEqual(drain_would_end(folder), [shape.x.open_id])
            self.assertLess(folder.clock, shape.deadline)
            for at in ((shape.deadline // 1000 + 1) * 1000, shape.t0, folder.clock + 30 * DAY_NS):
                with self.subTest(variant=variant, past_a_deadline_ns=at - shape.deadline):
                    self.assertTrue(self.written_as_read(events, shape.x.open_id, at))

    def test_every_prefix_a_drain_would_end_folds_as_its_writer_read(self):
        """At each prefix of the pinned, generated and drained journals where a drain would write a note, between opens
        (not before the open's own send), for a stamp an hour before J, just past the next pending trigger of a record
        of another open, and J + 30 days. Some stamp crosses another open's trigger, and some crosses one that changes
        a ledger (probe_rise among them), so the pin is not empty; each such change is a rise (see the class)."""
        journals = list(fresh.pinned()) + [fresh.generated(seed) for seed in range(fresh.GENERATED)] + [
            fresh.drained(seed) for seed in range(fresh.GENERATED)]
        checked, found, cases, crossing, moving = [], [], 0, 0, 0
        for label, events, warm in journals:
            start = max((fresh.common_prefix(events, other) for other in checked), default=0)
            checked.append(events)
            acting = last_acts(events)
            for n, folder in fresh.journal_order(events):
                if n <= start:
                    continue
                for open_id in (o for o in drain_would_end(folder) if acting.get(o, 0) <= n):
                    others = sorted(t for rec in folder.records if rec.open_id != open_id for t in rec.triggers)
                    stamps = [folder.clock - HOUR, folder.clock + 30 * DAY_NS]
                    if others:
                        stamps.append((others[0] // 1000 + 1) * 1000)
                    for at in stamps:
                        cases += 1
                        crossing += bool(others) and others[0] <= at
                        moving += bool(fresh.trigger_moves(folder, at))
                        if not self.written_as_read(events[:n], open_id, at):
                            found.append((label, n, open_id, at - folder.clock))
        self.assertEqual(found[:3], [], f"{len(found)} notes fold otherwise than their writer read")
        self.assertGreater(min(cases, crossing, moving), 0)


if __name__ == "__main__":
    unittest.main()
