"""Exchange Stream API recorder: raw mcm lines, byte for byte, in segment files that replay on their own.

One session is one invocation; one segment is one subscription. Every segment starts at a fresh
SUB_IMAGE and the recorder never resubscribes with clocks, so each seg-NNN.jsonl is read by
betangel_research.stream.messages() (and the replay and motif code built on it) without any other
file. A line is written only if it is an mcm for the active subscription with an integer,
non-decreasing pt, no segmentType and no carriage return. Status and connection messages go to
events.jsonl. Time between segments is listed as a gap and never forward-filled.

Receipt clocks are the local wall clock and monotonic clock at line read, unauthenticated; pt is
Betfair publish time. The recorder is read-only: it never places, cancels or changes an order. It
uses the delayed key unless recorder.allow_live_key is set, because Betfair does not permit
read-only use of the live key (F46); require_delayed_key refuses any other key whatever that setting
says. Delayed-key data is 1-180 s delayed and conflated to 180 s (F44, F51), so it cannot support
features finer than 180 s.

Every run goes through recording admission (admission.recorder_admission) before it connects:
INHIBIT or a live ADMISSION refuses it, it holds recordings/.recorder.lock for the whole run and
writes recorder.pid. It never takes .gateway.lock. end_at_ns bounds the run on the wall clock
(time_ns), checked with every stop check and again before every pre-count, login and relogin,
because the monotonic clock stops while the Mac sleeps. Each segment's pre-count, the catalogue's
(marketId, marketStartTime) pairs, is kept beside the session in precount.jsonl as structural data.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re

from .. import stream as _stream
from .admission import check_invocation_id, recorder_admission
from .book import replay_market_time
from .config import config_sha256, validate_config
from .constants import STREAM_STATUS_ERROR_CODES
from .errors import BetfairError, ConfigError, StreamError, TransportError, classify, safe_code
from .esa import LOCAL_CONNECTION_CODES, ReconnectPolicy, StreamClient
from .filters import MARKET_ID_RE, to_rest_market_filter, to_stream_market_filter
from .paths import require_outside_worktrees
from .receipts import (SYSTEM_CLOCK, append_jsonl, atomic_write_json, bytes_sha256, canonical_sha256, code_hashes,
                       file_sha256, full_fsync, git_state, parse_utc_ns, strict_json_loads, utc_compact_from_ns,
                       utc_from_ns, utc_ms_from_ns)
from .secret import assert_clean

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_STATUS = "betfair_stream_recording_not_profitability_evidence"
DATA_KIND = "betfair_exchange_stream"
COVERAGE = "continuous_within_segment_only_gaps_listed"
REPLAY_READER = "betangel_research.stream.messages"
TIMESTAMP_PROVENANCE = {
    "received_at": "local_wall_clock_utc_at_line_read_not_independently_verified",
    "monotonic_ns": "time.monotonic_ns_at_line_read_comparable_within_session_only",
    "pt": "betfair_publish_time_ms",
}
LICENCE_NOTE = ("Betfair data: personal non-commercial use (General T&Cs cl.19.3, Wayback snapshot 2026-05-11); "
                "keep outside Git")
LIMITATIONS = ["pt is publisher time", "receipt clocks unauthenticated", "gaps are not forward-filled",
               "delayed-key data unsuitable for sub-180s features",
               "motif compatibility checked on synthetic data only"]
FRESHNESS_NOTES = {
    "delayed_conflated": ("delayed key: 1-180 s delayed snapshots, 3 levels, stream conflated "
                          "(official Betfair documentation, accessed 2026-10-10)"),
    "live_unconflated": ("live key: server echoed conflateMs 0; Betfair does not permit read-only use of the "
                         "live key (official Betfair documentation, accessed 2026-10-10)"),
    "live_conflated": ("live key: conflated or conflation not echoed; Betfair does not permit read-only use of "
                       "the live key (official Betfair documentation, accessed 2026-10-10)"),
}
REJECT_REASONS = ("not_mcm", "superseded_id", "pt_not_int", "pt_regression", "segmented", "carriage_return")
RECV_ROW_KEYS = frozenset({"line", "byte_offset", "bytes", "sha256", "received_at", "monotonic_ns", "pt", "clk"})
PRECOUNT_LOOKBACK_S = 1800
PRECOUNT_LIMIT = 1000          # the lister asks for at most 1000 ids, so 1000 means "possibly more"
PRECOUNT_NAME = "precount.jsonl"
PRECOUNT_ROW_KEYS = frozenset({"segment", "taken_at", "lookback_s", "count", "possibly_more", "subscription_mode",
                               "requested_count", "markets"})
END_AT_REACHED = "end_at_reached"
POLL_S = 1.0
PROGRESS_INTERVAL_S = 60
KEEPALIVE_RETRY_S = 60
_NS = 1_000_000_000
_SEGMENT_RE = re.compile(r"seg-[0-9]{3,6}\.jsonl")
# Transport failures worth another connection attempt; anything else (network_forbidden, an
# allowlist refusal, a certificate that does not verify) stops the session.
_TRANSPORT_RETRY_CODES = frozenset({"connect_failed", "tls_failed", "deadline_exceeded"})
# Local codes that make a connection unusable but are worth a new one. A FAILURE status without a
# code, or an unknown statusCode, is "unrecognized" and stops the session instead.
_LOCAL_RECONNECT_CODES = LOCAL_CONNECTION_CODES - {"status_failure", "unknown_status_code"}


def line_acceptance(raw, msg, prev_pt, active_id):
    """None when the line may be written; otherwise the reason it may not (see REJECT_REASONS).

    The pt rule mirrors stream.messages(): an int (never a bool) not below the previous pt, with -1
    as the starting floor.
    """
    if not isinstance(msg, dict) or msg.get("op") != "mcm":
        return "not_mcm"
    request_id = msg.get("id")
    if type(request_id) is not int or type(active_id) is not int or request_id != active_id:
        return "superseded_id"
    pt = msg.get("pt")
    if type(pt) is not int:
        return "pt_not_int"
    if pt < (prev_pt if prev_pt is not None else -1):
        return "pt_regression"
    if msg.get("segmentType") is not None:
        return "segmented"
    if b"\r" in bytes(raw):
        return "carriage_return"
    return None


def _short(text, limit=200):
    return text if len(text) <= limit else text[:limit] + "..."


def replay_check(path):
    """(replay_check dict, market ids whose first change lacks marketDefinition.marketTime).

    stream.messages() must consume the whole file, the first change of every market must carry a
    marketTime, and every marketTime in the file must be one the replay code parses on Python 3.10
    and later (book.replay_market_time: whole milliseconds, 0, 3 or 6 fraction digits), or the
    check fails.
    """
    seen = set()
    missing = []
    problem = None
    try:
        for msg in _stream.messages(path):
            changes = msg.get("mc", [])
            if not isinstance(changes, list):
                problem = "mc_not_a_list"
                break
            for change in changes:
                if not isinstance(change, dict) or "id" not in change:
                    problem = "market_change_without_id"
                    break
                market_id = str(change["id"])
                definition = change.get("marketDefinition")
                definition = definition if isinstance(definition, dict) else {}
                if market_id not in seen:
                    seen.add(market_id)
                    if replay_market_time(definition.get("marketTime")) is None:
                        missing.append(market_id)
                elif "marketTime" in definition and replay_market_time(definition["marketTime"]) is None:
                    # The replay readers parse every later marketTime too (schedule changes).
                    problem = "market_time_not_replayable"
                    break
            if problem:
                break
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        problem = f"{type(exc).__name__}: {_short(str(exc))}"
    if problem is None and missing:
        problem = "markets_without_initial_definition"
    return ({"reader": REPLAY_READER, "status": "passed" if problem is None else "failed", "error": problem},
            sorted(missing))


def _code(exc):
    """Safe code text for an exception: its code or status, else its class name."""
    for attribute in ("code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, str) and value:
            return safe_code(value)
    return type(exc).__name__


def _start_time(value):
    """A catalogue marketStartTime as UTC text with milliseconds, or None when absent or not a timestamp."""
    if not isinstance(value, str):
        return None
    try:
        return utc_ms_from_ns(parse_utc_ns(value))
    except ValueError:
        return None


def _clean_precount(items):
    """[(market_id, market_start_time or None)] from a market_lister result, first occurrence kept.

    Each item is a market id, or a (market id, catalogue marketStartTime) pair (ReadClient.precount_markets).
    Only the id and the normalised start time are kept: the whitelist of the structural snapshot (C4)."""
    if not isinstance(items, (list, tuple)):
        raise ValueError("market_lister_must_return_a_list")
    cleaned, seen = [], set()
    for item in items:
        market_id, start = (item, None) if isinstance(item, str) else (
            tuple(item) if isinstance(item, (list, tuple)) and len(item) == 2 else (None, None))
        if not isinstance(market_id, str) or not MARKET_ID_RE.fullmatch(market_id):
            raise ValueError("market_lister_returned_invalid_id")
        if market_id not in seen:
            seen.add(market_id)
            cleaned.append((market_id, _start_time(start)))
    return cleaned


class _SegmentWriter:
    """seg-NNN.jsonl (accepted raw lines) and seg-NNN.recv.jsonl (one receipt row per line)."""

    def __init__(self, directory, index, *, clock, fsync_interval_s, connection_id, plan):
        self.index = index
        self.name = f"seg-{index:03d}.jsonl"
        self.recv_name = f"seg-{index:03d}.recv.jsonl"
        self._clock = clock
        self._fsync_ns = int(fsync_interval_s * _NS)
        self._segment = self._open(directory / self.name)
        try:
            self._recv = self._open(directory / self.recv_name)
        except BaseException:
            self._segment.close()
            raise
        self._hash = hashlib.sha256()
        self._recv_hash = hashlib.sha256()
        self._last_sync_ns = clock.monotonic_ns()
        self._last_line_ns = None
        self.closed = False
        self.bytes = 0
        self.lines = 0
        self.last_pt = None
        self.superseded_dropped = 0
        self.info = {"index": index, "path": self.name, "connection_id": connection_id,
                     "subscription_mode": plan["mode"], "requested_count": plan["requested_count"],
                     "requested_market_ids_sha256": plan["ids_sha256"]}
        self._first_pt = None
        self._first_received_at = None
        self._last_received_at = None
        self._market_ids = set()
        self._stats = {"heartbeat_lines": 0, "con_true_lines": 0, "status_503_lines": 0, "img_market_changes": 0}
        self._max_gap_ms = 0

    @staticmethod
    def _open(path):
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return os.fdopen(fd, "wb")

    def prepare(self, raw, msg, received_at, monotonic_ns):
        """(segment bytes, receipt-row bytes) for one accepted line; nothing is written yet."""
        raw = bytes(raw)
        assert_clean(raw, where=self.name)
        clk = msg.get("clk")
        row = {"line": self.lines + 1, "byte_offset": self.bytes, "bytes": len(raw), "sha256": bytes_sha256(raw),
               "received_at": received_at, "monotonic_ns": monotonic_ns, "pt": msg["pt"],
               "clk": clk if isinstance(clk, str) else None}
        row_bytes = (json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
                     + "\n").encode("utf-8")
        assert_clean(row_bytes, where=self.recv_name)
        return raw + b"\n", row_bytes

    def commit(self, line, row_bytes, msg, received_at, monotonic_ns):
        self._segment.write(line)
        self._recv.write(row_bytes)
        self._hash.update(line)
        self._recv_hash.update(row_bytes)
        self.bytes += len(line)
        self.lines += 1
        pt = msg["pt"]
        if self._first_pt is None:
            self._first_pt = pt
            self._first_received_at = received_at
        self.last_pt = pt
        self._last_received_at = received_at
        if self._last_line_ns is not None:
            self._max_gap_ms = max(self._max_gap_ms, (monotonic_ns - self._last_line_ns) // 1_000_000)
        self._last_line_ns = monotonic_ns
        changes = msg.get("mc") if isinstance(msg.get("mc"), list) else []
        for change in changes:
            if isinstance(change, dict):
                if isinstance(change.get("id"), str):
                    self._market_ids.add(change["id"])
                if change.get("img") is True:
                    self._stats["img_market_changes"] += 1
        if msg.get("ct") == "HEARTBEAT":
            self._stats["heartbeat_lines"] += 1
        if any(isinstance(c, dict) and c.get("con") is True for c in changes):
            self._stats["con_true_lines"] += 1
        if msg.get("status") == 503:
            self._stats["status_503_lines"] += 1
        if self._clock.monotonic_ns() - self._last_sync_ns >= self._fsync_ns:
            self.sync()

    def sync(self):
        for handle in (self._segment, self._recv):
            handle.flush()
            full_fsync(handle.fileno())
        self._last_sync_ns = self._clock.monotonic_ns()

    def close(self, reason):
        """Flush and fsync both files; returns the segment's receipt fields (no replay check yet)."""
        try:
            self.sync()
        finally:
            self._segment.close()
            self._recv.close()
            self.closed = True
        return dict(self.info, sha256=self._hash.hexdigest(), bytes=self.bytes, lines=self.lines,
                    recv_path=self.recv_name, recv_sha256=self._recv_hash.hexdigest(), first_pt=self._first_pt,
                    last_pt=self.last_pt, first_received_at=self._first_received_at,
                    last_received_at=self._last_received_at, market_ids=sorted(self._market_ids),
                    superseded_dropped=self.superseded_dropped, max_inter_line_gap_ms=self._max_gap_ms,
                    close_reason=reason, **self._stats)

    def abort(self):
        """Best effort after a crash: keep what was written, flushed to disk."""
        if self.closed:
            return
        for handle in (self._segment, self._recv):
            try:
                handle.flush()
                full_fsync(handle.fileno())
            except (OSError, ValueError):
                pass
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        self.closed = True


