"""Pure Pilot 2 logic for the plain capped bot (P2-LP): the constants, the runner rule and the leg prices, the screen,
each leg's choice, the race counts, the gateway's leg refusals, the ARM line and record, config-p2's pins, the handoff
and the summary.

docs/betfair-pilot-2-policy.md (the page) holds the numbers in its Values and Money tables, and the constants below
are their only other copy (tests/test_betfair_p2_lp.py pins each to its row); docs/betfair-pilot-2-engineering.md
(the note) holds the detail and is cited by section. There is no contract, no record type and no fold of Pilot 2's
own: every count comes from the core fold's order records with ref p2-lp (journal.py:758-777), and the ARM record is
the core kind operator_confirm, which the fold ignores (note §7). Every book is a validated snapshot
(schemas.validate_book_snapshot): ladders best first, prices as Betfair's decimals, read here as int hundredths.

Nothing here reads a clock, a file, the network, the environment or a random source, and nothing here can send: every
time comes in as an argument (now_ns, a book's observed_at and market_time, each order's submitted_at), and the
journal as the event dicts and the core fold the caller read. The gateway's lp_place and p2-run (PR-2) do the I/O.
This module imports only pilot (timestamps and the UTC day) and ticks.

What this cannot show: that the caller fetched a fresh book, passed the journal it read, or sent what was chosen.
"""

from . import pilot
from .ticks import is_on_ladder, tick_index, tick_offset, to_hundredths, to_pence

NS = pilot.NS
# Only the go-live commit, which also applies the page's AGENTS.md, RS-19 and order-path lines, sets this True.
IN_FORCE = False
STRATEGY_REF = "p2-lp"
SOURCE = "pilot_lp"
SIDES = ("BACK", "LAY")
COUNTRIES = ("GB", "IE")
# The page's Values and Money rows, by constant name (page, One race and Money).
STAKE_P = 200
MAX_LAY_H = 600
BAND_H = (150, 590)
SPREAD_TICKS = (3, 4)
MIN_ACTIVE = 5
PAIR_WORST_P = 1000
SCREEN_S = (-840, -780)
OPEN_S = -740
DECIDE_S = (-590, -575)
LAY_BOOK_BY_S = -532
PULL_S = -300
RACES_DAY = 5
RACES_TOTAL = 10
SKIP_STREAK = 5
SLICE_S = 1
LATE_WAKE_S = 5
ARM_LINE = "P2 ARM {cfg12} {day} UNTIL {hhmm}Z RACES {n}"
ARM_KEYS = frozenset({"session_id", "config_sha256", "utc_day", "until_ns", "races", "code_commit"})
# config-p2.json's exact values (note §9): loosening one needs a code change and Astra's review. Money in pounds and
# seconds as the config writes them; orders.pilot must be absent (config.py:436-441 then gives every order p2-lp).
CONFIG_PINS = (
    (("orders", "customer_strategy_ref"), STRATEGY_REF),
    (("orders", "caps", "max_stake_per_order"), 2.0),
    (("orders", "caps", "max_liability_per_order"), 10.0),
    (("orders", "caps", "max_market_exposure"), 10.0),
    (("orders", "caps", "max_total_exposure"), 20.0),
    (("orders", "caps", "max_daily_loss"), 20.0),
    (("orders", "caps", "max_orders_per_minute"), 2),
    (("orders", "caps", "max_orders_per_hour"), 12),
    (("orders", "caps", "max_orders_per_day"), 10),
    (("orders", "caps", "max_instructions_per_request"), 1),
    (("orders", "caps", "max_price_deviation_ticks"), 3),
    (("orders", "caps", "max_book_age_s"), 2.0),
    (("orders", "caps", "max_minutes_before_off"), 10),
    (("orders", "rules", "market_types"), ["WIN"]),
    (("orders", "rules", "event_type_ids"), ["7"]),
    (("orders", "rules", "external_exposure_tolerance"), 0.0),
    (("orders", "live", "heartbeat_api_enabled"), False),
    (("read", "app_key_slot"), "delayed"))
