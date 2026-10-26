"""P1-10 S8a: the look note, which changes no fold input (design v4 S8a; note §12:188; contract:192; IR-7).

A look is journaled as {"type": "look", "description_sha256"}, with no column. A valid look is listed in
PilotState.looks as (seq, description_sha256), and _Folder.apply returns before the clock, last_seq, the time triggers,
the records' evaluation, S2a-0's commission method check, the pause and N: the contract says a look "cannot change the
rule, inclusion, N or the threshold", and a look that moved the clock could change inclusion (the design's probe, on
astra_7's shape). A malformed look is an invalid note like any other, a look under another contract is refused as any
other note under it is, and only a note is a look (a KILL carrying a look's payload is a KILL). Nothing reads looks
yet: S6c's departures and S6d's after_claim will.

On 5cf5bb3, before this slice, a look note is pilot_record_invalid, and its stamp moves the clock as any event's does.

The look-free twin of a journal is the same builder calls without the looks, so every seq the journal names is the
builder's own; renumbered() maps a state's seqs onto it. The generated and pinned journals of S2a-0's freshness pin are
rebuilt with looks written between their events (LookingPilot). Synthetic: these show the fold, not that pilot-look
(S8b) writes a look or that a look's file holds no figure.
"""

import contextlib
import copy
import dataclasses
import functools
import json
import random
import unittest
from unittest.mock import patch

from betangel_research.betfair import journal, pilot
from betangel_research.betfair.journal import JournalError
from betangel_research.betfair.pilot import DAY_NS, PilotError

from test_betfair_pilot import C, DAY0, HOUR, MIN, S, Pilot, book_item, only, replayed, sha
import test_betfair_p110_fold_fresh as fresh

