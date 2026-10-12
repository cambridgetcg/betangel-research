"""Exchange Stream API client: CRLF JSON over a verified TLS socket, handshake, subscriptions, clocks, liveness.

Read-only. The only messages this module sends are authentication, marketSubscription and
orderSubscription; every subscription carries "segmentationEnabled": false. The authentication
line holds the app key and session token: it is written straight to the socket and is never
returned, stored, logged or put into exception text. Change messages for any id other than the
active subscription's are dropped and counted. A status FAILURE raises StreamError with the
server's code; Betfair closes the connection for every code except SUBSCRIPTION_LIMIT_EXCEEDED.

Liveness follows the echoed heartbeat and conflation, not the requested values. The protocol facts
come from Betfair's stream documentation and Swagger file (F48-F53, accessed 2026-10-10); none has
been checked against a live stream (test T6). Reconnect timing lives in ReconnectPolicy; what to do
after each error is the caller's decision (recorder.py).
"""

from collections import deque
import copy
import json
import socket

from .constants import (STREAM_CONFLATE_MS_RANGE, STREAM_FIRST_MESSAGE_TIMEOUT_S, STREAM_HEARTBEAT_MS_RANGE,
                        STREAM_HOSTS, STREAM_MARKET_FILTER_KEYS, STREAM_PORT)
from .errors import ConfigError, StreamError
from .filters import validate_market_data_filter
from .receipts import SYSTEM_CLOCK
from .secret import Secret
from .transport import open_stream_socket

STATES = ("DISCONNECTED", "CONNECTING", "AWAIT_CONNECTION", "AUTHENTICATING", "AUTHENTICATED", "SUBSCRIBING",
          "STREAMING", "CLOSED")
READ_CHUNK = 65536
MAX_REQUEST_ID = 2 ** 31 - 1
ORDER_FILTER_KEYS = frozenset({"includeOverallPosition", "customerStrategyRefs", "partitionMatchedByStrategyRef"})
# Documented echo bounds (F50: heartbeatMs is echoed within 500-30000; F51: conflateMs at most 120000
# on request, and a delayed key is conflated to 180000). An echo outside them is treated as absent and
# counted (counters["echo_out_of_range"]), so a bad echo can never stretch the liveness timeout.
_ECHO_HEARTBEAT_MS = (500, 30000)
_ECHO_CONFLATE_MS = (0, 180000)
# Local StreamError codes raised by this module. They mean "this connection is unusable"; the
# error table in errors.py lists only liveness_timeout and line_too_long, so callers that want
# to reconnect on the others must say so themselves (recorder.py does).
LOCAL_CONNECTION_CODES = frozenset({"liveness_timeout", "line_too_long", "connection_lost", "receive_failed",
                                    "send_failed", "invalid_json", "invalid_message", "connection_message_timeout",
                                    "auth_response_timeout", "unexpected_first_message", "unknown_status_code",
                                    "connection_closed_by_server", "status_failure"})


def _check_int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{name}_out_of_range")
    return value


def _validate_stream_market_filter(f):
    """Stream MarketFilter as filters.to_stream_market_filter builds it; an empty filter (every market) is refused."""
    if not isinstance(f, dict) or not f:
        raise ConfigError("stream_market_filter_must_be_non_empty_object")
    if set(f) - set(STREAM_MARKET_FILTER_KEYS):
        raise ConfigError("stream_market_filter_unknown_key")
    for key, values in f.items():
        if (not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values)
                or len(set(values)) != len(values)):
            raise ConfigError(f"stream_market_filter_{key}_must_be_unique_strings")
    return copy.deepcopy(f)


def _validate_order_filter(f):
    if f is None:
        return None
    if not isinstance(f, dict) or set(f) - ORDER_FILTER_KEYS:
        # accountIds is for Betfair's internal use only (F57 notes), so it is refused here.
        raise ConfigError("order_filter_unknown_key")
    for key in ("includeOverallPosition", "partitionMatchedByStrategyRef"):
        if key in f and type(f[key]) is not bool:
            raise ConfigError(f"order_filter_{key}_must_be_boolean")
    refs = f.get("customerStrategyRefs")
    if refs is not None and (not isinstance(refs, list) or any(not isinstance(r, str) or not r for r in refs)):
        raise ConfigError("order_filter_customerStrategyRefs_must_be_strings")
    return copy.deepcopy(f)


def _clock_token(value, name):
    if value is not None and (not isinstance(value, str) or not value):
        raise ConfigError(f"{name}_must_be_string")
    return value


def _encode(message):
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\r\n"


