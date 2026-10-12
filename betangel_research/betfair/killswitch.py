"""Kill switch (spec section 9.8, amendment A3 D4): engage the global KILL file, cancel our unmatched bets,
re-sweep, verify.

engage() needs no lock, no login and no network: it only creates STATE_ROOT/KILL, which every
gateway in every process and profile reads fail-closed before each place, probe and paper replace,
and which a live place re-checks at the first byte of its request (the transport's send gate). The
sweep never takes or waits for .gateway.lock. It finds our open bets by two legs only: our strategy
refs and the betIds our journal knows. A bet without our strategy ref that our journal does not know
is never cancelled here (only the explicit account-wide scope does that). It cancels them by explicit
betId market by market (at most 60 per request, one request at a time), sweeps again at T0 + 20 s and
T0 + 35 s, where T0 is when the KILL file was engaged, and then reads both legs once more. A read
failure, a cancel that raised, or an EXECUTABLE bet left on that final check is kill_incomplete; a
cancel that raises (any Exception, e.g. a journal write that timed out) never stops the other
markets' cancels, the re-sweeps or the final check. Matched bets cannot be undone; nothing here
pretends otherwise.

Why the last pass at T0 + 35 s covers the longest in-flight order: no gateway writes the first byte
of a place-orders request once the KILL file exists, so every place request still in flight was
handed to the kernel by T0; Betfair may then take the order call's timeout (ORDER_TIMEOUT_S, F28),
the time for an order to appear (ORDER_VISIBILITY_S, F28) and an in-play bet delay
(IN_PLAY_DELAY_MAX_S, F42), 32 s in all, and 3 s are left for transit. The bound is checked below at
import. Residual (documented): a request whose bytes TCP retransmits late, or a process descheduled
between its send gate and its write, can still reach Betfair after the last pass.

placed_with_key (sourceIdKey compared in memory only, never stored or returned) stays for the
startup and unknown-outcome reconciliation, where a match only halts.
"""

import hmac
import os
import re

from .constants import (CANCEL_INSTRUCTIONS_LIMIT, IN_PLAY_DELAY_MAX_S, MAX_IDS_PER_REQUEST, ORDER_TIMEOUT_S,
                        ORDER_VISIBILITY_S)
from .errors import BetfairError, safe_code
from .filters import MARKET_ID_RE
from .receipts import SYSTEM_CLOCK, canonical_json, full_fsync
from .schemas import TERMINAL_STATES
from .secret import Secret, assert_clean

LEGS = ("strategy_ref", "bet_id")
# Re-sweeps, in seconds after the KILL file was engaged (monotonic). A pass whose time has passed runs at once.
RESWEEP_OFFSETS_S = (20, 35)
TRANSIT_MARGIN_S = 3
if (RESWEEP_OFFSETS_S != tuple(sorted(RESWEEP_OFFSETS_S))
        or RESWEEP_OFFSETS_S[-1] < ORDER_TIMEOUT_S + ORDER_VISIBILITY_S + IN_PLAY_DELAY_MAX_S + TRANSIT_MARGIN_S):
    raise RuntimeError("kill re-sweep offsets must be sorted and cover the longest in-flight order (F28, F42)")
PASSES = 1 + len(RESWEEP_OFFSETS_S)
# The kill's journal connections wait at most this long for another writer (amendment A3, D4): its events move
# no order, so a write it cannot make is skipped, never waited for; a normal append takes milliseconds.
JOURNAL_BUSY_TIMEOUT_S = 2
_BET_ID_RE = re.compile(r"[0-9]{1,20}")
_REASON_RE = re.compile(r"[A-Za-z0-9 _\-.:,]{1,120}")
_NS_PER_S = 1_000_000_000