# schemas.UNRESOLVED_STATES and schemas.TERMINAL_STATES, copied: this module imports only pilot and ticks (build
# spec §1). The test pins each copy to its original.
UNRESOLVED_STATES = frozenset({"SUBMITTING", "UNKNOWN", "ABSENT_UNCONFIRMED", "CANCEL_PENDING", "REPLACE_PENDING"})
TERMINAL_STATES = frozenset({"REFUSED", "FAILED", "ABSENT_ACCEPTED", "LAPSED", "REPLACED", "SETTLED", "VOIDED"})
PLACED_STATES = ("EXECUTABLE", "EXECUTION_COMPLETE")
_MISSING = object()


# ---------------------------------------------------------------------------------------------------------------
# The runner rule, the leg prices and the passive check (note §4), carried from the parked draft (p2-pure
# pilot_lp.py:666-709, :800-807) and read from the snapshot instead of raw bodies and a contract.

def _best_h(levels):
    """The best price of one snapshot ladder side (best first) as int hundredths, or None when the side is empty or
    the price is not a whole number of hundredths."""
    if not levels:
        return None
    try:
        return to_hundredths(levels[0][0])
    except ValueError:
        return None


def runner_band(runner):
    """The runner rule for one snapshot runner. In band: ACTIVE, best back b and best lay l both on the CLASSIC
    ladder, b < l, and BAND_H[0] <= b <= BAND_H[1]. Eligible: in band with l SPREAD_TICKS[0] to SPREAD_TICKS[1]
    ticks above b. There is no depth predicate. A price off the ladder is None, and the spread is given only when
    both prices are on it and b < l."""
    back_h, lay_h = _best_h(runner["back"]), _best_h(runner["lay"])
    back_h = back_h if is_on_ladder(back_h) else None
    lay_h = lay_h if is_on_ladder(lay_h) else None
    spread = (tick_index(lay_h) - tick_index(back_h)
              if back_h is not None and lay_h is not None and back_h < lay_h else None)
    in_band = runner["status"] == "ACTIVE" and spread is not None and BAND_H[0] <= back_h <= BAND_H[1]
    return {"in_band": in_band, "eligible": in_band and SPREAD_TICKS[0] <= spread <= SPREAD_TICKS[1],
            "best_back_h": back_h, "best_lay_h": lay_h, "spread_ticks": spread}


def select_runner(book):
    """The runner the bot takes: no in-band runner is no_runner_in_band, in-band runners none of them eligible is
    spread_out_of_band, else the lowest (best back, best lay, selection id). A tie is never a SKIP."""
    bands = [(runner_band(runner), int(selection)) for selection, runner in book["runners"].items()]
    none = {"selection_id": None, "best_back_h": None, "best_lay_h": None}
    if not any(band["in_band"] for band, _ in bands):
        return dict(none, code="no_runner_in_band")
    eligible = [(band, selection) for band, selection in bands if band["eligible"]]
    if not eligible:
        return dict(none, code="spread_out_of_band")
    band, selection = min(eligible, key=lambda item: (item[0]["best_back_h"], item[0]["best_lay_h"], item[1]))
    return {"code": None, "selection_id": selection, "best_back_h": band["best_back_h"],
            "best_lay_h": band["best_lay_h"]}


def leg_prices(best_back_h, best_lay_h):
    """{"BACK": best lay - 1 tick, "LAY": best back + 1 tick}, exact across band edges. The BACK must stay above the
    LAY (V8's self-match rule), else PilotError("leg_prices_invalid"); a LAY above MAX_LAY_H raises
    PilotError("lay_price_above_cap"), which BAND_H[1] + 1 tick == MAX_LAY_H makes unreachable."""
    if not (is_on_ladder(best_back_h) and is_on_ladder(best_lay_h) and best_back_h < best_lay_h):
        raise pilot.PilotError("leg_prices_invalid")
    back, lay = tick_offset(best_lay_h, -1), tick_offset(best_back_h, 1)
    if not lay < back:
        raise pilot.PilotError("leg_prices_invalid")
    if lay > MAX_LAY_H:
        raise pilot.PilotError("lay_price_above_cap")
    return {"BACK": back, "LAY": lay}


def passive_check(*, best_back_h, best_lay_h, side, price_h):
    """None, or leg_not_passive, for one leg on its own send book: passive iff best back < leg price < best lay,
    strictly, with our own resting BACK counted in the book the caller read. A missing book side or a non-int price
    refuses (fail closed). It only refuses: it never returns another price."""
    if (side not in SIDES or type(price_h) is not int or type(best_back_h) is not int or type(best_lay_h) is not int
            or not best_back_h < price_h < best_lay_h):
        return "leg_not_passive"
    return None


