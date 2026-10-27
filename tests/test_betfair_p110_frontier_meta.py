"""P1-10 S2b: frontier metadata at each note's position (design v4 S2b; Astra #4, Astra #2's J; IR-5, IR-7, IR-9).

S7's "matching" must be the fold's own decision at the note's position, kept apart from presence (Astra #4). Every
valid bsp_decided or finalized note for a known record is now an entry of that record, {type, seq, ok, frozen, claim,
differs}, decided once at the note's position by _frontier_claim_matches: ok when no claim field differs, never changed
by later evidence, and the mismatch stop is set from that same decision. differs names the fields, never their values
(IR-9). frontier_note_valid holds the shape checks the fold and P1-10's writer share. After finish(), a type is current
when some ok entry of it still matches the record at now, and an empty frontier_current fails closed. A record that
reuses another's ref is not addressable, since frontier notes are routed by ref to the first record (IR-5).
PilotState.journal_clock_ns is J, the fold clock after the last event, which valid looks never move (IR-7).

No existing stop, class or fold value changes. The fold runs with these entries and with main dda38a8's two handlers
and finish() (Before), on pinned, generated, drained and written journals and on every journal test_betfair_pilot's
frontier pins fold, and every field but S2b's is equal, in journal order at each frontier note and in the finished fold
at J, J + 8 days and J + 400 days, with no live open and the latest open live.

On dda38a8, before this slice, the fold keeps no entry, and RecordState and PilotState have none of these fields.

Synthetic: WritingPilot's drain only stands in for P1-10's (S2c, S3b, S3c), which does not exist yet.
"""

import dataclasses
from decimal import Decimal
import functools
import json
import random
import unittest
from unittest.mock import patch

from betangel_research.betfair import pilot
from betangel_research.betfair.pilot import DAY_NS, NS

import test_betfair_pilot
from test_betfair_pilot import C, DAY0, HOUR, MIN, S, Pilot, only, replayed, synthetic
import test_betfair_p110_fold_fresh as fresh
import test_betfair_p110_look as looks

DEADLINE = C.deadline_s * NS
TYPES = ("bsp_decided", "finalized")
NAMES = {"finalized": ("final", "class", "final_at_ns", "row"),
         "bsp_decided": ("decided", "actual_sp", "read_seq", "decided_at_ns")}