class StreamRecorder:
    """Records the market stream for config["recorder"].market_filter into one session directory per run.

    stream_factory() must return a new esa.StreamClient built on this same session object with the
    config's stream host, port, heartbeat_ms and conflate_ms (the values the receipt states, and the
    session the live-key gate checked). Anything else stops the session before it connects, with
    reason stream_factory_mismatch:<field>. market_lister(rest_filter) returns market ids, or
    (market id, marketStartTime) pairs (ReadClient.precount_markets), first to start first.

    require_delayed_key (record --require-delayed-key, C6) refuses unless the session's app_key_slot is
    "delayed", whatever recorder.allow_live_key says. invocation_id (record --invocation-id, C7) is
    written into recorder.pid, receipt.partial.json, receipt.json and the run-ledger row.
    """

    def __init__(self, config, *, paths, session, stream_factory, market_lister, clock=SYSTEM_CLOCK, rng=None,
                 emit=None, invocation_id=None, require_delayed_key=False):
        self.config = validate_config(config, paths=paths)
        self.paths = paths
        stream_config, recorder = self.config["stream"], self.config["recorder"]
        slot = session.app_key_slot
        if slot not in ("delayed", "live"):
            raise ConfigError("recorder_session_app_key_slot_unknown")
        if type(require_delayed_key) is not bool:
            raise TypeError("require_delayed_key must be a boolean")
        if require_delayed_key and slot != "delayed":
            raise ConfigError("recorder_delayed_key_required")
        if slot == "live" and not recorder["allow_live_key"]:
            # Betfair does not permit read-only use of the live key (F46).
            raise ConfigError("recorder_live_key_not_allowed")
        if slot != stream_config["app_key_slot"]:
            raise ConfigError("recorder_session_slot_does_not_match_stream_app_key_slot")
        if not callable(stream_factory) or not callable(market_lister):
            raise TypeError("stream_factory and market_lister must be callables")
        self.invocation_id = check_invocation_id(invocation_id)
        self.session = session
        self.stream_factory = stream_factory
        self.market_lister = market_lister
        self.clock = clock
        self.rng = rng if rng is not None else random.Random()
        self.emit = emit if emit is not None else (lambda event: None)
        self.key_type = slot
        self.root = Path(recorder["root"]) if recorder["root"] is not None else Path(paths.recordings_dir)
        self.market_filter = copy.deepcopy(recorder["market_filter"])
        self.market_data_filter = copy.deepcopy(recorder["market_data_filter"])

    def run(self, stop_requested=lambda: False, *, max_seconds=None, end_at_ns=None, admission=None):
        """Record until a stop condition; returns the final receipt (also written as receipt.json).

        Stops on stop_requested(), recorder.max_duration_s, max_seconds, end_at_ns (a UTC instant in
        nanoseconds, compared with clock.time_ns(), never at or before now), recorder.max_bytes (nothing is
        ever deleted), SUBSCRIPTION_LIMIT_EXCEEDED, a fatal stream or app-key error, an unrecognized
        error, or an exhausted reconnect budget. Each stop closes the segment, writes the receipt and
        logs out. One recorder per profile at a time: admission is the RecorderTicket cmd_record took
        before reading credentials; without one, run takes its own (admission.recorder_admission), so
        INHIBIT, a live ADMISSION or another recorder refuses before anything connects.
        """
        if max_seconds is not None and (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
                                        or not max_seconds > 0):
            raise ValueError("max_seconds must be a positive number")
        if end_at_ns is not None and (type(end_at_ns) is not int or end_at_ns <= self.clock.time_ns()):
            raise ValueError("end_at must be a UTC instant after now")
        if not callable(stop_requested):
            raise TypeError("stop_requested must be a callable")
        root = require_outside_worktrees(self.root, what="recordings_root")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if admission is not None:
            if not admission.holds(self.paths) or admission.invocation_id != self.invocation_id:
                raise ConfigError("recorder_admission_not_held")
            return _Session(self, root, stop_requested, max_seconds, end_at_ns).run()
        with recorder_admission(self.paths, invocation_id=self.invocation_id, clock=self.clock):
            return _Session(self, root, stop_requested, max_seconds, end_at_ns).run()