# ---------------------------------------------------------------------------------------------------------------
# The schedule, the screen and each leg's choice (note §4).

def schedule_row_ok(row):
    """None when a catalogue row (MARKET_DESCRIPTION projected) may be screened: bettingType ODDS and
    turnInPlayEnabled exactly true, so LAPSE acts at the turn in play (note §8); else betting_type_not_odds or
    turn_in_play_not_enabled."""
    description = row.get("description") if isinstance(row, dict) else None
    if not isinstance(description, dict) or description.get("bettingType") != "ODDS":
        return "betting_type_not_odds"
    if description.get("turnInPlayEnabled") is not True:
        return "turn_in_play_not_enabled"
    return None


def book_problems(book, *, live):
    """The first market problem's code, or None: OPEN (market_not_open), not in play (market_inplay) and at least
    MIN_ACTIVE ACTIVE runners (too_few_active_runners); on a live send book also isMarketDataDelayed false
    (book_delayed) and betDelay 0 (bet_delay_not_zero). The V-rules check neither the bet delay nor the ACTIVE
    count (validate.py:104-140, :280)."""
    if book["status"] != "OPEN":
        return "market_not_open"
    if book["inplay"] is not False:
        return "market_inplay"
    if sum(1 for runner in book["runners"].values() if runner["status"] == "ACTIVE") < MIN_ACTIVE:
        return "too_few_active_runners"
    if live and book["is_market_data_delayed"] is not False:
        return "book_delayed"
    if live and book["bet_delay"] != 0:
        return "bet_delay_not_zero"
    return None


def screen(book):
    """SCREEN's code on the delayed-key book, or None: the book must say isMarketDataDelayed true (not_delayed: false
    or missing, so a live-key book is never read here), then book_problems and the runner rule."""
    if book["is_market_data_delayed"] is not True:
        return "not_delayed"
    return book_problems(book, live=False) or select_runner(book)["code"]


def _off_ns(book):
    return None if book["market_time"] is None else pilot.parse_utc_ns(book["market_time"])


def _instruction(selection_id, side, price_h):
    """A V1 instruction: STAKE_P, LIMIT (the request body's), LAPSE, handicap 0."""
    return {"selection_id": selection_id, "handicap": 0, "side": side, "price_h": price_h, "size_p": STAKE_P,
            "persistence_type": "LAPSE"}


def choose_back(book, *, country):
    """The BACK on its own send book -> (instruction, pending, None), or (None, None, code). country is the event
    country of the catalogue row SCREEN stored (the snapshot has none): GB or IE, else p2_country (None refuses).
    The off is the book's market_time; observed_at must lie in [off + DECIDE_S[0], off + DECIDE_S[1]] (p2_window).
    Then book_problems, the runner rule, the leg prices and the passive check for the BACK. pending is what the LAY
    needs: {market_id, selection_id, lay_price_h}."""
    if country not in COUNTRIES:
        return None, None, "p2_country"
    off_ns = _off_ns(book)
    if off_ns is None or not (off_ns + DECIDE_S[0] * NS <= pilot.parse_utc_ns(book["observed_at"])
                              <= off_ns + DECIDE_S[1] * NS):
        return None, None, "p2_window"
    code = book_problems(book, live=True)
    if code is not None:
        return None, None, code
    runner = select_runner(book)
    if runner["code"] is not None:
        return None, None, runner["code"]
    prices = leg_prices(runner["best_back_h"], runner["best_lay_h"])
    code = passive_check(best_back_h=runner["best_back_h"], best_lay_h=runner["best_lay_h"], side="BACK",
                         price_h=prices["BACK"])
    if code is not None:
        return None, None, code
    pending = {"market_id": book["market_id"], "selection_id": runner["selection_id"], "lay_price_h": prices["LAY"]}
    return _instruction(runner["selection_id"], "BACK", prices["BACK"]), pending, None


