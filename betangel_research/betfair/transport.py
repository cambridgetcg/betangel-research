"""HTTPS transport for Betfair SSO and JSON-RPC, plus the TLS socket for the Exchange Stream API.

Preflight runs before any connection: the network-forbidden switch (the process environment or
the transport's env), an exact host and path allowlist (the Heartbeat API path is not on it), and
the digest gate, which lets only READ_METHODS travel without a capability and requires every other
JSON-RPC body to carry its own sha256 AND that digest to have been recorded by a capability consumed
in this process (capability.take_consumed, single use). The gate parses strictly
(duplicate keys, non-finite numbers and extra envelope keys are refused) so no body can mean one
method to the gate and another to Betfair. Only verifying TLS contexts are used. One deadline covers
connect, send and every read. Request bytes are counted, so a failure reports may_have_arrived =
bytes_written > 0; logins and other non-idempotent requests are never resent, and redirects are
never followed. Exception text is a phase and a code only: never headers, bodies or query strings.

A non-idempotent order request may carry an absolute send-by time and a send gate (amendment A3, C2;
the API client passes them for place orders only). Both are evaluated after the connection is
established and immediately before the request is written (putrequest and putheader only buffer):
the send-by on the monotonic clock first, then the gate. A refusal is TransportError(phase "send",
may_have_arrived False) with zero request bytes written, and the connection is dropped.
"""

import hmac
import http.client
import io
import re
import socket
import ssl
import time
from urllib.parse import urlsplit
import zlib

from .capability import take_consumed
from .constants import (ACCOUNT_METHOD_PREFIX, ACCOUNT_RPC_PATH, ALLOWED_PATHS, API_HOSTS, BETTING_METHOD_PREFIX,
                        BETTING_RPC_PATH, HTTP_HOSTS, PATH_HOST_KIND, READ_METHODS, SSO_CERT_HOSTS, SSO_HOSTS,
                        SSO_PATHS, STREAM_HOSTS, STREAM_PORT)
from .errors import ConfigError, CredentialError, ReadStopped, TransportError, safe_code
from .paths import network_forbidden
from .receipts import SYSTEM_CLOCK, bytes_sha256, strict_json_loads
from .secret import Secret

RPC_PATHS = frozenset({BETTING_RPC_PATH, ACCOUNT_RPC_PATH})
# A login may be processed even when its response is lost; a resend would be a second login the
# ledger never counted (spec section 2.10 rule 8: only reads are idempotent).
NON_IDEMPOTENT_PATHS = frozenset({SSO_PATHS["certlogin"], SSO_PATHS["login"]})
RPC_ENVELOPE_KEYS = frozenset({"jsonrpc", "method", "params", "id"})
MAX_DEADLINE_S = 600
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,20}")
_HOST_KINDS = {"sso": frozenset(SSO_HOSTS.values()), "sso_cert": frozenset(SSO_CERT_HOSTS.values()),
               "api": API_HOSTS}
_MANAGED_HEADERS = {"accept-encoding", "connection", "host", "content-length", "transfer-encoding", "expect"}
_RETRYABLE_READ_ERRORS = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError)
_READ_CHUNK = 65536


def _refuse(code, phase="preflight", may_have_arrived=False):
    return TransportError(code, phase=phase, may_have_arrived=may_have_arrived)


def _header_text(value):
    if isinstance(value, Secret):
        value = value.reveal()
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        return None
    return value