class StreamClient:
    """One connection's worth of Exchange Stream API state. Single-threaded; not reentrant."""

    def __init__(self, *, host, port, session, socket_factory=open_stream_socket, clock=SYSTEM_CLOCK,
                 heartbeat_ms=5000, conflate_ms=None, connect_timeout_s=15, max_line_bytes=16 * 1024 * 1024):
        if host not in STREAM_HOSTS:
            raise ConfigError("stream_host_not_allowed")
        if type(port) is not int or port != STREAM_PORT:
            raise ConfigError("stream_port_must_be_443")
        self.heartbeat_ms = _check_int(heartbeat_ms, "heartbeat_ms", *STREAM_HEARTBEAT_MS_RANGE)
        if conflate_ms is not None:
            _check_int(conflate_ms, "conflate_ms", *STREAM_CONFLATE_MS_RANGE)
        self.conflate_ms = conflate_ms
        if (isinstance(connect_timeout_s, bool) or not isinstance(connect_timeout_s, (int, float))
                or not 0 < connect_timeout_s <= 60):
            raise ConfigError("connect_timeout_s_out_of_range")
        self.connect_timeout_s = connect_timeout_s
        self.max_line_bytes = _check_int(max_line_bytes, "max_line_bytes", 1024, 64 * 1024 * 1024)
        self.host = host
        self.port = port
        self._session = session
        self._socket_factory = socket_factory
        self._clock = clock
        self.state = "DISCONNECTED"
        self._sock = None
        self._buffer = bytearray()
        self._request_id = 0
        self._last_rx_ns = None
        self.connection_id = None
        self.connections_available = None
        self.last_status = None
        self._echo = None
        self._active = {"market": None, "order": None}
        self._subscriptions = {"market": None, "order": None}
        self._clocks = {"market": {"initial_clk": None, "clk": None}, "order": {"initial_clk": None, "clk": None}}
        self.counters = {"superseded_dropped": 0, "status_503": 0, "change_messages": 0, "blank_lines": 0,
                         "echo_out_of_range": 0}
        self.rejected_echo = None

    # --- connection lifecycle ---------------------------------------------------------------

    def connect(self):
        """Open the socket and wait (at most 15 s) for op=connection. Returns {"connection_id"}."""
        if self.state not in ("DISCONNECTED", "CLOSED"):
            raise StreamError("invalid_state", connection_closed=False)
        self._buffer = bytearray()
        self.connection_id = None
        self.connections_available = None
        self.last_status = None
        self._echo = None
        self._active = {"market": None, "order": None}
        self.state = "CONNECTING"
        try:
            self._sock = self._socket_factory(self.host, self.port, self.connect_timeout_s)
        except BaseException:
            self.state = "CLOSED"
            raise
        self.state = "AWAIT_CONNECTION"
        self._last_rx_ns = self._clock.monotonic_ns()
        wait_ns = int(min(self.connect_timeout_s, STREAM_FIRST_MESSAGE_TIMEOUT_S) * 1_000_000_000)
        deadline = self._last_rx_ns + wait_ns
        line = self._read_line("connection_message_timeout", lambda: deadline)
        msg = self._parse(line)
        if msg["op"] == "status":
            self._handle_status(msg)
        if msg["op"] != "connection":
            self._fail("unexpected_first_message")
        connection_id = msg.get("connectionId")
        self.connection_id = connection_id if isinstance(connection_id, str) and connection_id else None
        self.state = "AUTHENTICATING"
        return {"connection_id": self.connection_id}

    def authenticate(self):
        """Send the authentication line and wait for its status. Returns {"status_code","connections_available"}."""
        if self.state != "AUTHENTICATING":
            raise StreamError("invalid_state", connection_closed=False)
        app_key = self._session.app_key()
        token = self._session.token()
        if not isinstance(app_key, Secret) or not isinstance(token, Secret):
            self.close()
            raise TypeError("the session must supply the app key and token as Secret values")
        request_id = self._next_id()
        self._send_authentication(request_id, app_key, token)
        deadline = self._clock.monotonic_ns() + STREAM_FIRST_MESSAGE_TIMEOUT_S * 1_000_000_000
        while True:
            msg = self._parse(self._read_line("auth_response_timeout", lambda: deadline))
            if msg["op"] != "status":
                continue
            self._handle_status(msg)
            if msg.get("id") == request_id and type(msg.get("id")) is int:
                available = msg.get("connectionsAvailable")
                self.connections_available = available if type(available) is int else None
                self.state = "AUTHENTICATED"
                return {"status_code": "SUCCESS", "connections_available": self.connections_available}

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._active = {"market": None, "order": None}
        self.state = "CLOSED"

    # --- subscriptions ------------------------------------------------------------------------

    def subscribe_markets(self, stream_market_filter, market_data_filter, *, initial_clk=None, clk=None):
        """Send a marketSubscription (replacing any previous one); returns its request id.

        The new id becomes active before the message is sent, so lines of the old subscription are
        dropped from here on. The status reply arrives later through next_message().
        """
        market_filter = _validate_stream_market_filter(stream_market_filter)
        data_filter = validate_market_data_filter(market_data_filter, motif_compatible=False)
        message = {"op": "marketSubscription", "segmentationEnabled": False, "heartbeatMs": self.heartbeat_ms,
                   "marketFilter": market_filter, "marketDataFilter": data_filter}
        return self._subscribe("market", message, initial_clk, clk)

    def subscribe_orders(self, order_filter=None, *, initial_clk=None, clk=None):
        """Send an orderSubscription; returns its request id. Unused by the v1 gateway."""
        message = {"op": "orderSubscription", "segmentationEnabled": False, "heartbeatMs": self.heartbeat_ms}
        order_filter = _validate_order_filter(order_filter)
        if order_filter is not None:
            message["orderFilter"] = order_filter
        return self._subscribe("order", message, initial_clk, clk)

    def _subscribe(self, stream, message, initial_clk, clk):
        if self.state not in ("AUTHENTICATED", "SUBSCRIBING", "STREAMING"):
            raise StreamError("invalid_state", connection_closed=False)
        _clock_token(initial_clk, "initial_clk")
        _clock_token(clk, "clk")
        request_id = self._next_id()
        message = dict(message, id=request_id)
        if self.conflate_ms is not None:
            message["conflateMs"] = self.conflate_ms
        if initial_clk is not None:
            message["initialClk"] = initial_clk
        if clk is not None:
            message["clk"] = clk
        self._active[stream] = request_id
        self._subscriptions[stream] = copy.deepcopy(message)
        # Silence is measured from the request: the caller may have spent time on REST calls first.
        self._last_rx_ns = self._clock.monotonic_ns()
        self._sendall(_encode(message))
        if stream == "market":
            self.state = "SUBSCRIBING"
        return request_id

    def subscription_message(self, stream="market"):
        """The exact last subscription message sent for this stream (a copy), or None."""
        return copy.deepcopy(self._subscriptions[self._stream_name(stream)])

    def uses_session(self, session):
        """True when this client authenticates with exactly that session object (callers that gate the
        app key on one session check that a client handed to them uses it)."""
        return self._session is session

    @property
    def active_subscription_id(self):
        return self._active["market"]

    # --- reading ------------------------------------------------------------------------------

    def next_message(self, *, max_wait_s=None):
        """The next message, as {"raw","msg","op","id","received_at","monotonic_ns"}.

        raw is the line with its terminator stripped. Change messages for a superseded id are
        dropped (counted in counters["superseded_dropped"]). A status FAILURE raises StreamError.
        With max_wait_s, returns None when no line arrived in that time (liveness still applies).
        """
        if self._sock is None:
            raise StreamError("not_connected")
        wait_deadline = None
        if max_wait_s is not None:
            if isinstance(max_wait_s, bool) or not isinstance(max_wait_s, (int, float)) or not max_wait_s > 0:
                raise ValueError("max_wait_s must be a positive number")
            wait_deadline = self._clock.monotonic_ns() + int(max_wait_s * 1_000_000_000)
        while True:
            line = self._read_line("liveness_timeout", self._liveness_deadline, wait_deadline)
            if line is None:
                return None
            if not line:
                self.counters["blank_lines"] += 1
                continue
            received_at = self._clock.utc_now()
            monotonic_ns = self._clock.monotonic_ns()
            msg = self._parse(line)
            op = msg["op"]
            request_id = msg.get("id") if type(msg.get("id")) is int else None
            if op == "status":
                self._handle_status(msg)
            elif op in ("mcm", "ocm"):
                stream = "market" if op == "mcm" else "order"
                active = self._active[stream]
                if active is None or request_id != active:
                    self.counters["superseded_dropped"] += 1
                    continue
                self._on_change(stream, msg)
            elif op == "connection":
                connection_id = msg.get("connectionId")
                if isinstance(connection_id, str) and connection_id:
                    self.connection_id = connection_id
            return {"raw": line, "msg": msg, "op": op, "id": request_id, "received_at": received_at,
                    "monotonic_ns": monotonic_ns}

    def clocks(self, stream="market"):
        return dict(self._clocks[self._stream_name(stream)])

    def drop_clocks(self, stream=None):
        """Forget initialClk/clk (after INVALID_CLOCK): the next subscription takes a fresh image."""
        for name in (("market", "order") if stream is None else (self._stream_name(stream),)):
            self._clocks[name] = {"initial_clk": None, "clk": None}

    def echoed(self):
        """{"heartbeat_ms","conflate_ms"} from this connection's first change message; None values until then."""
        if self._echo is None:
            return {"heartbeat_ms": None, "conflate_ms": None}
        return dict(self._echo)

    def liveness_timeout_s(self):
        """2 x requested heartbeat before the first change message; then 2 x echoed heartbeat, plus the
        echoed conflation when it is non-zero."""
        if self._echo is None:
            return 2 * self.heartbeat_ms / 1000
        heartbeat, conflate = self._echo["heartbeat_ms"], self._echo["conflate_ms"]
        if conflate in (None, 0):
            return 2 * heartbeat / 1000
        return (conflate + 2 * heartbeat) / 1000

    # --- internals ----------------------------------------------------------------------------

    @staticmethod
    def _stream_name(stream):
        if stream not in ("market", "order"):
            raise ValueError("stream must be 'market' or 'order'")
        return stream

    def _next_id(self):
        if self._request_id >= MAX_REQUEST_ID:
            raise StreamError("request_id_exhausted", connection_closed=False)
        self._request_id += 1
        return self._request_id

    def _liveness_deadline(self):
        return self._last_rx_ns + int(self.liveness_timeout_s() * 1_000_000_000)

    def _fail(self, code):
        self.close()
        raise StreamError(code, connection_closed=True)

    def _send_authentication(self, request_id, app_key, token):
        # Built and sent in one place; the bytes are dropped before any error is raised.
        data = _encode({"op": "authentication", "id": request_id, "appKey": app_key.reveal(),
                        "session": token.reveal()})
        failed = not self._sendall(data, raise_on_error=False)
        data = None
        if failed:
            self._fail("send_failed")

    def _sendall(self, data, *, raise_on_error=True):
        if self._sock is None:
            raise StreamError("not_connected")
        ok = True
        try:
            self._sock.settimeout(self.connect_timeout_s)
            self._sock.sendall(data)
        except OSError:
            ok = False
        if not ok and raise_on_error:
            self._fail("send_failed")
        return ok

    def _read_line(self, timeout_code, deadline_fn, wait_deadline_ns=None):
        """One line without its terminator, or None when wait_deadline_ns passes first.

        A deadline that has passed fails only after one last short read finds nothing, so data that
        arrived while the caller was busy is never mistaken for silence.
        """
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                if len(line) > self.max_line_bytes:
                    self._fail("line_too_long")
                return line
            # No newline yet: allow one byte past the limit when it is the \r of a CRLF whose \n is
            # still in flight, so a line of exactly max_line_bytes is accepted however it is split.
            pending = len(self._buffer)
            if pending > self.max_line_bytes + 1 or (pending == self.max_line_bytes + 1
                                                     and self._buffer[-1] != 0x0D):
                self._fail("line_too_long")
            if self._sock is None:
                raise StreamError("not_connected")
            now = self._clock.monotonic_ns()
            deadline = deadline_fn()
            expired = now >= deadline
            if not expired and wait_deadline_ns is not None and now >= wait_deadline_ns:
                return None
            if expired:
                timeout_s = 0.001
            else:
                limit = deadline if wait_deadline_ns is None else min(deadline, wait_deadline_ns)
                timeout_s = max((limit - now) / 1_000_000_000, 0.001)
            failure = None
            chunk = b""
            try:
                self._sock.settimeout(timeout_s)
                chunk = self._sock.recv(READ_CHUNK)
            except (socket.timeout, TimeoutError):
                if expired:
                    failure = timeout_code
                else:
                    continue
            except OSError:
                failure = "receive_failed"
            if failure is not None:
                self._fail(failure)
            if not chunk:
                self._fail("connection_lost")
            self._last_rx_ns = self._clock.monotonic_ns()
            self._buffer += chunk

    def _parse(self, line):
        msg = None
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            msg = None
        # Raised outside the except block: a JSONDecodeError keeps the whole line in .doc.
        if msg is None:
            self._fail("invalid_json")
        if not isinstance(msg, dict) or not isinstance(msg.get("op"), str):
            self._fail("invalid_message")
        return msg

    def _handle_status(self, msg):
        status_code = msg.get("statusCode")
        error_code = msg.get("errorCode")
        closed = msg.get("connectionClosed", True if status_code != "SUCCESS" else False)
        if type(closed) is not bool:
            closed = True
        self.last_status = {"id": msg.get("id") if type(msg.get("id")) is int else None,
                            "status_code": status_code if status_code in ("SUCCESS", "FAILURE") else None,
                            "error_code": error_code if isinstance(error_code, str) else None,
                            "connection_closed": closed}
        if status_code == "FAILURE":
            code = error_code if isinstance(error_code, str) and error_code else "status_failure"
            if code == "INVALID_CLOCK":
                self.drop_clocks()
            if closed:
                self.close()
            raise StreamError(code, connection_closed=closed)
        if status_code != "SUCCESS":
            self._fail("unknown_status_code")
        if closed:
            self._fail("connection_closed_by_server")

    def _on_change(self, stream, msg):
        self.counters["change_messages"] += 1
        for key, name in (("initialClk", "initial_clk"), ("clk", "clk")):
            value = msg.get(key)
            if isinstance(value, str) and value:
                self._clocks[stream][name] = value
        if self._echo is None and msg.get("heartbeatMs") is not None:
            heartbeat, conflate = msg.get("heartbeatMs"), msg.get("conflateMs")
            heartbeat_ok = type(heartbeat) is int and _ECHO_HEARTBEAT_MS[0] <= heartbeat <= _ECHO_HEARTBEAT_MS[1]
            conflate_ok = conflate is None or (type(conflate) is int
                                               and _ECHO_CONFLATE_MS[0] <= conflate <= _ECHO_CONFLATE_MS[1])
            if not (heartbeat_ok and conflate_ok):
                # Out of range: not adopted. A bad heartbeat keeps the requested-heartbeat timeout; a
                # bad conflation counts as none echoed (the shorter timeout).
                self.counters["echo_out_of_range"] += 1
                self.rejected_echo = {"heartbeat_ms": heartbeat if type(heartbeat) is int else None,
                                      "conflate_ms": conflate if type(conflate) is int else None}
            if heartbeat_ok:
                self._echo = {"heartbeat_ms": heartbeat, "conflate_ms": conflate if conflate_ok else None}
        if msg.get("status") == 503:
            # Betfair: data may be stale because of latency; clients must not disconnect (F50).
            self.counters["status_503"] += 1
        if stream == "market" and self.state == "SUBSCRIBING":
            self.state = "STREAMING"


