"""pilot-summary (s1, step 1): a read-only count of the registered pilot's live journal, in counts and codes only.

What it reads: the config the CLI loaded (as status loads it), the rule file that orders.pilot.contract_path names,
through the pilot context's read-only contract reader (from the checkout the command runs from), and the live journal,
once, through pilot_run.pilot_events_after (its own short-lived ?mode=ro connection). A journal file that does not
exist is tested for first (os.lstat) and reads as "absent", with every count zero. It folds once, with
pilot_run.recovery_projection, and returns a plain dict.

What it never does: open OrderJournal, append, take the gateway lock, the owner lock or any file lock of this package,
read the Keychain, build a confirmer, a gateway or a read session, log in, or create or write any file or directory of
its own. SQLite itself may create the journal's -wal and -shm side files beside it when it opens the read-only
connection, writes its read marks in the -shm and holds its own read locks while it reads. Importing pilot_run makes
Python load credentials, arming, auth, the journal writer and admission (pilot_run.py's own imports); this module
calls none of them.

What it returns: the fixed keys of TOP_KEYS, and for each session, oldest first, the fixed keys of SESSION_KEYS. A
session's window runs from its session_start note to the next session's session_start note, or the journal's end, so an
earlier session never counts a later session's stops. Every code printed is in a closed list built from the code's own
tables (pilot.SKIP_CODES, pilot.SCOPE_TABLE, pilot.AUTHORITY_CODES, pilot.R13_CODES, pilot.LEDGER_GAP_REASONS) and
from the stop, session-end and ending reasons the code writes; anything else is counted under "other". Never a market
id, bet id, selection id, order ref, session id or stop id (a pause's stop id carries a pence level), a price, any
pound or pence figure, the running total or a pause level. Sends and fills are counts, so a fill rate can be worked
out from them. Stops are counted by reason, so the number of pause stops, with the rule file's public pause step, shows
how many pause levels were reached, and so a band of the running total; a daily_loss code shows that the day stop was
reached.

place_records_left_today is today's session's day limit, from the fold's own capacity_and_pause, or, with no session
today, the limit the next session would get (a PT4 session while no PT4 receipt is journaled), less what today used,
and never more than what is left in all.

ended_by_own_end_note means only: the session was ended by exactly its own session_end stop, by the journal (the end
the release check calls (a), pilot_run.pilot_stop_check). It does not show that recording was released: the release
also needs facts the journal does not hold (no handoff due, not abnormal, every read session closed).
"""

import os
import re

from . import pilot
from .config import pilot_enabled
from .errors import JournalError
from .pilot_run import pilot_events_after, recovery_projection
from .schemas import UNRESOLVED_STATES

SCHEMA = "pilot-summary/v1"
OTHER = "other"
TOP_KEYS = ("schema", "contract_id", "contract_sha256_12", "journal", "place_records_used", "place_records_left",
            "place_records_today", "place_records_left_today", "placing_ended", "placing_ended_code",
            "session_armed_now", "sessions_total", "handoffs", "reads_owed", "review_stops_active", "due_stops",
            "sessions")
SESSION_KEYS = ("utc_day", "reservations", "place_records", "pt4_records", "sends", "fills", "cancels", "unknowns",
                "settles", "skips", "stops", "ended_reason", "ended_by_own_end_note")
REFUSAL_CODES = ("pilot_not_configured", "pilot_contract_unreadable", "pilot_contract_differs",
                 "pilot_journal_unreadable", "pilot_fold_failed")
CONTRACT_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,15}")

# The closed lists. A stop's reason is any [a-z0-9_:-]{1,80} (pilot.CODE_RE), and refusal_code turns any rule string,
# a Betfair error code included, into one, so only these are printed; anything else is "other".
SKIP_CODES = frozenset(pilot.SKIP_CODES)
STOP_SCOPES = frozenset(("session", "review"))
# Reasons the code writes outside the tables: the fold's implied stops (pilot.py), the executor's own stops and
# refusals (pilot_run.py) and the gateway's open refusals (gateway.py).
WRITTEN_STOP_REASONS = ("day_limit", "pause", "consistency_failed", "pilot_record_invalid", "commission_mismatch",
                        "bsp_decided_mismatch", "finalized_mismatch", "manual_order_during_session",
                        "capture_check_failed", "kill_file", "halted", "clock_regression", "validation:V9",
                        "executor_exception", "login_budget", "journal_anchor_write_failed", "pilot_not_configured",
                        "pilot_market_already_reserved")
STOP_REASONS = frozenset(pilot.refusal_code(code) for code in (
    *pilot.SCOPE_TABLE, *pilot.AUTHORITY_CODES.values(), *pilot.R13_CODES, *pilot.LEDGER_GAP_REASONS,
    *WRITTEN_STOP_REASONS))
# A session ends for good at its first KILL, HALT or stop (pilot._Folder.terminate): "kill", "halt" or a stop's reason.
SESSION_END_REASONS = STOP_REASONS | frozenset(("kill", "halt"))
ENDING_REASONS = frozenset(("breach", "yu_written", "lifetime_cap", "final_date", "n_reached"))


class SummaryRefused(Exception):
    """One code of REFUSAL_CODES and no value: the CLI prints 'Betfair input error: <code>' and exits 2."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def summarize(journal_path, *, clock, config, read_contract):
    """The summary dict of the live journal at journal_path, or SummaryRefused. config is the loaded config;
    read_contract(relative path) -> bytes is the pilot context's read-only contract reader."""
    contract = _contract(config, read_contract)
    now_ns = clock.time_ns()
    events, present = _events(journal_path)
    try:
        projection = recovery_projection(events, contract=contract, now_ns=now_ns)
        return _summary(contract, projection, now_ns=now_ns, present=present)
    except (pilot.PilotError, JournalError, KeyError, TypeError, ValueError):
        raise SummaryRefused("pilot_fold_failed") from None


