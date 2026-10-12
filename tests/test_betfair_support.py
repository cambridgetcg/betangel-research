"""Shared offline fakes for the Betfair tests (no TestCases here). Every value is synthetic.

Nothing in this module touches the network, the real Keychain or the real state root:
block_network patches every socket entry point and makes subprocess.run refuse /usr/bin/security
(both raise OfflineViolation, a BaseException that no `except Exception` can swallow),
FakeKeychainRunner replaces /usr/bin/security, and temp_state_paths builds a StatePaths under a
temporary directory with the BETANGEL_BETFAIR_FORBID_NETWORK=1 harness switch set in the process
environment. scripted_network_only lifts that switch for tests that drive the real HttpTransport
over ScriptedSocket; the socket patches stay on.
"""

import copy
import gzip
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import urllib.request
from unittest.mock import patch

from betangel_research.betfair import transport as transport_module
from betangel_research.betfair.constants import DEFAULT_API_HOST, SSO_CERT_HOSTS, SSO_HOSTS
from betangel_research.betfair.credentials import ANCHOR_SERVICE, SECURITY_BIN, SERVICES
from betangel_research.betfair.errors import TransportError
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, StatePaths
from betangel_research.betfair.receipts import parse_utc_ns, utc_from_ns
from betangel_research.temporal import timestamp_ms

PLANTED_SECRET = "secret-session-must-not-escape"
MARKET_ID = "1.1"
BASE_PT = timestamp_ms("2020-01-01T12:00:00Z")
BASE_UTC = "2020-01-01T12:00:00.123457Z"
FORBID_ENV = {FORBID_NETWORK_ENV: "1"}
REPO_ROOT = Path(__file__).resolve().parent.parent
# The source tree is a git checkout (a .git dir, or a .git file in a linked worktree). An export without
# .git is not inside any worktree, so checks against the real checkout run only when it is one.
IN_GIT_CHECKOUT = (REPO_ROOT / ".git").exists()
EXAMPLE_CONFIG = REPO_ROOT / "research" / "betfair-config.example.json"
# The com jurisdiction's HttpTransport allowlist, as config.allowed_http_hosts gives it for the example config.
COM_HTTP_HOSTS = frozenset({SSO_HOSTS["com"], SSO_CERT_HOSTS["com"], DEFAULT_API_HOST})
_REAL_SUBPROCESS_RUN = subprocess.run


class OfflineViolation(BaseException):
    """Raised by the guards below. A BaseException, so production `except Exception` handlers
    (which turn ordinary failures into codes such as connect_failed) cannot hide it."""


def _network_blocked(*args, **kwargs):
    raise OfflineViolation("network access attempted in an offline Betfair test")


def _is_security_command(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args)
        first = text.split()[0] if text.split() else ""
    else:
        args = list(args)
        first = os.fsdecode(args[0]) if args else ""
    return first == SECURITY_BIN or os.path.basename(first) == "security"


def _guarded_run(args, *more, **kwargs):
    if _is_security_command(args):
        raise OfflineViolation("the real /usr/bin/security must never run in a Betfair test")
    return _REAL_SUBPROCESS_RUN(args, *more, **kwargs)


def block_network(test):
    """Patch every socket entry point, refuse /usr/bin/security through subprocess.run, and set the
    forbid-network switch in the process environment; all undone at test cleanup."""
    patchers = [
        patch("socket.create_connection", side_effect=_network_blocked),
        patch.object(socket.socket, "connect", _network_blocked),
        patch.object(http.client.HTTPConnection, "connect", _network_blocked),
        patch.object(http.client.HTTPSConnection, "connect", _network_blocked),
        patch.object(urllib.request, "urlopen", side_effect=_network_blocked),
        patch.object(subprocess, "run", _guarded_run),
        patch.dict(os.environ, FORBID_ENV),
    ]
    for patcher in patchers:
        patcher.start()
        test.addCleanup(patcher.stop)


def scripted_network_only(test):
    """block_network, then lift the forbid-network switch from the process environment.

    For tests that drive the real HttpTransport over ScriptedSocket/ConnectionScript: the switch
    would refuse every request in preflight. The socket patches stay on, so nothing can dial, and
    StatePaths overrides stop working (temp_state_paths turns the switch back on).
    """
    block_network(test)
    lifted = patch.dict(os.environ)
    lifted.start()
    test.addCleanup(lifted.stop)
    os.environ.pop(FORBID_NETWORK_ENV, None)


