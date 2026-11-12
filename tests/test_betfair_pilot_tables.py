"""Pilot 1's registered end tables T1-T3 (betangel_research.betfair.pilot_tables) against note §12's block
"Registered report tables (Yu, 2026-10-31)" (docs/betfair-pilot-engineering.md, lines 194-234) and the build spec
of 2026-11-12.

Each test names in its docstring the note sentence it pins. States are synthetic RecordState values (k_record,
which wraps test_betfair_pilot.synthetic) or folds of synthetic journals (test_betfair_pilot.Pilot). Every expected
figure is computed here from the note's own rules in exact Fractions (the oracles below), never by running the
module under test; pilot.cr1, pilot.nclv and pilot.s_winner_value are used only where the note or contract p1
names them as the rule. Only the repository's sources, the note and movement-v1 are read; no clock, network,
journal file or Keychain is touched.
"""

import ast
import copy
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import inspect
import json
import math
import random
import re
import unittest

from betangel_research.betfair import pilot, pilot_tables, ticks
from betangel_research.betfair.backtest import accounting, movement
from betangel_research.betfair.pilot import DAY_NS, PilotError

import test_betfair_backtest_boundaries as bb
import test_betfair_boundaries as boundaries
import test_betfair_pilot as tp
import test_betfair_reports_no_account_data as reports
from test_betfair_pilot import C, DAY0, MIN, S, Pilot, state_of, synthetic

TABLES = tp.REPO / "betangel_research" / "betfair" / "pilot_tables.py"
NOTE = tp.REPO / "docs" / "betfair-pilot-engineering.md"
MOVEMENT = tp.REPO / "research" / "studies" / "movement-v1.json"
MOVEMENT_SHA256 = "08cf0c755599862f26cc396ae8f15c990221e97fc2b324b6e2446c40cd852cf9"
MODULE = f"{bb.BETFAIR}.pilot_tables"
# The modules that import pilot_tables. The analysis command's PR adds itself here (spec A3; Yu's OK).
IMPORTERS = ()
# A K that is not part of a word and not contract p1's "K:<line>" citation (note line 197).
BARE_K = re.compile(r"(?<![A-Za-z0-9_])K(?![A-Za-z0-9_]|:[0-9])")
SEL = 900000001                          # a selection id that cannot occur in honest output
NOW_LATE = DAY0 + 400 * DAY_NS
T = pilot.parse_utc_ns
CR = C.commission_rate
T2 = "no_decision_to_buy"

# ---------------------------------------------------------------------------------------------------------------
# The note's fixed values, copied here from the note (lines 203, 212, 216, 219, 225-234) and the spec (4.2, 4.5);
# the module's constants must equal them, and test_date_lists_are_the_notes ties them to the note's text.

NOTE_BANDS = (("B1", 150, 199), ("B2", 200, 248), ("B3", 250, 298), ("B4", 300, 400))
NOTE_FIRST_DAY, NOTE_LAST_DAY = "2026-12-01", "2027-06-30"
NOTE_BANK_HOLIDAYS_EW = ("2026-12-25", "2026-12-28", "2027-01-01", "2027-03-26", "2027-03-29", "2027-05-03",
                         "2027-05-31")
NOTE_PUBLIC_HOLIDAYS_IE = ("2026-12-25", "2026-12-26", "2027-01-01", "2027-02-01", "2027-03-17", "2027-03-29",
                           "2027-05-03", "2027-06-07")
NOTE_FESTIVALS = (
    ("Ladbrokes Christmas Festival (King George VI Chase)", "Kempton (GB)", "2026-12-26", "2026-12-27", True),
    ("Christmas Festival", "Leopardstown (IE)", "2026-12-26", "2026-12-29", False),
    ("Dublin Racing Festival", "Leopardstown (IE)", "2027-01-30", "2027-01-31", False),
    ("The Cheltenham Festival", "Cheltenham (GB)", "2027-03-16", "2027-03-19", True),
    ("Easter Festival (Irish Grand National)", "Fairyhouse (IE)", "2027-03-27", "2027-03-29", True),
    ("Randox Grand National Festival", "Aintree (GB)", "2027-04-08", "2027-04-10", True),
    ("Punchestown Festival", "Punchestown (IE)", "2027-04-27", "2027-05-01", False),
    ("Betfred Guineas Festival", "Newmarket (GB)", "2027-04-30", "2027-05-02", True),
    ("Betfred Derby Festival", "Epsom (GB)", "2027-06-04", "2027-06-05", True),
    ("Royal Ascot", "Ascot (GB)", "2027-06-15", "2027-06-19", True),
)
NOTE_T3_ROWS = ("H_SATURDAY", "H_OTHER", "SUNDAY_NOT_H", "WEDNESDAY_NOT_H", "OTHER_WEEKDAYS_NOT_H")
NOTE_T3_ROW_LABELS = {"H_SATURDAY": "H Saturday", "H_OTHER": "H other", "SUNDAY_NOT_H": "Sunday, not H",
                      "WEDNESDAY_NOT_H": "Wednesday, not H", "OTHER_WEEKDAYS_NOT_H": "other weekdays, not H"}
H_ROWS = ("H_SATURDAY", "H_OTHER")
NOTE_T2_STATES = {"no_decision_to_buy": "no file", "decided_not_bought": "files not bought",
                  "awaiting_files": "files not yet held"}
UNCONFIRMED = ("Christmas Festival, Leopardstown (IE)", "Dublin Racing Festival, Leopardstown (IE)",
               "Punchestown Festival, Punchestown (IE)")

# The fixed rendered texts of spec 4.5.
TITLE = "Registered report tables T1-T3 (note §12). End only. They never decide, and nothing here is a test."
N_NOT_REACHED = "Bets: N not reached; every eligible bet of the analysis population ({})."
NOT_FORMED = "k_outcome bets: not formed: input missing: {}"
T1_NOT = "T1. k_outcome by price band: not computed: input missing: {}"
T3_NOT = "T3. Day types with a Wednesday placebo: not computed: input missing: {}"
T2_LINES = {
    "no_decision_to_buy": ("T2. Late-move cells: not computed: no file",),
    "decided_not_bought": ("T2. Late-move cells: not computed: files not bought",
                           "Departure to report: a decision to buy T2's files was not carried out (note §12)."),
    "awaiting_files": ("T2. Late-move cells: not computed: files not yet held",
                       "T2 runs once, in its own terminal run, when the files are held (note §12)."),
}
NO_BAND = "Bets with a limit price in no band: {} (should be 0)."
MAIN_LABEL = "main: H minus other weekdays"
PLACEBO_LABEL = "placebo: non-H Wednesday minus other weekdays"
MARK_HEADING = "placebo at least half"
POOLED_SENTENCE = "The pooled mean NCLV is over the k_outcome bets; it can differ from contract p1's primary mean."
OUTSIDE_DAYS = "Bets on days before 2026-12-01 or after 2027-06-30: {}."
UNCONFIRMED_PREFIX = "Festival rows still unconfirmed"


# ---------------------------------------------------------------------------------------------------------------
# Days, independent of the module: the BST rule through backtest.accounting.race_day_of, and the T3 rows from the
# note's lists above.

def iso_days(first, last):
    day, end, found = datetime.strptime(first, "%Y-%m-%d"), datetime.strptime(last, "%Y-%m-%d"), []
    while day <= end:
        found.append(day.strftime("%Y-%m-%d"))
        day += timedelta(days=1)
    return found


ORACLE_H_OTHER = (set(NOTE_BANK_HOLIDAYS_EW) | set(NOTE_PUBLIC_HOLIDAYS_IE)
                  | {day for _, _, first, last, _ in NOTE_FESTIVALS for day in iso_days(first, last)})


def oracle_row(day):
    """T3's row of a date by the note's rule (line 216), computed here."""
    weekday = datetime.strptime(day, "%Y-%m-%d").weekday()
    if weekday == 5:
        return "H_SATURDAY"
    if day in ORACLE_H_OTHER:
        return "H_OTHER"
    if weekday == 6:
        return "SUNDAY_NOT_H"
    if weekday == 2:
        return "WEDNESDAY_NOT_H"
    return "OTHER_WEEKDAYS_NOT_H"


def moment(ns):
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ns // 1000)


def london_oracle(ns):
    return accounting.race_day_of(moment(ns)).isoformat()


def day_of(text):
    """The day offset of a 'YYYY-MM-DD' from DAY0 (2027-01-04, a Monday), as k_record's day takes it."""
    return (T(text + "T00:00:00Z") - DAY0) // DAY_NS


def offsets_in(row, count, start=0):
    """The first count day offsets from start whose date is in T3 row `row` (by the oracle)."""
    found, day = [], start
    while len(found) < count:
        if oracle_row(pilot.utc_day(DAY0 + day * DAY_NS)) == row:
            found.append(day)
        day += 1
    return found


# ---------------------------------------------------------------------------------------------------------------
# States.

def k_record(index, *, day, limit_h=300, P="3.00", bsp="3.10", ours="LOSER", other_winners=None, size="2.00",
             pt4=False, ledger=None, off_ns=None, cls="eligible", extra_runners=None):
    """An eligible RecordState with the fields the tables read, and ids that cannot occur in the rendering.
    other_winners: how many other runners are WINNER (default 1 when ours is LOSER, 0 when ours is WINNER);
    extra_runners: more runner entries for the CLOSED read. P and bsp are decimal text (a Decimal, as the fold
    keeps them) or a Fraction (as synthetic() uses). Always pass day: the final times come from synthetic()'s
    own off."""
    base = synthetic(cls, index, day=day, pt4=pt4, size=Decimal(size))
    off = base.off_ns if off_ns is None else off_ns
    price = Decimal(P) if isinstance(P, str) else P
    sp = Decimal(bsp) if isinstance(bsp, str) else bsp
    winners = (0 if ours == "WINNER" else 1) if other_winners is None else other_winners
    runners = {str(SEL): {"status": ours, "actual_sp": str(sp)}}
    for k in range(max(5, winners)):
        runners[str(SEL + 1 + k)] = {"status": "WINNER" if k < winners else "LOSER", "actual_sp": "5.00"}
    runners.update(extra_runners or {})
    decided = off - 295 * S
    return dataclasses.replace(
        base, ref=f"p1tab-{index}", market_id=f"1.98765{index:04d}", selection_id=SEL, off_ns=off,
        utc_day=pilot.utc_day(decided), decision_wall_ns=decided,
        record={"limit_price_h": limit_h, "off_ns": off, "utc_day": pilot.utc_day(decided)},
        execution=dict(base.execution, P=price, size=Decimal(size)),
        bsp=dict(base.bsp, bsp=sp, closed_read={"seq": 1, "recorded_ns": off + 5 * MIN, "status": "CLOSED",
                                                 "bsp_reconciled": True, "runners": runners}),
        ledger=dict(ledger) if ledger is not None else base.ledger)


def s_ledger(value_p):
    return {"part": "S", "value_p": value_p, "basis": None, "reason": None}


def w_ledger(value_p=-200):
    return {"part": "W", "value_p": value_p, "basis": None, "reason": "worst_case"}


def own_net_p(size, P):
    """Our own net in pence (note §10; K:111), computed here: the gross floored to the penny, the commission on it
    rounded up."""
    gross = math.floor(Fraction(size) * (Fraction(P) - 1) * 100)
    return gross - math.ceil(CR * max(gross, 0))


def spread(n, days, *, start=0, first=0, bsps=("2.90", "2.88"), **kw):
    """n k_record bets over `days` UTC dates (offset start + i % days), one WINNER each, ours on every third; the
    BSP changes with the date so the CR1 variance is not 0."""
    return [k_record(first + i, day=start + i % days, bsp=bsps[(i % days) % len(bsps)],
                     ours="WINNER" if i % 3 == 0 else "LOSER", **kw) for i in range(n)]


VARIED = (("1.52", 152), ("2.04", 204), ("2.56", 256), ("3.05", 305), ("3.444", 340), ("3.95", 395))
VARIED_BSP = ("1.60", "2.10", "2.40", "3.20", "3.10", "4.20", "2.90")


def varied(index, day, **kw):
    """A k_record whose price, band, BSP and result vary with index."""
    P, limit_h = VARIED[index % len(VARIED)]
    return k_record(index, day=day, limit_h=limit_h, P=P, bsp=VARIED_BSP[index % len(VARIED_BSP)],
                    ours="WINNER" if index % 3 == 0 else "LOSER", **kw)


def on_days(offsets, *, first=0, per_day=2):
    return [varied(first + i * per_day + j, day) for i, day in enumerate(offsets) for j in range(per_day)]


# (London date of the off, limit price, P, BSP, our result): every band and every T3 row.
MIXED = (("2027-01-05", 150, "1.52", "1.60", "WINNER"), ("2027-01-05", 300, "3.05", "3.40", "LOSER"),
         ("2027-01-06", 200, "2.04", "2.10", "LOSER"), ("2027-01-07", 250, "2.56", "2.40", "WINNER"),
         ("2027-01-09", 400, "4.10", "3.80", "LOSER"), ("2027-01-10", 199, "1.99", "2.06", "WINNER"),
         ("2027-01-13", 248, "2.50", "2.62", "LOSER"), ("2027-01-16", 298, "3.00", "2.90", "WINNER"),
         ("2027-01-17", 150, "1.50", "1.47", "LOSER"), ("2027-03-16", 300, "3.444", "3.2", "WINNER"),
         ("2027-03-17", 200, "2.02", "2.20", "LOSER"), ("2027-01-08", 350, "3.50", "3.75", "LOSER"))


