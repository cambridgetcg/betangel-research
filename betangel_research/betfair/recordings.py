"""Offline structural index of recordings, and verify-day (recording design v3 section 6, v4 section F, v5).

Read only and offline: nothing here opens a connection, reads a credential or writes anywhere but the
profile's recordings/index directory. Segment lines are parsed for a whitelist of market-level keys
only: pt, and per market change id, img and marketDefinition.status, .inPlay, .marketTime and .eventId.
Runner definitions, runner changes (rc), prices, volumes, BSP and bspReconciled are never kept, so
neither the per-market index (which stays under the state root on this Mac) nor the counts-only
summary the CLI prints can carry a price, runner or BSP value. Times are the receipt sidecar's
received_at (local wall clock); pt is Betfair's publish time, and on the delayed stream lines are
conflated (about 180 s), so in-play and CLOSED times are conflated publish times, not transition times.

Coverage (v4 section F). A market's window runs from its initial marketTime (the marketTime on its
first observed change) minus 60 minutes to its first observed CLOSED, or, when no CLOSED was seen,
to the end of the last session that observed it. A market whose observing sessions all ended before
its window began gets the hour before its marketTime as its window, all of it late start, and the flag
never_in_window (plus dropped_explicit when an explicit segment during that hour left it out); one
CLOSED before its window began gets closed_before_window and no coverage figures. Within a segment a
market is covered from its own first line there (a late market never inherits the segment's start)
to the segment's last line, except across a stall: two consecutive lines further apart than the
stream's liveness timeout, which the client would have ended had its monotonic clock run (sleep).
Every other uncovered stretch inside the window is a gap, with its cause and length, except one
tolerated handover: a scheduled_refresh after which the market's next appearance is the next
segment's image within one conflation interval. A fresh image restores state but never the lost
observations, so a gap stays a gap. Gaps are kept across segments and sessions; flags are a set
beside one class, highest precedence first: UNVERIFIED, NOT_OBSERVED (a pre-count market never
observed, anchored on its catalogue start), NO_ANCHOR (observed, first change without marketTime),
DROPPED_EXPLICIT, LATE_START, GAP, NO_CLOSE, CLOSED_WITHOUT_OBSERVED_INPLAY, SCHEDULE_CHANGED,
COMPLETE. COMPLETE means coverage under this window and tolerance, not proof every change was seen.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .errors import safe_code
from .filters import MARKET_ID_RE
from .receipts import atomic_write_json, file_sha256, parse_utc_ns, strict_json_loads, utc_from_ns
from .recorder import (PRECOUNT_NAME, PRECOUNT_ROW_KEYS, RECEIPT_STATUS, RECV_ROW_KEYS, _verify_segment,
                       verify_recording)

INDEX_STATUS = "structural_recording_index_not_profitability_evidence"
INDEX_SCHEMA_VERSION = 1
TIME_NOTE = ("received_at is the local wall clock at line read; publish_pt is Betfair's publish time; delayed-key "
             "lines are conflated (about 180 s), so in-play and CLOSED times are conflated publish times, not "
             "transition times")
CLASSES = ("UNVERIFIED", "NOT_OBSERVED", "NO_ANCHOR", "DROPPED_EXPLICIT", "LATE_START", "GAP", "NO_CLOSE",
           "CLOSED_WITHOUT_OBSERVED_INPLAY", "SCHEDULE_CHANGED", "COMPLETE")
FLAG_CLASSES = (("unverified", "UNVERIFIED"), ("not_observed", "NOT_OBSERVED"), ("no_anchor", "NO_ANCHOR"),
                ("dropped_explicit", "DROPPED_EXPLICIT"), ("late_start", "LATE_START"), ("gap", "GAP"),
                ("no_close", "NO_CLOSE"), ("closed_without_observed_inplay", "CLOSED_WITHOUT_OBSERVED_INPLAY"),
                # CLOSED before its window began: the initial marketTime did not hold, and the window has no
                # coverage figures, so it is never COMPLETE (Astra REC-R2-1).
                ("closed_before_window", "SCHEDULE_CHANGED"),
                ("schedule_changed", "SCHEDULE_CHANGED"))
MARKET_STATUSES = frozenset({"INACTIVE", "OPEN", "SUSPENDED", "CLOSED"})
_NS = 1_000_000_000
DAY_NS = 86_400 * _NS
WINDOW_BEFORE_NS = 3600 * _NS               # v4 section F: initial marketTime - 60 min
SCAN_BEFORE_NS = 3600 * _NS                 # sessions this far before the day can hold its earliest windows
SCAN_AFTER_NS = 6 * 3600 * _NS              # and this far after it, a late CLOSED
DEFAULT_HEARTBEAT_MS = 5000
STALL_MARGIN_NS = 5 * _NS
SESSION_RE = re.compile(r"rec-([0-9]{8}T[0-9]{6}Z)-[0-9a-f]{8}")
SEGMENT_FILE_RE = re.compile(r"seg-([0-9]{3,6})\.jsonl")
RECV_FILE_RE = re.compile(r"seg-([0-9]{3,6})\.recv\.jsonl")
DAY_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
EVENT_ID_RE = re.compile(r"[0-9]{1,20}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
SESSION_FILES = frozenset({"events.jsonl", "receipt.json", "receipt.partial.json", PRECOUNT_NAME})
# What a crash during receipts.atomic_write_json can leave behind.
CRASH_LEFTOVER_RE = re.compile(r"\.(receipt\.json|receipt\.partial\.json)\.tmp-[0-9]+-[0-9a-f]{8}")
LEDGER_ROW_KEYS = frozenset({"created_at", "session_id", "receipt_path", "receipt_sha256", "segments", "status"})
PRECOUNT_DECLARATION_KEYS = frozenset({"path", "rows", "sha256"})
PRECOUNT_MARKET_KEYS = frozenset({"market_id", "market_start_time"})


# --- small helpers -------------------------------------------------------------------------------------


def day_bounds(day):
    """(start_ns, end_ns) of the UTC date YYYY-MM-DD; ValueError when it is not one."""
    if not isinstance(day, str) or not DAY_RE.fullmatch(day):
        raise ValueError("day must be a UTC date YYYY-MM-DD")
    start = parse_utc_ns(f"{day}T00:00:00Z")
    return start, start + DAY_NS


def _session_start_ns(compact):
    text = f"{compact[0:4]}-{compact[4:6]}-{compact[6:8]}T{compact[9:11]}:{compact[11:13]}:{compact[13:15]}Z"
    return parse_utc_ns(text)


def _ns(value):
    try:
        return parse_utc_ns(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _int(value):
    return value if type(value) is int else None


def _code(value):
    return safe_code(value) if isinstance(value, str) else None


def _no_constant(name):
    return None             # Infinity sizes (F54) and the like sit in fields this module never keeps


def _complement(intervals, low, high):
    """[(a, b)] of [low, high] not covered by intervals."""
    out, cursor = [], low
    for start, end in sorted(intervals):
        if end <= cursor:
            continue
        if start > cursor:
            out.append((cursor, min(start, high)))
        cursor = max(cursor, end)
        if cursor >= high:
            break
    if cursor < high:
        out.append((cursor, high))
    return [(a, b) for a, b in out if b > a]


def _seconds(ns):
    return round(ns / _NS, 3)


# --- sessions and receipts -----------------------------------------------------------------------------


def _load_receipt(directory):
    """(kind "final"|"partial"|None, receipt or None, sha256 of its bytes or None, problem code or None).
    receipt.json wins when present, valid or not."""
    for kind, name in (("final", "receipt.json"), ("partial", "receipt.partial.json")):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        try:
            value = strict_json_loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return kind, None, digest, "receipt_unparseable"
        if not (isinstance(value, dict) and value.get("schema_version") == 1 and value.get("status") == RECEIPT_STATUS
                and isinstance(value.get("segments"), list) and all(isinstance(s, dict) for s in value["segments"])
                and isinstance(value.get("gaps", []), list)):
            return kind, None, digest, "receipt_shape"
        return kind, value, digest, None
    return None, None, None, None


def _segment_meta(segment):
    """The whitelisted receipt fields of one listed segment, or None when its paths are not a segment's."""
    name = segment.get("path")
    match = SEGMENT_FILE_RE.fullmatch(name) if isinstance(name, str) else None
    if match is None or segment.get("recv_path") != name[:-len(".jsonl")] + ".recv.jsonl":
        return None
    mode = segment.get("subscription_mode")
    return {"index": int(match.group(1)), "path": name, "sha256": segment.get("sha256"),
            "recv_sha256": segment.get("recv_sha256"), "bytes": _int(segment.get("bytes")),
            "lines": _int(segment.get("lines")), "close_reason": _code(segment.get("close_reason")),
            "subscription_mode": mode if mode in ("coarse", "explicit") else None,
            "first_received_at": segment.get("first_received_at"), "last_received_at": segment.get("last_received_at")}


