"""Pure Pilot 1 logic (betangel_research.betfair.pilot) against contract p1 and the RS-19 engineering note, synthetic.

These show the P1 predicates one by one and in any runner order, the decision record and its R13 binding, capacity
and the pause at classification, the journal fold (reservations, SKIP(interrupted), slots, stops, ACKs, protocol
violations), the final execution record and canonical observations, BSP, the measurement classes, NCLV, the
ledger's W and S parts with contradiction, the pause by a PLACE and by a ledger change, PT4 membership, the
sample, endings, the data lock, the validity gates, CR1 with the stdlib Student t, and the verdict order; every
pinned example in the note (§10, §11) is a test here, with a re-fold standing in for a restart. Journals are
synthetic live event dicts shaped as OrderJournal.events() yields them; no journal file, clock, network or Keychain
is touched. They do not show that a caller fetched, journaled or sent what these functions assume (P1-06, P1-08,
P1-10), nor anything about Betfair's real responses: the catalogue's event projection and the cleared-order fields
are as Betfair documents them, not repo-verified.
"""

import ast
import copy
import dataclasses
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import inspect
import itertools
import json
import math
import os
from pathlib import Path
import random
import tempfile
import unittest

from betangel_research.betfair import journal, pilot, pnl, receipts, schemas, secret
from betangel_research.betfair.pilot import NS, DAY_NS, PilotError

import test_betfair_backtest_boundaries as backtest_boundaries
import test_betfair_boundaries as boundaries
from test_betfair_support import Raw, dumps, rpc

REPO = Path(__file__).resolve().parent.parent
# The one place the frozen contract is named; Nuance repoints these two lines when contract p1 is re-frozen.
CONTRACT_PATH = REPO / "research" / "pilots" / "p1-fav-t5.json"
CONTRACT_SHA256 = "4dbccb5a45f7a4779e0263d4df5048f582f10b8dc3bd22c5f2e93e791155de92"


def load_p1():
    """(raw bytes, Contract) of contract p1, read once through this helper only."""
    raw = CONTRACT_PATH.read_bytes()
    return raw, pilot.contract_from_bytes(raw)


RAW, C = load_p1()
SEEDS = int(os.environ.get("BETANGEL_BETFAIR_PROPERTY_SEEDS", "40"))
S = NS
MIN = 60 * S
HOUR = 3600 * S
T = pilot.parse_utc_ns
DAY0 = T("2027-01-04T00:00:00Z")
OFF = T("2027-01-01T15:00:00.000Z")
MARKET = "1.234"
FAV = 11


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def levels(pairs):
    return [{"price": Raw(price), "size": Raw(size)} for price, size in pairs]


def runner(selection, back=(), lay=(), status="ACTIVE"):
    return {"selectionId": selection, "handicap": 0, "status": status,
            "ex": {"availableToBack": levels(back), "availableToLay": levels(lay), "tradedVolume": []}}


BASE_RUNNERS = {
    11: (("3.00", "15.00"), ("2.98", "50.00")), 12: (("4.5", "30"),), 13: (("5.0", "30"),), 14: (("6.0", "30"),),
    15: (("8.0", "30"),), 16: (("10.0", "30"),)}
BASE_LAYS = {11: (("3.05", "20.00"),), 12: (("4.6", "30"),), 13: (("5.1", "30"),), 14: (("6.2", "30"),),
             15: (("8.2", "30"),), 16: (("10.5", "30"),)}


def catalogue_item(market_id=MARKET, off_text="2027-01-01T15:00:00.000Z", **changes):
    row = {"marketId": market_id, "marketName": "Synthetic 5f Hcap", "totalMatched": 0,
           "marketStartTime": off_text,
           "description": {"bettingType": "ODDS", "marketType": "WIN", "marketTime": off_text,
                           "persistenceEnabled": True, "priceLadderDescription": {"type": "CLASSIC"}},
           "eventType": {"id": "7", "name": "Horse Racing"},
           "event": {"id": "31000001", "name": "Synthetic Park", "countryCode": "GB", "timezone": "Europe/London",
                     "venue": "Synthetic Park", "openDate": off_text}}
    for key, value in changes.items():
        if value is DROP:
            row.pop(key)
        else:
            row[key] = value
    return row


DROP = object()


def book_item(market_id=MARKET, runners=None, **changes):
    if runners is None:
        runners = [runner(s, BASE_RUNNERS[s], BASE_LAYS[s]) for s in sorted(BASE_RUNNERS)]
    book = {"marketId": market_id, "isMarketDataDelayed": False, "status": "OPEN", "betDelay": 0, "inplay": False,
            "numberOfWinners": 1, "numberOfRunners": len(runners), "version": 1, "runners": runners}
    for key, value in changes.items():
        if value is DROP:
            book.pop(key)
        else:
            book[key] = value
    return book


def base_runners(**overrides):
    """The base runners with some replaced: overrides maps 'r<sel>' to (back, lay, status)."""
    found = []
    for selection in sorted(BASE_RUNNERS):
        back, lay, status = overrides.get(f"r{selection}", (BASE_RUNNERS[selection], BASE_LAYS[selection], "ACTIVE"))
        found.append(runner(selection, back, lay, status))
    return found


def bodies(market_id=MARKET, off=OFF, row=None, book=None):
    text = pilot.utc_text_from_ns(off)[:-4] + "Z"            # millisecond text as Betfair writes it
    return (rpc(row if row is not None else catalogue_item(market_id, text)),
            rpc(book if book is not None else book_item(market_id)))


def decide(row=None, book=None, *, observed=OFF - 295 * S, market_id=MARKET):
    """(outcome, code, predicates) as classify reaches them: unreadable evidence is SKIP(evidence_invalid)."""
    row_body, book_body = bodies(market_id, OFF, row, book)
    try:
        ev = pilot.decision_evidence(row_body, book_body, market_id=market_id)
    except pilot.EvidenceError:
        return "SKIP", "evidence_invalid", None
    result = pilot.evaluate_predicates(C, ev, observed_wall_ns=observed)
    return result["outcome"], result["code"], result


def current_row(matched="2.00", cancelled="0.00", lapsed="0.00", voided="0.00", remaining="0.00", price="3.00",
                status="EXECUTION_COMPLETE", **extra):
    row = {"status": status, "sizeMatched": matched, "sizeCancelled": cancelled, "sizeLapsed": lapsed,
           "sizeVoided": voided, "sizeRemaining": remaining, "averagePriceMatched": price, "selectionId": None,
           "marketId": None, "customerOrderRef": None, "customerStrategyRef": None}
    row.update(extra)
    return row


def cleared_row(size="2.00", price="3.00", reduced=False, profit="-2.00", outcome="LOSE", status="SETTLED"):
    return {"betStatus": status, "sizeSettled": size, "priceMatched": price, "priceReduced": reduced,
            "profit": profit, "betOutcome": outcome, "selectionId": None, "marketId": None,
            "customerOrderRef": None, "settledDate": None}


def dec_row(row):
    return pilot.parse_current_row(row) if "sizeMatched" in row else pilot.parse_cleared_row(row)


@dataclasses.dataclass
class Placed:
    market: str
    ref: str
    seq: int
    record: dict
    off: int
    open_id: str
    bet_id: str


class Pilot:
    """A synthetic live journal of pilot notes and core order events, written in time order."""

    def __init__(self, *, pt1=True, pt4=False, registration="registration-1"):
        self.events = []
        self.now = DAY0 - DAY_NS
        self.registration = sha(registration)
        self.sessions, self.day_used = {}, {}
        self.lifetime, self.pt4_done, self.count = 0, False, 0
        self.note("registration", registration_receipt_sha256=self.registration, predecessor_contract_sha256=None)
        if pt1:
            self.receipt("PT1")
        if pt4:
            self.receipt("PT4", matched=1)

    # -- low level -----------------------------------------------------------------------------------------------
    def add(self, kind, payload, *, at=None, **columns):
        at = self.now + S if at is None else at
        self.now = max(self.now, at)
        seq = len(self.events) + 1
        event = {"seq": seq, "event_id": f"ev-{seq:032x}", "recorded_at": pilot.utc_text_from_ns(at),
                 "monotonic_ns": seq, "kind": kind, "mode": "live", "intent_id": None, "customer_order_ref": None,
                 "customer_ref": None, "bet_id": None, "market_id": None, "state_after": None, "payload": payload,
                 "prev_sha256": "0" * 64, "event_sha256": "0" * 64}
        event.update(columns)
        self.events.append(event)
        return seq

    def note(self, record_type, /, *, at=None, market_col=None, ref_col=None, **fields):
        return self.add("note", pilot.pilot_note(record_type, C.sha256, **fields), at=at, market_id=market_col,
                        customer_order_ref=ref_col)

    def inner(self, inner, *, at=None, market_id=None, customer_order_ref=None):
        return self.add("note", {"pilot": inner}, at=at, market_id=market_id, customer_order_ref=customer_order_ref)

    def fold(self, now=None, live_open_id=None, contract=C):
        return pilot.fold(self.events, contract=contract, now_ns=self.now if now is None else now,
                          live_open_id=live_open_id)

    # -- sessions and receipts -----------------------------------------------------------------------------------
    def receipt(self, kind, *, verified=True, matched=0, basis="gross", registration=None, at=None):
        if kind == "PT4" and matched >= 1:
            self.pt4_done = True
        return self.note("pt_receipt", at=at, kind=kind, receipt_sha256=sha(f"{kind}-{len(self.events)}"),
                         registration_receipt_sha256=registration or self.registration, l_fields_verified=verified,
                         matched_settled_bets=matched, profit_basis=basis)

    def arm(self, day, *, registration=None, until_h=23, until=None):
        start = DAY0 + day * DAY_NS
        if registration and registration != self.registration:
            self.registration = registration
            self.note("registration", at=start + 7 * HOUR, registration_receipt_sha256=registration,
                      predecessor_contract_sha256=None)
        session = f"s{day}"
        self.note("session_start", at=start + 8 * HOUR, session_id=session,
                  registration_receipt_sha256=self.registration, utc_day=pilot.utc_day(start),
                  until_ns=start + until_h * HOUR if until is None else until)
        self.sessions[session] = {"day": day, "pt4": not self.pt4_done, "count": 0}
        return session

    def next_off(self, session):
        info = self.sessions[session]
        info["count"] += 1
        return DAY0 + info["day"] * DAY_NS + 10 * HOUR + (info["count"] - 1) * 30 * MIN

    # -- decisions -----------------------------------------------------------------------------------------------
    def reserve(self, market, off, *, session, open_id):
        return self.note("reservation", at=off - 299 * S, market_col=market, session_id=session, open_id=open_id)

    def place(self, session, *, off=None, market=None, open_id=None, ref=None, record_changes=None, decided_at=None):
        """Reservation and a PLACE record built as the gateway would, with the counts this journal implies."""
        self.count += 1
        off = self.next_off(session) if off is None else off
        market = market or f"1.{100000 + self.count}"
        open_id = open_id or f"open-{self.count}"
        ref = ref or f"ref-{self.count}"
        res = self.reserve(market, off, session=session, open_id=open_id)
        observed = off - 295 * S
        row_body, book_body = bodies(market, off)
        ev = pilot.decision_evidence(row_body, book_body, market_id=market)
        predicates = pilot.evaluate_predicates(C, ev, observed_wall_ns=observed)
        day = pilot.utc_day(observed)
        limit = C.per_pt4_session_day if self.sessions[session]["pt4"] else C.per_utc_day
        record = pilot.decision_record(
            C, outcome="PLACE", code=None, registration_receipt_sha256=self.registration, session_id=session,
            open_id=open_id, reservation_seq=res, market_id=market, customer_order_ref=ref, ev=ev,
            row_body=row_body, book_body=book_body, predicates=predicates, observed_wall_ns=observed,
            observed_monotonic_ns=7, day_limit=limit, lifetime_slot=self.lifetime + 1,
            day_count=self.day_used.get(day, 0) + 1)
        record.update(record_changes or {})
        seq = self.inner(record, at=off - 294 * S if decided_at is None else decided_at, market_id=market)
        self.lifetime += 1
        self.day_used[day] = self.day_used.get(day, 0) + 1
        return Placed(market, ref, seq, record, off, open_id, str(5000 + self.count))

    def classify(self, session, *, off=None, market=None, open_id=None, ref=None, row=None, book=None,
                 observed=None, journal_it=True):
        """Reserve, fold as the live open and classify on fresh bytes; journal the record and stops it returns."""
        self.count += 1
        off = self.next_off(session) if off is None else off
        market = market or f"1.{100000 + self.count}"
        open_id = open_id or f"open-{self.count}"
        ref = ref or f"ref-{self.count}"
        res = self.reserve(market, off, session=session, open_id=open_id)
        observed = off - 295 * S if observed is None else observed
        row_body, book_body = bodies(market, off, row, book)
        state = self.fold(now=observed, live_open_id=open_id)
        result = pilot.classify(C, state, reservation_seq=res, open_id=open_id, session_id=session,
                                registration_receipt_sha256=self.registration, customer_order_ref=ref,
                                row_body=row_body, book_body=book_body, observed_wall_ns=observed,
                                observed_monotonic_ns=7)
        placed = None
        if journal_it and result["record"] is not None:
            seq = self.inner(result["record"], at=off - 294 * S, market_id=market)
            if result["record"]["outcome"] == "PLACE":
                self.lifetime += 1
                day = result["record"]["utc_day"]
                self.day_used[day] = self.day_used.get(day, 0) + 1
                placed = Placed(market, ref, seq, result["record"], off, open_id, str(5000 + self.count))
        if journal_it:
            for stop in result["stops"]:
                self.inner(stop, at=off - 293 * S)
        return result, placed, res

    # -- orders --------------------------------------------------------------------------------------------------
    def intent(self, p, *, order=None, source="pilot", strategy="p1-fav-t5", at=None):
        payload = {"order": order or pilot.expected_instruction(p.record), "customer_strategy_ref": strategy,
                   "is_probe": False, "parent_ref": None, "provisional_child": False, "source": source}
        return self.add("intent", payload, at=at or p.off - 293 * S, intent_id=f"i-{p.ref}",
                        customer_order_ref=p.ref, customer_ref=f"c-{p.ref}", market_id=p.market,
                        state_after="RECORDED")

    def submit(self, p, *, at=None, strategy="p1-fav-t5", count=1):
        payload = {"action": "place", "instruction_count": count, "body_sha256": None, "decision_id": "d",
                   "probe": False, "params": {"customerStrategyRef": strategy},
                   "transitions": [{"customer_order_ref": p.ref, "state": "SUBMITTING"}]}
        return self.add("submit", payload, at=at or p.off - 292 * S, customer_ref=f"c-{p.ref}", market_id=p.market)

    def report(self, p, matched_p=200, *, at=None, status="SUCCESS"):
        item = {"customer_order_ref": p.ref, "status": status, "error_code": None,
                "order_status": "EXECUTION_COMPLETE", "bet_id": p.bet_id, "placed_date": None,
                "average_price_matched_h": 300 if matched_p else None, "size_matched_p": matched_p}
        payload = {"action": "place", "outcome": "reported",
                   "report": {"status": "SUCCESS", "error_code": None, "market_id": p.market, "instructions": [item],
                              "source": "betfair"},
                   "transitions": [{"customer_order_ref": p.ref, "state": "EXECUTABLE", "reason": "place_report",
                                    "fields": {"bet_id": p.bet_id, "size_matched_p": matched_p or 0}}]}
        return self.add("report", payload, at=at or p.off - 291 * S, market_id=p.market)

    def failed(self, p, *, at=None):
        payload = {"action": "place", "report": None, "outcome": "FAILED_NOT_SENT",
                   "transitions": [{"customer_order_ref": p.ref, "state": "FAILED", "reason": "place_exception",
                                    "fields": {"error_code": "transport_not_sent"}}]}
        return self.add("report", payload, at=at or p.off - 291 * S, market_id=p.market)

    def send(self, p, matched_p=200, **kw):
        self.intent(p)
        self.submit(p)
        return self.report(p, matched_p, **kw)

    def reconcile(self, p, matched_p, *, at=None):
        payload = {"transitions": [{"customer_order_ref": p.ref, "state": "EXECUTION_COMPLETE",
                                    "reason": "reconcile", "fields": {"bet_id": p.bet_id,
                                                                      "size_matched_p": matched_p}}]}
        return self.add("reconcile", payload, at=at, market_id=p.market)

    def c_read(self, p, *, at=None, **row):
        return self.note("capture_c", at=at or p.off + MIN, ref_col=p.ref, bet_id=p.bet_id,
                         raw_sha256=sha(f"c{len(self.events)}"), row=current_row(**row))

    def l_row(self, p, *, at=None, **row):
        return self.note("capture_l", at=at or p.off + 15 * MIN, ref_col=p.ref, bet_id=p.bet_id,
                         raw_sha256=sha(f"l{len(self.events)}"), row=cleared_row(**row))

    def closed(self, p, *, ours="LOSER", sp="3.10", reconciled=True, status="CLOSED", at=None, removed=(),
               winners=None, statuses=None):
        if statuses is None:
            statuses = {}
            others = [s for s in sorted(BASE_RUNNERS) if s != FAV]
            winner_left = ours != "WINNER" if winners is None else False
            for s in others:
                if s in removed:
                    statuses[s] = "REMOVED"
                elif winner_left or (winners and s in winners):
                    statuses[s] = "WINNER"
                    winner_left = False
                else:
                    statuses[s] = "LOSER"
            statuses[FAV] = ours
        runners = {str(s): {"status": st, "actual_sp": sp if s == FAV else "5.00"} for s, st in statuses.items()}
        return self.note("capture_book", at=at or p.off + 5 * MIN, market_col=p.market,
                         raw_sha256=sha(f"b{len(self.events)}"), status=status, bsp_reconciled=reconciled,
                         runners=runners)

    def settle(self, p, *, at=None):
        payload = {"basis": "operator_settled_worst_case", "echo": f"SETTLE MARKET {p.market}",
                   "shown_sha256": "0" * 64, "market_status": "CLOSED", "commission_p": 0,
                   "transitions": [{"customer_order_ref": p.ref, "state": "SETTLED", "reason": "operator_settle_market",
                                    "fields": {"settled_profit_own_p": None, "settled_profit_betfair_p": None}}]}
        return self.add("settle", payload, at=at or p.off + 10 * MIN, market_id=p.market)

    def commission(self, p, text, *, at=None):
        return self.note("capture_commission", at=at or p.off + 16 * MIN, market_col=p.market,
                         raw_sha256=sha(f"m{len(self.events)}"), commission=text)

    def lose(self, p):
        """A settled, verified £2 loser at 3.00 (part S at -200 once the PT4 receipt exists)."""
        self.send(p)
        self.c_read(p)
        self.closed(p)
        self.settle(p)
        self.l_row(p)
        self.commission(p, "0.00")

    def win(self, p, profit="4.00", commission="0.08"):
        self.send(p)
        self.c_read(p)
        self.closed(p, ours="WINNER")
        self.settle(p)
        self.l_row(p, profit=profit, outcome="WIN")
        if commission is not None:
            self.commission(p, commission)

    def stop(self, stop_id, scope, reason, *, level=None, source=None, at=None):
        return self.note("stop", at=at, stop_id=stop_id, scope=scope, reason=reason, level_p=level,
                         source_seq=source)

    def ack(self, stop_id, *, cause=False, at=None):
        return self.note("ack", at=at, stop_id=stop_id, cause_written=cause)

    def journal_due(self, state):
        for stop in state.due_stops:
            self.inner(stop)


def only(state):
    (record,) = state.place_records
    return record


def occurrences_of(stops, reason, decision_seq):
    """The ids of the stop occurrences '<reason>-<decision seq>-<raising seq>' for one record (Nuance R2-1)."""
    head = "-".join(reason.split("_")) + f"-{decision_seq}-"
    return [s["stop_id"] for s in stops if s["reason"] == reason and s["stop_id"].startswith(head)]


def occurrence(stops, reason, decision_seq):
    (found,) = occurrences_of(stops, reason, decision_seq)
    return found


def codes(state):
    return sorted({v["code"] for v in state.violations})


def bulk(journal, n, lifecycle=None, *, first_day=0, per_day=None):
    """n PLACE records over consecutive days at the day limit, each run through lifecycle(journal, placed)."""
    placed, day, session = [], first_day, None
    while len(placed) < n:
        if session is None or journal.sessions[session]["count"] >= (per_day or (
                C.per_pt4_session_day if journal.sessions[session]["pt4"] else C.per_utc_day)):
            session = journal.arm(day)
            day += 1
        p = journal.place(session)
        if lifecycle:
            lifecycle(journal, p)
        placed.append(p)
    return placed


# ---------------------------------------------------------------------------------------------------------------

