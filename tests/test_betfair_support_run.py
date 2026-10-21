"""Offline harness for the P1-08 executor commands, driven through cli.main (no TestCases here). Every value is
synthetic.

build_run assembles the P1-06 pilot fixtures (test_betfair_support_pilot) without a gateway object: an unoverridden
temporary state root (A13 passes), the pilot config, a FakeClock whose sleep moves time, a FakeKeychainRunner that
counts every Keychain call by service, RunBetfair (PilotBetfair that also answers the scheduler's catalogue query and
the capture reads), a PilotTransport, a FakeGit and injected code hashes in the PilotContext, a FakeConfirmer that types
exactly what each prompt asks (it stands in for Yu; no pty anywhere), and a fake ProcessTable for admission. run_cli
calls cli.main with those as deps and captures stdout and stderr; out and err writers that raise OSError(EIO) on
demand stand in for a closed terminal. No network, no sockets, no real Keychain, no real state root.
"""

import contextlib
import copy
import io
import json
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from betangel_research.betfair import admission, cli, pilot, pilot_gate, pilot_run
from betangel_research.betfair.arming import expected_token
from betangel_research.betfair.credentials import ANCHOR_SERVICE, SERVICES, anchor_write
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.paths import FORBID_NETWORK_ENV
from betangel_research.betfair.receipts import canonical_sha256

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
from test_betfair_admission import FakeProcesses
from test_betfair_support import dumps

S = sp.S
OFF = sp.OFF
DAY_S = 86400
CREDENTIAL_SERVICES = frozenset(SERVICES.values())
Death = sp.Death


class HookedKeychain(support.FakeKeychainRunner):
    """FakeKeychainRunner with on_credential: a callable run once, right before the first credential item read (the
    session credential loader), to inject a change "while the loader runs"."""

    on_credential = None

    def __call__(self, argv, **kwargs):
        argv_list = list(argv)
        service = argv_list[argv_list.index("-s") + 1] if "-s" in argv_list else None
        if service in CREDENTIAL_SERVICES and "-w" in argv_list and self.on_credential is not None:
            hook, self.on_credential = self.on_credential, None
            hook()
        return super().__call__(argv, **kwargs)


class RunBetfair(sp.PilotBetfair):
    """PilotBetfair plus the scheduler's catalogue query: a listMarketCatalogue filter with no marketIds is answered
    with every fixture market whose marketStartTime lies in the filter's window, first to start first."""

    def __call__(self, record):
        url = record["url"]
        if not (url.endswith("/api/login") or url.endswith("/api/certlogin") or url.endswith("/api/keepAlive")
                or url.endswith("/api/logout")):
            body = json.loads(record["body"])
            name = body["method"].split("/")[-1]
            params = body["params"]
            if name == sp.CATALOGUE and not (params.get("filter") or {}).get("marketIds") \
                    and not self.next_response.get(name):
                self.reads.append({"method": name, "params": copy.deepcopy(params), "slot": "scheduler"})
                window = params["filter"].get("marketStartTime") or {}
                low = pilot.parse_utc_ns(window["from"]) if window.get("from") else 0
                high = pilot.parse_utc_ns(window["to"]) if window.get("to") else 2 ** 62
                rows = [market.row({"MARKET_START_TIME"}) for market in sorted(self.markets.values(),
                                                                               key=lambda m: m.off_ns)
                        if low <= market.off_ns <= high]
                self.rpc_methods.append(name)
                return support.http_response(body=dumps({"jsonrpc": "2.0", "result": rows, "id": body["id"]}).encode())
        return super().__call__(record)


class BrokenStream(io.StringIO):
    """A terminal that raises OSError(EIO) once broken (after a SIGHUP, say), or a reentrant RuntimeError."""

    def __init__(self):
        super().__init__()
        self.broken = None

    def write(self, text):
        if self.broken is not None:
            raise self.broken
        return super().write(text)