def preflight(method, url, headers, body, *, allowed_hosts, idempotent, digest=None, env=None, check_env=True):
    """Every refusal that must happen before a connection exists. Returns (host, target_path).

    digest is the order digest presented with the request. The network is forbidden when the
    process environment or env sets BETANGEL_BETFAIR_FORBID_NETWORK=1 (env cannot lift the
    process switch). The fake transport in the tests runs this same function with check_env=False,
    so gateway tests exercise the same gate.
    """
    if check_env and network_forbidden(env):
        raise _refuse("network_forbidden")
    if method not in ("GET", "POST") or type(idempotent) is not bool:
        raise _refuse("invalid_request")
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise _refuse("url_not_allowed")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        parts = None
    if parts is None:
        # Raised outside the except block so the parser's message (which may echo the URL) is not chained.
        raise _refuse("url_not_allowed")
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or port not in (None, 443) or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or host not in allowed_hosts
            or parts.path not in ALLOWED_PATHS or host not in _HOST_KINDS[PATH_HOST_KIND[parts.path]]):
        raise _refuse("url_not_allowed")
    if not isinstance(headers, dict):
        raise _refuse("invalid_request")
    for name, value in headers.items():
        if (not isinstance(name, str) or not name or any(c in name for c in ":\r\n\x00 ")
                or name.lower() == "expect" or _header_text(value) is None):
            raise _refuse("invalid_request")
    if body is not None and not isinstance(body, (bytes, bytearray)):
        raise _refuse("invalid_request")
    if idempotent and parts.path in NON_IDEMPOTENT_PATHS:
        raise _refuse("invalid_request")
    if parts.path in RPC_PATHS:
        _digest_gate(parts.path, body, idempotent=idempotent, digest=digest)
    elif digest is not None:
        raise _refuse("unbound_rpc_refused")
    return host, parts.path


def _digest_gate(path, body, *, idempotent, digest):
    """Spec section 2.10 rule 3 on exactly one strictly parsed envelope.

    strict_json_loads refuses duplicate keys (a body naming an order method first and a read
    method last must not pass as a read under a last-key-wins parser), non-finite numbers and
    nesting too deep to parse. The envelope must be exactly {jsonrpc: "2.0", method, params: {...},
    id: int}; a JSON array would be a batch and is refused outright.
    """
    if body is None:
        raise _refuse("unbound_rpc_refused")
    try:
        envelope = strict_json_loads(bytes(body).decode("utf-8"))
    except ValueError:
        envelope = None   # the decode error carries the body; it is dropped, not chained
    if (not isinstance(envelope, dict) or set(envelope) != RPC_ENVELOPE_KEYS or envelope["jsonrpc"] != "2.0"
            or not isinstance(envelope["method"], str) or not isinstance(envelope["params"], dict)
            or type(envelope["id"]) is not int):
        raise _refuse("unbound_rpc_refused")
    rpc_method = envelope["method"]
    if rpc_method in READ_METHODS:
        prefix = BETTING_METHOD_PREFIX if path == BETTING_RPC_PATH else ACCOUNT_METHOD_PREFIX
        if digest is not None or not rpc_method.startswith(prefix):
            raise _refuse("unbound_rpc_refused")
        return
    # The digest shape is checked before compare_digest, which raises TypeError on non-ASCII text.
    if (path != BETTING_RPC_PATH or idempotent or not rpc_method.startswith(BETTING_METHOD_PREFIX)
            or not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)
            or not hmac.compare_digest(digest, bytes_sha256(body))):
        raise _refuse("unbound_rpc_refused")
    # Last, so a refused request never uses up a consumed capability's digest.
    if not take_consumed(digest):
        raise _refuse("unbound_rpc_refused")


def check_send_bounds(idempotent, send_by_monotonic_ns, send_gate):
    """Send-by and send gate belong to non-idempotent order requests only, with valid types."""
    if send_by_monotonic_ns is None and send_gate is None:
        return
    if idempotent or (send_by_monotonic_ns is not None and type(send_by_monotonic_ns) is not int) or (
            send_gate is not None and not callable(send_gate)):
        raise _refuse("invalid_request")


def send_refusal(clock, send_by_ns, send_gate):
    """The code that stops a request at its first byte, or None: send_window_expired when the monotonic
    clock has reached send_by_ns, else the gate's own code (an exception in the gate counts as
    send_gate_failed), else send_window_expired when send_by_ns has passed while the gate ran. Shared with the
    test fakes so they enforce exactly this."""
    if send_by_ns is not None and clock.monotonic_ns() >= send_by_ns:
        return "send_window_expired"
    if send_gate is None:
        return None
    try:
        code = send_gate()
    except Exception:
        return "send_gate_failed"
    if code is not None:
        return safe_code(code)
    # The gate may take time (the pilot gate reads the journal head, P1-06 P106-C1): the absolute send-by
    # bound, the capability's expiry included, is checked again after it, right before the first byte.
    if send_by_ns is not None and clock.monotonic_ns() >= send_by_ns:
        return "send_window_expired"
    return None