def engage(paths, reason, *, clock=SYSTEM_CLOCK):
    """Create the KILL file (0600) with {"reason", "at"} if it is absent. Never locks, logs in or reads.

    A reason that is not short plain text is stored as "reason_not_recorded", so a kill can never
    fail on its reason. An existing KILL entry of any kind (even a directory or a dangling link)
    already engages the switch and is left as it is.
    """
    text = reason if isinstance(reason, str) and _REASON_RE.fullmatch(reason) else "reason_not_recorded"
    record = {"reason": text, "at": clock.utc_now()}
    data = canonical_json(record) + b"\n"
    try:
        assert_clean(data, where="KILL")
    except BetfairError:
        record = {"reason": "reason_not_recorded", "at": record["at"]}
        data = canonical_json(record) + b"\n"
    paths.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(str(paths.kill_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return {"status": "kill_already_engaged", "kill_file": str(paths.kill_file), "at": record["at"]}
    try:
        os.write(fd, data)
        full_fsync(fd)
    finally:
        os.close(fd)
    try:
        dir_fd = os.open(str(paths.root), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
    return {"status": "kill_engaged", "kill_file": str(paths.kill_file), "at": record["at"], "reason": text}


def placed_with_key(item, live_app_key):
    """True when a listCurrentOrders item's sourceIdKey is our live app key (compared in memory only).

    The comparison lives here so the key is revealed in as few modules as possible; nothing about
    the key or the item's sourceIdKey is kept or returned.
    """
    if not isinstance(item, dict) or not isinstance(live_app_key, Secret):
        return False
    value = item.get("sourceIdKey")
    key = live_app_key.reveal()
    return (isinstance(value, str) and value.isascii() and key.isascii() and hmac.compare_digest(value, key))


def _orders(journal_state):
    return list((journal_state or {}).get("orders", {}).values())


def journal_bet_ids(journal_state):
    """Every betId of a non-terminal order in the fold (a paper replace child included; live has none)."""
    return sorted({o["bet_id"] for o in _orders(journal_state)
                   if o.get("bet_id") is not None and o["state"] not in TERMINAL_STATES})


def _item_ids(item):
    if not isinstance(item, dict):
        return None
    bet_id, market_id = item.get("betId"), item.get("marketId")
    if (not isinstance(bet_id, str) or not _BET_ID_RE.fullmatch(bet_id) or not isinstance(market_id, str)
            or not MARKET_ID_RE.fullmatch(market_id)):
        return None
    return bet_id, market_id


def _read_leg(fetch, failures, leg):
    try:
        return list(fetch())
    except (BetfairError, ValueError) as exc:
        failures.append({"leg": leg, "code": safe_code(getattr(exc, "code", None) or type(exc).__name__)})
        return None


def target_set(read, journal_state, *, strategy_refs):
    """Our EXECUTABLE bets on the exchange, by two legs: our strategy refs and our journal's betIds.

    -> {"targets": {market_id: [bet_id, ...]}, "sources": {bet_id: [legs]}, "kill_targets": [{"bet_id",
    "market_id", "leg"}], "read_failures": [...]}. A read failure is reported, never
    treated as "nothing found".
    """
    failures, found = [], {}

    def add(items, leg):
        for item in items:
            ids = _item_ids(item)
            if ids is None:
                failures.append({"leg": leg, "code": "unexpected_item_shape"})
                continue
            entry = found.setdefault(ids[0], {"market_id": ids[1], "legs": set()})
            if entry["market_id"] != ids[1]:
                failures.append({"leg": leg, "code": "bet_market_conflict"})
            entry["legs"].add(leg)

    refs = sorted({r for r in strategy_refs or () if isinstance(r, str) and r})
    for start in range(0, len(refs), MAX_IDS_PER_REQUEST):
        batch_refs = refs[start:start + MAX_IDS_PER_REQUEST]
        items = _read_leg(lambda: read.iter_current_orders(customer_strategy_refs=batch_refs,
                                                           order_projection="EXECUTABLE"), failures, "strategy_ref")
        if items is not None:
            add(items, "strategy_ref")
    bet_ids = journal_bet_ids(journal_state)
    for start in range(0, len(bet_ids), MAX_IDS_PER_REQUEST):
        batch = bet_ids[start:start + MAX_IDS_PER_REQUEST]
        items = _read_leg(lambda: read.iter_current_orders(bet_ids=batch, order_projection="EXECUTABLE"), failures,
                          "bet_id")
        if items is not None:
            add(items, "bet_id")
    targets, sources, kill_targets = {}, {}, []
    for bet_id in sorted(found):
        entry = found[bet_id]
        targets.setdefault(entry["market_id"], []).append(bet_id)
        legs = sorted(entry["legs"], key=LEGS.index)
        sources[bet_id] = legs
        kill_targets.append({"bet_id": bet_id, "market_id": entry["market_id"], "leg": legs[0]})
    return {"targets": targets, "sources": sources, "kill_targets": kill_targets, "read_failures": failures}


def _cancel_targets(found, cancel_fn, record):
    """One cancel request per market chunk. An exception from one (Exception, not BaseException: a signal or
    KeyboardInterrupt still ends the kill) is recorded with raised=True and the next chunk goes on."""
    for market_id in sorted(found["targets"]):
        bet_ids = found["targets"][market_id]
        for start in range(0, len(bet_ids), CANCEL_INSTRUCTIONS_LIMIT):
            chunk = bet_ids[start:start + CANCEL_INSTRUCTIONS_LIMIT]
            proofs = [t for t in found["kill_targets"] if t["bet_id"] in chunk]
            record["cancel_requests"] += 1
            record["requested"].update(chunk)
            try:
                outcome = cancel_fn(market_id, chunk, proofs)
            except Exception as exc:          # one market's failure never stops the others
                record["cancel_failures"].append({"market_id": market_id, "bet_ids": chunk, "raised": True,
                                                  "code": safe_code(getattr(exc, "rule", None)
                                                                    or getattr(exc, "code", None)
                                                                    or type(exc).__name__)})
                continue
            if isinstance(outcome, dict) and outcome.get("decision") == "refused":
                refusal = outcome.get("refusal") or {}
                record["cancel_failures"].append({"market_id": market_id, "bet_ids": chunk,
                                                  "code": safe_code(refusal.get("rule") or "cancel_refused")})


def sweep(read, journal_state, *, cancel_fn, strategy_refs, clock, engaged_monotonic_ns=None, progress=None):
    """Cancel every bet the two legs find, at once and again at T0 + RESWEEP_OFFSETS_S, then verify.

    cancel_fn(market_id, bet_ids, kill_targets) cancels one request's worth of bets (the gateway's
    capability path). engaged_monotonic_ns is T0, read right after the KILL file was engaged (now when
    None). The waits are monotonic and never longer than the last offset; the wall clock is never read.
    progress(text), when given, receives one line per pass for the operator. -> {"status":
    "kill_complete"|"kill_incomplete", "remaining", "passes", "cancel_requests", "cancel_failures",
    "cancel_requested_bet_ids", "read_failures", "notes"}; kill_complete needs no read failure, no cancel
    that raised and no bet of ours EXECUTABLE on the final check. cancel_requested_bet_ids: every bet a
    cancel was requested for (on kill_complete none of them has an unmatched part left).
    """
    record = {"cancel_requests": 0, "cancel_failures": [], "requested": set()}
    read_failures = []
    engaged = engaged_monotonic_ns if type(engaged_monotonic_ns) is int else clock.monotonic_ns()
    cap_ns = RESWEEP_OFFSETS_S[-1] * _NS_PER_S

    def say(text):
        if progress is not None:
            progress(text)

    def one_pass():
        found = target_set(read, journal_state, strategy_refs=strategy_refs)
        read_failures.extend(found["read_failures"])
        _cancel_targets(found, cancel_fn, record)

    one_pass()
    passes = 1
    for offset in RESWEEP_OFFSETS_S:
        wait_ns = min(max(engaged + offset * _NS_PER_S - clock.monotonic_ns(), 0), cap_ns)
        say(f"pass {passes} of {PASSES}; next pass in {-(-wait_ns // _NS_PER_S)} s (orders sent just before the "
            f"kill can still appear; bound {RESWEEP_OFFSETS_S[-1]} s after the KILL file)")
        if wait_ns > 0:
            clock.sleep(wait_ns / _NS_PER_S)
        one_pass()
        passes += 1
    say("final check")
    verify = target_set(read, journal_state, strategy_refs=strategy_refs)
    read_failures.extend(verify["read_failures"])
    remaining = sorted(bet for bets in verify["targets"].values() for bet in bets)
    raised = any(failure.get("raised") for failure in record["cancel_failures"])
    complete = not remaining and not read_failures and not raised
    return {"status": "kill_complete" if complete else "kill_incomplete", "remaining": remaining, "passes": passes,
            "cancel_requests": record["cancel_requests"], "cancel_failures": record["cancel_failures"],
            "cancel_requested_bet_ids": sorted(record["requested"], key=int), "read_failures": read_failures,
            "notes": ["kill_cancel_raised"] if raised else []}