class Boundaries(unittest.TestCase):
    """The module is pure: its closure, imports, calls and text (plan :118; design §1)."""

    PATH = REPO / "betangel_research" / "betfair" / "pilot.py"

    def test_pilot_registered_allowed(self):
        self.assertIn("pilot", backtest_boundaries.ALLOWED_BETFAIR)
        self.assertNotIn("pilot", backtest_boundaries.FORBIDDEN_BETFAIR)

    def test_pilot_closure_avoids_forbidden_modules(self):
        bb = backtest_boundaries
        found = bb.pilot_violations(bb.REAL, betfair=bb.BETFAIR, forbidden=bb.FORBIDDEN_BETFAIR)
        self.assertEqual(found, [], bb.describe(found))
        closure = set(bb.REAL.closure([bb.PILOT]))
        self.assertEqual(closure, {bb.TOP, bb.BETFAIR, bb.PILOT, f"{bb.BETFAIR}.ticks", f"{bb.BETFAIR}.constants"})
        for name in ("gateway", "api", "transport", "auth", "credentials", "config", "paths", "journal", "receipts",
                     "schemas", "filters", "lifecycle", "risk", "reconcile", "validate", "pnl", "cli"):
            self.assertNotIn(f"{bb.BETFAIR}.{name}", closure)
        external = {d.split(".")[0] for m in closure for d in bb.REAL.external[m]}
        self.assertEqual(external & {"os", "time", "subprocess", "socket", "random", "logging", "pathlib"}, set())

    def test_pilot_mutations_fail_directly_and_transitively(self):
        bb = backtest_boundaries
        cases = {
            "api in pilot": ({"pkg/betfair/pilot.py": "from . import api\n"}, "pkg.betfair.api"),
            "receipts hop": ({"pkg/betfair/pilot.py": "from . import receipts\n",
                              "pkg/betfair/receipts.py": "import subprocess\n"}, "subprocess"),
            "os in a hop": ({"pkg/betfair/pilot.py": "from . import helper\n",
                             "pkg/betfair/helper.py": "import os\n"}, "os"),
            "gateway hop": ({"pkg/betfair/pilot.py": "from . import helper\n",
                             "pkg/betfair/helper.py": "from . import gateway\n"}, "pkg.betfair.gateway"),
        }
        for label, (files, endpoint) in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                bb.write_tree(tmp, {**bb.SYNTHETIC_BASE, **files})
                found = bb.pilot_violations(bb.ImportGraph(tmp, "pkg"), betfair="pkg.betfair",
                                            forbidden=bb.SYNTHETIC_FORBIDDEN)
                self.assertIn(endpoint, [e for _, e, _ in found], bb.describe(found))
        with tempfile.TemporaryDirectory() as tmp:
            bb.write_tree(tmp, {**bb.SYNTHETIC_BASE, "pkg/betfair/pilot.py": "from . import helper\nimport json\n"})
            self.assertEqual(bb.pilot_violations(bb.ImportGraph(tmp, "pkg"), betfair="pkg.betfair",
                                                 forbidden=bb.SYNTHETIC_FORBIDDEN), [])

    def test_pilot_imports_pinned(self):
        imports = {(module, level, tuple(names)) for module, level, names in boundaries.imported_modules(self.PATH)}
        self.assertEqual(imports, {
            ("dataclasses", 0, ("dataclass",)), ("datetime", 0, ("datetime", "timedelta", "timezone")),
            ("decimal", 0, ("Decimal", "DecimalException")), ("fractions", 0, ("Fraction",)),
            ("hashlib", 0, ("hashlib",)),
            ("json", 0, ("json",)), ("math", 0, ("math",)), ("re", 0, ("re",)),
            ("constants", 1, ("CLASSIC_MAX_PRICE_H", "CLASSIC_MIN_PRICE_H")),
            ("ticks", 1, ("is_on_ladder", "tick_index"))})
        self.assertTrue({m for m, level, _ in imports if not level} <= backtest_boundaries.BACKTEST_STDLIB)

    def test_pilot_no_clock_file_calls(self):
        tree = boundaries.tree(self.PATH)
        banned = {"open", "write", "write_text", "write_bytes", "read_text", "read_bytes", "mkdir", "touch", "unlink",
                  "rename", "replace", "chmod", "now", "utcnow", "today", "time", "time_ns", "monotonic",
                  "monotonic_ns", "perf_counter", "sleep", "getenv", "urandom", "random", "randint"}
        called = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and boundaries.called_name(n) in banned]
        self.assertEqual(called, [])
        self.assertEqual([n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ("open", "environ")],
                         [])
        self.assertEqual([n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "environ"], [])

    def test_pilot_not_imported_by_cli_entry_closure_except_via_gateway_path(self):
        """Informational today: pilot imports neither the gateway nor the CLI (P1-06 wires it in)."""
        bb = backtest_boundaries
        closure = bb.REAL.closure([bb.PILOT])
        self.assertNotIn(f"{bb.BETFAIR}.gateway", closure)
        self.assertNotIn(f"{bb.BETFAIR}.cli", closure)
        self.assertFalse(boundaries.imports_betfair_module(self.PATH, "gateway"))
        self.assertFalse(boundaries.imports_betfair_module(self.PATH, "cli"))

    def test_text_bans(self):
        text = self.PATH.read_text(encoding="utf-8")
        for banned in ("frozen_experiment", "allowed_experiments", "_MINT_TOKEN", "settle_from_cleared",
                       "close_from_cleared", "cleared_move", "pnl_basis_mismatch", "_pnl_mismatch",
                       "stopped_for_kill", "timestamp_ms", "temporal", *backtest_boundaries.LIVE_STATE_TEXT):
            self.assertNotIn(banned, text)
        for banned in boundaries.ORDER_LITERALS + boundaries.NEVER_ANYWHERE + boundaries.REMOVED_ORDER_LITERALS:
            self.assertNotIn(banned.lower(), text.lower())

    def test_canonical_sha_matches_receipts(self):
        rng = random.Random(105)

        def value(depth):
            kind = rng.randrange(7 if depth < 4 else 4)
            if kind == 0:
                return rng.randrange(-10 ** 12, 10 ** 12)
            if kind == 1:
                return "".join(rng.choice("aZ09 _-é€\"\\") for _ in range(rng.randrange(6)))
            if kind == 2:
                return rng.choice([True, False, None])
            if kind == 3:
                return rng.random() * 1000
            if kind in (4, 5):
                return {f"k{rng.randrange(9)}": value(depth + 1) for _ in range(rng.randrange(4))}
            return [value(depth + 1) for _ in range(rng.randrange(4))]

        for _ in range(200):
            item = value(0)
            self.assertEqual(pilot.canonical_json(item), receipts.canonical_json(item))
            self.assertEqual(pilot.canonical_sha256(item), receipts.canonical_sha256(item))

    def test_parse_utc_ns_matches_receipts(self):
        corpus = ["2027-01-01T15:00:00Z", "2027-01-01T15:00:00.1Z", "2027-01-01T15:00:00.123Z",
                  "2027-01-01T15:00:00.123456Z", "2027-01-01T15:00:00+01:00", "2027-01-01T15:00:00.5-05:30",
                  "1970-01-01T00:00:00Z", "2027-06-30T00:00:00.000000Z", "2024-02-29T23:59:59.999999Z"]
        refused = ["2027-01-01T15:00:00.1234567Z", "2027-01-01T15:00:00", "2027-13-01T15:00:00Z",
                   "2027-01-01 15:00:00Z", "", "2027-01-01T15:00:00.Z", "２027-01-01T15:00:00Z", None, 17]
        for text in corpus:
            with self.subTest(text=text):
                self.assertEqual(pilot.parse_utc_ns(text), receipts.parse_utc_ns(text))
                ns = pilot.parse_utc_ns(text)
                self.assertEqual(pilot.utc_text_from_ns(ns), receipts.utc_from_ns(ns))
        for text in refused:
            with self.subTest(text=text):
                self.assertRaises(ValueError, receipts.parse_utc_ns, text)
                self.assertRaises(PilotError, pilot.parse_utc_ns, text)

    def test_decimal_loads_refuses_dupes_nan_inf(self):
        for text in ('{"a":1,"a":2}', '[NaN]', '[Infinity]', '[-Infinity]', '[1e999]', '[1e-999]', '{"a":',
                     b'\xff', '[1]x', 3.0):
            with self.subTest(text=str(text)[:20]):
                self.assertRaises(PilotError, pilot.decimal_loads, text)
        parsed = pilot.decimal_loads('[NaN, Infinity, -Infinity, 3.44, 2]', allow_nonfinite=True)
        self.assertEqual([p.text for p in parsed[:3]], ["NaN", "Infinity", "-Infinity"])
        self.assertEqual(parsed[3:], [Decimal("3.44"), 2])
        self.assertIs(type(parsed[3]), Decimal)
        self.assertRaises(PilotError, pilot.decimal_loads, '{"a":1,"a":1}', allow_nonfinite=True)

    def test_decimal_roundtrip_exact(self):
        rng = random.Random(1050)
        for _ in range(1000):
            digits = rng.randrange(1, 26)
            point = rng.randrange(0, digits)
            body = "".join(rng.choice("0123456789") for _ in range(digits))
            whole = str(int(body[:point] or "0"))
            text = whole + "." + body[point:] if point < digits else str(int(body)) + ".0"
            value = pilot.decimal_loads(f"[{text}]")[0]
            with self.subTest(text=text):
                self.assertEqual(value, Decimal(text))
                self.assertEqual(Decimal(pilot.money_text(value)), value)
                self.assertEqual(pilot.money_text(value), format(Decimal(text), "f"))
        self.assertEqual(pilot.money_text(Decimal("1E+1")), "10")

    def test_every_payload_passes_journal_prepare_event(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        result, placed, _ = journal_.classify(session)
        journal_.send(placed)
        journal_.c_read(placed)
        journal_.closed(placed)
        journal_.l_row(placed)
        journal_.commission(placed, "0.00")
        journal_.note("commission_verified", market_id=placed.market, our_commission_p=8, website_commission_p=8)
        journal_.stop("pause-15000", "review", "pause", level=-15000)
        journal_.ack("pause-15000")
        journal_.note("ending", reason="yu_written", code="yu_ended_in_writing")
        journal_.note("violation", code="send_without_record", source_seq=1)
        journal_.note("gap_resolved", stop_id="cleared-figure-gap-9", decision_seq=9, cause_written=True)
        journal_.note("finalized", ref_col=placed.ref, final_at_ns=placed.off + DAY_NS, row=1, **{"class": "eligible"})
        journal_.note("bsp_decided", ref_col=placed.ref, decided_at_ns=placed.off + 5 * MIN, outcome="bsp",
                      actual_sp="3.10", read_seq=1)
        payloads = [e for e in journal_.events if e["kind"] == "note"]
        payloads.append({"payload": {"pilot": result["record"]}, "market_id": placed.market,
                         "customer_order_ref": None})
        refused = pilot.classify(C, journal_.fold(), reservation_seq=999, open_id="o", session_id="s0",
                                 registration_receipt_sha256=journal_.registration, customer_order_ref="r",
                                 row_body=b"", book_body=b"", observed_wall_ns=OFF, observed_monotonic_ns=1)
        payloads.append({"payload": {"pilot": refused["refusal_record"]}, "market_id": None,
                         "customer_order_ref": None})
        for stop in pilot.pause_stops(C, -15000) + refused["stops"]:
            payloads.append({"payload": {"pilot": stop}, "market_id": None, "customer_order_ref": None})
        kinds = set()
        for event in payloads:
            kinds.add(pilot.validate_pilot_payload(event["payload"]))
            journal.prepare_event("note", event["payload"], market_id=event.get("market_id"),
                                  customer_order_ref=event.get("customer_order_ref"))
        self.assertEqual(kinds, set(pilot.NOTE_FIELDS))

    def test_payload_has_no_forbidden_top_keys(self):
        journal_ = Pilot()
        journal_.arm(0)
        for event in journal_.events:
            payload = event["payload"]
            self.assertEqual(set(payload), {"pilot"})
            secret.refuse_forbidden(payload, where="test")
            for key in ("transitions", "transactions", "halts", "paper_state"):
                self.assertNotIn(key, payload)
        for kind, fields in pilot.NOTE_FIELDS.items():
            for name in fields:
                normalised = name.lower().replace("_", "").replace("-", "")
                self.assertNotIn(normalised, secret.FORBIDDEN_KEYS, (kind, name))
        self.assertRaises(PilotError, pilot.validate_pilot_payload, {"pilot": {}, "transitions": []})
        self.assertIsNone(pilot.validate_pilot_payload({"reason": "bulk"}))


class ContractTests(unittest.TestCase):
    def test_load_contract_p1(self):
        self.assertEqual(hashlib.sha256(RAW).hexdigest(), CONTRACT_SHA256)
        self.assertEqual(pilot.contract_sha256(RAW), CONTRACT_SHA256)
        parsed = json.loads(RAW)
        self.assertEqual(len(parsed), 27)
        self.assertEqual(set(parsed), pilot.CONTRACT_KEYS)
        expected = dict(
            sha256=CONTRACT_SHA256, id="p1", customer_strategy_ref="p1-fav-t5", intent_source="pilot",
            commission_rate=Fraction(1, 50), country_codes=frozenset({"GB", "IE"}), event_type_id="7",
            market_type="WIN", betting_type="ODDS", number_of_winners=1, price_ladder="CLASSIC",
            min_active_runners=5, window_from_offset_s=-300, window_length_s=15, send_deadline_offset_s=-240,
            side="BACK", order_type="LIMIT", persistence_type="LAPSE", size_p=200, best_back_min_h=150,
            best_back_max_h=400, min_available_p=1000, max_lay_ticks=2, lifetime_place_records=495,
            per_utc_day=25, per_pt4_session_day=5, pause_step_p=15000, new_place_counted_at_p=-200,
            gap_tolerance_p=1, deadline_s=604800, bsp_cutoff_s=86400, n_eligible=400, alpha_one_sided=Fraction(1, 40),
            min_clusters=15, fill_rate_min=Fraction(4, 5), missing_bsp_max=Fraction(1, 50),
            unresolved_max=Fraction(1, 100), final_date="2027-06-30", class_order=pilot.CLASS_ORDER)
        self.assertEqual(dataclasses.asdict(C), expected)
        self.assertEqual(C.final_date_ns, T("2027-06-30T00:00:00Z"))
        self.assertEqual(pilot.load_contract(parsed, raw_sha256=CONTRACT_SHA256), C)

    def test_contract_refuses(self):
        def changed(path, value):
            parsed = json.loads(RAW)
            target = parsed
            *parents, last = path.split(".")
            for part in parents:
                target = target[part]
            if value is DROP:
                del target[last]
            else:
                target[last] = value
            return parsed

        cases = {
            "status draft": changed("status", "draft"), "frozen_on null": changed("frozen_on", None),
            "extra key": dict(json.loads(RAW), extra=1), "missing key": changed("units", DROP),
            "float rate": changed("commission_rate", 0.02), "float deep": changed("ledger.gap_tolerance_p", 1.0),
            "bool for int": changed("sample.n_eligible", True), "decimal rate": changed("commission_rate", Decimal("0.02")),
            "class order": changed("measurement_classes.order", list(reversed(pilot.CLASS_ORDER))),
            "side LAY": changed("rule.order.side", "LAY"), "intent source": changed("intent_source", "api"),
            "persistence": changed("rule.order.persistence_type", "PERSIST"),
            "two instructions": changed("rule.order.instructions_per_request", 2),
            "two decisions": changed("rule.decisions_per_market_for_the_contract_life", 2),
            "rate text": changed("inference.alpha_one_sided", "1/40"), "one_minus_c": changed("metric.one_minus_c", "0.97"),
            "schema": changed("schema", "betangel.pilot-contract/v2"),
            "off-ladder price": changed("rule.selection.best_back_max_h", 405),
        }
        for label, parsed in cases.items():
            with self.subTest(case=label), self.assertRaises(PilotError) as caught:
                pilot.load_contract(parsed, raw_sha256=CONTRACT_SHA256)
            self.assertTrue(caught.exception.code.startswith("contract_invalid:"))
        for bad in ("0" * 63, "A" * 64, None):
            with self.subTest(sha=bad), self.assertRaises(PilotError):
                pilot.load_contract(json.loads(RAW), raw_sha256=bad)
        self.assertRaises(PilotError, pilot.contract_from_bytes, RAW.replace(b'"0.02"', b'"0.02", "x": 1'))


class Predicates(unittest.TestCase):
    """Note §4's predicates, each left out in turn (N:208), on raw catalogue and book bytes."""

    def assert_decision(self, expected, row=None, book=None, **kw):
        outcome, code, _ = decide(row, book, **kw)
        self.assertEqual((outcome, code), ("PLACE", None) if expected == "PLACE" else ("SKIP", expected))

    def test_base_place(self):
        outcome, code, found = decide()
        self.assertEqual(found, {"outcome": "PLACE", "code": None, "selection_id": FAV, "limit_price_h": 300,
                                 "active_selection_ids": [11, 12, 13, 14, 15, 16],
                                 "send_deadline_ns": OFF - 240 * S})
        self.assertIs(type(found["send_deadline_ns"]), int)
        journal_ = Pilot(pt4=True)
        result, placed, _ = journal_.classify(journal_.arm(0), off=OFF + 3 * DAY_NS)
        record = result["record"]
        self.assertEqual((record["side"], record["order_type"], record["limit_price_h"], record["stake_p"],
                          record["persistence_type"]), ("BACK", "LIMIT", 300, 200, "LAPSE"))
        self.assertEqual(record["send_deadline_ns"], placed.off - 240 * S)
        self.assertEqual(record["active_selection_ids"], [11, 12, 13, 14, 15, 16])
        self.assertEqual(set(record), {
            "v", "type", "contract_sha256", "registration_receipt_sha256", "session_id", "open_id",
            "reservation_seq", "market_id", "outcome", "code", "selection_id", "side", "order_type",
            "limit_price_h", "stake_p", "persistence_type", "customer_order_ref", "country", "off", "off_ns",
            "decision_book_observed_at", "decision_book_wall_ns", "decision_book_monotonic_ns",
            "is_market_data_delayed", "send_deadline_ns", "row_sha256", "book_sha256", "active_selection_ids",
            "utc_day", "day_limit", "lifetime_slot", "day_count"})
        for key in ("off_ns", "decision_book_wall_ns", "send_deadline_ns"):
            self.assertIs(type(record[key]), int)

    def test_country_ie_place(self):
        row = catalogue_item()
        row["event"]["countryCode"] = "IE"
        self.assert_decision("PLACE", row=row)

    def test_market_skips(self):
        def with_event(**changes):
            row = catalogue_item()
            row["event"].update(changes)
            return row

        def with_description(**changes):
            row = catalogue_item()
            row["description"].update(changes)
            return row

        removed = base_runners(r15=(BASE_RUNNERS[15], BASE_LAYS[15], "REMOVED"),
                               r16=(BASE_RUNNERS[16], BASE_LAYS[16], "REMOVED"))
        cases = {
            "test_country_fr": (dict(row=with_event(countryCode="FR")), "country_not_allowed"),
            "test_country_missing": (dict(row=catalogue_item(event=DROP)), "country_not_allowed"),
            "test_event_type_4339": (dict(row=catalogue_item(eventType={"id": "4339"})), "event_type_not_allowed"),
            "test_event_type_int": (dict(row=catalogue_item(eventType={"id": 7})), "event_type_not_allowed"),
            "test_market_type_place": (dict(row=with_description(marketType="PLACE")), "market_type_not_allowed"),
            "test_betting_type": (dict(row=with_description(bettingType="ASIAN_HANDICAP_DOUBLE_LINE")),
                                  "betting_type_not_odds"),
            "test_winners_2": (dict(book=book_item(numberOfWinners=2)), "number_of_winners_not_1"),
            "test_ladder_finest": (dict(row=with_description(priceLadderDescription={"type": "FINEST"})),
                                   "ladder_not_classic"),
            "test_active_4": (dict(book=book_item(runners=removed)), "too_few_active_runners"),
            "test_active_5": (dict(book=book_item(runners=base_runners(
                r16=(BASE_RUNNERS[16], BASE_LAYS[16], "REMOVED")))), "PLACE"),
            "row missing description": (dict(row=catalogue_item(description=DROP)), "market_type_not_allowed"),
            "row missing eventType": (dict(row=catalogue_item(eventType=DROP)), "event_type_not_allowed"),
        }
        for label, (kw, expected) in cases.items():
            with self.subTest(case=label):
                self.assert_decision(expected, **kw)

    def test_window(self):
        cases = {"test_window_minus_301": (301, "window"), "test_window_minus_300": (300, "PLACE"),
                 "test_window_minus_285": (285, "PLACE"), "test_window_minus_284": (284, "window")}
        for label, (seconds, expected) in cases.items():
            with self.subTest(case=label):
                self.assert_decision(expected, observed=OFF - seconds * S)
        self.assert_decision("window", observed=OFF - 285 * S + 1)
        self.assert_decision("window", observed=OFF - 300 * S - 1)

    def test_window_uses_marketStartTime(self):
        row = catalogue_item()
        row["event"]["openDate"] = pilot.utc_text_from_ns(OFF - 600 * S)
        self.assert_decision("PLACE", row=row)
        self.assert_decision("window", row=row, observed=OFF - 600 * S)

    def test_off_missing_and_bad(self):
        self.assert_decision("off_missing", row=catalogue_item(marketStartTime=DROP))
        self.assert_decision("off_missing", row=catalogue_item(marketStartTime="2027-13-01T15:00:00.000Z"))
        self.assert_decision("off_missing", row=catalogue_item(marketStartTime=17))

    def test_tie(self):
        book = book_item(runners=base_runners(r12=((("3.00", "40"),), (("3.05", "5"),), "ACTIVE")))
        self.assert_decision("favourite_tie", book=book)
        book = book_item(runners=base_runners(r12=((("3.0", "40"),), (("3.05", "5"),), "ACTIVE")))
        self.assert_decision("favourite_tie", book=book)
        removed_tie = book_item(runners=base_runners(r12=((("3.00", "40"),), (("3.05", "5"),), "REMOVED")))
        self.assert_decision("PLACE", book=removed_tie)

    def test_runner_order_irrelevant(self):
        """Every permutation of runners and of ladder levels gives the same result (property)."""
        rng = random.Random(7)
        books = [book_item(), book_item(runners=base_runners(r12=((("3.00", "40"),), (("3.05", "5"),), "ACTIVE"))),
                 book_item(runners=base_runners(r11=((("3.05", "15"), ("3.00", "15"), ("2.5", "1")),
                                                     (("3.10", "1"), ("3.15", "9")), "ACTIVE")))]
        for book in books:
            expected = decide(book=book)
            for _ in range(max(SEEDS, 20)):
                shuffled = json.loads(dumps(book), parse_float=Raw)
                rng.shuffle(shuffled["runners"])
                for item in shuffled["runners"]:
                    rng.shuffle(item["ex"]["availableToBack"])
                    rng.shuffle(item["ex"]["availableToLay"])
                with self.subTest(book=books.index(book)):
                    self.assertEqual(decide(book=shuffled), expected)              # the whole predicates dict

    def favourite(self, back, lay, size="15.00"):
        return book_item(runners=base_runners(r11=(((back, size),), ((lay, "20"),) if lay else (), "ACTIVE")))

    def test_best_back_range(self):
        cases = {("1.49", "1.50"): "best_back_out_of_range", ("1.50", "1.51"): "PLACE", ("4.00", "4.10"): "PLACE",
                 ("4.10", "4.20"): "best_back_out_of_range"}
        others = dict(r12=((("4.5", "30"),), (("4.6", "30"),), "ACTIVE"))
        for (back, lay), expected in cases.items():
            book = book_item(runners=base_runners(r11=(((back, "15.00"),), ((lay, "20"),), "ACTIVE"), **others))
            with self.subTest(back=back):
                self.assert_decision(expected, book=book)

    def test_depth(self):
        for size, expected in (("9.99", "depth_below_min"), ("10.00", "PLACE"), ("10", "PLACE"),
                               ("10.000000000000002", "PLACE"), ("9.999999999999998", "depth_below_min")):
            with self.subTest(size=size):
                self.assert_decision(expected, book=self.favourite("3.00", "3.05", size))

    def test_spread(self):
        cases = [("3.00", "3.10", "PLACE"), ("3.00", "3.15", "spread_too_wide"), ("1.99", "2.02", "PLACE"),
                 ("1.99", "2.04", "spread_too_wide"), ("4.00", "4.20", "PLACE"), ("4.00", "4.30", "spread_too_wide"),
                 ("3.00", "3.05", "PLACE")]
        for back, lay, expected in cases:
            with self.subTest(back=back, lay=lay):
                self.assert_decision(expected, book=self.favourite(back, lay))

    def test_lay_cases(self):
        self.assert_decision("best_lay_missing", book=self.favourite("3.00", None))
        self.assert_decision("book_crossed", book=self.favourite("3.00", "2.98"))
        self.assert_decision("book_crossed", book=self.favourite("3.00", "3.00"))
        self.assert_decision("best_lay_off_ladder", book=self.favourite("3.00", "3.07"))

    def test_limit_not_snapped(self):
        self.assertEqual(decide(book=self.favourite("3.05", "3.10"))[2]["limit_price_h"], 305)

    def test_off_ladder_back(self):
        self.assert_decision("best_back_off_ladder", book=self.favourite("3.444", "3.50"))
        self.assert_decision("best_back_off_ladder", book=self.favourite("3.43", "3.50"))

    def test_delayed(self):
        self.assert_decision("book_delayed", book=book_item(isMarketDataDelayed=True))
        self.assert_decision("book_delayed", book=book_item(isMarketDataDelayed=DROP))
        self.assert_decision("book_delayed", book=book_item(isMarketDataDelayed=None))
        self.assert_decision("book_delayed", book=book_item(isMarketDataDelayed=0))

    def test_empty_back_active_runner(self):
        """An ACTIVE runner with no back levels counts toward the 5 but is never the favourite (decision Q6)."""
        runners = base_runners(r15=(BASE_RUNNERS[15], BASE_LAYS[15], "REMOVED"),
                               r16=(BASE_RUNNERS[16], BASE_LAYS[16], "REMOVED"))
        runners.append(runner(17, (), (("2.00", "9"),)))
        outcome, code, found = decide(book=book_item(runners=runners))
        self.assertEqual((outcome, found["selection_id"], found["active_selection_ids"]), ("PLACE", FAV,
                                                                                            [11, 12, 13, 14, 17]))
        only_empty = [runner(s, (), ()) for s in (11, 12, 13, 14, 15)]
        self.assert_decision("no_priced_active_runner", book=book_item(runners=only_empty))

    def test_evidence_invalid(self):
        two = rpc(book_item(), book_item("1.999"))
        dup = base_runners()
        dup.append(runner(11, (("5.0", "1"),), ()))
        dup_price = base_runners(r11=((("3.00", "15"), ("3.00", "20")), BASE_LAYS[11], "ACTIVE"))
        cases = {
            "test_evidence_market_mismatch": dict(book=book_item("1.999")),
            "test_evidence_dup_selection": dict(book=book_item(runners=dup)),
            "test_evidence_dup_price": dict(book=book_item(runners=dup_price)),
            "nan price": dict(book=book_item(runners=base_runners(
                r12=(((Raw("NaN"), "1"),), (), "ACTIVE")))),
            "negative size": dict(book=book_item(runners=base_runners(r12=((("4.5", "-1"),), (), "ACTIVE")))),
            "price 1.00": dict(book=book_item(runners=base_runners(r12=((("1.00", "1"),), (), "ACTIVE")))),
            "string price": dict(book=book_item(runners=[runner(11, ((json.dumps("3.00"), "15"),), ())])),
            "runners missing": dict(book={k: v for k, v in book_item().items() if k != "runners"}),
        }
        for label, kw in cases.items():
            with self.subTest(case=label):
                self.assert_decision("evidence_invalid", **kw)
        self.assertRaises(pilot.EvidenceError, pilot.parse_rpc_single, two, market_id=MARKET)
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        result, _, _ = journal_.classify(session, off=OFF + 3 * DAY_NS, book=book_item(runners=dup))
        self.assertEqual((result["refusal"], result["record"]["outcome"], result["record"]["code"],
                          result["stops"]), (None, "SKIP", "evidence_invalid", []))
        self.assertIsNone(result["record"]["active_selection_ids"])

    def test_no_caller_country_parameter(self):
        parameters = set(inspect.signature(pilot.classify).parameters)
        self.assertEqual(parameters & {"country", "off", "off_ns", "book", "catalogue", "snapshot", "row",
                                       "market_time", "evidence"}, set())
        self.assertEqual({p for p in parameters if p.endswith("_body")}, {"row_body", "book_body"})

    def test_record_digests_and_sha(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = OFF + 3 * DAY_NS
        result, placed, res = journal_.classify(session, off=off, journal_it=False)
        row_body, book_body = bodies(placed.market if placed else "1.100001", off)
        record = result["record"]
        self.assertEqual(record["row_sha256"], hashlib.sha256(row_body).hexdigest())
        self.assertEqual(record["book_sha256"], hashlib.sha256(book_body).hexdigest())
        digest = pilot.record_sha256(record)
        self.assertEqual(digest, receipts.canonical_sha256(record))
        state = journal_.fold(now=off - 295 * S, live_open_id=record["open_id"])
        again = pilot.classify(C, state, reservation_seq=res, open_id=record["open_id"], session_id=session,
                               registration_receipt_sha256=journal_.registration,
                               customer_order_ref=record["customer_order_ref"], row_body=row_body,
                               book_body=book_body, observed_wall_ns=off - 295 * S, observed_monotonic_ns=7)
        self.assertEqual(pilot.record_sha256(again["record"]), digest)
        self.assertEqual(pilot.record_sha256(json.loads(json.dumps(record))), digest)

    def test_check_binding_refusals(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        result, placed, _ = journal_.classify(session, off=OFF + 3 * DAY_NS)
        record = result["record"]
        good = dict(contract=C, market_id=record["market_id"], instruction=pilot.expected_instruction(record),
                    customer_order_ref=record["customer_order_ref"], customer_strategy_ref="p1-fav-t5",
                    open_id=record["open_id"], session_id=session, record_digest=pilot.record_sha256(record))
        self.assertIsNone(pilot.check_binding(record, **good))
        schemas.validate_instruction_v1(good["instruction"])
        body = good["instruction"]
        other = dataclasses.replace(C, sha256="f" * 64)
        cases = {
            "r13_not_place": (dict(record, outcome="SKIP"), {}),
            "r13_other_open": (record, dict(open_id="open-x")),
            "r13_other_session": (record, dict(session_id="s9")),
            "r13_other_contract": (record, dict(contract=other)),
            "r13_digest_mismatch": (record, dict(record_digest="0" * 64)),
            "r13_market_differs": (record, dict(market_id="1.999")),
            "r13_market_differs missing": (record, dict(market_id=None)),
            "r13_body_differs price +1 tick": (record, dict(instruction=dict(body, price_h=305))),
            "r13_body_differs price -1 tick": (record, dict(instruction=dict(body, price_h=298))),
            "r13_body_differs size 199": (record, dict(instruction=dict(body, size_p=199))),
            "r13_body_differs side LAY": (record, dict(instruction=dict(body, side="LAY"))),
            "r13_body_differs persistence": (record, dict(instruction=dict(body, persistence_type="PERSIST"))),
            "r13_body_differs handicap float": (record, dict(instruction=dict(body, handicap=0.0))),
            "r13_ref_differs": (record, dict(customer_order_ref="ref-other")),
            "r13_strategy_ref_differs": (record, dict(customer_strategy_ref="other-ref")),
        }
        for label, (rec, changes) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(pilot.check_binding(rec, **dict(good, **changes)), label.split(" ")[0])


class Capacity(unittest.TestCase):
    """Capacity and the pause at classification, reservations and SKIP(interrupted) (note §4, §6, §10)."""

    @classmethod
    def setUpClass(cls):
        journal_ = Pilot(pt4=True)
        cls.session = journal_.arm(0)
        cls.state = journal_.fold(now=DAY0 + 9 * HOUR)
        pt4 = Pilot()
        cls.pt4_session = pt4.arm(0)
        cls.pt4_state = pt4.fold(now=DAY0 + 9 * HOUR)

    def capacity(self, state=None, session=None, day="2027-01-04", **changes):
        state = dataclasses.replace(state or self.state, **changes)
        return pilot.capacity_and_pause(C, state, utc_day=day, session_id=session or self.session, source_seq=40)

    def test_caps(self):
        cases = {
            "test_cap_lifetime_494": (dict(lifetime_used=494), None, 495, None),
            "test_cap_lifetime_495": (dict(lifetime_used=495), "cap", None, None),
            "test_cap_day_24": (dict(day_used={"2027-01-04": 24}), None, 1, 25),
            "test_cap_day_25": (dict(day_used={"2027-01-04": 25}), "cap", None, None),
        }
        for label, (changes, code, slot, count) in cases.items():
            with self.subTest(case=label):
                found = self.capacity(**changes)
                self.assertEqual(found["code"], code)
                if code is None:
                    self.assertEqual(found["lifetime_slot"], slot)
                    if count:
                        self.assertEqual(found["day_count"], count)
                self.assertEqual(found["day_limit"], 25)
        day_cap = self.capacity(day_used={"2027-01-04": 25})
        self.assertEqual([(s["scope"], s["reason"], s["stop_id"]) for s in day_cap["stops"]],
                         [("session", "day_limit", "day-limit-40-session")])
        self.assertEqual(self.capacity(lifetime_used=495)["stops"], [])

    def test_cap_pt4_day(self):
        four = self.capacity(self.pt4_state, self.pt4_session, day_used={"2027-01-04": 4})
        self.assertEqual((four["code"], four["day_count"], four["day_limit"]), (None, 5, 5))
        five = self.capacity(self.pt4_state, self.pt4_session, day_used={"2027-01-04": 5})
        self.assertEqual((five["code"], five["day_limit"]), ("cap", 5))

    def test_pause_at_classification(self):
        cases = {
            "test_pause_from_14800": (dict(equity_p=-14800), "pause", -15000),
            "test_place_at_14799": (dict(equity_p=-14799), None, None),
            "test_ack_then_place": (dict(equity_p=-14800, pause_acked={-15000: 9}), None, None),
            "test_next_level": (dict(equity_p=-29800, pause_acked={-15000: 9}), "pause", -30000),
            "test_pause_beats_cap": (dict(equity_p=-14800, lifetime_used=495), "pause", -15000),
        }
        for label, (changes, code, level) in cases.items():
            with self.subTest(case=label):
                found = self.capacity(**changes)
                self.assertEqual((found["code"], found["level_p"]), (code, level))
        found = self.capacity(equity_p=-14800)
        self.assertEqual([(s["stop_id"], s["scope"], s["reason"], s["level_p"]) for s in found["stops"]],
                         [("pause-15000-session", "session", "pause", -15000),
                          ("pause-15000", "review", "pause", -15000)])

    def test_two_levels_one_change(self):
        """One stop per level, each with its own ACK (decision Q20); a single bet cannot move this far, so pure."""
        self.assertEqual(pilot.pause_levels_hit(-30100, step_p=15000, acked=set()), [-15000, -30000])
        self.assertEqual(pilot.pause_levels_hit(-14999, step_p=15000, acked=set()), [])
        self.assertEqual(pilot.pause_levels_hit(-15000, step_p=15000, acked={-15000}), [])
        self.assertEqual(pilot.pause_levels_hit(400, step_p=15000, acked=set()), [])
        found = self.capacity(equity_p=-29900)
        self.assertEqual([s["stop_id"] for s in found["stops"]],
                         ["pause-15000-session", "pause-15000", "pause-30000-session", "pause-30000"])

    def test_race_last_slot(self):
        """Two heads, same bytes: on 494 used the record takes slot 495; on the moved head (495 used) it is
        SKIP(cap) with no refetch argument (note §4 step 3)."""
        results = []
        for used in (494, 495):
            journal_ = Pilot(pt4=True)
            bulk(journal_, used)
            session = journal_.arm(30)
            off = journal_.next_off(session)
            market, open_id = "1.999999", "open-race"
            res = journal_.reserve(market, off, session=session, open_id=open_id)
            row_body, book_body = bodies(market, off)
            state = journal_.fold(now=off - 295 * S, live_open_id=open_id)
            self.assertEqual(state.lifetime_used, used)
            results.append(pilot.classify(C, state, reservation_seq=res, open_id=open_id, session_id=session,
                                          registration_receipt_sha256=journal_.registration,
                                          customer_order_ref="ref-race", row_body=row_body, book_body=book_body,
                                          observed_wall_ns=off - 295 * S, observed_monotonic_ns=7))
        first, second = results
        self.assertEqual((first["record"]["outcome"], first["record"]["lifetime_slot"]), ("PLACE", 495))
        self.assertEqual((second["refusal"], second["record"]["outcome"], second["record"]["code"]),
                         (None, "SKIP", "cap"))
        self.assertEqual(second["record"]["row_sha256"], first["record"]["row_sha256"])

    def test_second_reservation_refused(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        _, placed, _ = journal_.classify(session, market="1.555")
        second = journal_.reserve("1.555", placed.off + 30 * MIN, session=session, open_id="open-again")
        state = journal_.fold(live_open_id="open-again")
        self.assertIn("second_reservation", codes(state))
        self.assertEqual(state.ending["reason"], "breach")
        result = pilot.classify(C, state, reservation_seq=second, open_id="open-again", session_id=session,
                                registration_receipt_sha256=journal_.registration, customer_order_ref="ref-x",
                                row_body=b"", book_body=b"", observed_wall_ns=state.now_ns, observed_monotonic_ns=1)
        self.assertEqual(result["refusal"], "placing_ended")
        clean = Pilot(pt4=True)
        session = clean.arm(0)
        clean.classify(session, market="1.555")
        state = clean.fold(now=DAY0 + 11 * HOUR, live_open_id="open-again")
        result = pilot.classify(C, state, reservation_seq=999, open_id="open-again", session_id=session,
                                registration_receipt_sha256=clean.registration, customer_order_ref="ref-x",
                                row_body=b"", book_body=b"", observed_wall_ns=DAY0 + 11 * HOUR,
                                observed_monotonic_ns=1)
        self.assertEqual(result["refusal"], "reservation_unknown")
        self.assertEqual((result["stops"], result["refusal_record"]["code"]), ([], "reservation_unknown"))
        seq = clean.inner(result["refusal_record"])
        self.assertEqual(stop_ids(clean.fold().due_stops), [(f"reservation-unknown-999-{seq}", "review")])

    def test_interrupted_skip(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = journal_.next_off(session)
        res = journal_.reserve("1.700", off, session=session, open_id="open-crashed")
        for _ in range(2):
            state = journal_.fold(live_open_id="open-later")
            self.assertTrue(state.reservations["1.700"]["interrupted"])
            self.assertEqual((state.lifetime_used, state.violations, state.place_records), (0, (), ()))
            self.assertEqual(state, pilot.fold(json.loads(json.dumps(journal_.events)), contract=C,
                                               now_ns=journal_.now, live_open_id="open-later"))
        result = pilot.classify(C, journal_.fold(now=off - 295 * S, live_open_id="open-later"), reservation_seq=res,
                                open_id="open-crashed", session_id=session,
                                registration_receipt_sha256=journal_.registration, customer_order_ref="ref-1",
                                row_body=bodies("1.700", off)[0], book_body=bodies("1.700", off)[1],
                                observed_wall_ns=off - 295 * S, observed_monotonic_ns=1)
        self.assertEqual(result["refusal"], "reservation_interrupted")
        journal_.reserve("1.700", off, session=session, open_id="open-later")
        self.assertIn("second_reservation", codes(journal_.fold(live_open_id="open-later")))

    def test_interrupted_not_in_own_open(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        journal_.reserve("1.700", journal_.next_off(session), session=session, open_id="open-live")
        state = journal_.fold(live_open_id="open-live")
        self.assertFalse(state.reservations["1.700"]["interrupted"])
        self.assertIsNone(state.reservations["1.700"]["decision_seq"])

    def test_place_no_submit_crash(self):
        journal_ = Pilot(pt4=True)
        _, placed, _ = journal_.classify(journal_.arm(0))
        rec = only(journal_.fold(live_open_id=placed.open_id))
        self.assertEqual((rec.unsent, rec.ledger["value_p"]), (False, -200))
        state = journal_.fold(now=placed.off + DAY_NS + 1)
        rec = only(state)
        self.assertEqual((state.lifetime_used, rec.unsent, rec.ledger["value_p"]), (1, True, 0))
        self.assertEqual(pilot.measurement_class(rec, now_ns=state.now_ns)["class"], "definitely_unsent")

    def test_no_release(self):
        """No reservation is released by a cancel, a settle, a restart or RESUME (note §6)."""
        def cancel(j, p):
            j.add("report", {"action": "cancel", "outcome": "reported", "transitions": [],
                             "report": {"status": "SUCCESS", "error_code": None, "market_id": p.market,
                                        "instructions": [{"bet_id": p.bet_id, "size_cancelled_p": 200}],
                                        "source": "betfair"}}, market_id=p.market)

        steps = {"cancel": cancel, "settle": lambda j, p: j.settle(p, at=j.now + S), "restart": lambda j, p: None,
                 "RESUME": lambda j, p: j.add("resume", {"scope": "global"})}
        for label, step in steps.items():
            with self.subTest(case=label):
                journal_ = Pilot(pt4=True)
                session = journal_.arm(0)
                _, placed, _ = journal_.classify(session)
                journal_.send(placed, 0)
                step(journal_, placed)
                second = journal_.reserve(placed.market, placed.off + HOUR, session=session, open_id="open-new")
                state = journal_.fold(live_open_id="open-new")
                self.assertIn("second_reservation", codes(state))
                self.assertNotIn(second, [r["seq"] for r in state.reservations.values()])

    def test_record_other_open_refused(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = journal_.next_off(session)
        res = journal_.reserve("1.800", off, session=session, open_id="open-a")
        row_body, book_body = bodies("1.800", off)
        state = journal_.fold(now=off - 295 * S, live_open_id="open-a")
        refused = pilot.classify(C, state, reservation_seq=res, open_id="open-b", session_id=session,
                                 registration_receipt_sha256=journal_.registration, customer_order_ref="ref-b",
                                 row_body=row_body, book_body=book_body, observed_wall_ns=off - 295 * S,
                                 observed_monotonic_ns=1)
        self.assertEqual((refused["refusal"], refused["record"]), ("reservation_other_open", None))
        self.assertEqual((refused["stops"], refused["refusal_record"]["reservation_seq"]), ([], res))
        good = pilot.classify(C, state, reservation_seq=res, open_id="open-a", session_id=session,
                              registration_receipt_sha256=journal_.registration, customer_order_ref="ref-b",
                              row_body=row_body, book_body=book_body, observed_wall_ns=off - 295 * S,
                              observed_monotonic_ns=1)
        journal_.inner(dict(good["record"], open_id="open-b"), market_id="1.800")
        state = journal_.fold(live_open_id="open-b")
        self.assertIn("decision_other_open", codes(state))
        self.assertEqual(only(state).measurement["class"], "protocol_violation")

    def test_decision_order_is_reservation_seq(self):
        """Res A (open 1), res B (open 2), record B, record A: one open at a time (note §4), so record A comes
        from an open that had ended; the order is still [A, B] by reservation seq, and A is a violation
        (critique 19)."""
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off_a, off_b = journal_.next_off(session), journal_.next_off(session)
        records = {}
        for name, off, market in (("a", off_a, "1.901"), ("b", off_b, "1.902")):
            records[name] = (journal_.reserve(market, off_a, session=session, open_id=f"open-{name}"), market)
        decisions = {}
        for name in ("b", "a"):
            res, market = records[name]
            observed = off_a - 295 * S
            row_body, book_body = bodies(market, off_a)
            ev = pilot.decision_evidence(row_body, book_body, market_id=market)
            record = pilot.decision_record(
                C, outcome="PLACE", code=None, registration_receipt_sha256=journal_.registration,
                session_id=session, open_id=f"open-{name}", reservation_seq=res, market_id=market,
                customer_order_ref=f"ref-{name}", ev=ev, row_body=row_body, book_body=book_body,
                predicates=pilot.evaluate_predicates(C, ev, observed_wall_ns=observed), observed_wall_ns=observed,
                observed_monotonic_ns=1, day_limit=25, lifetime_slot=len(decisions) + 1, day_count=len(decisions) + 1)
            decisions[name] = journal_.inner(record, market_id=market)
        state = journal_.fold(live_open_id="open-b")
        self.assertEqual([r.ref for r in state.place_records], ["ref-a", "ref-b"])
        self.assertEqual([r.measurement["class"] for r in state.place_records][0], "protocol_violation")
        self.assertIn("decision_open_ended", codes(state))
        self.assertEqual(state.ending["reason"], "breach")

    def test_stop_active_refuses_classification(self):
        journal_ = Pilot(pt4=True)
        # An unacknowledged review stop from before this ARM (a stop inside a session ends it, Astra #1).
        stop_seq = journal_.stop("capture-check-failed-9", "review", "capture_check_failed", source=9,
                                 at=DAY0 + 7 * HOUR)
        session = journal_.arm(0)
        result, placed, res = journal_.classify(session)
        self.assertEqual((result["refusal"], result["record"]), ("stop_active", None))
        self.assertEqual((result["stops"], result["refusal_record"]["code"]), ([], "stop_active"))
        seq = journal_.inner(result["refusal_record"])
        self.assertIn((f"stop-active-{res}-{seq}", "review"), stop_ids(journal_.fold().due_stops))
        self.assertLess(stop_seq, res)

    def test_session_checks(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0, until_h=11)
        late, _, _ = journal_.classify(session, off=DAY0 + 11 * HOUR + 4 * MIN, observed=DAY0 + 11 * HOUR,
                                       journal_it=False)
        self.assertEqual(late["refusal"], "session_expired")
        self.assertEqual(codes(journal_.fold()), [])
        unarmed = Pilot(pt4=True)
        unarmed.sessions["s0"] = {"day": 0, "pt4": False, "count": 0}
        result, _, _ = unarmed.classify("s0", off=OFF + 3 * DAY_NS)
        self.assertEqual(result["refusal"], "placing_ended")             # the unarmed reservation is a breach
        self.assertEqual(codes(unarmed.fold()), ["session_not_armed"])
        other = Pilot(pt4=True)
        session = other.arm(0)
        off = other.next_off(session)
        res = other.reserve("1.300", off, session=session, open_id="open-1")
        row_body, book_body = bodies("1.300", off)
        at_decision = other.fold(now=off - 295 * S, live_open_id="open-1")
        refused = pilot.classify(C, at_decision, reservation_seq=res, open_id="open-1",
                                 session_id="s9", registration_receipt_sha256=other.registration,
                                 customer_order_ref="ref-1", row_body=row_body, book_body=book_body,
                                 observed_wall_ns=off - 295 * S, observed_monotonic_ns=1)
        self.assertEqual(refused["refusal"], "session_not_armed")
        refused = pilot.classify(C, at_decision, reservation_seq=res, open_id="open-1",
                                 session_id=session, registration_receipt_sha256=sha("other"),
                                 customer_order_ref="ref-1", row_body=row_body, book_body=book_body,
                                 observed_wall_ns=off - 295 * S, observed_monotonic_ns=1)
        self.assertEqual(refused["refusal"], "registration_mismatch")
        stopped = Pilot(pt4=True)
        session = stopped.arm(0)
        stopped.stop("day-limit-3-session", "session", "day_limit", source=3)
        self.assertEqual(stopped.classify(session)[0]["refusal"], "placing_ended")
        self.assertEqual(codes(stopped.fold()), ["session_stopped"])     # a reservation after a session stop


# ---------------------------------------------------------------------------------------------------------------

class LedgerPinned(unittest.TestCase):
    """The ledger's pinned examples, each a test (note §10 :132)."""

    def one(self, *, pt4=True):
        journal_ = Pilot(pt4=pt4)
        _, placed, _ = journal_.classify(journal_.arm(0))
        return journal_, placed

    def test_ledger_pending(self):
        journal_, p = self.one()
        journal_.send(p)
        self.assertEqual(only(journal_.fold()).ledger["value_p"], -200)
        journal_.c_read(p)
        rec = only(journal_.fold())
        self.assertEqual((rec.ledger["part"], rec.ledger["value_p"], rec.ledger["reason"]),
                         ("W", -200, "checked_current_read"))

    def test_ledger_never_matched(self):
        journal_, p = self.one()
        journal_.send(p, 0)
        self.assertEqual(only(journal_.fold()).ledger["value_p"], -200)
        journal_.c_read(p, matched="0.00", cancelled="2.00", price=None)
        journal_.closed(p)
        deadline = p.off + 7 * DAY_NS
        for now, final in ((p.off + 6 * DAY_NS, False), (deadline, False), (deadline + 1, True)):
            with self.subTest(now=now - p.off):
                rec = only(journal_.fold(now=now))
                self.assertEqual(rec.ledger["value_p"], 0)
                found = pilot.measurement_class(rec, now_ns=now)
                self.assertEqual(found["final"], final)
                if final:
                    self.assertEqual((found["class"], found["final_at_ns"]), ("unmatched", deadline + 1))

    def test_ledger_ordinary_winner(self):
        journal_, p = self.one()
        journal_.win(p)
        rec = only(journal_.fold())
        self.assertEqual((rec.ledger["part"], rec.ledger["value_p"]), ("S", 392))
        self.assertEqual(pilot.s_winner_value(Decimal("2.00"), Decimal("3.00"), 400, c=C.commission_rate),
                         {"gross_p": 400, "commission_p": 8, "net_p": 392, "gap_p": 0, "value_p": 392})
        verified = Pilot(pt4=True)
        _, q, _ = verified.classify(verified.arm(0))
        verified.win(q, commission=None)
        self.assertEqual(only(verified.fold()).ledger["reason"], "commission_unverified")
        verified.note("commission_verified", market_id=q.market, our_commission_p=8, website_commission_p=8)
        state = verified.fold()
        self.assertEqual((only(state).ledger["value_p"], state.commission_verified), (392, True))

    def test_ledger_unresolved_settlement(self):
        cases = {"test_ledger_profit_missing": (None, False), "test_ledger_profit_403": ("4.03", True),
                 "test_ledger_profit_397": ("3.97", True), "profit not to the penny": ("4.001", True)}
        for label, (profit, stop) in cases.items():
            with self.subTest(case=label):
                journal_, p = self.one()
                journal_.win(p, profit=profit)
                state = journal_.fold()
                self.assertEqual((only(state).ledger["part"], only(state).ledger["value_p"]), ("W", -200))
                gaps = [s for s in state.due_stops if s["reason"] == "cleared_figure_gap"]
                self.assertEqual(bool(gaps), stop)
                if stop:
                    l_seq = only(state).l_rows[0]["seq"]
                    self.assertEqual((gaps[0]["scope"], gaps[0]["stop_id"]),
                                     ("review", f"cleared-figure-gap-{p.seq}-{l_seq}"))

    def test_ledger_contradiction_restart(self):
        """Place report 0; C1 (0 matched, 2 cancelled) -> 0; C2 (1 matched, 1 cancelled) -> -200 and a review
        stop; with other equity at -14900 the total is -15100 and the -15000 pause fires; the analytical record
        still follows C1; a re-fold gives the same state."""
        journal_ = Pilot(pt4=True)
        placed = bulk(journal_, 76)
        target, partial, others = placed[0], placed[1], placed[2:]
        self.assertEqual(len(others), 74)
        # target, partial and the 74 others were placed in that order; their order events follow.
        journal_.send(target, 0, at=journal_.now + S)
        journal_.c_read(target, matched="0.00", cancelled="2.00", price=None, at=journal_.now + S)
        journal_.send(partial, 100, at=journal_.now + S)
        journal_.c_read(partial, matched="1.00", cancelled="1.00", at=journal_.now + S)
        for p in others:
            journal_.send(p, at=journal_.now + S)
        state = journal_.fold()
        self.assertEqual((state.equity_p, pilot.equity(state)), (-14900, -14900))
        self.assertEqual(state.place_records[0].ledger["value_p"], 0)
        self.assertEqual(state.pause_fired, {})
        c1 = [e["seq"] for e in journal_.events if e["kind"] == "note"
              and e["payload"]["pilot"]["type"] == "capture_c" and e["customer_order_ref"] == target.ref][0]
        c2 = journal_.c_read(target, matched="1.00", cancelled="1.00", at=journal_.now + S)
        for _ in range(2):
            state = journal_.fold()
            rec = state.place_records[0]
            self.assertEqual((rec.ledger["value_p"], rec.ledger["reason"], state.equity_p),
                             (-200, "contradiction", -15100))
            due = {(s["stop_id"], s["scope"]) for s in state.due_stops}
            self.assertEqual(due, {(f"ledger-contradiction-{target.seq}-{c2}", "review"),
                                   ("pause-15000", "review"), ("pause-15000-session", "session")})
            self.assertIn(-15000, state.pause_fired)
            self.assertEqual(state, pilot.fold(json.loads(json.dumps(journal_.events)), contract=C,
                                               now_ns=journal_.now))
        late = journal_.fold(now=target.off + 8 * DAY_NS)
        rec = late.place_records[0]
        self.assertEqual((rec.execution["c_seq"], rec.execution["size"], rec.execution["row"]), (c1, 0, 3))
        self.assertEqual(rec.ledger["value_p"], -200)

    def test_ledger_never_sent(self):
        refused, p = self.one()
        refused.intent(p)                     # V13 refused it before its submit: REFUSED, no submit event
        rec = only(refused.fold())
        self.assertEqual((rec.unsent, rec.ledger["value_p"], rec.ledger["reason"]), (True, 0, "not_sent"))
        failed, q = self.one()
        failed.intent(q)
        failed.submit(q)
        failed.failed(q)
        rec = only(failed.fold(live_open_id=q.open_id))
        self.assertEqual((rec.failed_not_sent, rec.ledger["value_p"]), (True, 0))

    def test_ledger_worst_case_bound(self):
        """485 settled £2 losers plus ten pending £2 bets: exactly -99000, never -101000 (the pause is not
        acknowledged here, so later records are protocol violations; their ledger values are unchanged)."""
        journal_ = Pilot(pt4=True)
        placed = bulk(journal_, 495, lambda j, p: j.lose(p) if p.record["lifetime_slot"] <= 485 else j.send(p))
        state = journal_.fold()
        parts = [(r.ledger["part"], r.ledger["value_p"]) for r in state.place_records]
        self.assertEqual(parts.count(("S", -200)), 485)
        self.assertEqual(parts.count(("W", -200)), 10)
        self.assertEqual((state.equity_p, pilot.equity(state)), (-99000, -99000))
        self.assertEqual(len(placed), C.lifetime_place_records)


class LedgerAdversarial(unittest.TestCase):
    """Adversarial ledger cases (note §14 :212-213)."""

    def one(self, *, pt4=True, **kw):
        journal_ = Pilot(pt4=pt4, **kw)
        _, placed, _ = journal_.classify(journal_.arm(0))
        return journal_, placed

    def test_gaps(self):
        cases = {"test_gap_2p": ("4.02", "W", -200, True), "test_gap_1p_up": ("4.01", "S", 392, False),
                 "test_gap_1p_down": ("3.99", "S", 391, False)}
        for label, (profit, part, value, stop) in cases.items():
            with self.subTest(case=label):
                journal_, p = self.one()
                journal_.win(p, profit=profit)
                state = journal_.fold()
                self.assertEqual((only(state).ledger["part"], only(state).ledger["value_p"]), (part, value))
                self.assertEqual(any(s["reason"] == "cleared_figure_gap" for s in state.due_stops), stop)

    def test_loser_sign(self):
        """A LOSER's gross is -ceil(size*100) against Betfair's -2.00 (critique 13): no spurious gap."""
        journal_, p = self.one()
        journal_.lose(p)
        state = journal_.fold()
        self.assertEqual((only(state).ledger["part"], only(state).ledger["value_p"], state.due_stops),
                         ("S", -200, ()))

    def test_always_w(self):
        def dead_heat(j, p):
            j.send(p)
            j.c_read(p)
            j.closed(p, ours="WINNER", winners={12})
            j.settle(p)
            j.l_row(p, profit="1.96", outcome="WIN")
            j.commission(p, "0.04")

        def void(j, p):
            j.send(p)
            j.c_read(p)
            j.closed(p, ours="REMOVED")
            j.settle(p)

        def removal(j, p):
            j.send(p)
            j.c_read(p)
            j.closed(p, removed=(12,))
            j.settle(p)
            j.l_row(p)
            j.commission(p, "0.00")

        cases = {"test_dead_heat": (dead_heat, {}, "dead_heat_or_no_winner", "eligible"),
                 "test_void_w": (void, {}, "void", "void"),
                 "test_removal_w": (removal, {}, "removal", "post_decision_removal"),
                 "test_no_pt4_receipt_w": (lambda j, p: j.lose(p), dict(pt4=False), "no_pt4_receipt", None),
                 "test_commission_unverified_w": (lambda j, p: j.win(p, commission=None), {},
                                                  "commission_unverified", None),
                 "test_profit_basis_net_w": (lambda j, p: (j.receipt("PT4", matched=1, basis="net"), j.lose(p)), {},
                                             "profit_basis_unverified", None),
                 "test_commission_gap": (lambda j, p: j.win(p, commission="0.10"), {}, "commission_gap", None)}
        for label, (lifecycle, kw, reason, cls) in cases.items():
            with self.subTest(case=label):
                journal_, p = self.one(**kw)
                lifecycle(journal_, p)
                state = journal_.fold(now=p.off + 8 * DAY_NS)
                rec = only(state)
                self.assertEqual((rec.ledger["part"], rec.ledger["reason"]), ("W", reason))
                if cls:
                    self.assertEqual(rec.measurement["class"], cls)
        journal_, p = self.one()
        journal_.win(p, commission="0.10")
        self.assertEqual(len(occurrences_of(journal_.fold().due_stops, "commission_gap", p.seq)), 1)
        mismatch, q = self.one()
        mismatch.win(q, commission=None)
        seq = mismatch.note("commission_verified", market_id=q.market, our_commission_p=8, website_commission_p=9)
        state = mismatch.fold()
        self.assertIn(f"commission-mismatch-{seq}", [s["stop_id"] for s in state.due_stops])
        self.assertFalse(state.commission_verified)

    def test_capture_check_failures(self):
        cases = {"test_capture_fail_below_report": (100, dict(matched="0.50", cancelled="1.50")),
                 "test_capture_fail_sum_199": (200, dict(matched="1.99", cancelled="0.00")),
                 "test_capture_artefact_sum": (100, dict(matched="1.3700000000000001", cancelled="0.63")),
                 "missing size": (200, dict(voided=None))}
        for label, (report, row) in cases.items():
            with self.subTest(case=label):
                journal_, p = self.one()
                journal_.send(p, report)
                read = journal_.c_read(p, **row)
                state = journal_.fold()
                self.assertEqual(only(state).ledger["value_p"], -200)
                self.assertIn(f"capture-check-failed-{read}", [s["stop_id"] for s in state.due_stops])
                late = journal_.fold(now=p.off + 8 * DAY_NS)
                self.assertEqual((only(late).execution["size"], only(late).measurement["class"]),
                                 (None, "measurement_unresolved"))
        self.assertTrue(pilot.capture_check(dec_row(current_row(matched="1.37", cancelled="0.63")),
                                            place_report_matched_p=100))
        self.assertFalse(pilot.capture_check(dec_row(current_row(matched="1.37", cancelled="0.63")),
                                             place_report_matched_p=138))

    def test_no_place_report_w(self):
        journal_, p = self.one()
        journal_.intent(p)
        journal_.submit(p)
        journal_.c_read(p, matched="0.00", cancelled="2.00", price=None)
        self.assertEqual(only(journal_.fold()).ledger["value_p"], -200)
        late = only(journal_.fold(now=p.off + 7 * DAY_NS + 1))
        self.assertEqual((late.execution["final"], late.ledger["value_p"]), (True, 0))

    def test_s_record_contradicted(self):
        journal_, p = self.one()
        journal_.win(p)
        self.assertEqual(only(journal_.fold()).ledger["part"], "S")
        journal_.reconcile(p, 300)
        for _ in range(2):
            state = journal_.fold()
            self.assertEqual((only(state).ledger["part"], only(state).ledger["value_p"]), ("W", -200))
            occurrence(state.due_stops, "ledger_contradiction", p.seq)
            self.assertEqual(only(state).execution["P"], Decimal("3.00"))
        for change in (dict(price="3.05"), dict(profit="3.99", outcome="WIN"), dict(outcome="LOSE", profit="4.00")):
            with self.subTest(change=change):
                other, q = self.one()
                other.win(q)
                other.l_row(q, **dict(dict(profit="4.00", outcome="WIN"), **change))
                self.assertEqual(only(other.fold()).ledger["reason"], "contradiction")

    def test_contradiction_sticky(self):
        journal_, p = self.one()
        journal_.send(p, 0)
        journal_.c_read(p, matched="0.00", cancelled="2.00", price=None)
        journal_.c_read(p, matched="1.00", cancelled="1.00")
        journal_.c_read(p, matched="0.00", cancelled="2.00", price=None)
        for now in (journal_.now, p.off + 8 * DAY_NS):
            rec = only(journal_.fold(now=now))
            self.assertEqual((rec.ledger["value_p"], rec.ledger["reason"]), (-200, "contradiction"))

    def test_s_arithmetic(self):
        rows = [("1.37", "3.44", 334, 7, 327), ("2.00", "1.53", 106, 3, 103), ("2.00", "3.475", 495, 10, 485),
                ("1.37", "3.449", 335, 7, 328), ("2.00", "3.444", 488, 10, 478), ("2.00", "1.01", 2, 1, 1)]
        for size, price, gross, commission, net in rows:
            with self.subTest(size=size, price=price):
                found = pilot.s_winner_value(Decimal(size), Decimal(price), gross, c=C.commission_rate)
                self.assertEqual((found["gross_p"], found["commission_p"], found["net_p"]), (gross, commission, net))
        self.assertEqual(pilot.loss_pence(Decimal("1.3700000000000001")), -138)
        self.assertEqual(pilot.loss_pence(Decimal("2.00")), -200)
        self.assertEqual(pilot.bf_profit_pence("-2.00"), -200)
        self.assertIsNone(pilot.bf_profit_pence(None))
        self.assertRaises(PilotError, pilot.bf_profit_pence, "4.001")
        self.assertRaises(PilotError, pilot.bf_profit_pence, "NaN")

    def test_s_matches_pnl_on_hundredths(self):
        rng = random.Random(11)
        ladder = [p for p in range(101, 1001) if pilot.is_on_ladder(p)]
        for _ in range(max(SEEDS, 40) * 10):
            price_h, size_p = rng.choice(ladder), rng.randrange(1, 201)
            found = pilot.s_winner_value(Fraction(size_p, 100), Fraction(price_h, 100), 0, c=C.commission_rate)
            self.assertEqual(found["gross_p"], pnl.order_pnl_pence("BACK", price_h, size_p, "WINNER"))


class Pause(unittest.TestCase):
    """The -15000 pause by a PLACE and by a ledger change, with its ACK rules (note §10 :133-138)."""

    def test_pause_pinned(self):
        """Settled equity -14800, nothing pending: the next PLACE is SKIP(pause) with a session and a review stop;
        Yu ACKs; in the next session the PLACE from -14800 to -15000 goes ahead, also after a restart; the next
        level stays armed; an acknowledged level never fires again."""
        journal_ = Pilot(pt4=True)
        bulk(journal_, 74, lambda j, p: j.lose(p))
        session = journal_.arm(10)
        state = journal_.fold()
        self.assertEqual((state.equity_p, state.due_stops, state.pause_fired), (-14800, (), {}))
        result, placed, _ = journal_.classify(session)
        self.assertEqual((result["record"]["outcome"], result["record"]["code"]), ("SKIP", "pause"))
        self.assertEqual([(s["stop_id"], s["scope"]) for s in result["stops"]],
                         [("pause-15000-session", "session"), ("pause-15000", "review")])
        state = journal_.fold()
        self.assertEqual(sorted(s["stop_id"] for s in state.active_stops), ["pause-15000", "pause-15000-session"])
        self.assertEqual(pilot.ack_check(state, "pause-15000", journal_reconciled=True, unresolved_order=False,
                                         cause_written=False), None)
        self.assertEqual({(s["stop_id"], s["scope"]) for s in state.stops},
                         {("pause-15000-session", "session"), ("pause-15000", "review")})
        journal_.ack("pause-15000")
        session = journal_.arm(11)
        for _ in range(2):
            state = journal_.fold()
            self.assertEqual(state.active_stops, ())
            self.assertEqual(state.pause_acked.keys(), {-15000})
        result, placed, _ = journal_.classify(session)
        self.assertEqual(result["record"]["outcome"], "PLACE")
        again = journal_.fold(live_open_id=placed.open_id)              # the order may still be sent: -200
        self.assertEqual((again.equity_p, again.due_stops, codes(again)), (-15000, (), []))
        self.assertEqual(again, pilot.fold(json.loads(json.dumps(journal_.events)), contract=C, now_ns=journal_.now,
                                           live_open_id=placed.open_id))
        at_30000 = pilot.capacity_and_pause(C, dataclasses.replace(again, equity_p=-29800), utc_day="2027-01-15",
                                            session_id=session)
        self.assertEqual((at_30000["code"], at_30000["level_p"]), ("pause", -30000))
        recovered = pilot.capacity_and_pause(C, dataclasses.replace(again, equity_p=-14800), utc_day="2027-01-15",
                                             session_id=session)
        self.assertIsNone(recovered["code"])
        self.assertEqual(pilot.pause_levels_hit(-15000, step_p=15000, acked=set(again.pause_acked)), [])

    def test_pause_by_ledger_change(self):
        """A ledger change, not a PLACE, takes equity from -14900 to -15100: the session and review stops are
        due at once. The change is a failed capture check on a later C read (sizes summing to 1.99): with a
        passed capture check the §11 consistency check cannot fail (see test_consistency_fail)."""
        journal_ = Pilot(pt4=True)
        placed = bulk(journal_, 76)
        target, partial, others = placed[0], placed[1], placed[2:]
        journal_.send(target, 0, at=journal_.now + S)
        journal_.c_read(target, matched="0.00", cancelled="2.00", price=None, at=journal_.now + S)
        journal_.send(partial, 100, at=journal_.now + S)
        journal_.c_read(partial, matched="1.00", cancelled="1.00", at=journal_.now + S)
        for p in others:
            journal_.send(p, at=journal_.now + S)
        self.assertEqual(journal_.fold().equity_p, -14900)
        read = journal_.c_read(target, matched="0.00", cancelled="1.99", price=None, at=journal_.now + S)
        state = journal_.fold()
        self.assertEqual((state.place_records[0].ledger["value_p"], state.equity_p), (-200, -15100))
        self.assertEqual({(s["stop_id"], s["scope"]) for s in state.due_stops},
                         {(f"capture-check-failed-{read}", "review"), ("pause-15000", "review"),
                          ("pause-15000-session", "session")})
        self.assertEqual(state.pause_fired, {-15000: read})

    def ack_state(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        journal_.stop("pause-15000-session", "session", "pause", level=-15000)
        journal_.stop("pause-15000", "review", "pause", level=-15000)
        journal_.stop("capture-check-failed-3", "review", "capture_check_failed", source=3)
        return journal_, session

    def test_ack_refusals(self):
        journal_, _ = self.ack_state()
        state = journal_.fold()
        ok = dict(journal_reconciled=True, unresolved_order=False, cause_written=True)
        cases = {"test_ack_unfired_level": ("pause-30000", ok, "ack_level_not_fired"),
                 "not a level": ("pause-15001", ok, "ack_unknown_stop"),
                 "unknown": ("whatever-1", ok, "ack_unknown_stop"),
                 "test_ack_unresolved_order": ("pause-15000", dict(ok, unresolved_order=True), "ack_unresolved_order"),
                 "test_ack_not_reconciled": ("pause-15000", dict(ok, journal_reconciled=False),
                                             "ack_journal_not_reconciled"),
                 "test_ack_nonpause_needs_cause": ("capture-check-failed-3", dict(ok, cause_written=False),
                                                   "ack_cause_required"),
                 "pause needs no cause": ("pause-15000", dict(ok, cause_written=False), None),
                 "session stop is not acked": ("pause-15000-session", ok, "ack_unknown_stop")}
        for label, (stop_id, flags, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(pilot.ack_check(state, stop_id, **flags), expected)
        journal_.ack("pause-15000")
        self.assertEqual(pilot.ack_check(journal_.fold(), "pause-15000", **ok), "ack_already_acked")
        journal_.stop("cleared-figure-gap-7", "review", "cleared_figure_gap", source=7)
        state = journal_.fold()
        self.assertEqual(pilot.ack_check(state, "capture-check-failed-3", **ok), "ack_ledger_gap_open")
        self.assertEqual(pilot.ack_check(state, "cleared-figure-gap-7", **ok), "ack_ledger_gap_open")
        journal_.note("gap_resolved", stop_id="cleared-figure-gap-7", decision_seq=7, cause_written=True)
        state = journal_.fold()
        self.assertIsNone(pilot.ack_check(state, "capture-check-failed-3", **ok))
        self.assertIsNone(pilot.ack_check(state, "cleared-figure-gap-7", **ok))

    def test_invalid_acks_are_not_folded(self):
        journal_, _ = self.ack_state()
        bad = journal_.ack("pause-30000")
        state = journal_.fold()
        self.assertEqual(state.pause_acked, {})
        self.assertEqual(state.invalid_records, (bad,))
        self.assertIn(f"pilot-record-invalid-{bad}", [s["stop_id"] for s in state.due_stops])


class FoldProtocol(unittest.TestCase):
    """Stops the fold implies, ACKs, lineage and receipts (note §6, §11; decisions Q13, Q24, Q26)."""

    def contradicted(self):
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p, 0)
        journal_.c_read(p, matched="0.00", cancelled="2.00", price=None)
        journal_.c_read(p, matched="1.00", cancelled="1.00")
        return journal_, p

    def test_due_stops_journal_then_ack(self):
        journal_, p = self.contradicted()
        state = journal_.fold()
        stop_id = occurrence(state.due_stops, "ledger_contradiction", p.seq)
        self.assertEqual([s["stop_id"] for s in state.due_stops], [stop_id])
        for stop in state.due_stops:
            journal.prepare_event("note", {"pilot": stop})
        journal_.journal_due(state)
        state = journal_.fold()
        self.assertEqual(state.due_stops, ())
        self.assertEqual([(s["stop_id"], s["acked_seq"]) for s in state.active_stops], [(stop_id, None)])
        flags = dict(journal_reconciled=True, unresolved_order=False)
        self.assertEqual(pilot.ack_check(state, stop_id, cause_written=True, **flags), "ack_ledger_gap_open")
        journal_.note("gap_resolved", stop_id=stop_id, decision_seq=p.seq, cause_written=True)
        state = journal_.fold()
        self.assertEqual(pilot.ack_check(state, stop_id, cause_written=False, **flags), "ack_cause_required")
        self.assertIsNone(pilot.ack_check(state, stop_id, cause_written=True, **flags))
        journal_.ack(stop_id, cause=True)
        state = journal_.fold()
        self.assertEqual((state.active_stops, state.due_stops), ((), ()))
        self.assertEqual(only(state).ledger["value_p"], -200)                 # still sticky after the ACK

    def test_acknowledged_level_never_fires_again(self):
        journal_ = Pilot(pt4=True)
        placed = bulk(journal_, 77)
        target, partial, spare, others = placed[0], placed[1], placed[2], placed[3:]
        journal_.send(target, 0, at=journal_.now + S)
        journal_.c_read(target, matched="0.00", cancelled="2.00", price=None, at=journal_.now + S)
        journal_.send(partial, 100, at=journal_.now + S)
        journal_.c_read(partial, matched="1.00", cancelled="1.00", at=journal_.now + S)
        journal_.send(spare, 0, at=journal_.now + S)
        journal_.c_read(spare, matched="0.00", cancelled="2.00", price=None, at=journal_.now + S)
        for p in others:
            journal_.send(p, at=journal_.now + S)
        self.assertEqual(journal_.fold().equity_p, -14900)
        journal_.c_read(target, matched="1.00", cancelled="1.00", at=journal_.now + S)
        state = journal_.fold()
        journal_.journal_due(state)
        journal_.ack("pause-15000")
        journal_.ack(occurrence(state.due_stops, "ledger_contradiction", target.seq), cause=True)
        journal_.c_read(spare, matched="1.00", cancelled="1.00", at=journal_.now + S)
        state = journal_.fold()
        self.assertEqual(state.equity_p, -15300)
        self.assertEqual([s["stop_id"] for s in state.due_stops],
                         [occurrence(state.due_stops, "ledger_contradiction", spare.seq)])
        self.assertEqual(state.pause_acked.keys(), {-15000})

    def test_replacement_contract_refused(self):
        journal_ = Pilot(pt4=True)
        journal_.add("note", {"pilot": dict(pilot.stop_payload(C, "x-1", "review", "x"), contract_sha256="e" * 64)})
        with self.assertRaises(PilotError) as caught:
            journal_.fold()
        self.assertEqual(caught.exception.code, "replacement_contract_not_supported_v1")
        successor = Pilot(pt4=True)
        successor.note("registration", registration_receipt_sha256=sha("r2"), predecessor_contract_sha256="d" * 64)
        self.assertRaises(PilotError, successor.fold)

    def test_sessions_and_receipts(self):
        journal_ = Pilot(pt4=True)
        bad = journal_.note("session_start", at=DAY0 + 8 * HOUR, session_id="s-bad",
                            registration_receipt_sha256=sha("unregistered"), utc_day="2027-01-04",
                            until_ns=DAY0 + 20 * HOUR)
        journal_.arm(0)
        twice = journal_.note("session_start", at=DAY0 + 9 * HOUR, session_id="s-again",
                              registration_receipt_sha256=journal_.registration, utc_day="2027-01-04",
                              until_ns=DAY0 + 20 * HOUR)
        journal_.receipt("PT1", verified=False, at=DAY0 + 21 * HOUR)
        after = journal_.note("session_start", at=DAY0 + DAY_NS + 8 * HOUR, session_id="s1",
                              registration_receipt_sha256=journal_.registration, utc_day="2027-01-05",
                              until_ns=DAY0 + DAY_NS + 20 * HOUR)
        state = journal_.fold()
        self.assertEqual(state.invalid_records, (bad, twice, after))
        self.assertEqual([s["session_id"] for s in state.sessions], ["s0"])
        self.assertTrue(state.l_fields_refuted)
        self.assertEqual(state.profit_basis, "gross")
        net = Pilot(pt4=True)
        net.receipt("PT4", matched=1, basis="net")
        self.assertIsNone(net.fold().profit_basis)                            # receipts disagree: unverified

    def test_rejected_order_read_literally(self):
        """Q26: a place report FAILURE is not definitely_unsent; with no C or L by the deadline it is
        measurement_unresolved, and W stays at -200."""
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.intent(p)
        journal_.submit(p)
        journal_.report(p, 0, status="FAILURE")
        journal_.closed(p)
        rec = only(journal_.fold(now=p.off + 7 * DAY_NS + 1))
        self.assertEqual((rec.unsent, rec.ledger["value_p"], rec.measurement["class"]),
                         (False, -200, "measurement_unresolved"))

    def test_invalid_notes(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        _, p, _ = journal_.classify(session)
        cases = [journal_.add("note", {"pilot": {"v": 1, "type": "stop"}}),
                 journal_.add("note", {"pilot": dict(pilot.stop_payload(C, "x-1", "review", "x"), level_p=1.5)}),
                 journal_.note("capture_c", ref_col="ref-unknown", bet_id="1", raw_sha256="0" * 64,
                               row=current_row()),
                 journal_.note("capture_c", ref_col=p.ref, bet_id="1", raw_sha256="0" * 64,
                               row=current_row(marketId="1.999")),
                 journal_.inner(dict(p.record, outcome="SKIP"), market_id=p.market),
                 journal_.stop("pause-15000", "review", "pause", level=-15001)]
        state = journal_.fold()
        self.assertEqual(state.invalid_records, tuple(cases))
        self.assertEqual({s["reason"] for s in state.due_stops}, {"pilot_record_invalid"})
        self.assertEqual(codes(state), [])                                    # no send relied on them
        self.assertEqual(set(pilot.R13_CODES), {
            "r13_not_place", "r13_other_open", "r13_other_session", "r13_other_contract", "r13_digest_mismatch",
            "r13_market_differs", "r13_body_differs", "r13_ref_differs", "r13_strategy_ref_differs"})


C_ONE = dataclasses.replace(C, n_eligible=1)          # one eligible bet completes N, as the reviewers' probes did


def manual_intent(journal_, *, at, ref="man-1", source="cli_manual"):
    payload = {"order": {"selection_id": 11, "handicap": 0, "side": "BACK", "price_h": 300, "size_p": 200,
                         "persistence_type": "LAPSE"},
               "customer_strategy_ref": "p1-fav-t5", "is_probe": False, "parent_ref": None,
               "provisional_child": False, "source": source}
    return journal_.add("intent", payload, at=at, intent_id=f"i-{ref}", customer_order_ref=ref,
                        customer_ref=f"c-{ref}", market_id="1.5", state_after="RECORDED")


class ReviewFindings(unittest.TestCase):
    """Regression tests for the 22 findings that survived the refute-first review of 459fcd1
    (code-review-wf_987e7fce.json), numbered as there. Each fails on 459fcd1 and passes now, except the
    coverage-only findings 11, 13 and 17, whose code was right: their tests fail on the reviewers' mutants."""

    def eligible_c_only(self, journal_, session):
        """A sent bet with C 2.00 at 3.44 and a CLOSED read (LOSER, BSP 3.10): eligible once its deadline passes."""
        _, p, _ = journal_.classify(session)
        journal_.send(p)
        journal_.c_read(p, price="3.44")
        journal_.closed(p)
        return p

    def test_finding_01_r13_checks_the_market(self):
        journal_ = Pilot(pt4=True)
        result, placed, _ = journal_.classify(journal_.arm(0))
        record = result["record"]
        good = dict(contract=C, market_id=record["market_id"], instruction=pilot.expected_instruction(record),
                    customer_order_ref=placed.ref, customer_strategy_ref="p1-fav-t5", open_id=placed.open_id,
                    session_id=record["session_id"], record_digest=pilot.record_sha256(record))
        self.assertIsNone(pilot.check_binding(record, **good))
        self.assertEqual(pilot.check_binding(record, **dict(good, market_id="1.999")), "r13_market_differs")
        self.assertIn("r13_market_differs", pilot.R13_CODES)

    def test_finding_02_place_too_few_active(self):
        for ids, flagged in (([11, 12, 13], True), ([11], True), ([11, 12, 13, 14, 15], False)):
            with self.subTest(ids=ids):
                journal_ = Pilot(pt4=True)
                journal_.place(journal_.arm(0), record_changes={"active_selection_ids": ids})
                self.assertEqual("place_too_few_active" in codes(journal_.fold()), flagged)

    def test_finding_03_deadline_trigger_between_reservation_and_decision(self):
        """A deadline at which equity rises falls between a reservation and its decision: classify (at the
        observation) and the fold (trigger first, then the decision) agree, so no false pause breach."""
        journal_ = Pilot(pt4=True)
        bulk(journal_, 73, lambda j, p: j.lose(p))
        _, r, _ = journal_.classify(journal_.arm(3))
        journal_.intent(r)
        journal_.submit(r)
        journal_.c_read(r, matched="0.00", cancelled="2.00", price=None)
        self.assertEqual(journal_.fold().equity_p, -14800)
        deadline = r.off + 7 * DAY_NS
        result, placed, _ = journal_.classify(journal_.arm(10), off=deadline + 297 * S)  # reserve -2 s, observe +2 s
        self.assertEqual((result["record"]["outcome"], result["stops"]), ("PLACE", []))
        state = journal_.fold(live_open_id=placed.open_id)          # its order may still be sent: -200
        self.assertEqual((codes(state), state.ending, state.equity_p), ([], None, -14800))

    def test_finding_04_negative_or_oversized_sizes_never_count(self):
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.intent(p)
        journal_.submit(p)
        journal_.c_read(p, matched="-1.00", cancelled="3.00")
        state = journal_.fold(now=p.off + 8 * DAY_NS)
        self.assertEqual((only(state).ledger["value_p"], state.equity_p, only(state).execution["size"]),
                         (-200, -200, None))
        for row in (dict(matched="-1.00", cancelled="3.00"), dict(matched="2.50", cancelled="-0.50"),
                    dict(matched="2.01", cancelled="0.00", lapsed="-0.01")):
            with self.subTest(row=row):
                self.assertFalse(pilot.capture_check(dec_row(current_row(**row)), place_report_matched_p=None))
                self.assertFalse(pilot.consistency_check(Decimal("1.00"), place_report_matched_p=None,
                                                         c=dec_row(current_row(**row))))
        for size in ("-1.00", "2.01"):
            self.assertFalse(pilot.consistency_check(Decimal(size), place_report_matched_p=None, c=None))
        l_only = Pilot(pt4=True)
        _, q, _ = l_only.classify(l_only.arm(0))
        l_only.intent(q)
        l_only.submit(q)
        l_only.l_row(q, size="-1.00", price="3.00")
        self.assertEqual(only(l_only.fold()).ledger["value_p"], -200)

    def test_finding_05_nonpause_ack_needs_its_cause(self):
        journal_ = Pilot(pt4=True)
        journal_.arm(0)
        journal_.stop("capture-check-failed-3", "review", "capture_check_failed", source=3)
        bad = journal_.ack("capture-check-failed-3", cause=False)
        state = journal_.fold()
        self.assertIn(("capture-check-failed-3", None), [(s["stop_id"], s["acked_seq"]) for s in state.active_stops])
        self.assertEqual(state.invalid_records, (bad,))
        self.assertIn(f"pilot-record-invalid-{bad}", [s["stop_id"] for s in state.due_stops])
        journal_.ack("capture-check-failed-3", cause=True)
        self.assertNotIn("capture-check-failed-3", [s["stop_id"] for s in journal_.fold().active_stops])

    def test_finding_06_evidence_before_the_basis_is_kept(self):
        def one():
            journal_ = Pilot(pt4=True)
            _, p, _ = journal_.classify(journal_.arm(0))
            journal_.intent(p)
            journal_.submit(p)
            return journal_, p

        # probe5: the place report is journaled after C1 (0 matched) and C2 (1.00 matched).
        journal_, p = one()
        journal_.c_read(p, matched="0.00", cancelled="2.00", price=None)
        journal_.c_read(p, matched="1.00", cancelled="1.00", at=p.off + 2 * MIN)
        journal_.report(p, 0, at=p.off + 3 * MIN)
        state = journal_.fold()
        self.assertEqual((only(state).ledger["value_p"], only(state).ledger["reason"]), (-200, "contradiction"))
        occurrence(state.due_stops, "ledger_contradiction", p.seq)
        # probe1 case B: no place report (UNKNOWN); C1 0 matched, then C2 1.00 matched.
        journal_, p = one()
        journal_.c_read(p, matched="0.00", cancelled="2.00", price=None)
        journal_.c_read(p, matched="1.00", cancelled="1.00", at=p.off + 2 * MIN)
        state = journal_.fold(now=p.off + 7 * DAY_NS + 1)
        self.assertEqual((only(state).ledger["value_p"], only(state).ledger["reason"]), (-200, "contradiction"))
        occurrence(state.due_stops, "ledger_contradiction", p.seq)
        # probe1 case A: a reconcile showed 200 matched, then C1 shows 0: C1 is never used.
        journal_, p = one()
        journal_.reconcile(p, 200, at=p.off + 30 * S)
        read = journal_.c_read(p, matched="0.00", cancelled="2.00", price=None)
        state = journal_.fold(now=p.off + 7 * DAY_NS + 1)
        self.assertEqual((only(state).ledger["value_p"], only(state).execution["size"]), (-200, None))
        self.assertIn(f"capture-check-failed-{read}", [s["stop_id"] for s in state.due_stops])

    def deadline_final(self):
        journal_ = Pilot(pt4=True)
        return journal_, self.eligible_c_only(journal_, journal_.arm(0))

    def test_finding_07_deadline_final_class_ignores_later_reads(self):
        late = {"reduced L": lambda j, p, at: j.l_row(p, price="3.44", reduced=True, at=at),
                "voided L": lambda j, p, at: j.l_row(p, price="3.44", status="VOIDED", at=at),
                "failing C": lambda j, p, at: j.c_read(p, matched="1.00", cancelled="1.00", at=at),
                "removal CLOSED": lambda j, p, at: j.closed(p, removed=(12,), at=at)}
        for label, step in late.items():
            with self.subTest(case=label):
                journal_, p = self.deadline_final()
                before = only(journal_.fold(now=p.off + 7 * DAY_NS + 1))
                self.assertEqual(before.measurement["class"], "eligible")
                step(journal_, p, p.off + 8 * DAY_NS)              # the first event after the deadline
                after = only(journal_.fold())
                self.assertEqual((after.measurement, after.execution), (before.measurement, before.execution))

    def test_finding_08_15_one_number_bound(self):
        def current(number):
            return dumps({"jsonrpc": "2.0", "id": 1, "result": {"currentOrders": [{
                "betId": "5001", "status": "EXECUTION_COMPLETE", "sizeMatched": Raw(number),
                "sizeCancelled": Raw("0"), "sizeLapsed": Raw("0"), "sizeVoided": Raw("0"),
                "averagePriceMatched": Raw("3.4")}]}}).encode()

        for number in ("2." + "0" * 39 + "1", "1" * 40, "9" * 39 + ".5", "1E+39", "-" + "1" * 40):
            with self.subTest(accepted=number[:12]):
                row = pilot.extract_current_row(current(number), bet_id="5001")
                pilot.pilot_note("capture_c", C.sha256, bet_id="5001", raw_sha256="0" * 64, row=row)
        for number in ("2." + "0" * 40 + "1", "1" * 41, "1.5E+40", "2e40", "1" * 300, "3." + "1" * 45):
            with self.subTest(refused=number[:12]), self.assertRaises(pilot.EvidenceError):
                pilot.extract_current_row(current(number), bet_id="5001")
        book = book_item(status="CLOSED", runners=[{"selectionId": 11, "status": "WINNER",
                                                    "sp": {"actualSP": Raw("3." + "1" * 45)}}])
        self.assertRaises(pilot.EvidenceError, pilot.extract_closed_book, rpc(book), market_id=MARKET)

    def test_finding_09_classify_judges_at_the_decision_instant(self):
        """N is reached by time (a C-only bet final at deadline + 1 ns) between the fold and the observation."""
        journal_ = Pilot(pt4=True)
        a = self.eligible_c_only(journal_, journal_.arm(0))
        deadline = a.off + 7 * DAY_NS
        session = journal_.arm(7)
        off = deadline + 297 * S
        res = journal_.reserve("1.777", off, session=session, open_id="open-b")
        observed = off - 295 * S
        row_body, book_body = bodies("1.777", off)
        args = dict(reservation_seq=res, open_id="open-b", session_id=session, customer_order_ref="ref-b",
                    registration_receipt_sha256=journal_.registration, row_body=row_body, book_body=book_body,
                    observed_wall_ns=observed, observed_monotonic_ns=1)
        early = journal_.fold(now=deadline - S, live_open_id="open-b", contract=C_ONE)
        self.assertIsNone(early.ending)
        refused = pilot.classify(C_ONE, early, **args)                # a head folded before the book was read
        self.assertEqual((refused["refusal"], refused["record"]), ("state_before_observation", None))
        self.assertEqual(refused["refusal_record"]["code"], "state_before_observation")
        at = journal_.fold(now=observed, live_open_id="open-b", contract=C_ONE)
        self.assertEqual(at.ending["reason"], "n_reached")
        self.assertEqual(pilot.classify(C_ONE, at, **args)["refusal"], "placing_ended")

    def test_finding_10_final_class_never_changes_rows_3_and_5(self):
        """Property: a record final through the deadline (C only, or neither) keeps its class and execution
        record whatever is captured after the deadline."""
        rng = random.Random(10)
        for seed in range(max(SEEDS // 4, 8)):
            neither = seed % 2 == 1
            journal_ = Pilot(pt4=True)
            _, p, _ = journal_.classify(journal_.arm(0))
            journal_.send(p)
            if not neither:
                journal_.c_read(p, price="3.44")
            journal_.closed(p)
            deadline = p.off + 7 * DAY_NS
            before = only(journal_.fold(now=deadline + 30 * MIN))
            self.assertEqual(before.measurement["class"], "measurement_unresolved" if neither else "eligible")
            at = deadline + HOUR
            for _ in range(rng.randrange(1, 6)):
                at += rng.randrange(1, 3600) * S
                choice = rng.randrange(6)
                if choice == 0:
                    journal_.c_read(p, matched=rng.choice(["1.00", "2.00"]), cancelled=rng.choice(["1.00", "0.00"]),
                                    price="3.5", at=at)
                elif choice == 1:
                    journal_.l_row(p, price="3.44", reduced=rng.choice([True, False]), at=at)
                elif choice == 2:
                    journal_.l_row(p, price="3.44", status="VOIDED", at=at)
                elif choice == 3:
                    journal_.closed(p, removed=(12,), sp="3.12", at=at)
                elif choice == 4:
                    journal_.closed(p, ours="REMOVED", at=at)
                else:
                    journal_.receipt("PT1", at=at)
            after = only(journal_.fold(now=at + HOUR))
            with self.subTest(seed=seed):
                self.assertEqual((after.measurement, after.execution), (before.measurement, before.execution))

    def test_finding_11_place_after_n_reached_in_the_fold(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        a = self.eligible_c_only(journal_, session)
        journal_.l_row(a, price="3.44")                    # A is final: one eligible bet, so N is reached here
        _, b, _ = journal_.classify(journal_.arm(1))
        state = journal_.fold(contract=C_ONE)
        self.assertEqual(codes(state), ["place_after_n_reached"])
        self.assertEqual((state.ending["reason"], state.place_records[1].measurement["class"]),
                         ("breach", "protocol_violation"))
        surplus = Pilot(pt4=True)
        session = surplus.arm(0)
        a = self.eligible_c_only(surplus, session)
        _, b, _ = surplus.classify(session)
        surplus.l_row(a, price="3.44", at=b.off)           # N is reached only after B's decision
        state = surplus.fold(contract=C_ONE)
        self.assertEqual((codes(state), state.ending["reason"]), ([], "n_reached"))
        split = pilot.analysis_population(state, now_ns=state.now_ns)
        self.assertEqual(([r.ref for r in split["population"]], [r.ref for r in split["surplus"]]), ([a.ref], [b.ref]))

    def test_finding_12_huge_exponents_are_refused_as_evidence(self):
        for text in ("[1e99999999999999999999]", "[1e-99999999999999999999]", "[1e1000000000000000000]",
                     "[1e9999999]"):
            with self.subTest(text=text):
                self.assertRaises(PilotError, pilot.decimal_loads, text)
                with localcontext() as context:
                    context.traps[InvalidOperation] = False
                    self.assertRaises(PilotError, pilot.decimal_loads, text)
        journal_ = Pilot(pt4=True)
        result, _, _ = journal_.classify(journal_.arm(0), market=MARKET,
                                         book=book_item(totalMatched=Raw("1e99999999999999999999")))
        self.assertEqual((result["record"]["outcome"], result["record"]["code"]), ("SKIP", "evidence_invalid"))
        body = dumps({"jsonrpc": "2.0", "id": 1, "result": {"currentOrders": [
            {"betId": "5001", "x": Raw("1e-99999999999999999999")}]}}).encode()
        self.assertRaises(pilot.EvidenceError, pilot.extract_current_row, body, bet_id="5001")

    def test_finding_13_best_lay_is_the_lowest_level(self):
        """Several lay levels, shuffled: the best lay is the lowest price, so a crossed level is never hidden."""
        cases = [((("3.10", "5"), ("2.98", "5")), "book_crossed"), ((("3.30", "5"), ("3.00", "5")), "book_crossed"),
                 ((("3.20", "5"), ("3.05", "5")), None), ((("3.15", "5"), ("3.05", "5"), ("3.10", "5")), None),
                 ((("3.20", "5"), ("3.15", "5")), "spread_too_wide")]
        rng = random.Random(13)
        for lays, code in cases:
            expected = {"outcome": "SKIP" if code else "PLACE", "code": code, "selection_id": None if code else FAV,
                        "limit_price_h": None if code else 300, "active_selection_ids": [11, 12, 13, 14, 15, 16],
                        "send_deadline_ns": OFF - 240 * S}
            for _ in range(6):
                order = list(lays)
                rng.shuffle(order)
                with self.subTest(lays=order):
                    book = book_item(runners=base_runners(r11=((("3.00", "15.00"),), tuple(order), "ACTIVE")))
                    self.assertEqual(decide(book=book)[2], expected)

    def row_for(self, off, **changes):
        row = catalogue_item(MARKET, pilot.utc_text_from_ns(off))
        row.update(changes)
        return row

    def test_finding_14_captured_text_is_printable(self):
        session_off = DAY0 + 10 * HOUR
        for label, (change, field, code) in {
                "lone surrogate country": (dict(event={"countryCode": "\ud800"}), "country", "country_not_allowed"),
                "lower-case country": (dict(event={"countryCode": "gb"}), "country", "country_not_allowed"),
                "lone surrogate off": (dict(marketStartTime="\udc00"), "off", "off_missing"),
                "unparseable off": (dict(marketStartTime="soon"), "off", "off_missing")}.items():
            with self.subTest(case=label):
                journal_ = Pilot(pt4=True)
                result, _, _ = journal_.classify(journal_.arm(0), market=MARKET, off=session_off,
                                                 row=self.row_for(session_off, **change))
                record = result["record"]
                self.assertEqual((record["outcome"], record["code"], record[field]), ("SKIP", code, None))
                pilot.record_sha256(record)
                journal.prepare_event("note", {"pilot": record}, market_id=record["market_id"])
        body = dumps({"jsonrpc": "2.0", "id": 1, "result": {"currentOrders": [
            {"betId": "5001", "status": "\ud800", "customerOrderRef": "a\nb", "marketId": "1.234"}]}}).encode()
        row = pilot.extract_current_row(body, bet_id="5001")
        self.assertEqual((row["status"], row["customerOrderRef"], row["marketId"]), (None, None, "1.234"))

    def test_finding_16_bad_ns_hour_24_and_deep_nesting(self):
        journal_ = Pilot(pt4=True)
        bad = journal_.place(journal_.arm(0), record_changes={"decision_book_wall_ns": 10 ** 30})
        state = journal_.fold()
        self.assertEqual((state.invalid_records, state.place_records), ((bad.seq,), ()))
        for text in ("2026-10-21T24:00:00Z", "2026-10-21T24:00:00.000+00:00"):
            self.assertRaises(PilotError, pilot.parse_utc_ns, text)
        self.assertEqual(pilot.parse_utc_ns("2026-10-21T23:59:59Z") + S, pilot.parse_utc_ns("2026-10-22T00:00:00Z"))
        self.assertEqual(pilot.decimal_loads("[" * 64 + "]" * 64), pilot.decimal_loads("[" * 64 + "]" * 64))
        for text in ("[" * 65 + "]" * 65, "[" * 100000 + "]" * 100000, '{"a":' * 65 + "1" + "}" * 65):
            self.assertRaises(PilotError, pilot.decimal_loads, text)
        self.assertEqual(pilot.decimal_loads('["' + "[" * 100 + '\\"[[{"]'), ["[" * 100 + '"[[{'])

    def test_finding_17_failed_codes_bools_and_error_envelopes(self):
        for code in ("INSUFFICIENT_FUNDS", "rejected_before_processing"):
            with self.subTest(code=code):
                journal_ = Pilot(pt4=True)
                _, p, _ = journal_.classify(journal_.arm(0))
                journal_.intent(p)
                journal_.submit(p)
                journal_.add("report", {"action": "place", "report": None, "outcome": "FAILED",
                                        "transitions": [{"customer_order_ref": p.ref, "state": "FAILED",
                                                         "reason": "place_exception",
                                                         "fields": {"error_code": code}}]},
                             at=p.off - 291 * S, market_id=p.market)
                rec = only(journal_.fold())
                self.assertEqual((rec.unsent, rec.failed_not_sent, rec.ledger["value_p"]), (False, False, -200))
                late = only(journal_.fold(now=p.off + 7 * DAY_NS + 1))
                self.assertEqual(late.measurement["class"], "measurement_unresolved")
        cases = {"price true": base_runners(r12=(((Raw("true"), "1"),), (), "ACTIVE")),
                 "size false": base_runners(r12=((("4.5", Raw("false")),), (), "ACTIVE")),
                 "selectionId true": base_runners() + [runner(True, (("5.0", "1"),), ())]}
        for label, runners in cases.items():
            with self.subTest(case=label):
                self.assertEqual(decide(book=book_item(runners=runners))[:2], ("SKIP", "evidence_invalid"))
        body = dumps({"jsonrpc": "2.0", "result": [book_item()], "error": {"code": -32099}, "id": 1}).encode()
        self.assertRaises(pilot.EvidenceError, pilot.parse_rpc_single, body, market_id=MARKET)

    def test_finding_18_submit_after_a_stop_halt_or_ending(self):
        steps = {"submit_while_stopped review": lambda j, p: j.stop("capture-check-failed-9", "review",
                                                                    "capture_check_failed", source=9),
                 "submit_while_stopped session": lambda j, p: j.stop("day-limit-9-session", "session", "day_limit",
                                                                     source=9),
                 "submit_while_halted halt": lambda j, p: j.add("halt", {"reason": "operator", "scope": "global"}),
                 "submit_while_halted market": lambda j, p: j.add("halt", {"reason": "x", "scope": "market"},
                                                                  market_id=p.market),
                 "submit_while_halted kill": lambda j, p: j.add("kill", {"reason": "kill", "scope": "own"}),
                 "submit_after_ending": lambda j, p: j.note("ending", reason="yu_written", code="yu_in_writing"),
                 # A RESUME clears the HALT but not the session's end (Astra #1): still a violation.
                 "submit_after_session_end: halt then resume": lambda j, p: (
                     j.add("halt", {"reason": "x", "scope": "global"}), j.add("resume", {"scope": "global"}))}
        for label, step in steps.items():
            with self.subTest(case=label):
                journal_ = Pilot(pt4=True)
                _, p, _ = journal_.classify(journal_.arm(0))
                step(journal_, p)
                journal_.send(p)
                found = codes(journal_.fold(live_open_id=p.open_id))
                expected = {label.split(" ")[0].rstrip(":")}
                if label.startswith(("submit_while_stopped", "submit_while_halted")):
                    expected.add("submit_after_session_end")              # the stop or HALT also ended it
                self.assertEqual(found, sorted(expected))
        for failed, expected in ((False, ["sent_after_session_end"]), (True, [])):
            with self.subTest(case="session end", failed=failed):
                journal_ = Pilot(pt4=True)
                session = journal_.arm(0, until=DAY0 + 10 * HOUR - 290 * S)
                _, p, _ = journal_.classify(session)
                journal_.intent(p)
                journal_.submit(p, at=p.off - 285 * S)
                if failed:
                    journal_.failed(p, at=p.off - 284 * S)
                else:
                    journal_.report(p, at=p.off - 284 * S)
                self.assertEqual(codes(journal_.fold(live_open_id=p.open_id)), expected)

    def test_finding_19_manual_orders_are_not_pilot_sends(self):
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.lose(p)
        manual_intent(journal_, at=DAY0 + 2 * DAY_NS + 12 * HOUR)       # outside any session: ignored
        state = journal_.fold()
        self.assertEqual((codes(state), state.ending, state.due_stops), ([], None, ()))
        journal_.arm(3)
        seq = manual_intent(journal_, at=DAY0 + 3 * DAY_NS + 9 * HOUR, ref="man-2")
        state = journal_.fold()
        self.assertEqual((codes(state), state.ending), ([], None))
        self.assertEqual([(s["stop_id"], s["scope"]) for s in state.due_stops],
                         [(f"manual-order-during-session-{seq}", "review")])

    def test_finding_20_evidence_of_a_send_that_was_not_journaled(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        _, p, _ = journal_.classify(session)
        journal_.classify(session)                          # p's open ends with no submit
        journal_.reconcile(p, 200)
        journal_.c_read(p, at=journal_.now + S)
        journal_.closed(p, at=journal_.now + S)
        journal_.l_row(p, at=journal_.now + S)
        state = journal_.fold()
        rec = state.place_records[0]
        self.assertEqual((codes(state), rec.unsent, rec.measurement["class"]),
                         (["sent_without_submit"], False, "protocol_violation"))
        failed = Pilot(pt4=True)
        _, q, _ = failed.classify(failed.arm(0))
        failed.intent(q)
        failed.submit(q)
        failed.failed(q)
        failed.report(q, at=q.off - 280 * S)
        self.assertEqual(codes(failed.fold()), ["sent_after_not_sent"])

    def test_finding_21_late_submit_shown_sent_by_any_evidence(self):
        for failed, expected in ((False, ["sent_after_deadline"]), (True, [])):
            with self.subTest(failed=failed):
                journal_ = Pilot(pt4=True)
                _, p, _ = journal_.classify(journal_.arm(0))
                journal_.intent(p, at=p.off - 250 * S)
                journal_.submit(p, at=p.off - 230 * S)
                if failed:
                    journal_.failed(p, at=p.off - 229 * S)
                else:
                    journal_.reconcile(p, 200, at=p.off + MIN)
                    journal_.c_read(p, at=p.off + 3 * MIN)
                self.assertEqual(codes(journal_.fold()), expected)

    def test_finding_22_n_by_journal_order_not_capture_time(self):
        """A's L row is journaled after B's PLACE but stamped before it (a clock step back): N is reached after B
        in journal order, so B is surplus, not a breach."""
        journal_ = Pilot(pt4=True)
        a = self.eligible_c_only(journal_, journal_.arm(0))
        journal_.settle(a)
        _, b, _ = journal_.classify(journal_.arm(1))
        journal_.send(b)
        journal_.l_row(a, price="3.44", at=b.off - 400 * S)
        state = journal_.fold(contract=C_ONE)
        self.assertEqual((codes(state), state.ending["reason"]), ([], "n_reached"))


def stop_ids(stops):
    return sorted((s["stop_id"], s["scope"]) for s in stops)


def replayed(journal_, **kw):
    return pilot.fold(json.loads(json.dumps(journal_.events)), contract=kw.pop("contract", C),
                      now_ns=kw.pop("now", journal_.now), **kw)


class AstraReview(unittest.TestCase):
    """Regression tests for Astra's review of 93d5d83 (astra-review-p1-05.md, findings 1-7), built on the inputs
    of her synthetic probes (p1-05/astra-r1-*). Each fails on 93d5d83."""

    def test_astra_1_a_terminated_session_stays_ended(self):
        """ARM, then a HALT, a KILL or a review stop: a RESUME or an ACK clears it, but the session stays ended; a
        reservation in it is a violation, classify refuses, a submit is a violation; a new ARM the next day may
        place again (astra-r1-ledger-probes: session_termination)."""
        def review_stop_then_ack(j, session):
            _, first, _ = j.classify(session)
            j.send(first)
            j.c_read(first, matched="1.00", cancelled="1.00")          # below the place report: capture check fails
            j.journal_due(j.fold())
            (stop,) = [s for s in j.fold().active_stops if s["scope"] == "review"]
            self.assertEqual(stop["reason"], "capture_check_failed")
            j.ack(stop["stop_id"], cause=True)

        cases = {"halt_then_resume": lambda j, s: (j.add("halt", {"scope": "global", "reason": "operator"}),
                                                  j.add("resume", {"scope": "global"})),
                 "kill_then_resume": lambda j, s: (j.add("kill", {"scope": "own", "reason": "operator"}),
                                                  j.add("resume", {"scope": "global"})),
                 "review_stop_then_ack": review_stop_then_ack}
        for label, end in cases.items():
            with self.subTest(case=label):
                journal_ = Pilot(pt4=True)
                session = journal_.arm(0)
                end(journal_, session)
                state = journal_.fold()
                self.assertEqual((state.active_stops, codes(state)), ((), []))
                self.assertIsNotNone(state.sessions[-1]["ended_seq"])
                result, placed, _ = journal_.classify(session)
                self.assertIsNone(placed)
                self.assertEqual(result["refusal"], "placing_ended")
                self.assertEqual(codes(journal_.fold()), ["session_stopped"])     # the reservation itself
                self.assertEqual(journal_.fold(), replayed(journal_))
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = journal_.next_off(session)
        res = journal_.reserve("1.400", off, session=session, open_id="open-1")   # before the HALT: valid
        journal_.add("halt", {"scope": "global", "reason": "operator"})
        journal_.add("resume", {"scope": "global"})
        row_body, book_body = bodies("1.400", off)
        refused = pilot.classify(C, journal_.fold(now=off - 290 * S, live_open_id="open-1"), reservation_seq=res,
                                 open_id="open-1", session_id=session, registration_receipt_sha256=journal_.registration,
                                 customer_order_ref="ref-1", row_body=row_body, book_body=book_body,
                                 observed_wall_ns=off - 295 * S, observed_monotonic_ns=1)
        self.assertEqual(refused["refusal"], "session_ended")
        result, placed, _ = journal_.classify(journal_.arm(1))                  # a new ARM, the next day
        self.assertEqual((result["record"]["outcome"], codes(journal_.fold())), ("PLACE", []))

    def test_astra_2_a_skip_decision_implies_its_stops(self):
        """A SKIP(pause) persisted without its two stop notes still fires the level and both stops on replay, and
        keeps them through equity recovery (astra-r1-ledger-probes: interrupted_pause_notes)."""
        journal_ = Pilot(pt4=True)
        placed = bulk(journal_, 74, lambda j, p: j.send(p) if p.ref == "ref-1" else j.lose(p), per_day=24)
        session = placed[-1].record["session_id"]
        result, _, _ = journal_.classify(session, journal_it=False)
        record = result["record"]
        self.assertEqual((record["outcome"], record["code"]), ("SKIP", "pause"))
        seq = journal_.inner(record, at=record["decision_book_wall_ns"] + S, market_id=record["market_id"])
        restarted = journal_.fold()
        self.assertEqual((restarted.equity_p, restarted.pause_fired), (-14800, {-15000: seq}))
        self.assertEqual(stop_ids(restarted.due_stops), stop_ids(result["stops"]))
        with_stops = Pilot(pt4=True)
        with_stops.events = [dict(e) for e in journal_.events]
        with_stops.now = journal_.now
        for stop in result["stops"]:
            with_stops.inner(stop)
        full = with_stops.fold()
        self.assertEqual(full.pause_fired, restarted.pause_fired)
        self.assertEqual(stop_ids(full.active_stops), stop_ids(restarted.active_stops))
        pending = placed[0]                                              # the pending winner settles: equity recovers
        journal_.c_read(pending, at=journal_.now + S)
        journal_.closed(pending, ours="WINNER", at=journal_.now + S)
        journal_.settle(pending, at=journal_.now + S)
        journal_.l_row(pending, profit="4.00", outcome="WIN", at=journal_.now + S)
        journal_.commission(pending, "0.08", at=journal_.now + S)
        recovered = journal_.fold()
        self.assertGreater(recovered.equity_p, -14800)
        self.assertIn(("pause-15000", "review"), stop_ids(recovered.active_stops))
        self.assertIsNone(journal_.classify(session)[1])                 # no PLACE without an ACK and a new ARM

    def test_astra_2_cap_and_refusal_records_imply_their_stops(self):
        journal_ = Pilot(pt4=False)                                      # a PT4 day: 5 PLACE records
        session = journal_.arm(0)
        for _ in range(5):
            journal_.classify(session)
        result, _, res = journal_.classify(session, journal_it=False)
        self.assertEqual(result["record"]["code"], "cap")
        journal_.inner(result["record"], market_id=result["record"]["market_id"])
        self.assertEqual(stop_ids(journal_.fold().due_stops), stop_ids(result["stops"]))
        refusing = Pilot(pt4=True)
        session = refusing.arm(0)
        refused = pilot.classify(C, refusing.fold(now=DAY0 + 9 * HOUR), reservation_seq=999, open_id="open-x",
                                 session_id=session, registration_receipt_sha256=refusing.registration,
                                 customer_order_ref="ref-x", row_body=b"", book_body=b"",
                                 observed_wall_ns=DAY0 + 9 * HOUR, observed_monotonic_ns=1)
        self.assertEqual(refused["refusal"], "reservation_unknown")
        seq = refusing.inner(refused["refusal_record"])
        self.assertEqual(stop_ids(refusing.fold().due_stops), [(f"reservation-unknown-999-{seq}", "review")])

    def test_astra_3_s_basis_price_and_commission_contradictions(self):
        """A settled winner (S +392) and 74 losers: equity -14408. A later EXECUTION_COMPLETE C at 3.50 restores W at
        -200: equity -15000, the contradiction stop and the pause, also on replay; the analytical record is kept
        (astra-r1-ledger-probes: corrected_c_price_misses_pause, later_commission_correction)."""
        journal_ = Pilot(pt4=True)
        placed = bulk(journal_, 75, lambda j, p: j.win(p) if p.ref == "ref-1" else j.lose(p))
        before = journal_.fold()
        self.assertEqual((before.equity_p, before.place_records[0].ledger["value_p"]), (-14408, 392))
        read = journal_.c_read(placed[0], at=journal_.now + S, price="3.50")
        for after in (journal_.fold(), replayed(journal_)):
            target = after.place_records[0]
            self.assertEqual((target.ledger["part"], target.ledger["value_p"], after.equity_p), ("W", -200, -15000))
            self.assertEqual(target.execution, before.place_records[0].execution)
            self.assertEqual(stop_ids(after.due_stops), [(f"ledger-contradiction-{placed[0].seq}-{read}", "review"),
                                                         ("pause-15000", "review"),
                                                         ("pause-15000-session", "session")])
            self.assertIn(-15000, after.pause_fired)
        same_price = Pilot(pt4=True)
        p = same_price.place(same_price.arm(0))
        same_price.win(p)
        same_price.c_read(p, at=same_price.now + S, price="3.00")
        self.assertEqual(only(same_price.fold()).ledger["value_p"], 392)
        commission = Pilot(pt4=True)
        p = commission.place(commission.arm(0))
        commission.win(p)
        self.assertEqual(commission.fold().equity_p, 392)
        commission.commission(p, "0.20", at=commission.now + S)
        for after in (commission.fold(), replayed(commission)):
            self.assertEqual((only(after).ledger["part"], after.equity_p), ("W", -200))
            self.assertEqual({s["reason"] for s in after.due_stops}, {"ledger_contradiction", "commission_gap"})
        later = Pilot(pt4=True)                             # every capture is checked, not only captured[0]
        q = later.place(later.arm(0))
        later.win(q, commission=None)
        bad = later.commission(q, "0.20")
        later.commission(q, "0.08")
        state = later.fold()
        self.assertEqual((only(state).ledger["part"], only(state).ledger["reason"]), ("W", "commission_gap"))
        self.assertEqual(sorted(state.resolved_gaps), [f"commission-gap-{q.seq}-{bad}"])  # the later figure agrees

    def test_astra_4_benign_head_change_is_rechecked_cleanly(self):
        """An unrelated note one second after the observation: classify on the latest head, same bytes, PLACE
        (astra-r1-root-probes: latest_head)."""
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = journal_.next_off(session)
        observed = off - 295 * S
        reservation = journal_.reserve("1.900001", off, session=session, open_id="head-open")
        row, book = bodies("1.900001", off)
        kwargs = dict(reservation_seq=reservation, open_id="head-open", session_id=session,
                      registration_receipt_sha256=journal_.registration, customer_order_ref="head-ref",
                      row_body=row, book_body=book, observed_wall_ns=observed, observed_monotonic_ns=1)
        before = pilot.classify(C, journal_.fold(now=observed, live_open_id="head-open"), **kwargs)
        journal_.add("note", {"operator": "read-only capture completed"}, at=observed + S)
        after = pilot.classify(C, journal_.fold(now=observed, live_open_id="head-open"), **kwargs)
        self.assertEqual((before["record"]["outcome"], after["refusal"], after["record"]), ("PLACE", None,
                                                                                           before["record"]))

    def test_astra_4_a_stop_between_observation_and_classify(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        off = journal_.next_off(session)
        observed = off - 295 * S
        reservation = journal_.reserve("1.900002", off, session=session, open_id="open-s")
        journal_.stop("capture-check-failed-2", "review", "capture_check_failed", source=2, at=observed + S)
        row, book = bodies("1.900002", off)
        result = pilot.classify(C, journal_.fold(now=observed + 2 * S, live_open_id="open-s"),
                                reservation_seq=reservation, open_id="open-s", session_id=session,
                                registration_receipt_sha256=journal_.registration, customer_order_ref="ref-s",
                                row_body=row, book_body=book, observed_wall_ns=observed, observed_monotonic_ns=1)
        # Judged at the latest head: the stop is active and has ended the session, whatever the book showed.
        self.assertEqual((result["refusal"], result["record"]), ("session_ended", None))

    def test_astra_4_time_driven_n_between_observation_and_append(self):
        """N = 400 completes by time (the 400th eligible record, C only, is final at deadline + 1 ns). A record
        observed at deadline - 2 s and appended at deadline + 5 s is a violation, and classify on a head at or after
        the deadline refuses (astra-r1-root-probes: n_at_append)."""
        journal_ = Pilot(pt4=True)
        last = None
        for i in range(400):
            if i % 25 == 0:
                session = journal_.arm(i // 25)
            p = journal_.place(session)
            if i != 399:
                journal_.win(p)
            else:
                journal_.send(p)
                journal_.c_read(p)
                journal_.closed(p)
                last = p
        deadline = last.off + 7 * DAY_NS
        self.assertIsNone(journal_.fold(now=deadline).ending)
        session = journal_.arm(22)
        off = deadline + 293 * S
        res = journal_.reserve("1.950001", off, session=session, open_id="open-late")
        row, book = bodies("1.950001", off)
        late_head = journal_.fold(now=deadline + 3 * S, live_open_id="open-late")
        self.assertEqual(late_head.ending["reason"], "n_reached")
        refused = pilot.classify(C, late_head, reservation_seq=res, open_id="open-late", session_id=session,
                                 registration_receipt_sha256=journal_.registration, customer_order_ref="ref-late",
                                 row_body=row, book_body=book, observed_wall_ns=off - 295 * S, observed_monotonic_ns=1)
        self.assertEqual(refused["refusal"], "placing_ended")
        p = journal_.place(session, off=off + 30 * MIN, decided_at=deadline + 5 * S)
        journal_.intent(p, at=deadline + 6 * S)
        journal_.submit(p, at=deadline + 7 * S)
        journal_.report(p, at=deadline + 8 * S)
        after = journal_.fold()
        self.assertIn("place_after_n_reached", codes(after))
        self.assertEqual(after.ending["reason"], "breach")

    def test_astra_5_matched_prices_outside_classic_are_unresolved(self):
        def raw_l(price):
            body = dumps({"jsonrpc": "2.0", "id": 1, "result": {"clearedOrders": [{
                "betId": "5001", "sizeSettled": Raw("2.00"), "priceMatched": Raw(price), "priceReduced": False,
                "profit": Raw("2000.00"), "betOutcome": "WIN"}]}}).encode()
            return pilot.extract_cleared_row(body, bet_id="5001")["priceMatched"]

        def raw_c(price):
            body = dumps({"jsonrpc": "2.0", "id": 1, "result": {"currentOrders": [{
                "betId": "5001", "status": "EXECUTION_COMPLETE", "sizeMatched": Raw("2.00"),
                "averagePriceMatched": Raw(price)}]}}).encode()
            return pilot.extract_current_row(body, bet_id="5001")["averagePriceMatched"]

        for price, kept in (("0", None), ("-1", None), ("1", None), ("1.00999", None), ("1001", None),
                            ("1000.01", None), ("1.01", "1.01"), ("1000", "1000"), ("3.444", "3.444")):
            with self.subTest(price=price):
                self.assertEqual((raw_l(price), raw_c(price)), (kept, kept))
        for price in ("NaN", "Infinity"):
            self.assertRaises(pilot.EvidenceError, raw_l, price)
        for price, cls in (("0", "measurement_unresolved"), ("1001", "measurement_unresolved"),
                           ("3.444", "eligible")):
            with self.subTest(l_only=price):
                journal_ = Pilot(pt4=True)
                _, p, _ = journal_.classify(journal_.arm(0))
                journal_.send(p)
                journal_.closed(p, sp="3.10")
                journal_.inner({"v": 1, "type": "capture_l", "contract_sha256": C.sha256, "bet_id": p.bet_id,
                                "raw_sha256": "0" * 64, "row": cleared_row(price=price)},
                               customer_order_ref=p.ref)
                self.assertEqual(only(journal_.fold()).measurement["class"], cls)

    def test_astra_5_full_sample_at_an_impossible_price_does_not_pass(self):
        """400 bets over 16 dates, C = L = 2.00 at 1001 with matching profit and commission and valid BSPs
        (astra-r1-analysis-probes: final_price_probe) gave PASS; now every record is unresolved."""
        journal_ = Pilot(pt4=True)
        for day in range(16):
            session = journal_.arm(day)
            for _ in range(25):
                p = journal_.place(session)
                journal_.send(p)
                journal_.c_read(p, price="1001")
                journal_.closed(p, ours="WINNER", sp="3.10" if day < 8 else "3.20")
                journal_.settle(p)
                journal_.l_row(p, price="1001", profit="2000.00", outcome="WIN")
                journal_.commission(p, "40.00")
        state = journal_.fold()
        classes = {r.measurement["class"] for r in state.place_records}
        # Each bet is now unresolved at -200, so the pause fires and later PLACEs are protocol violations.
        self.assertEqual(classes, {"measurement_unresolved", "protocol_violation"})
        self.assertEqual(pilot.verdict(state, contract=C, now_ns=state.now_ns)["result"], "INVALID")

    def test_astra_6_two_gaps_resolve_then_ack(self):
        """Two sent orders with 3p cleared-figure gaps: both genuinely open, so neither ACK is accepted; one is
        resolved by a later L row that agrees, the other by a gap_resolved record with its cause; then both ACKs
        are accepted in either order, and a replay agrees (astra-r1-ledger-probes: two_gap_ack_deadlock)."""
        def two_gaps():
            journal_ = Pilot(pt4=True)
            session = journal_.arm(0)
            placed = []
            for _ in range(2):
                p = journal_.place(session)
                journal_.send(p)
                journal_.c_read(p, at=p.off - 290 * S)
                placed.append(p)
            gaps = []
            for p in placed:
                journal_.closed(p, ours="WINNER", at=max(journal_.now + S, p.off + 20 * MIN))
                journal_.settle(p, at=journal_.now + S)
                gaps.append(f"cleared-figure-gap-{p.seq}-"
                            f"{journal_.l_row(p, profit='4.03', outcome='WIN', at=journal_.now + S)}")
                journal_.commission(p, "0.08", at=journal_.now + S)
            journal_.journal_due(journal_.fold())
            return journal_, placed, gaps

        flags = dict(journal_reconciled=True, unresolved_order=False, cause_written=True)
        for order in ((0, 1), (1, 0)):
            with self.subTest(ack_order=order):
                journal_, placed, gaps = two_gaps()
                state = journal_.fold()
                self.assertEqual([pilot.ack_check(state, g, **flags) for g in gaps], ["ack_ledger_gap_open"] * 2)
                journal_.l_row(placed[0], profit="4.00", outcome="WIN", at=journal_.now + S)    # agrees: resolved
                state = journal_.fold()
                self.assertEqual(sorted(state.resolved_gaps), [gaps[0]])
                self.assertEqual([pilot.ack_check(state, g, **flags) for g in gaps], ["ack_ledger_gap_open"] * 2)
                bad = journal_.note("gap_resolved", stop_id=gaps[1], decision_seq=placed[1].seq, cause_written=False)
                wrong = journal_.note("gap_resolved", stop_id=gaps[1], decision_seq=placed[0].seq, cause_written=True)
                journal_.note("gap_resolved", stop_id=gaps[1], decision_seq=placed[1].seq, cause_written=True)
                state = journal_.fold()
                self.assertEqual(state.invalid_records, (bad, wrong))
                self.assertEqual(sorted(state.resolved_gaps), sorted(gaps))
                not_a_gap = journal_.note("gap_resolved", stop_id=f"pilot-record-invalid-{bad}", decision_seq=None,
                                          cause_written=True)
                self.assertIn(not_a_gap, journal_.fold().invalid_records)
                journal_.journal_due(journal_.fold())                # the invalid notes' own review stops
                for stop in [s for s in journal_.fold().active_stops if s["reason"] == "pilot_record_invalid"]:
                    journal_.ack(stop["stop_id"], cause=True)
                for index in order:
                    self.assertIsNone(pilot.ack_check(journal_.fold(), gaps[index], **flags))
                    journal_.ack(gaps[index], cause=True)
                final = journal_.fold()
                self.assertEqual([s for s in final.active_stops if s["reason"] == "cleared_figure_gap"], [])
                self.assertEqual(final, replayed(journal_))

    def test_astra_7_a_finalized_note_is_the_durable_frontier(self):
        """C 3.44 and a CLOSED book; a fold at deadline + 2 s gives row 3 / eligible. An L at 3.06 stamped exactly at
        the deadline and journaled afterwards would make a fresh fold row 2 / unresolved; with the finalized note
        that P1-10 writes after that first fold, the record stays eligible (astra-r1-analysis-probes)."""
        for with_note in (False, True):
            with self.subTest(finalized_note=with_note):
                journal_ = Pilot(pt4=True)
                _, p, _ = journal_.classify(journal_.arm(0))
                journal_.send(p)
                deadline = p.off + 7 * DAY_NS
                journal_.c_read(p, price="3.44")
                journal_.closed(p, sp="3.10")
                first = only(journal_.fold(now=deadline + 2 * S))
                self.assertEqual((first.execution["row"], first.measurement["class"]), (3, "eligible"))
                if with_note:
                    journal_.note("finalized", at=deadline + 2 * S, ref_col=p.ref,
                                  final_at_ns=first.measurement["final_at_ns"], row=3, **{"class": "eligible"})
                journal_.l_row(p, price="3.06", at=deadline)
                rec = only(journal_.fold(now=deadline + 2 * S))
                expected = (3, "eligible") if with_note else (2, "measurement_unresolved")
                self.assertEqual((rec.execution["row"], rec.measurement["class"]), expected)
                if with_note:
                    state = journal_.fold()
                    self.assertEqual((state.invalid_records, state.due_stops), ((), ()))
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p)
        journal_.c_read(p, price="3.44")
        journal_.closed(p, sp="3.10")
        deadline = p.off + 7 * DAY_NS
        seq = journal_.note("finalized", at=deadline + 2 * S, ref_col=p.ref, final_at_ns=deadline + 1, row=1,
                            **{"class": "missing_bsp"})
        self.assertEqual(stop_ids(journal_.fold().due_stops), [(f"finalized-mismatch-{seq}", "review")])
        unknown = journal_.note("finalized", at=deadline + 3 * S, ref_col="ref-none", final_at_ns=deadline + 1, row=3,
                                **{"class": "eligible"})
        self.assertIn(unknown, journal_.fold().invalid_records)


class BspFrontier(unittest.TestCase):
    """The BSP decision's durable frontier, a bsp_decided note written by P1-10 (Nuance, after Astra's analysis
    probe: a CLOSED read stamped exactly at the 24 h cutoff but journaled after a later event)."""

    def lifecycle(self):
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p)
        journal_.c_read(p, price="3.44")
        return journal_, p

    def test_bsp_decided_note_is_the_durable_frontier(self):
        """A fold at cutoff + 2 s decides no BSP (no CLOSED read by the cutoff). A CLOSED read stamped exactly at the
        cutoff and journaled afterwards changes a fresh fold without the note; with the note it is only logged."""
        for with_note in (False, True):
            with self.subTest(bsp_decided_note=with_note):
                journal_, p = self.lifecycle()
                cutoff = p.off + DAY_NS
                first = only(journal_.fold(now=cutoff + 2 * S))
                self.assertEqual((first.bsp["decided"], first.bsp["bsp"]), (True, None))
                if with_note:
                    journal_.note("bsp_decided", at=cutoff + 2 * S, ref_col=p.ref,
                                  decided_at_ns=first.bsp["decided_at_ns"], outcome="none", actual_sp=None,
                                  read_seq=None)
                journal_.closed(p, sp="3.10", at=cutoff)
                journal_.l_row(p, price="3.44", at=cutoff + 3 * S)
                state = journal_.fold()
                rec = only(state)
                expected = (None, "missing_bsp") if with_note else (Decimal("3.10"), "eligible")
                self.assertEqual((rec.bsp["bsp"], rec.measurement["class"]), expected)
                if with_note:
                    self.assertEqual((state.invalid_records, state.due_stops), ((), ()))
                    self.assertEqual(state, replayed(journal_))

    def test_matching_note_then_a_correction(self):
        journal_, p = self.lifecycle()
        read = journal_.closed(p, sp="3.10")
        journal_.note("bsp_decided", ref_col=p.ref, decided_at_ns=p.off + 5 * MIN, outcome="bsp", actual_sp="3.1",
                      read_seq=read)
        journal_.closed(p, sp="3.12", at=journal_.now + S)
        state = journal_.fold()
        self.assertEqual((only(state).bsp["bsp"], only(state).bsp["read_seq"], state.due_stops),
                         (Decimal("3.10"), read, ()))

    def test_a_note_the_evidence_does_not_support(self):
        """The note's value is never adopted: the fold keeps what the reads before the note give."""
        journal_, p = self.lifecycle()
        cutoff = p.off + DAY_NS
        seq = journal_.note("bsp_decided", at=cutoff + 2 * S, ref_col=p.ref, decided_at_ns=cutoff, outcome="bsp",
                            actual_sp="3.10", read_seq=7)
        state = journal_.fold()
        self.assertEqual((only(state).bsp["bsp"], stop_ids(state.due_stops)),
                         (None, [(f"bsp-decided-mismatch-{seq}", "review")]))
        other, q = self.lifecycle()
        other.closed(q, sp="3.10")
        seq = other.note("bsp_decided", ref_col=q.ref, decided_at_ns=q.off + 5 * MIN, outcome="none", actual_sp=None,
                         read_seq=None)
        state = other.fold()
        self.assertEqual((only(state).bsp["bsp"], stop_ids(state.due_stops)),
                         (Decimal("3.10"), [(f"bsp-decided-mismatch-{seq}", "review")]))
        early, r = self.lifecycle()                                    # no CLOSED read and the cutoff not yet passed
        seq = early.note("bsp_decided", at=r.off + HOUR, ref_col=r.ref, decided_at_ns=r.off + HOUR, outcome="none",
                         actual_sp=None, read_seq=None)
        state = early.fold(now=r.off + 2 * HOUR)
        self.assertEqual((only(state).bsp["decided"], stop_ids(state.due_stops)),
                         (False, [(f"bsp-decided-mismatch-{seq}", "review")]))

    def test_invalid_bsp_decided_notes(self):
        journal_, p = self.lifecycle()
        bad = [journal_.inner({"v": 1, "type": "bsp_decided", "contract_sha256": C.sha256, "decided_at_ns": p.off,
                               "outcome": "bsp", "actual_sp": None, "read_seq": None}, customer_order_ref=p.ref),
               journal_.note("bsp_decided", ref_col="ref-none", decided_at_ns=p.off, outcome="none", actual_sp=None,
                             read_seq=None)]
        self.assertEqual(journal_.fold().invalid_records, tuple(bad))      # a wrong time is a mismatch (R2-5)


R2_FLAGS = dict(journal_reconciled=True, unresolved_order=False, cause_written=True)


class AstraReviewR2(unittest.TestCase):
    """Regression tests for Astra's second review of 1763130 (astra-review-p1-05-r2.md), on the inputs of her
    probes (p1-05/astra-r2-*). Each fails on 1763130."""

    def one_gap(self, kind):
        journal_ = Pilot(pt4=True)
        p = journal_.place(journal_.arm(0))
        if kind == "profit":
            journal_.win(p, profit="4.03")
            raised = only(journal_.fold()).l_rows[0]["seq"]
            stop_id = f"cleared-figure-gap-{p.seq}-{raised}"
        else:
            journal_.win(p, commission="0.20")
            raised = [e["seq"] for e in journal_.events if e["kind"] == "note"
                      and e["payload"]["pilot"]["type"] == "capture_commission"][0]
            stop_id = f"commission-gap-{p.seq}-{raised}"
        journal_.journal_due(journal_.fold())
        self.assertEqual([s["stop_id"] for s in journal_.fold().active_stops], [stop_id])
        return journal_, p, stop_id

    @staticmethod
    def figure(journal_, p, kind, agreeing):
        if kind == "profit":
            return journal_.l_row(p, profit="4.00" if agreeing else "4.05", outcome="WIN", at=journal_.now + S)
        return journal_.commission(p, "0.08" if agreeing else "0.30", at=journal_.now + S)

    def resolve(self, journal_, p, stop_id, kind, method):
        if method == "automatic":
            return self.figure(journal_, p, kind, True)
        return journal_.note("gap_resolved", stop_id=stop_id, decision_seq=p.seq, cause_written=True)

    def test_r2_1_a_gap_recurs_before_its_ack(self):
        """Gap, resolution (agreeing figure or gap_resolved), then a new disagreement: a new unacknowledged
        occurrence, so the first stop's ACK is refused while it is open (astra-r2-ledger-probes:
        gap_lifecycle, bad_again_before_ack)."""
        for kind in ("profit", "commission"):
            for method in ("automatic", "explicit"):
                with self.subTest(kind=kind, resolution=method):
                    journal_, p, stop_id = self.one_gap(kind)
                    resolution = self.resolve(journal_, p, stop_id, kind, method)
                    self.assertIsNone(pilot.ack_check(journal_.fold(), stop_id, **R2_FLAGS))
                    bad = self.figure(journal_, p, kind, False)
                    state = journal_.fold()
                    reason = "cleared_figure_gap" if kind == "profit" else "commission_gap"
                    reopened = f"{'-'.join(reason.split('_'))}-{p.seq}-{bad}"
                    self.assertEqual(state.resolved_gaps, {stop_id: resolution})
                    self.assertEqual([s["stop_id"] for s in state.due_stops], [reopened])
                    self.assertEqual(pilot.ack_check(state, stop_id, **R2_FLAGS), "ack_ledger_gap_open")
                    self.assertEqual(only(state).ledger["value_p"], -200)
                    self.assertEqual(state, replayed(journal_))

    def test_r2_1_a_gap_recurs_after_a_valid_ack_and_a_new_arm(self):
        """ACK while the figures agree, ARM the next day, then a fresh bad capture: a new occurrence ends the new
        session, and no PLACE follows (astra-r2-ledger-probes: legitimate_ack_then_new_session_then_bad)."""
        for kind in ("profit", "commission"):
            for method in ("automatic", "explicit"):
                with self.subTest(kind=kind, resolution=method):
                    journal_, p, stop_id = self.one_gap(kind)
                    self.resolve(journal_, p, stop_id, kind, method)
                    journal_.ack(stop_id, cause=True)
                    session = journal_.arm(1)
                    self.assertIsNone(journal_.fold().sessions[-1]["ended_seq"])
                    bad = self.figure(journal_, p, kind, False)
                    fresh = journal_.fold()
                    self.assertEqual(len(fresh.due_stops), 1)
                    self.assertTrue(fresh.due_stops[0]["stop_id"].endswith(f"-{p.seq}-{bad}"))
                    self.assertEqual(fresh.sessions[-1]["ended_seq"], bad)
                    result, placed, _ = journal_.classify(session)
                    self.assertIsNone(placed)
                    self.assertEqual(journal_.fold().place_records[0].ledger["value_p"], -200)

    def test_r2_1_a_contradiction_recurs_after_its_ack(self):
        journal_ = Pilot(pt4=True)
        p = journal_.place(journal_.arm(0))
        journal_.win(p)
        first = journal_.c_read(p, at=journal_.now + S, price="3.50")
        stop_id = f"ledger-contradiction-{p.seq}-{first}"
        journal_.journal_due(journal_.fold())
        journal_.note("gap_resolved", stop_id=stop_id, decision_seq=p.seq, cause_written=True)
        journal_.ack(stop_id, cause=True)
        journal_.c_read(p, at=journal_.now + S, price="3.00")              # agrees: no new occurrence
        self.assertEqual(journal_.fold().due_stops, ())
        again = journal_.c_read(p, at=journal_.now + S, price="3.60")
        self.assertEqual(stop_ids(journal_.fold().due_stops), [(f"ledger-contradiction-{p.seq}-{again}", "review")])

    def test_r2_4_each_refusal_note_is_its_own_occurrence(self):
        """Day 0: a refusal, its stop journaled and ACKed. Day 1: the same refusal again, persisted before an
        interruption: its own unacknowledged stop ends the new session (astra-r2-ledger-probes:
        repeated_refusal)."""
        journal_ = Pilot(pt4=True)
        notes = []
        for day in (0, 1):
            session = journal_.arm(day)
            at = DAY0 + day * DAY_NS + 9 * HOUR
            refused = pilot.classify(C, journal_.fold(now=at), reservation_seq=999, open_id=f"missing-{day}",
                                     session_id=session, registration_receipt_sha256=journal_.registration,
                                     customer_order_ref=f"bad-{day}", row_body=b"", book_body=b"",
                                     observed_wall_ns=at, observed_monotonic_ns=1)
            self.assertEqual(refused["refusal"], "reservation_unknown")
            notes.append(journal_.inner(refused["refusal_record"], at=at + S))
            state = journal_.fold()
            stop_id = f"reservation-unknown-999-{notes[-1]}"
            self.assertEqual([s["stop_id"] for s in state.due_stops], [stop_id])
            self.assertEqual(state.sessions[-1]["ended_seq"], notes[-1])
            if day == 0:
                journal_.journal_due(state)
                self.assertEqual([s["stop_id"] for s in journal_.fold().active_stops], [stop_id])   # one occurrence
                self.assertIsNone(pilot.ack_check(journal_.fold(), stop_id, **R2_FLAGS))
                journal_.ack(stop_id, cause=True)
        result, placed, _ = journal_.classify(session)
        self.assertIsNone(placed)
        self.assertEqual(journal_.fold(), replayed(journal_))

    @classmethod
    def setUpClass(cls):
        """Astra's full N = 400 journal: 399 eligible winners, then the 400th, C only, final at its deadline."""
        journal_ = Pilot(pt4=True)
        for i in range(400):
            if i % 25 == 0:
                session = journal_.arm(i // 25)
            p = journal_.place(session)
            if i < 399:
                journal_.win(p)
            else:
                journal_.send(p)
                journal_.c_read(p)
                journal_.closed(p)
                last = p
        cls.full_n, cls.deadline = journal_, last.off + 7 * DAY_NS

    def n_journal(self):
        journal_ = copy.deepcopy(self.full_n)
        session = journal_.arm(22)
        result, placed, _ = journal_.classify(session, off=self.deadline + 293 * S)  # observed -2 s, appended -1 s
        self.assertEqual((result["record"]["outcome"], placed.record["lifetime_slot"]), ("PLACE", 401))
        return journal_, placed

    def test_r2_2_a_send_after_n_reached_by_time(self):
        """Slot 401 placed validly before N completes at deadline + 1 ns, but submitted at + 7 s and reported at
        + 8 s: a send after the ending (astra-r2-root-probes: authority_probe, n_during_send)."""
        journal_, p = self.n_journal()
        journal_.intent(p, at=self.deadline + 6 * S)
        journal_.submit(p, at=self.deadline + 7 * S)
        journal_.report(p, at=self.deadline + 8 * S)
        state = journal_.fold()
        self.assertEqual(codes(state), ["sent_after_ending"])
        self.assertEqual(state.place_records[-1].measurement["class"], "protocol_violation")

    def test_r2_2_not_sent_and_sent_before_the_ending(self):
        journal_, p = self.n_journal()
        journal_.intent(p, at=self.deadline + 6 * S)
        journal_.submit(p, at=self.deadline + 7 * S)
        journal_.failed(p, at=self.deadline + 8 * S)                      # FAILED(transport_not_sent): no bet
        self.assertEqual(codes(journal_.fold()), [])
        before, q = self.n_journal()
        before.intent(q, at=self.deadline - S // 2)
        before.submit(q, at=self.deadline - S // 4)                       # sent before the ending
        before.report(q, at=self.deadline + 8 * S)                        # its report after it
        before.c_read(q, at=self.deadline + MIN)
        self.assertEqual(codes(before.fold()), [])

    def test_r2_2_a_send_after_the_lifetime_cap(self):
        """The 495th record's own send is allowed; an earlier record's send after it is not."""
        journal_ = Pilot(pt4=True)
        placed = bulk(journal_, 495)
        last = placed[-1]
        journal_.send(last, at=None)
        self.assertEqual(codes(journal_.fold()), [])
        earlier = placed[-2]
        journal_.intent(earlier, at=journal_.now + S)
        journal_.submit(earlier, at=journal_.now + S)
        journal_.report(earlier, at=journal_.now + S)
        self.assertIn("sent_after_ending", codes(journal_.fold()))

    def unsent(self, failed):
        """FAILED(transport_not_sent), or no submit with its open ended where the journal shows it: another open
        reserves (Nuance R3-1: the fold's live_open_id never supports an earlier frontier note)."""
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        _, p, _ = journal_.classify(session)
        if failed:
            journal_.intent(p)
            journal_.submit(p)
            journal_.failed(p)
        journal_.closed(p, sp="3.10")
        if not failed:
            journal_.reserve("1.299999", journal_.now + 300 * S, session=session, open_id="open-later")
        return journal_, p

    def test_r2_3_unsent_frontier_round_trips(self):
        """FAILED_NOT_SENT and no submit (its open ended): final definitely_unsent before seven days with no
        execution row; a finalized note with row None round-trips with no stop, the final time is the derived one,
        and later events never move it (astra-r2-analysis-probes: frontier_probe)."""
        for failed in (True, False):
            with self.subTest(failed_not_sent=failed):
                journal_, p = self.unsent(failed)
                first = only(journal_.fold(live_open_id="open-later"))
                self.assertEqual((first.measurement["class"], first.measurement["final"], first.execution["row"]),
                                 ("definitely_unsent", True, None))
                self.assertLess(first.measurement["final_at_ns"], p.off + 7 * DAY_NS)
                journal_.note("finalized", at=journal_.now + S, ref_col=p.ref,
                              final_at_ns=first.measurement["final_at_ns"], row=None,
                              **{"class": "definitely_unsent"})
                journal_.add("note", {"operator": "later"}, at=journal_.now + HOUR)
                for state in (journal_.fold(), replayed(journal_)):
                    self.assertEqual((state.invalid_records, state.due_stops), ((), ()))
                    self.assertEqual(only(state).measurement, first.measurement)
        journal_, p = self.unsent(False)                               # at the deadline its row is 5
        late = only(journal_.fold(now=p.off + 7 * DAY_NS + 2 * S))
        self.assertEqual((late.execution["row"], late.measurement["class"]), (5, "definitely_unsent"))
        journal_.note("finalized", at=p.off + 7 * DAY_NS + 2 * S, ref_col=p.ref,
                      final_at_ns=late.measurement["final_at_ns"], row=5, **{"class": "definitely_unsent"})
        self.assertEqual(journal_.fold().due_stops, ())

    def test_r2_3_the_note_class_is_never_adopted(self):
        journal_, p = self.unsent(True)
        first = only(journal_.fold())
        claimed = journal_.note("finalized", at=journal_.now + S, ref_col=p.ref,
                                final_at_ns=first.measurement["final_at_ns"], row=None,
                                **{"class": "protocol_violation"})
        state = journal_.fold()
        self.assertEqual((only(state).measurement["class"], stop_ids(state.due_stops)),
                         ("definitely_unsent", [(f"finalized-mismatch-{claimed}", "review")]))
        bad = journal_.note("finalized", at=journal_.now + S, ref_col=p.ref,
                            final_at_ns=first.measurement["final_at_ns"], row=None, **{"class": "eligible"})
        self.assertIn(bad, journal_.fold().invalid_records)          # row None only for classes 1 and 2

    def test_r2_5_frontier_times_must_match_exactly(self):
        """decided_at_ns or final_at_ns of 0 (a wrong past time) or in the future: the mismatch stop, the derived
        values kept (astra-r2-analysis-probes: wrong_frontier_timestamps)."""
        for label, delta in (("past", None), ("future", HOUR)):
            with self.subTest(time=label):
                journal_ = Pilot(pt4=True)
                _, p, _ = journal_.classify(journal_.arm(0))
                journal_.send(p)
                journal_.c_read(p, price="3.44")
                book = journal_.closed(p, sp="3.10")
                derived = only(journal_.fold())
                decided = 0 if delta is None else derived.bsp["decided_at_ns"] + delta
                bsp_note = journal_.note("bsp_decided", ref_col=p.ref, decided_at_ns=decided, outcome="bsp",
                                         actual_sp="3.10", read_seq=book)
                deadline = p.off + 7 * DAY_NS
                final = only(journal_.fold(now=deadline + 2 * S)).measurement
                final_at = 0 if delta is None else final["final_at_ns"] + delta
                final_note = journal_.note("finalized", at=deadline + 2 * S, ref_col=p.ref, final_at_ns=final_at,
                                           row=3, **{"class": "eligible"})
                state = journal_.fold()
                self.assertEqual(stop_ids(state.due_stops), sorted([(f"bsp-decided-mismatch-{bsp_note}", "review"),
                                                                    (f"finalized-mismatch-{final_note}", "review")]))
                self.assertEqual((only(state).bsp["decided_at_ns"], only(state).measurement), (
                    derived.bsp["decided_at_ns"], final))
                self.assertEqual(state.invalid_records, ())


C_CAP2 = dataclasses.replace(C, lifetime_place_records=2)  # the lifetime cap at the second PLACE record


class AstraReviewR3(unittest.TestCase):
    """Regression tests for Astra's third review of 7c083b6 (astra-review-p1-05-r3.md, R3-1 and R3-2), on the inputs
    of her probes (p1-05/astra-r3-analysis-probes: deferred_frontier_probe; astra-r3-root-probes: authority_probe).
    Each fails on 7c083b6, except two pins that already held there: bsp_decided checked at its position, and
    FAILED(transport_not_sent) after an evidence-driven N."""

    @staticmethod
    def c_only():
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        p = journal_.place(session)
        journal_.send(p)
        journal_.c_read(p, price="3.44")
        journal_.closed(p, sp="3.10")
        return journal_, p, session

    def assert_stopped_at(self, state, seq, reason="finalized_mismatch"):
        self.assertEqual([(s["stop_id"], s["source_seq"]) for s in state.due_stops],
                         [(f"{'-'.join(reason.split('_'))}-{seq}", seq)])
        self.assertEqual((state.sessions[0]["ended_seq"], state.sessions[0]["ended_reason"]), (seq, reason))

    # -- R3-1: each frontier note is checked at its own position --------------------------------------------------

    def test_r3_1_a_premature_deadline_note_keeps_its_stop(self):
        """A row-3 eligible note at off + 6 min claiming deadline + 1 ns: not final there, so the stop is set at the
        note and stays once the deadline passes and the record is final as claimed (premature_deadline_note)."""
        journal_, p, _ = self.c_only()
        deadline = p.off + 7 * DAY_NS
        note = journal_.note("finalized", at=p.off + 6 * MIN, ref_col=p.ref, final_at_ns=deadline + 1, row=3,
                             **{"class": "eligible"})
        self.assert_stopped_at(journal_.fold(), note)
        late = journal_.fold(now=deadline + 2 * S)
        self.assertEqual(only(late).measurement, {"class": "eligible", "final": True, "final_at_ns": deadline + 1})
        self.assert_stopped_at(late, note)
        self.assertEqual(late, replayed(journal_, now=deadline + 2 * S))

    def test_r3_1_a_premature_row1_note_keeps_its_stop(self):
        """A row-1 note at off + 6 min, before the L it predicts arrives at off + 15 min (premature_row1_note)."""
        journal_, p, _ = self.c_only()
        note = journal_.note("finalized", at=p.off + 6 * MIN, ref_col=p.ref, final_at_ns=p.off + 15 * MIN, row=1,
                             **{"class": "eligible"})
        self.assert_stopped_at(journal_.fold(), note)
        journal_.l_row(p, price="3.44", at=p.off + 15 * MIN)
        state = journal_.fold()
        self.assertEqual((only(state).execution["row"], only(state).measurement),
                         (1, {"class": "eligible", "final": True, "final_at_ns": p.off + 15 * MIN}))
        self.assert_stopped_at(state, note)

    def test_r3_1_a_mismatch_ends_its_session_before_a_later_place(self):
        """A wrong-class note at seq 13, then PLACE 15 and its send by 18: the session ends at 13, so the PLACE and
        its send are violations; a next day's session and send never move that end (same_session_mismatch,
        next_session_mismatch)."""
        journal_, p, session = self.c_only()
        journal_.l_row(p, price="3.44")
        actual = only(journal_.fold()).measurement["final_at_ns"]
        bad = journal_.note("finalized", at=p.off + 16 * MIN, ref_col=p.ref, final_at_ns=actual, row=1,
                            **{"class": "missing_bsp"})
        q = journal_.place(session)
        journal_.send(q)
        self.assertEqual((bad, q.seq, journal_.events[-1]["seq"]), (13, 15, 18))
        state = journal_.fold()
        self.assert_stopped_at(state, bad)
        later = state.place_records[1]
        self.assertEqual(later.measurement["class"], "protocol_violation")
        self.assertTrue({"session_stopped", "place_while_stopped", "submit_after_session_end"} <= set(later.violations))
        self.assertEqual(state.ending["reason"], "breach")
        self.assertEqual(state.place_records[0].measurement["class"], "eligible")    # the note's class not adopted
        nxt = copy.deepcopy(journal_)
        r = nxt.place(nxt.arm(1))
        nxt.send(r)
        following = nxt.fold()
        self.assertEqual([(s["session_id"], s["ended_seq"]) for s in following.sessions], [("s0", bad), ("s1", None)])
        self.assertIn("place_while_stopped", following.place_records[2].violations)
        self.assertEqual(following, replayed(nxt))

    def test_r3_1_an_open_end_counts_where_the_journal_shows_it(self):
        """No submit and a CLOSED book: the fold (no live open) finds the record definitely unsent, but a row-None
        note journaled before anything shows its open ended is premature, and the open's later end, journaled by
        another open's reservation, never removes that stop. After that reservation a note round-trips."""
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        _, p, _ = journal_.classify(session)
        journal_.closed(p, sp="3.10")
        derived = only(journal_.fold()).measurement
        self.assertEqual((derived["class"], derived["final"]), ("definitely_unsent", True))
        early = journal_.note("finalized", at=journal_.now + S, ref_col=p.ref, final_at_ns=derived["final_at_ns"],
                              row=None, **{"class": "definitely_unsent"})
        self.assert_stopped_at(journal_.fold(), early)
        journal_.reserve("1.299999", journal_.now + 300 * S, session=session, open_id="open-later")
        later = journal_.note("finalized", at=journal_.now + S, ref_col=p.ref, final_at_ns=derived["final_at_ns"],
                              row=None, **{"class": "definitely_unsent"})
        for state in (journal_.fold(), journal_.fold(live_open_id="open-later"), replayed(journal_)):
            self.assertEqual(only(state).measurement, derived)
            self.assertEqual([s["stop_id"] for s in state.due_stops], [f"finalized-mismatch-{early}"])
            self.assertNotIn(later, [s["source_seq"] for s in state.due_stops])

    def test_r3_1_bsp_decided_is_checked_where_it_is(self):
        """A bsp_decided note claiming 3.12 while the CLOSED read so far says 3.10; a later read of 3.12 never
        removes the stop (bsp_mismatch_stays: already positional at 7c083b6, pinned here)."""
        journal_, p, _ = self.c_only()
        read = only(journal_.fold()).bsp["read_seq"]
        note = journal_.note("bsp_decided", at=p.off + 6 * MIN, ref_col=p.ref, decided_at_ns=p.off + 5 * MIN,
                             outcome="bsp", actual_sp="3.12", read_seq=read)
        journal_.closed(p, sp="3.12", at=p.off + 7 * MIN)
        self.assert_stopped_at(journal_.fold(), note, "bsp_decided_mismatch")

    # -- R3-2: a derived ending's boundary --------------------------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        cls.bases = {n: cls.base(n) for n in (1, 400)}

    @staticmethod
    def base(n):
        """Astra's base(n): n - 1 eligible winners, then record n with C only (awaiting L), and slot n + 1 placed
        validly on the next session's day."""
        journal_ = Pilot(pt4=True)
        for i in range(n):
            if i % 25 == 0:
                session = journal_.arm(i // 25)
            a = journal_.place(session)
            if i < n - 1:
                journal_.win(a)
            else:
                journal_.send(a)
                journal_.c_read(a, price="3.44")
                journal_.closed(a)
                journal_.settle(a)
        result, b, _ = journal_.classify(journal_.arm((n - 1) // 25 + 1))
        assert result["record"]["outcome"] == "PLACE"
        return journal_, a, b

    def n_case(self, n, steps):
        journal_, a, b = copy.deepcopy(self.bases[n])
        steps(journal_, a, b)
        contract = C_ONE if n == 1 else C
        state = journal_.fold(contract=contract)
        self.assertEqual(state, replayed(journal_, contract=contract))
        return state

    def test_r3_2_the_submit_position_decides_against_an_evidence_driven_n(self):
        """N completed by an L: a submit before it in seq, at the same timestamp, is no send after the ending, nor
        after a later C read; a submit after it in seq is one, even with its clock stepped back a second
        (authority_probe: n_by_evidence)."""
        def submit_before(j, a, b):                      # submit 4022, completing L 4023 at one timestamp
            j.intent(b)
            j.submit(b)
            j.l_row(a, price="3.44", at=b.off - 292 * S)
            j.report(b)

        def report_before_then_c(j, a, b):               # SUCCESS report before the completing L, later C read
            j.intent(b)
            j.submit(b)
            j.report(b, at=b.off - 292 * S)
            j.l_row(a, price="3.44", at=b.off - 292 * S)
            j.c_read(b)

        def submit_after_regressed(j, a, b):             # completing L 4022, submit 4023 one second earlier
            j.intent(b)
            j.l_row(a, price="3.44", at=b.off - 292 * S)
            j.submit(b, at=b.off - 293 * S)
            j.report(b)

        def submit_after_monotonic(j, a, b):
            j.intent(b)
            j.l_row(a, price="3.44", at=b.off - 292 * S)
            j.submit(b, at=b.off - 291 * S)
            j.report(b, at=b.off - 290 * S)

        for n in (1, 400):
            for steps, flagged in ((submit_before, False), (report_before_then_c, False),
                                   (submit_after_regressed, True), (submit_after_monotonic, True)):
                with self.subTest(n=n, case=steps.__name__):
                    state = self.n_case(n, steps)
                    self.assertTrue(pilot.n_status(state, now_ns=state.now_ns)["reached"])
                    self.assertEqual(codes(state), ["sent_after_ending"] if flagged else [])
                    self.assertEqual(state.ending["reason"], "breach" if flagged else "n_reached")
                    self.assertEqual(state.place_records[-1].violations, ("sent_after_ending",) if flagged else ())

    def test_r3_2_not_sent_after_an_evidence_driven_n(self):
        """A submit after the completing L that the first-byte gate turned into FAILED(transport_not_sent) is no
        send: no violation."""
        def failed_after(j, a, b):
            j.intent(b)
            j.l_row(a, price="3.44", at=b.off - 292 * S)
            j.submit(b, at=b.off - 291 * S)
            j.failed(b, at=b.off - 290 * S)
        state = self.n_case(1, failed_after)
        self.assertEqual((codes(state), state.place_records[-1].measurement["class"]), ([], "definitely_unsent"))

    def test_r3_2_the_lifetime_cap_by_journal_order(self):
        """Two markets with one off and a cap of two: the first record's submit, appended before the cap record at
        the cap's own timestamp, is no send after the ending (its C read comes later); appended after the cap
        record, it is one even with its clock stepped back. The cap record's own send is never one."""
        for after_cap in (False, True):
            with self.subTest(submit_after_cap=after_cap):
                journal_ = Pilot(pt4=True)
                session = journal_.arm(0)
                off = journal_.next_off(session)
                first = journal_.place(session, off=off)
                journal_.intent(first)
                if not after_cap:
                    journal_.submit(first)                                  # off - 292 s
                cap = journal_.place(session, off=off)                      # its decision at off - 294 s
                if after_cap:
                    journal_.submit(first, at=off - 295 * S)                # one second before the cap's decision
                journal_.c_read(first)
                journal_.send(cap)
                state = journal_.fold(contract=C_CAP2)
                self.assertEqual(state.ending["reason"], "breach" if after_cap else "lifetime_cap")
                self.assertEqual("sent_after_ending" in state.place_records[0].violations, after_cap)
                self.assertNotIn("sent_after_ending", state.place_records[1].violations)


class Finalization(unittest.TestCase):
    """The finalization table and canonical observation (note §11 :148-160)."""

    def finalize(self, c=None, l=None, *, c_ns=OFF + MIN, l_ns=OFF + DAY_NS, now=OFF + 2 * DAY_NS, removal=False):
        return pilot.finalize(None if c is None else dec_row(c), None if l is None else dec_row(l), c_ns=c_ns,
                              l_count_ns=l_ns if l is not None else None, deadline_ns=OFF + 7 * DAY_NS, now_ns=now,
                              removal_classed=removal)

    def test_table(self):
        deadline = OFF + 7 * DAY_NS
        cases = {
            "test_c_then_contradicting_l": (current_row(price="3.44"), cleared_row(price="3.06"), 2, None, None),
            "test_c3444_l344": (current_row(price="3.444"), cleared_row(price="3.44"), 2, None, None),
            "test_both_344": (current_row(price="3.44"), cleared_row(price="3.44"), 1, Decimal("2.00"),
                              Decimal("3.44")),
            "sizes differ": (current_row(matched="1.37", cancelled="0.63", price="3.44"),
                             cleared_row(size="1.36", price="3.44"), 2, None, None),
            "test_l_only_not_reduced": (None, cleared_row(price="3.44"), 4, Decimal("2.00"), Decimal("3.44")),
            "test_l_only_reduced": (None, cleared_row(price="3.44", reduced=True), 4, None, None),
            "l reduced missing": (None, cleared_row(price="3.44", reduced=None), 4, None, None),
        }
        for label, (c, l, row, size, price) in cases.items():
            with self.subTest(case=label):
                found = self.finalize(c, l)
                self.assertEqual((found["final"], found["final_at_ns"], found["row"], found["size"], found["P"]),
                                 (True, OFF + DAY_NS, row, size, price))
                self.assertEqual(found["unresolved"], size is None)
        both = self.finalize(current_row(price="3.44"), cleared_row(price="3.44"))
        self.assertEqual(str(both["P"]), "3.44")
        for now, final in ((deadline, False), (deadline + 1, True)):
            found = self.finalize(current_row(price="3.44"), None, now=now)
            self.assertEqual(found["final"], final)
        found = self.finalize(current_row(price="3.44"), None, now=deadline + 1)
        self.assertEqual((found["row"], found["final_at_ns"], found["size"], found["P"]),
                         (3, deadline + 1, Decimal("2.00"), Decimal("3.44")))
        neither = self.finalize(None, None, now=deadline + 1)
        self.assertEqual((neither["row"], neither["unresolved"], neither["final_at_ns"]), (5, True, deadline + 1))
        self.assertFalse(self.finalize(None, None, now=deadline)["final"])
        removed = self.finalize(current_row(price="3.44"), cleared_row(price="3.06", reduced=True), removal=True)
        self.assertEqual((removed["row"], removed["size"], removed["P"]), (1, Decimal("2.00"), None))

    def lifecycle(self, *, receipt=True):
        journal_ = Pilot(pt1=receipt, pt4=receipt)
        _, placed, _ = journal_.classify(journal_.arm(0))
        journal_.send(placed)
        return journal_, placed

    def test_c_only_deadline_and_whole_number_pt(self):
        journal_, p = self.lifecycle()
        journal_.c_read(p, price="3.44")
        deadline = p.off + 7 * DAY_NS
        self.assertFalse(only(journal_.fold(now=deadline)).execution["final"])
        rec = only(journal_.fold(now=deadline + 1))
        self.assertEqual((rec.execution["row"], rec.execution["size"], rec.execution["P"]),
                         (3, Decimal("2.00"), Decimal("3.44")))
        journal_, p = self.lifecycle()
        journal_.c_read(p, price="3.44")
        journal_.l_row(p, price="3.06", at=p.off + DAY_NS)
        rec = only(journal_.fold())
        self.assertEqual((rec.execution["row"], rec.execution["unresolved"], rec.execution["final_at_ns"]),
                         (2, True, p.off + DAY_NS))

    def test_two_c_reads(self):
        journal_, p = self.lifecycle()
        first = journal_.c_read(p, price="3.44")
        journal_.c_read(p, price="3.440000000000001")
        journal_.l_row(p, price="3.44")
        rec = only(journal_.fold())
        self.assertEqual((rec.execution["row"], rec.execution["P"], rec.execution["c_seq"]), (1, Decimal("3.44"), first))
        reads = [{"seq": 11, "recorded_ns": 1, "row": {"status": "EXECUTION_COMPLETE"}},
                 {"seq": 10, "recorded_ns": 2, "row": {"status": "EXECUTION_COMPLETE"}},
                 {"seq": 9, "recorded_ns": 0, "row": {"status": "EXECUTABLE"}}]
        self.assertEqual(pilot.canonical_c(reads)["seq"], 10)

    def test_two_l_rows_any_load_order(self):
        journal_, p = self.lifecycle(receipt=False)
        journal_.c_read(p, price="3.44")
        first = journal_.l_row(p, price="3.44")
        journal_.l_row(p, price="3.45")
        shuffled = list(journal_.events)
        shuffled[-1], shuffled[-2] = shuffled[-2], shuffled[-1]
        with self.assertRaises(PilotError) as caught:
            pilot.fold(shuffled, contract=C, now_ns=journal_.now)
        self.assertEqual(caught.exception.code, "events_not_in_seq_order")
        journal_.receipt("PT1")
        rec = only(journal_.fold())
        self.assertEqual((rec.execution["l_seq"], rec.execution["P"]), (first, Decimal("3.44")))
        rows = [{"seq": s, "recorded_ns": 0, "row": {"betStatus": "SETTLED"}} for s in (14, 12, 13)]
        for order in itertools.permutations(rows):
            self.assertEqual(pilot.canonical_l(list(order))["seq"], 12)

    def test_l_only_reduced_class(self):
        journal_, p = self.lifecycle()
        journal_.closed(p)
        journal_.l_row(p, price="3.44", reduced=True)
        rec = only(journal_.fold())
        self.assertEqual((rec.measurement["class"], rec.execution["size"]), ("post_decision_removal", Decimal("2.00")))

    def test_l_before_receipt(self):
        journal_, p = self.lifecycle(receipt=False)
        journal_.c_read(p, price="3.44")
        journal_.l_row(p, price="3.44", at=p.off + DAY_NS)
        self.assertFalse(only(journal_.fold()).execution["final"])
        seq = journal_.receipt("PT1", at=p.off + 2 * DAY_NS)
        rec = only(journal_.fold())
        self.assertEqual((rec.execution["row"], rec.execution["final_at_ns"], rec.execution["final_seq"]),
                         (1, p.off + 2 * DAY_NS, seq))

    def test_l_receipt_after_deadline(self):
        journal_, p = self.lifecycle(receipt=False)
        journal_.c_read(p, price="3.44")
        journal_.l_row(p, price="3.06", at=p.off + DAY_NS)
        journal_.receipt("PT1", at=p.off + 8 * DAY_NS)
        rec = only(journal_.fold())
        self.assertEqual((rec.execution["row"], rec.execution["P"], rec.execution["final_at_ns"]),
                         (3, Decimal("3.44"), p.off + 7 * DAY_NS + 1))

    def test_cancel_race_and_lapse(self):
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p, 50)
        journal_.c_read(p, matched="1.20", cancelled="0.80")
        rec = only(journal_.fold(now=p.off + 8 * DAY_NS))
        self.assertEqual((rec.execution["size"], rec.execution["consistency_failed"]), (Decimal("1.20"), False))
        journal_ = Pilot(pt4=True)
        _, q, _ = journal_.classify(journal_.arm(0))
        journal_.send(q, 0)
        journal_.c_read(q, matched="1.00", lapsed="1.00")
        rec = only(journal_.fold(now=q.off + 8 * DAY_NS))
        self.assertEqual((rec.execution["size"], rec.execution["unresolved"]), (Decimal("1.00"), False))

    def test_consistency_fail(self):
        """L only, with the place report showing more matched than the final size: unresolved plus a review stop."""
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p, 150)
        journal_.l_row(p, size="1.00", price="3.00")
        state = journal_.fold()
        rec = only(state)
        self.assertEqual((rec.execution["consistency_failed"], rec.execution["size"], rec.ledger["value_p"]),
                         (True, None, -200))
        occurrence(state.due_stops, "consistency_failed", p.seq)
        self.assertFalse(pilot.consistency_check(Decimal("1.00"), place_report_matched_p=150, c=None))
        self.assertTrue(pilot.consistency_check(Decimal("1.00"), place_report_matched_p=None, c=None))

    def test_reduction_with_c_and_l(self):
        journal_, p = self.lifecycle()
        journal_.c_read(p, price="3.44")
        journal_.closed(p)
        journal_.l_row(p, price="3.30", reduced=True)
        rec = only(journal_.fold())
        self.assertEqual((rec.measurement["class"], rec.execution["unresolved"]), ("post_decision_removal", False))

    def test_read_after_final_ignored(self):
        journal_, p = self.lifecycle()
        journal_.c_read(p, price="3.44")
        journal_.closed(p, sp="3.10")
        journal_.l_row(p, price="3.44")
        before = only(journal_.fold())
        self.assertEqual(before.measurement["class"], "eligible")
        journal_.c_read(p, matched="1.00", cancelled="1.00", price="3.50")
        journal_.l_row(p, price="3.50")
        journal_.closed(p, sp="3.12", ours="WINNER")
        after = only(journal_.fold())
        self.assertEqual((after.execution, after.bsp, after.measurement), (before.execution, before.bsp,
                                                                           before.measurement))

    def test_deadline_inclusive(self):
        journal_, p = self.lifecycle()
        journal_.c_read(p, price="3.44")
        journal_.l_row(p, price="3.44", at=p.off + 7 * DAY_NS)
        rec = only(journal_.fold())
        self.assertEqual((rec.execution["row"], rec.execution["final_at_ns"]), (1, p.off + 7 * DAY_NS))
        later, q = self.lifecycle()
        later.c_read(q, price="3.44")
        later.l_row(q, price="3.06", at=q.off + 7 * DAY_NS + 1000)          # recorded_at has microseconds
        self.assertEqual(only(later.fold()).execution["row"], 3)

    def test_neither(self):
        journal_, p = self.lifecycle()
        rec = only(journal_.fold(now=p.off + 7 * DAY_NS + 1))
        self.assertEqual((rec.execution["row"], rec.execution["unresolved"], rec.measurement["class"]),
                         (5, True, "measurement_unresolved"))


class Bsp(unittest.TestCase):
    """The frozen BSP (note §11 :161)."""

    def freeze(self, reads, now=OFF + 2 * DAY_NS):
        captures = [{"seq": i + 1, "recorded_ns": at, "status": status, "bsp_reconciled": reconciled,
                     "runners": {str(FAV): {"status": ours, "actual_sp": sp}}}
                    for i, (at, status, sp, ours, reconciled) in enumerate(reads)]
        return pilot.freeze_bsp(captures, selection_id=FAV, off_ns=OFF, now_ns=now)

    def test_cases(self):
        good = (OFF + HOUR, "CLOSED", "3.10", "LOSER", True)
        cases = {
            "test_bsp_first_closed": ([good], Decimal("3.10")),
            "test_bsp_correction_ignored": ([good, (OFF + 2 * HOUR, "CLOSED", "3.12", "LOSER", True)],
                                            Decimal("3.10")),
            "test_bsp_inplay_only": ([(OFF + MIN, "OPEN", "3.10", "ACTIVE", False)], None),
            "test_bsp_nan": ([(OFF + HOUR, "CLOSED", "NaN", "LOSER", True)], None),
            "test_bsp_inf": ([(OFF + HOUR, "CLOSED", "Infinity", "LOSER", True)], None),
            "test_bsp_missing": ([(OFF + HOUR, "CLOSED", None, "LOSER", True)], None),
            "test_bsp_late": ([(OFF + 86401 * S, "CLOSED", "3.10", "LOSER", True)], None),
            "test_bsp_1_00": ([(OFF + HOUR, "CLOSED", "1.00", "LOSER", True)], None),
            "test_bsp_unreconciled": ([(OFF + HOUR, "CLOSED", "3.10", "LOSER", False)], None),
            "test_bsp_our_runner_removed": ([(OFF + HOUR, "CLOSED", "3.10", "REMOVED", True)], None),
            "at the cutoff": ([(OFF + 86400 * S, "CLOSED", "3.10", "WINNER", True)], Decimal("3.10")),
            "open before closed": ([(OFF + MIN, "OPEN", "2.00", "ACTIVE", True), good], Decimal("3.10")),
        }
        for label, (reads, expected) in cases.items():
            with self.subTest(case=label):
                found = self.freeze(reads)
                self.assertEqual((found["decided"], found["bsp"]), (True, expected))
        self.assertFalse(self.freeze([], now=OFF + 86400 * S)["decided"])
        self.assertTrue(self.freeze([], now=OFF + 86400 * S + 1)["decided"])
        self.assertFalse(self.freeze([(OFF + MIN, "OPEN", "3.10", "ACTIVE", True)], now=OFF + HOUR)["decided"])

    def test_bsp_in_fold(self):
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p)
        journal_.c_read(p, price="3.44")
        journal_.l_row(p, price="3.44")
        journal_.closed(p, status="OPEN", sp="NaN", ours="ACTIVE")
        self.assertEqual(only(journal_.fold(now=p.off + DAY_NS)).measurement["final"], False)
        rec = only(journal_.fold(now=p.off + DAY_NS + 1))
        self.assertEqual((rec.bsp["bsp"], rec.measurement["class"]), (None, "missing_bsp"))
        removed = Pilot(pt4=True)
        _, q, _ = removed.classify(removed.arm(0))
        removed.send(q)
        removed.c_read(q, price="3.44")
        removed.closed(q, ours="REMOVED")
        removed.l_row(q, price="3.44")
        rec = only(removed.fold())
        self.assertEqual((rec.bsp["bsp"], rec.measurement["class"]), (None, "void"))

    def test_closed_book_extraction(self):
        book = book_item(status="CLOSED", bspReconciled=True, runners=[
            {"selectionId": 11, "status": "WINNER", "sp": {"nearPrice": Raw("NaN"), "farPrice": Raw("Infinity"),
                                                           "actualSP": Raw("3.1")}},
            {"selectionId": 12, "status": "LOSER", "sp": {"actualSP": Raw("NaN")}},
            {"selectionId": 13, "status": "REMOVED"}])
        found = pilot.extract_closed_book(rpc(book), market_id=MARKET)
        self.assertEqual(found, {"status": "CLOSED", "bsp_reconciled": True, "runners": {
            "11": {"status": "WINNER", "actual_sp": "3.1"}, "12": {"status": "LOSER", "actual_sp": "NaN"},
            "13": {"status": "REMOVED", "actual_sp": None}}})
        self.assertRaises(pilot.EvidenceError, pilot.extract_closed_book, rpc(book_item(runners=[
            {"selectionId": 11}, {"selectionId": 11}])), market_id=MARKET)

    def test_order_row_extraction(self):
        body = dumps({"jsonrpc": "2.0", "id": 1, "result": {"moreAvailable": False, "currentOrders": [
            {"betId": "5001", "status": "EXECUTION_COMPLETE", "sizeMatched": Raw("1.3700000000000001"),
             "sizeCancelled": Raw("0.63"), "sizeLapsed": Raw("0"), "sizeVoided": Raw("0.0"), "sizeRemaining": 0,
             "averagePriceMatched": Raw("3.449"), "selectionId": 11, "marketId": MARKET, "customerOrderRef": "ref-1",
             "customerStrategyRef": "p1-fav-t5"}, {"betId": "5002", "sizeMatched": Raw("NaN")}]}}).encode()
        row = pilot.extract_current_row(body, bet_id="5001")
        self.assertEqual((row["sizeMatched"], row["sizeLapsed"], row["sizeRemaining"], row["averagePriceMatched"]),
                         ("1.3700000000000001", "0", "0", "3.449"))
        self.assertRaises(pilot.EvidenceError, pilot.extract_current_row, body, bet_id="5002")
        self.assertRaises(pilot.EvidenceError, pilot.extract_current_row, body, bet_id="9")
        cleared = dumps({"jsonrpc": "2.0", "id": 1, "result": {"moreAvailable": False, "clearedOrders": [
            {"betId": "5001", "sizeSettled": Raw("2.0"), "priceMatched": Raw("3.44"), "profit": Raw("-2.0"),
             "betOutcome": "LOSE", "selectionId": 11, "marketId": MARKET}]}}).encode()
        row = pilot.extract_cleared_row(cleared, bet_id="5001")
        self.assertEqual((row["priceReduced"], row["profit"], row["betStatus"]), (None, "-2.0", "SETTLED"))


class Nclv(unittest.TestCase):
    """NCLV with c = 0.02, exact (K:149-159; P:56-58)."""

    def test_pins(self):
        c = C.commission_rate
        cases = {"test_nclv_par": ("3.00", "3.00", Fraction(-1, 75)),
                 "test_nclv_306": ("3.06", "3.00", Fraction(47, 7500)),
                 "test_nclv_4_35": ("4.00", "3.50", Fraction(22, 175)),
                 "test_nclv_2_2": ("2", "2", Fraction(-1, 100)),
                 "test_nclv_150_160": ("1.50", "1.60", Fraction(-11, 160)),
                 "test_nclv_fractional": ("3.475", "3.10", Fraction(21, 200))}
        for label, (price, bsp, x) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(pilot.nclv(Decimal(price), Decimal(bsp), c=c), x)
        breakeven = Fraction(149, 49)                       # 1 + 2/0.98 (critique 1)
        self.assertEqual(pilot.nclv(breakeven, Decimal(3), c=c), 0)
        self.assertEqual(pilot.nclv(Fraction(150, 49), Decimal(3), c=c), Fraction(1, 150))
        self.assertEqual(round(float(breakeven / 3 - 1), 4), 0.0136)
        self.assertEqual(pilot.nclv(Decimal("3.06"), Decimal("3.00"), c=c) * 100, Fraction(47, 75))

    def test_monotone(self):
        rng = random.Random(3)
        c = C.commission_rate
        for _ in range(max(SEEDS, 40) * 5):
            p1, p2 = sorted(rng.sample(range(101, 100000), 2))
            b1, b2 = sorted(rng.sample(range(101, 100000), 2))
            bsp = Fraction(rng.randrange(101, 100000), 100)
            price = Fraction(rng.randrange(101, 100000), 100)
            self.assertLess(pilot.nclv(Fraction(p1, 100), bsp, c=c), pilot.nclv(Fraction(p2, 100), bsp, c=c))
            self.assertGreater(pilot.nclv(price, Fraction(b1, 100), c=c), pilot.nclv(price, Fraction(b2, 100), c=c))

    def test_partial_fill_unweighted_and_reporting(self):
        full, part = synthetic("eligible", 1, x=Fraction(3, 100)), synthetic("eligible", 2, x=Fraction(3, 100),
                                                                                  size=Decimal("1.37"))
        x = [pilot.nclv(r.execution["P"], r.bsp["bsp"], c=C.commission_rate) for r in (full, part)]
        self.assertEqual(x, [Fraction(3, 100)] * 2)                          # the size never enters NCLV
        mean = pilot.stake_weighted_mean([(Decimal("2.00"), Fraction(1, 10)), (Decimal("1.00"), Fraction(-1, 10))])
        self.assertEqual(mean, Fraction(1, 30))
        self.assertIsNone(pilot.stake_weighted_mean([(Decimal("0"), Fraction(1))]))
        self.assertEqual(pilot.decision_price_nclv(300, Decimal("3.00"), c=C.commission_rate), Fraction(-1, 75))
        self.assertRaises(PilotError, pilot.nclv, 3.0, Decimal(3), c=C.commission_rate)


class ClassPrecedence(unittest.TestCase):
    """Measurement classes, first match wins (note §11 :163-172)."""

    def journal(self):
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        return journal_, p

    def cls(self, journal_, p, *, now=None, live=None):
        return only(journal_.fold(now=now or p.off + 8 * DAY_NS, live_open_id=live)).measurement

    def test_cases(self):
        def violation_unsent(j, p):
            j.note("violation", code="test_violation", source_seq=p.seq)

        def unsent_void(j, p):
            j.closed(p, ours="REMOVED")

        def void_removal(j, p):
            j.send(p)
            j.c_read(p)
            j.closed(p, ours="REMOVED", removed=(12,))

        def removal_mismatch(j, p):
            j.send(p)
            j.c_read(p, price="3.44")
            j.closed(p, removed=(12,))
            j.l_row(p, price="3.06")

        def unresolved(j, p):
            j.send(p, 0)
            j.c_read(p, matched="0.00", cancelled="1.99", price=None)
            j.closed(p)

        def unmatched_no_bsp(j, p):
            j.send(p, 0)
            j.c_read(p, matched="0.00", cancelled="2.00", price=None)

        def missing_bsp(j, p):
            j.send(p)
            j.c_read(p)
            j.closed(p, reconciled=False)
            j.l_row(p)

        def eligible(j, p):
            j.send(p)
            j.c_read(p, price="3.44")
            j.closed(p)
            j.l_row(p, price="3.44")

        def partial(j, p):
            j.send(p, 0)
            j.c_read(p, matched="1.37", cancelled="0.63", price="3.44")
            j.closed(p)
            j.l_row(p, size="1.37", price="3.44")

        def unknown(j, p):
            j.send(p)
            j.closed(p)

        def dead_heat(j, p):
            eligible(j, p)
            j.closed(p, ours="WINNER", winners={12}, at=p.off + 4 * MIN)

        cases = {"test_violation_beats_unsent": (violation_unsent, "protocol_violation"),
                 "test_unsent_beats_void": (unsent_void, "definitely_unsent"),
                 "test_void_beats_removal": (void_removal, "void"),
                 "test_removal_beats_mismatch": (removal_mismatch, "post_decision_removal"),
                 "test_unresolved_never_unmatched": (unresolved, "measurement_unresolved"),
                 "test_unmatched_beats_missing_bsp": (unmatched_no_bsp, "unmatched"),
                 "test_missing_bsp": (missing_bsp, "missing_bsp"), "test_eligible": (eligible, "eligible"),
                 "test_partial_eligible": (partial, "eligible"),
                 "test_unknown_at_deadline": (unknown, "measurement_unresolved")}
        for label, (lifecycle, expected) in cases.items():
            with self.subTest(case=label):
                journal_, p = self.journal()
                lifecycle(journal_, p)
                found = self.cls(journal_, p)
                self.assertEqual((found["class"], found["final"]), (expected, True))
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p)
        journal_.c_read(p, price="3.44")
        journal_.closed(p, ours="WINNER", winners={12})
        journal_.l_row(p, price="3.44", profit="4.88", outcome="WIN")
        rec = only(journal_.fold())
        self.assertEqual((rec.measurement["class"], rec.ledger["reason"]), ("eligible", "dead_heat_or_no_winner"))

    def test_class_never_changes_after_final(self):
        rng = random.Random(21)
        for seed in range(max(SEEDS // 4, 8)):
            journal_ = Pilot(pt4=True)
            _, p, _ = journal_.classify(journal_.arm(0))
            journal_.send(p)
            journal_.c_read(p, price="3.44")
            journal_.closed(p)
            journal_.l_row(p, price="3.44")
            before = only(journal_.fold())
            self.assertTrue(before.measurement["final"])
            for _ in range(rng.randrange(1, 6)):
                choice = rng.randrange(5)
                if choice == 0:
                    journal_.c_read(p, matched=rng.choice(["1.00", "2.00"]), cancelled="1.00",
                                    price=rng.choice(["3.5", "2.98"]))
                elif choice == 1:
                    journal_.l_row(p, price=rng.choice(["3.06", "3.44"]), reduced=rng.choice([True, False]))
                elif choice == 2:
                    journal_.closed(p, sp=rng.choice(["3.12", "NaN"]), ours=rng.choice(["WINNER", "REMOVED"]))
                elif choice == 3:
                    journal_.closed(p, removed=(12, 13))
                else:
                    journal_.receipt("PT1", verified=False)
            after = only(journal_.fold(now=p.off + 30 * DAY_NS))
            with self.subTest(seed=seed):
                self.assertEqual(after.measurement, before.measurement)


# ---------------------------------------------------------------------------------------------------------------
# Synthetic folded states for the sample, gates and verdict: RecordState values with final classes.

BASE_STATE = pilot.fold([], contract=C, now_ns=DAY0)


def synthetic(cls, index, *, day=None, x=None, size=None, pt4=False, final=True, bsp=Decimal(2)):
    """One final PLACE record of class cls; an eligible one has NCLV x at BSP 2."""
    price = None
    if cls == "eligible":
        x = Fraction(1, 100) if x is None else Fraction(x)
        price = 1 + ((x + 1) * 2 - 1) / Fraction(49, 50)
        size = Decimal("2.00") if size is None else size
    elif cls == "unmatched":
        size = Decimal(0)
    elif cls in ("missing_bsp",):
        size, price, bsp = Decimal("2.00"), Decimal("3.00"), None
    elif cls in ("measurement_unresolved", "definitely_unsent", "protocol_violation"):
        size = None
    else:
        size = Decimal("2.00") if size is None else size
    off = DAY0 + (index if day is None else day) * DAY_NS + 12 * HOUR + index * S
    return pilot.RecordState(
        ref=f"ref-{index}", market_id=f"1.{index + 1}", selection_id=FAV, reservation_seq=2 * index + 1,
        decision_seq=2 * index + 2, open_id=f"open-{index}", session_id="s", record={}, pt4=pt4,
        utc_day=pilot.utc_day(off), off_ns=off, deadline_ns=off + 7 * DAY_NS, decision_wall_ns=off - 295 * S,
        submit_seq=None if cls == "definitely_unsent" else 1, failed_not_sent=False,
        unsent=cls == "definitely_unsent", violations=(), place_report=None, c_reads=(), l_rows=(),
        execution={"final": final, "final_at_ns": off + DAY_NS if final else None, "row": 1, "size": size,
                   "P": price, "unresolved": size is None},
        bsp={"decided": True, "decided_at_ns": off + HOUR, "bsp": bsp, "read_seq": 1, "closed_read": None},
        measurement={"class": cls, "final": final, "final_at_ns": off + DAY_NS if final else None},
        ledger={"part": "W", "value_p": -200, "basis": None, "reason": "worst_case"}, contradicted_seq=None)


def state_of(records, *, violations=(), ended=True, now=DAY0 + 100 * DAY_NS):
    notes = ({"reason": "yu_written", "code": "yu", "seq": 1, "ns": DAY0},) if ended else ()
    return dataclasses.replace(BASE_STATE, place_records=tuple(records), lifetime_used=len(records),
                               violations=tuple(violations), ending_notes=notes, now_ns=now)


def eligible_spread(n, days=16, start=0, x=None):
    """n eligible records over days dates; NCLV about 1% with a small spread (or exactly x for all)."""
    return [synthetic("eligible", start + i, day=i % days,
                      x=Fraction(1, 100) + Fraction(i % 7 - 3, 1000) if x is None else x) for i in range(n)]


class Pt4Membership(unittest.TestCase):
    """PT4 records are always reported apart (Yu, 2026-10-21; contract re-freeze pending): one function,
    pt4_membership, decides it, whatever sessions or registrations follow."""

    def journal_with_pt4(self, *, next_registration=None, sessions_after=1, receipt=True):
        journal_ = Pilot()
        pt4_session = journal_.arm(0)
        pt4_records = []
        for _ in range(5):
            pt4_records.append(journal_.classify(pt4_session)[1])
            journal_.lose(pt4_records[-1])
        if receipt:
            journal_.receipt("PT4", matched=1, at=DAY0 + 22 * HOUR)
        later = []
        for day in range(1, 1 + sessions_after):
            session = journal_.arm(day, registration=next_registration)
            later.append(journal_.classify(session)[1])
            journal_.send(later[-1])
        return journal_, pt4_records, later

    def test_pt4_records_always_apart(self):
        for label, kw in {"same registration": {}, "new registration": dict(next_registration=sha("reg-2")),
                          "no session after": dict(sessions_after=0), "no receipt": dict(receipt=False)}.items():
            with self.subTest(case=label):
                journal_, pt4_records, later = self.journal_with_pt4(**kw)
                state = journal_.fold()
                # With no PT4 receipt yet, the next session is a PT4 session again (PT4 repeats).
                expected_apart = [p.ref for p in pt4_records] + ([p.ref for p in later] if label == "no receipt"
                                                                 else [])
                apart = [r for r in state.place_records if pilot.pt4_membership(r) == "apart"]
                self.assertEqual([r.ref for r in apart], expected_apart)
                self.assertEqual(pilot.pt4_partition(state)["apart"], expected_apart)
                for rec in state.place_records:
                    self.assertEqual(pilot.pt4_membership(rec), "apart" if rec.pt4 else "sample")
                split = pilot.analysis_population(state, now_ns=state.now_ns)
                self.assertEqual([r.ref for r in split["apart"]], expected_apart)
                self.assertEqual([r.ref for r in split["population"]],
                                 [p.ref for p in later if p.ref not in expected_apart])
                self.assertEqual(codes(state), [])

    def test_pt4_repeat(self):
        journal_ = Pilot()
        first = journal_.arm(0)
        a = journal_.classify(first)[1]
        journal_.receipt("PT4", matched=0)
        second = journal_.arm(1)
        b = journal_.classify(second)[1]
        journal_.receipt("PT4", matched=1)
        third = journal_.arm(2)
        c = journal_.classify(third)[1]
        state = journal_.fold()
        self.assertEqual([(r.ref, r.pt4, pilot.pt4_membership(r)) for r in state.place_records],
                         [(a.ref, True, "apart"), (b.ref, True, "apart"), (c.ref, False, "sample")])
        self.assertEqual([s["pt4"] for s in state.sessions], [True, True, False])
        self.assertEqual(state.place_records[1].record["day_limit"], 5)

    def test_pt4_records_use_slots_and_count_in_ledger_and_pause(self):
        journal_, pt4_records, later = self.journal_with_pt4()
        state = journal_.fold()
        self.assertEqual(state.lifetime_used, 6)
        self.assertEqual([r.record["lifetime_slot"] for r in state.place_records], [1, 2, 3, 4, 5, 6])
        self.assertEqual(state.equity_p, 5 * -200 + -200)
        self.assertEqual([r.ledger["part"] for r in state.place_records[:5]], ["S"] * 5)
        crowded = dataclasses.replace(state, equity_p=state.equity_p - 13800)
        self.assertEqual(pilot.capacity_and_pause(C, crowded, utc_day="2027-01-06", session_id="s1")["code"],
                         "pause")
        pt4_only = Pilot()
        session = pt4_only.arm(0)
        for _ in range(5):
            pt4_only.classify(session)
        result = pt4_only.classify(session)[0]
        self.assertEqual((result["record"]["code"], result["record"]["day_limit"]), ("cap", 5))

    def test_pt4_records_are_earlier_place_records_for_n(self):
        """A pending PT4 record blocks N although it is apart (critique 10)."""
        records = [synthetic("eligible", 0, pt4=True, final=False)] + eligible_spread(400, start=1)
        status = pilot.n_status(state_of(records), now_ns=DAY0 + 100 * DAY_NS)
        self.assertEqual((status["reached"], status["eligible_counted"]), (False, 0))
        records[0] = synthetic("eligible", 0, pt4=True)
        status = pilot.n_status(state_of(records), now_ns=DAY0 + 100 * DAY_NS)
        self.assertEqual((status["reached"], status["eligible_counted"], status["index_400th"]), (True, 400, 400))

    def test_sample_starts_after_pt4(self):
        records = [synthetic("eligible", i, pt4=True) for i in range(5)] + eligible_spread(400, start=5)
        state = state_of(records)
        split = pilot.analysis_population(state, now_ns=state.now_ns)
        self.assertEqual((len(split["apart"]), len(split["population"]), len(split["surplus"])), (5, 400, 0))
        self.assertTrue(all(not r.pt4 for r in split["population"]))


class Gates(unittest.TestCase):
    """The validity gates, exactly (K:256-271), on the analysis population."""

    def population(self, **counts):
        records, index = [], 0
        for cls, n in counts.items():
            for _ in range(n):
                records.append(synthetic(cls, index, day=index % 16))
                index += 1
        return records

    def test_gates(self):
        cases = {
            "test_fill_320_400": (dict(eligible=320, unmatched=80), "fill_rate", Fraction(4, 5), True),
            "test_fill_319_400": (dict(eligible=319, unmatched=81), "fill_rate", Fraction(319, 400), False),
            "test_missing_bsp_8_400": (dict(eligible=392, missing_bsp=8), "missing_bsp_share", Fraction(1, 50), True),
            "test_missing_bsp_9_409": (dict(eligible=400, missing_bsp=9), "missing_bsp_share", Fraction(9, 409),
                                       False),
            "test_unresolved_4_495": (dict(eligible=491, measurement_unresolved=4), "measurement_unresolved_share",
                                      Fraction(4, 495), True),
            "test_unresolved_5_495": (dict(eligible=490, measurement_unresolved=5), "measurement_unresolved_share",
                                      Fraction(5, 495), False),
        }
        for label, (counts, gate, value, passed) in cases.items():
            with self.subTest(case=label):
                found = pilot.validity_gates(self.population(**counts), contract=C)[gate]
                self.assertEqual((found["value"], found["pass"]), (value, passed))
        sent = pilot.validity_gates(self.population(eligible=10, definitely_unsent=5, protocol_violation=1),
                                    contract=C)["measurement_unresolved_share"]
        self.assertEqual((sent["num"], sent["den"]), (0, 11))

    def test_zero_denominators(self):
        for label, counts in {"test_zero_denominators": dict(definitely_unsent=3),
                              "test_zero_eligible_some_missing": dict(missing_bsp=3, unmatched=2)}.items():
            with self.subTest(case=label):
                records = self.population(**counts)
                gates = pilot.validity_gates(records, contract=C)
                self.assertEqual({g["pass"] for g in gates.values()}, {None})
                found = pilot.verdict(state_of(records), contract=C, now_ns=DAY0 + 100 * DAY_NS)
                self.assertEqual((found["result"], found["reason"]), ("INCONCLUSIVE", "insufficient_eligible_sample"))

    def test_surplus_and_apart_excluded(self):
        records = [synthetic("missing_bsp", i, pt4=True) for i in range(5)]
        records += eligible_spread(400, start=5)
        records += [synthetic("missing_bsp", 405 + i, day=i) for i in range(50)]
        state = state_of(records)
        found = pilot.verdict(state, contract=C, now_ns=state.now_ns)
        self.assertEqual((found["population_size"], found["surplus"], found["apart"]), (400, 50, 5))
        self.assertEqual(found["gates"]["missing_bsp_share"]["num"], 0)
        self.assertEqual(found["result"], "PASS")


class SampleAndEndings(unittest.TestCase):
    """The sample, late completion, endings and the data lock (K:31-41, K:240-252; note §12, §15)."""

    NOW = DAY0 + 100 * DAY_NS

    def test_population_ends_at_400th(self):
        records = eligible_spread(410)
        split = pilot.analysis_population(state_of(records), now_ns=self.NOW)
        self.assertEqual((len(split["population"]), len(split["surplus"]), split["n_reached"]), (400, 10, True))
        self.assertEqual(split["population"][-1].ref, "ref-399")

    def test_pending_blocks_n(self):
        records = eligible_spread(410)
        records[3] = synthetic("eligible", 3, day=3, final=False)
        state = state_of(records)
        self.assertFalse(pilot.n_status(state, now_ns=self.NOW)["reached"])
        records[3] = synthetic("eligible", 3, day=3)
        status = pilot.n_status(state_of(records), now_ns=self.NOW)
        self.assertEqual((status["reached"], status["index_400th"]), (True, 399))
        self.assertEqual(len(pilot.analysis_population(state_of(records), now_ns=self.NOW)["surplus"]), 10)

    def cap_records(self, completed):
        records = eligible_spread(395)
        for i in range(10):
            cls = "eligible" if i < completed else "unmatched"
            records.append(synthetic(cls, 395 + i, day=i % 16))
        records += [synthetic("unmatched", 405 + i, day=i % 16) for i in range(90)]
        return records

    def test_cap_late_completion(self):
        for completed, expected in ((5, "PASS"), (3, "INCONCLUSIVE")):
            with self.subTest(completed=completed):
                records = self.cap_records(completed)
                state = state_of(records, ended=False)
                self.assertIn(pilot.placing_ending(state, now_ns=self.NOW)["reason"], ("n_reached", "lifetime_cap"))
                self.assertEqual(pilot.n_status(state, now_ns=self.NOW)["reached"], completed == 5)
                found = pilot.verdict(state, contract=C, now_ns=self.NOW)
                self.assertEqual(found["result"], expected)
                if completed == 3:
                    self.assertEqual(found["reason"], "insufficient_eligible_sample")
                else:
                    self.assertEqual(found["inference"]["N"], 400)

    def test_ending_final_date(self):
        journal_ = Pilot(pt4=True)
        day = (T("2027-06-29T00:00:00Z") - DAY0) // DAY_NS
        session = journal_.arm(day, until_h=23)
        state = journal_.fold(now=T("2027-06-29T12:00:00Z"))
        self.assertIsNone(pilot.placing_ending(state, now_ns=T("2027-06-29T23:59:59Z")))
        self.assertEqual(pilot.placing_ending(state, now_ns=T("2027-06-30T00:00:00Z"))["reason"], "final_date")
        result, _, _ = journal_.classify(session, observed=T("2027-06-30T00:00:00Z"),
                                         off=T("2027-06-30T00:04:55Z"), journal_it=False)
        self.assertIn(result["refusal"], ("placing_ended",))

    def test_ending_lifetime(self):
        journal_ = Pilot(pt4=True)
        bulk(journal_, 495)
        state = journal_.fold()
        self.assertEqual((state.lifetime_used, state.ending["reason"], codes(state)), (495, "lifetime_cap", []))
        session = journal_.arm(40)
        result = journal_.classify(session)[0]
        self.assertEqual((result["refusal"], result["record"]["code"]), (None, "cap"))
        self.assertEqual(journal_.fold().ending["reason"], "lifetime_cap")

    def test_ending_yu_written(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        journal_.classify(session)
        journal_.note("ending", reason="yu_written", code="yu_ended_in_writing")
        state = journal_.fold()
        self.assertEqual(state.ending["reason"], "yu_written")
        self.assertEqual(journal_.classify(session)[0]["refusal"], "placing_ended")
        late = journal_.place(session)
        self.assertIn("place_after_ending", codes(journal_.fold()))
        self.assertIsNotNone(late)

    def test_breach_ends_at_once(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        _, placed, _ = journal_.classify(session)
        journal_.note("violation", code="operator_reported", source_seq=placed.seq)
        state = journal_.fold()
        self.assertEqual(state.ending["reason"], "breach")
        self.assertEqual(only(state).measurement["class"], "protocol_violation")
        self.assertEqual(journal_.classify(session)[0]["refusal"], "placing_ended")
        breach = Pilot(pt4=True)
        breach.note("ending", reason="breach", code="money_number_changed")
        self.assertEqual(breach.fold().ending["reason"], "breach")

    def test_not_violations(self):
        journal_ = Pilot(pt4=True)
        session = journal_.arm(0)
        journal_.reserve("1.700", journal_.next_off(session), session=session, open_id="open-crashed")
        _, p, _ = journal_.classify(session)
        journal_.send(p)
        journal_.c_read(p)
        journal_.l_row(p)
        state = journal_.fold(now=p.off + 2 * DAY_NS)
        self.assertEqual((state.violations, state.ending), ((), None))
        self.assertEqual(only(state).measurement["class"], "missing_bsp")
        self.assertTrue(state.reservations["1.700"]["interrupted"])

    def test_send_without_record_is_violation(self):
        journal_ = Pilot(pt4=True)
        journal_.arm(0)
        journal_.add("intent", {"order": {"selection_id": 11, "handicap": 0, "side": "BACK", "price_h": 300,
                                          "size_p": 200, "persistence_type": "LAPSE"},
                                "customer_strategy_ref": "p1-fav-t5", "is_probe": False, "parent_ref": None,
                                "provisional_child": False, "source": "pilot"},
                     intent_id="i-x", customer_order_ref="ref-x", customer_ref="c-x", market_id="1.5",
                     state_after="RECORDED")
        state = journal_.fold()
        self.assertEqual(codes(state), ["send_without_record"])
        self.assertEqual(state.ending["reason"], "breach")

    def test_fold_violations(self):
        def wrong_body(j, p):
            j.intent(p, order=dict(pilot.expected_instruction(p.record), price_h=305))

        def wrong_strategy(j, p):
            j.intent(p)
            j.submit(p, strategy="other")

        def two_instructions(j, p):
            j.intent(p)
            j.submit(p, count=2)

        def late_send(j, p):
            j.intent(p)
            j.submit(p, at=p.off - 240 * S)
            j.report(p, at=p.off - 239 * S)

        def not_pilot_source(j, p):
            j.intent(p, source="cli_manual")              # not a pilot intent (finding 19) ...
            j.submit(p)                                     # ... but this submit names a pilot record's ref

        def wrong_market(j, p):
            j.add("intent", {"order": pilot.expected_instruction(p.record), "customer_strategy_ref": "p1-fav-t5",
                             "is_probe": False, "parent_ref": None, "provisional_child": False, "source": "pilot"},
                  at=p.off - 293 * S, intent_id=f"i-{p.ref}", customer_order_ref=p.ref, customer_ref=f"c-{p.ref}",
                  market_id="1.999", state_after="RECORDED")

        cases = {"r13_body_differs": wrong_body, "strategy_ref_differs": wrong_strategy,
                 "more_than_one_instruction": two_instructions, "sent_after_deadline": late_send,
                 "submit_without_intent": not_pilot_source, "r13_market_differs": wrong_market}
        for code, step in cases.items():
            with self.subTest(code=code):
                journal_ = Pilot(pt4=True)
                _, p, _ = journal_.classify(journal_.arm(0))
                step(journal_, p)
                self.assertIn(code, codes(journal_.fold(live_open_id=p.open_id)))
        record_cases = {"place_outside_window": dict(decision_book_wall_ns=None),
                        "place_order_not_allowed": dict(stake_p=199),
                        "place_country_not_allowed": dict(country="FR"),
                        "place_price_not_allowed": dict(limit_price_h=410),
                        "place_send_deadline_differs": dict(send_deadline_ns=None),
                        "place_slot_differs": dict(lifetime_slot=7), "place_book_delayed":
                            dict(is_market_data_delayed=None)}
        for code, change in record_cases.items():
            with self.subTest(code=code):
                journal_ = Pilot(pt4=True)
                session = journal_.arm(0)
                off = journal_.next_off(session)
                change = dict(change)
                if "decision_book_wall_ns" in change:
                    change["decision_book_wall_ns"] = off - 284 * S
                if "send_deadline_ns" in change:
                    change["send_deadline_ns"] = off - 239 * S
                journal_.place(session, off=off, record_changes=change)
                self.assertIn(code, codes(journal_.fold()))
        unarmed = Pilot(pt4=True)
        session = unarmed.arm(0)
        unarmed.place(session, off=DAY0 + 23 * HOUR + 10 * MIN)
        self.assertIn("session_expired", codes(unarmed.fold()))

    def test_data_lock(self):
        journal_ = Pilot(pt4=True)
        _, p, _ = journal_.classify(journal_.arm(0))
        journal_.send(p)
        journal_.c_read(p)
        journal_.closed(p)
        state = journal_.fold(now=p.off + 2 * DAY_NS)
        self.assertFalse(pilot.data_lock_reached(state, now_ns=state.now_ns))
        with self.assertRaises(PilotError) as caught:
            pilot.verdict(state, contract=C, now_ns=state.now_ns)
        self.assertEqual(caught.exception.code, "data_lock_not_reached")
        later = journal_.fold(now=p.off + 8 * DAY_NS)
        self.assertTrue(only(later).measurement["final"])
        self.assertFalse(pilot.data_lock_reached(later, now_ns=later.now_ns))   # placing has not ended
        journal_.note("ending", reason="yu_written", code="yu_ended_in_writing", at=p.off + 2 * DAY_NS)
        ended = journal_.fold(now=p.off + 2 * DAY_NS + S)
        self.assertFalse(pilot.data_lock_reached(ended, now_ns=ended.now_ns))
        locked = journal_.fold(now=p.off + 8 * DAY_NS)
        self.assertTrue(pilot.data_lock_reached(locked, now_ns=locked.now_ns))
        found = pilot.verdict(locked, contract=C, now_ns=locked.now_ns)
        self.assertEqual((found["result"], found["apart"]), ("INCONCLUSIVE", 0))

    def test_verdict_refused_while_placing_open(self):
        state = state_of(eligible_spread(400), ended=False)
        state = dataclasses.replace(state, place_records=state.place_records[:399], lifetime_used=399)
        self.assertIsNone(pilot.placing_ending(state, now_ns=self.NOW))
        with self.assertRaises(PilotError) as caught:
            pilot.verdict(state, contract=C, now_ns=self.NOW)
        self.assertEqual(caught.exception.code, "data_lock_not_reached")

    def test_place_after_n_reached(self):
        records = eligible_spread(400)
        state = state_of(records, ended=False)
        status = pilot.n_status(state, now_ns=self.NOW)
        self.assertEqual(pilot.placing_ending(state, now_ns=self.NOW),
                         {"reason": "n_reached", "seq": None, "at_ns": status["at_ns"]})


class VerdictAndCr1(unittest.TestCase):
    """The verdict order and CR1 (K:85-100, K:272-279; P:64-70)."""

    NOW = DAY0 + 100 * DAY_NS
    # m -> CR1's result in the 16x25 case (se2 1/150000, t*SE about 0.0055034).
    PINNED = {"0.02": "PASS", "-0.02": "FAIL", "0.005": "INCONCLUSIVE", "0.0055": "INCONCLUSIVE",
              "0.0056": "PASS", "-0.0055": "INCONCLUSIVE", "-0.0056": "FAIL", "0": "INCONCLUSIVE"}

    def test_invalid_beats_pass_like_data(self):
        violation = {"code": "send_without_record", "seq": 9, "ns": DAY0, "ref": None}
        state = state_of(eligible_spread(400, x=Fraction(1, 10)), violations=[violation])
        found = pilot.verdict(state, contract=C, now_ns=self.NOW)
        self.assertEqual((found["result"], found["reason"]), ("INVALID", "protocol_violation"))
        clean = pilot.verdict(state_of(eligible_spread(400, x=Fraction(1, 10))), contract=C, now_ns=self.NOW)
        self.assertEqual(pilot.verdict(state_of(eligible_spread(400)), contract=C, now_ns=self.NOW)["result"], "PASS")
        self.assertEqual(clean["result"], "INCONCLUSIVE")                 # identical x: se_undefined
        self.assertEqual(clean["reason"], "se_undefined")

    def test_breach_invalid(self):
        journal_ = Pilot(pt4=True)
        journal_.note("ending", reason="breach", code="session_yu_did_not_start")
        state = journal_.fold()
        self.assertEqual(pilot.verdict(state, contract=C, now_ns=state.now_ns)["result"], "INVALID")

    def test_399(self):
        records = eligible_spread(399) + [synthetic("unmatched", 399, day=3)]
        found = pilot.verdict(state_of(records), contract=C, now_ns=self.NOW)
        self.assertEqual((found["result"], found["reason"]), ("INCONCLUSIVE", "insufficient_eligible_sample"))
        self.assertTrue(all(g["pass"] for g in found["gates"].values()))
        failing = eligible_spread(399) + [synthetic("unmatched", 399 + i, day=i % 16) for i in range(101)]
        found = pilot.verdict(state_of(failing), contract=C, now_ns=self.NOW)
        self.assertEqual((found["result"], found["reason"]), ("INVALID", "gate_failed"))

    def test_g14_too_few_and_se_zero(self):
        records = [synthetic("eligible", i, day=i % 14, x=Fraction(i % 7, 100)) for i in range(400)]
        found = pilot.verdict(state_of(records), contract=C, now_ns=self.NOW)
        self.assertEqual((found["result"], found["reason"], found["inference"]["G"]),
                         ("INCONCLUSIVE", "too_few_clusters", 14))
        same = pilot.cr1([(f"d{i % 15}", Fraction(1, 50)) for i in range(400)], min_clusters=15,
                         alpha=Fraction(1, 40))
        self.assertEqual((same["result"], same["reason"], same["G"]), ("INCONCLUSIVE", "se_undefined", 15))

    def test_cr1_toy(self):
        found = pilot.cr1([("a", 1), ("a", 2), ("b", 3)], min_clusters=2, alpha=Fraction(1, 40))
        self.assertEqual((found["m"], found["se2"], found["G"], found["N"]), (2, Fraction(4, 9), 2, 3))
        self.assertEqual(pilot.cr1([("a", 1), ("a", 2), ("b", 3)], min_clusters=15,
                                   alpha=Fraction(1, 40))["reason"], "too_few_clusters")

    def sixteen(self, m):
        return [(f"d{d:02}", Fraction(m) + (Fraction(1, 100) if d < 8 else -Fraction(1, 100)))
                for d in range(16) for _ in range(25)]

    def test_cr1_16x25(self):
        base = pilot.cr1(self.sixteen(Fraction(2, 100)), min_clusters=15, alpha=Fraction(1, 40))
        self.assertEqual(base["se2"], Fraction(1, 150000))
        self.assertAlmostEqual(math.sqrt(base["se2"]), 0.0025819889, places=10)
        self.assertAlmostEqual(base["t"], 2.131449546, places=9)
        self.assertAlmostEqual(base["t"] * math.sqrt(base["se2"]), 0.0055033791, places=10)
        for m, expected in self.PINNED.items():
            with self.subTest(m=m):
                found = pilot.cr1(self.sixteen(Fraction(m)), min_clusters=15, alpha=Fraction(1, 40))
                self.assertEqual(found["result"], expected)
                self.assertEqual(found["m"], Fraction(m))
                self.assertFalse(found["near_tie"])
        records = [synthetic("eligible", i, day=d, x=x) for i, (d, x) in enumerate(
            (int(day[1:]), x) for day, x in self.sixteen(Fraction(2, 100)))]
        found = pilot.verdict(state_of(records), contract=C, now_ns=self.NOW)
        self.assertEqual((found["result"], found["inference"]["G"]), ("PASS", 16))

    def test_near_tie_flag(self):
        t = pilot.student_t_quantile(0.975, 15)
        se = math.sqrt(1 / 150000)
        m = Fraction(t * se)
        found = pilot.cr1(self.sixteen(m), min_clusters=15, alpha=Fraction(1, 40))
        self.assertTrue(found["near_tie"])

    def test_cr1_properties(self):
        rng = random.Random(400)
        for _ in range(max(SEEDS // 2, 20)):
            bets = [(f"d{rng.randrange(20)}", Fraction(rng.randrange(-50, 51), 1000)) for _ in range(rng.randrange(20, 80))]
            found = pilot.cr1(bets, min_clusters=2, alpha=Fraction(1, 40))
            shuffled = list(bets)
            rng.shuffle(shuffled)
            again = pilot.cr1(shuffled, min_clusters=2, alpha=Fraction(1, 40))
            self.assertEqual((found["m"], found["se2"], found["result"]), (again["m"], again["se2"], again["result"]))
            if found["se2"] is not None:
                self.assertGreaterEqual(found["se2"], 0)
            singletons = [(f"x{i}", x) for i, (_, x) in enumerate(bets)]
            n = len(singletons)
            single = pilot.cr1(singletons, min_clusters=2, alpha=Fraction(1, 40))
            mean = sum(x for _, x in singletons) / n
            self.assertEqual(single["se2"], Fraction(n, n - 1) * sum((x - mean) ** 2 for _, x in singletons) / n ** 2)

    def test_min_clusters_16_not_threshold(self):
        bets = [(f"d{d}", Fraction(5, 100) + Fraction(d % 2, 100)) for d in range(15) for _ in range(27)]
        found = pilot.cr1(bets, min_clusters=C.min_clusters, alpha=C.alpha_one_sided)
        self.assertEqual((found["G"], found["result"]), (15, "PASS"))

    def test_cr1_t_exact_binary64(self):
        """t is the exact Fraction of the binary64 quantile, so the bound is the one decided on (Astra #7); t_p is the
        float 0.975 exactly, not 39/40."""
        found = pilot.cr1(self.sixteen(Fraction(2, 100)), min_clusters=15, alpha=Fraction(1, 40))
        t = found["t"]
        self.assertIs(type(t), Fraction)
        self.assertEqual(t.denominator & (t.denominator - 1), 0)
        self.assertEqual(Fraction(float(t)), t)
        self.assertEqual(t, Fraction(pilot.student_t_quantile(float(1 - Fraction(1, 40)), 15)))
        self.assertIs(type(found["t_p"]), Fraction)                      # a float would compare equal below
        self.assertIs(type(found["t_df"]), int)
        self.assertEqual((found["t_df"], found["t_p"]), (15, Fraction(0.975)))
        self.assertEqual(found["t_p"], Fraction(8782019273372467, 2 ** 53))
        self.assertNotEqual(found["t_p"], Fraction(39, 40))

    def test_cr1_bound_is_t2_se2(self):
        """bound is t*t*se2 with the returned t, the old Fraction(t) ** 2 * se2, and _cr1_decide on it gives the
        returned result and near tie: there is one t, so nothing downstream can decide on another copy."""
        rng = random.Random(400)
        cases = [(self.sixteen(Fraction(m)), 15) for m in self.PINNED]
        cases.append((self.sixteen(Fraction(pilot.student_t_quantile(0.975, 15) * math.sqrt(1 / 150000))), 15))
        for _ in range(max(SEEDS // 2, 20)):
            cases.append(([(f"d{rng.randrange(20)}", Fraction(rng.randrange(-50, 51), 1000))
                           for _ in range(rng.randrange(20, 80))], 2))
        seen = set()
        for bets, min_clusters in cases:
            found = pilot.cr1(bets, min_clusters=min_clusters, alpha=Fraction(1, 40))
            t, se2 = found["t"], found["se2"]
            self.assertEqual([type(found[key]) for key in ("t", "bound", "t_df", "t_p")],
                             [Fraction, Fraction, int, Fraction])
            self.assertEqual(found["bound"], t * t * se2)
            self.assertEqual(found["bound"], Fraction(pilot.student_t_quantile(0.975, found["G"] - 1)) ** 2 * se2)
            self.assertEqual(found["t_df"], found["G"] - 1)
            self.assertEqual(pilot._cr1_decide(found["m"], found["bound"]), (found["result"], found["near_tie"]))
            self.assertIsNone(found["reason"])
            seen.add((found["result"], found["near_tie"]))
        self.assertTrue({"PASS", "FAIL", "INCONCLUSIVE"} <= {result for result, _ in seen})
        self.assertEqual({near for _, near in seen}, {False, True})

    def test_t_perturbation_band(self):
        """Outside the near-tie band a t error of 1e-10 never changes the result: it moves the bound by about 2e-10/t
        of itself, well inside the band's 10^-9 for any t >= 1.96. The band's edge, |m^2 - bound| = bound/10^9, is
        inside it; at m^2 == bound PASS holds (inclusive) but FAIL does not (strict)."""
        delta = Fraction(1, 10 ** 10)
        for m, expected in self.PINNED.items():
            with self.subTest(m=m):
                found = pilot.cr1(self.sixteen(Fraction(m)), min_clusters=15, alpha=Fraction(1, 40))
                self.assertFalse(found["near_tie"])
                self.assertEqual({pilot._cr1_decide(found["m"], (found["t"] + d) ** 2 * found["se2"])[0]
                                  for d in (-delta, delta)}, {expected})
        # m^2 = B(1 +- eps) exactly is unreachable with sixteen()'s se2 = 1/150000, so se2 = bound / t^2 here.
        t = Fraction(pilot.student_t_quantile(0.975, 15))
        outside, edge = Fraction(1, 10 ** 9) + Fraction(1, 10 ** 12), Fraction(1, 10 ** 9)
        for m in (Fraction(55, 10000), -Fraction(55, 10000), Fraction(2, 100), -Fraction(2, 100)):
            for sign in (1, -1):
                expected = ("PASS" if m >= 0 else "FAIL") if sign > 0 else "INCONCLUSIVE"
                with self.subTest(m=m, sign=sign):
                    bound = m * m / (1 + sign * outside)
                    se2 = bound / (t * t)
                    self.assertEqual(pilot._cr1_decide(m, bound), (expected, False))
                    self.assertEqual({pilot._cr1_decide(m, (t + d) ** 2 * se2)[0] for d in (-delta, delta)},
                                     {expected})
                    self.assertEqual(pilot._cr1_decide(m, m * m / (1 + sign * edge)), (expected, True))
            with self.subTest(m=m, tie=True):
                self.assertEqual(pilot._cr1_decide(m, m * m), ("PASS" if m > 0 else "INCONCLUSIVE", True))

    def test_cr1_no_t_without_decision(self):
        """too_few_clusters and se_undefined give no t, bound, t_df or t_p and no near tie; se2 is None only on
        too_few_clusters and stays the computed zero on se_undefined (Astra v2 #3); verdict carries both as cr1
        returns them. The too_few_clusters inputs have constant x, so they also pin Q19's order: G is checked
        before se2 == 0."""
        no_t = dict.fromkeys(("t", "bound", "t_df", "t_p"))
        cases = {("too_few_clusters", "cr1"): pilot.cr1([("a", 1), ("b", 1)], min_clusters=15, alpha=Fraction(1, 40)),
                 ("too_few_clusters", "verdict"): pilot.verdict(
                     state_of(eligible_spread(400, days=14, x=Fraction(1, 10))), contract=C,
                     now_ns=self.NOW)["inference"],
                 ("se_undefined", "cr1"): pilot.cr1([(f"d{i % 15}", Fraction(1, 50)) for i in range(400)],
                                                    min_clusters=15, alpha=Fraction(1, 40)),
                 ("se_undefined", "verdict"): pilot.verdict(
                     state_of(eligible_spread(400, x=Fraction(1, 10))), contract=C, now_ns=self.NOW)["inference"]}
        for (reason, source), found in cases.items():
            with self.subTest(reason=reason, source=source):
                self.assertEqual((found["result"], found["reason"]), ("INCONCLUSIVE", reason))
                self.assertEqual({key: found[key] for key in no_t}, no_t)
                self.assertIs(found["near_tie"], False)
                if reason == "too_few_clusters":
                    self.assertIsNone(found["se2"])
                else:
                    self.assertIs(type(found["se2"]), Fraction)
                    self.assertEqual(found["se2"], 0)


class StudentT(unittest.TestCase):
    """The stdlib Student t quantile used by CR1 (design §3.12; P:66)."""

    REFERENCE = {1: 12.706204736, 14: 2.144786688, 15: 2.131449546, 16: 2.119905299, 24: 2.063898562,
                 30: 2.042272456, 40: 2.021075390, 100: 1.983971519, 1000: 1.962339081}

    def test_t_reference(self):
        for df, expected in self.REFERENCE.items():
            with self.subTest(df=df):
                self.assertLess(abs(pilot.student_t_quantile(0.975, df) - expected), 1e-9)

    def test_t_cdf_roundtrip(self):
        for df in range(1, 201):
            for p in (0.9, 0.95, 0.975, 0.995):
                self.assertLess(abs(pilot.student_t_cdf(pilot.student_t_quantile(p, df), df) - p), 1e-12, (df, p))

    def test_t_closed_form_oracle(self):
        """Abramowitz and Stegun 26.7.3 (odd df) and 26.7.4 (even df), an independent CDF."""
        def closed(t, n):
            theta = math.atan(t / math.sqrt(n))
            s, c = math.sin(theta), math.cos(theta)
            if n % 2:
                term, total = c, (c if n > 1 else 0.0)
                for k in range(3, n - 1, 2):
                    term *= (k - 1) / k * c * c
                    total += term
                a = 2 / math.pi * (theta + s * total) if n > 1 else 2 / math.pi * theta
            else:
                term, total = 1.0, 1.0
                for k in range(2, n - 1, 2):
                    term *= (k - 1) / k * c * c
                    total += term
                a = s * total
            return 0.5 + a / 2

        for df in range(1, 201):
            for t in (0.1, 0.5, 1.0, 2.0, 2.131449546, 3.0, 8.0):
                self.assertLess(abs(closed(t, df) - pilot.student_t_cdf(t, df)), 1e-12, (df, t))

    def test_t_monotone_df_and_symmetry(self):
        values = [pilot.student_t_quantile(0.975, df) for df in range(1, 300)]
        self.assertTrue(all(a > b for a, b in zip(values, values[1:])))
        self.assertLess(abs(pilot.student_t_quantile(0.975, 10 ** 6) - 1.959963985), 1e-5)
        for df in (1, 5, 15, 40):
            for t in (0.3, 1.7, 2.5):
                self.assertLess(abs(pilot.student_t_cdf(-t, df) - (1 - pilot.student_t_cdf(t, df))), 1e-15)

    def test_betainc_edges_and_guards(self):
        self.assertEqual(pilot.betainc_reg(2.5, 0.5, 0.0), 0.0)
        self.assertEqual(pilot.betainc_reg(2.5, 0.5, 1.0), 1.0)
        for p in (0.5, 1.0, 0.2, 1.5, 0.975001 - 1):
            with self.subTest(p=p):
                self.assertRaises(PilotError, pilot.student_t_quantile, p, 15)
        self.assertRaises(PilotError, pilot.student_t_quantile, 0.975, 0)
        self.assertRaises(PilotError, pilot.student_t_quantile, 0.975, True)


if __name__ == "__main__":
    unittest.main()