def mixed_records():
    """Twelve bets over all four bands and all five T3 rows; every other one in S at its true value."""
    records = []
    for index, (day, limit_h, P, bsp, ours) in enumerate(MIXED):
        ledger = None
        if index % 2 == 0:
            ledger = s_ledger(own_net_p("2.00", P) if ours == "WINNER" else -200)
        records.append(k_record(index, day=day_of(day), limit_h=limit_h, P=P, bsp=bsp, ours=ours, ledger=ledger))
    return records


def mixed_state():
    return state_of(mixed_records())


def with_closed_runners(rec, runners):
    return dataclasses.replace(rec, bsp=dict(rec.bsp, closed_read=dict(rec.bsp["closed_read"], runners=runners)))


def ledger_journal():
    """Five eligible bets from one journal (2027-01-04, the default book: limit 3.00, P 3.00, BSP 3.10, unless
    said): an S loser; an S winner at our own net (profit 4.00, 392p); an S winner valued at Betfair's figure
    (profit 3.99, 391p); a winner still in W (never settled); and a partial fill, 1.37 matched at 2.52 from a
    2.52 book (B3), in S at 203p. Placing ended in writing; fold at the last off + 8 days."""
    journal = Pilot(pt4=True)
    session = journal.arm(0)
    placed = []
    for life in ("lose", "win", "win_399"):
        _, p, _ = journal.classify(session)
        if life == "lose":
            journal.lose(p)
        else:
            journal.win(p, profit="4.00" if life == "win" else "3.99")
        placed.append(p)
    _, p, _ = journal.classify(session)
    journal.send(p)
    journal.c_read(p)
    journal.closed(p, ours="WINNER")
    journal.l_row(p, profit="4.00", outcome="WIN")
    journal.commission(p, "0.08")
    placed.append(p)
    market = "1.100777"
    book = tp.book_item(market, runners=tp.base_runners(r11=((("2.52", "15.00"),), (("2.54", "20.00"),), "ACTIVE")))
    _, p, _ = journal.classify(session, market=market, book=book)
    journal.send(p, 0)
    journal.c_read(p, matched="1.37", cancelled="0.63", price="2.52")
    journal.closed(p, ours="WINNER")
    journal.settle(p)
    journal.l_row(p, size="1.37", price="2.52", profit="2.08", outcome="WIN")
    journal.commission(p, "0.05")
    placed.append(p)
    journal.note("ending", reason="yu_written", code="yu_ended_in_writing")
    return journal, placed[-1].off + 8 * DAY_NS


def breach_journal():
    """One S loser, then a protocol violation: placing ends by breach; the record is a protocol violation."""
    journal = Pilot(pt4=True)
    _, p, _ = journal.classify(journal.arm(0))
    journal.lose(p)
    journal.note("violation", code="operator_reported", source_seq=p.seq)
    return journal, p.off + 8 * DAY_NS


