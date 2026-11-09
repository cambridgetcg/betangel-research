"""Pilot 2's plain capped bot (P2-LP), the executor: p2-run (the P2 ARM line, then one race at a time) and p2-summary.

docs/betfair-pilot-2-policy.md (the page) is the policy and docs/betfair-pilot-2-engineering.md (the note) the detail.
Every leg rule lives in the pure p2_lp and in the gateway's lp_place, the only order path, which this module never
imports: the CLI hands it a factory that opens one config-p2 "place" gateway per race with the session's preloaded
credentials, and a delayed read session. This module adds the parts the note gives the executor (§3, §7, §8, §10): the
ARM pre-checks, the typed ARM line (always Yu's, at the terminal, through the live confirmer; nothing here types or
supplies one), the ARM record, the market list and the screen on the delayed key, one race per open (WAIT, BACK, LAY,
REST and PULL, with every failure edge), the handoff, the end and its release, and the offline summary. Ownership of
the live recording state goes through admission.live_ownership.

What it reads: the journal on short-lived read-only connections (pilot_run.pilot_events_after), git for the commit and
a clean tree, the Keychain once per session (pilot_run.load_session_credentials), and Betfair only through the gateways
and the read session the CLI builds. What it writes: the ARM record (the core kind operator_confirm, compare-and-
appended on the head Yu saw, under .gateway.lock), nothing else to the journal; screens, flags and SKIPs are printed,
never journaled. caffeinate (injected as env.popen) keeps the Mac awake from the ARM to the end; it is process-scoped
and changes no setting. A signal from the typed line on only sets a flag (pilot_run.StopFlag) outside an open.
"""

import os
import secrets
import subprocess
from types import SimpleNamespace

from . import admission, p2_lp, pilot, pilot_gate, pilot_run
from .api import PILOT_ROW_PROJECTIONS
from .config import config_sha256
from .credentials import publish_anchor
from .errors import AdmissionRefused, BetfairError, CredentialError, JournalError, LockBusy, safe_code
from .journal import OrderJournal, fold_events
from .paths import file_lock, kill_engaged
from .receipts import git_identity, utc_from_ns
from .schemas import UNRESOLVED_STATES

NS = p2_lp.NS
Refused = pilot_run.Refused
SSH_VARS = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")     # friction only (lab rule 2: Yu types at the Mac itself)
UNTIL_LEAD_NS = 20 * 60 * NS                               # note §7: UNTIL after now + 20 min
CAFFEINATE = "/usr/bin/caffeinate"
CHECKOUT = str(pilot_gate.MODULE_ROOT)                     # the handoff's own-scope kill runs from here


def _code(exc):
    """A safe code for an exception: a BetfairError's own code, else its type name."""
    text = str(exc) if isinstance(exc, BetfairError) else ""
    return safe_code(text) if text else type(exc).__name__


# --- ARM (note §7): the view, the pre-checks, the banner and the record ------------------------------------------


def _until_ns(text, now_ns):
    """until_ns for HHMM (UTC, 00:00 to 23:59) on now's UTC day; Refused for anything else."""
    match = pilot_run.UNTIL_RE.fullmatch(text or "")
    if match is None:
        raise Refused("p2_until_invalid")
    return now_ns - now_ns % pilot.DAY_NS + (int(match.group(1)) * 3600 + int(match.group(2)) * 60) * NS


def arm_view(env):
    """One whole-journal read (read only) and its core fold, with the checkout's git identity -> {events, state, head,
    git}; Refused when the journal cannot be read or folded."""
    read = pilot_run.pilot_events_after(env.paths.journal_path("live"), 0)
    if not read["readable"]:
        raise Refused("p2_journal_unreadable")
    try:
        state = fold_events(read["events"], mode="live", now_ns=env.clock.time_ns())
    except (JournalError, KeyError, TypeError, ValueError):
        raise Refused("p2_fold_failed") from None
    return {"events": read["events"], "state": state, "head": pilot_run.head_of(read["events"]),
            "git": git_identity(pilot_gate.MODULE_ROOT, runner=env.git_runner)}