class _Session:
    """The state of one recording run."""

    def __init__(self, recorder, root, stop_requested, max_seconds, end_at_ns):
        self.r = recorder
        self.clock = recorder.clock
        self.root = root
        self.stop_requested = stop_requested
        self.max_seconds = max_seconds
        self.end_at_ns = end_at_ns
        self.precount = None            # {"taken_at", "rows"} of the latest successful pre-count
        self.precount_rows = 0
        self.rec = recorder.config["recorder"]
        self.stream_config = recorder.config["stream"]
        self.start_ns = self.clock.monotonic_ns()
        self.created_at = self.clock.utc_now()
        suffix = format(recorder.rng.getrandbits(32), "08x")
        self.session_id = f"rec-{utc_compact_from_ns(self.clock.time_ns())}-{suffix}"
        self.dir = root / self.session_id
        self.events_path = self.dir / "events.jsonl"
        self.segments = []
        self.gaps = []
        self.pending_gap = None
        self.segment = None
        self.next_index = 0
        self.bytes_total = 0
        self.echo_heartbeat_ms = None
        self.echo_conflate_ms = None
        self.connection_echo = None
        self.client = None
        self.client_superseded = 0
        self.client_echo_rejected = 0
        self.next_refresh_ns = None
        self.last_progress_ns = self.start_ns
        self.keepalive_retry_ns = None
        self.static = None

    # --- top level ----------------------------------------------------------------------------

    def run(self):
        try:
            self.dir.mkdir(mode=0o700)
        except FileExistsError:
            raise ConfigError("recording_session_dir_exists") from None
        self.static = {"config_sha256": config_sha256(self.r.config), "code_sha256": code_hashes(REPO_ROOT),
                       **git_state(REPO_ROOT), "python": platform.python_version()}
        self._write_partial()
        try:
            reason = self._loop()
        except BaseException:
            self._abort()
            raise
        logout_attempted = False
        try:
            self._event("stopped", reason=reason)
            receipt = self._receipt(complete=True, closed_at=self.clock.utc_now(), stop_reason=reason)
            receipt_path = self.dir / "receipt.json"
            receipt_sha = atomic_write_json(receipt_path, receipt)
            # The ledger stays at the profile's recordings/run-ledger.jsonl even when recorder.root
            # puts the sessions on another disk; receipt_path is absolute either way.
            row = {"created_at": self.created_at, "session_id": self.session_id, "receipt_path": str(receipt_path),
                   "receipt_sha256": receipt_sha, "segments": len(self.segments), "status": RECEIPT_STATUS}
            if self.r.invocation_id is not None:
                row["invocation_id"] = self.r.invocation_id
            append_jsonl(Path(self.r.paths.recordings_dir) / "run-ledger.jsonl", row)
            logout_attempted = True
            logout = self._logout()
        finally:
            if not logout_attempted:
                # Finishing the receipt failed (a full disk, say): still end the Betfair session.
                self._logout(quiet=True)
        self.r.emit({"event": "stopped", "at": self.clock.utc_now(), "reason": reason, "session_id": self.session_id,
                     "segments": len(self.segments), "gaps": len(self.gaps), "receipt_path": str(receipt_path),
                     "logout": logout})
        return receipt

    def _loop(self):
        policy = ReconnectPolicy(self.stream_config["reconnect_backoff_s"], self.stream_config["max_reconnects_per_hour"],
                                 rng=self.r.rng, clock=self.clock)
        category = None
        while True:
            reason = self._stop_reason()
            if reason:
                return reason
            if category is not None:
                try:
                    delay = policy.next_delay(category=category)
                except StreamError:
                    return "reconnect_budget_exhausted"
                reason = self._sleep(delay)
                if reason:
                    return reason
            outcome, value = self._connection(policy)
            if outcome == "stop":
                return value
            category = value

    def _connection(self, policy):
        ids, failure = self._precount()
        if failure is not None:
            return "stop", failure
        client = self.r.stream_factory()
        mismatch = self._client_mismatch(client)
        if mismatch is not None:
            # Never connected: the receipt would misstate the key or endpoint, so stop instead.
            self._event("stream_factory_refused", field=mismatch)
            return "stop", f"stream_factory_mismatch:{mismatch}"
        self.client = client
        self.client_superseded = 0
        self.client_echo_rejected = 0
        self.connection_echo = None
        try:
            try:
                info = client.connect()
            except TransportError as exc:
                self._event("disconnected", reason=f"connect:{exc.code}")
                return self._transport_outcome(exc)
            except StreamError as exc:
                return self._stream_outcome(exc, client)
            connection_id = info.get("connection_id")
            self._event("connected", connection_id=connection_id)
            reason = self._end_at_reason()          # authenticate() may log in
            if reason:
                self._event("disconnected", reason=reason)
                return "stop", reason
            try:
                auth = client.authenticate()
            except StreamError as exc:
                return self._stream_outcome(exc, client)
            except TransportError as exc:          # a lazy login inside session.token()
                self._event("disconnected", reason=f"login:{exc.code}")
                return self._transport_outcome(exc)
            except BetfairError as exc:            # login refused, banned or over budget
                self._event("disconnected", reason=f"login:{_code(exc)}")
                return "stop", f"session_unavailable:{_code(exc)}"
            self._event("auth_status", status_code=auth.get("status_code"),
                        connections_available=auth.get("connections_available"))
            while True:
                outcome = self._subscribe(client, connection_id, ids)
                if outcome is not None:
                    return outcome
                outcome, value = self._stream_segment(client, policy)
                if outcome != "refresh":
                    return outcome, value
                ids = value
        finally:
            client.close()
            self.client = None

    def _client_mismatch(self, client):
        """None when stream_factory() gave a StreamClient on the gated session with the validated
        stream config (the values the receipt states); else the first field that differs."""
        if not isinstance(client, StreamClient):
            return "client_type"
        if not client.uses_session(self.r.session):
            return "session"           # the live-key gate checked self.r.session, so only it may stream
        for field in ("host", "port", "heartbeat_ms", "conflate_ms"):
            if getattr(client, field) != self.stream_config[field]:
                return field
        return None

    # --- market selection and subscription ----------------------------------------------------

    def _precount(self):
        """Market ids for the next subscription, or (None, stop reason) when the lister fails or end_at passed."""
        reason = self._end_at_reason()
        if reason:
            return None, reason
        try:
            taken_ns = self.clock.time_ns()
            start_from = utc_from_ns(taken_ns - PRECOUNT_LOOKBACK_S * _NS)
            rest_filter = to_rest_market_filter(self.r.market_filter, start_from_utc=start_from)
            rows = _clean_precount(self.r.market_lister(rest_filter))
        except (BetfairError, ValueError) as exc:
            return None, f"market_lister_failed:{_code(exc)}"
        self.precount = {"taken_at": utc_from_ns(taken_ns), "rows": rows}
        return [market_id for market_id, _ in rows], None

    def _plan(self, ids):
        """Coarse filter while the pre-count fits under max_markets; else the first max_markets ids."""
        max_markets = self.rec["max_markets"]
        if len(ids) <= max_markets and len(ids) < PRECOUNT_LIMIT:
            return {"mode": "coarse", "filter": to_stream_market_filter(self.r.market_filter),
                    "requested_count": len(ids), "ids_sha256": None}
        selected = ids[:max_markets]
        neutral = dict(self.r.market_filter, market_ids=selected)
        return {"mode": "explicit", "filter": to_stream_market_filter(neutral), "requested_count": len(selected),
                "ids_sha256": canonical_sha256(selected)}

    def _subscribe(self, client, connection_id, ids):
        plan = self._plan(ids)
        try:
            request_id = client.subscribe_markets(plan["filter"], self.r.market_data_filter)
        except StreamError as exc:
            return self._stream_outcome(exc, client)
        index = self.next_index
        self.next_index += 1
        self.segment = _SegmentWriter(self.dir, index, clock=self.clock, fsync_interval_s=self.rec["fsync_interval_s"],
                                      connection_id=connection_id, plan=plan)
        self.next_refresh_ns = self.clock.monotonic_ns() + self.rec["refresh_s"] * _NS
        self._write_precount(index, plan)
        sent = client.subscription_message("market") or {}       # what actually went on the wire
        self._event("subscribed", segment=index, request_id=request_id, subscription_mode=plan["mode"],
                    market_filter=sent.get("marketFilter"), market_data_filter=sent.get("marketDataFilter"),
                    segmentation_enabled=sent.get("segmentationEnabled"), heartbeat_ms=sent.get("heartbeatMs"),
                    conflate_ms=sent.get("conflateMs"), requested_count=plan["requested_count"])
        self.r.emit({"event": "segment_opened", "at": self.clock.utc_now(), "session_id": self.session_id,
                     "segment": index, "subscription_mode": plan["mode"], "requested_count": plan["requested_count"]})
        return None

    def _write_precount(self, index, plan):
        """One precount.jsonl row for the segment just opened (C4): the pre-count that planned it, as the
        catalogue's (marketId, marketStartTime) pairs only."""
        if self.precount is None:
            return
        rows = self.precount["rows"]
        append_jsonl(self.dir / PRECOUNT_NAME, {
            "segment": index, "taken_at": self.precount["taken_at"], "lookback_s": PRECOUNT_LOOKBACK_S,
            "count": len(rows), "possibly_more": len(rows) >= PRECOUNT_LIMIT, "subscription_mode": plan["mode"],
            "requested_count": plan["requested_count"],
            "markets": [{"market_id": market_id, "market_start_time": start} for market_id, start in rows]})
        self.precount_rows += 1

    # --- reading one segment ------------------------------------------------------------------

    def _stream_segment(self, client, policy):
        while True:
            reason = self._stop_reason()
            if reason:
                self._close_segment(reason)
                return "stop", reason
            now = self.clock.monotonic_ns()
            if now >= self.next_refresh_ns:
                ids, failure = self._precount()
                if failure is None:
                    self._close_segment("scheduled_refresh")
                    return "refresh", ids
                if failure == END_AT_REACHED:
                    self._close_segment(failure)
                    return "stop", failure
                # Keep the current subscription (it was within limits) and try again next interval.
                self._event("refresh_failed", reason=failure)
                self.next_refresh_ns = now + self.rec["refresh_s"] * _NS
            self._keep_alive()
            self._progress()
            try:
                item = client.next_message(max_wait_s=POLL_S)
            except StreamError as exc:
                self._absorb_counters(client)
                self._close_segment(exc.code)
                return self._stream_outcome(exc, client)
            self._absorb_counters(client)
            self._track_echo(client)
            if item is None:
                continue
            outcome = self._handle(item, client, policy)
            if outcome is not None:
                return outcome

    def _handle(self, item, client, policy):
        raw, msg = item["raw"], item["msg"]
        verdict = line_acceptance(raw, msg, self.segment.last_pt, client.active_subscription_id)
        if verdict is None:
            line, row = self.segment.prepare(raw, msg, item["received_at"], item["monotonic_ns"])
            if self.bytes_total + len(line) + len(row) > self.rec["max_bytes"]:
                self._close_segment("max_bytes_reached")
                return "stop", "max_bytes_reached"
            if self.pending_gap is not None:
                gap = {"after_segment": self.pending_gap["after_segment"],
                       "from_received_at": self.pending_gap["from_received_at"],
                       "to_received_at": item["received_at"], "reason": self.pending_gap["reason"]}
                self.pending_gap = None
                self.gaps.append(gap)
                self._event("gap", **gap)
                self.r.emit(dict(gap, event="gap", at=self.clock.utc_now()))
            self.segment.commit(line, row, msg, item["received_at"], item["monotonic_ns"])
            self.bytes_total += len(line) + len(row)
            if self.segment.lines == 1:
                policy.reset()
            return None
        if verdict == "not_mcm":
            self._non_mcm_event(item)
            return None
        if verdict == "superseded_id":
            self.segment.superseded_dropped += 1
            return None
        pt = msg.get("pt") if type(msg.get("pt")) is int else None
        self._event("rejected_line", rejected_line_sha256=bytes_sha256(raw), reason=verdict, pt=pt)
        self._close_segment(verdict)
        self._event("disconnected", reason=verdict)
        return "reconnect", "stream_reconnect"

    def _non_mcm_event(self, item):
        msg = item["msg"]
        if item["op"] == "status":
            self._event("status_message", **self._status_fields(msg))
        elif item["op"] == "connection":
            connection_id = msg.get("connectionId")
            self._event("connected", connection_id=safe_code(connection_id) if connection_id is not None else None)
        else:
            self._event("ignored_message", op=safe_code(item["op"]), sha256=bytes_sha256(item["raw"]))

    @staticmethod
    def _status_fields(status):
        """Whitelisted status fields (never errorMessage free text)."""
        request_id = status.get("id")
        status_code = status.get("statusCode", status.get("status_code"))
        error_code = status.get("errorCode", status.get("error_code"))
        closed = status.get("connectionClosed", status.get("connection_closed"))
        return {"id": request_id if type(request_id) is int else None,
                "status_code": safe_code(status_code) if status_code is not None else None,
                "error_code": safe_code(error_code) if error_code is not None else None,
                "connection_closed": closed if type(closed) is bool else None}

    def _absorb_counters(self, client):
        counters = getattr(client, "counters", None) or {}
        dropped = counters.get("superseded_dropped", 0)
        if self.segment is not None and dropped > self.client_superseded:
            self.segment.superseded_dropped += dropped - self.client_superseded
        self.client_superseded = dropped
        rejected = counters.get("echo_out_of_range", 0)
        if rejected > self.client_echo_rejected:
            # The server echoed heartbeat/conflation outside the documented bounds; esa ignored it.
            echo = getattr(client, "rejected_echo", None) or {}
            self._event("echo_out_of_range", heartbeat_ms=echo.get("heartbeat_ms"),
                        conflate_ms=echo.get("conflate_ms"))
        self.client_echo_rejected = rejected

    def _track_echo(self, client):
        echo = client.echoed()
        if echo.get("heartbeat_ms") is None or echo == self.connection_echo:
            return
        self.connection_echo = dict(echo)
        self._event("echo", heartbeat_ms=echo["heartbeat_ms"], conflate_ms=echo["conflate_ms"])
        self.echo_heartbeat_ms = max(self.echo_heartbeat_ms or 0, echo["heartbeat_ms"])
        if echo["conflate_ms"] is not None:
            self.echo_conflate_ms = max(self.echo_conflate_ms or 0, echo["conflate_ms"])

    # --- outcomes -----------------------------------------------------------------------------

    def _stream_outcome(self, exc, client):
        """("stop", reason) or ("reconnect", category) for a StreamError, per the stream_status table (§11)."""
        status = getattr(client, "last_status", None)
        if isinstance(status, dict) and status.get("status_code") == "FAILURE":
            self._event("status_message", **self._status_fields(status))
        code = exc.code
        surface = "stream_status" if code in STREAM_STATUS_ERROR_CODES else "local"
        category = classify(code, surface=surface)
        if surface == "local" and code in _LOCAL_RECONNECT_CODES:
            category = "stream_reconnect"
        self._event("disconnected", reason=code)
        if category == "stream_subscription_limit":
            return "stop", "subscription_limit_exceeded"       # never retry the same set
        if category == "session":
            reason = self._end_at_reason()          # never a relogin after the end instant
            if reason:
                return "stop", reason
            try:
                self.r.session.invalidate_and_relogin()
            except BetfairError as relogin_error:
                return "stop", f"relogin_failed:{_code(relogin_error)}"
            return "reconnect", "session"
        if category in ("stream_reconnect", "stream_clock", "stream_backoff"):
            return "reconnect", category
        return "stop", f"{category}:{code}"                    # app_key, stream_fatal, unrecognized

    @staticmethod
    def _transport_outcome(exc):
        if exc.code in _TRANSPORT_RETRY_CODES:
            return "reconnect", "stream_reconnect"
        return "stop", f"transport:{exc.code}"

    # --- segments, gaps and receipts ----------------------------------------------------------

    def _close_segment(self, reason):
        segment, self.segment = self.segment, None
        if segment is None:
            return
        info = segment.close(reason)
        check, missing = replay_check(self.dir / segment.name)
        info["markets_without_initial_definition"] = missing
        info["replay_check"] = check
        self.segments.append(info)
        self._event("segment_closed", segment=segment.index, sha256=info["sha256"], bytes=info["bytes"],
                    lines=info["lines"])
        if info["lines"]:
            # Coverage stops at this segment's last line until the next segment's first line.
            self.pending_gap = {"after_segment": segment.index, "from_received_at": info["last_received_at"],
                                "reason": reason}
        self._write_partial()
        self.r.emit({"event": "segment_closed", "at": self.clock.utc_now(), "segment": segment.index,
                     "lines": info["lines"], "bytes": info["bytes"], "close_reason": reason,
                     "replay_check": check["status"]})

    def _receipt(self, *, complete, closed_at, stop_reason):
        if self.r.key_type == "delayed":
            label = "delayed_conflated"
        elif self.echo_conflate_ms == 0:
            label = "live_unconflated"
        else:
            label = "live_conflated"         # conflated, or conflation never echoed: claim less, not more
        extra = {"invocation_id": self.r.invocation_id} if self.r.invocation_id is not None else {}
        precount_path = self.dir / PRECOUNT_NAME
        precount = {"path": PRECOUNT_NAME, "rows": self.precount_rows,
                    "sha256": file_sha256(precount_path) if self.precount_rows else None}
        return {
            **extra, "precount": precount,
            "schema_version": 1, "status": RECEIPT_STATUS, "complete": complete, "session_id": self.session_id,
            "created_at": self.created_at, "closed_at": closed_at, "stop_reason": stop_reason,
            "data_kind": DATA_KIND, "key_type": self.r.key_type,
            "endpoint": f"{self.stream_config['host']}:{self.stream_config['port']}",
            "data_freshness": {"conflate_ms_requested": self.stream_config["conflate_ms"],
                               "conflate_ms_echoed": self.echo_conflate_ms,
                               "heartbeat_ms_echoed": self.echo_heartbeat_ms, "label": label,
                               "note": FRESHNESS_NOTES[label]},
            "subscription": {"market_filter": copy.deepcopy(self.r.market_filter),
                             "market_data_filter": copy.deepcopy(self.r.market_data_filter),
                             "segmentation_enabled": False},
            "timestamp_provenance": dict(TIMESTAMP_PROVENANCE),
            "segments": copy.deepcopy(self.segments), "gaps": copy.deepcopy(self.gaps), "coverage": COVERAGE,
            "licence_note": LICENCE_NOTE, **copy.deepcopy(self.static), "limitations": list(LIMITATIONS),
            "economic_result": None,
        }

    def _write_partial(self):
        atomic_write_json(self.dir / "receipt.partial.json",
                          self._receipt(complete=False, closed_at=None, stop_reason=None))

    def _event(self, name, **fields):
        row = {"event": name, "at": self.clock.utc_now(), "monotonic_ns": self.clock.monotonic_ns()}
        row.update(fields)
        append_jsonl(self.events_path, row)

    # --- housekeeping -------------------------------------------------------------------------

    def _end_at_reason(self):
        """END_AT_REACHED once the wall clock reached end_at_ns (time_ns keeps running while the Mac sleeps,
        the monotonic clock does not)."""
        if self.end_at_ns is not None and self.clock.time_ns() >= self.end_at_ns:
            return END_AT_REACHED
        return None

    def _stop_reason(self):
        if self.stop_requested():
            return "stop_requested"
        reason = self._end_at_reason()
        if reason:
            return reason
        elapsed = self.clock.monotonic_ns() - self.start_ns
        if elapsed >= self.rec["max_duration_s"] * _NS:
            return "max_duration_reached"
        if self.max_seconds is not None and elapsed >= self.max_seconds * _NS:
            return "max_seconds_reached"
        if self.bytes_total >= self.rec["max_bytes"]:
            return "max_bytes_reached"
        return None

    def _sleep(self, seconds):
        """Wait in slices of at most POLL_S, returning a stop reason as soon as one applies. Progress
        events and the session keepAlive carry on while waiting (a stream_backoff wait can be an hour)."""
        end = self.clock.monotonic_ns() + int(seconds * _NS)
        while True:
            reason = self._stop_reason()
            if reason:
                return reason
            remaining = end - self.clock.monotonic_ns()
            if remaining <= 0:
                return None
            self._keep_alive()
            self._progress()
            self.clock.sleep(min(remaining / _NS, POLL_S))

    def _keep_alive(self):
        """session.maybe_keep_alive(), at most once per KEEPALIVE_RETRY_S after a failure.

        A failure is a raised BetfairError or a returned {"status","error"} whose status is not
        SUCCESS (SessionManager.keep_alive reports FAIL that way, I5, and then stays due on every
        call, so without this throttle each read-loop pass would send another SSO request).
        """
        now = self.clock.monotonic_ns()
        if self.keepalive_retry_ns is not None and now < self.keepalive_retry_ns:
            return
        if self._end_at_reason():
            return                              # a keepAlive can relogin (NO_SESSION); the loop stops next
        try:
            result = self.r.session.maybe_keep_alive()
        except BetfairError as exc:
            failure = _code(exc)
        else:
            failure = None
            if result is not None and not (isinstance(result, dict) and result.get("status") == "SUCCESS"):
                fields = result if isinstance(result, dict) else {}
                code = fields.get("error") if fields.get("error") is not None else fields.get("status")
                failure = safe_code(code) if code is not None else "unrecognized_keepalive_result"
        if failure is None:
            self.keepalive_retry_ns = None
            return
        # The stream stays authenticated; a later reconnect re-logs in if the session lapsed.
        self._event("keepalive_failed", code=failure)
        self.keepalive_retry_ns = now + KEEPALIVE_RETRY_S * _NS

    def _progress(self):
        now = self.clock.monotonic_ns()
        if now - self.last_progress_ns < PROGRESS_INTERVAL_S * _NS:
            return
        self.last_progress_ns = now
        self.r.emit({"event": "heartbeat_at", "at": self.clock.utc_now(), "session_id": self.session_id,
                     "segment": self.segment.index if self.segment is not None else None,
                     "segment_lines": self.segment.lines if self.segment is not None else 0,
                     "bytes_total": self.bytes_total})

    def _logout(self, *, quiet=False):
        try:
            result = self.r.session.logout()
        except Exception as exc:          # best effort: the receipt is already written
            if not quiet and not isinstance(exc, BetfairError):
                raise
            return {"status": "failed", "error": _code(exc)}
        status = result.get("status") if isinstance(result, dict) else None
        return {"status": safe_code(status) if status is not None else None, "error": None}

    def _abort(self):
        """After an unexpected exception: keep files flushed, close the connection, log out (best effort).
        receipt.partial.json (complete: false) stays as the last record; no receipt.json is written."""
        if self.segment is not None:
            self.segment.abort()
            self.segment = None
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self._logout(quiet=True)