class FakeClock:
    """Injected clock: time only moves through sleep/advance (optionally a little per monotonic read)."""

    def __init__(self, start=BASE_UTC, *, monotonic_ns=5_000_000_000, auto_advance_s=0.0):
        self.wall_ns = parse_utc_ns(start)
        self.mono_ns = monotonic_ns
        self.auto_advance_ns = int(auto_advance_s * 1_000_000_000)
        self.sleeps = []

    def time_ns(self):
        return self.wall_ns

    def monotonic_ns(self):
        value = self.mono_ns
        if self.auto_advance_ns:
            self.mono_ns += self.auto_advance_ns
            self.wall_ns += self.auto_advance_ns
        return value

    def sleep(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError("sleep seconds must be a non-negative number")
        self.sleeps.append(seconds)
        self.advance(seconds)

    def utc_now(self):
        return utc_from_ns(self.wall_ns)

    def advance(self, seconds):
        step = int(round(seconds * 1_000_000_000))
        self.wall_ns += step
        self.mono_ns += step

    def step_wall(self, seconds):
        """Move only the wall clock (it may go backwards); monotonic time is untouched."""
        self.wall_ns += int(round(seconds * 1_000_000_000))


def default_keychain_items(profile="default"):
    return {
        (SERVICES["username"], profile): "synthetic-user",
        (SERVICES["password"], profile): "synthetic-password-0001",
        (SERVICES["app_key_delayed"], profile): "synthetic-delayed-key-0001",
        (SERVICES["app_key_live"], profile): "synthetic-live-key-0001",
    }


class FakeKeychainRunner:
    """Stands in for subprocess.run on /usr/bin/security. Records every call; never runs anything.

    Missing items return exit 44 (errSecItemNotFound) with the planted secret on stderr, to prove
    callers drop stderr. find_exit forces every find-generic-password to that exit code (36 stands
    for a locked Keychain, 51 or 128 for a denied or cancelled dialog); raise_error makes every
    call raise that exception, as when security cannot start.
    """

    def __init__(self, items=None, *, fail_services=(), add_fails=False, timeout=False, find_exit=None,
                 raise_error=None):
        self.items = dict(default_keychain_items() if items is None else items)
        self.fail_services = set(fail_services)
        self.add_fails = add_fails
        self.timeout = timeout
        self.find_exit = find_exit
        self.raise_error = raise_error
        self.calls = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append({"argv": argv, "env": dict(kwargs.get("env") or {}), "kwargs": dict(kwargs)})
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        if self.raise_error is not None:
            raise self.raise_error
        text = bool(kwargs.get("text"))

        def done(code, out=b"", err=b""):
            if text:
                out, err = out.decode(), err.decode()
            return subprocess.CompletedProcess(argv, code, out, err)

        if not argv or argv[0] != SECURITY_BIN or len(argv) < 2:
            return done(1, err=PLANTED_SECRET.encode())
        options = {}
        index = 2
        while index < len(argv):
            flag = argv[index]
            if flag in ("-s", "-a", "-T") and index + 1 < len(argv):
                options[flag] = argv[index + 1]
                index += 2
            elif flag == "-w" and index + 1 < len(argv):
                options[flag] = argv[index + 1]
                index += 2
            else:
                options[flag] = True
                index += 1
        key = (options.get("-s"), options.get("-a"))
        if argv[1] == "find-generic-password":
            if self.find_exit is not None:
                return done(self.find_exit, err=("security: " + PLANTED_SECRET + " keychain locked\n").encode())
            if key not in self.items or key[0] in self.fail_services:
                return done(44, err=("security: " + PLANTED_SECRET + " item could not be found\n").encode())
            if options.get("-w") is True:
                return done(0, (self.items[key] + "\n").encode())
            return done(0, f'keychain: "login.keychain-db"\nattributes:\n    "svce"<blob>="{key[0]}"\n'.encode())
        if argv[1] == "add-generic-password":
            if self.add_fails or not isinstance(options.get("-w"), str):
                return done(1, err=PLANTED_SECRET.encode())
            self.items[key] = options["-w"]
            return done(0)
        return done(1, err=PLANTED_SECRET.encode())


def json_body(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def http_response(value=None, *, status=200, headers=None, body=None):
    """A scripted FakeHttpTransport response; value is JSON-encoded unless body bytes are given."""
    return {"status": status, "headers": dict(headers or {"content-type": "application/json"}),
            "body": body if body is not None else json_body(value)}


def rpc_response(result, rpc_id=1):
    return http_response({"jsonrpc": "2.0", "result": result, "id": rpc_id})


def rpc_error_response(code, message, data=None, rpc_id=1, status=200):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return http_response({"jsonrpc": "2.0", "error": error, "id": rpc_id}, status=status)


def transport_failure(phase, code="scripted_failure", bytes_written=None):
    """Script entry that makes FakeHttpTransport raise at a chosen phase.

    With bytes_written None, TransportError derives may_have_arrived conservatively: False for
    preflight and connect, True for send, receive and decode (an unknown outcome is never "not
    sent"). Combinations the real HttpTransport cannot produce are refused: it reaches receive or
    decode only after writing the whole request, and writes nothing before send.
    """
    if phase not in ("preflight", "connect", "send", "receive", "decode"):
        raise ValueError("unknown transport phase")
    if bytes_written is not None and (type(bytes_written) is not int or bytes_written < 0):
        raise ValueError("bytes_written must be a non-negative integer or None")
    if phase in ("receive", "decode") and bytes_written == 0:
        raise ValueError("the real transport writes the whole request before the receive phase")
    if phase in ("preflight", "connect") and bytes_written:
        raise ValueError("the real transport writes nothing before the send phase")
    return {"raise_phase": phase, "code": code, "bytes_written": bytes_written}


class FakeHttpTransport:
    """Scripted stand-in for HttpTransport.request with a request log.

    Each request first runs the real transport.preflight (host/path allowlist and digest gate), the
    real deadline validation and, for a real ssl.SSLContext, the real TLS-context check, so tests
    exercise the same refusals; only the forbid-network switch is skipped because tests set it.
    allowed_hosts defaults to the com jurisdiction (COM_HTTP_HOSTS); pass
    config.allowed_http_hosts(config) for any other. Script entries: a response dict, an exception
    instance, transport_failure(...), or a callable taking the request record and returning one of those.

    send_by_monotonic_ns and send_gate (a place request, amendment A3 C2) are enforced as the real
    transport does, on the injected clock and after connect_delay_s (a slow connect) has elapsed: a
    refusal raises TransportError(phase "send", may_have_arrived False), writes zero bytes and is
    recorded in refused, never in requests. A send-by needs the fake to hold a clock.
    """

    def __init__(self, responses=(), *, allowed_hosts=None, enforce_preflight=True, clock=None, connect_delay_s=0.0):
        self.responses = list(responses)
        self.allowed_hosts = frozenset(COM_HTTP_HOSTS if allowed_hosts is None else allowed_hosts)
        self.enforce_preflight = enforce_preflight
        self.clock = clock
        self.connect_delay_s = connect_delay_s
        self.requests = []
        self.refused = []
        self.closed = 0
        self.bytes_written = []

    def queue(self, *items):
        self.responses.extend(items)

    def request(self, method, url, headers, body, *, deadline_s, idempotent, tls_context=None, order_digest=None,
                send_by_monotonic_ns=None, send_gate=None):
        record = {"method": method, "url": url, "headers": dict(headers), "body": body, "deadline_s": deadline_s,
                  "idempotent": idempotent, "tls_context": tls_context, "order_digest": order_digest,
                  "send_by_monotonic_ns": send_by_monotonic_ns, "send_gate": send_gate is not None}
        if self.enforce_preflight:
            try:
                transport_module.preflight(method, url, headers, body, allowed_hosts=self.allowed_hosts,
                                           idempotent=idempotent, digest=order_digest, check_env=False)
                transport_module.validate_deadline(deadline_s)
                if isinstance(tls_context, ssl.SSLContext):
                    transport_module.check_tls_context(tls_context)
                transport_module.check_send_bounds(idempotent, send_by_monotonic_ns, send_gate)
            except TransportError as exc:
                self.refused.append({**record, "code": exc.code})
                raise
        if self.clock is not None and self.connect_delay_s:
            self.clock.advance(self.connect_delay_s)          # DNS, TCP and TLS before any byte is written
        if send_by_monotonic_ns is not None or send_gate is not None:
            if send_by_monotonic_ns is not None and self.clock is None:
                raise AssertionError("FakeHttpTransport needs a clock to enforce a send-by")
            code = transport_module.send_refusal(self.clock, send_by_monotonic_ns, send_gate)
            if code is not None:
                self.refused.append({**record, "code": code})
                self.bytes_written.append(0)
                raise TransportError(code, phase="send", may_have_arrived=False)
        self.requests.append(record)
        if not self.responses:
            raise AssertionError("FakeHttpTransport has no scripted response left")
        item = self.responses.pop(0)
        if callable(item):
            item = item(record)
        if isinstance(item, BaseException):
            self.bytes_written.append(0)
            raise item
        if "raise_phase" in item:
            phase = item["raise_phase"]
            written = item.get("bytes_written")
            if written is None:
                self.bytes_written.append(0 if phase in ("preflight", "connect") else len(body or b""))
                arrived = None            # TransportError derives it conservatively from the phase
            else:
                self.bytes_written.append(written)
                arrived = written > 0
            raise TransportError(item.get("code", "scripted_failure"), phase=phase, may_have_arrived=arrived)
        self.bytes_written.append(len(body or b""))
        return {"status": item.get("status", 200), "headers": dict(item.get("headers", {})),
                "body": item.get("body", b"")}

    def close(self):
        self.closed += 1

    def json_requests(self):
        """Request bodies decoded as JSON (None for bodiless requests)."""
        return [json.loads(r["body"]) if r["body"] else None for r in self.requests]

    @staticmethod
    def header_text(record, name):
        value = record["headers"].get(name)
        return value.reveal() if hasattr(value, "reveal") else value


def temp_state_paths(test, *, profile="default", ensure=True):
    """StatePaths rooted in a fresh temporary directory (home and state root), cleaned up after the test.

    Overrides need the switch in the process environment too, so it is set here for the rest of
    the test (undone at cleanup); that also keeps every transport in the process offline.
    """
    switch = patch.dict(os.environ, FORBID_ENV)
    switch.start()
    test.addCleanup(switch.stop)
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    base = Path(temp.name).resolve()
    (base / "home").mkdir()
    paths = StatePaths(profile=profile, home=base / "home", root_override=base / "state", env=dict(FORBID_ENV))
    if ensure:
        paths.ensure()
    return paths


def deep_merge(base, overrides):
    merged = copy.deepcopy(base)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def make_test_config(paths, overrides=None):
    """The example config (unvalidated dict) for this profile, with deep-merged overrides."""
    config = json.loads(EXAMPLE_CONFIG.read_text())
    config["profile"] = paths.profile
    return deep_merge(config, overrides)


def write_test_config(paths, overrides=None, *, path=None):
    """Write a valid synthetic config to paths.config_path (or path) and return the path."""
    target = Path(path) if path is not None else paths.config_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(make_test_config(paths, overrides), indent=2))
    return target


def gzip_bytes(data):
    return gzip.compress(data, mtime=0)


def http_bytes(status=200, body=b"", headers=None, reason="OK"):
    """Raw HTTP/1.1 response bytes for ScriptedSocket."""
    fields = {"Content-Length": str(len(body))}
    fields.update(headers or {})
    head = [f"HTTP/1.1 {status} {reason}"] + [f"{name}: {value}" for name, value in fields.items()]
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


class ScriptedSocket:
    """In-memory socket for HttpTransport tests. Each response is released only after the next
    request starts being sent, as on a real connection, so buffered read-ahead cannot swallow it."""

    def __init__(self, responses=(), *, recv_chunk=65536, send_error=None, send_error_after=0, recv_error=None,
                 clock=None, advance_per_recv_s=0.0):
        self.queue = [bytes(r) for r in responses]
        self.pending = b""
        self.recv_chunk = recv_chunk
        self.send_error = send_error
        self.send_error_after = send_error_after
        self.recv_error = recv_error
        self.clock = clock
        self.advance_per_recv_s = advance_per_recv_s
        self.sent = bytearray()
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def setsockopt(self, *args):
        pass

    def fileno(self):
        return -1

    def send(self, data):
        data = bytes(data)
        if not self.pending and self.queue:
            self.pending = self.queue.pop(0)
        limit = len(data)
        if self.send_error is not None:
            room = self.send_error_after - len(self.sent)
            if room <= 0:
                raise self.send_error
            limit = min(limit, room)
        self.sent += data[:limit]
        return limit

    def sendall(self, data):
        view = bytes(data)
        while view:
            view = view[self.send(view):]

    def recv_into(self, buffer):
        if self.recv_error is not None:
            raise self.recv_error
        if self.clock is not None and self.advance_per_recv_s:
            self.clock.advance(self.advance_per_recv_s)
        size = min(len(buffer), self.recv_chunk, len(self.pending))
        buffer[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        return size

    def close(self):
        self.closed = True


class ScriptedConnection(http.client.HTTPSConnection):
    """A real http.client connection whose connect() attaches a ScriptedSocket instead of dialling."""

    def __init__(self, host, scripted, *, context, timeout, connect_error=None):
        super().__init__(host, 443, context=context, timeout=timeout)
        self.scripted = scripted
        self.connect_error = connect_error
        self.connects = 0

    def connect(self):
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.sock = self.scripted


class ConnectionScript:
    """connection_factory for HttpTransport: hands out one ScriptedSocket per new connection."""

    def __init__(self, *sockets, connect_error=None):
        self.sockets = list(sockets)
        self.connect_error = connect_error
        self.connections = []
        self.calls = []

    def __call__(self, host, *, context, timeout):
        self.calls.append({"host": host, "context": context, "timeout": timeout})
        if not self.sockets and self.connect_error is None:
            raise AssertionError("ConnectionScript has no scripted socket left")
        scripted = self.sockets.pop(0) if self.sockets else None
        conn = ScriptedConnection(host, scripted, context=context, timeout=timeout, connect_error=self.connect_error)
        self.connections.append(conn)
        return conn