LOOKED_SEEDS = max(fresh.GENERATED // 2, 10)        # generated journals rebuilt with looks: 0 and 8 start near a pause


# -- writing looks ------------------------------------------------------------------------------------------------

def look_note(text):
    """A look note's payload, written out rather than built by pilot_note, so that 5cf5bb3, which has no look type,
    journals and folds it too (as pilot_record_invalid)."""
    return {"pilot": {"v": 1, "type": "look", "contract_sha256": C.sha256, "description_sha256": sha(text)}}


def look(journal_, text="a look", *, at=None):
    """A valid look note, stamped at `at` (the builder's clock by default). The builder's clock stays where it was, as
    the fold's does, so the events after it are stamped as they would be without it."""
    now = journal_.now
    seq = journal_.add("note", look_note(text), at=now if at is None else at)
    journal_.now = now
    return seq


def look_first(journal_, text="before the registration"):
    """A valid look as seq 1, stamped as the registration. The builder's first events (the registration and its
    receipts) name no other event's seq, so moving them up one changes nothing else."""
    journal_.events.insert(0, dict(journal_.events[0], payload=look_note(text)))
    for seq, event in enumerate(journal_.events, start=1):
        event.update(seq=seq, event_id=f"ev-{seq:032x}", monotonic_ns=seq)
    return 1


class LookingPilot(Pilot):
    """The builder, writing a look before about one event in five, and always right after Yu's commission_verified
    (so between it and the CLOSED read that turns the method on in probe_stale and probe_adv): stamped as the event, at
    the builder's clock, a week and 2 s past it (past a deadline), 400 days past it (past every trigger and the final
    date) or before the journal begins. Its draws come from a stream of its own and the builder's clock stays where it
    was, so every other event has the stamp and columns the plain builder gives it; only the digests and the seqs it
    names move with the looks."""

    def __init__(self, *, looks_rng, **kw):
        self.looks_rng = looks_rng
        super().__init__(**kw)

    def add(self, kind, payload, *, at=None, **columns):
        last = (self.events[-1]["payload"].get("pilot") or {}).get("type") if self.events else None
        if last == "commission_verified" or self.looks_rng.random() < 0.2:
            now = self.now
            stamp = self.looks_rng.choice((now + S if at is None else at, now, now + 7 * DAY_NS + 2 * S,
                                           now + 400 * DAY_NS, DAY0 - 9 * DAY_NS))
            super().add("note", look_note(f"look {len(self.events)}"), at=stamp)
            self.now = now
        return super().add(kind, payload, at=at, **columns)


@contextlib.contextmanager
def looking(name):
    """fold_fresh's builder replaced by LookingPilot, with looks drawn from the stream name, and its cached loser
    preamble by an uncached one built with looks. Only uncached functions may run inside: a cached journal built here
    would carry looks into fold_fresh's own tests."""
    builder = functools.partial(LookingPilot, looks_rng=random.Random(f"looks {name}"))
    with patch.object(fresh, "Pilot", builder), patch.object(fresh, "preamble", lambda: fresh.losers(70)):
        yield


def look_seqs(events):
    return [e["seq"] for e in events if e["kind"] == "note" and (e["payload"].get("pilot") or {}).get("type") == "look"]


def inputs(folder):
    """What the events after this point read of the folder: every field but looks, each record by its fields in
    records and by its index wherever else it is held. A copy, so a later apply() leaves it as it was."""
    def held(value):
        if isinstance(value, pilot._Rec):
            return ("record", value.index)
        if isinstance(value, dict):
            return {key: held(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [held(item) for item in value]
        return value

    fields = {name: held(value) for name, value in vars(folder).items() if name not in ("contract", "looks", "records")}
    return copy.deepcopy(dict(fields, records=[vars(rec) for rec in folder.records]))


# -- the same state without the looks -----------------------------------------------------------------------------

def seq_map(events, looks):
    """{seq: the event's seq once the looks are removed} for every event but the looks."""
    looks = set(looks)
    kept = [e["seq"] for e in events if e["seq"] not in looks]
    return {seq: index for index, seq in enumerate(kept, start=1)}


def renumbered(state, new):
    """The state with every event seq it holds moved to that event's seq in the journal without its looks (new), and
    no looks. These are all the places a PilotState holds an event's seq, so they are the only fields besides looks in
    which a journal's fold may differ from its look-free twin's: each reservation's seq and decision_seq; each PLACE
    record's reservation_seq, decision_seq, its decision's reservation_seq, submit_seq, place report, C and L reads,
    execution (final_seq, c_seq, l_seq), BSP read, ledger basis source and contradicted_seq; each SKIP's seq and
    reservation_seq; each session's seq and ended_seq; the PT4 and L-verifying receipts; where each pause level fired
    and was acknowledged; each stop's seq, source_seq, acked_seq and the seqs in its id (a pause's id holds its level,
    not a seq); violations, invalid notes, resolved gaps (by stop id), ending notes and the ending's seq. The due stops,
    and the due tail of the active ones, are sorted again as the fold sorts them, since a renumbered id can sort
    elsewhere as a string (-99 after -100). ids() takes every all-digit part of an id for a seq: true of the ids these
    journals raise, not of a gateway session stop anchored on an open id's hex."""
    def at(seq):
        return None if seq is None else new[seq]

    def ids(text):
        return "-".join(str(new[int(part)]) if part.isdigit() else part for part in text.split("-"))

    def due(items):
        return tuple(sorted(items, key=lambda s: (s["source_seq"] or 0, s["stop_id"], s["scope"])))  # due_stops()

    def stop(item):
        moved = dict(item, stop_id=item["stop_id"] if item["reason"] == "pause" else ids(item["stop_id"]),
                     source_seq=at(item["source_seq"]))
        moved.update({key: at(item[key]) for key in ("seq", "acked_seq") if key in item})
        return moved

    def read(item):
        return None if item is None else dict(item, seq=new[item["seq"]])

    def source(seq):
        return seq if seq in (None, 0) else new[seq]          # 0: an unsent record's basis names no observation

    def record(rec):
        basis, execution = rec.ledger["basis"], rec.execution
        return dataclasses.replace(
            rec, reservation_seq=new[rec.reservation_seq], decision_seq=new[rec.decision_seq],
            record=dict(rec.record, reservation_seq=new[rec.record["reservation_seq"]]), submit_seq=at(rec.submit_seq),
            place_report=read(rec.place_report), c_reads=tuple(map(read, rec.c_reads)),
            l_rows=tuple(map(read, rec.l_rows)),
            execution=dict(execution, **{key: at(execution[key]) for key in ("final_seq", "c_seq", "l_seq")
                                         if key in execution}),
            bsp=dict(rec.bsp, read_seq=at(rec.bsp["read_seq"]), closed_read=read(rec.bsp["closed_read"])),
            ledger=dict(rec.ledger, basis=None if basis is None else dict(basis, source=source(basis["source"]))),
            contradicted_seq=at(rec.contradicted_seq))

    return dataclasses.replace(
        state,
        reservations={market: dict(entry, seq=new[entry["seq"]], decision_seq=at(entry["decision_seq"]))
                      for market, entry in state.reservations.items()},
        place_records=tuple(map(record, state.place_records)),
        skip_records=tuple(dict(skip, seq=new[skip["seq"]], reservation_seq=new[skip["reservation_seq"]])
                           for skip in state.skip_records),
        sessions=tuple(dict(s, seq=new[s["seq"]], ended_seq=at(s["ended_seq"])) for s in state.sessions),
        pt4_receipt_seq=at(state.pt4_receipt_seq), l_verified_seq=at(state.l_verified_seq),
        pause_fired={level: at(seq) for level, seq in state.pause_fired.items()},
        pause_acked={level: at(seq) for level, seq in state.pause_acked.items()},
        stops=tuple(map(stop, state.stops)), due_stops=due(map(stop, state.due_stops)),
        active_stops=(tuple(stop(s) for s in state.active_stops if s["seq"] is not None)
                      + due(stop(s) for s in state.active_stops if s["seq"] is None)),
        violations=tuple(dict(v, seq=new[v["seq"]]) for v in state.violations),
        invalid_records=tuple(new[seq] for seq in state.invalid_records),
        resolved_gaps={ids(stop_id): new[seq] for stop_id, seq in state.resolved_gaps.items()},
        ending_notes=tuple(dict(note, seq=new[note["seq"]]) for note in state.ending_notes),
        ending=None if state.ending is None else dict(state.ending, seq=at(state.ending["seq"])),
        looks=())


def last_open_id(events):
    found = [e["payload"]["pilot"]["open_id"] for e in events
             if (e["payload"].get("pilot") or {}).get("type") == "reservation"]
    return found[-1] if found else None


# -- the shapes ---------------------------------------------------------------------------------------------------

def astra_7(*, look_at=None, inner=None):
    """astra_7's shape: C 3.44 and a CLOSED book, then an L at 3.06 stamped at the deadline. A note between them
    (a look, or the inner given) stamped at deadline + 2 s would cross the deadline before the L if it moved the clock.
    -> (journal, record, deadline, the note's seq or None)."""
    journal_ = Pilot(pt4=True)
    _, p, _ = journal_.classify(journal_.arm(0))
    journal_.send(p)
    deadline = p.off + C.deadline_s * pilot.NS
    journal_.c_read(p, price="3.44")
    journal_.closed(p, sp="3.10")
    seq = None
    if inner is not None:
        now = journal_.now
        seq = journal_.add("note", inner, at=deadline + 2 * S)
        journal_.now = now
    elif look_at is not None:
        seq = look(journal_, at=look_at(deadline))
    journal_.l_row(p, price="3.06", at=deadline)
    return journal_, p, deadline, seq


def rich(with_looks):
    """A journal that puts an event seq in each place renumbered() moves but the pause (generated seed 0 fires and
    acknowledges one), with looks before the registration, between a report and the captures after it (two, with one
    description), between a ledger gap and the L row that resolves it, a month and a year ahead of the clock, and
    last; or the same journal without them. -> (journal, look seqs)."""
    journal_, looks = Pilot(pt4=True), []

    def look_(text, **kw):
        if with_looks:
            looks.append(look(journal_, text, **kw))

    if with_looks:
        looks.append(look_first(journal_))
    s0 = journal_.arm(0)
    look_("after the ARM")
    a = journal_.place(s0)
    journal_.lose(a)
    look_("a month ahead", at=a.off + 30 * DAY_NS)
    journal_.classify(s0, market="1.555555", book=book_item("1.555555", isMarketDataDelayed=True))   # SKIP
    _, b, _ = journal_.classify(s0)
    journal_.send(b)
    look_("between b's report and its C read")
    look_("between b's report and its C read")                    # the same file, logged again
    journal_.c_read(b, price="3.44")
    journal_.closed(b, sp="3.10")
    journal_.closed(a, ours="WINNER", at=a.off + 20 * MIN)       # contradicts a: its stop ends s0
    journal_.l_row(a, profit="-1.50")                             # a cleared figure gap in a's ledger
    look_("between a's gap and the L row that resolves it")
    journal_.l_row(a)                                             # agrees, so it resolves the gap
    deadline = b.off + C.deadline_s * pilot.NS
    rec = next(r for r in journal_.fold(now=deadline + 2 * S).place_records if r.ref == b.ref)
    journal_.note("finalized", at=deadline + 2 * S, ref_col=b.ref, final_at_ns=rec.measurement["final_at_ns"], row=3,
                  **{"class": "eligible"})
    journal_.note("bsp_decided", ref_col=b.ref, decided_at_ns=rec.bsp["decided_at_ns"], outcome="bsp",
                  actual_sp="3.1", read_seq=rec.bsp["read_seq"])
    journal_.note("finalized", ref_col="ref-none", final_at_ns=deadline, row=3, **{"class": "eligible"})  # invalid
    journal_.journal_due(journal_.fold())
    for stop in journal_.fold().active_stops:
        if stop["scope"] == "review":
            journal_.ack(stop["stop_id"], cause=True)
    look_("before the next ARM")
    c = journal_.place(journal_.arm(9))
    look_("after c's decision")
    journal_.note("violation", code="yu_typed_code", source_seq=c.seq)
    journal_.note("ending", reason="yu_written", code="yu_ended_in_writing")
    journal_.note("finalized", ref_col="ref-none", final_at_ns=deadline, row=3, **{"class": "eligible"})  # left due
    look_("last, a year ahead", at=journal_.now + 400 * DAY_NS)
    return journal_, looks


# -- the tests ----------------------------------------------------------------------------------------------------

class LookCase(unittest.TestCase):
    """The assertions both groups share."""

    def assert_same_without_looks(self, events, twin, looks, *, nows, lives=(None,)):
        """At each now and live open, the journal's fold lists its looks, equals its look-free twin's once renumbered,
        and equals its own replay (a JSON round trip)."""
        new = seq_map(events, looks)
        digests = {e["seq"]: e["payload"]["pilot"]["description_sha256"] for e in events if e["seq"] in looks}
        for now in nows:
            for live in lives:
                with self.subTest(now=now, live_open_id=live):
                    state = pilot.fold(list(events), contract=C, now_ns=now, live_open_id=live)
                    self.assertEqual(state.looks, tuple((seq, digests[seq]) for seq in looks))
                    self.assertEqual(renumbered(state, new),
                                     pilot.fold(list(twin), contract=C, now_ns=now, live_open_id=live))
                    self.assertEqual(state, pilot.fold(json.loads(json.dumps(list(events))), contract=C, now_ns=now,
                                                       live_open_id=live))

    def assert_looks_change_nothing(self, events):
        """Each look is listed and leaves every other field of the folder as it was (inputs()): the clock, last_seq,
        the triggers and all else a later event reads. -> how many looks were stamped at or past the next trigger,
        where a look that moved the clock would cross it."""
        looks, folder, changed, past = set(look_seqs(events)), pilot._Folder(C, None), [], 0
        for event in pilot._check_events(list(events)):
            if event["seq"] not in looks:
                folder.apply(event)
                continue
            before = inputs(folder)
            past += folder.next_trigger is not None and event["_ns"] >= folder.next_trigger
            folder.apply(event)
            if folder.looks[-1][0] != event["seq"] or inputs(folder) != before:
                changed.append(event["seq"])
        self.assertEqual(changed[:3], [], f"{len(changed)} looks changed the fold")
        return past

    def assert_fresh(self, journals_):
        """S2a-0's freshness pin at every prefix of each (label, events), with no live open and with the prefix's
        latest open live; a prefix another journal began with is checked once, as fold_fresh checks it."""
        checked, found = [], []
        for label, events in journals_:
            start = max((fresh.common_prefix(events, other) for other in checked), default=0)
            checked.append(events)
            for n, folder in fresh.journal_order(events):
                for live in (None, fresh.latest_open(folder)) if n > start else ():
                    stale = fresh.stale(folder, live)
                    if stale:
                        found.append((label, n, live, stale[0]))
        self.assertEqual(found[:3], [], f"{len(found)} stale prefix folds")


class LookNoteTests(LookCase):

    def test_a_look_past_the_deadline_leaves_inclusion_as_it_was(self):
        """The design's probe: without a look the record is row 2, measurement_unresolved (the L stamped at the
        deadline counts). A look stamped at deadline + 2 s keeps that: it moves neither the fold clock nor last_seq.
        On 5cf5bb3 the look is pilot_record_invalid and moves the clock past the deadline before the L, so the record
        freezes on C alone, row 3 and eligible."""
        plain, _, deadline, _ = astra_7()
        journal_, _, _, seq = astra_7(look_at=lambda deadline: deadline + 2 * S)
        for case, j in (("without the look", plain), ("with it", journal_)):
            with self.subTest(case):
                state = j.fold(now=deadline + 2 * S)
                rec = only(state)
                self.assertEqual((rec.execution["row"], rec.measurement["class"]), (2, "measurement_unresolved"))
                self.assertEqual((state.invalid_records, state.due_stops), ((), ()))
                self.assertEqual(state, replayed(j, now=deadline + 2 * S))
        before, after = fresh.through(journal_.events, seq - 1), fresh.through(journal_.events, seq)
        self.assertEqual((after.clock, after.last_seq, after.next_trigger),
                         (before.clock, seq - 1, before.next_trigger))
        self.assertLess(after.clock, deadline)
        self.assertEqual(journal_.fold().looks, ((seq, sha("a look")),))
        self.assert_same_without_looks(journal_.events, plain.events, [seq],
                                       nows=(deadline + 2 * S, deadline + 8 * DAY_NS))

    def test_a_trigger_after_a_look_keeps_its_label(self):
        """C 3.44 and a CLOSED read, a look an hour before the deadline, then a settle an hour after it: the settle
        crosses the deadline + 1, where the record freezes on C (row 3). The trigger is labelled with the event before
        the look, as in the journal without it, so final_seq is the CLOSED read's seq. On 5cf5bb3 the look takes
        last_seq, so final_seq is the look's seq (and the look is an invalid note)."""
        journals = {}
        for with_look in (False, True):
            journal_ = Pilot(pt4=True)
            _, p, _ = journal_.classify(journal_.arm(0))
            journal_.send(p)
            journal_.c_read(p, price="3.44")
            closed = journal_.closed(p, sp="3.10")
            deadline = p.off + C.deadline_s * pilot.NS
            seq = look(journal_, at=deadline - HOUR) if with_look else None
            journal_.settle(p, at=deadline + HOUR)
            rec = only(journal_.fold())
            with self.subTest(with_look=with_look):
                self.assertEqual((rec.execution["row"], rec.execution["final_seq"], rec.execution["final_at_ns"]),
                                 (3, closed, deadline + 1))
                self.assertEqual((rec.measurement["class"], rec.measurement["final_at_ns"]), ("eligible", deadline + 1))
            journals[with_look] = journal_, seq
        (journal_, seq), (plain, _) = journals[True], journals[False]
        self.assertEqual(seq, closed + 1)
        self.assert_same_without_looks(journal_.events, plain.events, [seq],
                                       nows=(journal_.now, journal_.now + 8 * DAY_NS))

    def test_a_journal_folds_as_its_look_free_twin_renumbered(self):
        """Removing the looks and renumbering the seqs gives an equal state, at J and at J + 400 days, with no live
        open and with the last open live, and each look leaves the folder as it was. Both looks with one description
        are listed. Before renumbering, the folds differ in exactly these fields, each because it holds the seq of an
        event after a look: reservations, place_records, skip_records, sessions, pt4_receipt_seq, l_verified_seq,
        stops, due_stops, active_stops, violations, invalid_records, resolved_gaps, ending_notes and ending; and in
        looks. Every other field is equal as it stands: the clock, equity, slots, day counts, the commission method,
        the profit basis and the rest. (The pause fields are left to the generated journals.)"""
        journal_, looks = rich(True)
        twin, _ = rich(False)
        self.assertEqual(len(looks), 9)
        self.assertEqual(len(journal_.events), len(twin.events) + len(looks))
        self.assertEqual([(e["kind"], e["recorded_at"]) for e in journal_.events if e["seq"] not in looks],
                         [(e["kind"], e["recorded_at"]) for e in twin.events])
        self.assert_same_without_looks(journal_.events, twin.events, looks, nows=(twin.now, twin.now + 400 * DAY_NS),
                                       lives=(None, last_open_id(twin.events)))
        self.assert_looks_change_nothing(journal_.events)
        state, plain = journal_.fold(), twin.fold()
        self.assertEqual(len({digest for _, digest in state.looks}), len(looks) - 1)
        differ = {f.name for f in dataclasses.fields(state) if getattr(state, f.name) != getattr(plain, f.name)}
        self.assertEqual(differ, {"reservations", "place_records", "skip_records", "sessions", "pt4_receipt_seq",
                                  "l_verified_seq", "stops", "due_stops", "active_stops", "violations",
                                  "invalid_records", "resolved_gaps", "ending_notes", "ending", "looks"})
        self.assertEqual(state.now_ns, twin.now)
        # The places rich() is there to reach.
        (skip,), session = state.skip_records, state.sessions[0]
        a, b, c = state.place_records
        self.assertEqual((skip["code"], session["ended_reason"], a.contradicted_seq is not None, b.bsp["read_seq"]
                          is not None, c.submit_seq), ("book_delayed", "ledger_contradiction", True, True, None))
        ((gap, resolved),) = state.resolved_gaps.items()
        self.assertTrue(gap.startswith(f"cleared-figure-gap-{a.decision_seq}-"))
        self.assertIn(resolved - 1, looks)
        self.assertEqual([s["reason"] for s in state.due_stops], ["pilot_record_invalid"])
        self.assertEqual(state.ending["reason"], "breach")

    def test_due_stops_sorted_by_id_keep_the_fold_order_without_the_looks(self):
        """Two refusals of one reservation behind 93 looks, at seqs 99 and 100, and at 6 and 7 without the looks. The
        fold sorts due stops by id as a string, so the looked journal lists -100 before -99 and its twin -6 before -7;
        renumbered, they are the twin's, in its order."""
        def refused_twice(with_looks):
            journal_ = Pilot(pt4=True)
            session = journal_.arm(0)
            _, _, res = journal_.classify(session, journal_it=False)              # a reservation with no record
            looks = [look(journal_, f"look {n}") for n in range(93)] if with_looks else []
            for _ in range(2):
                journal_.note("refusal", session_id=session, open_id="open-1", reservation_seq=res, code="book_stale",
                              row_sha256=None, book_sha256=None)
            return journal_, looks

        (journal_, looks), (twin, _) = refused_twice(True), refused_twice(False)
        self.assertEqual([s["stop_id"] for s in journal_.fold().due_stops], ["book-stale-5-100", "book-stale-5-99"])
        self.assertEqual([s["stop_id"] for s in twin.fold().due_stops], ["book-stale-5-6", "book-stale-5-7"])
        self.assert_same_without_looks(journal_.events, twin.events, looks, nows=(twin.now,), lives=(None, "open-1"))

    def test_a_look_logged_last_leaves_j_where_it_was(self):
        """pilot-look's ordinary case (S8b): a look logged after the last capture, an hour after J and days before the
        next trigger. J ignores it (IR-7): the fold clock stays at J, a fold at now J is at J, and fold(1..s) at J
        equals fold(1..s-1) but for the look. On 5cf5bb3 the look is an invalid note and J moves to its stamp."""
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p)
        journal_.c_read(p, price="3.44")
        journal_.closed(p, sp="3.10")
        j = journal_.now
        seq = look(journal_, at=j + HOUR)
        before, after = fresh.through(journal_.events, seq - 1), fresh.through(journal_.events, seq)
        self.assertGreater(before.next_trigger, j + HOUR)
        self.assertEqual((before.clock, after.clock), (j, j))
        state = journal_.fold(now=j)
        self.assertEqual(state.now_ns, j)
        self.assertEqual(state.looks, ((seq, sha("a look")),))
        self.assertEqual(dataclasses.replace(state, looks=()), pilot.fold(journal_.events[:-1], contract=C, now_ns=j))
        self.assertEqual(state, replayed(journal_, now=j))
        self.assert_looks_change_nothing(journal_.events)

    def test_a_prefix_ending_in_a_look_folds_as_the_prefix_before_it(self):
        """S6d's premise: whenever event s is a valid look, fold(1..s) equals fold(1..s-1) but for the look listed, at
        J(s-1) and 8 days later. S* can then never be a look's seq."""
        journal_, looks = rich(True)
        events = journal_.events
        for seq in looks:
            j = fresh.through(events, seq - 1).clock if seq > 1 else DAY0
            for now in (j, j + 8 * DAY_NS):
                with self.subTest(seq=seq, now=now):
                    before = pilot.fold(events[:seq - 1], contract=C, now_ns=now)
                    after = pilot.fold(events[:seq], contract=C, now_ns=now)
                    self.assertEqual(after.looks, before.looks + ((seq, events[seq - 1]["payload"]["pilot"]
                                                                   ["description_sha256"]),))
                    self.assertEqual(dataclasses.replace(after, looks=before.looks), before)

    def test_a_malformed_look_is_an_invalid_note(self):
        """Each takes the normal invalid-note path: listed in invalid_records with its review stop, not in looks, and,
        like any other note, it moves the clock: at deadline + 2 s it freezes astra_7's record on C alone."""
        good = look_note("a look")["pilot"]
        cases = {"digest not hex": dict(good, description_sha256="x" * 64),
                 "digest upper case": dict(good, description_sha256=good["description_sha256"].upper()),
                 "digest short": dict(good, description_sha256=good["description_sha256"][:63]),
                 "digest None": dict(good, description_sha256=None), "digest int": dict(good, description_sha256=1),
                 "digest missing": {k: v for k, v in good.items() if k != "description_sha256"},
                 "extra field": dict(good, seen="balance"), "v 2": dict(good, v=2),
                 "contract digest bad": dict(good, contract_sha256="0" * 63)}
        payloads = {label: {"pilot": inner} for label, inner in cases.items()}
        payloads["beside pilot"] = {"pilot": good, "note": "x"}
        for label, payload in payloads.items():
            with self.subTest(case=label):
                journal_, _, deadline, seq = astra_7(inner=payload)
                state = journal_.fold(now=deadline + 2 * S)
                rec = only(state)
                self.assertEqual(state.invalid_records, (seq,))
                self.assertEqual([s["stop_id"] for s in state.due_stops], [f"pilot-record-invalid-{seq}"])
                self.assertEqual((rec.execution["row"], rec.measurement["class"]), (3, "eligible"))
                self.assertEqual(state, replayed(journal_, now=deadline + 2 * S))
                self.assertEqual(state.looks, ())
        journal_ = Pilot(pt4=True)
        seq = journal_.add("note", {"pilot": good}, state_after="RECORDED")      # a column no pilot record may have
        self.assertEqual(journal_.fold().invalid_records, (seq,))
        self.assertEqual(journal_.fold().looks, ())
        self.assertEqual({"pilot": good}, pilot.pilot_note("look", C.sha256, description_sha256=sha("a look")))

    def test_only_a_note_is_a_look(self):
        """A KILL or HALT whose payload is a look's {"pilot": ...} is a KILL or HALT: the journal accepts that payload
        for either kind (it checks an intent's or a settle's keys, so neither can carry it), and the fold ends the
        session there as with the usual payload, lists no look and finds no invalid note."""
        payload = look_note("on a KILL")
        for kind, code in (("intent", "intent_payload_keys_must_be_exact"),
                           ("settle", "settle_commission_p_must_be_non_negative_integer")):
            with self.subTest(kind=kind), self.assertRaises(JournalError) as caught:
                journal.prepare_event(kind, payload)
            self.assertEqual(str(caught.exception), code)
        for kind, usual in (("kill", {"reason": "kill", "scope": "own"}),
                            ("halt", {"reason": "operator", "scope": "global"})):
            journal.prepare_event(kind, payload)
            folds = []
            for carried in (payload, usual):
                journal_ = Pilot(pt4=True)
                journal_.place(journal_.arm(0))
                seq = journal_.add(kind, carried)
                folds.append(journal_.fold())
            with self.subTest(kind=kind):
                state, plain = folds
                self.assertEqual((state.sessions[0]["ended_seq"], state.sessions[0]["ended_reason"]), (seq, kind))
                self.assertEqual((state.looks, state.invalid_records), ((), ()))
                self.assertEqual(state, plain)

    def test_a_look_before_the_registration_or_under_another_contract_is_as_any_other_note(self):
        """The fold checks no registration for a look, as for a stop or an ending: one before the registration is a
        look, and one alone is the empty journal's fold with a look. Under another contract sha, a well-formed look
        is refused as a well-formed stop is (replacement_contract_not_supported_v1), and a malformed one under it is
        an invalid note, as a malformed stop is."""
        journal_, twin = Pilot(pt4=True), Pilot(pt4=True)
        seq = look_first(journal_)
        for j in (journal_, twin):
            j.lose(j.place(j.arm(0)))
        listed = ((1, sha("before the registration")),)
        self.assertEqual(journal_.fold().invalid_records, ())
        self.assertEqual(journal_.fold().looks, listed)
        self.assert_same_without_looks(journal_.events, twin.events, [seq], nows=(twin.now,))
        self.assertEqual(pilot.fold(journal_.events[:1], contract=C, now_ns=DAY0),
                         dataclasses.replace(pilot.fold([], contract=C, now_ns=DAY0), looks=listed))
        other = sha("another contract")
        inners = {"look": {"description_sha256": sha("a look")},
                  "stop": {"stop_id": "pilot-record-invalid-1", "scope": "review", "reason": "pilot_record_invalid",
                           "level_p": None, "source_seq": 1}}
        for kind, fields in inners.items():
            for well_formed in (True, False):
                with self.subTest(kind=kind, well_formed=well_formed):
                    inner = dict(fields, v=1, type=kind, contract_sha256=other)
                    if not well_formed:
                        inner["extra"] = 1
                    journal_ = Pilot(pt4=True)
                    seq = journal_.inner(inner)
                    if well_formed:
                        with self.assertRaises(PilotError) as caught:
                            journal_.fold()
                        self.assertEqual(caught.exception.code, "replacement_contract_not_supported_v1")
                    else:
                        self.assertEqual(journal_.fold().invalid_records, (seq,))
                        self.assertEqual(journal_.fold().looks, ())


class LookFreshnessTests(LookCase):
    """S2a-0's freshness with looks present: fold_fresh's pinned shapes and generated journals, rebuilt with looks
    between their events (LookingPilot), are fresh at every prefix, and the generated ones fold as the same journals
    without their looks, renumbered."""

    def test_the_ir25_shapes_with_looks_turn_the_method_on_where_they_did_and_are_fresh(self):
        """fold_fresh's pinned shapes, with looks: the method turns on, and moves W or A, at the event it does without
        them (CommissionMethodTests), every prefix is fresh, and each look leaves the folder as it was. Where Yu's
        commission_verified comes first, a look sits right before the CLOSED read that turns the method on. Each shape
        draws its looks from the same stream, so the shapes that begin with the same losers share their looks there
        too, and that prefix is checked once."""
        shapes = []
        for note_first in (True, False):
            with looking("pinned"):
                shape = fresh.probe_stale(note_first)
            events = shape.journal.events
            shapes.append((f"probe_stale note_first={note_first}", events))
            with self.subTest(probe="probe_stale", note_first=note_first):
                self.assertEqual(shape.turned_on - 1 in look_seqs(events), note_first)
                self.assertEqual(fresh.ledger(fresh.through(events, shape.turned_on - 1), shape.w.ref),
                                 ("W", -200, "commission_unverified"))
                self.assertEqual(fresh.ledger(fresh.through(events, shape.turned_on), shape.w.ref), ("S", 392, None))
                self.assertEqual(shape.journal.fold().violations, ())
        for variant, after in {"late_c": ("W", -200, "contradiction"), "second_c": ("W", -200, "contradiction"),
                               "second_closed": ("W", -200, "contradiction"), "control": ("S", -100, None)}.items():
            with looking("pinned"):
                shape = fresh.probe_adv(variant)
            events = shape.journal.events
            shapes.append((f"probe_adv {variant}", events))
            with self.subTest(probe="probe_adv", variant=variant):
                self.assertIn(shape.closed_seq - 1, look_seqs(events))
                self.assertEqual(fresh.ledger(fresh.through(events, shape.closed_seq - 1), shape.a.ref),
                                 ("W", -100, "commission_unverified"))
                self.assertEqual(fresh.ledger(fresh.through(events, shape.closed_seq), shape.a.ref), after)
        with looking("pinned"):
            shape = fresh.probe_record()
        events = shape.journal.events
        shapes.append(("probe_record", events))
        self.assertEqual(fresh.ledger(fresh.through(events, shape.b.seq - 1), shape.w.ref),
                         ("W", -200, "commission_unverified"))
        self.assertEqual(fresh.ledger(fresh.through(events, shape.b.seq), shape.w.ref), ("S", 392, None))
        for label, build in (("probe_rise", fresh.probe_rise), ("probe_gw_head", fresh.probe_gw_head)):
            with looking("pinned"):
                shapes.append((label, build().journal.events))
        self.assertTrue(all(look_seqs(events) for _, events in shapes))
        for label, events in shapes:
            with self.subTest(shape=label):
                self.assert_looks_change_nothing(events)
        self.assert_fresh(shapes)

    def test_generated_journals_with_looks_fold_as_without_them_and_are_fresh(self):
        """For each seed, with looks drawn from a stream of the seed's own, the generator writes the same journal with
        and without the looks (the same events in the same order and at the same stamps), the head folds to the same
        state renumbered, at J and 400 days later, with no live open and the latest open live, each look leaves the
        folder as it was, and every prefix is fresh. Some look is stamped at or past the next time trigger, where a
        look that moved the clock would cross it, and some journal fires a pause, so renumbered() moves pause_fired
        too."""
        journals, past_a_trigger, paused = [], 0, 0
        for seed in range(LOOKED_SEEDS):
            with looking(f"generated {seed}"):
                events = list(fresh.journal(seed))
            _, plain, _ = fresh.generated(seed)
            looks = look_seqs(events)
            with self.subTest(seed=seed):
                self.assertTrue(looks)
                self.assertEqual([(e["kind"], e["recorded_at"], e["market_id"], e["customer_order_ref"])
                                  for e in events if e["seq"] not in looks],
                                 [(e["kind"], e["recorded_at"], e["market_id"], e["customer_order_ref"])
                                  for e in plain])
                now = max(pilot.parse_utc_ns(e["recorded_at"]) for e in plain)
                self.assert_same_without_looks(events, plain, looks, nows=(now, now + 400 * DAY_NS),
                                               lives=(None, last_open_id(plain)))
                past_a_trigger += self.assert_looks_change_nothing(events)
            journals.append((f"seed {seed}", events))
            paused += bool(pilot.fold(list(plain), contract=C, now_ns=DAY0).pause_fired)
        self.assertGreater(min(past_a_trigger, paused), 0)
        self.assert_fresh(journals)


if __name__ == "__main__":
    unittest.main()