def choose_lay(book, pending):
    """The LAY on a fresh send book -> (instruction, None), or (None, code). pending is the placed BACK's (None, or
    another market, is p2_lay_without_back). observed_at must be at or before off + LAY_BOOK_BY_S
    (p2_send_deadline); then book_problems on the live book, the runner ACTIVE (runner_not_active), and the passive
    check for the LAY on this book, our resting BACK included."""
    if pending is None or pending["market_id"] != book["market_id"]:
        return None, "p2_lay_without_back"
    off_ns = _off_ns(book)
    if off_ns is None or pilot.parse_utc_ns(book["observed_at"]) > off_ns + LAY_BOOK_BY_S * NS:
        return None, "p2_send_deadline"
    code = book_problems(book, live=True)
    if code is not None:
        return None, code
    runner = book["runners"].get(str(pending["selection_id"]))
    if runner is None or runner["status"] != "ACTIVE":
        return None, "runner_not_active"
    code = passive_check(best_back_h=_best_h(runner["back"]), best_lay_h=_best_h(runner["lay"]), side="LAY",
                         price_h=pending["lay_price_h"])
    if code is not None:
        return None, code
    return _instruction(pending["selection_id"], "LAY", pending["lay_price_h"]), None


# ---------------------------------------------------------------------------------------------------------------
# Counts, the ARM record and the gateway's leg refusals (note §6, §7).

def races(orders, *, since_ns=None, day=None):
    """The markets of p2-lp orders in the core fold's orders that have a submitted_at: a race counts from its first
    submit (page, Money). since_ns keeps orders submitted at or after it (one session, from its ARM record); day keeps
    those submitted on that UTC day ('YYYY-MM-DD')."""
    found = set()
    for order in orders.values():
        if order["customer_strategy_ref"] != STRATEGY_REF or order["submitted_at"] is None:
            continue
        at = pilot.parse_utc_ns(order["submitted_at"])
        if (since_ns is None or at >= since_ns) and (day is None or pilot.utc_day(at) == day):
            found.add(order["market_id"])
    return found


def arm_line(cfg12, day, hhmm, races):
    """The typed ARM line: cfg12 is config-p2's sha256[:12], day the UTC day 'YYYY-MM-DD', hhmm UNTIL's 'HHMM'."""
    return ARM_LINE.format(cfg12=cfg12, day="".join(day.split("-")), hhmm=hhmm, n=races)


def arm_payload(line, *, session_id, config_sha256, utc_day, until_ns, races, code_commit):
    """The ARM record's operator_confirm payload (note §7): the typed line as its echo and the session it binds."""
    return {"echo": line, "typed_before_lock": True,
            "p2_arm": {"session_id": session_id, "config_sha256": config_sha256, "utc_day": utc_day,
                       "until_ns": until_ns, "races": races, "code_commit": code_commit}}


def _arm(event):
    """The p2_arm part of a P2 ARM record (an operator_confirm event with a readable recorded_at whose p2_arm has
    exactly ARM_KEYS, an int until_ns and an int races), else None."""
    if not isinstance(event, dict) or event.get("kind") != "operator_confirm":
        return None
    payload = event.get("payload")
    inner = payload.get("p2_arm") if isinstance(payload, dict) else None
    if (not isinstance(inner, dict) or set(inner) != ARM_KEYS or type(inner["until_ns"]) is not int
            or type(inner["races"]) is not int):
        return None
    try:
        pilot.parse_utc_ns(event.get("recorded_at"))
    except pilot.PilotError:
        return None
    return inner


def arm_records(events, *, day=None):
    """The P2 ARM records among events, in order; day keeps those whose utc_day is that UTC day."""
    return [event for event in events if _arm(event) is not None and (day is None or _arm(event)["utc_day"] == day)]


def arm_problem(event, *, config_sha256, session_id, now_ns):
    """None when event is this session's ARM record and still binds at now_ns, else the first of p2_arm_missing,
    p2_arm_other_config, p2_arm_other_session, p2_arm_not_today and p2_arm_expired (now at or after UNTIL)."""
    inner = _arm(event)
    if inner is None:
        return "p2_arm_missing"
    if inner["config_sha256"] != config_sha256:
        return "p2_arm_other_config"
    if inner["session_id"] != session_id:
        return "p2_arm_other_session"
    if inner["utc_day"] != pilot.utc_day(now_ns):
        return "p2_arm_not_today"
    if not now_ns < inner["until_ns"]:
        return "p2_arm_expired"
    return None