def _last_recv_ns(path):
    """received_at of the last row of a receipt sidecar, or None."""
    try:
        lines = [line for line in path.read_bytes().split(b"\n") if line.strip()]
        row = strict_json_loads(lines[-1].decode("utf-8")) if lines else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return _ns(row.get("received_at")) if isinstance(row, dict) else None


def _precount_row_ok(row, listed):
    """True when one precount.jsonl row has the recorder's exact structure (C4) and names a listed segment."""
    if not isinstance(row, dict) or set(row) != PRECOUNT_ROW_KEYS or not isinstance(row["markets"], list):
        return False
    if type(row["segment"]) is not int or row["segment"] not in listed or _ns(row["taken_at"]) is None:
        return False
    if (type(row["lookback_s"]) is not int or type(row["count"]) is not int or row["count"] != len(row["markets"])
            or type(row["possibly_more"]) is not bool or row["subscription_mode"] not in ("coarse", "explicit")
            or type(row["requested_count"]) is not int):
        return False
    for market in row["markets"]:
        if not isinstance(market, dict) or set(market) != PRECOUNT_MARKET_KEYS:
            return False
        if not isinstance(market["market_id"], str) or not MARKET_ID_RE.fullmatch(market["market_id"]):
            return False
        if market["market_start_time"] is not None and _ns(market["market_start_time"]) is None:
            return False
    return True


