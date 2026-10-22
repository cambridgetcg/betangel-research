"""P1-10 S2a-0 (IR-25): the journal order keeps up with the present.

The present re-evaluates every record at now (pilot.py finish); the journal order re-evaluates only the records an
event names. The commission method is one input that an event in one market turns on for every market: a
commission_verified note, its market's first CLOSED read, or a new record in that market. The fold now re-evaluates
every record at the event where the method turns on, so replay reproduces the present (design v3 S2a-0; Astra's v3
review: compared around every handler, since a new record can turn it on too).

The journal order is observed without a production hook: these tests drive pilot._Folder over pilot._check_events
exactly as pilot.fold does and read its records before finish(); a deep copy then runs finish() at the fold clock J
for the present. The freshness pin checks that copy against pilot.fold itself at every 100th prefix and the last. The
pinned shapes are the design round's pure probes (probe_adv, probe_stale, probe_term, probe_gw) and two from this
slice's review: probe_record (a new record turns the method on) and probe_rise (a trigger that changes a ledger).
Generated journals use test_betfair_pilot's builder. Synthetic: nothing here shows that a writer of
commission_verified exists (P1-16).
"""

import copy
import functools
import heapq
import itertools
import random
from types import SimpleNamespace
import unittest

from betangel_research.betfair import config, pilot, pilot_gate
from betangel_research.betfair.pilot import DAY_NS

from test_betfair_pilot import C, DAY0, HOUR, MIN, S, SEEDS, Pilot