def leg_refusal(*, side, market_id, instruction, orders, others, p1_day, arm_event, arm_events_today, lp, pair,
                off_ns, config_sha256, now_ns):
    """The gateway's check for one leg on the submit's basis read (note §6), or None. orders is the core fold's;
    others the refs of our open or unsettled orders (for a LAY, not this open's legs); p1_day Pilot 1's
    session_start of today, or None; arm_event the event at lp["arm_seq"] and arm_events_today the P2 ARM records of
    today; lp {arm_seq, session_id, races}; pair the placed BACK's pending, or None; off_ns the send book's off. In
    order: the ARM record (arm_problem, then p2_arm_already_today for an earlier one today), p2_pilot_1_day; for a BACK
    p2_market_used, p2_until_before_pull, p2_races_total, p2_races_today, p2_races_session, p2_other_bet_open; for a
    LAY p2_lay_without_back (unless this pair's BACK rests, or has matched), p2_lay_already_sent, p2_lay_not_pair,
    p2_other_bet_open; then p2_leg_shape."""
    code = arm_problem(arm_event, config_sha256=config_sha256, session_id=lp["session_id"], now_ns=now_ns)
    if code is not None:
        return code
    if any(event["seq"] < arm_event["seq"] for event in arm_events_today):
        return "p2_arm_already_today"
    if p1_day is not None:
        return "p2_pilot_1_day"
    arm = _arm(arm_event)
    mine = [order for order in orders.values()
            if order["customer_strategy_ref"] == STRATEGY_REF and order["market_id"] == market_id]
    if side == "BACK":
        if mine:
            return "p2_market_used"
        if off_ns is None or arm["until_ns"] < off_ns + PULL_S * NS:
            return "p2_until_before_pull"
        if len(races(orders)) >= RACES_TOTAL:
            return "p2_races_total"
        if len(races(orders, day=pilot.utc_day(now_ns))) >= RACES_DAY:
            return "p2_races_today"
        if len(races(orders, since_ns=pilot.parse_utc_ns(arm_event["recorded_at"]))) >= arm["races"]:
            return "p2_races_session"
        if others:
            return "p2_other_bet_open"
    elif side == "LAY":
        if pair is None or pair["market_id"] != market_id or not any(
                order["side"] == "BACK" and order["selection_id"] == pair["selection_id"]
                and (order["state"] == "EXECUTABLE"
                     or (order["state"] == "EXECUTION_COMPLETE" and order["size_matched_p"] > 0)) for order in mine):
            return "p2_lay_without_back"
        if any(order["side"] == "LAY" for order in mine):
            return "p2_lay_already_sent"
        if instruction["selection_id"] != pair["selection_id"] or instruction["price_h"] != pair["lay_price_h"]:
            return "p2_lay_not_pair"
        if others:
            return "p2_other_bet_open"
    if (side not in SIDES or instruction["side"] != side or instruction["size_p"] != STAKE_P
            or instruction["persistence_type"] != "LAPSE" or instruction["handicap"] != 0):
        return "p2_leg_shape"
    return None


# ---------------------------------------------------------------------------------------------------------------
# config-p2's pins (note §9).

def _value(config, path):
    for key in path:
        if not isinstance(config, dict) or key not in config:
            return _MISSING
        config = config[key]
    return config


def config_problems(config):
    """Every config-p2 pin a validated config breaks, in CONFIG_PINS order, as p2_config_<key>; p2_config_pilot
    first when orders.pilot is present, and p2_not_in_force before all while IN_FORCE is False (note §1). [] means it
    is config-p2 exactly as note §9 pins it, and in force."""
    found = [] if IN_FORCE else ["p2_not_in_force"]
    if _value(config, ("orders", "pilot")) is not _MISSING:
        found.append("p2_config_pilot")
    for path, pinned in CONFIG_PINS:
        value = _value(config, path)
        same = (value is pinned) if isinstance(pinned, bool) else (type(value) is not bool and value == pinned)
        if not same:
            found.append(f"p2_config_{path[-1]}")
    return found


# ---------------------------------------------------------------------------------------------------------------
# The handoff and the summary (note §8, §10).

def _money(pence):
    sign = "-" if pence < 0 else ""
    return f"{sign}£{abs(pence) // 100}.{abs(pence) % 100:02d}"


def _leg_text(ref, order, market_id):
    if order is None:
        return f"p2-lp leg {ref} in {market_id or 'unknown market'}: its side, price and state could not be read"
    price = f"{order['price_h'] // 100}.{order['price_h'] % 100:02d}"
    return (f"p2-lp leg {order['bet_id'] or ref}: {order['side']} {_money(order['size_p'])} @ {price} on runner "
            f"{order['selection_id']} in {order['market_id']}, state {order['state']}")