def arm_problems(env, view, *, now_ns, until_ns, races):
    """ARM's content checks (note §7), read only, before the line and again after it -> [codes], [] when clean:
    config-p2's pins; a known commit and a clean tree; release's clean live state, the login ledger and no Pilot 1
    armed-session file; no Pilot 1 session today or open, and no P2 ARM record today (one pilot session a UTC day
    across both pilots); none of our bets open or unsettled; the race counts and N (1 to min(5, 10 - total), and 1
    while no race has run); UNTIL after now + 20 min; the login cap. An SSH session is refused before this, in run."""
    paths, config, clock = env.paths, env.config, env.clock
    events, orders, git = view["events"], view["state"]["orders"], view["git"]
    problems = list(p2_lp.config_problems(config))
    if git["commit"] is None or git["dirty"] is None:
        problems.append("p2_code_unknown")
    elif git["dirty"]:
        problems.append("p2_tree_dirty")
    problems += admission.live_state_problems(paths, clock=clock)
    problems += admission.ledger_problems(config, paths, clock=clock)
    problems += pilot_run.armed_session_check(paths)
    day = pilot.utc_day(now_ns)
    start = pilot_gate.latest_note(events, "session_start")
    if (start is not None and start["utc_day"] == day) or pilot_gate.session_window_open(events, now_ns=now_ns):
        problems.append("p2_pilot_1_day")
    if p2_lp.arm_records(events, day=day):
        problems.append("p2_arm_already_today")
    if pilot_gate.open_manual_refs([], orders):
        problems.append("p2_other_bet_open")
    total = len(p2_lp.races(orders))
    if total >= p2_lp.RACES_TOTAL:
        problems.append("p2_races_total")
    if not 1 <= races <= min(p2_lp.RACES_DAY, p2_lp.RACES_TOTAL - total) or (total == 0 and races != 1):
        problems.append("p2_races_invalid")
    if not until_ns > now_ns + UNTIL_LEAD_NS:
        problems.append("p2_until_invalid")
    if pilot_run.cap_blocks(config, paths, clock=clock):
        problems.append("p2_login_cap")
    return problems


def banner(*, cfg12, commit, day, until, races, today, total):
    return (f"MODE=LIVE: P2 ARM starts one attended Pilot 2 session (the plain capped bot) on {day}: config-p2 "
            f"{cfg12}, code commit {commit[:12]}. Races so far: {today} today, {total} of {p2_lp.RACES_TOTAL} in "
            f"total; this session may run {races} until {until}Z. Each race is one £2 BACK and one £2 LAY with no "
            f"per-order line, decided about 10 minutes before the off and pulled at 5 minutes if unmatched; the "
            f"session ends after any race with a matched leg. To stop: the Betfair phone app, Ctrl-C here, the KILL "
            f"file, or the own-scope kill from {CHECKOUT}. While legs rest a typed cancel fails (this run holds "
            f".gateway.lock). Nothing heavy runs on this Mac while the session is armed or an order rests. Keep it on "
            f"mains power with the lid open. Each Keychain item is read once.")


def run(env):
    """p2-run: the ARM pre-checks, the P2 ARM line, then the session -> the exit code (0 an end that released the
    recording admission; 5 any other end once the ARM record exists). Refusals before the record raise Refused: exit 2,
    nothing sent, nothing journaled."""
    paths, config, clock = env.paths, env.config, env.clock
    if paths.profile != pilot_run.RECORDER_PROFILE:
        raise Refused("p2_profile_not_recorder_profile")
    if any(env.environ.get(name) for name in SSH_VARS):     # first: no Keychain anchor, journal or git read (note §7)
        raise Refused("p2_arm_refused:p2_over_ssh")
    pilot_run.gates(env, "pilot-arm")                       # A2 on config-p2's token, A11, A13
    now = clock.time_ns()
    until_ns = _until_ns(env.args.until, now)
    races = env.args.races
    view = arm_view(env)
    problems = arm_problems(env, view, now_ns=now, until_ns=until_ns, races=races)
    if problems:
        raise Refused("p2_arm_refused:" + ",".join(problems))
    day, cfg12, orders = pilot.utc_day(now), config_sha256(config)[:12], view["state"]["orders"]
    line = p2_lp.arm_line(cfg12, day, env.args.until, races)
    env.confirmer.confirm_line(line, banner=banner(cfg12=cfg12, commit=view["git"]["commit"], day=day,
                                                   until=env.args.until, races=races,
                                                   today=len(p2_lp.races(orders, day=day)),
                                                   total=len(p2_lp.races(orders))))
    stop = pilot_run.StopFlag()
    stop.install()
    try:
        return _armed(env, view, stop, line=line, day=day, until_ns=until_ns, races=races)
    finally:
        stop.restore()