class ReconnectPolicy:
    """Delays between connections: reconnect_backoff_s with +/-20% jitter from an injected rng, and at
    most max_per_hour attempts in any rolling hour (StreamError("reconnect_budget_exhausted") after).

    stream_backoff codes (MAX_CONNECTION_LIMIT_EXCEEDED, TOO_MANY_REQUESTS) wait at least 60 s,
    doubling while they repeat, up to an hour. That policy is our own design: Betfair lists the
    codes but documents no recovery schedule.
    """

    def __init__(self, backoff_s, max_per_hour, *, rng, clock=SYSTEM_CLOCK):
        if (not isinstance(backoff_s, (list, tuple)) or not backoff_s
                or any(isinstance(b, bool) or not isinstance(b, (int, float)) or not b > 0 for b in backoff_s)):
            raise ConfigError("reconnect_backoff_s_must_be_positive_numbers")
        if type(max_per_hour) is not int or max_per_hour < 1:
            raise ConfigError("max_reconnects_per_hour_must_be_positive_int")
        self.backoff_s = list(backoff_s)
        self.max_per_hour = max_per_hour
        self._rng = rng
        self._clock = clock
        self._attempts = deque()
        self._step = 0
        self._backoff_streak = 0

    def attempts_in_last_hour(self):
        self._prune()
        return len(self._attempts)

    def next_delay(self, *, category="stream_reconnect"):
        """Seconds to wait before the next connection; records the attempt against the hourly cap."""
        self._prune()
        if len(self._attempts) >= self.max_per_hour:
            raise StreamError("reconnect_budget_exhausted")
        self._attempts.append(self._clock.monotonic_ns())
        base = self.backoff_s[min(self._step, len(self.backoff_s) - 1)]
        self._step += 1
        delay = base * self._rng.uniform(0.8, 1.2)
        if category == "stream_backoff":
            floor = min(60 * 2 ** self._backoff_streak, 3600)
            self._backoff_streak += 1
            delay = max(delay, floor)
        else:
            self._backoff_streak = 0
        return delay

    def reset(self):
        """Back to the first step after a healthy connection; the hourly cap is not reset."""
        self._step = 0
        self._backoff_streak = 0

    def _prune(self):
        horizon = self._clock.monotonic_ns() - 3600 * 1_000_000_000
        while self._attempts and self._attempts[0] <= horizon:
            self._attempts.popleft()