def validate_deadline(deadline_s):
    """A positive, finite number of seconds up to MAX_DEADLINE_S; bool is not a number."""
    if (isinstance(deadline_s, bool) or not isinstance(deadline_s, (int, float))
            or not 0 < deadline_s <= MAX_DEADLINE_S):
        raise _refuse("invalid_deadline")
    return deadline_s


def check_tls_context(context):
    """Refuse any context that would not verify the server (CERT_REQUIRED, check_hostname, TLS >= 1.2)
    or that writes a TLS key log (keylog_filename set, for example from SSLKEYLOGFILE).

    Betfair's own sample disables verification (-k); a context built that way never carries a
    username, password, token or app key through this transport.
    """
    if not isinstance(context, ssl.SSLContext):
        raise _refuse("tls_context_unsafe")
    minimum = context.minimum_version
    if (context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname
            or (minimum != ssl.TLSVersion.MAXIMUM_SUPPORTED and minimum < ssl.TLSVersion.TLSv1_2)):
        raise _refuse("tls_context_unsafe")
    # A key log file would hold every session secret: with a capture, the login, tokens and app keys.
    if getattr(context, "keylog_filename", None) is not None:
        raise _refuse("tls_context_unsafe")
    return context


class _PassphraseNeeded(Exception):
    pass


def _no_passphrase():
    # OpenSSL calls this only for an encrypted key. Raising stops its interactive terminal prompt,
    # so a passphrase is never read outside the Keychain flow and the process never blocks on it.
    raise _PassphraseNeeded()


def make_tls_context(cert_path=None, key_path=None, key_passphrase=None):
    """Verifying TLS >= 1.2 context; loads the client certificate when one is given.

    key_passphrase must be None or a Secret. Without one, an encrypted key fails with
    client_certificate_unloadable instead of prompting on the terminal.

    Built from SSLContext directly, not ssl.create_default_context(): that function reads
    SSLKEYLOGFILE and would make OpenSSL create a world-readable key log of every Betfair session
    (login body, session token, app keys, stream authentication). The environment is never read
    here, and check_tls_context refuses any context with a key log.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    # The same chain-building flags create_default_context sets on current CPython (absent on older ones).
    context.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0) | getattr(ssl, "VERIFY_X509_STRICT", 0)
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    if (cert_path is None) != (key_path is None):
        raise CredentialError("client_certificate_needs_cert_and_key")
    if key_passphrase is not None and not isinstance(key_passphrase, Secret):
        raise CredentialError("key_passphrase_must_be_secret")
    if cert_path is not None:
        password = key_passphrase.reveal() if key_passphrase is not None else _no_passphrase
        try:
            context.load_cert_chain(str(cert_path), str(key_path), password=password)
        except (OSError, ssl.SSLError, ValueError, TypeError, _PassphraseNeeded):
            failed = True
        else:
            failed = False
        if failed:
            raise CredentialError("client_certificate_unloadable")
    return check_tls_context(context)


class _BudgetedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection whose TCP connect and TLS handshake share one time budget.

    The stock connect gives the TCP connect and the handshake the full timeout each. Here the
    handshake gets only what the TCP connect left over. Name resolution inside create_connection
    cannot be bounded with the standard library; the transport re-checks its deadline afterwards.
    Proxies and tunnels are not supported.
    """

    def __init__(self, host, *, context, timeout):
        super().__init__(host, 443, context=context, timeout=timeout)
        self._budget_context = context
        self._budget_s = timeout

    def connect(self):
        started = time.monotonic()
        sock = socket.create_connection((self.host, self.port), timeout=self._budget_s)
        try:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            remaining = self._budget_s - (time.monotonic() - started)
            if remaining <= 0:
                raise socket.timeout("connect budget spent")
            sock.settimeout(remaining)
            self.sock = self._budget_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _default_connection(host, *, context, timeout):
    return _BudgetedHTTPSConnection(host, context=context, timeout=timeout)


class _DeadlineExceeded(Exception):
    pass


class _MeteredReader(io.RawIOBase):
    def __init__(self, owner):
        super().__init__()
        self._owner = owner
        self._released = False

    def readable(self):
        return True

    def readinto(self, buffer):
        return self._owner._recv_into(buffer)

    def close(self):
        if not self._released:
            self._released = True
            self._owner._reader_closed()
        super().close()