def build_run(test, *, overrides=None, start=None, contract_raw=sp.CONTRACT_RAW, roundtrip=True, profile="default",
              processes=None, keychain_items=None):
    """The executor's offline world (not yet registered or armed). env.state holds the injected identity reads."""
    contract = pilot.contract_from_bytes(contract_raw)
    paths = gw.unoverridden_paths(test, profile=profile)
    config = gw.live_config(paths, support.deep_merge(sp.pilot_profile(contract.sha256), overrides or {}))
    clock = support.FakeClock(start or pilot.utc_text_from_ns(OFF - 600 * S))
    runner = HookedKeychain(items=keychain_items)
    exchange = gw.SimExchange(clock)
    router = RunBetfair(exchange)
    transport = sp.PilotTransport(router, clock=clock)
    journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
    test.addCleanup(journal.close)
    if roundtrip:
        journal.append("ref_roundtrip_verified", {"customer_order_ref_found": True})
    else:
        journal.append("note", {"reason": "harness_start_without_ref_roundtrip"})
    anchor_write(paths.profile, dict(journal.anchor(), updated_at=clock.utc_now()), runner=runner)
    runner.calls.clear()
    state = SimpleNamespace(contract_raw=contract_raw, disk_hashes=dict(sp.CODE_HASHES), contract_reads=0)
    git = sp.FakeGit()

    def read_contract(relative):
        state.contract_reads += 1
        if isinstance(state.contract_raw, BaseException):
            raise state.contract_raw
        return state.contract_raw

    ctx = pilot_gate.PilotContext(repo_root=sp.REPO, git_runner=git, contract_bytes_reader=read_contract,
                                  code_hashes=lambda root: state.disk_hashes,
                                  pinned_code_hashes_sha256=canonical_sha256(sp.CODE_HASHES))
    env = SimpleNamespace(test=test, paths=paths, config=config, clock=clock, runner=runner, exchange=exchange,
                          router=router, transport=transport, journal=journal, contract=contract, state=state, git=git,
                          ctx=ctx, confirmer=gw.FakeConfirmer(), processes=processes or FakeProcesses(pid=1),
                          out=None, err=None)
    return env


def token(env):
    return expected_token(env.config, env.clock.time_ns())


def run_cli(env, *argv, confirmer=None, out=None, err=None):
    """cli.main with the harness deps -> (exit code, stdout text, stderr text)."""
    out = out if out is not None else io.StringIO()
    err = err if err is not None else io.StringIO()
    env.out, env.err = out, err
    deps = {"keychain_runner": env.runner, "http_transport": env.transport, "clock": env.clock,
            "confirmer": confirmer or env.confirmer, "env": {FORBID_NETWORK_ENV: "1"}, "pilot_context": env.ctx,
            "processes": env.processes}
    with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
        code = cli.main(list(argv), deps=deps)
    return code, out.getvalue(), err.getvalue()


def register(env, **kw):
    return run_cli(env, "pilot-register", "--mode", "live", "--confirm-live", token(env), **kw)


def arm_run(env, until="2000", *extra, **kw):
    return run_cli(env, "pilot-run", "--mode", "live", "--confirm-live", token(env), "--until", until, *extra, **kw)


def events(env):
    return list(env.journal.events())


def notes(env, record_type=None):
    found = []
    for event in events(env):
        inner = event["payload"].get("pilot") if event["kind"] == "note" else None
        if inner is not None and (record_type is None or inner["type"] == record_type):
            found.append(inner)
    return found


def fold(env, now_ns=None):
    return pilot.fold(events(env), contract=env.contract, now_ns=env.clock.time_ns() if now_ns is None else now_ns)


def credential_reads(env):
    """Keychain calls that read or check a credential item (username, password, app keys, passphrase): the anchor
    service's reads by static A13 are counted apart (anchor_calls)."""
    found = []
    for call in env.runner.calls:
        argv = call["argv"]
        service = argv[argv.index("-s") + 1] if "-s" in argv else None
        if service in CREDENTIAL_SERVICES and "-w" in argv:
            found.append(service)
    return found