def _armed(env, view, stop, *, line, day, until_ns, races):
    """After the line, under the owner lock to the end: the ARM steps, the races and the end."""
    paths, config, clock = env.paths, env.config, env.clock
    tag = secrets.token_hex(4)
    try:
        with admission.live_ownership(paths, owner=f"p2-session:{tag}",
                                      purpose=f"P2 plain bot {config_sha256(config)[:12]} {day}", config=config,
                                      clock=clock, processes=env.processes) as ticket:
            s = SimpleNamespace(session_id=f"p2s-{tag}", ticket=ticket, stop=stop, races=races, until_ns=until_ns,
                                arm_seq=None, head=None, credentials=None, caffeinate=None, read_session=None,
                                read=None, raced=0, streak=0, pulled=False, handoff=False, codes={}, listed=0,
                                screened=0, flagged=0)
            try:
                _arm(env, s, view, line=line, day=day)
            except Refused:
                raise
            except BaseException as exc:                  # as pilot-run's OWN-4: the end still names generation N
                _close(s)
                env.terminal.line(f"ARM did not finish ({_code(exc)}): recording stays off until Yu's typed "
                                  f"recording-release (generation {ticket.generation})")
                raise
            return _session(env, s)
    except admission.OwnershipRefused as exc:
        raise Refused(exc.code, pilot_run.owner_live_lines(paths) if exc.code == "owner_live" else []) from None
    except AdmissionRefused as exc:
        raise Refused(exc.outcome, pilot_run.admit_refusal_lines(exc, paths)) from None