# --- verification ---------------------------------------------------------------------------------

def verify_recording(session_dir):
    """Re-check a closed session against its receipt: {"status": "verified"|"failed", "checks": [...]}.

    Checks: receipt.json parses strictly and is complete; each segment's sha256, bytes and line count;
    the receipt sidecar's sha256, contiguous rows 1..lines, and each row's offset, length, sha256 and
    pt; stream.messages() consumes every segment; first_pt and last_pt match the data.
    """
    directory = Path(session_dir)
    checks = []

    def record(name, ok, *, segment=None, detail=None):
        entry = {"check": name, "status": "passed" if ok else "failed"}
        if segment is not None:
            entry["segment"] = segment
        if detail is not None:
            entry["detail"] = detail
        checks.append(entry)
        return ok

    receipt = _load_receipt(directory, record)
    if receipt is not None:
        for segment in receipt["segments"]:
            _verify_segment(directory, segment, record)
    status = "verified" if checks and all(c["status"] == "passed" for c in checks) else "failed"
    return {"status": status, "checks": checks}


def _load_receipt(directory, record):
    path = directory / "receipt.json"
    if path.is_symlink() or not path.is_file():
        partial = (directory / "receipt.partial.json").is_file()
        record("receipt_present", False,
               detail="only receipt.partial.json: the session never closed" if partial else "receipt.json missing")
        return None
    try:
        receipt = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        record("receipt_parses", False)
        return None
    record("receipt_parses", True)
    shaped = (isinstance(receipt, dict) and receipt.get("schema_version") == 1
              and receipt.get("status") == RECEIPT_STATUS and isinstance(receipt.get("segments"), list)
              and all(isinstance(s, dict) for s in receipt["segments"]))
    if not record("receipt_shape", shaped):
        return None
    record("receipt_complete", receipt.get("complete") is True)
    return receipt