def _contract(config, read_contract):
    if not pilot_enabled(config):
        raise SummaryRefused("pilot_not_configured")
    section = config["orders"]["pilot"]
    try:
        contract = pilot.contract_from_bytes(read_contract(section["contract_path"]))
    except (OSError, ValueError, KeyError, TypeError):
        raise SummaryRefused("pilot_contract_unreadable") from None
    if contract.sha256 != section["contract_sha256"]:
        raise SummaryRefused("pilot_contract_differs")          # run from another tree than the config names
    return contract


def _events(journal_path):
    """(events, present). An absent file is tested for before the read: pilot_events_after alone calls it unreadable."""
    try:
        os.lstat(journal_path)
    except FileNotFoundError:
        return [], False
    except OSError:
        raise SummaryRefused("pilot_journal_unreadable") from None
    read = pilot_events_after(journal_path, 0)
    if not read["readable"]:
        raise SummaryRefused("pilot_journal_unreadable")
    return read["events"], True


def _closed(code, allowed):
    return code if isinstance(code, str) and code in allowed else OTHER


def _bump(counts, key):
    counts[key] = counts.get(key, 0) + 1


def _day_limit(contract, state, today):
    """Today's day limit: today's session's, from the fold's own capacity_and_pause; with no session today, the one
    the next session would get (pilot._Folder.note_session_start: a PT4 session while no PT4 receipt is journaled)."""
    latest = state.sessions[-1] if state.sessions else None
    if latest is not None and latest["utc_day"] == today:
        return pilot.capacity_and_pause(contract, state, utc_day=today, session_id=latest["session_id"])["day_limit"]
    return contract.per_pt4_session_day if state.pt4_receipt_seq is None else contract.per_utc_day


def _summary(contract, projection, *, now_ns, present):
    state, core = projection["state"], projection["core"]
    today = pilot.utc_day(now_ns)
    day_limit = _day_limit(contract, state, today)
    used_today = state.day_used.get(today, 0)
    left = max(0, contract.lifetime_place_records - state.lifetime_used)
    ending = state.ending
    return {
        "schema": SCHEMA,
        "contract_id": contract.id if CONTRACT_ID_RE.fullmatch(contract.id) else OTHER,
        "contract_sha256_12": contract.sha256[:12],
        "journal": "present" if present else "absent",
        "place_records_used": state.lifetime_used,
        "place_records_left": left,
        "place_records_today": used_today,
        "place_records_left_today": max(0, min(day_limit - used_today, left)),
        "placing_ended": ending is not None,
        "placing_ended_code": None if ending is None else _closed(ending["reason"], ENDING_REASONS),
        "session_armed_now": pilot.session_armed(state, now_ns=now_ns),
        "sessions_total": len(state.sessions),
        "handoffs": len(projection["handoffs"]),
        "reads_owed": len(projection["pending_capture"]),
        "review_stops_active": sum(1 for stop in state.active_stops if stop["scope"] == "review"),
        "due_stops": len(state.due_stops),
        "sessions": _sessions(state, core),
    }


def _sessions(state, core):
    """Oldest first. A session's window: after its session_start note, before the next session's (or to the end)."""
    sessions = list(state.sessions)
    ends = [later["seq"] for later in sessions[1:]] + [None]
    return [_session(state, core, session, end) for session, end in zip(sessions, ends)]


def _session(state, core, session, end):
    start, session_id = session["seq"], session["session_id"]
    mine = [rec for rec in state.place_records if rec.session_id == session_id]
    reserved = {market for market, entry in state.reservations.items() if entry["session_id"] == session_id}
    orders = [core["orders"].get(rec.ref) or {} for rec in mine]
    skips = {}
    for skip in state.skip_records:
        if skip["market_id"] in reserved:
            _bump(skips, _closed(skip["code"], SKIP_CODES))
    stops = {}
    for stop in state.stops:                                  # journaled stops only; due ones are counted apart
        if start < stop["seq"] and (end is None or stop["seq"] < end):
            _bump(stops.setdefault(_closed(stop["scope"], STOP_SCOPES), {}), _closed(stop["reason"], STOP_REASONS))
    end_id = pilot.session_stop_id("session_end", start)
    own_end = [stop["seq"] for stop in state.stops if stop["stop_id"] == end_id]
    return {
        "utc_day": session["utc_day"],
        "reservations": len(reserved),
        "place_records": len(mine),
        "pt4_records": sum(1 for rec in mine if rec.pt4),
        "sends": sum(1 for rec in mine if rec.submit_seq is not None),
        "fills": sum(1 for order in orders if (order.get("size_matched_p") or 0) > 0),
        "cancels": sum(1 for order in orders if (order.get("size_cancelled_p") or 0) > 0),
        "unknowns": sum(1 for order in orders if order.get("state") in UNRESOLVED_STATES),
        "settles": sum(1 for order in orders if order.get("state") == "SETTLED"),
        "skips": skips,
        "stops": stops,
        "ended_reason": None if session["ended_reason"] is None else _closed(session["ended_reason"],
                                                                              SESSION_END_REASONS),
        "ended_by_own_end_note": bool(session["ended_reason"] == "session_end" and own_end
                                      and session["ended_seq"] == own_end[0]),
    }