def full_journal(sps):
    """400 settled bets over 16 UTC dates (ours WINNER on every third slot), all in S, BSP sps[0] or sps[1] by the
    date: N is reached, so placing ends; ("2.90", "2.88") gives PASS and ("3.10", "3.12") FAIL."""
    journal = Pilot(pt4=True)

    def life(j, p):
        won = p.record["lifetime_slot"] % 3 == 0
        j.send(p)
        j.c_read(p)
        j.closed(p, ours="WINNER" if won else "LOSER", sp=sps[(p.off // DAY_NS) % 2])
        j.settle(p)
        j.l_row(p, profit="4.00" if won else "-2.00", outcome="WIN" if won else "LOSE")
        j.commission(p, "0.08" if won else "0.00")

    placed = tp.bulk(journal, 400, life)
    return journal, placed[-1].off + 8 * DAY_NS


VIOLATION = {"code": "send_without_record", "seq": 9, "ns": DAY0, "ref": None}


def verdict_cases():
    """label -> (a function giving a fresh state, the verdict's (result, reason)); synthetic and journal-built."""
    def journal_case(build, *args):
        made = []

        def state():                       # the journal is built once; each call is a fresh fold of it
            if not made:
                made.append(build(*args))
            journal, now = made[0]
            return journal.fold(now=now)

        return state

    return {
        "PASS": (lambda: state_of(spread(400, 20)), ("PASS", None)),
        "FAIL": (lambda: state_of(spread(400, 20, bsps=("3.10", "3.12"))), ("FAIL", None)),
        "too_few_clusters": (lambda: state_of(spread(400, 14, bsps=("2.90", "3.10"))),
                             ("INCONCLUSIVE", "too_few_clusters")),
        "se_undefined": (lambda: state_of(spread(400, 16, bsps=("3.00",))), ("INCONCLUSIVE", "se_undefined")),
        "insufficient": (lambda: state_of(spread(37, 16)), ("INCONCLUSIVE", "insufficient_eligible_sample")),
        "protocol_violation": (lambda: state_of(spread(400, 20), violations=[VIOLATION]),
                               ("INVALID", "protocol_violation")),
        "gate_failed": (lambda: state_of(spread(300, 20) + [synthetic("unmatched", 1000 + i, day=i % 20)
                                                            for i in range(100)]), ("INVALID", "gate_failed")),
        "journal PASS": (journal_case(full_journal, ("2.90", "2.88")), ("PASS", None)),
        "journal FAIL": (journal_case(full_journal, ("3.10", "3.12")), ("FAIL", None)),
        "journal insufficient": (journal_case(ledger_journal), ("INCONCLUSIVE", "insufficient_eligible_sample")),
        "journal protocol_violation": (journal_case(breach_journal), ("INVALID", "protocol_violation")),
    }


# ---------------------------------------------------------------------------------------------------------------
# Oracles: what each table must hold, computed here from the note's rules (spec sections 4.4 and 5).

def mean(values):
    values = list(values)
    return sum(values, Fraction(0)) / len(values) if values else None


def root9(value):
    """floor(sqrt(value) * 10**9) / 10**9, exact (spec 5.4)."""
    return Fraction(math.isqrt(value.numerator * 10 ** 18 // value.denominator), 10 ** 9)


def split_of(P, bsp, win):
    """The note's split (line 200), each term from its own formula: (net_payout, NCLV, k_outcome, r)."""
    price, base = Fraction(P), Fraction(bsp)
    net_payout = 1 + (price - 1) * (1 - CR)
    return net_payout, net_payout / base - 1, net_payout * (win - 1 / base), net_payout * win - 1


def band_oracle(limit_h):
    for name, low, high in NOTE_BANDS:
        if low <= limit_h <= high:
            return name
    return None


def winners_of(rec):
    return [(runner or {}).get("status") for runner in rec.bsp["closed_read"]["runners"].values()].count("WINNER")


def facts(rec):
    """The bet the note's rules make of one k_outcome record: exactly the shape of spec 4.4's bet."""
    win = 1 if rec.bsp["closed_read"]["runners"][str(rec.selection_id)]["status"] == "WINNER" else 0
    net_payout, nclv, k_outcome, r = split_of(rec.execution["P"], rec.bsp["bsp"], win)
    london = london_oracle(rec.off_ns)
    return {"ref": rec.ref, "market_id": rec.market_id, "selection_id": rec.selection_id, "off_ns": rec.off_ns,
            "utc_day": rec.utc_day, "win": win, "P": rec.execution["P"], "bsp": rec.bsp["bsp"],
            "net_payout": net_payout, "nclv": nclv, "k_outcome": k_outcome, "r": r,
            "limit_price_h": rec.record["limit_price_h"], "band": band_oracle(rec.record["limit_price_h"]),
            "size": rec.execution["size"], "ledger_part": rec.ledger["part"],
            "ledger_value_p": rec.ledger["value_p"], "london_day": london, "day_row": oracle_row(london)}


def expected_selection(state):
    """k_outcome_bets' result (N12 to N16): verdict()'s own bets, then exactly one WINNER in the CLOSED read."""
    split = pilot.analysis_population(state, now_ns=state.now_ns)
    base = [rec for rec in split["population"] if rec.measurement["class"] == "eligible"][:C.n_eligible]
    counts = [winners_of(rec) for rec in base]
    return {"source": "primary_sample" if split["n_reached"] else "analysis_population",
            "n_reached": split["n_reached"], "eligible": len(base),
            "excluded": {"dead_heat": sum(1 for n in counts if n >= 2), "no_winner": counts.count(0)},
            "not_in": {"pt4_records": len(split["apart"]), "surplus_records": len(split["surplus"])},
            "missing": {"bets": None, "T1": None, "T3": None},
            "bets": tuple(facts(rec) for rec, n in zip(base, counts) if n == 1)}


def expected_interval(rows):
    """One group's interval by contract p1's CR1 (pilot.cr1, K:85-100) on [(UTC date, x)] (spec 5.4)."""
    found = pilot.cr1(rows, min_clusters=C.min_clusters, alpha=C.alpha_one_sided)
    if found["reason"] == "too_few_clusters":
        return {"shown": False, "reason": "too few days"}
    if found["reason"] == "se_undefined":
        return {"shown": False, "reason": "se undefined"}
    half = root9(found["bound"])
    return {"shown": True, "low": found["m"] - half, "high": found["m"] + half, "half_width": half,
            "half_width_squared": found["bound"], "se2": found["se2"], "t": found["t"], "t_df": found["t_df"]}


def expected_group(bets, intervals=()):
    """group_figures' result: n, G (distinct UTC decision dates), the three unweighted means and the add-up."""
    means = {key: mean(bet[key] for bet in bets) for key in ("nclv", "k_outcome", "r")}
    row = {"n": len(bets), "G": len({bet["utc_day"] for bet in bets}), "mean_nclv": means["nclv"],
           "mean_k_outcome": means["k_outcome"], "mean_r": means["r"],
           "means_add_up": None if not bets else means["nclv"] + means["k_outcome"] == means["r"]}
    for measure in intervals:
        row["interval_" + measure] = expected_interval([(bet["utc_day"], bet[measure]) for bet in bets])
    return row


def expected_ledger(bets):
    """T1's ledger check (line 208), computed here."""
    residuals, outside, in_w, at_betfair = [], 0, 0, 0
    for bet in bets:
        if bet["ledger_part"] == "W":
            in_w += 1
            continue
        s = Fraction(bet["size"]) * 100
        if bet["win"] == 1 and bet["ledger_value_p"] < own_net_p(bet["size"], bet["P"]):
            at_betfair += 1
            continue
        residual = Fraction(bet["ledger_value_p"]) / s - bet["r"]
        residuals.append(residual)
        if not -2 / s < residual <= 0:
            outside += 1
    return {"checked": len(residuals), "mean_residual": mean(residuals), "outside_range": outside, "in_w": in_w,
            "s_winner_at_betfair_figure": at_betfair}


def expected_t1(bets):
    def row(group):
        found = expected_group(group, ("k_outcome",))
        values = [bet["k_outcome"] for bet in group]
        var = None
        if len(values) >= 2:
            var = sum(((x - found["mean_k_outcome"]) ** 2 for x in values), Fraction(0)) / (len(values) - 1)
        found.update(var_k_outcome=var, sd_k_outcome=None if var is None else root9(var),
                     ledger=expected_ledger(group))
        return found

    rows = {name: row([bet for bet in bets if bet["band"] == name]) for name, _, _ in NOTE_BANDS}
    rows["pooled"] = row(list(bets))
    return {"computed": True, "rows": rows, "no_band": sum(1 for bet in bets if bet["band"] is None)}


def expected_contrast(a, b):
    """A - B of one measure over [(UTC date, x)] with movement-v1's CR1 variance of a difference
    (registered_pass.contrasts_rule), written out directly; an interval only when each side has 15 dates."""
    n_a, n_b = len(a), len(b)
    days_a, days_b = {day for day, _ in a}, {day for day, _ in b}
    dates = days_a | days_b
    m_a, m_b = mean(x for _, x in a), mean(x for _, x in b)
    found = {"n": (n_a, n_b), "G": (len(days_a), len(days_b)), "G_either": len(dates),
             "estimate": None if not n_a or not n_b else m_a - m_b}
    if len(days_a) < C.min_clusters or len(days_b) < C.min_clusters:
        return dict(found, interval={"shown": False, "reason": "too few days"})
    g = len(dates)
    var = Fraction(g, g - 1) * sum(
        ((sum(((x - m_a) / n_a for d, x in a if d == day), Fraction(0))
          - sum(((x - m_b) / n_b for d, x in b if d == day), Fraction(0))) ** 2 for day in dates), Fraction(0))
    if var == 0:
        return dict(found, interval={"shown": False, "reason": "se undefined"})
    t = Fraction(pilot.student_t_quantile(0.975, g - 1))
    bound = t * t * var
    half = root9(bound)
    estimate = found["estimate"]
    return dict(found, interval={"shown": True, "low": estimate - half, "high": estimate + half, "half_width": half,
                                 "half_width_squared": bound, "se2": var, "t": t, "t_df": g - 1})


def pairs(bets, measure, rows):
    return [(bet["utc_day"], bet[measure]) for bet in bets if bet["day_row"] in rows]


def expected_t3(bets):
    contrasts, marked = {"main": {}, "placebo": {}}, {}
    for measure in ("nclv", "k_outcome"):
        other = pairs(bets, measure, ("OTHER_WEEKDAYS_NOT_H",))
        contrasts["main"][measure] = expected_contrast(pairs(bets, measure, H_ROWS), other)
        contrasts["placebo"][measure] = expected_contrast(pairs(bets, measure, ("WEDNESDAY_NOT_H",)), other)
        main, placebo = contrasts["main"][measure]["estimate"], contrasts["placebo"][measure]["estimate"]
        marked[measure] = None if main is None or placebo is None else 2 * abs(placebo) >= abs(main)
    return {"computed": True,
            "rows": {name: expected_group([bet for bet in bets if bet["day_row"] == name], ("nclv", "k_outcome"))
                     for name in NOTE_T3_ROWS},
            "contrasts": contrasts, "placebo_marked": marked, "unconfirmed_festivals": UNCONFIRMED,
            "bets_outside_registered_days": sum(1 for bet in bets if not NOTE_FIRST_DAY <= bet["london_day"]
                                                <= NOTE_LAST_DAY)}


# ---------------------------------------------------------------------------------------------------------------
# Calls and the rendering.

def tables_of(state, t2=T2):
    return pilot_tables.report_tables(state, contract=C, now_ns=state.now_ns, t2=t2)


def selection_of(state):
    return pilot_tables.k_outcome_bets(state, contract=C, now_ns=state.now_ns)


def lines_of(state, t2=T2):
    return pilot_tables.render_tables(tables_of(state, t2))


def signed(value, places=4):
    """The spec's display rule (4.5): round(value * 10**places) on the Fraction, sign always shown."""
    scaled = round(value * 10 ** places)
    digits = str(abs(scaled)).rjust(places + 1, "0")
    return ("-" if scaled < 0 else "+") + digits[:-places] + "." + digits[-places:]


def leaves(value):
    """Every key and value in a nested result."""
    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from leaves(item)


def dict_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from dict_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from dict_keys(item)


def without_ledger(t1):
    found = copy.deepcopy(t1)
    for row in found["rows"].values():
        row.pop("ledger")
    return found


def contrast_lines(lines, label):
    """{measure word: line} for the rendered contrast rows of one label."""
    return {line[len(label):].split()[0]: line for line in lines if line.startswith(label)}


class TablesCase(unittest.TestCase):
    """Shared checks; no tests of its own."""

    def assert_tables(self, state, t2=T2):
        """k_outcome_bets and report_tables equal the oracles, whole."""
        expected = expected_selection(state)
        self.assertEqual(selection_of(state), expected)
        tables = tables_of(state, t2)
        head = {key: value for key, value in expected.items() if key != "bets"}
        head["k_outcome_bets"] = len(expected["bets"])
        self.assertEqual(tables["bets"], head)
        self.assertEqual(tables["T1"], expected_t1(expected["bets"]))
        self.assertEqual(tables["T2"], {"computed": False, "reason": NOTE_T2_STATES[t2], "state": t2})
        self.assertEqual(tables["T3"], expected_t3(expected["bets"]))
        return tables

    def assert_refused(self, code, function, *args, **kw):
        with self.assertRaises(PilotError) as caught:
            function(*args, **kw)
        self.assertEqual(caught.exception.code, code)


# ---------------------------------------------------------------------------------------------------------------

class Boundaries(unittest.TestCase):
    """The module is pure and nothing shows its figures early (note line 196: «The interim allowlist above is
    unchanged: none of their figures appears in an interim view or in Yu's private risk view.»)."""

    def test_module_is_classified_allowed(self):
        """N34 (line 211): T2's reader «lives outside pilot.py, which stays pure»; the tables' module is pure too,
        so it is classified with the allowed offline modules."""
        self.assertIn("pilot_tables", bb.ALLOWED_BETFAIR)
        self.assertNotIn("pilot_tables", bb.FORBIDDEN_BETFAIR)

    def test_closure_obeys_the_offline_rule(self):
        """N34: the module's closure is pilot's and itself, and obeys the backtest import rule as pilot's does."""
        closure = set(bb.REAL.closure([MODULE]))
        self.assertEqual(closure, {bb.TOP, bb.BETFAIR, bb.PILOT, f"{bb.BETFAIR}.ticks", f"{bb.BETFAIR}.constants",
                                   MODULE})
        found = bb.REAL.violations([MODULE], *bb.backtest_rules(bb.BETFAIR, bb.FORBIDDEN_BETFAIR,
                                                                bb.shadowing_names(bb.REAL, [MODULE])))
        for module in sorted(closure):
            found += [(module, site, "binds an internal package object outside the backtest package")
                      for site in bb.REAL.packages[module] if not bb.under(site.partition(": ")[2], bb.BACKTEST)]
        found += bb.stdlib_shadow_violations(bb.REAL, [MODULE])
        self.assertEqual(found, [], bb.describe(found))

    def test_imports_pinned(self):
        """N34, spec 4.1: datetime, Decimal, Fraction, math and pilot, nothing else, no alias."""
        imports = sorted((module, level, tuple(sorted(names)))
                         for module, level, names in boundaries.imported_modules(TABLES))
        self.assertEqual(imports, sorted([("datetime", 0, ("datetime", "timedelta", "timezone")),
                                          ("decimal", 0, ("Decimal",)), ("fractions", 0, ("Fraction",)),
                                          ("math", 0, ("math",)), ("", 1, ("pilot",))]))
        self.assertTrue({module for module, level, _ in imports if not level} <= bb.BACKTEST_STDLIB)
        aliases = [alias.asname for node in ast.walk(boundaries.tree(TABLES))
                   if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names if alias.asname]
        self.assertEqual(aliases, [])

    def test_no_clock_file_or_print_calls(self):
        """N5 (line 196): «computed once, after the data lock, on the lab Mac, in the terminal»; N8: «Publication
        follows the rule above.» No clock, file, environment, random source, print or input."""
        tree = boundaries.tree(TABLES)
        banned = {"open", "write", "write_text", "write_bytes", "read_text", "read_bytes", "mkdir", "touch", "unlink",
                  "rename", "replace", "chmod", "now", "utcnow", "today", "time", "time_ns", "monotonic",
                  "monotonic_ns", "perf_counter", "sleep", "getenv", "urandom", "random", "randint", "print", "input"}
        called = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and boundaries.called_name(n) in banned]
        self.assertEqual(called, [])
        self.assertEqual([n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ("open", "environ")],
                         [])
        self.assertEqual([n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "environ"], [])

    def test_text_bans(self):
        """N8: the strings pilot.py is held to (tests/test_betfair_pilot.py test_text_bans) hold for the module."""
        text = TABLES.read_text(encoding="utf-8")
        for banned in ("frozen_experiment", "allowed_experiments", "_MINT_TOKEN", "settle_from_cleared",
                       "close_from_cleared", "cleared_move", "pnl_basis_mismatch", "_pnl_mismatch",
                       "stopped_for_kill", "timestamp_ms", "temporal", *bb.LIVE_STATE_TEXT):
            self.assertNotIn(banned, text)
        for banned in boundaries.ORDER_LITERALS + boundaries.NEVER_ANYWHERE + boundaries.REMOVED_ORDER_LITERALS:
            self.assertNotIn(banned.lower(), text.lower())

    def test_no_bare_K(self):
        """N11 (line 197): «In the code and its comments the split term below is k_outcome, never a bare K, because
        pilot.py cites contract p1 as "K:<line>"». Not in the text, not in any rendered line."""
        text = TABLES.read_text(encoding="utf-8")
        found = [(number, line) for number, line in enumerate(text.splitlines(), 1) if BARE_K.search(line)]
        self.assertEqual(found, [])
        self.assertIn("k_outcome", text)
        missing = state_of([dataclasses.replace(k_record(0, day=0), utc_day=None)])
        for state in (mixed_state(), state_of(spread(37, 16)), missing):
            for t2 in NOTE_T2_STATES:
                for line in lines_of(state, t2):
                    self.assertIsNone(BARE_K.search(line), line)

    def test_pilot_attributes_used(self):
        """N3 (line 195): «They never decide.» N10: «a test pins that verdict() returns the same». The module reads
        only these attributes of pilot and never names the deciding or ledger functions or a record's frontier."""
        tree = boundaries.tree(TABLES)
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "pilot"}
        allowed = {"PilotError", "MAX_NS", "RESULTS", "analysis_population", "data_lock_reached", "nclv", "cr1",
                   "s_winner_value"}
        self.assertEqual(used - allowed, set())
        self.assertNotIn("verdict", used)
        self.assertNotIn("fold", used)
        named = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                 | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})
        never = {"verdict", "fold", "equity", "ledger_value", "classify", "capacity_and_pause", "pause_levels_hit"}
        self.assertEqual(named & never, set())
        self.assertNotIn("frontier", named | {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                                              and isinstance(n.value, str)})

    def test_pilot_does_not_import_the_tables(self):
        """N10 (line 197): «It adds output only». pilot does not reach the tables."""
        self.assertNotIn(MODULE, bb.REAL.internal[bb.PILOT])
        self.assertNotIn(MODULE, bb.REAL.closure([bb.PILOT]))

    def test_nothing_imports_the_tables_yet(self):
        """N6 (line 196): «none of their figures appears in an interim view or in Yu's private risk view». No module
        imports pilot_tables but IMPORTERS (by the import graph, which also sees two-dot imports), and no script."""
        found = tuple(sorted(m for m in bb.REAL.files if MODULE in bb.REAL.internal[m]))
        self.assertEqual(found, IMPORTERS)
        scripts = sorted((tp.REPO / "scripts").glob("*.py"))
        self.assertEqual([p.name for p in scripts if boundaries.imports_betfair_module(p, "pilot_tables")], [])

    def test_registered_numbers_are_contract_p1s(self):
        """N17 (line 200): «c = 0.02 (p1-fav-t5.json:29)»; N27 (line 207): «a two-sided 95% interval ... shown only
        when G is at least 15 (min_clusters, :94)». The tables read these from contract p1, which gives them."""
        self.assertEqual(1 - 2 * C.alpha_one_sided, Fraction(19, 20))
        self.assertEqual(C.min_clusters, 15)
        self.assertEqual(C.commission_rate, Fraction(1, 50))
        self.assertEqual(C.n_eligible, 400)


class Selection(TablesCase):
    """The bets (note lines 198-199): «Every table uses the same bets»."""

    def test_primary_sample_is_verdicts_400(self):
        """N12 (line 198): «contract p1's primary sample (its first 400 eligible bets, p1-fav-t5.json:245) ...
        which the tables then say»."""
        records = spread(430, 20)
        state = state_of(records)
        found = selection_of(state)
        split = pilot.analysis_population(state, now_ns=state.now_ns)
        verdicts = [rec for rec in split["population"] if rec.measurement["class"] == "eligible"][:C.n_eligible]
        self.assertEqual([bet["ref"] for bet in found["bets"]], [rec.ref for rec in verdicts])
        self.assertEqual(len(found["bets"]), 400)
        self.assertEqual((found["source"], found["n_reached"], found["eligible"]), ("primary_sample", True, 400))
        self.assertEqual(found["not_in"], {"pt4_records": 0, "surplus_records": 30})
        self.assertEqual(pilot.verdict(state, contract=C, now_ns=state.now_ns)["population_size"], 400)
        self.assert_tables(state)
        lines = lines_of(state)
        self.assertEqual(lines[0], TITLE)
        self.assertTrue(lines[1].startswith("Bets:"), lines[1])
        self.assertIn("primary sample", lines[1])

    def test_n_not_reached_is_said(self):
        """N12 (line 198): «or, if N is not reached, every eligible bet of the analysis population (:241), which the
        tables then say»."""
        records = []
        for i in range(37):
            records.append(varied(i, i % 16))
            if i % 4 == 1:
                records.append(synthetic("unmatched", 500 + i, day=i % 16))
        state = state_of(records)
        found = selection_of(state)
        self.assertEqual((found["source"], found["n_reached"], found["eligible"], len(found["bets"])),
                         ("analysis_population", False, 37, 37))
        self.assert_tables(state)
        lines = lines_of(state)
        self.assertEqual(lines[0], TITLE)
        self.assertEqual(lines[1], N_NOT_REACHED.format(37))

    def test_pt4_and_surplus_never_in(self):
        """N14 (line 198): «Surplus bets, PT4 records (:246-249) and the decision-price series are never in them.»"""
        sample = spread(400, 20, first=100)
        extreme = dict(P="50.00", bsp="1.01", ours="WINNER", limit_h=150)
        pt4 = [k_record(i, day=0, pt4=True, **extreme) for i in range(5)]
        surplus = [k_record(600 + i, day=19, **extreme) for i in range(30)]
        plain, crowded = state_of(sample), state_of(pt4 + sample + surplus)
        a, b = tables_of(plain), tables_of(crowded)
        self.assertEqual(a["T1"], b["T1"])
        self.assertEqual(a["T3"], b["T3"])
        self.assertEqual(a["T2"], b["T2"])
        self.assertEqual({k: v for k, v in a["bets"].items() if k != "not_in"},
                         {k: v for k, v in b["bets"].items() if k != "not_in"})
        self.assertEqual((a["bets"]["not_in"], b["bets"]["not_in"]),
                         ({"pt4_records": 0, "surplus_records": 0}, {"pt4_records": 5, "surplus_records": 30}))
        self.assertEqual(selection_of(plain)["bets"], selection_of(crowded)["bets"])
        self.assert_tables(crowded)

    def test_winner_count_rule_and_reasons(self):
        """N12 (line 198): «only the bets whose frozen BSP's CLOSED read (§11) shows exactly one WINNER»; N13: «The
        rest are counted by reason: two or more WINNERs (a dead heat, which does not pay 0 or 1); no WINNER.»"""
        records = [k_record(0, day=0, ours="LOSER"),                       # one WINNER, not ours: in, win 0
                   k_record(1, day=1, ours="WINNER"),                      # ours the one WINNER: in, win 1
                   k_record(2, day=2, ours="WINNER", other_winners=1),     # dead heat, ours among them
                   k_record(3, day=3, ours="LOSER", other_winners=2),      # dead heat without ours
                   k_record(4, day=4, ours="WINNER", other_winners=2),     # three WINNERs
                   k_record(5, day=5, ours="LOSER", other_winners=0)]      # no WINNER
        state = state_of(records)
        found = selection_of(state)
        self.assertEqual([(bet["ref"], bet["win"]) for bet in found["bets"]], [("p1tab-0", 0), ("p1tab-1", 1)])
        self.assertEqual(found["excluded"], {"dead_heat": 3, "no_winner": 1})
        self.assertEqual(found["eligible"], len(found["bets"]) + 3 + 1)
        self.assert_tables(state)

    def test_only_the_winner_count_is_added(self):
        """N15, N16 (line 199): «the K rule adds only the WINNER count. It does not need every runner's status to
        be final, and it does not re-check numberOfWinners»."""
        records = [k_record(0, day=0, extra_runners={str(SEL + 50): {"status": None, "actual_sp": None}}),
                   k_record(1, day=1, extra_runners={str(SEL + 50): {"status": "ACTIVE", "actual_sp": None}}),
                   k_record(2, day=2, extra_runners={str(SEL + 50): {"status": "REMOVED", "actual_sp": None}}),
                   k_record(3, day=3, ledger=w_ledger()),
                   k_record(4, day=4, ours="WINNER", ledger=s_ledger(own_net_p("2.00", "3.00")))]
        self.assertTrue(all(rec.l_rows == () for rec in records))
        state = state_of(records)
        found = selection_of(state)
        self.assertEqual([bet["ref"] for bet in found["bets"]], [rec.ref for rec in records])
        self.assertEqual(found["excluded"], {"dead_heat": 0, "no_winner": 0})
        self.assert_tables(state)

    def test_final_price_not_limit_price(self):
        """N14 (line 198) and N17 (line 200): «For a K bet with final price P (§11)»; the limit price only sets the
        band (line 203)."""
        record = k_record(0, day=0, limit_h=300, P="3.444", bsp="3.2", ours="WINNER")
        state = state_of([record])
        (bet,) = selection_of(state)["bets"]
        self.assertEqual(bet["nclv"], pilot.nclv(Decimal("3.444"), Decimal("3.2"), c=C.commission_rate))
        self.assertEqual(bet["nclv"], Fraction(2439, 40000))
        self.assertNotEqual(bet["nclv"], pilot.decision_price_nclv(300, Decimal("3.2"), c=C.commission_rate))
        self.assertEqual(bet["P"], Decimal("3.444"))
        moved = state_of([dataclasses.replace(record, record=dict(record.record, limit_price_h=350))])
        a, b = tables_of(state), tables_of(moved)
        self.assertEqual(a, b)
        self.assert_tables(moved)

    def test_missing_shared_input_stops_both(self):
        """N7 (line 196): «one whose input is missing is shown as "not computed: <reason>"». An input every table
        needs stops T1 and T3; T2 keeps its own reason."""
        records = [varied(i, i % 5) for i in range(5)]
        rec = records[2]
        others = {key: value for key, value in rec.bsp["closed_read"]["runners"].items() if key != str(SEL)}
        broken = {"closed_read": dataclasses.replace(rec, bsp=dict(rec.bsp, closed_read=None)),
                  "our_result": with_closed_runners(rec, others),
                  "final_price_or_bsp": dataclasses.replace(rec, execution=dict(rec.execution, P=None)),
                  "utc_day": dataclasses.replace(rec, utc_day=None)}
        for name, bad in broken.items():
            with self.subTest(input=name):
                state = state_of(records[:2] + [bad] + records[3:])
                found = selection_of(state)
                self.assertEqual(found["missing"]["bets"], name)
                self.assertEqual(found["bets"], ())
                self.assertEqual(found["excluded"], {"dead_heat": None, "no_winner": None})
                for t2 in NOTE_T2_STATES:
                    tables = tables_of(state, t2)
                    reason = {"computed": False, "reason": "input missing: " + name}
                    self.assertEqual((tables["T1"], tables["T3"]), (reason, reason))
                    self.assertEqual(tables["T2"], {"computed": False, "reason": NOTE_T2_STATES[t2], "state": t2})
                    self.assertIsNone(tables["bets"]["k_outcome_bets"])
                    lines = pilot_tables.render_tables(tables)
                    for line in (NOT_FORMED.format(name), T1_NOT.format(name), T3_NOT.format(name),
                                 T2_LINES[t2][0]):
                        self.assertIn(line, lines)

    def test_missing_table_input_stops_only_that_table(self):
        """N7 (line 196): «Each table is computed and shown whatever it shows; one whose input is missing is shown
        as "not computed: <reason>"». An input only one table reads stops that table only."""
        records = [varied(i, i % 5) for i in range(5)]
        rec = records[2]
        whole = tables_of(state_of(records))
        t1_inputs = {"limit_price": dataclasses.replace(rec, record={k: v for k, v in rec.record.items()
                                                                      if k != "limit_price_h"}),
                     "final_size": dataclasses.replace(rec, execution=dict(rec.execution, size=None)),
                     "ledger": dataclasses.replace(rec, ledger=dict(rec.ledger, part=None))}
        t1_fields = ("limit_price_h", "band", "size", "ledger_part", "ledger_value_p")
        for name, bad in t1_inputs.items():
            with self.subTest(input=name):
                state = state_of(records[:2] + [bad] + records[3:])
                tables = tables_of(state)
                self.assertEqual(tables["T1"], {"computed": False, "reason": "input missing: " + name})
                self.assertEqual(tables["T3"], whole["T3"])
                self.assertIn(T1_NOT.format(name), pilot_tables.render_tables(tables))
                found = selection_of(state)
                self.assertEqual((found["missing"]["bets"], found["missing"]["T1"], found["missing"]["T3"]),
                                 (None, name, None))
                self.assertEqual([found["bets"][2][field] for field in t1_fields], [None] * len(t1_fields))
        bad = dataclasses.replace(rec, off_ns=None)
        state = state_of(records[:2] + [bad] + records[3:])
        tables = tables_of(state)
        self.assertEqual(tables["T3"], {"computed": False, "reason": "input missing: off"})
        self.assertEqual(tables["T1"], whole["T1"])
        self.assertIn(T3_NOT.format("off"), pilot_tables.render_tables(tables))
        found = selection_of(state)
        self.assertEqual((found["missing"]["T1"], found["missing"]["T3"]), (None, "off"))
        self.assertEqual((found["bets"][2]["london_day"], found["bets"][2]["day_row"]), (None, None))
        # The first missing input in decision order, and the inputs of one bet in the spec's order (5.2).
        first = dataclasses.replace(records[1], execution=dict(records[1].execution, size=None))
        later = t1_inputs["limit_price"]
        self.assertEqual(selection_of(state_of([records[0], first, later] + records[3:]))["missing"]["T1"],
                         "final_size")
        both = dataclasses.replace(later, execution=dict(later.execution, size=None))
        self.assertEqual(selection_of(state_of(records[:2] + [both] + records[3:]))["missing"]["T1"], "limit_price")

    def test_refused_before_the_data_lock(self):
        """N5 (line 196): «They are computed once, after the data lock». Refused with verdict()'s codes, in its
        order, before anything else."""
        records = spread(37, 16)
        placing_open = state_of(records, ended=False)
        rec = records[3]
        pending = state_of(records[:3] + [dataclasses.replace(
            rec, measurement=dict(rec.measurement, final=False, final_at_ns=None))] + records[4:])
        for label, state in (("placing open", placing_open), ("record not final", pending)):
            with self.subTest(case=label):
                self.assertFalse(pilot.data_lock_reached(state, now_ns=state.now_ns))
                for function in (lambda s: tables_of(s), lambda s: selection_of(s),
                                 lambda s: tables_of(s, "not a state"),
                                 lambda s: pilot.verdict(s, contract=C, now_ns=s.now_ns)):
                    self.assert_refused("data_lock_not_reached", function, state)
        other = dataclasses.replace(C, min_clusters=16)
        for state in (state_of(records), placing_open):
            for function in (pilot_tables.k_outcome_bets, pilot.verdict):
                self.assert_refused("contract_mismatch", function, state, contract=other, now_ns=state.now_ns)
            self.assert_refused("contract_mismatch", pilot_tables.report_tables, state, contract=other,
                                now_ns=state.now_ns, t2="not a state")


class Split(TablesCase):
    """The split (note line 200): «Then NCLV = G / BSP − 1, contract p1's metric (:154); K = G × (win − 1 / BSP);
    and r, the net return per £1 matched, is (P − 1)(1 − c) for a win and −1 for a loss, that is G × win − 1.»"""

    def test_identity_holds_exactly_per_bet(self):
        """N19 (line 200): «So NCLV + K = (G / BSP − 1) + (G × win − G / BSP) = G × win − 1 = r, exactly, for
        every bet»."""
        rng = random.Random(1950)
        for seed in range(max(tp.SEEDS // 8, 4)):
            records = [k_record(0, day=0, P="3.444", bsp="3.2", ours="WINNER")]
            for i in range(1, 30):
                low, high, scale = rng.choice(((11, 100, 10), (101, 1000, 100), (1010, 10000, 1000)))
                P = str(Decimal(rng.randrange(low, high + 1)) / Decimal(scale))     # 1.1 to 10, up to 3 places
                bsp = str(Decimal(rng.randrange(101, 2001)) / Decimal(100))
                records.append(k_record(i, day=i % 9, P=P, bsp=bsp, ours=rng.choice(("WINNER", "LOSER"))))
            with self.subTest(seed=seed):
                found = selection_of(state_of(records))
                self.assertEqual(len(found["bets"]), len(records))
                for rec, bet in zip(records, found["bets"]):
                    P, bsp, win = rec.execution["P"], rec.bsp["bsp"], bet["win"]
                    self.assertEqual(win, 1 if rec.bsp["closed_read"]["runners"][str(SEL)]["status"] == "WINNER"
                                     else 0)
                    for key in ("net_payout", "nclv", "k_outcome", "r"):
                        self.assertIs(type(bet[key]), Fraction)
                    self.assertEqual(bet["nclv"] + bet["k_outcome"], bet["r"])
                    self.assertEqual(bet["net_payout"] / Fraction(bsp) - 1, bet["nclv"])
                    self.assertEqual(bet["nclv"], pilot.nclv(P, bsp, c=C.commission_rate))
                    self.assertEqual(bet["net_payout"], 1 + (Fraction(P) - 1) * (1 - CR))
                    self.assertEqual(bet["k_outcome"], bet["net_payout"] * (win - 1 / Fraction(bsp)))
                    self.assertEqual(bet["r"], (Fraction(P) - 1) * (1 - CR) if win else -1)

    def test_worked_values(self):
        """N17, N18 (line 200): the spec's worked values (5.1), c = 0.02."""
        rows = (("3.00", "3.10", "LOSER", 150, (Fraction(74, 25), Fraction(-7, 155), Fraction(-148, 155), -1)),
                ("3.00", "3.10", "WINNER", 200,
                 (Fraction(74, 25), Fraction(-7, 155), Fraction(1554, 775), Fraction(49, 25))),
                ("3.444", "3.2", "WINNER", 300, (Fraction(42439, 12500), Fraction(2439, 40000),
                                                 Fraction(466829, 200000), Fraction(29939, 12500))),
                ("1.50", "1.50", "LOSER", 150, (Fraction(149, 100), Fraction(-1, 150), Fraction(-149, 150), -1)),
                ("2.00", "2.00", "WINNER", 200,
                 (Fraction(99, 50), Fraction(-1, 100), Fraction(99, 100), Fraction(49, 50))))
        records = [k_record(i, day=i, P=P, bsp=bsp, ours=ours, limit_h=limit_h)
                   for i, (P, bsp, ours, limit_h, _) in enumerate(rows)]
        found = selection_of(state_of(records))
        self.assertEqual([(bet["net_payout"], bet["nclv"], bet["k_outcome"], bet["r"]) for bet in found["bets"]],
                         [values for *_, values in rows])

    def test_means_add_up(self):
        """N19 (line 200): «and so for any mean taken with the same weights over the same bets»; N25 (line 205):
        «with mean NCLV + mean K = mean r shown»."""
        state = mixed_state()
        tables = self.assert_tables(state)
        rows = list(tables["T1"]["rows"].values()) + list(tables["T3"]["rows"].values())
        for row in rows:
            if row["n"]:
                self.assertEqual(row["mean_nclv"] + row["mean_k_outcome"], row["mean_r"])
                self.assertIs(row["means_add_up"], True)
            else:
                self.assertEqual((row["mean_nclv"], row["mean_k_outcome"], row["mean_r"], row["means_add_up"]),
                                 (None, None, None, None))
        lines = lines_of(state)
        pooled = tables["T1"]["rows"]["pooled"]
        texts = [signed(pooled[key]) for key in ("mean_nclv", "mean_k_outcome", "mean_r")]
        self.assertTrue(any(all(text in line for text in texts) for line in lines if line.startswith("pooled")),
                        texts)
        for name, row in tables["T3"]["rows"].items():
            texts = [signed(row[key]) for key in ("mean_nclv", "mean_k_outcome", "mean_r")]
            self.assertTrue(any(all(text in line for text in texts) for line in lines
                                if line.startswith(NOTE_T3_ROW_LABELS[name])), (name, texts))

    def test_long_decimals_stay_exact(self):
        """Section 2 of the spec: «No arithmetic on a Decimal» (journaled numbers may hold 40 + 40 digits). A size
        and a price with 35 significant digits give the exact residual and split."""
        size = "1." + "2345678901" * 3 + "2345"
        price = "2." + "0" * 33 + "1"
        self.assertEqual((len(size) - 1, len(price) - 1), (35, 35))       # significant digits
        loser = k_record(0, day=0, limit_h=300, size=size, P=price, bsp="2.10", ours="LOSER",
                         ledger=s_ledger(pilot.loss_pence(Decimal(size))))
        winner = k_record(1, day=1, limit_h=150, size=size, P=price, bsp="1.90", ours="WINNER",
                          ledger=s_ledger(own_net_p(size, price)))
        state = state_of([loser, winner])
        tables = self.assert_tables(state)
        s = Fraction(Decimal(size)) * 100
        r_win = (Fraction(Decimal(price)) - 1) * (1 - CR)
        residual_loser = Fraction(-math.ceil(s)) / s + 1
        residual_winner = Fraction(own_net_p(size, price)) / s - r_win
        rows = tables["T1"]["rows"]
        self.assertEqual(rows["B4"]["ledger"]["mean_residual"], residual_loser)
        self.assertEqual(rows["B1"]["ledger"]["mean_residual"], residual_winner)
        self.assertEqual(rows["pooled"]["ledger"]["mean_residual"], (residual_loser + residual_winner) / 2)
        self.assertEqual(rows["pooled"]["ledger"]["outside_range"], 0)
        bets = selection_of(state)["bets"]
        self.assertEqual(bets[1]["r"], r_win)
        self.assertEqual(bets[1]["nclv"], (1 + (Fraction(Decimal(price)) - 1) * (1 - CR)) / Fraction(19, 10) - 1)


class T1(TablesCase):
    """T1 (note lines 203-209): «A bet's band is set by its decision limit price, known before any outcome»."""

    def test_band_edges(self):
        """N22 (line 203): «B1 from 1.50 to 1.99, B2 from 2.00 to 2.48, B3 from 2.50 to 2.98, B4 from 3.00 to 4.00,
        bounds inclusive»."""
        for price, name in ((150, "B1"), (199, "B1"), (200, "B2"), (248, "B2"), (250, "B3"), (298, "B3"),
                            (300, "B4"), (400, "B4"), (149, None), (249, None), (299, None), (401, None),
                            (405, None)):
            with self.subTest(price=price):
                self.assertEqual(pilot_tables.price_band(price), name)
        ladder = [p for p in ticks.LADDER_CLASSIC if 150 <= p <= 400]
        bands = [pilot_tables.price_band(p) for p in ladder]
        self.assertEqual([bands.count(name) for name, _, _ in NOTE_BANDS], [50, 25, 25, 21])
        self.assertEqual(len(bands), 121)
        for price in ladder:
            self.assertEqual(sum(1 for _, low, high in pilot_tables.PRICE_BANDS_H if low <= price <= high), 1)

    def test_bands_are_movement_v1s(self):
        """N22 (line 203): «the bands of the price-movement study movement-v1 (research/studies/movement-v1.json
        `strata.price_bands_h`, PR #50)»; N2 (line 194): «Their bets, bands, cells, day types and dates are fixed
        from the first PLACE record on.»"""
        raw = MOVEMENT.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), MOVEMENT_SHA256)
        self.assertEqual([list(band) for band in pilot_tables.PRICE_BANDS_H],
                         json.loads(raw)["strata"]["price_bands_h"])
        self.assertEqual(pilot_tables.PRICE_BANDS_H, NOTE_BANDS)
        self.assertIn("B1 from 1.50 to 1.99, B2 from 2.00 to 2.48, B3 from 2.50 to 2.98, B4 from 3.00 to 4.00, "
                      "bounds inclusive", NOTE.read_text(encoding="utf-8"))

    def test_n_G_means_sd(self):
        """N24 (line 204): «n, and G, the number of UTC decision dates»; N25: «mean NCLV, mean K and mean r,
        unweighted»; N26 (line 206): «the sample SD of K»."""
        # k_outcome 1, -1, 1/2, -1/2 in B1 (two on one UTC date), and one bet alone in B4. P 99/49 gives a net
        # payout of 2 and P 74/49 one of 3/2 (c = 1/50); P is a Fraction here, as in synthetic().
        price_two, price_three_halves = 1 + Fraction(50, 49), 1 + Fraction(25, 49)
        records = [k_record(0, day=0, limit_h=150, P=price_two, bsp=Fraction(2), ours="WINNER"),
                   k_record(1, day=0, limit_h=150, P=price_two, bsp=Fraction(2), ours="LOSER"),
                   k_record(2, day=1, limit_h=150, P=price_three_halves, bsp=Fraction(3, 2), ours="WINNER"),
                   k_record(3, day=2, limit_h=150, P=price_three_halves, bsp=Fraction(3), ours="LOSER"),
                   k_record(4, day=3, limit_h=300)]
        state = state_of(records)
        tables = self.assert_tables(state)
        self.assertEqual([bet["k_outcome"] for bet in selection_of(state)["bets"]][:4],
                         [1, -1, Fraction(1, 2), Fraction(-1, 2)])
        b1, b4 = tables["T1"]["rows"]["B1"], tables["T1"]["rows"]["B4"]
        self.assertEqual((b1["n"], b1["G"]), (4, 3))
        self.assertEqual((b1["mean_nclv"], b1["mean_k_outcome"], b1["mean_r"]),
                         (Fraction(-1, 8), Fraction(0), Fraction(-1, 8)))
        self.assertEqual(b1["var_k_outcome"], Fraction(5, 6))
        self.assertEqual(b1["sd_k_outcome"], Fraction(912870929, 10 ** 9))
        self.assertEqual((b4["n"], b4["G"], b4["var_k_outcome"], b4["sd_k_outcome"]), (1, 1, None, None))
        for name in ("B2", "B3"):
            row = tables["T1"]["rows"][name]
            self.assertEqual((row["n"], row["G"], row["var_k_outcome"], row["sd_k_outcome"]), (0, 0, None, None))

    def test_pooled_is_every_bet(self):
        """N23 (line 203): «Per band and pooled»; N25 (line 205): «the pooled mean NCLV is over the K bets, so it
        can differ from contract p1's primary mean»."""
        records = spread(400, 20)
        records[7] = k_record(7, day=7, bsp="2.50", ours="WINNER", other_winners=1)        # a dead heat
        state = state_of(records)
        tables = self.assert_tables(state)
        rows = tables["T1"]["rows"]
        self.assertEqual(rows["pooled"]["n"], sum(rows[name]["n"] for name, _, _ in NOTE_BANDS)
                         + tables["T1"]["no_band"])
        self.assertEqual(rows["pooled"]["n"], 399)
        found = pilot.verdict(state, contract=C, now_ns=state.now_ns)
        self.assertEqual(found["result"], "PASS")
        self.assertNotEqual(rows["pooled"]["mean_nclv"], found["inference"]["m"])
        self.assertEqual(rows["pooled"]["mean_nclv"], mean(bet["nclv"] for bet in selection_of(state)["bets"]))
        self.assertIn(POOLED_SENTENCE, lines_of(state))

    def test_too_few_days_below_15(self):
        """N27 (line 207): «a two-sided 95% interval for mean K by contract p1's CR1 with t(G − 1) (:85-100), shown
        only when G is at least 15 (min_clusters, :94), else "too few days"»."""
        records = [varied(i, i // 2) for i in range(28)]                                  # 14 dates
        records = [dataclasses.replace(rec, record=dict(rec.record, limit_price_h=150)) for rec in records]
        records += [dataclasses.replace(rec, record=dict(rec.record, limit_price_h=200))
                    for rec in (varied(100 + i, i // 2) for i in range(30))]              # 15 dates
        state = state_of(records)
        tables = self.assert_tables(state)
        rows = tables["T1"]["rows"]
        self.assertEqual((rows["B1"]["G"], rows["B2"]["G"], rows["B3"]["n"]), (14, 15, 0))
        self.assertEqual(rows["B1"]["interval_k_outcome"], {"shown": False, "reason": "too few days"})
        self.assertEqual(rows["B3"]["interval_k_outcome"], {"shown": False, "reason": "too few days"})
        shown = rows["B2"]["interval_k_outcome"]
        self.assertEqual(set(shown), {"shown", "low", "high", "half_width", "half_width_squared", "se2", "t", "t_df"})
        self.assertIs(shown["shown"], True)
        b2 = [bet for bet in selection_of(state)["bets"] if bet["band"] == "B2"]
        found = pilot.cr1([(bet["utc_day"], bet["k_outcome"]) for bet in b2], min_clusters=15,
                          alpha=Fraction(1, 40))
        self.assertEqual((shown["t"], shown["se2"], shown["half_width_squared"], shown["t_df"]),
                         (found["t"], found["se2"], found["bound"], 14))
        self.assertEqual((shown["low"], shown["high"]),
                         (found["m"] - shown["half_width"], found["m"] + shown["half_width"]))
        self.assertLessEqual(shown["half_width"] ** 2, found["bound"])
        self.assertLess(found["bound"], (shown["half_width"] + Fraction(1, 10 ** 9)) ** 2)
        lines = lines_of(state)
        self.assertTrue(any("too few days" in line for line in lines if line.startswith("B1")))
        self.assertTrue(any("too few days" in line for line in lines if line.startswith("B3")))

    def test_interval_is_not_a_test(self):
        """N27 (line 207): «it is descriptive and tests nothing»; N20 (line 201): «K is part of profit, which is
        reported at the end and never decides»; N21 (line 202): «Over 400 bets, K is a guard, never a verdict.»"""
        for label, (build, expected) in verdict_cases().items():
            with self.subTest(case=label):
                state = build()
                found = pilot.verdict(state, contract=C, now_ns=state.now_ns)
                self.assertEqual((found["result"], found["reason"]), expected)
                for t2 in NOTE_T2_STATES:
                    tables = tables_of(state, t2)
                    keys = set(dict_keys(tables)) | set(dict_keys(selection_of(state)))
                    self.assertEqual(keys & {"result", "near_tie", "t_p", "p_two_sided"}, set())
                    for line in pilot_tables.render_tables(tables):
                        for word in ("PASS", "FAIL", "INCONCLUSIVE", "INVALID", "Holm"):
                            self.assertNotIn(word, line)

    def test_se_undefined(self):
        """(inference, spec S3): with 15 dates and a CR1 variance of exactly 0 the interval is not shown; the
        reason is pilot.cr1's own, "se undefined"."""
        state = state_of([k_record(i, day=i % 15) for i in range(30)])
        tables = self.assert_tables(state)
        for name in ("B4", "pooled"):
            self.assertEqual(tables["T1"]["rows"][name]["interval_k_outcome"],
                             {"shown": False, "reason": "se undefined"})
        self.assertTrue(any("se undefined" in line for line in lines_of(state)))

    def test_ledger_check_from_a_fold(self):
        """N28 (line 208): «a ledger check, only on K bets in ledger part S whose S value is our own net»; N29: «Its
        residual is the S value ÷ the final matched size − r.»; N32: «The other K bets are counted apart: those
        still in W ... and S WINNERs valued at Betfair's figure»."""
        journal, now = ledger_journal()
        state = journal.fold(now=now)
        self.assertEqual([(r.measurement["class"], r.ledger["part"], r.ledger["value_p"]) for r in state.place_records],
                         [("eligible", "S", -200), ("eligible", "S", 392), ("eligible", "S", 391),
                          ("eligible", "W", -200), ("eligible", "S", 203)])
        tables = self.assert_tables(state)
        rows = tables["T1"]["rows"]
        partial = Fraction(203, 137) - (Fraction("2.52") - 1) * (1 - CR)
        self.assertEqual(partial, Fraction(-672, 85625))
        self.assertEqual(rows["pooled"]["ledger"], {"checked": 3, "mean_residual": partial / 3, "outside_range": 0,
                                                    "in_w": 1, "s_winner_at_betfair_figure": 1})
        self.assertEqual(rows["B4"]["ledger"], {"checked": 2, "mean_residual": Fraction(0), "outside_range": 0,
                                                "in_w": 1, "s_winner_at_betfair_figure": 1})
        self.assertEqual(rows["B3"]["ledger"], {"checked": 1, "mean_residual": partial, "outside_range": 0,
                                                "in_w": 0, "s_winner_at_betfair_figure": 0})
        for name in ("B1", "B2"):
            self.assertEqual(rows[name]["ledger"], {"checked": 0, "mean_residual": None, "outside_range": 0,
                                                    "in_w": 0, "s_winner_at_betfair_figure": 0})
        for row in rows.values():
            ledger = row["ledger"]
            self.assertEqual(ledger["checked"] + ledger["in_w"] + ledger["s_winner_at_betfair_figure"], row["n"])

    def test_ledger_residual_range(self):
        """N30 (line 208): «so the residual lies above −2 / s and at or below 0, s the final matched size in pence:
        exactly 0 for a LOSER whose size is whole pence, above −0.01 for a full £2 match»."""
        rng = random.Random(208)
        for case in range(max(tp.SEEDS * 4, 40)):
            size = Decimal(rng.randrange(100, 20001)) / Decimal(10000)
            if case % 5 == 0:
                size = Decimal("2.00")
            P = Decimal(rng.randrange(101, 1001)) / Decimal(100)
            won = case % 2 == 1
            if won:
                value = pilot.s_winner_value(size, P, 0, c=C.commission_rate)["net_p"]
                self.assertEqual(value, own_net_p(size, P))
            else:
                value = pilot.loss_pence(size)
            rec = k_record(case, day=0, size=str(size), P=str(P), bsp="3.00", ours="WINNER" if won else "LOSER",
                           ledger=s_ledger(value))
            s = Fraction(size) * 100
            r = (Fraction(P) - 1) * (1 - CR) if won else Fraction(-1)
            residual = Fraction(value) / s - r
            with self.subTest(size=str(size), P=str(P), won=won):
                self.assertTrue(-2 / s < residual <= 0)
                if not won and s.denominator == 1:
                    self.assertEqual(residual, 0)
                if s == 200:
                    self.assertGreater(residual, Fraction(-1, 100))
                ledger = tables_of(state_of([rec]))["T1"]["rows"]["pooled"]["ledger"]
                self.assertEqual(ledger, {"checked": 1, "mean_residual": residual, "outside_range": 0, "in_w": 0,
                                          "s_winner_at_betfair_figure": 0})

    def test_out_of_range_is_counted_and_changes_no_table(self):
        """N31 (line 208): «The table shows the count, the mean residual and the count outside that range, which
        should be 0; one outside it is a defect to explain and changes no table.»"""
        records = mixed_records()
        good = k_record(50, day=1, limit_h=300, ours="LOSER", ledger=s_ledger(-200))
        wrong = dataclasses.replace(good, ledger=s_ledger(-150))
        a, b = tables_of(state_of(records + [good])), tables_of(state_of(records + [wrong]))
        self.assertEqual((a["T1"]["rows"]["B4"]["ledger"]["outside_range"],
                          a["T1"]["rows"]["pooled"]["ledger"]["outside_range"]), (0, 0))
        self.assertEqual((b["T1"]["rows"]["B4"]["ledger"]["outside_range"],
                          b["T1"]["rows"]["pooled"]["ledger"]["outside_range"]), (1, 1))
        self.assertEqual(without_ledger(a["T1"]), without_ledger(b["T1"]))
        self.assertEqual(a["T3"], b["T3"])
        self.assertEqual(a["T2"], b["T2"])
        self.assertEqual(a["bets"], b["bets"])
        self.assert_tables(state_of(records + [wrong]))

    def test_w_winner_residual_would_be_minus_net_payout(self):
        """N32 (line 208): «those still in W, which count minus their stake whatever the result (so a winner's
        residual there would be −G, exactly so when its size is whole pence)»."""
        state = state_of([k_record(0, day=0, ours="WINNER", ledger=w_ledger(-200))])
        (bet,) = selection_of(state)["bets"]
        self.assertEqual(Fraction(-200, 200) - bet["r"], -bet["net_payout"])
        self.assertEqual(-bet["net_payout"], -(1 + (Fraction("3.00") - 1) * (1 - CR)))
        ledger = self.assert_tables(state)["T1"]["rows"]["pooled"]["ledger"]
        self.assertEqual((ledger["in_w"], ledger["checked"], ledger["mean_residual"]), (1, 0, None))

    def test_no_band_is_counted(self):
        """(inference, spec 5.3): a hand-built k_outcome bet whose limit price is in no band is counted in no_band,
        stays in the pooled row and is in no band row."""
        records = mixed_records() + [k_record(80, day=2, limit_h=249)]
        state = state_of(records)
        tables = self.assert_tables(state)
        rows = tables["T1"]["rows"]
        self.assertEqual(tables["T1"]["no_band"], 1)
        self.assertEqual(rows["pooled"]["n"], len(records))
        self.assertEqual(sum(rows[name]["n"] for name, _, _ in NOTE_BANDS), len(records) - 1)
        self.assertIsNone(selection_of(state)["bets"][-1]["band"])
        self.assertIn(NO_BAND.format(1), lines_of(state))
        self.assertFalse(any(line.startswith("Bets with a limit price in no band") for line in lines_of(mixed_state())))

    def test_not_the_contracts_band_reports(self):
        """N33 (line 209): «contract p1's own "raw CLV by price band" and "BSP calibration by band" (:184-185) name
        no bands; this block does not set theirs.»; N4 (line 195): «its own list of end-only reports
        (p1-fav-t5.json:180-187) still applies as written»."""
        for state in (mixed_state(), state_of(spread(37, 16))):
            tables = tables_of(state)
            for key in dict_keys(tables):
                self.assertNotIn("raw clv", str(key).lower())
                self.assertNotIn("calibration", str(key).lower())
            for line in pilot_tables.render_tables(tables):
                self.assertNotIn("raw clv", line.lower())
                self.assertNotIn("calibration", line.lower())


class T2(TablesCase):
    """T2 (note lines 210-215): only its "not computed" forms and the seam are built now."""

    def test_three_states(self):
        """N36 (line 212): «Without a decision to buy, T2 is shown as "not computed: no file"»; N37: «A decision to
        buy that is then not carried out ... is reported as a departure, and T2 is shown as "not computed: files not
        bought".» The third state's wording is the spec's (inference, YA1)."""
        self.assertEqual(pilot_tables.T2_STATES, NOTE_T2_STATES)
        block = note_block()
        self.assertIn('"not computed: no file"', block)
        self.assertIn('"not computed: files not bought"', block)
        state = mixed_state()
        for key in NOTE_T2_STATES:
            with self.subTest(state=key):
                tables = self.assert_tables(state, key)
                self.assertEqual(tables["T2"], {"computed": False, "reason": pilot_tables.T2_STATES[key],
                                                "state": key})
                lines = pilot_tables.render_tables(tables)
                expected = T2_LINES[key]
                at = lines.index(expected[0])
                self.assertEqual(tuple(lines[at:at + len(expected)]), expected)
                others = {line for other in T2_LINES.values() for line in other} - set(expected)
                self.assertEqual(others & set(lines), set())

    def test_state_is_required(self):
        """N35c (line 212): «Yu decides before the data lock whether to buy them, and the decision is recorded in
        the plan's Decided list». The state is a required argument with no default, and only a key of T2_STATES."""
        state = mixed_state()
        with self.assertRaises(TypeError):
            pilot_tables.report_tables(state, contract=C, now_ns=state.now_ns)
        self.assertIs(inspect.signature(pilot_tables.report_tables).parameters["t2"].default, inspect.Parameter.empty)
        for bad in (None, "", "no file", "NO_DECISION_TO_BUY", "files not bought", 1, True, ("awaiting_files",)):
            with self.subTest(t2=bad):
                self.assert_refused("tables_input_invalid", tables_of, state, bad)

    def test_t2_does_not_touch_t1_or_t3(self):
        """N7 (line 196): «Each table is computed and shown whatever it shows»; N35b (line 212): «It runs whatever
        the verdict and the other tables show.»"""
        state = mixed_state()
        found = [tables_of(state, key) for key in NOTE_T2_STATES]
        for tables in found[1:]:
            self.assertEqual((tables["bets"], tables["T1"], tables["T3"]),
                             (found[0]["bets"], found[0]["T1"], found[0]["T3"]))

    def test_seam_inputs(self):
        """N34 (line 210): «both quotes are read at exactly those times from the Betfair Historical Data file of the
        same market, joined by market and selection id»; «Here the off is the decision record's (§4)». N35 (line
        212): T2's own run says which bets it uses in the same words (N12)."""
        state = mixed_state()
        found = selection_of(state)
        by_ref = {rec.ref: rec for rec in state.place_records}
        for bet in found["bets"]:
            rec = by_ref[bet["ref"]]
            self.assertEqual((bet["market_id"], bet["selection_id"], bet["off_ns"], bet["utc_day"]),
                             (rec.market_id, rec.selection_id, rec.off_ns, rec.utc_day))
        for t2 in NOTE_T2_STATES:
            tables = tables_of(state, t2)
            lines = pilot_tables.render_tables(tables)
            for source in (found, tables["bets"]):
                header = tuple(pilot_tables.header_lines(source))
                self.assertTrue(header)
                self.assertEqual(tuple(lines[:len(header)]), header)
                self.assertEqual(header[0], TITLE)

    def test_group_functions_serve_cells(self):
        """N38 (line 213): «Columns, per cell (STEAM, FLAT, DRIFT): n, G, mean NCLV, mean K and mean r»; N39: «The
        contrasts STEAM − FLAT and DRIFT − FLAT ... use movement-v1's CR1 variance of a difference». The public
        group and contrast functions take any groups of bets."""
        records = (on_days(offsets_in("H_SATURDAY", 16)) + on_days(offsets_in("OTHER_WEEKDAYS_NOT_H", 15), first=200)
                   + on_days(offsets_in("WEDNESDAY_NOT_H", 3), first=400))
        state = state_of(records, now=NOW_LATE)
        bets = selection_of(state)["bets"]
        tables = self.assert_tables(state)
        figures = pilot_tables.group_figures(bets, contract=C)
        self.assertEqual(figures, expected_group(list(bets)))
        self.assertEqual(set(figures), {"n", "G", "mean_nclv", "mean_k_outcome", "mean_r", "means_add_up"})
        self.assertEqual(pilot_tables.group_figures((), contract=C, intervals=("k_outcome",)),
                         dict(expected_group([]), interval_k_outcome={"shown": False, "reason": "too few days"}))
        h = [bet for bet in bets if bet["day_row"] in H_ROWS]
        other = [bet for bet in bets if bet["day_row"] == "OTHER_WEEKDAYS_NOT_H"]
        for measure in ("nclv", "k_outcome"):
            found = pilot_tables.contrast(h, other, measure, contract=C)
            self.assertEqual(found, tables["T3"]["contrasts"]["main"][measure])
            self.assertIs(found["interval"]["shown"], True)


class T3(TablesCase):
    """T3 (note lines 216-234): «T3, day types with a Wednesday placebo.»"""

    def test_london_day_is_race_day_of(self):
        """N41 (line 216): «A bet's day is the Europe/London calendar date of the decision record's off (the
        convention of the backtest's race_day_of, backtest/accounting.py:93-97).»"""
        try:
            import zoneinfo
            zone = zoneinfo.ZoneInfo("Europe/London")
        except Exception:                      # no zone data here: the race_day_of comparison still runs
            zone = None
        start, end = T("2026-10-01T00:00:00Z"), T("2027-07-31T23:00:00Z")
        instants = list(range(start, end + 1, 3600 * S))
        for edge in ("2026-10-25T01:00:00Z", "2027-03-28T01:00:00Z"):
            instants += [T(edge) - S, T(edge), T(edge) + S]
        for ns in instants:
            found = pilot_tables.london_day(ns)
            self.assertEqual(found, london_oracle(ns), pilot.utc_text_from_ns(ns))
            if zone is not None:
                self.assertEqual(found, moment(ns).astimezone(zone).date().isoformat())
        self.assertEqual(pilot_tables.london_day(T("1996-01-01T00:00:00Z")), "1996-01-01")
        for bad in (None, True, -1, pilot.MAX_NS, 0, T("1995-12-31T23:59:59Z"), "2027-01-04"):
            with self.subTest(off=bad):
                self.assert_refused("timestamp_invalid", pilot_tables.london_day, bad)

    def test_row_follows_london_date_and_cluster_follows_utc(self):
        """N41 (line 216) and N24 (line 204): the row is the London date of the off; G counts «UTC decision dates
        (contract p1's cluster, p1-fav-t5.json:89)»."""
        late = k_record(0, day=day_of("2027-06-05"), off_ns=T("2027-06-05T23:30:00Z"))
        saturday = k_record(1, day=day_of("2027-06-05"))
        sunday = k_record(2, day=day_of("2027-06-06"))
        state = state_of([late, saturday, sunday], now=NOW_LATE)
        bets = selection_of(state)["bets"]
        self.assertEqual((bets[0]["london_day"], bets[0]["day_row"], bets[0]["utc_day"]),
                         ("2027-06-06", "SUNDAY_NOT_H", "2027-06-05"))
        rows = self.assert_tables(state)["T3"]["rows"]
        self.assertEqual((rows["SUNDAY_NOT_H"]["n"], rows["SUNDAY_NOT_H"]["G"]), (2, 2))
        self.assertEqual((rows["H_SATURDAY"]["n"], rows["H_SATURDAY"]["G"]), (1, 1))

    def test_date_lists_are_the_notes(self):
        """N50, N51 (line 219): the England-and-Wales and Irish dates; N52: «Festivals: the ten in the table below»;
        N54 (line 220): «From the first PLACE record nothing is added, removed or moved»; N2: «Their bets, bands,
        cells, day types and dates are fixed». The constants equal the literals and the note's own text."""
        self.assertEqual(pilot_tables.BANK_HOLIDAYS_EW, NOTE_BANK_HOLIDAYS_EW)
        self.assertEqual(pilot_tables.PUBLIC_HOLIDAYS_IE, NOTE_PUBLIC_HOLIDAYS_IE)
        self.assertEqual(pilot_tables.FESTIVALS, NOTE_FESTIVALS)
        self.assertEqual((pilot_tables.T3_FIRST_DAY, pilot_tables.T3_LAST_DAY), (NOTE_FIRST_DAY, NOTE_LAST_DAY))
        block = note_block()
        self.assertIn("Dates, 2026-12-01 to 2027-06-30", block)
        dates = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
        start = block.index("read 2026-10-31: ", block.index("bank-holidays.json")) + len("read 2026-10-31: ")
        self.assertEqual(tuple(dates.findall(block[start:block.index(". Irish public holidays", start)])),
                         pilot_tables.BANK_HOLIDAYS_EW)
        start = block.index("They are ", block.index("Irish public holidays, from")) + len("They are ")
        self.assertEqual(tuple(dates.findall(block[start:block.index(". Festivals:", start)])),
                         pilot_tables.PUBLIC_HOLIDAYS_IE)
        lines = block.splitlines()
        head = next(i for i, line in enumerate(lines) if line.startswith("| Festival | Course | Dates |"))
        rows = []
        for line in lines[head + 2:]:
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            self.assertEqual(len(cells), 4, line)
            match = re.fullmatch(r"([0-9]{4})-([0-9]{2}-[0-9]{2}) to ([0-9]{2}-[0-9]{2})", cells[2])
            rows.append((cells[0], cells[1], f"{match[1]}-{match[2]}", f"{match[1]}-{match[3]}",
                         not cells[3].startswith("unconfirmed")))
        self.assertEqual(tuple(rows), pilot_tables.FESTIVALS)

    def test_note_self_checks(self):
        """N56 (line 221): «59 of the 212 days from 2026-12-01 to 2027-06-30 are H. Three Wednesdays are H
        (2027-03-17, 2027-04-28 and 2027-06-16), which leaves 28 non-H Wednesdays for the placebo.»"""
        days = iso_days(NOTE_FIRST_DAY, NOTE_LAST_DAY)
        self.assertEqual(len(days), 212)
        rows = {day: pilot_tables.day_row(day) for day in days}
        self.assertEqual(rows, {day: oracle_row(day) for day in days})
        self.assertEqual(sum(1 for day in days if pilot_tables.is_attention_day(day)), 59)
        wednesdays = [day for day in days if datetime.strptime(day, "%Y-%m-%d").weekday() == 2]
        self.assertEqual([day for day in wednesdays if pilot_tables.is_attention_day(day)],
                         ["2027-03-17", "2027-04-28", "2027-06-16"])
        self.assertEqual(sum(1 for day in wednesdays if not pilot_tables.is_attention_day(day)), 28)
        self.assertEqual([list(rows.values()).count(name) for name in NOTE_T3_ROWS], [30, 29, 26, 28, 99])
        h_other = [datetime.strptime(day, "%Y-%m-%d").weekday() for day in days if rows[day] == "H_OTHER"]
        self.assertEqual((h_other.count(2), h_other.count(6)), (3, 4))
        self.assertIn("59 of the 212 days from 2026-12-01 to 2027-06-30 are H. Three Wednesdays are H (2027-03-17, "
                      "2027-04-28 and 2027-06-16), which leaves 28 non-H Wednesdays", note_block())

    def test_rows(self):
        """N42 (line 216): «An attention day (H) is a Saturday, an England-and-Wales bank holiday, an Irish public
        holiday or a day of a listed festival»; N43: «Rows: H Saturday; H other (a holiday or festival day that is
        not a Saturday); Sunday, not H; Wednesday, not H; other weekdays (Monday, Tuesday, Thursday and Friday, not
        H).»"""
        self.assertEqual(pilot_tables.T3_ROWS, NOTE_T3_ROWS)
        self.assertEqual(pilot_tables.T3_ROW_LABELS, NOTE_T3_ROW_LABELS)
        for day, row in (("2026-12-26", "H_SATURDAY"), ("2026-12-27", "H_OTHER"), ("2027-03-17", "H_OTHER"),
                         ("2027-03-28", "H_OTHER"), ("2027-05-31", "H_OTHER"), ("2027-01-03", "SUNDAY_NOT_H"),
                         ("2027-01-06", "WEDNESDAY_NOT_H"), ("2027-01-05", "OTHER_WEEKDAYS_NOT_H")):
            with self.subTest(day=day):
                self.assertEqual(pilot_tables.day_row(day), row)
                self.assertEqual(pilot_tables.is_attention_day(day), row in H_ROWS)
        for day in iso_days("2026-11-01", "2027-07-31"):
            self.assertIn(pilot_tables.day_row(day), NOTE_T3_ROWS)
            self.assertEqual(pilot_tables.is_attention_day(day), pilot_tables.day_row(day) in H_ROWS)

    def test_unlisted_meeting_is_not_h(self):
        """N42 (line 216): «H holds for every GB and IE market that day, at any course»; N53 (line 219): «A meeting
        not in it (for example Chester's May meeting ...) gives H only on a Saturday or a holiday.»"""
        self.assertEqual(pilot_tables.day_row("2027-05-12"), "WEDNESDAY_NOT_H")
        self.assertFalse(pilot_tables.is_attention_day("2027-05-12"))
        self.assertEqual(list(inspect.signature(pilot_tables.day_row).parameters), ["day"])
        self.assertEqual(list(inspect.signature(pilot_tables.is_attention_day).parameters), ["day"])

    def test_days_before_december(self):
        """N49 (line 219): «If the first PLACE record comes earlier, the days before 2026-12-01 are H only as
        Saturdays or as holidays from the same sources, fixed before it; no festival is added.»"""
        for day, row in (("2026-11-14", "H_SATURDAY"), ("2026-11-18", "WEDNESDAY_NOT_H"),
                         ("2026-11-17", "OTHER_WEEKDAYS_NOT_H"), ("2026-11-30", "OTHER_WEEKDAYS_NOT_H")):
            self.assertEqual(pilot_tables.day_row(day), row)
        listed = (pilot_tables.BANK_HOLIDAYS_EW + pilot_tables.PUBLIC_HOLIDAYS_IE
                  + tuple(festival[2] for festival in pilot_tables.FESTIVALS))
        self.assertGreaterEqual(min(listed), "2026-12-25")
        state = state_of([k_record(0, day=day_of("2026-11-14")), k_record(1, day=day_of("2027-01-05"))])
        tables = self.assert_tables(state)
        self.assertEqual(tables["T3"]["bets_outside_registered_days"], 1)
        self.assertEqual(tables["T3"]["rows"]["H_SATURDAY"]["n"], 1)
        self.assertIn(OUTSIDE_DAYS.format(1), lines_of(state))

    def test_row_figures(self):
        """N44 (line 217): «Per row: n, G, mean NCLV, mean K and mean r.»"""
        state = mixed_state()
        tables = self.assert_tables(state)
        bets = selection_of(state)["bets"]
        expected_n = {"H_SATURDAY": 2, "H_OTHER": 2, "SUNDAY_NOT_H": 2, "WEDNESDAY_NOT_H": 2,
                      "OTHER_WEEKDAYS_NOT_H": 4}
        for name in NOTE_T3_ROWS:
            row, group = tables["T3"]["rows"][name], [bet for bet in bets if bet["day_row"] == name]
            self.assertEqual((row["n"], row["G"]), (expected_n[name], len({bet["utc_day"] for bet in group})))
            self.assertEqual((row["mean_nclv"], row["mean_k_outcome"], row["mean_r"]),
                             tuple(mean(bet[key] for bet in group) for key in ("nclv", "k_outcome", "r")))
        self.assertEqual(tables["T3"]["rows"]["OTHER_WEEKDAYS_NOT_H"]["G"], 3)

    def test_labels_are_the_registered_names(self):
        """N43 (line 216): the rows' names, "not H" included; N45 (line 217): «the main one, H (both H rows) minus
        other weekdays; the placebo, non-H Wednesday minus other weekdays»."""
        lines = lines_of(mixed_state())
        for label in NOTE_T3_ROW_LABELS.values():
            self.assertTrue(any(line.startswith(label) for line in lines), label)
        for label in (MAIN_LABEL, PLACEBO_LABEL):
            self.assertEqual(set(contrast_lines(lines, label)), {"NCLV", "k_outcome"}, label)
        self.assertTrue(any(MARK_HEADING in line for line in lines))

    def test_contrasts(self):
        """N45 (line 217): «Contrasts, each of mean NCLV and of mean K»; N48: «Intervals as in T2: two-sided 95%, CR1
        with t(G − 1)»; N39 (line 213): «movement-v1's CR1 variance of a difference
        (`registered_pass.contrasts_rule`), clustered by UTC decision date»."""
        records = (on_days(offsets_in("H_SATURDAY", 16)) + on_days([day_of("2027-03-16"), day_of("2027-03-18")],
                                                                  first=100)
                   + on_days(offsets_in("OTHER_WEEKDAYS_NOT_H", 18), first=200)
                   + on_days(offsets_in("WEDNESDAY_NOT_H", 4), first=300)
                   + [varied(400, day_of("2027-05-14"), off_ns=T("2027-05-14T23:30:00Z")),     # London 05-15
                      varied(401, day_of("2027-05-14"), off_ns=T("2027-05-14T14:00:00Z"))])    # London 05-14
        state = state_of(records, now=NOW_LATE)
        tables = self.assert_tables(state)
        bets = selection_of(state)["bets"]
        self.assertEqual((bets[-2]["day_row"], bets[-2]["utc_day"], bets[-1]["day_row"], bets[-1]["utc_day"]),
                         ("H_SATURDAY", "2027-05-14", "OTHER_WEEKDAYS_NOT_H", "2027-05-14"))
        for measure in ("nclv", "k_outcome"):
            with self.subTest(measure=measure):
                found = tables["T3"]["contrasts"]["main"][measure]
                a = [(bet["utc_day"], bet[measure]) for bet in bets if bet["day_row"] in H_ROWS]
                b = [(bet["utc_day"], bet[measure]) for bet in bets if bet["day_row"] == "OTHER_WEEKDAYS_NOT_H"]
                self.assertEqual(found["estimate"], mean(x for _, x in a) - mean(x for _, x in b))
                days_a, days_b = {d for d, _ in a}, {d for d, _ in b}
                g = len(days_a | days_b)
                self.assertEqual((found["G"], found["G_either"]), ((len(days_a), len(days_b)), g))
                self.assertEqual(found["G_either"], len(days_a) + len(days_b) - 1)        # 2027-05-14, once
                self.assertEqual(found["n"], (len(a), len(b)))
                interval = found["interval"]
                var = expected_contrast(a, b)["interval"]["se2"]
                t = Fraction(pilot.student_t_quantile(0.975, g - 1))
                self.assertEqual((interval["t"], interval["t_df"], interval["se2"]), (t, g - 1, var))
                self.assertEqual(interval["half_width_squared"], t ** 2 * var)
                self.assertEqual((interval["low"], interval["high"]),
                                 (found["estimate"] - interval["half_width"], found["estimate"] + interval["half_width"]))
                floats = movement.cr1_contrast([(d, float(x)) for d, x in a], [(d, float(x)) for d, x in b], 1, 1)
                self.assertLessEqual(abs(math.sqrt(var) - floats["se"]), 1e-12 * floats["se"])

    def test_contrast_needs_15_dates_on_each_side(self):
        """N48 (line 217): «only where G is at least 15: for a row, in that row; for a contrast, on each side.»"""
        saturdays, others = offsets_in("H_SATURDAY", 20), offsets_in("OTHER_WEEKDAYS_NOT_H", 15)
        cases = {"20 and 14": (saturdays, others[:14], False), "15 and 15": (saturdays[:15], others, True)}
        for label, (h_days, other_days, shown) in cases.items():
            with self.subTest(case=label):
                state = state_of(on_days(h_days) + on_days(other_days, first=200), now=NOW_LATE)
                main = self.assert_tables(state)["T3"]["contrasts"]["main"]
                for measure in ("nclv", "k_outcome"):
                    self.assertIsNotNone(main[measure]["estimate"])
                    self.assertEqual(main[measure]["interval"]["shown"], shown)
                    if not shown:
                        self.assertEqual(main[measure]["interval"], {"shown": False, "reason": "too few days"})
        state = state_of(on_days(saturdays), now=NOW_LATE)
        found = self.assert_tables(state)["T3"]
        for kind in ("main", "placebo"):
            for measure in ("nclv", "k_outcome"):
                self.assertIsNone(found["contrasts"][kind][measure]["estimate"])
                self.assertEqual(found["contrasts"][kind][measure]["interval"],
                                 {"shown": False, "reason": "too few days"})
        self.assertEqual(found["placebo_marked"], {"nclv": None, "k_outcome": None})

    def test_row_intervals(self):
        """N48 (line 217): «Intervals as in T2: two-sided 95%, CR1 with t(G − 1), only where G is at least 15: for a
        row, in that row». (inference, spec S1: for mean NCLV and mean k_outcome, none for mean r.)"""
        state = state_of(on_days(offsets_in("OTHER_WEEKDAYS_NOT_H", 15))
                         + on_days(offsets_in("SUNDAY_NOT_H", 14), first=200), now=NOW_LATE)
        tables = self.assert_tables(state)
        bets = selection_of(state)["bets"]
        for name, shown in (("OTHER_WEEKDAYS_NOT_H", True), ("SUNDAY_NOT_H", False)):
            row = tables["T3"]["rows"][name]
            self.assertEqual(set(row), {"n", "G", "mean_nclv", "mean_k_outcome", "mean_r", "means_add_up",
                                        "interval_nclv", "interval_k_outcome"})
            for measure in ("nclv", "k_outcome"):
                interval = row["interval_" + measure]
                self.assertEqual(interval["shown"], shown)
                if shown:
                    found = pilot.cr1([(bet["utc_day"], bet[measure]) for bet in bets if bet["day_row"] == name],
                                      min_clusters=15, alpha=Fraction(1, 40))
                    self.assertEqual((interval["t"], interval["se2"], interval["half_width_squared"]),
                                     (found["t"], found["se2"], found["bound"]))
                    self.assertEqual(interval["low"] + interval["high"], 2 * found["m"])
                else:
                    self.assertEqual(interval, {"shown": False, "reason": "too few days"})

    def placebo_state(self, w_bsp, *, wednesday=True):
        """One bet each at P 3.00 (net payout 74/25): other weekdays BSP 2, lost; H BSP 4, won; non-H Wednesday
        BSP w_bsp, lost. With w_bsp 8/3 the placebo NCLV estimate is exactly half the main one (-37/100 against
        -37/50) and the placebo k_outcome one is a tenth of it (37/100 against 37/10)."""
        records = [k_record(0, day=day_of("2027-01-05"), bsp=Fraction(2), ours="LOSER"),
                   k_record(1, day=day_of("2027-01-09"), bsp=Fraction(4), ours="WINNER")]
        if wednesday:
            records.append(k_record(2, day=day_of("2027-01-06"), bsp=w_bsp, ours="LOSER"))
        return state_of(records)

    def test_placebo_mark(self):
        """N46 (line 217): «A placebo whose estimate is at least half the main one's, in absolute value and for the
        same measure, is marked beside the main one, for people to read.» (inference, spec S9: a column of the main
        contrast's own row, per measure.)"""
        cases = {"exactly half": (Fraction(8, 3), {"nclv": True, "k_outcome": False}),
                 "just under half": (1 / (Fraction(3, 8) + Fraction(1, 10 ** 6)), {"nclv": False, "k_outcome": False})}
        for label, (w_bsp, marked) in cases.items():
            with self.subTest(case=label):
                state = self.placebo_state(w_bsp)
                found = self.assert_tables(state)["T3"]
                self.assertEqual(found["placebo_marked"], marked)
                if label == "exactly half":
                    self.assertEqual((found["contrasts"]["main"]["nclv"]["estimate"],
                                      found["contrasts"]["placebo"]["nclv"]["estimate"]),
                                     (Fraction(-37, 50), Fraction(-37, 100)))
                    self.assertEqual((found["contrasts"]["main"]["k_outcome"]["estimate"],
                                      found["contrasts"]["placebo"]["k_outcome"]["estimate"]),
                                     (Fraction(37, 10), Fraction(37, 100)))
                lines = lines_of(state)
                main, placebo = contrast_lines(lines, MAIN_LABEL), contrast_lines(lines, PLACEBO_LABEL)
                self.assertEqual(main["NCLV"].split()[-1], "yes" if marked["nclv"] else "no")
                self.assertEqual(main["k_outcome"].split()[-1], "yes" if marked["k_outcome"] else "no")
                for line in placebo.values():
                    self.assertNotIn(line.split()[-1], ("yes", "no"))
        state = self.placebo_state(None, wednesday=False)
        self.assertEqual(self.assert_tables(state)["T3"]["placebo_marked"], {"nclv": None, "k_outcome": None})
        main = contrast_lines(lines_of(state), MAIN_LABEL)
        self.assertEqual((main["NCLV"].split()[-1], main["k_outcome"].split()[-1]), ("-", "-"))

    def test_sunday_is_in_no_contrast(self):
        """N47 (line 217): «Sunday is shown on its own.»"""
        records = mixed_records()
        changed = [dataclasses.replace(rec, bsp=dict(rec.bsp, bsp=Decimal("9.50")))
                   if oracle_row(london_oracle(rec.off_ns)) == "SUNDAY_NOT_H" else rec for rec in records]
        self.assertNotEqual(changed, records)
        a, b = tables_of(state_of(records))["T3"], tables_of(state_of(changed))["T3"]
        self.assertNotEqual(a["rows"]["SUNDAY_NOT_H"], b["rows"]["SUNDAY_NOT_H"])
        for name in NOTE_T3_ROWS:
            if name != "SUNDAY_NOT_H":
                self.assertEqual(a["rows"][name], b["rows"][name])
        self.assertEqual((a["contrasts"], a["placebo_marked"]), (b["contrasts"], b["placebo_marked"]))
        self.assert_tables(state_of(changed))

    def test_unconfirmed_rows_are_said(self):
        """N55 (line 220): «a row that still cannot be read keeps its registered dates, still marked unconfirmed,
        and T3 says so.»"""
        state = mixed_state()
        self.assertEqual(tables_of(state)["T3"]["unconfirmed_festivals"], UNCONFIRMED)
        found = [line for line in lines_of(state) if line.startswith(UNCONFIRMED_PREFIX)]
        self.assertEqual(len(found), 1)
        self.assertIn("; ".join(UNCONFIRMED), found[0])


class OutputOnly(TablesCase):
    """Note line 197: «It adds output only: no class, sample, ledger value or verdict changes, and a test pins that
    verdict() returns the same with the tables and without them.»"""

    def test_verdict_same_with_and_without_tables(self):
        """N10 (line 197) and N3 (line 195): «They cannot change the rule, inclusion, N, the threshold or the
        verdict, and they never set or lift a pause or a stop.»"""
        for label, (build, expected) in verdict_cases().items():
            with self.subTest(case=label):
                state = build()
                before = pilot.verdict(state, contract=C, now_ns=state.now_ns)
                self.assertEqual((before["result"], before["reason"]), expected)
                kept = copy.deepcopy(state)
                for t2 in NOTE_T2_STATES:
                    pilot_tables.render_tables(tables_of(state, t2))
                selection_of(state)
                after = pilot.verdict(state, contract=C, now_ns=state.now_ns)
                fresh = build()
                unseen = pilot.verdict(fresh, contract=C, now_ns=fresh.now_ns)
                self.assertEqual(before, after)
                self.assertEqual(before, unseen)
                self.assertEqual(state, kept)

    def test_no_class_sample_or_ledger_change(self):
        """N10 (line 197): «no class, sample, ledger value or verdict changes»; N3 (line 195): «they never set or
        lift a pause or a stop»."""
        journal, now = ledger_journal()
        state = journal.fold(now=now)

        def snapshot():
            split = pilot.analysis_population(state, now_ns=state.now_ns)
            return copy.deepcopy({
                "records": [(r.record, r.measurement, r.execution, r.bsp, r.ledger) for r in state.place_records],
                "equity": (pilot.equity(state), state.equity_p), "stops": state.stops, "due": state.due_stops,
                "active": state.active_stops, "pause": state.pause_fired,
                "split": {key: [r.ref for r in value] if isinstance(value, list) else value
                          for key, value in split.items()},
                "ending": pilot.placing_ending(state, now_ns=state.now_ns)})

        before = snapshot()
        for t2 in NOTE_T2_STATES:
            pilot_tables.render_tables(tables_of(state, t2))
        bets = selection_of(state)["bets"]
        pilot_tables.group_figures(bets, contract=C, intervals=("nclv", "k_outcome"))
        pilot_tables.contrast(bets[:2], bets[2:], "nclv", contract=C)
        self.assertEqual(snapshot(), before)
        self.assertEqual(state, journal.fold(now=now))

    def test_shown_whatever_the_verdict(self):
        """N7 (line 196): «Each table is computed and shown whatever it shows»."""
        cases = verdict_cases()
        for label in ("protocol_violation", "gate_failed", "too_few_clusters", "insufficient", "FAIL",
                      "journal protocol_violation"):
            with self.subTest(case=label):
                state = cases[label][0]()
                tables = self.assert_tables(state)
                self.assertIs(tables["T1"]["computed"], True)
                self.assertIs(tables["T3"]["computed"], True)
        empty = state_of([synthetic("unmatched", i, day=i) for i in range(5)])
        tables = self.assert_tables(empty)
        self.assertEqual((tables["bets"]["eligible"], tables["bets"]["k_outcome_bets"]), (0, 0))
        for row in list(tables["T1"]["rows"].values()) + list(tables["T3"]["rows"].values()):
            self.assertEqual((row["n"], row["G"], row["mean_nclv"], row["means_add_up"]), (0, 0, None, None))

    def test_deterministic(self):
        """N5 (line 196): «Astra recomputes them as it recomputes that analysis»: the same state gives the same
        tables, and the result holds no float."""
        journal, now = ledger_journal()
        for build in (mixed_state, lambda: journal.fold(now=now)):
            first, second = build(), build()
            a, b, c = tables_of(first), tables_of(first), tables_of(second)
            self.assertEqual(a, b)
            self.assertEqual(a, c)
            self.assertEqual(pilot_tables.render_tables(a), pilot_tables.render_tables(c))
            for value in list(leaves(a)) + list(leaves(selection_of(first))):
                self.assertNotIsInstance(value, float)

    def test_three_tables_only(self):
        """N1 (line 194): «uses that yes for three tables only (T1-T3)». And the shapes of spec 4.4."""
        state = mixed_state()
        tables = tables_of(state)
        self.assertEqual(set(tables), {"bets", "T1", "T2", "T3"})
        self.assertEqual(set(tables["bets"]), {"source", "n_reached", "eligible", "k_outcome_bets", "excluded",
                                               "not_in", "missing"})
        self.assertEqual(set(selection_of(state)), {"source", "n_reached", "eligible", "excluded", "not_in",
                                                    "missing", "bets"})
        self.assertEqual(set(tables["T1"]), {"computed", "rows", "no_band"})
        self.assertEqual(set(tables["T1"]["rows"]), {"B1", "B2", "B3", "B4", "pooled"})
        self.assertEqual(set(tables["T1"]["rows"]["pooled"]),
                         {"n", "G", "mean_nclv", "mean_k_outcome", "mean_r", "means_add_up", "var_k_outcome",
                          "sd_k_outcome", "interval_k_outcome", "ledger"})
        self.assertEqual(set(tables["T3"]), {"computed", "rows", "contrasts", "placebo_marked",
                                             "unconfirmed_festivals", "bets_outside_registered_days"})
        self.assertEqual(set(tables["T3"]["rows"]), set(NOTE_T3_ROWS))
        self.assertEqual(set(tables["T3"]["contrasts"]["main"]["nclv"]), {"n", "G", "G_either", "estimate",
                                                                         "interval"})

    def test_rendering_has_no_ids(self):
        """N8 (line 196): «Publication follows the rule above.» The rendering and report_tables' result hold no ref,
        market id or selection id; only k_outcome_bets does."""
        for state in (mixed_state(), state_of(spread(430, 20))):
            refs = {rec.ref for rec in state.place_records}
            markets = {rec.market_id for rec in state.place_records}
            selections = {rec.selection_id for rec in state.place_records}
            for t2 in NOTE_T2_STATES:
                tables = tables_of(state, t2)
                for line in pilot_tables.render_tables(tables):
                    for text in ("p1tab-", "1.98765", "90000000", "exposure"):
                        self.assertNotIn(text, line)
                    self.assertIsNone(reports.BET_ID_RE.search(line), line)
                    self.assertIsNone(reports.CUSTOMER_REF_RE.search(line), line)
                    self.assertIsNone(reports.ACCOUNT_KEY_RE.search(line), line)
                    self.assertIsNone(re.search(r"[0-9]{11,}", line), line)
                for value in leaves(tables):
                    if isinstance(value, str):
                        self.assertFalse(any(ref in value for ref in refs | markets), value)
                    elif type(value) is int:
                        self.assertNotIn(value, selections)

    def test_render_returns_lines(self):
        """N5 (line 196): «in the terminal»: render_tables returns lines and prints nothing."""
        missing = state_of([dataclasses.replace(k_record(0, day=0), utc_day=None)])
        for state in (mixed_state(), state_of(spread(37, 16)), missing):
            for t2 in NOTE_T2_STATES:
                lines = lines_of(state, t2)
                self.assertIs(type(lines), tuple)
                self.assertTrue(lines)
                self.assertEqual(lines[0], TITLE)
                for line in lines:
                    self.assertIs(type(line), str)
                    self.assertNotIn("\n", line)
                    self.assertNotIn("\r", line)


def note_block():
    """Note §12's block "Registered report tables (Yu, 2026-10-31)", to the start of §13."""
    text = NOTE.read_text(encoding="utf-8")
    start = text.index("Registered report tables (Yu, 2026-10-31).")
    return text[start:text.index("## 13.", start)]


if __name__ == "__main__":
    unittest.main()