def anchor_calls(env):
    return [c for c in env.runner.calls if ANCHOR_SERVICE in c["argv"]]


def logins(env):
    return [r for r in env.transport.requests if r["url"].endswith("/api/login") or r["url"].endswith("/api/certlogin")]


def order_requests(env):
    return env.router.order_requests


def recording_files(env):
    found = {}
    for path in (env.paths.admission_file, env.paths.inhibit_file, env.paths.admission_generation_file,
                 env.paths.recorder_pid_file):
        found[path.name] = path.read_bytes() if path.exists() else None
    return found


def add_market(env, off_ns=OFF, market_id=None, **changes):
    market_id = market_id or f"1.{300000 + len(env.router.markets) + 1}"
    market = sp.Market(market_id, off_ns, **changes)
    env.router.markets[market_id] = market
    return market


def armed_file(env):
    return pilot_run.armed_session_path(env.paths)


# --- injections -------------------------------------------------------------------------------------------------


def fire(sig=signal.SIGINT):
    """Deliver sig synchronously, as the kernel would between two bytecodes: call the Python-level handler installed
    now (pilot-run's outer StopFlag handler, or a gateway's inner _SignalGuard)."""
    handler = signal.getsignal(sig)
    if not callable(handler):
        raise AssertionError(f"no Python handler installed for {sig!r}")
    handler(sig, None)


def on_sleep(env, when, action):
    """Run action() once, inside the first clock.sleep for which when(clock) is True (before that sleep moves time)."""
    original = env.clock.sleep
    done = []

    def sleep(seconds):
        if not done and when(env.clock):
            done.append(True)
            action()
        return original(seconds)

    env.clock.sleep = sleep
    return done


@contextlib.contextmanager
def sigkill_after(kind, pilot_type=None, *, when=None):
    """The process dies right after the first durable journal append of kind (and pilot record type, and when(payload)):
    nothing of pilot-run after it runs. Death passes through the §3.7 handler (a SIGKILL never runs it); the with
    blocks it unwinds stand in for the kernel letting go of the owner lock and .gateway.lock."""
    original = OrderJournal.append
    handler = pilot_run.Run.after_exception

    def append(self, event_kind, payload, **columns):
        event = original(self, event_kind, payload, **columns)
        inner = payload.get("pilot") if isinstance(payload, dict) else None
        if (event_kind == kind and (pilot_type is None or (isinstance(inner, dict) and inner.get("type") == pilot_type))
                and (when is None or when(payload))):
            raise Death(pilot_type or kind)
        return event

    def after(self, exc, ctx, *, in_cancel=False):
        if isinstance(exc, Death):
            raise exc
        return handler(self, exc, ctx, in_cancel=in_cancel)

    with patch.object(OrderJournal, "append", append), patch.object(pilot_run.Run, "after_exception", after):
        yield


def fills(env, *, matched=None, complete=False):
    """placeOrders matches part (matched £) or all (complete) of each pilot order at its price."""
    exchange = env.exchange

    def handler(params):
        exchange.place_handler = None
        try:
            result = exchange.place(params)
        finally:
            exchange.place_handler = handler
        for report in result["instructionReports"]:
            order = exchange.orders[report["betId"]]
            size, price = order["priceSize"]["size"], order["priceSize"]["price"]
            done = size if complete else matched
            status = "EXECUTION_COMPLETE" if complete else "EXECUTABLE"
            order.update(sizeMatched=done, sizeRemaining=round(size - done, 2), averagePriceMatched=price, status=status)
            report.update(sizeMatched=done, averagePriceMatched=price, orderStatus=status)
        return result

    exchange.place_handler = handler


def closes_after_off(env, market):
    """The market's book shows CLOSED once its off has passed (capture and settle reads)."""
    original = type(market).book

    def book(self=market):
        found = original(self)
        if env.clock.time_ns() > self.off_ns:
            found["status"] = "CLOSED"
        return found

    market.book = book


