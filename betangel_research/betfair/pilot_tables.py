"""Pure Pilot 1 registered end tables T1-T3 over a folded state (docs/betfair-pilot-engineering.md §12, "Registered
report tables (Yu, 2026-10-31)", the note's lines 194 to 234).

The tables never decide (note line 195): this module reads a state that pilot.fold returned and returns plain values
and terminal lines. It never calls the verdict, the fold or any ledger, pause or stop function, and it never writes
into the state or into the fold's own dicts, so no class, sample, ledger value, stop or verdict can change (line
197). It is called only at the data lock (line 196) and refuses before it with the code the verdict uses. Nothing
here reads a clock, a file, the network or the environment, and nothing prints: the analysis command prints the
lines and owns "computed once".

The split term of line 200 is k_outcome here, in names, keys, comments and rendered text, because contract p1 is
cited by its line, as in K:154 (line 197). The payout 1 + (P - 1)(1 - c) of line 200 is net_payout, because the
note also uses G for the number of UTC decision dates (line 204), and contract p1 (K:111) and pilot.s_winner_value
use "gross" for the winnings before commission.

Numbers are exact as in pilot: Betfair numbers are Decimal and become Fraction before any arithmetic, money is int
pence, ladder prices are int hundredths, times are int nanoseconds. Floats appear only inside pilot.cr1's Student t
routine, whose result is already a Fraction. Display rounding happens in render_tables only; the data stay exact.

What this delivers of the note's registered code: T1, T3, T2's "not computed" forms and the functions T2's own run
needs (k_outcome_bets, group_figures, contrast, header_lines). T2's file reader and the analysis command are not
here (lines 197 and 211).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
import math

from . import pilot


# ---------------------------------------------------------------------------------------------------------------
# Registered constants (note lines 194, 203 and 219 to 234). Fixed from the first PLACE record on: no argument,
# config value or environment value changes them.

# T1's bands on the decision limit price in int hundredths, bounds inclusive: movement-v1's strata.price_bands_h
# (note line 203).
PRICE_BANDS_H = (("B1", 150, 199), ("B2", 200, 248), ("B3", 250, 298), ("B4", 300, 400))

# T3's registered days: the plan's December start to contract p1's final date (note line 219; K:32).
T3_FIRST_DAY, T3_LAST_DAY = "2026-12-01", "2027-06-30"

# England-and-Wales bank holidays, gov.uk/bank-holidays.json read 2026-10-31 (note line 219).
BANK_HOLIDAYS_EW = ("2026-12-25", "2026-12-28", "2027-01-01", "2027-03-26", "2027-03-29", "2027-05-03",
                    "2027-05-31")

# Irish public holidays, workplacerelations.ie read 2026-10-31, the 2027 ones by its rules (note line 219).
PUBLIC_HOLIDAYS_IE = ("2026-12-25", "2026-12-26", "2027-01-01", "2027-02-01", "2027-03-17", "2027-03-29",
                      "2027-05-03", "2027-06-07")

# The ten festivals of the note's table (lines 225 to 234): (name, course, first day, last day, confirmed). A row
# whose source cell starts with "unconfirmed" has confirmed False; it keeps its registered dates and T3 says so
# (note line 220).
FESTIVALS = (
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

# T3's rows (note line 216), with the registered names, "not H" included.
T3_ROWS = ("H_SATURDAY", "H_OTHER", "SUNDAY_NOT_H", "WEDNESDAY_NOT_H", "OTHER_WEEKDAYS_NOT_H")
T3_ROW_LABELS = {"H_SATURDAY": "H Saturday", "H_OTHER": "H other", "SUNDAY_NOT_H": "Sunday, not H",
                 "WEDNESDAY_NOT_H": "Wednesday, not H", "OTHER_WEEKDAYS_NOT_H": "other weekdays, not H"}

# T2's state at the analysis run, a required argument with no default (note line 212). The first two reasons are
# the note's own texts. (inference, spec YA1) The third has no text in the note: its wording, and whether it should
# also cover files already held at the analysis run (T2 has its own run either way), are for Yu and Astra.
T2_STATES = {"no_decision_to_buy": "no file",            # note line 212: never computed later
             "decided_not_bought": "files not bought",   # note line 212: reported as a departure
             "awaiting_files": "files not yet held"}     # (inference, spec YA1) provisional wording, not in the note

_MEASURES = ("nclv", "k_outcome")                       # the measures with intervals and contrasts (lines 207, 217)
_H_ROWS = ("H_SATURDAY", "H_OTHER")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_LONDON_RULE_FROM_YEAR = 1996                           # the rule's first year, as backtest race_day_of refuses
_ROOT_PLACES = 9


def _day_parts(day):
    """(year, month, day) of a 'YYYY-MM-DD' text, or PilotError("day_invalid")."""
    if (type(day) is not str or len(day) != 10 or day[4] != "-" or day[7] != "-" or not day.isascii()
            or not (day[:4] + day[5:7] + day[8:]).isdigit()):
        raise pilot.PilotError("day_invalid")
    return int(day[:4]), int(day[5:7]), int(day[8:])


def _midnight(day):
    year, month, number = _day_parts(day)
    try:
        return datetime(year, month, number)
    except ValueError:
        raise pilot.PilotError("day_invalid") from None


def _days_from_to(first, last):
    """Every 'YYYY-MM-DD' from first to last, both included."""
    moment, end, days = _midnight(first), _midnight(last), []
    while moment <= end:
        days.append(moment.strftime("%Y-%m-%d"))
        moment += timedelta(days=1)
    return days


_HOLIDAY_DAYS = frozenset(BANK_HOLIDAYS_EW + PUBLIC_HOLIDAYS_IE)
_FESTIVAL_DAYS = frozenset(day for _, _, first, last, _ in FESTIVALS for day in _days_from_to(first, last))


# ---------------------------------------------------------------------------------------------------------------
# Bands and days.

def price_band(limit_price_h):
    """The name of the band holding an int price in hundredths (inclusive bounds), or None (note line 203)."""
    if type(limit_price_h) is not int:
        return None
    for name, low, high in PRICE_BANDS_H:
        if low <= limit_price_h <= high:
            return name
    return None


def _last_sunday_1am(year, month):
    end = datetime(year, month, 31, 1, tzinfo=timezone.utc)          # March and October have 31 days
    return end - timedelta(days=(end.weekday() - 6) % 7)


def london_day(off_ns):
    """'YYYY-MM-DD': the Europe/London calendar date holding off_ns (note line 216). It mirrors the backtest's
    race_day_of (British Summer Time from 01:00 UTC on the last Sunday of March to 01:00 UTC on the last Sunday of
    October; Ireland changes at the same instants) and does not import it, which would widen this pure closure."""
    if type(off_ns) is not int or not 0 <= off_ns < pilot.MAX_NS:
        raise pilot.PilotError("timestamp_invalid")
    utc = _EPOCH + timedelta(microseconds=off_ns // 1000)
    if utc.year < _LONDON_RULE_FROM_YEAR:
        raise pilot.PilotError("timestamp_invalid")
    summer = _last_sunday_1am(utc.year, 3) <= utc < _last_sunday_1am(utc.year, 10)
    return (utc + timedelta(hours=1 if summer else 0)).strftime("%Y-%m-%d")


def day_row(day):
    """The T3 row of 'YYYY-MM-DD': one of T3_ROWS (note line 216). H depends on the date only: a Saturday, a listed
    holiday or a day of a listed festival; no country, course or other meeting is read (note line 219)."""
    weekday = _midnight(day).weekday()                                  # Monday 0 ... Sunday 6
    if weekday == 5:
        return "H_SATURDAY"
    if day in _HOLIDAY_DAYS or day in _FESTIVAL_DAYS:
        return "H_OTHER"
    if weekday == 6:
        return "SUNDAY_NOT_H"
    if weekday == 2:
        return "WEDNESDAY_NOT_H"
    return "OTHER_WEEKDAYS_NOT_H"


def is_attention_day(day):
    """Whether 'YYYY-MM-DD' is H: a Saturday, a listed holiday or a listed festival day (note line 216)."""
    return day_row(day) in _H_ROWS


# ---------------------------------------------------------------------------------------------------------------
# The k_outcome bets and their split (note lines 198 to 200).

def _exact(value):
    """A Fraction of an exact number (Fraction, int but not bool, finite Decimal), or PilotError("number_invalid")."""
    if isinstance(value, bool):
        raise pilot.PilotError("number_invalid")
    if isinstance(value, (Fraction, int)):
        return Fraction(value)
    if isinstance(value, Decimal) and value.is_finite():
        return Fraction(value)
    raise pilot.PilotError("number_invalid")


def _refuse(state, *, contract, now_ns):
    """The verdict's own two refusals, in its order (pilot.verdict): another contract, then before the data lock
    (note line 196: computed once, after the data lock)."""
    if contract != state.contract:
        raise pilot.PilotError("contract_mismatch")
    if not pilot.data_lock_reached(state, now_ns=now_ns):
        raise pilot.PilotError("data_lock_not_reached")


def _shared_missing(rec, rate):
    """The first shared input missing on an eligible record, or None: the CLOSED read, our result in it, the final
    price P with the frozen BSP, the UTC decision date. Without them no bet can be formed or split."""
    bsp = rec.bsp if isinstance(rec.bsp, dict) else {}
    closed = bsp.get("closed_read")
    if not isinstance(closed, dict) or not isinstance(closed.get("runners"), dict):
        return "closed_read"
    ours = closed["runners"].get(str(rec.selection_id))
    if not isinstance(ours, dict) or ours.get("status") not in pilot.RESULTS:
        return "our_result"
    execution = rec.execution if isinstance(rec.execution, dict) else {}
    try:
        pilot.nclv(execution.get("P"), bsp.get("bsp"), c=rate)
    except pilot.PilotError:
        return "final_price_or_bsp"
    if not isinstance(rec.utc_day, str):
        return "utc_day"
    return None


def _winners(rec):
    """The number of runners whose status in the CLOSED read is exactly WINNER (note line 199: numberOfWinners is
    not kept and not re-checked; another runner's status of None, ACTIVE or REMOVED excludes nothing)."""
    return sum(1 for entry in rec.bsp["closed_read"]["runners"].values()
               if isinstance(entry, dict) and entry.get("status") == "WINNER")


def _positive_size(size):
    if isinstance(size, bool):
        return False
    if type(size) is int or isinstance(size, Fraction) or (isinstance(size, Decimal) and size.is_finite()):
        return Fraction(size) > 0
    return False


def _t1_missing(rec):
    """The first of T1's own inputs missing on a k_outcome bet, or None: the decision limit price, the final
    matched size, the ledger part and value."""
    record = rec.record if isinstance(rec.record, dict) else {}
    if type(record.get("limit_price_h")) is not int:
        return "limit_price"
    execution = rec.execution if isinstance(rec.execution, dict) else {}
    if not _positive_size(execution.get("size")):
        return "final_size"
    ledger = rec.ledger if isinstance(rec.ledger, dict) else {}
    if ledger.get("part") not in ("W", "S") or type(ledger.get("value_p")) is not int:
        return "ledger"
    return None


def _london_day_or_none(off_ns):
    try:
        return london_day(off_ns)
    except pilot.PilotError:
        return None


def _bet(rec, rate, *, t1_ok, day):
    """One k_outcome bet with its split (note line 200). The three terms come from their own formulas, so that
    nclv + k_outcome == r is a check and not true by construction. P is used as it is: an eligible bet has no void
    and no post-decision removal (note line 199). T1's fields are None unless all of T1's inputs are present (t1_ok);
    T3's are None when the off has no London date (day None)."""
    P, bsp = rec.execution["P"], rec.bsp["bsp"]
    nclv = pilot.nclv(P, bsp, c=rate)                       # contract p1's metric (K:154), the verdict's NCLV
    price, base, c = Fraction(P), Fraction(bsp), Fraction(rate)
    win = 1 if rec.bsp["closed_read"]["runners"][str(rec.selection_id)]["status"] == "WINNER" else 0
    net_payout = 1 + (price - 1) * (1 - c)
    k_outcome = net_payout * (win - 1 / base)
    r = net_payout * win - 1
    limit = rec.record["limit_price_h"] if t1_ok else None
    return {"ref": rec.ref, "market_id": rec.market_id, "selection_id": rec.selection_id, "off_ns": rec.off_ns,
            "utc_day": rec.utc_day,
            "win": win, "P": P, "bsp": bsp,
            "net_payout": net_payout, "nclv": nclv, "k_outcome": k_outcome, "r": r,
            "limit_price_h": limit, "band": price_band(limit) if t1_ok else None,
            "size": rec.execution["size"] if t1_ok else None,
            "ledger_part": rec.ledger["part"] if t1_ok else None,
            "ledger_value_p": rec.ledger["value_p"] if t1_ok else None,
            "london_day": day, "day_row": None if day is None else day_row(day)}


def _selection(state, *, contract, now_ns):
    split = pilot.analysis_population(state, now_ns=now_ns)
    # The verdict's own list expression (pilot.verdict): the first n_eligible eligible records of the analysis
    # population. PT4 records ("apart") and surplus records are only counted (note line 198; K:241, K:245-249).
    base_bets = [rec for rec in split["population"] if rec.measurement["class"] == "eligible"][:contract.n_eligible]
    rate = contract.commission_rate
    n_reached = split["n_reached"] is True
    head = {"source": "primary_sample" if n_reached else "analysis_population", "n_reached": n_reached,
            "eligible": len(base_bets)}
    not_in = {"pt4_records": len(split["apart"]), "surplus_records": len(split["surplus"])}
    # (inference, spec S6) The fold cannot produce an eligible bet with a missing input. On a hand-built state a
    # shared input missing on any bet stops T1 and T3 both, and an input one table reads stops that table only; no
    # bet is dropped silently, which would change "the same bets" (note line 198).
    missing = {"bets": None, "T1": None, "T3": None}
    for rec in base_bets:
        name = _shared_missing(rec, rate)
        if name is not None:
            missing["bets"] = name
            return dict(head, excluded={"dead_heat": None, "no_winner": None}, not_in=not_in, missing=missing,
                        bets=())
    bets, dead_heat, no_winner = [], 0, 0
    for rec in base_bets:
        winners = _winners(rec)
        if winners >= 2:                    # a dead heat, whatever our runner's status (note line 198)
            dead_heat += 1
            continue
        if winners == 0:
            no_winner += 1
            continue
        t1, day = _t1_missing(rec), _london_day_or_none(rec.off_ns)
        if missing["T1"] is None:
            missing["T1"] = t1
        if missing["T3"] is None and day is None:
            missing["T3"] = "off"
        bets.append(_bet(rec, rate, t1_ok=t1 is None, day=day))
    return dict(head, excluded={"dead_heat": dead_heat, "no_winner": no_winner}, not_in=not_in, missing=missing,
                bets=tuple(bets))


def k_outcome_bets(state, *, contract, now_ns):
    """The bets every table uses (note lines 198 and 199), each with its split (line 200). Also the input of T2's
    reader, and what Astra's recomputation can be compared with bet by bet.

    {"source", "n_reached", "eligible", "excluded": {"dead_heat", "no_winner"}, "not_in": {"pt4_records",
    "surplus_records"}, "missing": {"bets", "T1", "T3"}, "bets"}: the bets are the contract's primary sample when
    N is reached, else every eligible bet of the analysis population, and of those only the ones whose frozen BSP's
    CLOSED read shows exactly one WINNER, in decision order."""
    _refuse(state, contract=contract, now_ns=now_ns)
    return _selection(state, contract=contract, now_ns=now_ns)


# ---------------------------------------------------------------------------------------------------------------
# Group figures, roots and CR1 intervals (note lines 204 to 207, 213 and 217).

def _root(value, places=_ROOT_PLACES):
    """floor(sqrt(value) * 10**places) / 10**places for a Fraction value >= 0: less than 10**-places below the
    exact root, with no float. The exact square is returned beside every root. (inference, spec S8: the cut at 9
    places is not registered, so a shown interval's ends lie up to 10**-9 inside the exact ones.)"""
    return Fraction(math.isqrt(value.numerator * 10 ** (2 * places) // value.denominator), 10 ** places)


def _interval(found, centre):
    """The two-sided interval from a pilot.cr1 result, around centre. Its decision words (result, near_tie, t_p)
    are never read: the intervals are descriptive and test nothing (note line 207)."""
    if found["reason"] == "too_few_clusters":
        return {"shown": False, "reason": "too few days"}
    if found["reason"] == "se_undefined":
        # (inference, spec S3) Enough dates and a CR1 variance of exactly 0: no interval of zero width, but
        # pilot.cr1's own second reason. The note names only "too few days" (line 207).
        return {"shown": False, "reason": "se undefined"}
    half = _root(found["bound"])
    return {"shown": True, "low": centre - half, "high": centre + half, "half_width": half,
            "half_width_squared": found["bound"], "se2": found["se2"], "t": found["t"], "t_df": found["t_df"]}


def _cr1(rows, contract):
    # alpha_one_sided 1/40 gives t at the 97.5% point: the two-sided 95% interval (K:85-100); min_clusters 15 (K:94).
    # (inference, spec S7) The level and the 15 are read from the contract object, not held as constants here; a
    # test pins that contract p1 gives 95%, 15 and c = 0.02.
    return pilot.cr1(rows, min_clusters=contract.min_clusters, alpha=contract.alpha_one_sided)


def _mean(values):
    return sum(values, Fraction(0)) / len(values) if values else None


def _measure(measure):
    if not isinstance(measure, str) or measure not in _MEASURES:
        raise pilot.PilotError("tables_input_invalid")
    return measure


def group_figures(bets, *, contract, intervals=()):
    """n, G, the three means and the add-up check for any group of bets, with a CR1 interval for each measure
    named in intervals ("nclv", "k_outcome"). T1 rows, T3 rows and, later, T2 cells.

    n is the number of bets and G the number of distinct UTC decision dates (contract p1's cluster, K:89). The
    means are unweighted, as contract p1's primary; mean_nclv + mean_k_outcome == mean_r exactly (note line 205)."""
    measures = [_measure(measure) for measure in intervals]
    bets = list(bets)
    nclvs = [_exact(bet["nclv"]) for bet in bets]
    k_outcomes = [_exact(bet["k_outcome"]) for bet in bets]
    rs = [_exact(bet["r"]) for bet in bets]
    mean_nclv, mean_k_outcome, mean_r = _mean(nclvs), _mean(k_outcomes), _mean(rs)
    figures = {"n": len(bets), "G": len({bet["utc_day"] for bet in bets}),
               "mean_nclv": mean_nclv, "mean_k_outcome": mean_k_outcome, "mean_r": mean_r,
               "means_add_up": None if not bets else mean_nclv + mean_k_outcome == mean_r}
    for measure in measures:
        found = _cr1([(bet["utc_day"], _exact(bet[measure])) for bet in bets], contract)
        figures["interval_" + measure] = _interval(found, found["m"])
    return figures


def contrast(first, second, measure, *, contract):
    """Mean of measure over first minus over second, with movement-v1's CR1 variance of a difference clustered by
    UTC decision date. T3's contrasts and, later, T2's (note lines 213 and 217).

    movement-v1's rule: Var = G / (G - 1) * sum over dates g of (sum over A in g of (x - m_A) / n_A - sum over B in
    g of (x - m_B) / n_B)^2, G the dates holding a bet of either side, t on G - 1 degrees of freedom. pilot.cr1 on
    the rows total * (x - m_A) / n_A and -total * (x - m_B) / n_B (total = n_A + n_B) gives exactly that: the rows
    sum to 0, so its mean is 0 and its SE^2 is the rule's Var. The interval is shown only when each side has at
    least min_clusters dates (note line 217: "on each side"); a side with no bet has G 0 and no estimate.

    (inference, spec S4) That pilot.cr1 on these rows is movement-v1's "CR1 variance of a difference", with t on
    G_either - 1 degrees of freedom as the rule's text says, is for Astra to confirm. (inference, spec S5) A side
    with no bet gives no estimate and "too few days"."""
    measure = _measure(measure)
    a = [(bet["utc_day"], _exact(bet[measure])) for bet in first]
    b = [(bet["utc_day"], _exact(bet[measure])) for bet in second]
    n_a, n_b = len(a), len(b)
    days_a, days_b = {day for day, _ in a}, {day for day, _ in b}
    m_a, m_b = _mean([x for _, x in a]), _mean([x for _, x in b])
    estimate = None if n_a == 0 or n_b == 0 else m_a - m_b
    if len(days_a) < contract.min_clusters or len(days_b) < contract.min_clusters:
        interval = {"shown": False, "reason": "too few days"}
    else:
        total = n_a + n_b
        rows = ([(day, total * (x - m_a) / n_a) for day, x in a]
                + [(day, -total * (x - m_b) / n_b) for day, x in b])
        interval = _interval(_cr1(rows, contract), estimate)
    return {"n": (n_a, n_b), "G": (len(days_a), len(days_b)), "G_either": len(days_a | days_b),
            "estimate": estimate, "interval": interval}


# ---------------------------------------------------------------------------------------------------------------
# T1, k_outcome by price band (note lines 203 to 209).

def _ledger_check(bets, *, contract):
    """The ledger check (note line 208), only on bets in ledger part S whose S value is our own net: every LOSER,
    and every WINNER valued at our net (the S value is the lower of our net and Betfair's figure, pilot.s_winner_value,
    so a lower value means Betfair's figure was used). Residual = S value / final matched size - r, in pence over
    pence, with range above -2 / s and at or below 0. Bets still in W and S WINNERs valued at Betfair's figure are
    counted apart. An out-of-range residual changes only this check.

    (inference) The mean residual is unweighted, as the note's other means are (line 205); the note says only "the
    mean residual", and a size-weighted mean would differ whenever a partial fill is checked."""
    rate = contract.commission_rate
    checked = outside = in_w = at_betfair = 0
    residuals = []
    for bet in bets:
        if bet["ledger_part"] == "W":
            in_w += 1
            continue
        size, value_p = bet["size"], bet["ledger_value_p"]
        if bet["win"] == 1 and value_p < pilot.s_winner_value(size, bet["P"], 0, c=rate)["net_p"]:
            at_betfair += 1
            continue
        s = _exact(size) * 100                              # the final matched size in pence
        residual = Fraction(value_p) / s - _exact(bet["r"])
        checked += 1
        residuals.append(residual)
        if not -2 / s < residual <= 0:
            outside += 1
    return {"checked": checked, "mean_residual": _mean(residuals), "outside_range": outside, "in_w": in_w,
            "s_winner_at_betfair_figure": at_betfair}


def _t1_row(bets, *, contract):
    figures = group_figures(bets, contract=contract, intervals=("k_outcome",))
    values = [_exact(bet["k_outcome"]) for bet in bets]
    if len(values) < 2:
        variance = root = None
    else:
        centre = figures["mean_k_outcome"]
        variance = sum(((x - centre) ** 2 for x in values), Fraction(0)) / (len(values) - 1)    # sample, n - 1
        root = _root(variance)
    return {"n": figures["n"], "G": figures["G"], "mean_nclv": figures["mean_nclv"],
            "mean_k_outcome": figures["mean_k_outcome"], "mean_r": figures["mean_r"],
            "means_add_up": figures["means_add_up"], "var_k_outcome": variance, "sd_k_outcome": root,
            "interval_k_outcome": figures["interval_k_outcome"], "ledger": _ledger_check(bets, contract=contract)}


def _t1(bets, *, contract):
    # (inference, spec S2) Each band's row and the pooled row carry the ledger check, because the check's bullet
    # stands under "Per band and pooled" (note line 203). (inference, spec S6) A hand-built bet in no band is
    # counted in no_band and stays in the pooled row; the fold cannot produce one.
    rows = {name: _t1_row([bet for bet in bets if bet["band"] == name], contract=contract)
            for name, _, _ in PRICE_BANDS_H}
    rows["pooled"] = _t1_row(list(bets), contract=contract)     # every k_outcome bet, a bet in no band included
    return {"computed": True, "rows": rows, "no_band": sum(1 for bet in bets if bet["band"] is None)}


# ---------------------------------------------------------------------------------------------------------------
# T3, day types with a Wednesday placebo (note lines 216 to 234).

def _t3(bets, *, contract):
    groups = {name: [bet for bet in bets if bet["day_row"] == name] for name in T3_ROWS}
    # (inference, spec S1) Each row carries an interval for mean NCLV and for mean k_outcome, the two measures the
    # contrasts are of, and none for mean r: the note says "for a row, in that row" (line 217), not for which means.
    rows = {name: group_figures(groups[name], contract=contract, intervals=_MEASURES) for name in T3_ROWS}
    attention = [bet for bet in bets if bet["day_row"] in _H_ROWS]
    weekdays = groups["OTHER_WEEKDAYS_NOT_H"]
    # Sunday, not H is shown on its own and is in no contrast (note line 217).
    contrasts = {"main": {m: contrast(attention, weekdays, m, contract=contract) for m in _MEASURES},
                 "placebo": {m: contrast(groups["WEDNESDAY_NOT_H"], weekdays, m, contract=contract)
                             for m in _MEASURES}}
    marked = {}
    for m in _MEASURES:
        main, placebo = contrasts["main"][m]["estimate"], contrasts["placebo"][m]["estimate"]
        # A placebo at least half the main one, in absolute value and for the same measure, is marked beside the
        # main one, for people to read (note line 217). (inference, spec S5) Read literally, a main estimate of
        # exactly 0 is always marked, 0 against 0 included; with no estimate on a side there is no mark.
        marked[m] = None if main is None or placebo is None else 2 * abs(placebo) >= abs(main)
    unconfirmed = tuple(name + ", " + course for name, course, _, _, confirmed in FESTIVALS if not confirmed)
    # (inference, spec 5.6 and S10) The bets whose London date lies outside the registered days (note line 219),
    # counted so that a reader sees how many fall under the rule for days before 2026-12-01.
    outside = sum(1 for bet in bets if bet["london_day"] < T3_FIRST_DAY or bet["london_day"] > T3_LAST_DAY)
    return {"computed": True, "rows": rows, "contrasts": contrasts, "placebo_marked": marked,
            "unconfirmed_festivals": unconfirmed, "bets_outside_registered_days": outside}


# ---------------------------------------------------------------------------------------------------------------
# The entry point (note line 196: same run as contract p1's frozen analysis).

def _not_computed(name):
    return {"computed": False, "reason": "input missing: " + name}


def report_tables(state, *, contract, now_ns, t2):
    """T1 to T3 on a folded state at the data lock. The one entry point the analysis command calls. t2 is a key
    of T2_STATES and has no default.

    {"bets": {"source", "n_reached", "eligible", "k_outcome_bets", "excluded", "not_in", "missing"}, "T1", "T2",
    "T3"}. A table whose input is missing is {"computed": False, "reason": ...}; the others are still computed
    (note line 196). The result holds no ref, market id or selection id."""
    _refuse(state, contract=contract, now_ns=now_ns)
    if not isinstance(t2, str) or t2 not in T2_STATES:
        raise pilot.PilotError("tables_input_invalid")
    selection = _selection(state, contract=contract, now_ns=now_ns)
    missing, bets = selection["missing"], selection["bets"]
    formed = missing["bets"] is None
    summary = {"source": selection["source"], "n_reached": selection["n_reached"],
               "eligible": selection["eligible"], "k_outcome_bets": len(bets) if formed else None,
               "excluded": dict(selection["excluded"]), "not_in": dict(selection["not_in"]),
               "missing": dict(missing)}
    if not formed:
        t1 = _not_computed(missing["bets"])
        t3 = _not_computed(missing["bets"])
    else:
        t1 = _not_computed(missing["T1"]) if missing["T1"] is not None else _t1(bets, contract=contract)
        t3 = _not_computed(missing["T3"]) if missing["T3"] is not None else _t3(bets, contract=contract)
    return {"bets": summary, "T1": t1, "T2": {"computed": False, "reason": T2_STATES[t2], "state": t2}, "T3": t3}


# ---------------------------------------------------------------------------------------------------------------
# The terminal lines. Display rounding only: round(value * 10**places) on the Fraction, ties to even, no float.
# (inference, spec S8) The display is not registered: means, estimates and interval ends signed at 4 places, the SD
# unsigned at 4, residuals signed at 6, None as "-". The text is built from the rounded int, so its sign is the
# int's: a negative value that rounds to 0, and 0 itself, show as +0.0000.

def _fixed(value, places, *, signed):
    if value is None:
        return "-"
    scaled = round(_exact(value) * 10 ** places)
    sign = "-" if scaled < 0 else ("+" if signed else "")
    digits = str(abs(scaled)).rjust(places + 1, "0")
    return sign + digits[:-places] + "." + digits[-places:]


def _signed(value):
    return _fixed(value, 4, signed=True)


def _yes_no(value):
    return "-" if value is None else ("yes" if value else "no")


def _interval_text(interval):
    if not interval["shown"]:
        return interval["reason"]
    return _signed(interval["low"]) + " to " + _signed(interval["high"])


def _price_text(hundredths):
    return str(hundredths // 100) + "." + str(hundredths % 100).rjust(2, "0")


def _columns(header, rows, right):
    """Aligned text lines: each cell padded to its column's widest cell, two spaces apart; the columns whose index
    is in right are right-aligned. Trailing spaces are dropped."""
    table = [tuple(header)] + [tuple(row) for row in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    return ["  ".join(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
                      for i, cell in enumerate(row)).rstrip() for row in table]


def header_lines(selection):
    """The lines that say which bets the tables use, from a k_outcome_bets or report_tables()["bets"] value
    (note line 198), so that T2's own run can say it in the same words."""
    eligible = selection["eligible"]
    if selection["n_reached"]:
        which = "Bets: primary sample, the first " + str(eligible) + " eligible bets."
    else:
        which = "Bets: N not reached; every eligible bet of the analysis population (" + str(eligible) + ")."
    shared = selection["missing"]["bets"]
    if shared is not None:
        formed = "k_outcome bets: not formed: input missing: " + shared
    else:
        count = selection["k_outcome_bets"] if "k_outcome_bets" in selection else len(selection["bets"])
        excluded = selection["excluded"]
        formed = ("k_outcome bets: " + str(count) + " of " + str(eligible) + ". Not in them: dead heat (two or more "
                  "WINNERs) " + str(excluded["dead_heat"]) + "; no WINNER " + str(excluded["no_winner"]) + ".")
    not_in = selection["not_in"]
    never = ("Never in them: PT4 records " + str(not_in["pt4_records"]) + "; surplus records "
             + str(not_in["surplus_records"]) + "; the decision-price series.")
    return ("Registered report tables T1-T3 (note §12). End only. They never decide, and nothing here is a test.",
            which, formed, never)


def _t1_lines(table):
    if not table["computed"]:
        return ["T1. k_outcome by price band: not computed: " + table["reason"]]
    limits = [(name, _price_text(low) + "-" + _price_text(high)) for name, low, high in PRICE_BANDS_H]
    limits.append(("pooled", _price_text(PRICE_BANDS_H[0][1]) + "-" + _price_text(PRICE_BANDS_H[-1][2])))
    figures = []
    ledger = []
    for name, limit in limits:
        row = table["rows"][name]
        figures.append((name, limit, str(row["n"]), str(row["G"]), _signed(row["mean_nclv"]),
                        _signed(row["mean_k_outcome"]), _signed(row["mean_r"]), _yes_no(row["means_add_up"]),
                        _fixed(row["sd_k_outcome"], 4, signed=False), _interval_text(row["interval_k_outcome"])))
        check = row["ledger"]
        ledger.append((name, str(check["checked"]), _fixed(check["mean_residual"], 6, signed=True),
                       str(check["outside_range"]), str(check["in_w"]), str(check["s_winner_at_betfair_figure"])))
    lines = ["T1. k_outcome by price band (decision limit price)"]
    lines += _columns(("band", "limit price", "n", "G (UTC days)", "mean NCLV", "mean k_outcome", "mean r",
                       "adds up (exact)", "SD k_outcome", "95% interval, mean k_outcome"), figures,
                      right={2, 3, 4, 5, 6, 8})
    if table["no_band"]:
        lines.append("Bets with a limit price in no band: " + str(table["no_band"]) + " (should be 0).")
    lines.append("The pooled mean NCLV is over the k_outcome bets; it can differ from contract p1's primary mean.")
    lines.append("Intervals are descriptive (CR1 with t(G - 1), two-sided 95%); they test nothing.")
    lines.append("Ledger check: part S, valued at our own net. Residual = S value / final matched size - r; range: "
                 "above -2/s, at or below 0.")
    lines += _columns(("band", "checked", "mean residual", "outside range (should be 0)", "still in W",
                       "S WINNER at Betfair's figure"), ledger, right={1, 2, 3, 4, 5})
    return lines


def _t2_lines(table):
    if table["computed"]:
        raise pilot.PilotError("tables_input_invalid")      # T2's cells are rendered by T2's own run, not here
    lines = ["T2. Late-move cells: not computed: " + table["reason"]]
    if table.get("state") == "decided_not_bought":
        # (inference, spec S10) A line the note does not ask for; it names the departure of note line 212.
        lines.append("Departure to report: a decision to buy T2's files was not carried out (note §12).")
    elif table.get("state") == "awaiting_files":
        # (inference, spec YA1) The provisional third state's line.
        lines.append("T2 runs once, in its own terminal run, when the files are held (note §12).")
    return lines


_CONTRAST_LABELS = (("main", "main: H minus other weekdays"),
                    ("placebo", "placebo: non-H Wednesday minus other weekdays"))
_MEASURE_LABELS = (("nclv", "NCLV"), ("k_outcome", "k_outcome"))


def _t3_lines(table):
    if not table["computed"]:
        return ["T3. Day types with a Wednesday placebo: not computed: " + table["reason"]]
    rows = []
    for name in T3_ROWS:
        row = table["rows"][name]
        rows.append((T3_ROW_LABELS[name], str(row["n"]), str(row["G"]), _signed(row["mean_nclv"]),
                     _interval_text(row["interval_nclv"]), _signed(row["mean_k_outcome"]),
                     _interval_text(row["interval_k_outcome"]), _signed(row["mean_r"]),
                     _yes_no(row["means_add_up"])))
    contrasts = []
    for key, label in _CONTRAST_LABELS:
        for measure, measure_label in _MEASURE_LABELS:
            found = table["contrasts"][key][measure]
            # (inference, spec S9) "marked beside the main one" (note line 217): a column of the main contrast's
            # own row, per measure, with the placebo rows directly below.
            mark = _yes_no(table["placebo_marked"][measure]) if key == "main" else ""
            contrasts.append((label, measure_label, _signed(found["estimate"]), _interval_text(found["interval"]),
                              str(found["n"][0]) + ", " + str(found["n"][1]),
                              str(found["G"][0]) + ", " + str(found["G"][1]) + "; " + str(found["G_either"]), mark))
    lines = ["T3. Day types with a Wednesday placebo (Europe/London date of the off)"]
    lines += _columns(("row", "n", "G (UTC days)", "mean NCLV", "95% interval", "mean k_outcome", "95% interval",
                       "mean r", "adds up (exact)"), rows, right={1, 2, 3, 5, 7})
    lines += _columns(("contrast", "measure", "estimate", "95% interval", "n (each side)", "G (each side; either)",
                       "placebo at least half"), contrasts, right={2})
    if table["unconfirmed_festivals"]:
        lines.append("Festival rows still unconfirmed, registered dates kept: "
                     + "; ".join(table["unconfirmed_festivals"]) + ".")
    # (inference, spec S10) A line the note does not ask for; it states the count of spec 5.6.
    lines.append("Bets on days before " + T3_FIRST_DAY + " or after " + T3_LAST_DAY + ": "
                 + str(table["bets_outside_registered_days"]) + ".")
    return lines


def render_tables(tables):
    """The terminal lines of a report_tables result, as a tuple of str. Prints nothing: the analysis command
    prints them. No line holds a market id, selection id, bet id or order ref (note line 196)."""
    lines = list(header_lines(tables["bets"]))
    for block in (_t1_lines(tables["T1"]), _t2_lines(tables["T2"]), _t3_lines(tables["T3"])):
        lines.append("")
        lines += block
    return tuple(lines)