class _MeteredSocket:
    """Socket proxy: counts bytes each way and re-arms the remaining deadline before every operation."""

    def __init__(self, sock, clock):
        self._sock = sock
        self._clock = clock
        self.deadline_ns = None
        self.bytes_written = 0
        self.bytes_received = 0
        self._readers = 0
        self._closing = False

    def arm(self, deadline_ns):
        self.deadline_ns = deadline_ns
        self.bytes_written = 0
        self.bytes_received = 0

    def _remaining(self):
        remaining = (self.deadline_ns - self._clock.monotonic_ns()) / 1e9
        if remaining <= 0:
            raise _DeadlineExceeded()
        return remaining

    def sendall(self, data):
        view = memoryview(data).cast("B")
        while view:
            self._sock.settimeout(self._remaining())
            sent = self._sock.send(view[:_READ_CHUNK])
            self.bytes_written += sent
            view = view[sent:]

    def send(self, data):
        self._sock.settimeout(self._remaining())
        sent = self._sock.send(data)
        self.bytes_written += sent
        return sent

    def makefile(self, mode="rb", buffering=None, **kwargs):
        if mode != "rb":
            raise ValueError("metered socket supports binary reads only")
        self._readers += 1
        return io.BufferedReader(_MeteredReader(self), buffer_size=_READ_CHUNK)

    def _recv_into(self, buffer):
        self._sock.settimeout(self._remaining())
        received = self._sock.recv_into(buffer)
        self.bytes_received += received
        return received

    def _reader_closed(self):
        self._readers -= 1
        if self._closing and self._readers <= 0:
            self._sock.close()

    def close(self):
        # Mirrors socket.makefile reference counting: http.client closes the connection while the
        # response still reads from it, so the real socket closes when the last reader does.
        self._closing = True
        if self._readers <= 0:
            self._sock.close()

    def settimeout(self, value):
        self._sock.settimeout(value)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class HttpTransport:
    """connection_factory(host, *, context, timeout) -> an http.client.HTTPSConnection-like object."""

    def __init__(self, *, allowed_hosts, max_response_bytes=32 * 1024 * 1024, idle_reconnect_s=170,
                 clock=SYSTEM_CLOCK, connection_factory=None, env=None):
        hosts = frozenset(h.lower() for h in allowed_hosts if isinstance(h, str))
        if not hosts or len(hosts) != len(frozenset(allowed_hosts)) or not hosts <= HTTP_HOSTS:
            raise ConfigError("transport_hosts_must_be_betfair_sso_or_api_hosts")
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ConfigError("max_response_bytes_must_be_positive_integer")
        if isinstance(idle_reconnect_s, bool) or not isinstance(idle_reconnect_s, (int, float)) or idle_reconnect_s <= 0:
            raise ConfigError("idle_reconnect_s_must_be_positive")
        self.allowed_hosts = hosts
        self.max_response_bytes = max_response_bytes
        self.idle_reconnect_ns = int(idle_reconnect_s * 1_000_000_000)
        self.clock = clock
        self.connection_factory = connection_factory or _default_connection
        self.env = env
        self.last_bytes_written = 0
        self._default_context = None
        self._connections = {}

    def _context(self, tls_context):
        if tls_context is not None:
            return check_tls_context(tls_context)
        if self._default_context is None:
            self._default_context = make_tls_context()
        return self._default_context

    def _drop(self, key):
        entry = self._connections.pop(key, None)
        if entry is not None:
            try:
                entry["conn"].close()
            except Exception:
                pass

    def close(self):
        for key in list(self._connections):
            self._drop(key)

    def _connection(self, host, context, deadline_ns):
        """Reuse a live keep-alive connection, or connect explicitly before anything is written."""
        key = (host, id(context))
        entry = self._connections.get(key)
        now = self.clock.monotonic_ns()
        if entry is not None and (entry["context"] is not context or entry["conn"].sock is None
                                  or now - entry["last_used_ns"] >= self.idle_reconnect_ns):
            self._drop(key)
            entry = None
        if entry is None:
            remaining = (deadline_ns - now) / 1e9
            if remaining <= 0:
                raise _refuse("deadline_exceeded", "connect")
            failure = None
            conn = None
            try:
                conn = self.connection_factory(host, context=context, timeout=remaining)
                conn.auto_open = 0
                conn.connect()
                if self.clock.monotonic_ns() >= deadline_ns:
                    # DNS, the TCP connect and the handshake may together overrun; nothing is sent then.
                    raise socket.timeout("connect overran the deadline")
                metered = _MeteredSocket(conn.sock, self.clock)
                conn.sock = metered
            except ssl.SSLCertVerificationError:
                failure = _refuse("tls_verification_failed", "connect")
            except ssl.SSLError:
                failure = _refuse("tls_failed", "connect")
            except (socket.timeout, TimeoutError):
                failure = _refuse("deadline_exceeded", "connect")
            except Exception:
                failure = _refuse("connect_failed", "connect")
            if failure is not None:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                raise failure
            entry = {"conn": conn, "context": context, "metered": metered, "last_used_ns": now}
            self._connections[key] = entry
        return key, entry

    accepts_attempt_guard = True             # api.GuardedTransport (P1-08 pilot reads) passes attempt_guard

    def request(self, method, url, headers, body, *, deadline_s, idempotent, tls_context=None, order_digest=None,
                send_by_monotonic_ns=None, send_gate=None, attempt_guard=None):
        """One request -> {"status", "headers" (lowercase, no set-cookie), "body" (decoded bytes)}.

        last_bytes_written is this request's request bytes, summed over its attempts (0 after any refusal).
        send_by_monotonic_ns and send_gate (non-idempotent requests only): see the module docstring.
        attempt_guard (an RS-19 pilot read only, P108-R2-2; None keeps today's behaviour): asked before every attempt,
        the idempotent retry on a fresh connection included; a non-empty answer raises ReadStopped and that attempt
        writes no byte.
        """
        self.last_bytes_written = 0
        host, path = preflight(method, url, headers, body, allowed_hosts=self.allowed_hosts, idempotent=idempotent,
                               digest=order_digest, env=self.env)
        validate_deadline(deadline_s)
        check_send_bounds(idempotent, send_by_monotonic_ns, send_gate)
        context = self._context(tls_context)
        deadline_ns = self.clock.monotonic_ns() + int(deadline_s * 1_000_000_000)
        final_headers = {}
        for name, value in headers.items():
            if name.lower() not in _MANAGED_HEADERS:
                final_headers[name] = _header_text(value)
        payload = bytes(body) if body is not None else None
        final_headers["Accept-Encoding"] = "gzip"
        final_headers["Connection"] = "keep-alive"
        if payload is not None or method == "POST":
            final_headers["Content-Length"] = str(len(payload or b""))
        attempts = 2 if idempotent else 1
        for attempt in range(attempts):
            if attempt_guard is not None:
                reason = attempt_guard()
                if reason:
                    raise ReadStopped(reason)
            try:
                outcome = self._attempt(host, path, method, final_headers, payload, context, deadline_ns,
                                        retry_allowed=idempotent and attempt + 1 < attempts,
                                        send_by_ns=send_by_monotonic_ns, send_gate=send_gate)
            except TransportError as failure:
                # A retry that fails before writing still follows an attempt that did write.
                if self.last_bytes_written > 0:
                    failure.may_have_arrived = True
                raise
            if outcome is not None:
                return outcome
        raise _refuse("connection_lost", "receive", self.last_bytes_written > 0)

    def _attempt(self, host, path, method, headers, payload, context, deadline_ns, *, retry_allowed, send_by_ns=None,
                 send_gate=None):
        """Returns the response, None to retry (idempotent reads only), or raises TransportError."""
        key, entry = self._connection(host, context, deadline_ns)
        conn, metered = entry["conn"], entry["metered"]
        metered.arm(deadline_ns)
        phase = "send"
        failure = None
        retry = False
        response = None
        try:
            # putrequest/putheader/endheaders: exactly these headers (plus Host), never Expect.
            conn.putrequest(method, path, skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            if send_by_ns is not None or send_gate is not None:
                refusal = send_refusal(self.clock, send_by_ns, send_gate)      # nothing written yet
                if refusal is not None:
                    raise _refuse(refusal, "send", False)
            conn.endheaders(payload)
            phase = "receive"
            response = conn.getresponse()
            status = response.status
            if 300 <= status < 400:
                failure = _refuse("redirect_refused", "receive", True)
            else:
                response_headers = _response_headers(response.getheaders())
                body = self._read_body(response, response_headers)
                phase = "decode"
                decoded = _decode(body, response_headers.get("content-encoding", ""), self.max_response_bytes)
        except _DeadlineExceeded:
            failure = _refuse("deadline_exceeded", phase, metered.bytes_written > 0)
        except TransportError as exc:
            failure = exc
        except (socket.timeout, TimeoutError):
            failure = _refuse("deadline_exceeded", phase, metered.bytes_written > 0)
        except _RETRYABLE_READ_ERRORS:
            if retry_allowed and metered.bytes_received == 0:
                retry = True
            else:
                failure = _refuse("connection_lost", phase, metered.bytes_written > 0)
        except Exception:
            # Unexpected failures still become a TransportError carrying only phase and code.
            failure = _refuse(f"{phase}_failed", phase, metered.bytes_written > 0)
        self.last_bytes_written += metered.bytes_written
        if failure is not None or retry:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            self._drop(key)
            if failure is not None:
                if failure.phase in ("send", "receive", "decode") and metered.bytes_written > 0:
                    failure.may_have_arrived = True
                raise failure
            return None
        response.close()
        entry["last_used_ns"] = self.clock.monotonic_ns()
        if response.will_close or conn.sock is None:
            self._drop(key)
        return {"status": status, "headers": response_headers, "body": decoded}

    def _read_body(self, response, response_headers):
        length = response_headers.get("content-length")
        if (length is not None and _CONTENT_LENGTH_RE.fullmatch(length.strip())
                and int(length) > self.max_response_bytes):
            raise _refuse("response_too_large", "receive", True)
        chunks = []
        total = 0
        while True:
            chunk = response.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_response_bytes:
                raise _refuse("response_too_large", "receive", True)
            chunks.append(chunk)
        # http.client returns a short body without complaint when the peer closes early.
        if getattr(response, "length", None):
            raise _refuse("incomplete_response", "receive", True)
        return b"".join(chunks)


def _response_headers(pairs):
    headers = {}
    for name, value in pairs:
        name = name.lower()
        if name in ("set-cookie", "set-cookie2"):
            continue
        headers[name] = value if name not in headers else headers[name] + ", " + value
    return headers


def _decode(body, encoding, limit):
    """gzip/deflate decoded with a bounded decompressor; identity passes through."""
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding in ("gzip", "x-gzip"):
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decompressor = zlib.decompressobj()
    else:
        raise _refuse("unsupported_content_encoding", "decode", True)
    out = bytearray()
    data = body
    while data:
        piece = decompressor.decompress(data, limit - len(out) + 1)
        out += piece
        if len(out) > limit:
            raise _refuse("response_too_large", "decode", True)
        data = decompressor.unconsumed_tail
        if not piece and data:
            raise _refuse("decode_failed", "decode", True)
    out += decompressor.flush()
    if len(out) > limit:
        raise _refuse("response_too_large", "decode", True)
    if not decompressor.eof or decompressor.unused_data:
        raise _refuse("decode_failed", "decode", True)
    return bytes(out)


def _stream_tls_context():
    return make_tls_context()


def open_stream_socket(host, port, timeout_s, *, env=None):
    """Verified TLS socket to the Exchange Stream API (allowlisted host, port 443 only)."""
    if network_forbidden(env):
        raise _refuse("network_forbidden")
    if host not in STREAM_HOSTS or type(port) is not int or port != STREAM_PORT:
        raise _refuse("url_not_allowed")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 120:
        raise _refuse("invalid_request")
    context = _stream_tls_context()
    raw = None
    failure = None
    try:
        raw = socket.create_connection((host, port), timeout=timeout_s)
        wrapped = context.wrap_socket(raw, server_hostname=host)
    except ssl.SSLCertVerificationError:
        failure = _refuse("tls_verification_failed", "connect")
    except ssl.SSLError:
        failure = _refuse("tls_failed", "connect")
    except (socket.timeout, TimeoutError):
        failure = _refuse("deadline_exceeded", "connect")
    except OSError:
        failure = _refuse("connect_failed", "connect")
    if failure is not None:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass
        raise failure
    return wrapped