def precount_problems(directory, receipt, *, final):
    """Check codes for the session's declared pre-count snapshot (C4); [] when it matches.

    A receipt without a "precount" entry predates C4: it is fine only while no precount.jsonl exists. A
    declared snapshot must be a regular file (lstat: no symlink; a path that cannot be inspected fails)
    whose first `rows` lines hash to the declared sha256 and each have the recorder's exact row structure
    for a segment the receipt lists. A final receipt's file holds exactly those rows; a crashed session's
    receipt.partial.json may lag its file by the rows written after its last rewrite. A declared file that
    is missing, truncated or altered is damaged inventory, never the legacy no-inventory case."""
    path = directory / PRECOUNT_NAME
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    except OSError:
        return ["precount_uninspectable"]
    if "precount" not in receipt:
        return [] if info is None else ["precount_undeclared"]
    declared = receipt["precount"]
    if not (isinstance(declared, dict) and set(declared) == PRECOUNT_DECLARATION_KEYS
            and declared["path"] == PRECOUNT_NAME and type(declared["rows"]) is int and declared["rows"] >= 0
            and (declared["sha256"] is None if declared["rows"] == 0
                 else isinstance(declared["sha256"], str) and SHA_RE.fullmatch(declared["sha256"]))):
        return ["precount_declaration_invalid"]
    rows = declared["rows"]
    if info is None:
        return ["precount_missing"] if rows else []
    if not stat.S_ISREG(info.st_mode):
        return ["precount_not_regular"]
    try:
        lines = path.read_bytes().splitlines(keepends=True)
    except OSError:
        return ["precount_uninspectable"]
    if rows == 0:
        return ["precount_rows_mismatch"] if final else []
    if len(lines) < rows or hashlib.sha256(b"".join(lines[:rows])).hexdigest() != declared["sha256"]:
        return ["precount_sha256_mismatch"]
    if final and len(lines) != rows:
        return ["precount_rows_mismatch"]
    listed = {meta["index"] for meta in (_segment_meta(s) for s in receipt["segments"]) if meta is not None}
    seen = []
    for line in lines[:rows]:
        try:
            row = strict_json_loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return ["precount_row_invalid"]
        if not _precount_row_ok(row, listed) or (seen and row["segment"] <= seen[-1]):
            return ["precount_row_invalid"]
        seen.append(row["segment"])
    return []


def _session(directory, start_ns):
    """Whitelisted facts of one session directory (no segment line is read here)."""
    kind, receipt, digest, problem = _load_receipt(directory)
    receipt = receipt or {}
    listed = []
    for segment in receipt.get("segments", []):
        meta = _segment_meta(segment)
        if meta is not None:
            listed.append(meta)
    listed.sort(key=lambda meta: meta["index"])
    try:
        names = sorted(entry.name for entry in directory.iterdir())
    except OSError:
        names = []
    listed_names = {meta["path"] for meta in listed}
    open_segments = sorted(name for name in names if SEGMENT_FILE_RE.fullmatch(name) and name not in listed_names)
    freshness = receipt.get("data_freshness") if isinstance(receipt.get("data_freshness"), dict) else {}
    ends = [start_ns, _ns(receipt.get("closed_at")) or start_ns]
    ends += [_ns(meta["last_received_at"]) or start_ns for meta in listed]
    ends += [_last_recv_ns(directory / (name[:-len(".jsonl")] + ".recv.jsonl")) or start_ns for name in open_segments]
    if kind == "final" and receipt.get("complete") is True and problem is None:
        status = "complete"
    elif problem is not None:
        status = "receipt_invalid"
    elif kind is None:
        status = "no_receipt"
    else:
        status = "crashed"
    precount = receipt.get("precount") if isinstance(receipt.get("precount"), dict) else {}
    damaged = precount_problems(directory, receipt, final=kind == "final") if kind and problem is None else []
    return {"precount_problems": damaged, "session_id": directory.name, "dir": directory, "start_ns": start_ns, "end_ns": max(ends),
            "receipt_kind": kind, "receipt_sha256": digest, "receipt_problem": problem, "status": status,
            "receipt": receipt,
            "segments": listed, "open_segments": open_segments, "names": names,
            "gaps": receipt.get("gaps") if isinstance(receipt.get("gaps"), list) else [],
            "conflate_ms": _int(freshness.get("conflate_ms_echoed")),
            "heartbeat_ms": _int(freshness.get("heartbeat_ms_echoed")),
            "label": _code(freshness.get("label")), "key_type": _code(receipt.get("key_type")),
            "precount_sha256": precount.get("sha256") if isinstance(precount.get("sha256"), str) else None}


