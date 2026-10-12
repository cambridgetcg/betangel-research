"""Offline fakes and synthetic builders for the stream tests (no TestCases here). Every value is invented.

FakeStreamSocket plays a script against the real StreamClient. Script items are bytes (delivered in
recv-sized pieces), wait(seconds) (moves the FakeClock; longer than the socket timeout raises
socket.timeout), EOF, an exception instance, or a callable taking the socket and returning bytes or
None. A callable that returns None is "not ready yet" (typically waiting for the client to send the
request it answers): the socket behaves as idle until it is ready. An idle socket advances the clock
by its timeout and raises socket.timeout, so every deadline eventually fires.

FakeStreamServer is a socket_factory handing out one scripted socket per connection. FakeSession is
the SessionManager surface the stream code uses (I5, signature only).
"""

import copy
import json
import socket

from betangel_research.betfair.config import validate_config
from betangel_research.betfair.errors import TransportError
from betangel_research.betfair.esa import StreamClient
from betangel_research.betfair.secret import Secret

import test_betfair_support as support

STREAM_HOST = "stream-api.betfair.com"
STREAM_PORT = 443
APP_KEY = "synthetic-stream-app-key-0001"
MARKET_TIME = "2020-01-01T12:00:00.000Z"       # equals support.BASE_PT
EOF = object()


class _Wait:
    def __init__(self, seconds):
        self.seconds = seconds


def wait(seconds):
    return _Wait(seconds)


def line(msg, terminator=b"\r\n"):
    """One wire line. json.dumps writes float('inf') as the bare Infinity token, as Betfair can."""
    return json.dumps(msg, separators=(",", ":")).encode("utf-8") + terminator


def connection_line(connection_id="002-200101120000-1"):
    return line({"op": "connection", "connectionId": connection_id})


def _sent_ids(sock, op):
    return [m.get("id") for m in sock.sent_messages() if m.get("op") == op]


def reply(op, *, nth=-1, status_code="SUCCESS", **fields):
    """A status reply to the nth request with this op the client has sent (waits until it is sent)."""
    def build(sock):
        ids = _sent_ids(sock, op)
        try:
            request_id = ids[nth]
        except IndexError:
            return None
        msg = {"op": "status", "id": request_id, "statusCode": status_code}
        if status_code == "SUCCESS":
            msg["connectionClosed"] = False
        msg.update(fields)
        return line(msg)
    return build


def auth_ok(connections_available=1):
    return reply("authentication", connectionsAvailable=connections_available)


def change(builder, *, nth=-1, op="marketSubscription", terminator=b"\r\n"):
    """A change message built by builder(subscription_id) for the nth subscription sent (waits until sent)."""
    def build(sock):
        ids = _sent_ids(sock, op)
        try:
            request_id = ids[nth]
        except IndexError:
            return None
        return line(builder(request_id), terminator)
    return build


def handshake(connection_id="002-200101120000-1", connections_available=1):
    return [connection_line(connection_id), auth_ok(connections_available), reply("marketSubscription")]


# --- synthetic market data ----------------------------------------------------------------------

def pt_at(offset_s):
    """Publish time offset_s seconds from support.BASE_PT (= MARKET_TIME)."""
    return support.BASE_PT + int(round(offset_s * 1000))


def market_definition(**changes):
    definition = {"bspMarket": False, "turnInPlayEnabled": True, "persistenceEnabled": True,
                  "marketBaseRate": 5.0, "eventId": "100", "eventTypeId": "7", "numberOfWinners": 1,
                  "bettingType": "ODDS", "marketType": "WIN", "marketTime": MARKET_TIME,
                  "suspendTime": MARKET_TIME, "bspReconciled": False, "complete": True, "inPlay": False,
                  "crossMatching": True, "runnersVoidable": False, "numberOfActiveRunners": 2, "betDelay": 0,
                  "status": "OPEN", "countryCode": "IE", "venue": "Synthetic Park",
                  "priceLadderDefinition": {"type": "CLASSIC"}, "version": 1,
                  "runners": [{"id": 11, "hc": 0, "sortPriority": 1, "status": "ACTIVE"},
                              {"id": 12, "hc": 0, "sortPriority": 2, "status": "ACTIVE"}]}
    definition.update(changes)
    return definition


def runners_adv(back=2.0, lay=2.02, volume=100.0, ltp=2.0):
    """ADV-style runner changes: batb/batl level triples, tv and ltp for runners 11 and 12."""
    return [{"id": rid, "batb": [[0, back, 30], [1, round(back - 0.02, 2), 99]], "batl": [[0, lay, 10]],
             "tv": volume, "ltp": ltp, "trd": [[back, volume]]} for rid in (11, 12)]


def mcm(sub_id, pt, mc=None, **top):
    msg = {"op": "mcm", "id": sub_id, "clk": top.pop("clk", f"clk-{pt}"), "pt": pt}
    if mc is not None:
        msg["mc"] = mc
    msg.update(top)
    return msg


def image(sub_id, pt, *, market_id=support.MARKET_ID, definition=None, rc=None, heartbeat_ms=5000,
          conflate_ms=0, extra_markets=()):
    """A SUB_IMAGE with one market (plus extra (market_id, definition, rc) triples)."""
    mc = [{"id": market_id, "img": True, "marketDefinition": definition or market_definition(),
           "rc": runners_adv() if rc is None else rc}]
    for other_id, other_definition, other_rc in extra_markets:
        mc.append({"id": other_id, "img": True, "marketDefinition": other_definition, "rc": other_rc})
    msg = mcm(sub_id, pt, mc, ct="SUB_IMAGE", initialClk=f"init-{pt}", heartbeatMs=heartbeat_ms)
    if conflate_ms is not None:
        msg["conflateMs"] = conflate_ms
    return msg