def handoff_lines(legs, *, checkout, market_id=None):
    """The handoff (note §8): one line per leg of legs ({ref: the core fold's order record, or None when the fold
    could not be read}; market_id names the market of a None leg), then the stops in order: the Betfair phone app,
    the website, and the own-scope kill run from Pilot 2's checkout by its absolute path."""
    return [_leg_text(ref, order, market_id) for ref, order in legs.items()] + [
        "These legs may still be open. Stop them now, in this order:",
        "1. the Betfair phone app: cancel each unmatched part;",
        "2. the Betfair website: cancel each unmatched part;",
        f"3. the own-scope kill from Pilot 2's checkout: cd \"{checkout}\" && python3 -m betangel_research.betfair "
        f"kill --reason \"p2 handoff\"",
        "A kill from Pilot 1's checkout misses these legs when the journal cannot be read."]


def _open_or_unsettled(order):
    """pilot_gate.open_manual_refs's rule, copied (pilot_gate runs git): not RECORDED, and either matched and not
    SETTLED or VOIDED, or neither terminal nor EXECUTION_COMPLETE with nothing matched."""
    if order["state"] == "RECORDED":
        return False
    if (order["size_matched_p"] or 0) > 0 and order["state"] not in ("SETTLED", "VOIDED"):
        return True
    return order["state"] not in TERMINAL_STATES and not (order["state"] == "EXECUTION_COMPLETE"
                                                           and order["size_matched_p"] == 0)