def sessions_between(root, start_ns, end_ns):
    """Sessions under root whose span (start to last line or close) meets [start_ns, end_ns), oldest first."""
    try:
        entries = sorted(Path(root).iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    found = []
    for entry in entries:
        match = SESSION_RE.fullmatch(entry.name)
        if not match or entry.is_symlink() or not entry.is_dir():
            continue
        session_start = _session_start_ns(match.group(1))
        if session_start >= end_ns:
            continue
        session = _session(entry, session_start)
        if session["end_ns"] >= start_ns:
            found.append(session)
    return found


# --- segment scan (whitelisted keys only) --------------------------------------------------------------


def _extract(markets, line, at_ns, msg):
    """Fold one parsed line into the per-market entries of its segment, keeping whitelisted keys only."""
    pt = _int(msg.get("pt"))
    changes = msg.get("mc")
    if not isinstance(changes, list):
        return
    for change in changes:
        if not isinstance(change, dict):
            continue
        market_id = change.get("id")
        if not isinstance(market_id, str) or not MARKET_ID_RE.fullmatch(market_id):
            continue
        definition = change.get("marketDefinition")
        definition = definition if isinstance(definition, dict) else {}
        status = definition.get("status")
        status = status if isinstance(status, str) and status in MARKET_STATUSES else None
        market_time = _ns(definition.get("marketTime"))
        event_id = definition.get("eventId")
        event_id = event_id if isinstance(event_id, str) and EVENT_ID_RE.fullmatch(event_id) else None
        entry = markets.get(market_id)
        if entry is None:
            entry = markets[market_id] = {"first_line": line, "first_ns": at_ns, "first_pt": pt,
                                          "first_img": change.get("img") is True, "first_market_time": market_time,
                                          "market_times": [], "event_id": None, "closed": None, "inplay": None,
                                          "last_line": line, "last_ns": at_ns}
        entry["last_line"], entry["last_ns"] = line, at_ns
        if market_time is not None and market_time not in entry["market_times"]:
            entry["market_times"].append(market_time)
        if entry["event_id"] is None and event_id is not None:
            entry["event_id"] = event_id
        if status == "CLOSED" and entry["closed"] is None:
            entry["closed"] = {"line": line, "ns": at_ns, "pt": pt}
        if definition.get("inPlay") is True and entry["inplay"] is None:
            entry["inplay"] = {"line": line, "ns": at_ns, "pt": pt}


def _scan_segment(directory, name):
    """One segment and its sidecar: hashes, line count, line times and whitelisted per-market entries.

    readable turns False at the first line whose sidecar row or JSON cannot be used; hashing goes on, but
    nothing after that line is extracted."""
    index = int(SEGMENT_FILE_RE.fullmatch(name).group(1))
    recv_path = directory / (name[:-len(".jsonl")] + ".recv.jsonl")
    seg_path = directory / name
    result = {"name": name, "index": index, "lines": 0, "bytes": 0, "sha256": None, "recv_sha256": None,
              "readable": True, "times": [], "markets": {}}
    if any(path.is_symlink() or not path.is_file() for path in (seg_path, recv_path)):
        result["readable"] = False
        return result
    recv_data = recv_path.read_bytes()
    result["recv_sha256"] = hashlib.sha256(recv_data).hexdigest()
    rows = recv_data.split(b"\n")
    digest = hashlib.sha256()
    with seg_path.open("rb") as handle:
        for raw_line in handle:
            digest.update(raw_line)
            result["bytes"] += len(raw_line)
            result["lines"] += 1
            number = result["lines"]
            if not result["readable"]:
                continue
            try:
                row = strict_json_loads(rows[number - 1].decode("utf-8")) if number <= len(rows) else None
                at_ns = parse_utc_ns(row["received_at"]) if isinstance(row, dict) else None
                msg = json.loads(raw_line.decode("utf-8"), parse_constant=_no_constant)
            except (ValueError, UnicodeDecodeError, KeyError, RecursionError):
                row = msg = None
            if not (isinstance(row, dict) and set(row) == RECV_ROW_KEYS and row["line"] == number
                    and at_ns is not None and isinstance(msg, dict)):
                result["readable"] = False
                continue
            result["times"].append(at_ns)
            _extract(result["markets"], number, at_ns, msg)
    result["sha256"] = digest.hexdigest()
    if sum(1 for row in rows if row.strip()) != result["lines"]:
        result["readable"] = False
    return result


def _liveness_ns(session):
    """The stream client's liveness timeout for this session's echo, plus a margin: a longer silence
    between two lines means the process was not running (sleep) or the wall clock jumped."""
    heartbeat = session["heartbeat_ms"] if session["heartbeat_ms"] else DEFAULT_HEARTBEAT_MS
    conflate = session["conflate_ms"] or 0
    return (conflate + 2 * heartbeat) * 1_000_000 + STALL_MARGIN_NS


def _tolerance_ns(session):
    """One conflation interval (the echoed conflateMs; the heartbeat interval when unconflated)."""
    if session["conflate_ms"]:
        return session["conflate_ms"] * 1_000_000
    return (session["heartbeat_ms"] or DEFAULT_HEARTBEAT_MS) * 1_000_000


def _segments(session):
    """Scanned segments of one session in index order, each with its receipt facts and verification."""
    listed = {meta["path"]: meta for meta in session["segments"]}
    out = []
    liveness = _liveness_ns(session)
    for name in sorted(set(listed) | set(session["open_segments"]), key=lambda n: int(SEGMENT_FILE_RE.fullmatch(n).group(1))):
        scan = _scan_segment(session["dir"], name)
        meta = listed.get(name)
        verified = (session["status"] == "complete" and meta is not None and scan["readable"]
                    and scan["sha256"] == meta["sha256"] and scan["recv_sha256"] == meta["recv_sha256"]
                    and scan["lines"] == meta["lines"] and scan["bytes"] == meta["bytes"])
        times = scan["times"]
        stalls = [(a, b) for a, b in zip(times, times[1:]) if b - a > liveness]
        out.append({"session_id": session["session_id"], "segment": scan["index"], "verified": verified,
                    "listed": meta is not None, "mode": meta["subscription_mode"] if meta else None,
                    "close_reason": meta["close_reason"] if meta else None,
                    "first_ns": times[0] if times else None, "last_ns": times[-1] if times else None,
                    "stalls": stalls, "tolerance_ns": _tolerance_ns(session), "markets": scan["markets"],
                    "lines": scan["lines"]})
    return out


def _precount_inventory(session):
    """{market_id: (catalogue start ns or None, verified)} from the session's precount.jsonl (C4); {} when the
    session has none or its declared snapshot is damaged (precount_problems: then counted, never used)."""
    path = session["dir"] / PRECOUNT_NAME
    if session["precount_problems"] or path.is_symlink() or not path.is_file():
        return {}
    verified = session["status"] == "complete" and session["precount_sha256"] == file_sha256(path)
    inventory = {}
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            row = strict_json_loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return inventory
        if not isinstance(row, dict) or set(row) != PRECOUNT_ROW_KEYS or not isinstance(row["markets"], list):
            return inventory
        for item in row["markets"]:
            market_id = item.get("market_id") if isinstance(item, dict) else None
            if isinstance(market_id, str) and MARKET_ID_RE.fullmatch(market_id):
                start, was_verified = inventory.get(market_id, (None, False))
                inventory[market_id] = (start or _ns(item.get("market_start_time")), was_verified or verified)
    return inventory


# --- per-market classification -------------------------------------------------------------------------


def _where(observation, line, at_ns, pt):
    return {"session_id": observation["session_id"], "segment": observation["segment"], "line": line,
            "received_at": utc_from_ns(at_ns), "publish_pt": pt}


def _cause(start, end, previous, following, absent):
    """(cause, tolerated, evidence) of one uncovered stretch after the market's first observation in its window.
    evidence is the explicit absent segments a dropped_explicit cause relied on (bot-33-578: the caller flags the
    row unverified when any of them is unverified), else []."""
    explicit = [segment for segment in absent if segment["mode"] == "explicit"]
    if explicit:
        return "dropped_explicit", False, explicit
    before = previous["obs"] if previous else None
    if before is not None and any(stall[0] == start for stall in before["stalls"]):
        return "stall_in_segment", False, []
    if following is None:
        return ("absent_from_segment" if absent else "recording_ended"), False, []
    after = following["obs"]
    if before is None or before["session_id"] != after["session_id"]:
        return "between_sessions", False, []
    if absent:
        return "absent_from_segment", False, []
    cause = before["close_reason"] or "segment_not_closed"
    tolerated = (cause == "scheduled_refresh" and after["segment"] == before["segment"] + 1
                 and after["entry"]["first_img"] and following["from"] == after["entry"]["first_ns"]
                 and end - start <= before["tolerance_ns"])
    return cause, tolerated, []


def _classify(flags):
    for flag, name in FLAG_CLASSES:
        if flag in flags:
            return name
    return "COMPLETE"


def _day_anchor(observations, catalogue):
    """The instant that puts an observed market on a day: initial marketTime, else catalogue start, else
    its first observation."""
    anchor = observations[0]["entry"]["first_market_time"]
    if anchor is not None:
        return anchor
    return catalogue if catalogue is not None else observations[0]["entry"]["first_ns"]


def _observed_row(market_id, observations, segments, session_ends, catalogue):
    """The index row of one observed market: class, flag set, window, coverage and gaps."""
    first = observations[0]
    entries = [obs["entry"] for obs in observations]
    anchor_ns = first["entry"]["first_market_time"]
    event_id = next((entry["event_id"] for entry in entries if entry["event_id"]), None)
    closed_obs = min((obs for obs in observations if obs["entry"]["closed"]),
                     key=lambda obs: obs["entry"]["closed"]["ns"], default=None)
    closed = closed_obs["entry"]["closed"] if closed_obs else None
    inplay_obs = min((obs for obs in observations if obs["entry"]["inplay"]
                      and (closed is None or obs["entry"]["inplay"]["ns"] <= closed["ns"])),
                     key=lambda obs: obs["entry"]["inplay"]["ns"], default=None)
    flags = set()
    if closed is None:
        flags.add("no_close")
    elif inplay_obs is None:
        flags.add("closed_without_observed_inplay")
    window, coverage, gaps, late_ns, gap_ns = None, [], [], 0, 0
    relevant = observations
    absence_evidence = []                   # explicit absent segments a dropped_explicit flag relied on
    if anchor_ns is None:
        flags.add("no_anchor")
    else:
        if any(value != anchor_ns for entry in entries for value in entry["market_times"]):
            flags.add("schedule_changed")
        window_from = anchor_ns - WINDOW_BEFORE_NS
        if closed is not None:
            window_to = closed["ns"]
        else:
            window_to = session_ends[observations[-1]["session_id"]]
            if window_to <= window_from:            # recording ended before the window began: the hour before off
                window_to = anchor_ns
        window = {"from": utc_from_ns(window_from), "to": utc_from_ns(window_to)}
        pieces = []
        for obs in observations:
            start, end = obs["entry"]["first_ns"], obs["last_ns"]
            if closed is not None:
                if start > closed["ns"]:
                    continue
                end = min(end, closed["ns"]) if obs is closed_obs else end
            for piece_from, piece_to in _complement(obs["stalls"], start, end) if end > start else [(start, end)]:
                pieces.append({"from": piece_from, "to": piece_to, "obs": obs})
        pieces.sort(key=lambda piece: (piece["from"], piece["to"]))
        inside = [piece for piece in pieces if piece["to"] >= window_from and piece["from"] <= window_to]
        relevant = [obs for obs in observations if any(piece["obs"] is obs for piece in inside)] or [first]
        first_seen = min((max(piece["from"], window_from) for piece in inside), default=None)
        if window_to <= window_from:              # CLOSED before the window began: no coverage figures
            flags.add("closed_before_window")
        elif first_seen is None:                  # nothing observed inside the window at all
            flags.update(("late_start", "never_in_window"))
            late_ns = window_to - window_from
            explicit = [segment for segment in segments if segment["mode"] == "explicit"
                        and segment["first_ns"] is not None and segment["first_ns"] < window_to
                        and segment["last_ns"] > window_from and market_id not in segment["markets"]]
            if explicit:
                flags.add("dropped_explicit")
                absence_evidence += explicit
        else:
            for start, end in _complement([(p["from"], p["to"]) for p in inside], window_from, window_to):
                if first_seen is None or end <= first_seen:
                    flags.add("late_start")
                    late_ns += end - start
                    continue
                previous = max((p for p in inside if p["to"] <= start), key=lambda p: p["to"], default=None)
                following = min((p for p in inside if p["from"] >= end), key=lambda p: p["from"], default=None)
                absent = [segment for segment in segments if segment["first_ns"] is not None
                          and segment["first_ns"] < end and segment["last_ns"] > start
                          and market_id not in segment["markets"]]
                cause, tolerated, evidence = _cause(start, end, previous, following, absent)
                if cause == "dropped_explicit":
                    flags.add("dropped_explicit")
                    absence_evidence += evidence
                if not tolerated:
                    flags.add("gap")
                    gap_ns += end - start
                gaps.append({"from": utc_from_ns(start), "to": utc_from_ns(end), "seconds": _seconds(end - start),
                             "cause": cause, "tolerated": tolerated})
    # An unverified segment is still evidence: the row stays visible (dropped_explicit) and is flagged unverified
    # whether the evidence was an observation or an explicit absence (bot-33-578).
    if any(not s["verified"] for s in relevant + absence_evidence):
        flags.add("unverified")
    image_obs = next((obs for obs in observations if obs["entry"]["first_img"]), None)
    for obs in observations:
        entry = obs["entry"]
        coverage.append({"session_id": obs["session_id"], "segment": obs["segment"], "first_line": entry["first_line"],
                         "last_line": entry["last_line"], "from": utc_from_ns(entry["first_ns"]),
                         "to": utc_from_ns(obs["last_ns"]), "image": entry["first_img"], "verified": obs["verified"],
                         "stalls": len([s for s in obs["stalls"] if s[0] >= entry["first_ns"]])})
    return {"market_id": market_id, "event_id": event_id, "class": _classify(flags), "flags": sorted(flags),
            "anchor": utc_from_ns(anchor_ns) if anchor_ns is not None else None,
            "anchor_source": "market_definition" if anchor_ns is not None else None,
            "catalogue_start": utc_from_ns(catalogue) if catalogue is not None else None, "window": window,
            "first_image": _where(image_obs, image_obs["entry"]["first_line"], image_obs["entry"]["first_ns"],
                                  image_obs["entry"]["first_pt"]) if image_obs else None,
            "first_inplay_publish": _where(inplay_obs, inplay_obs["entry"]["inplay"]["line"],
                                           inplay_obs["entry"]["inplay"]["ns"], inplay_obs["entry"]["inplay"]["pt"])
            if inplay_obs else None,
            "closed_publish": _where(closed_obs, closed["line"], closed["ns"], closed["pt"]) if closed_obs else None,
            "late_start_s": _seconds(late_ns), "gap_s": _seconds(gap_ns), "coverage": coverage, "gaps": gaps}


def _not_observed_row(market_id, start_ns, verified):
    flags = {"not_observed"} if verified else {"not_observed", "unverified"}
    return {"market_id": market_id, "event_id": None, "class": _classify(flags), "flags": sorted(flags),
            "anchor": utc_from_ns(start_ns), "anchor_source": "catalogue", "catalogue_start": utc_from_ns(start_ns),
            "window": {"from": utc_from_ns(start_ns - WINDOW_BEFORE_NS), "to": None}, "first_image": None,
            "first_inplay_publish": None, "closed_publish": None, "late_start_s": 0.0, "gap_s": 0.0,
            "coverage": [], "gaps": []}


def build_index(paths, *, root, day, clock):
    """Write the per-market structural index of one UTC day to recordings/index/<day>.json under the
    profile (0600, atomically) and return its counts-only summary (no market id, time or price).

    A market belongs to the day of its anchor: its initial marketTime, else its catalogue start (from a
    pre-count), else its first observation. Sessions are read from [day - 1 h, day + 1 d + 6 h)."""
    day_start, day_end = day_bounds(day)
    sessions = sessions_between(root, day_start - SCAN_BEFORE_NS, day_end + SCAN_AFTER_NS)
    segments, observations, inventory, session_ends, session_rows = [], {}, {}, {}, []
    for session in sessions:
        scanned = _segments(session)
        segments += scanned
        session_ends[session["session_id"]] = max([s["last_ns"] for s in scanned if s["last_ns"] is not None]
                                                  + [session["start_ns"]])
        for segment in scanned:
            for market_id, entry in segment["markets"].items():
                observations.setdefault(market_id, []).append(dict(segment, entry=entry))
        for market_id, (start, verified) in _precount_inventory(session).items():
            known_start, known_verified = inventory.get(market_id, (None, False))
            inventory[market_id] = (known_start or start, known_verified or verified)
        session_rows.append({"session_id": session["session_id"], "status": session["status"],
                             "key_type": session["key_type"], "label": session["label"],
                             "segments": len(scanned), "verified_segments": sum(1 for s in scanned if s["verified"]),
                             "open_segments": len(session["open_segments"]),
                             "precount": "damaged" if session["precount_problems"] else
                             "present" if (session["dir"] / PRECOUNT_NAME).is_file() else "none"})
    rows = []
    for market_id, found in observations.items():
        found.sort(key=lambda obs: (obs["entry"]["first_ns"], obs["segment"]))
        catalogue = inventory.get(market_id, (None,))[0]
        if day_start <= _day_anchor(found, catalogue) < day_end:
            rows.append(_observed_row(market_id, found, segments, session_ends, catalogue))
    for market_id, (start, verified) in inventory.items():
        if market_id not in observations and start is not None and day_start <= start < day_end:
            rows.append(_not_observed_row(market_id, start, verified))
    rows.sort(key=lambda row: (row["anchor"] or "", row["market_id"]))
    classes = {name: sum(1 for row in rows if row["class"] == name) for name in CLASSES}
    flags = {flag: sum(1 for row in rows if flag in row["flags"]) for flag, _ in FLAG_CLASSES}
    document = {"schema_version": INDEX_SCHEMA_VERSION, "status": INDEX_STATUS, "day": day, "built_at": clock.utc_now(),
                "time_note": TIME_NOTE, "scan": {"from": utc_from_ns(day_start - SCAN_BEFORE_NS),
                                                 "to": utc_from_ns(day_end + SCAN_AFTER_NS)},
                "classes_by_precedence": list(CLASSES), "sessions": session_rows, "markets": rows}
    paths.recordings_index_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = atomic_write_json(paths.recordings_index_dir / f"{day}.json", document)
    summary = {"status": INDEX_STATUS, "day": day, "sessions": len(sessions),
               "sessions_by_status": {status: sum(1 for s in sessions if s["status"] == status)
                                      for status in ("complete", "crashed", "receipt_invalid", "no_receipt")},
               "inventory_damaged_sessions": sum(1 for s in sessions if s["precount_problems"]),
               "segments": len(segments), "segments_verified": sum(1 for s in segments if s["verified"]),
               "markets": len(rows), "classes": classes, "flags": flags,
               "gaps": sum(1 for row in rows for gap in row["gaps"] if not gap["tolerated"]),
               "tolerated_handovers": sum(1 for row in rows for gap in row["gaps"] if gap["tolerated"]),
               "index_sha256": digest}
    return summary


# --- verify-day ----------------------------------------------------------------------------------------


def _run_ledger(paths):
    """(rows by session_id, problem code or None) from recordings/run-ledger.jsonl (whitelisted keys)."""
    path = paths.recordings_dir / "run-ledger.jsonl"
    rows = {}
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return rows, None
    except OSError:
        return rows, "run_ledger_unreadable"
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            row = strict_json_loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return rows, "run_ledger_unreadable"
        if not isinstance(row, dict) or not LEDGER_ROW_KEYS <= set(row) or set(row) - LEDGER_ROW_KEYS - {"invocation_id"}:
            return rows, "run_ledger_unreadable"
        rows.setdefault(row["session_id"], []).append(row)
    return rows, None


def expected_gaps(segments):
    """The gaps the recorder lists for these receipt segments: one per pair of consecutive segments with
    lines, from the earlier one's last line to the later one's first, with the earlier one's close reason."""
    with_lines = [s for s in segments if type(s.get("lines")) is int and s["lines"] > 0]
    return [{"after_segment": a.get("index"), "from_received_at": a.get("last_received_at"),
             "to_received_at": b.get("first_received_at"), "reason": a.get("close_reason")}
            for a, b in zip(with_lines, with_lines[1:])]


def verify_day(paths, *, root, day):
    """Offline checks of every session meeting the UTC day; returns counts and failed check names only.

    Checks: each closed session verifies (recorder.verify_recording); its receipt.json sha256 equals the
    one run-ledger row for it, whose receipt_path names it, whose invocation_id equals the receipt's (both
    absent is a match; ledger_invocation_mismatch) and whose segments count equals the receipt's
    (ledger_segments_mismatch); events.jsonl is present; the receipt's gap list equals the gaps its segment
    boundaries imply; its declared pre-count snapshot matches (precount_problems; damaged inventory fails, a
    receipt from before C4 without one passes); no file but the recorder's own; and no segment or recv file
    beside a final receipt that the receipt does not list (segment_not_in_receipt, recv_not_in_receipt;
    bot-33-740). A session that never closed (receipt.partial.json only, or no receipt) is classified, not
    failed: its listed segments are still hash-checked and its gaps compared, and the one segment the
    recorder can leave unlisted (index len(listed), the subscription open at the crash) is counted as open;
    any other unlisted segment beside a partial receipt fails. Leftovers of an interrupted atomic write are
    counted, not failed. A ledger row whose session directory is missing fails, as does a ledger that cannot be
    read; a torn or bad ledger line fails run_ledger_torn_line with its line index unless a typed repair row
    exempts exactly those bytes (section 3.8)."""
    day_start, day_end = day_bounds(day)
    sessions = sessions_between(root, day_start, day_end)
    ledger, ledger_problem = _run_ledger(paths)
    failures = []
    counts = {"complete": 0, "crashed": 0, "receipt_invalid": 0, "no_receipt": 0, "closed_not_complete": 0}
    open_segments = leftovers = 0

    def fail(session_id, check, segment=None):
        entry = {"session_id": session_id, "check": check}
        if segment is not None:
            entry["segment"] = segment
        failures.append(entry)

    if ledger_problem:
        fail(None, ledger_problem)
    for session in sessions:
        sid, directory = session["session_id"], session["dir"]
        kind = session["receipt_kind"]
        if session["status"] == "complete":
            counts["complete"] += 1
        elif kind == "final" and session["receipt_problem"] is None:
            counts["closed_not_complete"] += 1
        else:
            counts[session["status"]] += 1
        open_segments += len(session["open_segments"])
        for name in session["names"]:
            if CRASH_LEFTOVER_RE.fullmatch(name):
                leftovers += 1
            elif not (name in SESSION_FILES or SEGMENT_FILE_RE.fullmatch(name) or RECV_FILE_RE.fullmatch(name)):
                fail(sid, "unexpected_file")
        if session["receipt_problem"] is not None:
            fail(sid, session["receipt_problem"])
        elif kind == "final":
            for check in verify_recording(directory)["checks"]:
                if check["status"] != "passed":
                    fail(sid, check["check"], check.get("segment"))
            # bot-33-740: a final receipt lists every segment the session wrote, so nothing else may sit beside it.
            for name in session["open_segments"]:
                fail(sid, "segment_not_in_receipt", int(SEGMENT_FILE_RE.fullmatch(name).group(1)))
            listed_names = {meta["path"] for meta in session["segments"]}
            for name in session["names"]:
                match = RECV_FILE_RE.fullmatch(name)
                if match and f"seg-{match.group(1)}.jsonl" not in listed_names:
                    fail(sid, "recv_not_in_receipt", int(match.group(1)))
            rows = ledger.get(sid, [])
            if len(rows) != 1:
                fail(sid, "receipt_without_ledger_row" if not rows else "ledger_rows_duplicated")
            else:
                row = rows[0]
                if row.get("receipt_sha256") != session["receipt_sha256"]:
                    fail(sid, "ledger_receipt_sha256_mismatch")
                named = row.get("receipt_path")
                if not isinstance(named, str) or Path(named).resolve() != (directory / "receipt.json").resolve():
                    fail(sid, "ledger_receipt_path_mismatch")
                # R2: the row against the receipt already strict-parsed above (both ids absent is a match).
                receipt = session["receipt"]
                if row.get("invocation_id") != receipt.get("invocation_id"):
                    fail(sid, "ledger_invocation_mismatch")
                if type(row.get("segments")) is not int or row["segments"] != len(receipt["segments"]):
                    fail(sid, "ledger_segments_mismatch")
            if not (directory / "events.jsonl").is_file():
                fail(sid, "events_missing")
        elif kind == "partial":
            receipt = strict_json_loads((directory / "receipt.partial.json").read_text(encoding="utf-8"))
            # The recorder leaves at most one segment unlisted after a crash: the one at index len(listed).
            for name in session["open_segments"]:
                index = int(SEGMENT_FILE_RE.fullmatch(name).group(1))
                if index != len(receipt["segments"]):
                    fail(sid, "segment_not_in_receipt", index)
            for segment in receipt["segments"]:
                checks = []
                _verify_segment(directory, segment, lambda name, ok, segment=None, detail=None:
                                checks.append((name, ok, segment)))
                for name, ok, index in checks:
                    if not ok:
                        fail(sid, name, index)
            if sid in ledger:
                fail(sid, "ledger_row_without_receipt")
        for problem in session["precount_problems"]:
            fail(sid, problem)
        if kind is not None and session["receipt_problem"] is None:
            receipt = strict_json_loads((directory / ("receipt.json" if kind == "final" else "receipt.partial.json"))
                                        .read_text(encoding="utf-8"))
            if receipt.get("gaps") != expected_gaps(receipt["segments"]):
                fail(sid, "gaps_do_not_match_segments")
    present = {session["session_id"] for session in sessions}
    for sid, rows in ledger.items():
        match = SESSION_RE.fullmatch(sid) if isinstance(sid, str) else None
        if match and day_start <= _session_start_ns(match.group(1)) < day_end and sid not in present:
            fail(sid, "ledger_row_without_session")
    return {"status": "verified" if not failures else "failed", "day": day, "sessions": len(sessions),
            "sessions_by_status": counts, "open_segments": open_segments, "crash_leftovers": leftovers,
            "checks_failed": len(failures), "failures": failures}