def _verify_segment(directory, segment, record):
    index = segment.get("index")
    name, recv_name = segment.get("path"), segment.get("recv_path")
    if not (isinstance(name, str) and _SEGMENT_RE.fullmatch(name)
            and recv_name == name[:-len(".jsonl")] + ".recv.jsonl"):
        record("segment_paths", False, segment=index)
        return
    segment_path, recv_path = directory / name, directory / recv_name
    if any(p.is_symlink() or not p.is_file() for p in (segment_path, recv_path)):
        record("segment_files_present", False, segment=index)
        return
    record("segment_files_present", True, segment=index)
    data_hash, recv_hash = hashlib.sha256(), hashlib.sha256()
    offset = lines = 0
    contiguous = rows_ok = True
    row_pts = []
    with segment_path.open("rb") as data, recv_path.open("rb") as rows:
        for raw_line in data:
            lines += 1
            data_hash.update(raw_line)
            raw = raw_line[:-1] if raw_line.endswith(b"\n") else raw_line
            if not raw_line.endswith(b"\n"):
                rows_ok = False
            row_text = rows.readline()
            recv_hash.update(row_text)
            try:
                row = strict_json_loads(row_text.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                row = None
            if not isinstance(row, dict) or set(row) != RECV_ROW_KEYS:
                rows_ok = contiguous = False
            else:
                if row["line"] != lines:
                    contiguous = False
                if row["byte_offset"] != offset or row["bytes"] != len(raw) or row["sha256"] != bytes_sha256(raw):
                    rows_ok = False
                row_pts.append(row["pt"])
            offset += len(raw_line)
        rest = rows.read()
        recv_hash.update(rest)
        if rest:
            contiguous = False
    record("segment_sha256", data_hash.hexdigest() == segment.get("sha256") and offset == segment.get("bytes")
           and lines == segment.get("lines"), segment=index)
    record("recv_sha256", recv_hash.hexdigest() == segment.get("recv_sha256"), segment=index)
    record("recv_rows_contiguous", contiguous, segment=index)
    record("recv_row_hashes", rows_ok, segment=index)
    try:
        pts = [msg["pt"] for msg in _stream.messages(segment_path)]
        consumed = len(pts) == lines
    except (ValueError, OSError, UnicodeDecodeError):
        pts, consumed = [], False
    record("messages_consumes_segment", consumed, segment=index)
    record("recv_pt_matches", consumed and row_pts == pts, segment=index)
    first, last = (pts[0], pts[-1]) if pts else (None, None)
    record("pt_bounds", consumed and first == segment.get("first_pt") and last == segment.get("last_pt"),
           segment=index)