S2B_FIELDS = ("addressable", "frontier", "frontier_current")
WRITTEN = max(fresh.GENERATED // 4, 5)              # generated journals rebuilt with a frontier writer


# -- the fold without S2b -----------------------------------------------------------------------------------------

class Before(pilot._Folder):
    """The fold as main dda38a8 had it: the bodies of its two frontier handlers and of finish(), as written there but
    for the module prefix. The rest of the folder is this tree's (note_decision's addressability and fold()'s J among
    it, pinned on their own), so a record's frontier stays () and frontier_current empty, and every field but S2b's
    must equal the fold with the entries."""

    def note_bsp_decided(self, inner, event, seq, ns):
        rec = self.by_ref.get(event["customer_order_ref"])
        if rec is None or (inner["outcome"] == "bsp") != (inner["actual_sp"] is not None):
            self.invalid_note(seq)
            return
        if rec.bsp is None:
            bsp = pilot.freeze_bsp(self.books.get(rec.market_id, []), selection_id=rec.selection_id,
                                   off_ns=rec.off_ns, now_ns=self.clock, cutoff_s=self.contract.bsp_cutoff_s)
            if bsp["decided"]:
                rec.bsp = bsp
        claimed = None if inner["actual_sp"] is None else Decimal(inner["actual_sp"])
        if (rec.bsp is None or rec.bsp["bsp"] != claimed or rec.bsp["read_seq"] != inner["read_seq"]
                or rec.bsp["decided_at_ns"] != inner["decided_at_ns"]):      # the time must match exactly (R2-5)
            self.imply_review("bsp_decided_mismatch", seq)
        return {rec.index}

    def note_finalized(self, inner, event, seq, ns):
        rec = self.by_ref.get(event["customer_order_ref"])
        if rec is None or (inner["row"] is None and inner["class"] not in pilot.UNSENT_CLASSES):
            self.invalid_note(seq)
            return
        self.evaluate(rec, seq, self.clock)
        measurement = rec.measurement
        row = None if rec.execution is None else rec.execution["row"]
        row_ok = inner["row"] == row or (inner["row"] is None and measurement["class"] in pilot.UNSENT_CLASSES)
        if not (measurement["final"] and measurement["final_at_ns"] <= self.clock and row_ok
                and measurement["class"] == inner["class"] and measurement["final_at_ns"] == inner["final_at_ns"]):
            self.imply_review("finalized_mismatch", seq)
        return {rec.index}

    def finish(self, now_ns):
        now = now_ns if self.clock is None else max(self.clock, now_ns)
        self.at_seq, self.at_ns = self.last_seq, self.clock
        for index in sorted(self.end_opens(self.live_open_id, now if self.clock is None else self.clock, "present")):
            self.evaluate(self.records[index], self.last_seq, self.clock)
        self.advance(now, self.last_seq)
        self.at_seq, self.at_ns = self.last_seq, now
        for rec in self.records:
            self.evaluate(rec, self.last_seq, now)
        self.pause_check(self.last_seq)
        self.n_check(self.last_seq, None)
        return now


def fold_before(events, **kw):
    with patch.object(pilot, "_Folder", Before):
        return pilot.fold(list(events), contract=C, **kw)


def without_s2b(state):
    """The state with S2b's fields at their defaults."""
    return dataclasses.replace(state, journal_clock_ns=None, place_records=tuple(
        dataclasses.replace(rec, addressable=True, frontier=(), frontier_current=frozenset())
        for rec in state.place_records))


def old_view(folder):
    """Every field of the folder but S2b's: each record by its fields but S2b's in records, and by its index wherever
    else it is held."""
    def held(value):
        if isinstance(value, pilot._Rec):
            return ("record", value.index)
        if isinstance(value, dict):
            return {key: held(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [held(item) for item in value]
        return value

    fields = {name: held(value) for name, value in vars(folder).items() if name not in ("contract", "records")}
    return dict(fields, records=[{name: held(value) for name, value in vars(rec).items() if name not in S2B_FIELDS}
                                 for rec in folder.records])


# -- reading the entries ------------------------------------------------------------------------------------------

def entries(rec):
    return [(e["type"], e["seq"], e["ok"], e["frozen"], e["differs"]) for e in rec.frontier]


def record_of(state, ref):
    return next(rec for rec in state.place_records if rec.ref == ref)


def stop_ids(state):
    """Every stop id the fold knows: journaled, or implied and due."""
    return {s["stop_id"] for s in state.stops + state.due_stops}


def mismatch_id(entry):
    return pilot.stop_id_for(f"{entry['type']}_mismatch", entry["seq"])


def still_matches(entry, rec, now):
    """The test's own reading of an entry's claim against the record as folded (a RecordState) at now."""
    claim = entry["claim"]
    if entry["type"] == "finalized":
        m = rec.measurement
        row_ok = claim["row"] == rec.execution["row"] or (claim["row"] is None and m["class"] in pilot.UNSENT_CLASSES)
        return (m["final"] and m["final_at_ns"] <= now and m["class"] == claim["class"]
                and m["final_at_ns"] == claim["final_at_ns"] and row_ok)
    bsp, sp = rec.bsp, None if claim["actual_sp"] is None else Decimal(claim["actual_sp"])
    return (bsp["decided"] and bsp["bsp"] == sp and bsp["read_seq"] == claim["read_seq"]
            and bsp["decided_at_ns"] == claim["decided_at_ns"])


# -- writing the notes --------------------------------------------------------------------------------------------

def supported(rec):
    """{type: claim} for each frontier note the fold supports for this record (a RecordState) as it stands, with the
    fold's own values: bsp_decided once its BSP is decided, finalized once it is final and not unsent_by_present, with
    row None for a class final without the execution table (S2c rules 3 and 4, without their waits)."""
    found, bsp, m = {}, rec.bsp, rec.measurement
    if bsp["decided"]:
        found["bsp_decided"] = {"decided_at_ns": bsp["decided_at_ns"], "outcome": "none" if bsp["bsp"] is None else
                                "bsp", "actual_sp": None if bsp["bsp"] is None else pilot.money_text(bsp["bsp"]),
                                "read_seq": bsp["read_seq"]}
    if m["final"] and not rec.unsent_by_present:
        found["finalized"] = {"final_at_ns": m["final_at_ns"], "class": m["class"],
                              "row": None if m["class"] in pilot.UNSENT_CLASSES else rec.execution["row"]}
    return found


def write(journal_, ref, kind, claim, *, at=None):
    return journal_.note(kind, at=at, ref_col=ref, **claim)


def c_only():
    """AstraReviewR3's c_only: C 3.44 and a CLOSED read at 3.10, row 3 once the deadline passes. -> (journal, placed,
    session)"""
    journal_ = Pilot(pt4=True)
    session = journal_.arm(0)
    p = journal_.place(session)
    journal_.send(p)
    journal_.c_read(p, price="3.44")
    journal_.closed(p, sp="3.10")
    return journal_, p, session


def settled():
    """c_only, then an L at 3.44: eligible, row 1, final at the L. -> (journal, placed, session)"""
    journal_, p, session = c_only()
    journal_.l_row(p, price="3.44")
    return journal_, p, session


def both_notes(journal_, p, **changes):
    """The bsp_decided and finalized notes the fold supports for p, at the builder's clock + 1 s, each claim with the
    changes given for its fields. -> {type: seq}"""
    claims = supported(record_of(journal_.fold(now=journal_.now + S), p.ref))
    return {kind: write(journal_, p.ref, kind, {key: changes.get(key, value) for key, value in claims[kind].items()})
            for kind in TYPES}


class WritingPilot(Pilot):
    """The builder, with a frontier writer standing in for P1-10's drain: before about one event in four that a drain
    could precede (an ARM, a reservation, a capture, a receipt or a settle; never inside a decision or a send) it folds
    the journal at the builder's clock + 1 s with no live open and writes, stamped there, open_ended for each open only
    finish() ended that has a record with unsent_by_present, then for each record each note the fold supports whose type
    is not current and has no wrong entry with its stop active (S2c, roughly). Its draws come from a stream of its own:
    one note in twelve has one claim field changed, so its stop ends the session it falls in, and for one record in
    fifty that is not final yet it writes a finalized note anyway (row 3, eligible, at the deadline + 1). The builder's
    clock moves with the notes, so later events are stamped after them."""

    BEFORE = ("session_start", "reservation", "capture_c", "capture_l", "capture_book", "capture_commission",
              "pt_receipt", "commission_verified")

    def __init__(self, *, notes_rng, **kw):
        self.notes_rng, self.writing = notes_rng, False
        super().__init__(**kw)

    def add(self, kind, payload, *, at=None, **columns):
        before = kind == "settle" or (payload.get("pilot") or {}).get("type") in self.BEFORE
        if before and self.events and not self.writing and self.notes_rng.random() < 0.25:
            self.writing = True
            try:
                drain(self, self.now + S, self.notes_rng)
            finally:
                self.writing = False
        return super().add(kind, payload, at=at, **columns)


def changed(kind, claim, rng):
    """The claim with one field changed, keeping a valid shape."""
    key = rng.choice([k for k in claim if k != "outcome"])
    if key in ("final_at_ns", "decided_at_ns"):
        return dict(claim, **{key: claim[key] + rng.choice((-1, 1, HOUR))})
    if key == "read_seq":
        return dict(claim, read_seq=(claim["read_seq"] or 0) + 1)
    if key == "actual_sp":
        return dict(claim, actual_sp="9.99", outcome="bsp")
    if key == "row":
        return dict(claim, row=rng.choice([r for r in (1, 2, 3, 4, 5) if r != claim["row"]]))
    return dict(claim, **{"class": rng.choice([c for c in pilot.CLASS_ORDER if c != claim["class"]
                                               and (claim["row"] is not None or c in pilot.UNSENT_CLASSES)])})


def drain(journal_, at, rng=None):
    """One drain at `at` (see WritingPilot); with no rng every note has the fold's values. -> the seqs written"""
    state, written = journal_.fold(now=at), []
    for open_id in state.present_ended_opens:
        if any(rec.open_id == open_id and rec.unsent_by_present for rec in state.place_records):
            written.append(journal_.note("open_ended", at=at, open_id=open_id))
    if written:
        state = journal_.fold(now=at)
    active = {s["stop_id"] for s in state.active_stops}
    for rec in state.place_records:
        if not rec.addressable:
            continue
        claims = supported(rec)
        if rng is not None and "finalized" not in claims and rng.random() < 0.02:
            claims["finalized"] = {"final_at_ns": rec.deadline_ns + 1, "row": 3, "class": "eligible"}
        for kind, claim in claims.items():
            if kind in rec.frontier_current or any(not e["ok"] and mismatch_id(e) in active
                                                   for e in rec.frontier if e["type"] == kind):
                continue
            if rng is not None and rng.random() < 1 / 12:
                claim = changed(kind, claim, rng)
            written.append(write(journal_, rec.ref, kind, claim, at=at))
    return written


@functools.lru_cache(maxsize=None)
def written(seed):
    """fold_fresh's journal(seed) built by WritingPilot (its loser preamble too, uncached), then the close: in the even
    seeds, Yu's typed violation names a record whose finalized note is current and whose class is not yet
    protocol_violation, a day after the head; Yu journals the due stops and acknowledges every review stop, with its
    cause written; and a drain at the head + 400 days, past every trigger, writes the fold's own values. -> (events
    before the close, events after it)"""
    rng = random.Random(f"frontier {seed}")
    builder = functools.partial(WritingPilot, notes_rng=rng)
    with patch.object(fresh, "Pilot", builder), patch.object(fresh, "preamble", lambda: fresh.losers(70)):
        events = fresh.journal(seed)
    journal_ = Pilot()
    journal_.events = list(events)
    journal_.now = max(pilot.parse_utc_ns(e["recorded_at"]) for e in events)
    current = [rec for rec in journal_.fold().place_records
               if "finalized" in rec.frontier_current and rec.measurement["class"] != "protocol_violation"]
    if current and seed % 2 == 0:
        journal_.note("violation", at=journal_.now + DAY_NS, code="yu_typed_code",
                      source_seq=rng.choice(current).decision_seq)
    journal_.journal_due(journal_.fold())
    for stop in journal_.fold().active_stops:
        if stop["scope"] == "review":
            journal_.ack(stop["stop_id"], cause=True)
    drain(journal_, journal_.now + 400 * DAY_NS)
    return tuple(events), tuple(journal_.events)


def lives(events):
    """None and the latest open the journal reserved."""
    found = [e["payload"]["pilot"]["open_id"] for e in events
             if (e["payload"].get("pilot") or {}).get("type") == "reservation"]
    return (None, found[-1]) if found else (None,)


def frontier_seqs(events):
    return [e["seq"] for e in events if e["kind"] == "note" and (e["payload"].get("pilot") or {}).get("type")
            in TYPES]


def sampled(seqs, count=6):
    """About count of the seqs, spread from the first on."""
    return seqs[::max(len(seqs) // count, 1)]


PINS = {"AstraReview": ("test_astra_7",), "BspFrontier": ("test_",), "AstraReviewR2": ("test_r2_3", "test_r2_5"),
        "AstraReviewR3": ("test_r3_1",)}


def pin_folds():
    """Every fold test_betfair_pilot's frontier pins make (test_astra_7, BspFrontier, R2-3, R2-5, R3-1), each once: the
    pins run as written, with pilot.fold recording what it is given. -> (their result, [(events, now_ns,
    live_open_id), ...])"""
    seen, real, result = {}, pilot.fold, unittest.TestResult()

    def recording(events, *, contract, now_ns, live_open_id=None):
        if contract is C:
            copied = json.loads(json.dumps(events))
            seen.setdefault(json.dumps([copied, now_ns, live_open_id]), (copied, now_ns, live_open_id))
        return real(events, contract=contract, now_ns=now_ns, live_open_id=live_open_id)

    with patch.object(pilot, "fold", recording):
        for name, prefixes in PINS.items():
            case = getattr(test_betfair_pilot, name)
            for test in unittest.defaultTestLoader.getTestCaseNames(case):
                if test.startswith(prefixes):
                    case(test).run(result)
    return result, list(seen.values())


# -- the tests ----------------------------------------------------------------------------------------------------

class FrontierCase(unittest.TestCase):

    def assert_replays(self, journal_, *, now=None):
        """fold == replayed (a JSON round trip), entries included, with no live open and with the latest open live."""
        for live in lives(journal_.events):
            with self.subTest(live_open_id=live):
                self.assertEqual(journal_.fold(now=now, live_open_id=live),
                                 replayed(journal_, now=journal_.now if now is None else now, live_open_id=live))

    def assert_unchanged(self, events, *, prefixes=()):
        """With and without S2b: the folders equal but for S2b's fields after every frontier note and at the end of
        the pass, and the folds equal but for S2b's fields, with no live open and the latest open live: the journal's
        at J, J + 8 days and J + 400 days, and each prefix given at its own J and 8 days on. finish() changes no entry
        and no record's addressability (Astra v3-5). -> the head's fold at J with no live open"""
        events, notes = list(events), set(frontier_seqs(events))
        new, old = pilot._Folder(C, None), Before(C, None)
        at_end = {}
        for event in pilot._check_events(events):
            new.apply(event)
            old.apply(event)
            if event["seq"] in notes or event["seq"] == len(events):
                self.assertEqual(old_view(new), old_view(old), f"journal order differs at seq {event['seq']}")
            if event["seq"] in prefixes or event["seq"] == len(events):
                at_end[event["seq"]] = new.clock, {rec.decision_seq: (rec.addressable, list(rec.frontier))
                                                   for rec in new.records}
        head = None
        for n in sorted(set(prefixes) | {len(events)}):
            j, kept = at_end[n]
            for now in (j, j + 8 * DAY_NS) + ((j + 400 * DAY_NS,) if n == len(events) else ()):
                for live in lives(events[:n]):
                    with self.subTest(prefix=n, now=now, live_open_id=live):
                        state = pilot.fold(events[:n], contract=C, now_ns=now, live_open_id=live)
                        self.assertEqual(without_s2b(state),
                                         without_s2b(fold_before(events[:n], now_ns=now, live_open_id=live)))
                        self.assertEqual({rec.decision_seq: (rec.addressable, list(rec.frontier))
                                          for rec in state.place_records}, kept)
                        if n == len(events) and now == j and live is None:
                            head = state
        return head

    def assert_one_decision(self, state):
        """Each entry's flag, names and stop are one decision, and frontier_current is the types whose newest ok
        entry still matches, which is the types with any ok entry that does (_frontier_current's docstring). The fold's
        matcher reads the frozen record as the test does, and a record is addressable exactly when no earlier decision
        used its ref."""
        ids, first = stop_ids(state), {}
        for rec in sorted(state.place_records, key=lambda rec: rec.decision_seq):
            first.setdefault(rec.ref, rec.decision_seq)
        for rec in state.place_records:
            self.assertEqual(rec.addressable, first[rec.ref] == rec.decision_seq)
            self.assertEqual(pilot._frontier_current(rec, state.now_ns), rec.frontier_current)
            for entry in rec.frontier:
                self.assertEqual(set(entry), {"type", "seq", "ok", "frozen", "claim", "differs"})
                self.assertEqual(entry["ok"], not entry["differs"])
                self.assertEqual(entry["ok"], mismatch_id(entry) not in ids)
                self.assertTrue(set(entry["differs"]) <= set(NAMES[entry["type"]]), entry["differs"])
                self.assertEqual(entry["differs"], tuple(n for n in NAMES[entry["type"]] if n in entry["differs"]))
                if entry["ok"]:
                    self.assertTrue(entry["frozen"])
                if not entry["frozen"]:
                    self.assertEqual(entry["differs"], (NAMES[entry["type"]][0],))
                self.assertEqual(set(entry["claim"]), set(pilot.NOTE_FIELDS[entry["type"]]))
                self.assertEqual(not pilot._frontier_claim_matches(entry["type"], entry["claim"], rec, state.now_ns),
                                 bool(still_matches(entry, rec, state.now_ns)))
            newest ={kind: [e for e in rec.frontier if e["type"] == kind and e["ok"]][-1:] for kind in TYPES}
            self.assertEqual(rec.frontier_current, {kind for kind in TYPES
                                                    if newest[kind] and still_matches(newest[kind][0], rec,
                                                                                      state.now_ns)})
            self.assertEqual(rec.frontier_current, {e["type"] for e in rec.frontier
                                                    if e["ok"] and still_matches(e, rec, state.now_ns)})
            if not rec.addressable:
                self.assertEqual((rec.frontier, rec.frontier_current), ((), frozenset()))


class EntryTests(FrontierCase):
    """The design's cases (S2b, Tests)."""

    def test_two_valid_notes_give_two_ok_entries_both_current(self):
        """A bsp_decided note and a finalized note with the fold's values: two ok entries, frozen, with differs (),
        both types current, and no stop, at the head and 400 days on."""
        journal_, p, _ = settled()
        seqs = both_notes(journal_, p)
        for now in (journal_.now, journal_.now + 400 * DAY_NS):
            state = journal_.fold(now=now)
            rec = only(state)
            self.assertEqual(entries(rec), [("bsp_decided", seqs["bsp_decided"], True, True, ()),
                                            ("finalized", seqs["finalized"], True, True, ())])
            self.assertEqual(rec.frontier_current, frozenset(TYPES))
            self.assertEqual((state.due_stops, state.invalid_records), ((), ()))
            self.assert_one_decision(state)
        self.assertEqual([e["claim"] for e in only(journal_.fold()).frontier],
                         [{"decided_at_ns": p.off + 5 * MIN, "outcome": "bsp", "actual_sp": "3.10",
                           "read_seq": only(journal_.fold()).bsp["read_seq"]},
                          {"final_at_ns": p.off + 15 * MIN, "row": 1, "class": "eligible"}])
        self.assert_replays(journal_)

    def test_a_wrong_class_is_not_current_after_its_ack(self):
        """Probe A: a finalized note with the wrong class is not ok, names ("class",), and sets its stop at its seq.
        After the ACK no stop is active, yet finalized is not current."""
        journal_, p, _ = settled()
        bad = write(journal_, p.ref, "finalized", dict(supported(only(journal_.fold()))["finalized"],
                                                       **{"class": "missing_bsp"}))
        state = journal_.fold()
        self.assertEqual(entries(only(state)), [("finalized", bad, False, True, ("class",))])
        self.assertEqual([(s["stop_id"], s["source_seq"]) for s in state.due_stops],
                         [(f"finalized-mismatch-{bad}", bad)])
        journal_.journal_due(state)
        journal_.ack(f"finalized-mismatch-{bad}", cause=True)
        state = journal_.fold()
        self.assertEqual((state.active_stops, state.due_stops), ((), ()))
        self.assertEqual(entries(only(state)), [("finalized", bad, False, True, ("class",))])
        self.assertEqual(only(state).frontier_current, frozenset())
        self.assert_one_decision(state)
        self.assert_replays(journal_)

    def test_a_premature_row_none_note_stays_not_ok_after_the_open_ends(self):
        """The shape of test_r3_1_an_open_end_counts_where_the_journal_shows_it: no submit and a CLOSED read, and a
        row-None definitely_unsent note before anything shows the open ended. It is not ok ("final"; not frozen), and
        stays so after the open ends, by another open's reservation or an open_ended note. Its claim then matches the
        record, yet finalized is not current: presence is not matching (Astra #4). The same note written after the end
        is ok and current."""
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        _, p, _ = journal_.classify(session)
        journal_.closed(p, sp="3.10")
        derived = only(journal_.fold()).measurement
        claim = {"final_at_ns": derived["final_at_ns"], "row": None, "class": "definitely_unsent"}
        early = write(journal_, p.ref, "finalized", claim)
        self.assertEqual(entries(only(journal_.fold())), [("finalized", early, False, False, ("final",))])
        ends = {"reservation": lambda j: j.reserve("1.299999", j.now + 300 * S, session=session, open_id="open-later"),
                "open_ended": lambda j: j.note("open_ended", open_id=p.open_id)}
        for how, end in ends.items():
            live_then = "open-later" if how == "reservation" else p.open_id
            with self.subTest(ended_by=how):
                ended = Pilot()
                ended.events, ended.now = list(journal_.events), journal_.now
                end(ended)
                for live in (None, live_then):
                    rec = only(ended.fold(live_open_id=live))
                    self.assertEqual((rec.measurement, supported(rec)["finalized"]), (derived, claim))
                    self.assertEqual((entries(rec), rec.frontier_current),
                                     ([("finalized", early, False, False, ("final",))], frozenset()))
                later = write(ended, p.ref, "finalized", claim)
                for live in (None, live_then):
                    state = ended.fold(live_open_id=live)
                    self.assertEqual(entries(only(state)), [("finalized", early, False, False, ("final",)),
                                                            ("finalized", later, True, True, ())])
                    self.assertEqual(only(state).frontier_current, frozenset({"finalized"}))
                    self.assert_one_decision(state)
                self.assert_replays(ended)

    def test_a_premature_deadline_note_is_not_current_once_its_claim_comes_true(self):
        """AstraReviewR3's premature deadline note: row 3, eligible, at the deadline + 1, written at off + 6 min. A
        week later the record is final just as claimed, yet the entry is not ok and finalized is not current; a note
        written then is ok and makes it current."""
        journal_, p, _ = c_only()
        claim = {"final_at_ns": p.off + DEADLINE + 1, "row": 3, "class": "eligible"}
        note = write(journal_, p.ref, "finalized", claim, at=p.off + 6 * MIN)
        late = journal_.fold(now=p.off + DEADLINE + 2 * S)
        self.assertEqual(supported(only(late))["finalized"], claim)
        self.assertEqual((entries(only(late)), only(late).frontier_current),
                         ([("finalized", note, False, False, ("final",))], frozenset()))
        again = write(journal_, p.ref, "finalized", claim, at=p.off + DEADLINE + 2 * S)
        state = journal_.fold()
        self.assertEqual(entries(only(state)), [("finalized", note, False, False, ("final",)),
                                                ("finalized", again, True, True, ())])
        self.assertEqual(only(state).frontier_current, frozenset({"finalized"}))
        self.assert_one_decision(state)
        self.assert_replays(journal_)

    def test_a_later_violation_leaves_the_ok_entry_ok_but_not_current(self):
        """Probe B: an eligible note, then Yu's typed violation naming the record: the entry stays ok, with no stop,
        but finalized is not current, since the class is now protocol_violation. A protocol_violation note (row None)
        makes it current again; the bsp_decided entry was current throughout."""
        journal_, p, _ = settled()
        seqs = both_notes(journal_, p)
        journal_.note("violation", code="yu_typed_code", source_seq=p.seq)
        state = journal_.fold()
        rec = only(state)
        self.assertEqual(rec.measurement["class"], "protocol_violation")
        self.assertEqual(entries(rec)[1], ("finalized", seqs["finalized"], True, True, ()))
        self.assertEqual((rec.frontier_current, state.due_stops), (frozenset({"bsp_decided"}), ()))
        claim = supported(rec)["finalized"]
        self.assertEqual((claim["class"], claim["row"]), ("protocol_violation", None))
        again = write(journal_, p.ref, "finalized", claim)
        state = journal_.fold()
        self.assertEqual(entries(only(state))[2], ("finalized", again, True, True, ()))
        self.assertEqual((only(state).frontier_current, state.due_stops), (frozenset(TYPES), ()))
        self.assert_one_decision(state)
        self.assert_replays(journal_)

    def test_a_failed_report_moves_an_unsent_records_final_time_once(self):
        """The one move with no class change (_frontier_current): a no-submit record whose open a reservation ended is
        definitely_unsent, final at its last own event, and a note there is ok and current. A FAILED(transport_not_sent)
        report the fold accepts without a submit moves its final time to that report: the entry stays ok, finalized is
        no longer current, and a note at the new time makes it current again (Astra, S2b review)."""
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        _, p, _ = journal_.classify(session)
        journal_.closed(p, sp="3.10")
        journal_.reserve("1.299999", journal_.now + 300 * S, session=session, open_id="open-later")
        first = supported(only(journal_.fold()))["finalized"]
        self.assertEqual(first["class"], "definitely_unsent")
        note = write(journal_, p.ref, "finalized", first)
        self.assertEqual(only(journal_.fold()).frontier_current, frozenset({"finalized"}))
        failed = journal_.failed(p, at=journal_.now + S)
        state = journal_.fold()
        rec = only(state)
        moved = supported(rec)["finalized"]
        self.assertEqual((moved["class"], moved["final_at_ns"] > first["final_at_ns"]), ("definitely_unsent", True))
        self.assertEqual(entries(rec), [("finalized", note, True, True, ())])
        self.assertEqual((rec.frontier_current, state.due_stops), (frozenset(), ()))
        self.assertGreater(failed, note)
        again = write(journal_, p.ref, "finalized", moved)
        state = journal_.fold()
        self.assertEqual(entries(only(state)),
                         [("finalized", note, True, True, ()), ("finalized", again, True, True, ())])
        self.assertEqual((only(state).frontier_current, state.due_stops), (frozenset({"finalized"}), ()))
        self.assert_one_decision(state)
        self.assert_replays(journal_)

    def test_a_bsp_decided_note_then_a_differing_closed_read_stays_current(self):
        """BspFrontier.test_matching_note_then_a_correction's inputs: a note at 3.1 for a read of 3.10 (compared as a
        Decimal), then a CLOSED read of 3.12: the entry is ok and stays current, the BSP stays 3.10."""
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p)
        journal_.c_read(p, price="3.44")
        read = journal_.closed(p, sp="3.10")
        note = journal_.note("bsp_decided", ref_col=p.ref, decided_at_ns=p.off + 5 * MIN, outcome="bsp",
                             actual_sp="3.1", read_seq=read)
        journal_.closed(p, sp="3.12", at=journal_.now + S)
        for now in (journal_.now, journal_.now + 400 * DAY_NS):
            state = journal_.fold(now=now)
            self.assertEqual(entries(only(state)), [("bsp_decided", note, True, True, ())])
            self.assertEqual((only(state).bsp["bsp"], only(state).frontier_current),
                             (Decimal("3.10"), frozenset({"bsp_decided"})))
            self.assert_one_decision(state)
        self.assert_replays(journal_)

    def test_an_ok_note_stays_current_after_a_wrong_one_and_its_ack(self):
        """The "any" rule: an ok eligible note, then a wrong one (another final time) and its ACK: finalized is still
        current, by the first entry."""
        journal_, p, _ = settled()
        good = both_notes(journal_, p)["finalized"]
        claim = supported(only(journal_.fold()))["finalized"]
        bad = write(journal_, p.ref, "finalized", dict(claim, final_at_ns=claim["final_at_ns"] + 1))
        journal_.journal_due(journal_.fold())
        journal_.ack(f"finalized-mismatch-{bad}", cause=True)
        state = journal_.fold()
        self.assertEqual(entries(only(state))[1:], [("finalized", good, True, True, ()),
                                                    ("finalized", bad, False, True, ("final_at_ns",))])
        self.assertEqual((only(state).frontier_current, state.active_stops), (frozenset(TYPES), ()))
        self.assert_one_decision(state)
        self.assert_replays(journal_)

    def test_each_differing_claim_field_is_named_in_order(self):
        """One wrong field at a time, and two at once, on a final record with its BSP decided, then each type for a
        record that is not final with no BSP decided yet: the names are the fields', in a fixed order; none holds a
        digit or a price, and the claim keeps what the note said."""
        journal_, p, _ = settled()
        claims = supported(only(journal_.fold()))
        cases = [("finalized", {"class": "unmatched"}, ("class",)), ("finalized", {"final_at_ns": 0}, ("final_at_ns",)),
                 ("finalized", {"row": 2}, ("row",)), ("finalized", {"class": "void", "row": 4}, ("class", "row")),
                 ("bsp_decided", {"actual_sp": "3.12"}, ("actual_sp",)),
                 ("bsp_decided", {"read_seq": 1}, ("read_seq",)),
                 ("bsp_decided", {"decided_at_ns": claims["bsp_decided"]["decided_at_ns"] + 1}, ("decided_at_ns",)),
                 ("bsp_decided", {"read_seq": 1, "decided_at_ns": 0}, ("read_seq", "decided_at_ns")),
                 ("bsp_decided", {"actual_sp": None, "outcome": "none", "read_seq": None},
                  ("actual_sp", "read_seq"))]
        for kind, change, names in cases:
            with self.subTest(kind=kind, change=change):
                case = Pilot()
                case.events, case.now = list(journal_.events), journal_.now
                seq = write(case, p.ref, kind, dict(claims[kind], **change))
                (entry,) = only(case.fold()).frontier
                self.assertEqual((entry["seq"], entry["ok"], entry["differs"]), (seq, False, names))
                self.assertEqual(entry["claim"], dict(claims[kind], **change))
                self.assertFalse(any(ch.isdigit() or ch == "." for name in names for ch in name))
                self.assert_one_decision(case.fold())
                self.assert_replays(case)
        early = Pilot(pt4=True)
        _, q, _ = early.classify(early.arm(0))
        early.send(q)
        seqs = {"bsp_decided": write(early, q.ref, "bsp_decided", {"decided_at_ns": q.off + HOUR, "outcome": "none",
                                                                     "actual_sp": None, "read_seq": None}),
                "finalized": write(early, q.ref, "finalized", {"final_at_ns": q.off + DEADLINE + 1, "row": 3,
                                                               "class": "eligible"})}
        state = early.fold(now=q.off + 2 * HOUR)
        self.assertEqual(entries(only(state)), [("bsp_decided", seqs["bsp_decided"], False, False, ("decided",)),
                                                ("finalized", seqs["finalized"], False, False, ("final",))])
        for entry in only(state).frontier:                 # nothing has changed since: the frozen record names the same
            self.assertEqual(pilot._frontier_claim_matches(entry["type"], entry["claim"], only(state), state.now_ns),
                             entry["differs"])
        self.assert_one_decision(state)
        self.assert_replays(early, now=q.off + 2 * HOUR)

    def test_invalid_notes_have_no_entry(self):
        """An unknown ref, row None with a sent class, and an outcome inconsistent with actual_sp (both ways) are
        invalid notes, as on dda38a8, with no entry on any record; frontier_note_valid refuses those shapes, passes
        the valid ones, and refuses to judge any other kind, open_ended included."""
        journal_, p, _ = settled()
        claims = supported(only(journal_.fold()))
        bad = {"unknown ref": ("finalized", claims["finalized"], "ref-none", True),
               "row None, sent class": ("finalized", dict(claims["finalized"], row=None), p.ref, False),
               "bsp, no actual_sp": ("bsp_decided", dict(claims["bsp_decided"], actual_sp=None), p.ref, False),
               "none, an actual_sp": ("bsp_decided", dict(claims["bsp_decided"], outcome="none"), p.ref, False)}
        seqs = []
        for label, (kind, claim, ref, shape_valid) in bad.items():
            inner = dict(claim, v=1, type=kind, contract_sha256=C.sha256)
            with self.subTest(case=label):
                self.assertEqual(pilot.frontier_note_valid(kind, inner), shape_valid)
            seqs.append(journal_.inner(inner, customer_order_ref=ref))
        state = journal_.fold()
        self.assertEqual(state.invalid_records, tuple(seqs))
        self.assertEqual((only(state).frontier, only(state).frontier_current), ((), frozenset()))
        for kind, claim in claims.items():
            self.assertTrue(pilot.frontier_note_valid(kind, dict(claim, v=1, type=kind, contract_sha256=C.sha256)))
        unsent = dict(claims["finalized"], row=None, **{"class": "definitely_unsent"})
        self.assertTrue(pilot.frontier_note_valid("finalized", unsent))
        for kind, inner in (("open_ended", {"open_id": p.open_id}), ("look", {"description_sha256": "0" * 64}),
                            ("capture_c", claims["finalized"])):
            with self.subTest(kind=kind):
                with self.assertRaises(pilot.PilotError) as raised:
                    pilot.frontier_note_valid(kind, inner)
                self.assertEqual(raised.exception.code, "frontier_input_invalid")
        self.assert_replays(journal_)

    def test_a_reused_ref_is_not_addressable_and_has_no_entry(self):
        """A second PLACE record with the first's ref is ref_reused and not addressable. The first record's entries
        are as they were before it, and a later note naming the ref is the first record's entry: the second gets
        none, ever (IR-5)."""
        journal_, p, session = settled()
        both_notes(journal_, p)
        first = only(journal_.fold())
        q = journal_.place(session, ref=p.ref)
        state = journal_.fold()
        a, b = state.place_records
        self.assertEqual((a.ref, b.ref, b.decision_seq), (p.ref, p.ref, q.seq))
        self.assertEqual((a.addressable, b.addressable), (True, False))
        self.assertIn("ref_reused", b.violations)
        self.assertEqual((a.frontier, a.frontier_current), (first.frontier, first.frontier_current))
        self.assertEqual((b.frontier, b.frontier_current), ((), frozenset()))
        later = write(journal_, p.ref, "bsp_decided", supported(a)["bsp_decided"])
        a, b = journal_.fold().place_records
        self.assertEqual(entries(a)[-1], ("bsp_decided", later, True, True, ()))
        self.assertEqual((len(a.frontier), b.frontier), (3, ()))
        self.assert_one_decision(journal_.fold())
        self.assert_replays(journal_)

    def test_a_record_state_built_directly_fails_closed(self):
        """test_betfair_pilot's synthetic() RecordState still builds, addressable, with no entry and no type current."""
        for cls in ("eligible", "definitely_unsent", "protocol_violation"):
            rec = synthetic(cls, 0)
            self.assertEqual((rec.addressable, rec.frontier, rec.frontier_current), (True, (), frozenset()))
        self.assertIsNone(pilot.fold([], contract=C, now_ns=DAY0).journal_clock_ns)


class ClockTests(FrontierCase):
    """J (IR-7): the fold clock after the last event."""

    def test_j_is_the_largest_stamp_of_the_events_that_move_the_clock(self):
        """At every prefix of S8a's rich journal (looks before the registration, a month and a year ahead and last), of
        astra_7 with a look past the deadline, of the rich journal with a malformed look a month past its head, and of
        a written journal: journal_clock_ns is the largest recorded_at of the events in it but the valid looks, None
        when there is none, at J and 400 days on (finish() never moves it), and a fold at J is at J. A malformed look is
        an invalid note, which moves the clock as any event does (S8a)."""
        journal_, _ = looks.rich(True)
        astra, _, deadline, _ = looks.astra_7(look_at=lambda deadline: deadline + 2 * S)
        malformed = Pilot()
        malformed.events, malformed.now = list(journal_.events), journal_.now
        bad = malformed.inner({"v": 1, "type": "look", "contract_sha256": C.sha256, "description_sha256": "not-a-sha"},
                              at=journal_.now + 30 * DAY_NS)
        cases = {"rich": journal_.events, "astra_7": astra.events, "malformed look last": malformed.events,
                 "written": list(written(1)[1]), "looks only": journal_.events[:1]}
        self.assertEqual((bad in malformed.fold().invalid_records, malformed.fold().journal_clock_ns),
                         (True, journal_.now + 30 * DAY_NS))
        for label, events in cases.items():
            skipped = {seq for seq, _ in pilot.fold(events, contract=C, now_ns=DAY0).looks}    # the valid looks
            self.assertTrue(skipped or label == "written")
            stamps = []
            for n in range(len(events) + 1):
                if n:
                    if events[n - 1]["seq"] not in skipped:
                        stamps.append(pilot.parse_utc_ns(events[n - 1]["recorded_at"]))
                j = max(stamps, default=None)
                for now in (DAY0 if j is None else j, (DAY0 if j is None else j) + 400 * DAY_NS):
                    with self.subTest(journal=label, prefix=n, now=now):
                        state = pilot.fold(events[:n], contract=C, now_ns=now)
                        self.assertEqual(state.journal_clock_ns, j)
                        if j is not None and now == j:
                            self.assertEqual(state.now_ns, j)
        self.assertIsNone(pilot.fold(cases["looks only"], contract=C, now_ns=DAY0).journal_clock_ns)
        self.assertGreater(max(pilot.parse_utc_ns(e["recorded_at"]) for e in journal_.events),
                           journal_.fold().journal_clock_ns)


class UnchangedTests(FrontierCase):
    """No existing stop, class or fold value changes (S2b; Before)."""

    def test_the_old_fields_are_equal_with_and_without_s2b_on_pinned_journals(self):
        """fold_fresh's pinned shapes, S8a's rich journal with and without its looks, S2a's open_ended shapes, and
        this file's own: the notes above, every wrong field, a reused ref, a later violation and a premature note."""
        journals = [(label, events) for label, events, _ in fresh.pinned()]
        for with_looks in (True, False):
            journals.append((f"rich with_looks={with_looks}", looks.rich(with_looks)[0].events))
        astra, _, _, _ = looks.astra_7(look_at=lambda deadline: deadline + 2 * S)
        journals.append(("astra_7 with a look", astra.events))
        journal_, p, session = settled()
        both_notes(journal_, p)
        claim = supported(only(journal_.fold()))["finalized"]
        for change in ({"class": "unmatched"}, {"final_at_ns": 0}, {"row": 2}):
            write(journal_, p.ref, "finalized", dict(claim, **change))
        journal_.note("bsp_decided", ref_col=p.ref, decided_at_ns=p.off, outcome="bsp", actual_sp="3.12", read_seq=1)
        journal_.journal_due(journal_.fold())
        for stop in journal_.fold().active_stops:
            journal_.ack(stop["stop_id"], cause=True)
        q = journal_.place(session, ref=p.ref)
        write(journal_, q.ref, "finalized", claim)
        journal_.note("violation", code="yu_typed_code", source_seq=p.seq)
        write(journal_, p.ref, "finalized", supported(record_of(journal_.fold(), p.ref))["finalized"])
        unsent = Pilot(pt4=True)
        s = unsent.arm(0)
        _, r, _ = unsent.classify(s)
        unsent.closed(r, sp="3.10")
        write(unsent, r.ref, "finalized", {"final_at_ns": only(unsent.fold()).measurement["final_at_ns"], "row": None,
                                           "class": "definitely_unsent"})
        unsent.note("open_ended", open_id=r.open_id)
        drain(unsent, unsent.now + 400 * DAY_NS)
        journals += [("this file's notes", journal_.events), ("premature, then the open ended", unsent.events)]
        for label, events in journals:
            with self.subTest(journal=label):
                notes = frontier_seqs(events)
                head = self.assert_unchanged(events, prefixes=sampled(notes))
                self.assert_one_decision(head)
        self.assertTrue(all(frontier_seqs(events) for label, events in journals[-2:]))

    def test_the_old_fields_are_equal_with_and_without_s2b_on_the_frontier_pins(self):
        """Every fold test_betfair_pilot's frontier pins make (pin_folds), at the pin's own now and live open, and each
        journal of theirs with a frontier note as above, at its head."""
        result, folds = pin_folds()
        self.assertEqual((result.testsRun > 10, result.failures, result.errors), (True, [], []))
        journals = {}
        for events, now, live in folds:
            with self.subTest(events=len(events), now=now, live_open_id=live):
                self.assertEqual(without_s2b(pilot.fold(events, contract=C, now_ns=now, live_open_id=live)),
                                 without_s2b(fold_before(events, now_ns=now, live_open_id=live)))
            if frontier_seqs(events):
                journals.setdefault(json.dumps(events), events)
        self.assertGreater(len(journals), 10)
        for events in journals.values():
            with self.subTest(journal=len(events)):
                self.assert_one_decision(self.assert_unchanged(events))

    def test_the_old_fields_are_equal_with_and_without_s2b_on_generated_journals(self):
        """fold_fresh's generated and drained journals, which have no frontier notes: the head's folds agree. Of S2b,
        finish()'s frontier loop is the one part Before leaves out that runs on them; addressability and J run on
        both sides."""
        for seed in range(max(fresh.GENERATED // 2, 10)):
            for label, events, _ in (fresh.generated(seed), fresh.drained(seed)):
                with self.subTest(journal=label):
                    self.assert_unchanged(events)

    def test_the_old_fields_are_equal_with_and_without_s2b_on_written_journals(self):
        """fold_fresh's journals rebuilt with WritingPilot's drain, before and after the closing drain: equal in
        journal order after every frontier note, and folded at six or so of them and at the head. Some entry is not
        ok, some not frozen, some ok and no longer matching at the head, and some journal ends a session at a
        mismatch."""
        seen = {"not ok": 0, "not frozen": 0, "ok, no longer matching": 0, "ended at a mismatch": 0}
        for seed in range(WRITTEN):
            for part, events in zip(("before the close", "closed"), written(seed)):
                with self.subTest(seed=seed, part=part):
                    head = self.assert_unchanged(events, prefixes=sampled(frontier_seqs(events)))
                    self.assert_one_decision(head)
                for rec in head.place_records:
                    seen["not ok"] += sum(not e["ok"] for e in rec.frontier)
                    seen["not frozen"] += sum(not e["frozen"] for e in rec.frontier)
                    seen["ok, no longer matching"] += sum(e["ok"] and not still_matches(e, rec, head.now_ns)
                                                          for e in rec.frontier)
                seen["ended at a mismatch"] += any(s["ended_reason"] in ("finalized_mismatch", "bsp_decided_mismatch")
                                                   for s in head.sessions)
        self.assertGreater(min(seen.values()), 0, seen)

    def test_a_closing_drain_makes_every_addressable_record_current(self):
        """What S6d's gate reads: after the closing drain, which writes the fold's own values past every trigger,
        every addressable record has both types current, whatever was written before it, a class changed by Yu's
        violation included; and the journal replays, entries included, with no live open and the latest open live."""
        changed_class = 0
        for seed in range(WRITTEN):
            before, closed = written(seed)
            journal_ = Pilot()
            journal_.events, journal_.now = list(closed), max(pilot.parse_utc_ns(e["recorded_at"]) for e in closed)
            state = journal_.fold()
            with self.subTest(seed=seed):
                self.assertTrue(state.place_records)
                self.assertEqual([rec.decision_seq for rec in state.place_records
                                  if rec.addressable and rec.frontier_current != frozenset(TYPES)], [])
                self.assert_replays(journal_)
            changed_class += any(len([e for e in rec.frontier if e["type"] == "finalized" and e["ok"]]) > 1
                                 for rec in state.place_records)
        self.assertGreater(changed_class, 0)


if __name__ == "__main__":
    unittest.main()