GENERATED = max(SEEDS // 2, 20)                      # journals for the freshness pin and its coverage check
RESERVED = max(SEEDS // 4, 10)                       # journals for the reservation order pin


# -- the journal order and the present ----------------------------------------------------------------------------

def journal_order(events):
    """(n, folder) after each event in turn: pilot.fold's own pass before finish(), so every prefix in one pass."""
    folder = pilot._Folder(C, None)
    for n, event in enumerate(pilot._check_events(list(events)), start=1):
        folder.apply(event)
        yield n, folder


def through(events, seq):
    """The journal order after event seq."""
    for _, folder in journal_order(events[:seq]):
        pass
    return folder


def finished(folder, live_open_id):
    """A copy of the pass, finished as pilot.fold finishes it: the present at the fold clock J."""
    later = copy.deepcopy(folder)
    later.live_open_id = live_open_id                # read only by finish()
    later.finish(folder.clock)
    return later


def latest_open(folder):
    return folder.opens[-1] if folder.opens else None


def view(rec):
    frozen = pilot._freeze_record(rec)
    return frozen.ledger, frozen.execution, frozen.measurement


def fold_view(folder):
    """What the fold keeps beside its records: implied stops, each session's end, the pause, N and the due stops
    (where probe_term's stop was implied again)."""
    return (sorted(folder.implied), [(s["ended_seq"], s["ended_reason"]) for s in folder.sessions],
            folder.pause_fired, folder.n_done, folder.due_stops())


def stale(folder, live_open_id):
    """[(ref or "fold", journal order, present)] for each record whose ledger, execution or measurement after the last
    event differs from the present at J, and for the fold's own state. With no live open, the records of the opens
    finish() ends are left out, and the fold's state with them, since their end can change it: that end is the
    present's knowledge, not a journal fact (Nuance R3-1). With the latest open live, nothing is left out."""
    ending = set() if live_open_id is not None else {o for o in folder.opens if o not in folder.ended_opens}
    present = finished(folder, live_open_id)
    found = [(rec.ref, view(rec), view(now)) for rec, now in zip(folder.records, present.records)
             if rec.open_id not in ending and view(rec) != view(now)]
    if not ending and fold_view(folder) != fold_view(present):
        found.append(("fold", fold_view(folder), fold_view(present)))
    return found


def ledger(folder, ref):
    found = next(rec for rec in folder.records if rec.ref == ref).ledger
    return found["part"], found["value_p"], found["reason"]


def trigger_moves(folder, until_ns):
    """[(trigger, ref, before, after)] for each change of a record's journal-order ledger value at a time trigger
    crossed up to until_ns, one trigger time after another, as the next event's apply() would cross them."""
    later, moves = copy.deepcopy(folder), []
    while later.next_trigger is not None and later.next_trigger <= until_ns:
        at, before = later.next_trigger, [rec.value_p for rec in later.records]
        later.advance(at, later.last_seq)
        moves += [(at, rec.ref, value, rec.value_p) for rec, value in zip(later.records, before)
                  if rec.value_p != value]
    return moves


def trigger_drops(folder, until_ns):
    """[(ref, before, after)] for the moves that lower a ledger."""
    return [(ref, before, after) for _, ref, before, after in trigger_moves(folder, until_ns) if after < before]


def no_submit_yet(folder, keep=None):
    """Whether a reservation by an open other than keep would end a record unsent: one of an open not yet ended,
    with no submit, no FAILED(transport_not_sent) and no evidence of sending. Only such a record's ledger changes
    when its open ends."""
    return any(rec.open_id != keep and rec.open_ended_ns is None and rec.submit_seq is None and rec.failed_seq is None
               and rec.sent_seq is None for rec in folder.records)


def common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


# -- the pinned shapes --------------------------------------------------------------------------------------------

def losers(count):
    """The probes' preamble: settled £2 losers in S, 25 a day from day 0, after PT1 and PT4 receipts.
    -> (journal, the next day)."""
    journal_, day = Pilot(pt1=True, pt4=True), 0
    while count:
        session = journal_.arm(day)
        for _ in range(min(count, C.per_utc_day)):
            journal_.lose(journal_.place(session))
            count -= 1
        day += 1
    return journal_, day


def verified(journal_, market, at):
    return journal_.note("commission_verified", at=at, market_id=market, our_commission_p=8, website_commission_p=8)


def probe_adv(variant):
    """probe_adv: A (partly matched, settled, no commission capture) is final in M1; B's CLOSED read in M2 comes
    after Yu's commission_verified for M2 and after A's cutoff; X is placed at A's deadline, with no submit.
    variant is A's evidence: late_c (L only, then a C at another average price), second_c (C and L, then a second C
    at another average price), second_closed (a second CLOSED read with another result) or control (none)."""
    journal_, day = losers(72)
    warm = len(journal_.events)
    session = journal_.arm(day)
    a = journal_.place(session)
    journal_.send(a, matched_p=100)
    if variant != "late_c":
        journal_.c_read(a, matched="1.00", cancelled="1.00", price="3.00")
    journal_.closed(a, ours="LOSER")
    journal_.settle(a)
    journal_.l_row(a, size="1.00", price="3.00", profit="-1.00")
    if variant in ("late_c", "second_c"):
        journal_.c_read(a, at=a.off + 20 * MIN, matched="1.00", cancelled="1.00", price="3.05")
    elif variant == "second_closed":
        journal_.closed(a, ours="WINNER", at=a.off + 20 * MIN)
    b = journal_.place(session)
    journal_.send(b)
    journal_.c_read(b)
    at = DAY0 + (day + 2) * DAY_NS + 9 * HOUR
    verified(journal_, b.market, at)
    closed_seq = journal_.closed(b, ours="WINNER", at=at + 10 * MIN)
    x = journal_.place(journal_.arm(day + 7))
    return SimpleNamespace(journal=journal_, warm=warm, a=a, b=b, x=x, closed_seq=closed_seq, session=f"s{day + 7}",
                           t0=DAY0 + (day + 7) * DAY_NS + 10 * HOUR + 30 * MIN - 299 * S,
                           deadline=a.off + C.deadline_s * pilot.NS + 1)


def probe_stale(note_first):
    """probe_stale: W, a settled winner with no commission capture, is final in M1; B's first CLOSED read in M2
    comes after Yu's commission_verified for M2 (note_first) or before it (the control). The next session's gateway
    then classifies Y on the present fold."""
    journal_, day = losers(72)
    warm = len(journal_.events)
    session = journal_.arm(day)
    w = journal_.place(session)
    journal_.win(w, commission=None)
    b = journal_.place(session)
    journal_.send(b)
    journal_.c_read(b)
    at = DAY0 + day * DAY_NS + 20 * HOUR
    if note_first:
        verified(journal_, b.market, at)
        turned_on = journal_.closed(b, ours="WINNER", at=at + 10 * MIN)
    else:
        journal_.closed(b, ours="WINNER", at=at)
        turned_on = verified(journal_, b.market, at + 10 * MIN)
    result, y, _ = journal_.classify(journal_.arm(day + 1))
    return SimpleNamespace(journal=journal_, warm=warm, w=w, y=y, result=result, turned_on=turned_on)


def probe_record():
    """W, a settled winner with no commission capture, is final in M1. Yu's commission_verified for M2 and M2's
    CLOSED read showing our runner WINNER come before any record there (synthetic: a CLOSED read before the
    decision), so B's decision in M2 turns the method on: the growth of by_market that Astra's v3 review names."""
    journal_ = Pilot(pt1=True, pt4=True)
    session = journal_.arm(0)
    w = journal_.place(session)
    journal_.win(w, commission=None)
    off, market = DAY0 + 14 * HOUR, "1.777777"
    verified(journal_, market, off - 2 * HOUR)
    journal_.closed(SimpleNamespace(market=market, off=off), ours="WINNER", at=off - HOUR)
    b = journal_.place(session, off=off, market=market)
    return SimpleNamespace(journal=journal_, w=w, b=b)


def probe_rise():
    """R is sent with no place report and has a C read showing half matched; a week later X is placed, with no submit
    yet, just before R's deadline. At the deadline + 1, R's execution freezes at what the C read shows (W -200 to
    W -100): a trigger that changes a ledger, for a reservation that crosses it and ends X's open unsent."""
    journal_ = Pilot(pt1=True, pt4=True)
    r = journal_.place(journal_.arm(0))
    journal_.intent(r)
    journal_.submit(r)
    journal_.c_read(r, matched="1.00", cancelled="1.00", price="3.00")
    journal_.closed(r)
    journal_.settle(r)
    x = journal_.place(journal_.arm(7), off=r.off + C.deadline_s * pilot.NS + 2 * MIN)
    return SimpleNamespace(journal=journal_, r=r, x=x)


def probe_gw_head():
    """probe_gw's head: A (L only, then a C at another average price), W a settled winner with no commission
    capture, and B's CLOSED read after Yu's commission_verified for its market. pilot-capture journals the stop the
    present then implies (A's ledger_contradiction), and Yu acknowledges it with its cause written."""
    journal_, day = losers(71)
    warm = len(journal_.events)
    session = journal_.arm(day)
    a = journal_.place(session)
    journal_.send(a, matched_p=100)
    journal_.closed(a)
    journal_.settle(a)
    journal_.l_row(a, size="1.00", price="3.00", profit="-1.00")
    journal_.c_read(a, at=a.off + 20 * MIN, matched="1.00", cancelled="1.00", price="3.05")
    w = journal_.place(session)
    journal_.win(w, commission=None)
    b = journal_.place(session)
    journal_.send(b)
    journal_.c_read(b)
    at = DAY0 + day * DAY_NS + 20 * HOUR
    verified(journal_, b.market, at)
    journal_.closed(b, ours="WINNER", at=at + 10 * MIN)
    present = journal_.fold()
    journal_.journal_due(present)
    for stop in present.due_stops:
        journal_.ack(stop["stop_id"], cause=True)
    return SimpleNamespace(journal=journal_, warm=warm, a=a, w=w, day=day + 1, due=present.due_stops)


@functools.lru_cache(maxsize=None)
def pinned():
    """(label, events, length of the loser preamble) for each pinned shape as its probe leaves it. Shared: read
    only."""
    journals = []
    for variant in ("late_c", "second_c", "second_closed", "control"):
        shape = probe_adv(variant)
        shape.journal.note("reservation", at=shape.t0, market_col="1.999999", session_id=shape.session,
                           open_id="open-Y2")
        journals.append((f"probe_adv {variant}", shape.journal.events, shape.warm))
    for note_first in (True, False):
        shape = probe_stale(note_first)
        journals.append((f"probe_stale note_first={note_first}", shape.journal.events, shape.warm))
    term = probe_gw_head()
    session = term.journal.arm(term.day)
    term.journal.note("reservation", at=term.journal.now + HOUR, market_col="1.888888", session_id=session,
                      open_id="open-Z")
    journals.append(("probe_term", term.journal.events, term.warm))
    gw = probe_gw_head()
    gw.journal.classify(gw.journal.arm(gw.day))
    journals.append(("probe_gw", gw.journal.events, gw.warm))
    journals += [("probe_record", probe_record().journal.events, 0), ("probe_rise", probe_rise().journal.events, 0)]
    return tuple((label, tuple(events), warm) for label, events, warm in journals)


# -- generated journals -------------------------------------------------------------------------------------------

def cancel(journal_, p, cancelled_p, *, at):
    """A cancel report for p's bet (test_no_release's shape)."""
    return journal_.add("report", {"action": "cancel", "outcome": "reported", "transitions": [],
                                   "report": {"status": "SUCCESS", "error_code": None, "market_id": p.market,
                                              "instructions": [{"bet_id": p.bet_id, "size_cancelled_p": cancelled_p}],
                                              "source": "betfair"}}, at=at, market_id=p.market)


@functools.lru_cache(maxsize=None)
def preamble():
    return losers(70)                                # five placeholders of -200 from the first pause level


@functools.lru_cache(maxsize=None)
def generated(seed):
    """(label, events, length of the loser preamble): see journal()."""
    events = journal(seed)
    return f"seed {seed}", events, len(preamble()[0].events) if seed % 8 == 0 else 0


def journal(seed):
    """A journal of two to four sessions of two to five decisions, days 1, 2 or 7 apart so BSP cutoffs and deadlines
    fall inside later sessions, from the builder in time order. Decisions are the gateway's classify or written as
    placed, each with a random fate: no submit, FAILED(transport_not_sent), or sent (fully, partly or not matched, a few
    with no place report, so the deadline freezes what the C read shows) with C, CLOSED, settle, L and commission
    captures (some None) on time or late, a later C or CLOSED read that contradicts, a cancel or a reconcile move.
    Yu's commission_verified comes before or after a market's CLOSED read: near it, after the session for a winner
    whose CLOSED read is still to come (probe_stale's shape), or before the CLOSED read of a market whose record comes
    later. A few markets get a second record. PT receipts can come late, and Yu acknowledges the due stops before most
    ARMs. Every eighth seed starts 70 losers in, near the first pause level."""
    rng = random.Random(seed)
    unreported = random.Random(-1 - seed)            # a stream of its own, so the fates rng draws stay as they were
    if seed % 8 == 0:
        journal_, first_day = copy.deepcopy(preamble())
    else:
        journal_, first_day = Pilot(pt1=rng.random() < 0.8, pt4=rng.random() < 0.6), 0
    queue, order, markets, closing = [], itertools.count(), [], []     # closing: (CLOSED read at, winner's market)

    def at(ns, action, *args, **fields):
        heapq.heappush(queue, (ns, next(order), action, args, fields))

    def yu(day, *, at):
        if rng.random() < 0.8:
            journal_.journal_due(journal_.fold())
            for stop in journal_.fold().active_stops:
                if stop["scope"] == "review":
                    journal_.ack(stop["stop_id"], cause=True)
        journal_.arm(day)

    def decide(day, off, market, *, at):
        if market == "again":
            market = rng.choice(markets) if markets else None
        if rng.random() < 0.5:
            p = journal_.classify(f"s{day}", off=off, market=market)[1]
        else:
            p = journal_.place(f"s{day}", off=off, market=market)
        if p is not None:
            markets.append(p.market)
            fate(p)

    def verify_later(*, at):
        """Yu's commission_verified after the session, for a winner whose CLOSED read is still to come."""
        later = [market for closed, market in closing if closed > at]
        if later:
            journal_.note("commission_verified", at=at, market_id=rng.choice(later), our_commission_p=8,
                          website_commission_p=8)

    def fate(p):
        roll = rng.random()
        if roll < 0.1:
            return                                   # no submit: unsent once another open reserves
        if roll < 0.2:
            journal_.intent(p)
            journal_.submit(p)
            journal_.failed(p)
            return
        matched = rng.choice((200, 200, 100, 0))
        reported = unreported.random() >= 0.15       # if not, the deadline + 1 freezes what the C read shows
        if reported:
            journal_.send(p, matched)
        else:
            journal_.intent(p)
            journal_.submit(p)
        size, rest = f"{matched // 100}.00", f"{(200 - matched) // 100}.00"
        price = "3.00" if matched else None
        ours = rng.choice(("WINNER", "LOSER"))
        if rng.random() < 0.85:
            at(p.off + rng.choice((MIN, 20 * MIN, 3 * HOUR, 30 * HOUR, 7 * DAY_NS + HOUR)), journal_.c_read, p,
               matched=size, cancelled=rest, price=price)
        if matched and rng.random() < 0.15:
            at(p.off + rng.choice((2 * HOUR, 2 * DAY_NS)), journal_.c_read, p, matched=size, cancelled=rest,
               price="3.05")
        closed = p.off + rng.choice((5 * MIN, 40 * MIN, 25 * HOUR, 3 * DAY_NS))
        if rng.random() < 0.9:
            at(closed, journal_.closed, p, ours=ours)
            if ours == "WINNER":
                closing.append((closed, p.market))
        if rng.random() < 0.1:
            at(closed + HOUR, journal_.closed, p, ours="LOSER" if ours == "WINNER" else "WINNER")
        if rng.random() < 0.85:
            at(p.off + 10 * MIN, journal_.settle, p)
        if matched and rng.random() < 0.85:
            win = ours == "WINNER"
            when = rng.choice((15 * MIN, 2 * HOUR, 8 * DAY_NS))
            at(p.off + (when if reported else 8 * DAY_NS), journal_.l_row, p, size=size,
               price=rng.choice(("3.00",) * 9 + ("3.05",)),
               profit=f"{2 * matched // 100}.00" if win else f"-{size}", outcome="WIN" if win else "LOSE")
        roll = rng.random()
        if roll < 0.25:
            at(p.off + 16 * MIN, journal_.commission, p, None)
        elif roll < 0.4:
            at(p.off + 16 * MIN, journal_.commission, p, rng.choice(("0.00", "0.04", "0.08")))
        if rng.random() < 0.2:
            at(closed + rng.choice((-3 * HOUR, -MIN, MIN, 3 * HOUR)), journal_.note, "commission_verified",
               market_id=p.market, our_commission_p=8, website_commission_p=rng.choice((8,) * 9 + (9,)))
        if rng.random() < 0.1:
            at(p.off + rng.choice((30 * MIN, 2 * DAY_NS)), journal_.reconcile, p, rng.choice((100, 200, 300)))
        if rng.random() < 0.1:
            at(p.off + rng.choice((MIN, 2 * HOUR)), cancel, journal_, p, rng.choice((0, 100, 200)))

    day = first_day
    for _ in range(rng.randint(2, 4)):
        at(DAY0 + day * DAY_NS + 7 * HOUR + 30 * MIN, yu, day)
        off = DAY0 + day * DAY_NS + 10 * HOUR
        for k in range(rng.randint(2, 5)):
            off += rng.randint(6, 50) * MIN
            roll, market = rng.random(), None
            if roll < 0.1:
                market = "again"
            elif roll < 0.2:                         # Yu's note and the CLOSED read come before the record
                market = f"1.8{seed:05d}{day:02d}{k}"
                at(off - 2 * HOUR, journal_.note, "commission_verified", market_id=market, our_commission_p=8,
                   website_commission_p=8)
                at(off - HOUR, journal_.closed, SimpleNamespace(market=market, off=off), ours="WINNER")
            at(off - 300 * S, decide, day, off, market)
        if rng.random() < 0.6:
            at(DAY0 + day * DAY_NS + 20 * HOUR, verify_later)
        if rng.random() < 0.3:
            at(DAY0 + day * DAY_NS + rng.randint(9, 22) * HOUR, journal_.receipt, rng.choice(("PT1", "PT4")),
               matched=1)
        day += rng.choice((1, 1, 2, 7))
    while queue:
        ns, _, action, args, fields = heapq.heappop(queue)
        action(*args, at=max(ns, journal_.now + S), **fields)     # in time order: a planned past is written now
    return tuple(journal_.events)


# -- the pins -----------------------------------------------------------------------------------------------------

class CommissionMethodTests(unittest.TestCase):
    """IR-25's shapes, pinned: where the method turns on, every record is re-evaluated, in every market."""

    def test_the_method_turned_on_in_one_market_moves_a_winner_in_another_to_s(self):
        """probe_stale: W is in S from the event that verifies the method, in the journal order as in the present,
        so Y, which the gateway placed on the present fold, is no place_not_allowed_pause when replayed."""
        for note_first in (True, False):
            with self.subTest(note_first=note_first):
                shape = probe_stale(note_first)
                events = shape.journal.events
                self.assertEqual(ledger(through(events, shape.turned_on - 1), shape.w.ref),
                                 ("W", -200, "commission_unverified"))
                self.assertEqual(ledger(through(events, shape.turned_on), shape.w.ref), ("S", 392, None))
                self.assertEqual((shape.result["record"]["outcome"], shape.result["record"]["code"]), ("PLACE", None))
                state = shape.journal.fold()
                self.assertEqual(state.violations, ())
                y = next(rec for rec in state.place_records if rec.ref == shape.y.ref)
                self.assertEqual(y.measurement["class"], "definitely_unsent")

    def test_a_new_record_that_verifies_the_method_moves_a_winner_in_another_market_to_s(self):
        """probe_record: W is in S from B's decision, the event where the method turns on, though B's market had
        its note and CLOSED read before it."""
        shape = probe_record()
        events = shape.journal.events
        self.assertEqual(ledger(through(events, shape.b.seq - 1), shape.w.ref), ("W", -200, "commission_unverified"))
        self.assertEqual(ledger(through(events, shape.b.seq), shape.w.ref), ("S", 392, None))

    def test_a_contradiction_counts_at_the_closed_read_that_verifies_the_method(self):
        """probe_adv: A, in another market, meets its contradicting item at B's CLOSED read, not at its own next
        trigger; the control's A moves to S there."""
        for variant, after in {"late_c": ("W", -200, "contradiction"), "second_c": ("W", -200, "contradiction"),
                               "second_closed": ("W", -200, "contradiction"), "control": ("S", -100, None)}.items():
            with self.subTest(variant=variant):
                shape = probe_adv(variant)
                events = shape.journal.events
                self.assertEqual(ledger(through(events, shape.closed_seq - 1), shape.a.ref),
                                 ("W", -100, "commission_unverified"))
                self.assertEqual(ledger(through(events, shape.closed_seq), shape.a.ref), after)

    def test_no_trigger_lowers_the_journal_order_ledger(self):
        """probe_adv: crossing A's deadline lowers no record, so a reservation past it fires no pause of its own.
        The contradicted variants pause at X's decision in every order: the builder placed X where the gateway,
        on the present fold, would have skipped it (place_not_allowed_pause)."""
        for variant in ("late_c", "second_c", "second_closed", "control"):
            with self.subTest(variant=variant):
                shape = probe_adv(variant)
                prefix = list(shape.journal.events)
                self.assertLess(through(prefix, len(prefix)).clock, shape.deadline)
                self.assertLess(shape.deadline, shape.t0)
                self.assertEqual(trigger_drops(through(prefix, len(prefix)), shape.t0), [])
                gateway = pilot.fold(prefix, contract=C, now_ns=shape.t0, live_open_id="open-Y2")
                shape.journal.note("reservation", at=shape.t0, market_col="1.999999", session_id=shape.session,
                                   open_id="open-Y2")
                replay = shape.journal.fold(now=shape.t0, live_open_id="open-Y2")
                paused = {} if variant == "control" else {-15000: shape.x.seq}
                self.assertEqual((replay.pause_fired, gateway.pause_fired), (paused, paused))
                x_codes = sorted(v["code"] for v in replay.violations if v["ref"] == shape.x.ref)
                self.assertEqual(x_codes, [] if variant == "control" else ["place_not_allowed_pause",
                                                                           "place_while_stopped"])

    def test_an_acknowledged_contradiction_ends_no_later_session(self):
        """probe_term: the contradiction is implied once, at B's CLOSED read, and journaled and ACKed; the next
        session's first reservation finds nothing due and is not ended by the same stop implied again."""
        shape = probe_gw_head()
        self.assertEqual([s["reason"] for s in shape.due], ["ledger_contradiction"])
        session = shape.journal.arm(shape.day)
        shape.journal.note("reservation", at=shape.journal.now + HOUR, market_col="1.888888", session_id=session,
                           open_id="open-Z")
        state = shape.journal.fold()
        self.assertEqual((state.sessions[-1]["ended_seq"], state.due_stops), (None, ()))
        self.assertEqual([s["stop_id"] for s in state.stops], [shape.due[0]["stop_id"]])


class FreshnessTests(unittest.TestCase):
    """The freshness pin: after the last event of every prefix, each record's ledger, execution and measurement in
    journal order equal the present's at now = J, the fold clock, and so do the fold's implied stops, session ends,
    pause, N and due stops. It runs with no live open, leaving out the records of the opens finish() ends (and, when it
    ends any, the fold's state), and again with each prefix's latest open live, leaving out nothing. Every 100th prefix
    and the last also check the observation against pilot.fold."""

    def assert_fresh(self, journals_):
        """Every prefix of every journal; a prefix another journal began with is folded once."""
        checked, found = [], []
        for label, events, warm in journals_:
            start = max((common_prefix(events, other) for other in checked), default=0)   # folded already
            checked.append(events)
            for n, folder in journal_order(events):
                if n <= start:
                    continue
                for live in (None, latest_open(folder)):
                    stale_ = stale(folder, live)
                    if stale_:
                        found.append((label, n, live, stale_[0]))
                    if n == len(events) or n % 100 == 0:
                        self.assert_observed_as_folded(events[:n], folder, live)
        self.assertEqual(found[:3], [], f"{len(found)} stale prefix folds")

    def assert_observed_as_folded(self, events, folder, live_open_id):
        """The observation is pilot.fold: the copy's finish gives fold's records and equity."""
        state = pilot.fold(list(events), contract=C, now_ns=folder.clock, live_open_id=live_open_id)
        present = finished(folder, live_open_id)
        ordered = sorted(present.records, key=lambda rec: (rec.reservation_seq, rec.decision_seq))
        self.assertEqual(tuple(pilot._freeze_record(rec) for rec in ordered), state.place_records)
        self.assertEqual(present.equity, state.equity_p)

    def test_the_pinned_shapes_are_fresh_at_every_prefix(self):
        self.assert_fresh(pinned())

    def test_generated_journals_are_fresh_at_every_prefix(self):
        self.assert_fresh([generated(seed) for seed in range(GENERATED)])

    def test_the_generator_reaches_what_ir25_needs(self):
        """The generated journals reach the inputs the pin is for, IR-25's own case among them (the method turned on
        by a CLOSED read, and by a new record, while a record in another market waits on it), so a pass is not a pass
        by omission."""
        seen = set()
        for seed in range(GENERATED):
            _, events, _ = generated(seed)
            kinds = [(e["payload"].get("pilot") or {}).get("type") for e in events]
            waiting, verified_before = [], False
            for n, folder in journal_order(events):
                event, verified_now = events[n - 1], folder.commission_method_verified()
                how = {"capture_book": "a CLOSED read", "decision": "a new record"}.get(kinds[n - 1])
                if how and verified_now and not verified_before and any(
                        market != event["market_id"] for market in waiting):
                    seen.add(f"turned on elsewhere by {how}")
                verified_before = verified_now
                waiting = [rec.market_id for rec in folder.records if rec.ledger["reason"] == "commission_unverified"]
            closed = {e["market_id"]: e["seq"] for e in reversed(events) if kinds[e["seq"] - 1] == "capture_book"}
            for seq, market in folder.verified_commission:
                if market in closed:
                    seen.add("note before a CLOSED read" if seq < closed[market] else "note after a CLOSED read")
            if any(len(recs) > 1 for recs in folder.by_market.values()):
                seen.add("two records in a market")
            if any(kind == "capture_commission" and e["payload"]["pilot"]["commission"] is None
                   for kind, e in zip(kinds, events)):
                seen.add("None commission capture")
            if "pt_receipt" in kinds[kinds.index("decision"):]:
                seen.add("late PT receipt")
            if any(e["kind"] == "report" and e["payload"].get("action") == "cancel" for e in events):
                seen.add("cancel")
            if any(e["kind"] == "reconcile" for e in events):
                seen.add("reconcile move")
            if any(rec.contradicted_seq is not None for rec in folder.records):
                seen.add("contradiction")
            if folder.pause_fired:
                seen.add("pause")
        self.assertEqual(seen, {"turned on elsewhere by a CLOSED read", "turned on elsewhere by a new record",
                                "note before a CLOSED read", "note after a CLOSED read",
                                "two records in a market", "None commission capture", "late PT receipt", "cancel",
                                "reconcile move", "contradiction", "pause"})


class ReservationOrderTests(unittest.TestCase):
    """The reservation order pin (IR3-19; no rule change). A reservation still ends the earlier opens after apply()
    has crossed the triggers in (J, t0], while the gateway's present fold ended them first, so the two agree only if
    no trigger lowers the ledger: the first test checks that property at every prefix. Restricted as Astra's v3 review
    asks: only a reservation that passes the pilot-fold checks of the gateway's step A (admitted, which models those
    and not all of step A), since a reservation the gateway refuses may carry an
    intended violation (session_not_armed, say) that is no ordering divergence. Then fold(prefix + reservation(Y) at t0)
    equals fold(prefix, now=t0, live_open_id=Y) in equity, pause_fired, due stops and violations. A failure is a
    candidate for S2a-r's reachable counterexample (Q20) once the checks admitted() leaves out pass too: to report, not
    to fix here."""

    def test_no_time_trigger_lowers_a_ledger_at_any_prefix(self):
        """Design rule 8: a trigger changes a ledger only through the execution freeze at the deadline + 1, which
        never lowers it. After every prefix, each pending trigger crossed in turn lowers no record's journal-order
        ledger, and some trigger raises one, so the check is not empty."""
        checked, drops, moved = [], [], 0
        for label, events, warm in itertools.chain(pinned(), (generated(seed) for seed in range(GENERATED))):
            start = max((common_prefix(events, other) for other in checked), default=0)
            checked.append(events)
            for n, folder in journal_order(events):
                if n > start and folder.next_trigger is not None:
                    moves = trigger_moves(folder, max(t for rec in folder.records for t in rec.triggers))
                    moved += bool(moves)
                    drops += [(label, n, move) for move in moves if move[3] < move[2]]
        self.assertEqual(drops[:3], [], f"{len(drops)} ledgers lowered at a trigger")
        self.assertGreater(moved, 0)

    def admitted(self, folder, prefix, at, *, market_id, session_id, open_id):
        """The gateway's fold before reserving, or None if a check of its step A that reads that fold refuses
        (gateway.py _pilot_decide): a due stop, placement authority, a reserved market, the end margin or the trigger
        rule (at the widest evidence deadline a config allows, so under any config); and the open must be new. Step
        A's other checks are left out: kill, HALT and manual bets (files and the core fold), the window and V10 (the
        wall clock), and the guard's evaluate_pilot_reservation (risk.py), which reads the core fold: R1 (halted, KILL,
        clock regression), R3 (an unresolved order anywhere), R6's windows, R8 and R9 (worst-case exposure) and R10
        (the daily loss budget); R7's transaction limit is excluded there. Those omissions admit reservations the
        gateway would refuse, while the strict trigger rule refuses some it would admit, so the admitted set is not
        exactly the gateway's, and a failure is only a candidate counterexample (Astra, S2a-0 review)."""
        state = pilot.fold(list(prefix), contract=C, now_ns=at, live_open_id=open_id)
        if (state.due_stops or market_id in state.reservations or open_id in folder.opens
                or pilot.placement_authority(state, phase="reserve", now_ns=at, session_id=session_id)):
            return None
        session = next(s for s in reversed(state.sessions) if s["session_id"] == session_id)
        if at + pilot_gate.END_MARGIN_NS >= pilot.session_end_ns(session) or pilot_gate.trigger_blocks(
                state, t0_ns=at, evidence_deadline_ns=config.PILOT_EVIDENCE_DEADLINE_MAX_S * pilot.NS,
                look_ahead=False):
            return None
        return state

    def candidates(self, events, folder, n, start):
        """(at, market, session, open, events with the reservation) after prefix n, for n past start: past the
        prefixes another journal had and the loser preamble, where each loser is sent and settled in S before the next
        reservation. The journal's own next event, if it is a reservation. And, while a reservation would end a record
        unsent (no_submit_yet), one by a new open in a fresh market just past the next time trigger, just past each
        that changes a ledger, and just past the last before the latest session ends, when the journal order leaves
        that session able to admit it (implied stops and session ends only grow from it to the present)."""
        found = []
        if n <= start:
            return found
        following = events[n] if n < len(events) else None
        inner = (following["payload"].get("pilot") or {}) if following else {}
        if inner.get("type") == "reservation":
            found.append((pilot.parse_utc_ns(following["recorded_at"]), following["market_id"], inner["session_id"],
                          inner["open_id"], events[:n + 1]))
        if not no_submit_yet(folder) or not folder.sessions or folder.due_stops():
            return found
        session = folder.sessions[-1]
        end = pilot.session_end_ns(session)
        triggers = sorted(t for rec in folder.records for t in rec.triggers if t < end)
        moving = {trigger for trigger, *_ in trigger_moves(folder, end - 1)}
        for trigger in sorted({triggers[0], triggers[-1]} | moving) if triggers else ():
            at = (trigger // 1000 + 1) * 1000            # recorded_at has microseconds
            if folder.session_armed(at):
                journal_ = Pilot()                       # the builder, writing after the prefix
                journal_.events, journal_.now = list(events[:n]), at
                market, open_id = f"1.7{n:05d}{len(found)}", f"open-gateway-{n}-{len(found)}"
                journal_.note("reservation", at=at, market_col=market, session_id=session["session_id"],
                              open_id=open_id)
                found.append((at, market, session["session_id"], open_id, journal_.events))
        return found

    def assert_reservations_fold_as_the_gateway_folded(self, journals_):
        """-> (reservations admitted, those that cross a trigger that changes a ledger while they end a record unsent:
        the case the pin is for)."""
        checked, tried, crossing, found = [], 0, 0, []
        for label, events, warm in journals_:
            start = max((common_prefix(events, other) for other in checked), default=0)
            checked.append(events)
            for n, folder in journal_order(events):
                for at, market, session, open_id, reserved in self.candidates(events, folder, n, max(start, warm)):
                    gateway = self.admitted(folder, events[:n], at, market_id=market, session_id=session,
                                            open_id=open_id)
                    if gateway is None:
                        continue
                    tried += 1
                    crossing += no_submit_yet(folder, keep=open_id) and bool(trigger_moves(folder, at))
                    after = pilot.fold(list(reserved), contract=C, now_ns=at, live_open_id=open_id)
                    outcome = [(s.equity_p, s.pause_fired, s.due_stops, s.violations) for s in (after, gateway)]
                    if outcome[0] != outcome[1]:
                        found.append((label, n, open_id, outcome))
        self.assertEqual(found[:2], [], f"{len(found)} reservations fold otherwise than the gateway folded")
        return tried, crossing

    def test_an_admitted_reservation_folds_as_the_gateway_folded_on_the_pinned_shapes(self):
        """probe_rise gives a reservation whose crossed trigger changes a ledger."""
        tried, crossing = self.assert_reservations_fold_as_the_gateway_folded(pinned())
        self.assertGreater(min(tried, crossing), 0)

    def test_an_admitted_reservation_folds_as_the_gateway_folded_on_generated_journals(self):
        """Breadth: at the default seeds no generated reservation crosses a trigger that changes a ledger while it
        ends a record unsent (a send with no place report whose deadline falls in a later session, before a
        decision there), so that case rests on probe_rise."""
        tried, _ = self.assert_reservations_fold_as_the_gateway_folded([generated(seed) for seed in range(RESERVED)])
        self.assertGreater(tried, 0)


if __name__ == "__main__":
    unittest.main()