def _arm(env, s, view, *, line, day):
    """Steps 3-7 of note §7: the checks again on a fresh view, the credentials, caffeinate, the ARM record compare-
    and-appended on the head Yu saw while holding .gateway.lock (let go before the first open), the anchor, the read
    session. A refusal here releases this admission (release_own) and prints its lines."""
    paths, config, clock = env.paths, env.config, env.clock

    def refuse(code):
        _close(s)
        result = admission.release_own(s.ticket, paths, config=config, clock=clock)
        raise Refused(code, pilot_run.release_lines(result, env.paused_flag))

    try:
        fresh = arm_view(env)
    except Refused as exc:
        refuse(exc.code)
    problems = arm_problems(env, fresh, now_ns=clock.time_ns(), until_ns=s.until_ns, races=s.races)
    if problems:
        refuse("p2_arm_refused:" + ",".join(problems))
    if s.stop.requested:
        refuse("p2_arm_stopped")
    try:
        s.credentials = pilot_run.load_session_credentials(config, paths, runner=env.keychain_runner)
    except CredentialError:
        refuse("p2_arm_credentials_unavailable")
    if s.stop.requested:
        refuse("p2_arm_stopped")
    try:
        s.caffeinate = env.popen([CAFFEINATE, "-i", "-s", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        refuse("p2_caffeinate_failed")
    payload = p2_lp.arm_payload(line, session_id=s.session_id, config_sha256=config_sha256(config), utc_day=day,
                                until_ns=s.until_ns, races=s.races, code_commit=fresh["git"]["commit"])
    journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
    try:
        try:
            with file_lock(paths.gateway_lock, timeout_s=config["http"]["order_deadline_s"] + 5, clock=clock):
                event = journal.append("operator_confirm", payload, expect_head=view["head"])
        except LockBusy:
            refuse("lock_busy")
        except JournalError as exc:
            refuse("journal_changed_during_arm" if "journal_head_moved" in str(exc) else _code(exc))
        try:
            publish_anchor(journal, paths, runner=env.keychain_runner, clock=clock)
        except CredentialError as exc:
            env.terminal.line(f"Betfair warning: {safe_code(str(exc))}; the Keychain anchor lags the journal until "
                              f"the next live command writes it")
    finally:
        journal.close()
    s.arm_seq = s.head = event["seq"]
    s.read_session, s.read = env.read_session_factory(s.credentials["read"])
    env.terminal.line(f"P2 ARM record {event['seq']}: session {s.session_id}, {s.races} races until "
                      f"{utc_from_ns(s.until_ns)[11:16]}Z")


# --- the session: markets, the screen and one race per open (note §8) ---------------------------------------------


def _session(env, s):
    """The races, one at a time, then the end; whatever escapes still logs out the read session and ends caffeinate."""
    try:
        try:
            code = _races(env, s)
        except Exception as exc:
            env.terminal.line(f"p2-run: {_code(exc)} outside a race")
            code = "exception"
        return end(env, s, code)
    finally:
        _close(s)


def _races(env, s):
    found = markets(env, s.read, s.until_ns)
    if found is None:
        return "markets_read_failed"
    s.listed = len(found)
    for market_id, off_ns in found:
        code = race(env, s, market_id, off_ns)
        env.terminal.line(f"{market_id}: {code}")
        if code != "next":
            return code
        if s.raced >= s.races:
            return "races_done"
    return "no_more_markets"


def markets(env, read, until_ns):
    """The day's markets from one delayed catalogue call (note §8 IDLE): GB/IE, config-p2's event and market types,
    MARKET_START_TIME and MARKET_DESCRIPTION, first to start first; a row is kept when schedule_row_ok passes and its
    off - 300 s is not after UNTIL (else the gateway's p2_until_before_pull would refuse its BACK) -> [(market_id,
    off_ns)], or None when the read fails. One line counts what was left out."""
    rules = env.config["orders"]["rules"]
    last = until_ns - p2_lp.PULL_S * NS
    try:
        rows = read.list_market_catalogue(
            {"eventTypeIds": rules["event_type_ids"], "marketCountries": list(p2_lp.COUNTRIES),
             "marketTypeCodes": rules["market_types"],
             "marketStartTime": {"from": utc_from_ns(env.clock.time_ns()), "to": utc_from_ns(last)}},
            projections=("MARKET_START_TIME", "MARKET_DESCRIPTION"), max_results=200, sort="FIRST_TO_START")
    except (BetfairError, ValueError):
        return None
    found, left = [], {}
    for row in rows:
        try:
            market_id, off_ns = row["marketId"], pilot.parse_utc_ns(row["marketStartTime"])
        except (KeyError, TypeError, ValueError):
            code = "row_unreadable"
        else:
            code = p2_lp.schedule_row_ok(row) or (None if off_ns <= last else "p2_until_before_pull")
        if code is None:
            found.append((market_id, off_ns))
        else:
            left[code] = left.get(code, 0) + 1
    env.terminal.line(f"markets: {len(found)} listed; left out: "
                      + (", ".join(f"{code} {n}" for code, n in sorted(left.items())) or "none"))
    return sorted(found, key=lambda item: (item[1], item[0]))


def screen(env, read, market_id, off_ns):
    """SCREEN on the delayed session (note §8): one fresh catalogue row by marketIds (PILOT_ROW_PROJECTIONS), stored so
    that each send book takes its market_time and the BACK its country from it, never a cached row up to 6 h old; then
    one book -> None, or a code: off_moved, screen_read_failed or p2_lp.screen's. No live-key read (F46)."""
    try:
        rows = read.list_market_catalogue({"marketIds": [market_id]}, projections=PILOT_ROW_PROJECTIONS, max_results=1)
        row = next((r for r in rows if isinstance(r, dict) and r.get("marketId") == market_id), None)
        if row is None:
            return "screen_read_failed"
        if pilot.parse_utc_ns(row.get("marketStartTime")) != off_ns:
            return "off_moved"
        read.store_catalogue_row(market_id, row)
        book = read.market_snapshot(market_id, fetched_by="read_command")
    except (BetfairError, ValueError):
        return "screen_read_failed"
    return p2_lp.screen(book)


def _skip(s, code):
    """A SKIP: counted for the end's summary and printed with the market, never journaled -> "next"."""
    s.codes[code] = s.codes.get(code, 0) + 1
    return "next"


def _idle(env, s, target_ns):
    """IDLE and PRE-OPEN, no lock: 1 s slices to target_ns -> None, "stopped" (the flag) or "kill_file"."""
    while True:
        if s.stop.requested:
            return "stopped"
        if kill_engaged(env.paths):
            return "kill_file"
        now = env.clock.time_ns()
        if now >= target_ns:
            return None
        env.clock.sleep(min(p2_lp.SLICE_S, (target_ns - now) / NS))


def race(env, s, market_id, off_ns):
    """One market (note §8), from IDLE to the PULL -> "next" or an end code."""
    if env.clock.time_ns() > off_ns + p2_lp.SCREEN_S[1] * NS:
        return _skip(s, "missed_window")
    code = _idle(env, s, off_ns + p2_lp.SCREEN_S[0] * NS)
    if code is not None:
        return code
    s.screened += 1
    code = screen(env, s.read, market_id, off_ns)
    if code is not None:
        return _skip(s, code)
    s.flagged += 1
    code = _idle(env, s, off_ns + p2_lp.OPEN_S * NS)
    if code is not None:
        return code
    if pilot_run.cap_blocks(env.config, env.paths, clock=env.clock):
        return _skip(s, "login_cap")
    problems = pilot_run.admission_intact(env.paths, s.ticket, clock=env.clock)
    if problems:
        return problems[0]
    return _open(env, s, market_id, off_ns)


def _open(env, s, market_id, off_ns):
    """OPEN: one place gateway on config-p2 for this race. An exception out of the factory sent nothing (a signal
    there is Yu's stop); the race's own exceptions are handled inside the open, so one out of close() leaves the race's
    code as it was."""
    s.pulled = False
    lp = {"arm_seq": s.arm_seq, "session_id": s.session_id, "races": s.races}
    done = {}
    try:
        with env.gateway_factory(lp, credentials=s.credentials, stop=s.stop) as gw:
            done["code"] = in_open(env, s, gw, market_id, off_ns)
        return done["code"]
    except env.signal_stop:
        s.stop.requested = True
        return done.get("code", "stopped")
    except Exception as exc:
        return done.get("code") or f"open_failed:{_code(exc)}"
    finally:
        s.stop.install()                                  # the outer handler first, as pilot-run does after an open
        s.stop.take_pending(getattr(env.ctx, "signals", None))


def in_open(env, s, gw, market_id, off_ns):
    report = gw.open_report() or {}
    if report.get("armed") is not True:                   # dynamic failures do not raise; a foreign order HALTs
        return "not_armed:" + ((report.get("failures") or ["unknown"])[0])
    try:
        return _legs(env, s, gw, market_id, off_ns)
    except (Exception, env.signal_stop) as exc:
        return _raised(env, s, gw, market_id, exc)


def _legs(env, s, gw, market_id, off_ns):
    """WAIT, BACK, BETWEEN, LAY, REST and PULL in one open (note §8)."""
    trigger = _wait(env, s, off_ns + p2_lp.DECIDE_S[0] * NS)
    if trigger is not None:
        return trigger
    result, exc = _place(env, s, gw, market_id, "BACK")
    if exc is not None:                                   # nothing of the BACK submitted
        if isinstance(exc, env.signal_stop):
            s.stop.requested = True
            return "stopped"
        return _skip(s, "book_fetch_failed") if _code(exc) == "book_fetch_failed" else f"back_raised:{_code(exc)}"
    state = _state(result)
    if state in (None, "REFUSED"):                        # a choice, leg, V-, R- or A18 refusal: nothing sent
        s.streak += 1
        _skip(s, (result.get("refusal") or {}).get("rule") or "refused")
        return "skip_streak" if s.streak >= p2_lp.SKIP_STREAK else "next"
    s.streak, s.raced = 0, s.raced + 1                    # a race from its first submit
    if state == "FAILED":
        return "next"
    if state not in p2_lp.PLACED_STATES:                  # UNKNOWN or ABSENT_UNCONFIRMED after the in-line reconcile
        _handoff(env, s, gw, market_id)
        return "back_unresolved"
    trigger = _wait(env, s, env.clock.time_ns())          # BETWEEN: one look, no wait
    if trigger is not None:
        return _with_legs(env, s, gw, market_id, trigger)
    result, exc = _place(env, s, gw, market_id, "LAY")
    state = None if exc is not None else _state(result)
    if state not in p2_lp.PLACED_STATES:
        outcome = pull(env, s, gw, market_id)              # the BACK, at once
        if state in (None, "REFUSED", "FAILED"):
            return _pulled(env, s, gw, market_id, outcome) or "next"
        _pulled(env, s, gw, market_id, outcome)            # the LAY is unresolved: handed off with the pull's legs
        return "lay_unresolved"
    trigger = _wait(env, s, off_ns + p2_lp.PULL_S * NS)    # REST: no Betfair call
    if trigger is not None:
        return _with_legs(env, s, gw, market_id, trigger)
    return _pulled(env, s, gw, market_id, pull(env, s, gw, market_id)) or "next"


def _place(env, s, gw, market_id, side):
    """lp_place for one side -> (result, None), or (None, the exception) when it raised and lp_legs shows no submit of
    that side; any other raise (a submit, or legs that cannot be read) goes on to the EXCEPTION edge."""
    try:
        return gw.lp_place(market_id, side, confirm_token=env.args.confirm_live), None
    except (Exception, env.signal_stop) as exc:
        if _submitted(gw, side) is not False:
            raise
        return None, exc


def _state(result):
    """The leg's state, or None when nothing was sent: a refused result may list other bets (the gateway's
    resolved-at-open refusal lists the earlier orders it found live)."""
    views = result.get("orders") or []
    return views[0]["state"] if views and result.get("decision") == "accepted" else None


def _submitted(gw, side=None):
    """False when lp_legs shows no leg (of side) with a submit, True when one has one, None when it cannot tell."""
    try:
        legs = gw.lp_legs()
    except Exception:
        return None
    if any(order is None for order in legs.values()):
        return None
    return any(order["submitted_at"] is not None for order in legs.values() if side is None or order["side"] == side)


def _wait(env, s, until_ns):
    """The head after our own appends, then watch from it -> watch's trigger, or "unreadable"."""
    read = pilot_run.pilot_events_after(env.paths.journal_path("live"), s.arm_seq)
    if not read["readable"]:
        return "unreadable"
    s.head = read["events"][-1]["seq"] if read["events"] else s.arm_seq
    return watch(env, s, s.head, until_ns=until_ns)


def watch(env, s, since_seq, *, until_ns):
    """In-open waits in 1 s slices on the injected clock, no Betfair call (note §8) -> None at until_ns, or the first
    trigger: stopped (the flag, or a signal the gateway deferred), unreadable (the journal), kill_event (a kill since
    the ARM record), foreign (any event after since_seq: ours stop at our head, and .gateway.lock keeps every writer but
    the kill out), kill_file, drift (wall and monotonic time apart by more than DRIFT_TOLERANCE_NS) or late_wake (a
    slice ending more than LATE_WAKE_S late). Checked once before any wait."""
    clock = env.clock
    wall0, mono0 = clock.time_ns(), clock.monotonic_ns()
    while True:
        s.stop.take_pending(getattr(env.ctx, "signals", None))
        if s.stop.requested:
            return "stopped"
        read = pilot_run.pilot_events_after(env.paths.journal_path("live"), s.arm_seq)
        if not read["readable"]:
            return "unreadable"
        if read["kill_seq"] is not None:
            return "kill_event"
        if any(event["seq"] > since_seq for event in read["events"]):
            return "foreign"
        if kill_engaged(env.paths):
            return "kill_file"
        now = clock.time_ns()
        if abs((now - wall0) - (clock.monotonic_ns() - mono0)) > pilot_gate.DRIFT_TOLERANCE_NS:
            return "drift"
        if now >= until_ns:
            return None
        step = min(p2_lp.SLICE_S, (until_ns - now) / NS)
        clock.sleep(step)
        if clock.time_ns() - now > (step + p2_lp.LATE_WAKE_S) * NS:
            return "late_wake"


def _with_legs(env, s, gw, market_id, trigger):
    """A trigger with legs resting: a kill event or an unreadable journal hands off with no cancel (never race the
    kill); anything else pulls now. The session ends with the trigger."""
    if trigger in ("kill_event", "unreadable"):
        _handoff(env, s, gw, market_id)
    else:
        _pulled(env, s, gw, market_id, pull(env, s, gw, market_id))
    return trigger


def pull(env, s, gw, market_id):
    """PULL (note §8): first the journal since the ARM record (a kill event, or unreadable: nothing is cancelled,
    since a duplicate cancel only leaves an UNKNOWN); then one cancel per EXECUTABLE leg, LAY first, each on its own
    (the gateway reconciles in line); then the legs again -> {complete: no leg unresolved or EXECUTABLE, matched: a
    leg matched, codes}. Once a race: a failed pull is never retried."""
    s.pulled = True
    read = pilot_run.pilot_events_after(env.paths.journal_path("live"), s.arm_seq)
    if not read["readable"] or read["kill_seq"] is not None:
        return {"complete": False, "matched": False, "codes": ["kill_event" if read["readable"] else "unreadable"]}
    codes = []
    legs = gw.lp_legs()
    for side in ("LAY", "BACK"):
        for order in legs.values():
            if order is None or order["side"] != side or order["state"] != "EXECUTABLE" or not order["bet_id"]:
                continue
            try:
                result = gw.cancel(market_id, [order["bet_id"]], source="cli_manual")
            except Exception as exc:
                codes.append(f"cancel_raised:{_code(exc)}")
                continue
            if result["decision"] != "accepted":
                codes.append(f"cancel_refused:{(result.get('refusal') or {}).get('rule') or 'unknown'}")
    legs = gw.lp_legs()
    return {"complete": all(order is not None and order["state"] not in UNRESOLVED_STATES
                            and order["state"] != "EXECUTABLE" for order in legs.values()),
            "matched": any(order is not None and order["size_matched_p"] > 0 for order in legs.values()),
            "codes": codes}


def _pulled(env, s, gw, market_id, outcome):
    """The pull's outcome -> None (complete, nothing matched); "matched_leg" with the settle-market line; else the
    handoff, and kill_event, unreadable or pull_incomplete."""
    codes = outcome["codes"]
    if codes:
        env.terminal.line(f"{market_id} pull: " + ", ".join(codes))
    if not outcome["complete"]:
        _handoff(env, s, gw, market_id)
        return codes[0] if codes[:1] in (["kill_event"], ["unreadable"]) else "pull_incomplete"
    if outcome["matched"]:
        env.terminal.line(f"{market_id}: a p2-lp leg matched; after its result run settle-market --market {market_id}"
                          f"{env.paused_flag}. The next P2 ARM refuses until it is settled.")
        return "matched_leg"
    return None


def _handoff(env, s, gw, market_id):
    """The handoff (note §8) for each leg not shown complete, by bet id, else by ref and market; legs that cannot be
    listed at all are named by the market. Nothing is printed when every leg is complete."""
    try:
        legs = gw.lp_legs()
    except Exception:
        legs = {"unknown": None}
    shown = {ref: order for ref, order in legs.items()
             if order is None or order["state"] in UNRESOLVED_STATES or order["state"] == "EXECUTABLE"}
    if legs and not shown:
        return
    s.handoff = True
    env.terminal.lines(p2_lp.handoff_lines(shown, checkout=CHECKOUT, market_id=market_id))


def _raised(env, s, gw, market_id, exc):
    """EXCEPTION (note §8), signals deferred from here: with no leg submitted nothing can be live, so a signal is Yu's
    stop and anything else ends the session; else one best-effort pull in its own try unless this race's pull already
    ran (it is never retried), then the handoff for each leg not shown complete. The open's close lets go of
    .gateway.lock."""
    signals = getattr(env.ctx, "signals", None)
    if signals is not None:
        signals.deferring = True
    if isinstance(exc, env.signal_stop):
        s.stop.requested = True
    if _submitted(gw) is False:
        return "stopped" if isinstance(exc, env.signal_stop) else f"raised:{_code(exc)}"
    env.terminal.line(f"p2-run: {_code(exc)} after a submit in {market_id}")
    if not s.pulled:
        try:
            outcome = pull(env, s, gw, market_id)
            if outcome["codes"]:
                env.terminal.line(f"{market_id} pull: " + ", ".join(outcome["codes"]))
        except Exception:
            pass
    _handoff(env, s, gw, market_id)
    return "exception"


# --- the end (note §8) and p2-summary (note §10) --------------------------------------------------------------------


def _logout(session):
    """Log out one read session, best effort."""
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def _close(s):
    """Log out the read session and end caffeinate, once each, best effort."""
    session, s.read_session = s.read_session, None
    _logout(session)
    process, s.caffeinate = s.caffeinate, None
    if process is not None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


def end(env, s, code):
    """The end: the read session and caffeinate closed, the session's counts printed (screens, flags and SKIP codes:
    printed, never journaled), then release_own unless a leg was handed off or an exception ended the session -> 0
    when the recording admission was released, else 5."""
    w = env.terminal
    _close(s)
    w.json({"status": "p2_session_ended", "session_id": s.session_id, "end_code": code, "markets": s.listed,
            "screened": s.screened, "flagged": s.flagged, "races": s.raced, "codes": dict(sorted(s.codes.items())),
            "stop_requested": s.stop.requested, "signal": s.stop.name})
    if s.handoff or code == "exception":
        w.line(f"recording stays off until Yu's typed recording-release (generation {s.ticket.generation})")
        return 5
    result = admission.release_own(s.ticket, env.paths, config=env.config, clock=env.clock)
    w.lines(pilot_run.release_lines(result, env.paused_flag))
    return 0 if result["released"] else 5


def summary(env):
    """p2-summary (note §10): p2_lp's counts from one read-only journal read, per UTC day and in total, with the
    handoff for open or unsettled legs; --cleared adds Betfair's SETTLED rows grouped by MARKET for p2-lp since the
    first P2 ARM record, on the delayed read key, and "check the website" for each mismatch -> 0."""
    paths, clock, w = env.paths, env.clock, env.terminal
    read = pilot_run.pilot_events_after(paths.journal_path("live"), 0)
    if not read["readable"]:
        raise Refused("p2_journal_unreadable")
    try:
        state = fold_events(read["events"], mode="live", now_ns=clock.time_ns())
    except (JournalError, KeyError, TypeError, ValueError):
        raise Refused("p2_fold_failed") from None
    counts = p2_lp.summary(read["events"], state["orders"])
    cleared, arms = None, p2_lp.arm_records(read["events"])
    if env.args.cleared and not arms:
        w.line("no P2 ARM record: nothing to clear")
    elif env.args.cleared:
        try:
            credentials = pilot_run.load_session_credentials(env.config, paths, runner=env.keychain_runner,
                                                             purposes=("read",))
        except CredentialError:
            raise Refused("p2_credentials_unavailable") from None
        session, client = env.read_session_factory(credentials["read"])
        try:
            cleared = list(client.iter_cleared_orders("SETTLED", settled_from_utc=arms[0]["recorded_at"],
                                                      customer_strategy_refs=[p2_lp.STRATEGY_REF], group_by="MARKET"))
        except (BetfairError, ValueError) as exc:
            _print(w, p2_lp.summary_lines(counts, checkout=CHECKOUT))
            raise Refused(f"p2_cleared_read_failed:{_code(exc)}") from None
        finally:
            _logout(session)
    _print(w, p2_lp.summary_lines(counts, cleared, checkout=CHECKOUT))
    return 0


def _print(w, lines):
    """The summary is the command's result: stdout, through the one best-effort writer."""
    for line in lines:
        w.write("out", line + "\n")