def _settled_worst_p(order):
    """journal.settled_worst_loss_pence, copied (journal opens SQLite): what settle-market books for one order, its
    matched worst case plus its recorded remainder at its own limit (BACK: stake; LAY: liability, rounded up)."""
    matched, remaining = order["size_matched_p"], order["size_remaining_p"]
    if matched <= 0:
        worst = 0
    elif order["side"] == "BACK":
        worst = matched
    else:
        worst = -((-matched * (max(order["average_price_matched_h"] or 0, order["price_h"]) - 100)) // 100)
    if remaining <= 0:
        return worst
    return worst + (remaining if order["side"] == "BACK" else -((-remaining * (order["price_h"] - 100)) // 100))


def _counts(legs, *, sessions, races):
    def by_side(test):
        return {side: sum(1 for order in legs if order["side"] == side and test(order)) for side in SIDES}

    def refused(order):
        return order["state"] in ("REFUSED", "FAILED")

    codes = {}
    for order in filter(refused, legs):
        codes[order["error_code"] or "none"] = codes.get(order["error_code"] or "none", 0) + 1
    matched = {}
    for order in legs:
        if order["size_matched_p"] > 0:
            matched.setdefault(order["market_id"], set()).add(order["side"])
    return {"sessions": sessions, "races": races,
            "sent": by_side(lambda order: order["submitted_at"] is not None),
            "placed": by_side(lambda order: order["bet_id"] is not None),
            "refused": by_side(refused), "codes": dict(sorted(codes.items())),
            "matched": sum(1 for order in legs if order["size_matched_p"] > 0),
            "matched_full": sum(1 for order in legs if order["size_matched_p"] == order["size_p"]),
            "round_trips": sum(1 for sides in matched.values() if sides == set(SIDES)),
            "lone_backs": sum(1 for sides in matched.values() if sides == {"BACK"}),
            "lone_lays": sum(1 for sides in matched.values() if sides == {"LAY"}),
            "cancelled": sum(1 for order in legs if order["size_cancelled_p"] > 0),
            "lapsed": sum(1 for order in legs if order["state"] == "LAPSED" or order["size_lapsed_p"] > 0),
            "unresolved": sum(1 for order in legs if order["state"] in UNRESOLVED_STATES),
            "settled_worst_p": sum(_settled_worst_p(order) for order in legs if order["state"] == "SETTLED")}


def summary(events, orders):
    """p2-summary's counts (note §10) from the journal's events and the core fold's orders: {"total", "days",
    "races_left", "open", "matched_markets"}. A session is a P2 ARM record (by its utc_day), races come from races(),
    and a leg falls on the UTC day of its intent event. Per bucket: legs sent, placed and refused (REFUSED or FAILED,
    with their codes) by side; legs matched in part or in full; round trips, lone BACKs and lone LAYs by market; legs
    cancelled, lapsed and unresolved; and the worst case settle-market booked. open lists the legs still open or
    unsettled; matched_markets the markets with a matched leg, for --cleared."""
    legs = {ref: order for ref, order in orders.items() if order["customer_strategy_ref"] == STRATEGY_REF}
    leg_day = {event["customer_order_ref"]: pilot.utc_day(pilot.parse_utc_ns(event["recorded_at"]))
               for event in events if event.get("kind") == "intent" and event.get("customer_order_ref") in legs}
    arm_days = [_arm(event)["utc_day"] for event in arm_records(events)]
    days = {day: _counts([order for ref, order in sorted(legs.items()) if leg_day.get(ref) == day],
                         sessions=arm_days.count(day), races=len(races(orders, day=day)))
            for day in sorted(set(leg_day.values()) | set(arm_days))}
    total = len(races(orders))
    return {"total": _counts([order for _, order in sorted(legs.items())], sessions=len(arm_days), races=total),
            "days": days, "races_left": max(RACES_TOTAL - total, 0),
            "open": [order for _, order in sorted(legs.items()) if _open_or_unsettled(order)],
            "matched_markets": sorted({order["market_id"] for order in legs.values() if order["size_matched_p"] > 0})}


def _count_line(name, counts):
    def sides(key):
        return f"BACK {counts[key]['BACK']}, LAY {counts[key]['LAY']}"

    codes = ", ".join(f"{code} {n}" for code, n in counts["codes"].items()) or "none"
    return (f"{name}: sessions {counts['sessions']}, races {counts['races']}; legs sent {sides('sent')}; placed "
            f"{sides('placed')}; refused or failed {sides('refused')} (codes: {codes}); matched {counts['matched']} "
            f"({counts['matched_full']} in full); round trips {counts['round_trips']}, lone BACKs "
            f"{counts['lone_backs']}, lone LAYs {counts['lone_lays']}; cancelled {counts['cancelled']}, lapsed "
            f"{counts['lapsed']}, unresolved {counts['unresolved']}; booked at settle "
            f"{_money(-counts['settled_worst_p'])}")


def _pence(value):
    try:
        return to_pence(value)
    except ValueError:
        return None


def _cleared_lines(cleared, matched_markets):
    """Betfair's cleared rows (SETTLED, grouped by MARKET): profit and commission per market and in total, and
    "check the website" for a journal market not cleared, a cleared market not in the journal or a figure that
    cannot be read."""
    lines, profit, commission, seen = [], 0, 0, set()
    for row in cleared:
        market = row.get("marketId") if isinstance(row, dict) else None
        found = _pence(row.get("profit")) if isinstance(row, dict) else None
        if not isinstance(market, str) or found is None:
            lines.append(f"check the website: a cleared row for {market if isinstance(market, str) else 'a market'} "
                         f"has no readable profit")
            continue
        seen.add(market)
        fee = _pence(row.get("commission"))
        profit, commission = profit + found, commission + (fee or 0)
        lines.append(f"cleared {market}: profit {_money(found)}, commission "
                     f"{_money(fee) if fee is not None else 'not given'}")
        if market not in matched_markets:
            lines.append(f"check the website: {market} is cleared but has no matched p2-lp leg in the journal")
    lines += [f"check the website: {market} has a matched p2-lp leg but no cleared figure"
              for market in matched_markets if market not in seen]
    return lines + [f"cleared total: profit {_money(profit)}, commission {_money(commission)}"]


def summary_lines(summary, cleared=None, *, checkout):
    """The printed summary: the total, each UTC day, the handoff for open or unsettled legs, then, with cleared
    (Betfair's SETTLED rows grouped by MARKET), its profit and commission. Counts, codes and Betfair's figures only."""
    lines = [_count_line("total", summary["total"]) + f"; races left {summary['races_left']}"]
    lines += [_count_line(day, counts) for day, counts in summary["days"].items()]
    if summary["open"]:
        lines.append(f"{len(summary['open'])} p2-lp legs are open or unsettled; the next ARM refuses until each is "
                     f"cancelled or settled (settle-market after its result).")
        lines += handoff_lines({order["customer_order_ref"]: order for order in summary["open"]}, checkout=checkout)
    if cleared is not None:
        lines += _cleared_lines(cleared, summary["matched_markets"])
    return lines