def hides_after_off(env, market):
    """listCurrentOrders stops listing a cancelled, unmatched bet of market once its off has passed (as the first live
    probe saw a fully unmatched cancelled bet vanish), so settle-market sees none of our bets current."""
    exchange = env.exchange
    original = exchange.current

    def current(**kw):
        exchange.hide_cancelled_unmatched = env.clock.time_ns() > market.off_ns
        return original(**kw)

    exchange.current = current


def settling(env, market):
    """market closes after its off and its cancelled bet leaves the current list: a settle point can offer it."""
    closes_after_off(env, market)
    hides_after_off(env, market)


def set_day(env, days=1, *, before_off_s=600):
    """Move the fake clock to `days` UTC days on, before_off_s before that day's 10:00Z."""
    target = OFF + days * DAY_S * S - before_off_s * S
    env.clock.advance((target - env.clock.time_ns()) / S)


def typed_release(env, generation=None):
    """Stands in for Yu's typed recording-release (the release function itself, no typed line here)."""
    return admission.release(env.paths, config=env.config, generation=generation, clock=env.clock)


def cause_file(env, text="synthetic cause: checked and understood\n"):
    directory = tempfile.TemporaryDirectory()
    env.test.addCleanup(directory.cleanup)
    path = Path(directory.name) / "cause.txt"
    path.write_text(text)
    return str(path)


def end_session(env, **kw):
    return run_cli(env, "pilot-end-session", "--mode", "live", **kw)


def ack(env, stop_id, *, cause=True, **kw):
    extra = ("--cause-file", cause_file(env)) if cause is True else ("--cause-file", cause) if cause else ()
    return run_cli(env, "pilot-ack", "--mode", "live", "--confirm-live", token(env), "--stop-id", stop_id, *extra, **kw)


def gap_resolved(env, stop_id, **kw):
    return run_cli(env, "pilot-gap-resolved", "--mode", "live", "--confirm-live", token(env), "--stop-id", stop_id,
                   "--cause-file", cause_file(env), **kw)


def capture(env, *extra, **kw):
    return run_cli(env, "pilot-capture", "--mode", "live", "--confirm-live", token(env), *extra, **kw)


def admission_view(env):
    return admission.admission_status(env.paths)


def hold_recorder(env):
    """A fake running recorder (pid 4242): it holds .recorder.lock and wrote recorder.pid. env.processes gets a table
    that knows it, and SIGTERM (terminate) makes it exit: its lock and pid file go. -> the list of pids signalled."""
    context = admission.recorder_admission(env.paths, invocation_id="rec-1", clock=env.clock,
                                           processes=FakeProcesses())
    context.__enter__()
    running = [True]

    def stop(pid=None):
        if running:
            running.clear()
            context.__exit__(None, None, None)

    env.test.addCleanup(stop)
    env.processes = FakeProcesses(pid=1, starts={1: "start-self", 4242: "start-A"}, on_terminate=stop)
    return env.processes.signals


def old_recorder(env):
    """An old recorder the rollout has not retired: it holds .recorder.lock beside ADMISSION and INHIBIT."""
    lock = pilot_run.file_lock(env.paths.recorder_lock, timeout_s=0, clock=env.clock)
    lock.__enter__()
    env.test.addCleanup(lambda: lock.__exit__(None, None, None))


def stops(env, scope=None):
    return [s for s in fold(env).stops if scope is None or s["scope"] == scope]


def active_review(env):
    return [s["stop_id"] for s in fold(env).active_stops if s["scope"] == "review"]


def spy_run(test):
    """Patch Run.go to record the Run (and the admission state and armed file) when the loop is entered."""
    seen = {}
    original = pilot_run.Run.go

    def go(self):
        seen["run"] = self
        seen["admission"] = admission.admission_status(self.paths)
        path = pilot_run.armed_session_path(self.paths)
        seen["armed_bytes"] = path.read_bytes() if path.exists() else None
        return original(self)

    patcher = patch.object(pilot_run.Run, "go", go)
    patcher.start()
    test.addCleanup(patcher.stop)
    return seen