def delta(sub_id, pt, rc, *, market_id=support.MARKET_ID, **change_fields):
    return mcm(sub_id, pt, [dict({"id": market_id, "rc": rc}, **change_fields)])


def heartbeat(sub_id, pt):
    return mcm(sub_id, pt, ct="HEARTBEAT")


# --- fakes --------------------------------------------------------------------------------------

class FakeSession:
    """The SessionManager surface used by esa.py and recorder.py; values are synthetic."""

    def __init__(self, *, slot="delayed", token=support.PLANTED_SECRET, app_key=APP_KEY, relogin_error=None,
                 logout_error=None, keep_alive_error=None, token_error=None, keep_alive_result=None, clock=None):
        self._token = Secret(token)
        self._app_key = Secret(app_key)
        self._slot = slot
        self.relogin_error = relogin_error
        self.logout_error = logout_error
        self.keep_alive_error = keep_alive_error
        # What maybe_keep_alive() returns: None (not due), or a keep_alive() result such as
        # {"status": "FAIL", "error": "INTERNAL_ERROR"}, which SessionManager returns without raising.
        self.keep_alive_result = keep_alive_result
        self.token_error = token_error
        self.calls = []
        self.clock = clock
        self.keep_alive_at_ns = []          # monotonic time of each maybe_keep_alive() call (with a clock)

    @property
    def app_key_slot(self):
        return self._slot

    def token(self):
        self.calls.append("token")
        if self.token_error is not None:
            raise self.token_error
        return self._token

    def app_key(self):
        self.calls.append("app_key")
        return self._app_key

    def maybe_keep_alive(self):
        self.calls.append("maybe_keep_alive")
        if self.clock is not None:
            self.keep_alive_at_ns.append(self.clock.monotonic_ns())
        if self.keep_alive_error is not None:
            raise self.keep_alive_error
        return copy.deepcopy(self.keep_alive_result)

    def invalidate_and_relogin(self):
        self.calls.append("invalidate_and_relogin")
        if self.relogin_error is not None:
            raise self.relogin_error
        return {"status": "SUCCESS", "method": "cert", "can_bet": False, "logged_in_at": support.BASE_UTC}

    def logout(self):
        self.calls.append("logout")
        if self.logout_error is not None:
            raise self.logout_error
        return {"status": "SUCCESS", "error": None}


class FakeStreamSocket:
    def __init__(self, script, clock):
        self.script = list(script)
        self.clock = clock
        self.timeout = None
        self.sent = []
        self.closed = False
        self.recv_calls = 0

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def sendall(self, data):
        if self.closed:
            raise OSError("socket closed")
        self.sent.append(bytes(data))

    def sent_bytes(self):
        return b"".join(self.sent)

    def sent_messages(self):
        return [json.loads(chunk) for chunk in self.sent_bytes().split(b"\r\n") if chunk]

    def _idle(self):
        step = self.timeout if self.timeout is not None else 1.0
        self.clock.advance(step)
        raise socket.timeout("timed out")

    def pending(self):
        return bool(self.script)

    def recv(self, size):
        self.recv_calls += 1
        if self.closed:
            raise OSError("socket closed")
        while True:
            if not self.script:
                self._idle()
            item = self.script[0]
            if callable(item):
                produced = item(self)
                if produced is None:
                    self._idle()
                self.script[0] = produced
                continue
            if isinstance(item, _Wait):
                if self.timeout is not None and item.seconds > self.timeout:
                    self.clock.advance(self.timeout)
                    item.seconds -= self.timeout
                    raise socket.timeout("timed out")
                self.clock.advance(item.seconds)
                self.script.pop(0)
                continue
            if item is EOF:
                self.script.pop(0)
                return b""
            if isinstance(item, BaseException):
                self.script.pop(0)
                raise item
            data = bytes(item)
            if len(data) > size:
                self.script[0] = data[size:]
                return data[:size]
            self.script.pop(0)
            return data

    def close(self):
        self.closed = True


class FakeStreamServer:
    """socket_factory for StreamClient: one scripted FakeStreamSocket per connection, in order."""

    def __init__(self, connections, clock):
        self.pending_scripts = [list(script) for script in connections]
        self.clock = clock
        self.sockets = []
        self.calls = []

    def factory(self, host, port, timeout_s):
        self.calls.append((host, port, timeout_s))
        if not self.pending_scripts:
            raise TransportError("connect_failed", phase="connect", may_have_arrived=False)
        sock = FakeStreamSocket(self.pending_scripts.pop(0), self.clock)
        self.sockets.append(sock)
        return sock

    def exhausted(self):
        """No connection left to hand out and the current socket's script is used up."""
        return not self.pending_scripts and (not self.sockets or not self.sockets[-1].pending())

    def all_sent(self):
        return b"".join(sock.sent_bytes() for sock in self.sockets)


def make_client(server, session, clock, **kwargs):
    options = dict(host=STREAM_HOST, port=STREAM_PORT, session=session, socket_factory=server.factory, clock=clock)
    options.update(kwargs)
    return StreamClient(**options)


def recorder_config(paths, overrides=None):
    """A validated synthetic config for the recorder tests."""
    return validate_config(support.make_test_config(paths, overrides), paths=paths)


def lister(ids=(support.MARKET_ID,)):
    """A market_lister returning ids and recording each REST filter it was given."""
    calls = []

    def market_lister(rest_filter):
        calls.append(copy.deepcopy(rest_filter))
        return list(ids)
    market_lister.calls = calls
    return market_lister
