"""Shared offline harness for the RS-19 pilot gateway tests (P1-06; no TestCases here). Every value is synthetic.

build_pilot_gateway assembles a fully armed live gateway (test_betfair_support_gateway.build_live_gateway's parts)
for the pilot profile of contract p1, with a PilotContext whose git runner, contract bytes and code hashes are
injected, a registration receipt written to the temporary state root, and the registration and session notes
journaled. PilotBetfair serves per-market fixtures (a fixed off, the country, five or more ACTIVE runners with a
clear favourite) as exact JSON text, honours marketProjection and logs every read with its method, params and app
key slot. PilotTransport adds a before_gate hook that runs right before the first-byte check of an order request.
Only FakeClock moves time; a fresh state root per test; journaled ids and refs are asserted, never random values.
Restarts are new gateway objects; a re-fold stands in for a later process.
"""

import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from betangel_research.betfair import journal as journal_module
from betangel_research.betfair import pilot, pilot_gate
from betangel_research.betfair.api import JsonRpcClient, ReadClient
from betangel_research.betfair.arming import expected_token
from betangel_research.betfair.auth import SessionManager
from betangel_research.betfair.config import config_sha256
from betangel_research.betfair.credentials import anchor_write, load_credentials
from betangel_research.betfair.gateway import OrderGateway
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.receipts import atomic_write_json, canonical_json, canonical_sha256
from betangel_research.betfair.risk import RiskGuard

import test_betfair_support as support
import test_betfair_support_gateway as gw
from test_betfair_support import Raw, dumps

REPO = Path(__file__).resolve().parent.parent
CONTRACT_PATH = "research/pilots/p1-fav-t5.json"
CONTRACT_RAW = (REPO / CONTRACT_PATH).read_bytes()
C = pilot.contract_from_bytes(CONTRACT_RAW)
S = pilot.NS
DAY = "2027-01-04"
DAY_NS = pilot.parse_utc_ns(DAY + "T00:00:00Z")
OFF = DAY_NS + 10 * 3600 * S                       # 10:00:00Z
UNTIL = DAY_NS + 20 * 3600 * S                     # 20:00:00Z
SESSION = "s-20270104"
COMMIT = "c" * 40
TREE = "e" * 40
CODE_HASHES = {"betangel_research/betfair/gateway.py": "a" * 64, "betangel_research/betfair/pilot.py": "b" * 64}
FAV = 11
BASE_BACKS = {11: (("3.00", "15.00"), ("2.98", "50.00")), 12: (("4.5", "30"),), 13: (("5.0", "30"),),
              14: (("6.0", "30"),), 15: (("8.0", "30"),), 16: (("10.0", "30"),)}
BASE_LAYS = {11: (("3.05", "20.00"),), 12: (("4.6", "30"),), 13: (("5.1", "30"),), 14: (("6.2", "30"),),
             15: (("8.2", "30"),), 16: (("10.5", "30"),)}
CATALOGUE = "listMarketCatalogue"
BOOK = "listMarketBook"


def pilot_profile(contract_sha256=C.sha256):
    """The pilot profile (P1-06 §4): contract p1's caps in their existing homes, its strategy ref, orders.pilot."""
    return {"orders": {
        "customer_strategy_ref": "p1-fav-t5",
        "caps": {"max_stake_per_order": 2.0, "max_liability_per_order": 2.0, "max_market_exposure": 2.0,
                 "max_total_exposure": 20.0, "max_daily_loss": 50.0, "max_orders_per_minute": 2,
                 "max_orders_per_hour": 12, "max_orders_per_day": 25, "max_instructions_per_request": 1,
                 "max_price_deviation_ticks": 2, "max_book_age_s": 2.0, "max_minutes_before_off": 10},
        "rules": {"market_types": ["WIN"], "event_type_ids": ["7"], "external_exposure_tolerance": 0.0},
        "pilot": {"contract_path": CONTRACT_PATH, "contract_sha256": contract_sha256, "evidence_deadline_s": 5}}}


def off_text(off_ns, *, millis=True):
    text = pilot.utc_text_from_ns(off_ns)
    return text[:-4] + "Z" if millis else text[:-8] + "Z"


@dataclass
class Market:
    """One synthetic race: what the exchange serves for it, raw (decision reads) and parsed (the send book)."""
    market_id: str
    off_ns: int
    country: str = "GB"
    open_date: str = None
    delayed: bool = False
    status: str = "OPEN"
    backs: dict = field(default_factory=lambda: copy.deepcopy(BASE_BACKS))
    lays: dict = field(default_factory=lambda: copy.deepcopy(BASE_LAYS))
    statuses: dict = field(default_factory=dict)
    row_changes: dict = field(default_factory=dict)          # key -> value, or support.DROP-like None to drop
    market_time: str = None                                  # description.marketTime text (default the off)

    def runners(self):
        found = []
        for selection in sorted(self.backs):
            backs = [{"price": Raw(p), "size": Raw(s)} for p, s in self.backs[selection]]
            lays = [{"price": Raw(p), "size": Raw(s)} for p, s in self.lays.get(selection, ())]
            found.append({"selectionId": selection, "handicap": 0, "status": self.statuses.get(selection, "ACTIVE"),
                          "ex": {"availableToBack": backs, "availableToLay": lays, "tradedVolume": []}})
        return found

    def row(self, projections):
        off = off_text(self.off_ns)
        row = {"marketId": self.market_id, "marketName": "Synthetic 5f Hcap", "totalMatched": Raw("0")}
        if "MARKET_START_TIME" in projections:
            row["marketStartTime"] = off
        if "MARKET_DESCRIPTION" in projections:
            row["description"] = {"bettingType": "ODDS", "marketType": "WIN", "marketTime": self.market_time or off,
                                  "persistenceEnabled": True, "priceLadderDescription": {"type": "CLASSIC"}}
        if "EVENT_TYPE" in projections:
            row["eventType"] = {"id": "7", "name": "Horse Racing"}
        if "EVENT" in projections:
            row["event"] = {"id": "31000001", "name": "Synthetic Park", "countryCode": self.country,
                            "timezone": "Europe/London", "venue": "Synthetic Park", "openDate": self.open_date or off}
        if "RUNNER_DESCRIPTION" in projections:
            row["runners"] = [{"selectionId": s, "runnerName": f"R{s}", "handicap": 0, "sortPriority": i + 1}
                              for i, s in enumerate(sorted(self.backs))]
        for key, value in self.row_changes.items():
            if value is None:
                row.pop(key, None)
            else:
                row[key] = value
        return row

    def book(self):
        runners = self.runners()
        return {"marketId": self.market_id, "isMarketDataDelayed": self.delayed, "status": self.status,
                "betDelay": 0, "inplay": False, "numberOfWinners": 1, "numberOfRunners": len(runners),
                "version": 1, "runners": runners}


class PilotBetfair(gw.FakeBetfair):
    """FakeBetfair that serves the pilot fixtures' catalogue rows and books as exact JSON text (marketProjection
    honoured, so a row without EVENT has no event) and logs every read: (method, params, app key slot)."""

    def __init__(self, exchange):
        super().__init__(exchange)
        self.markets = {}
        self.reads = []

    def __call__(self, record):
        url = record["url"]
        if url.endswith("/api/login") or url.endswith("/api/certlogin") or url.endswith("/api/keepAlive") \
                or url.endswith("/api/logout"):
            return super().__call__(record)
        body = json.loads(record["body"])
        method, params, rpc_id = body["method"], body["params"], body["id"]
        name = method.split("/")[-1]
        key = support.FakeHttpTransport.header_text(record, "X-Application")
        slot = "live" if key == gw.LIVE_KEY else "delayed" if key == "synthetic-delayed-key-0001" else key
        if name in (CATALOGUE, BOOK):
            self.reads.append({"method": name, "params": copy.deepcopy(params), "slot": slot})
        ids = (params.get("filter") or {}).get("marketIds") if name == CATALOGUE else params.get("marketIds")
        if self.next_response.get(name) or name not in (CATALOGUE, BOOK) or not ids \
                or not all(m in self.markets for m in ids):
            return super().__call__(record)
        self.rpc_methods.append(name)
        if name == CATALOGUE:
            items = [self.markets[m].row(set(params.get("marketProjection") or ())) for m in ids]
        else:
            items = [self.markets[m].book() for m in ids]
        return support.http_response(body=dumps({"jsonrpc": "2.0", "result": items, "id": rpc_id}).encode())

    def reads_of(self, name, market_id=None):
        def market(read):
            params = read["params"]
            if read["method"] == CATALOGUE:
                return (params.get("filter") or {}).get("marketIds") or []
            return params.get("marketIds") or []
        return [r for r in self.reads if r["method"] == name and (market_id is None or market_id in market(r))]


class PilotTransport(gw.RoutingTransport):
    """RoutingTransport with before_gate: a callable run once, right before the first-byte check of the next request
    that carries a send gate (after the submit and the capability, before any byte)."""

    before_gate = None

    def request(self, method, url, headers, body, *, deadline_s, idempotent, tls_context=None, order_digest=None,
                send_by_monotonic_ns=None, send_gate=None):
        if send_gate is not None and self.before_gate is not None:
            hook, self.before_gate = self.before_gate, None
            hook()
        return super().request(method, url, headers, body, deadline_s=deadline_s, idempotent=idempotent,
                               tls_context=tls_context, order_digest=order_digest,
                               send_by_monotonic_ns=send_by_monotonic_ns, send_gate=send_gate)


class FakeGit:
    """subprocess.run for git: `rev-parse HEAD HEAD^{tree}` and `status --porcelain`; never runs git."""

    def __init__(self, commit=COMMIT, tree=TREE, dirty=False, fail=False):
        self.commit, self.tree, self.dirty, self.fail = commit, tree, dirty, fail
        self.calls = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if self.fail:
            return subprocess.CompletedProcess(argv, 128, "", "fatal")
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.commit}\n{self.tree}\n", "")
        if argv[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(argv, 0, "?? new.py\n" if self.dirty else "", "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")


def build_pilot_gateway(test, *, overrides=None, start=None, contract_raw=CONTRACT_RAW, register=True, arm=True,
                        pt4_done=False, until_ns=UNTIL, profile="default", roundtrip=True):
    """A live gateway for the pilot profile, not yet opened, over PilotBetfair. env.state holds what the injected
    identity reads return (contract bytes, disk code hashes); change it to model drift after construction."""
    contract = pilot.contract_from_bytes(contract_raw)
    paths = gw.unoverridden_paths(test, profile=profile)
    config = gw.live_config(paths, support.deep_merge(pilot_profile(contract.sha256), overrides or {}))
    clock = support.FakeClock(start or pilot.utc_text_from_ns(OFF - 299 * S))
    runner = support.FakeKeychainRunner()
    exchange = gw.SimExchange(clock)
    router = PilotBetfair(exchange)
    transport = PilotTransport(router, clock=clock)
    credentials = load_credentials(config, purpose="live_orders", profile=paths.profile, runner=runner)
    session = SessionManager(config, credentials, paths=paths, transport=transport, clock=clock)
    rpc = JsonRpcClient(config, session, clock=clock)
    read = ReadClient(rpc, paths=paths, clock=clock)
    journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
    test.addCleanup(journal.close)
    if roundtrip:
        journal.append("ref_roundtrip_verified", {"customer_order_ref_found": True})
    else:
        journal.append("note", {"reason": "harness_start_without_ref_roundtrip"})      # A19 then fails
    anchor_write(paths.profile, dict(journal.anchor(), updated_at=clock.utc_now()), runner=runner)
    guard = RiskGuard(config, mode="live", clock=clock, paths=paths)
    state = SimpleNamespace(contract_raw=contract_raw, disk_hashes=dict(CODE_HASHES), contract_reads=0)
    git = FakeGit()

    def read_contract(relative):
        state.contract_reads += 1
        if isinstance(state.contract_raw, BaseException):
            raise state.contract_raw
        return state.contract_raw

    ctx = pilot_gate.PilotContext(repo_root=REPO, git_runner=git, contract_bytes_reader=read_contract,
                                  code_hashes=lambda root: state.disk_hashes,
                                  pinned_code_hashes_sha256=canonical_sha256(CODE_HASHES))
    confirmer = gw.FakeConfirmer()
    gateway = OrderGateway(config, paths=paths, mode="live", journal=journal, guard=guard, rpc=rpc, read=read,
                           confirmer=confirmer, clock=clock, env={}, keychain_runner=runner, pilot=ctx)
    env = SimpleNamespace(paths=paths, config=config, clock=clock, runner=runner, exchange=exchange, router=router,
                          transport=transport, session=session, rpc=rpc, read=read, journal=journal, guard=guard,
                          confirmer=confirmer, gateway=gateway, token=expected_token(config, clock.time_ns()),
                          contract=contract, contract_raw=contract_raw, state=state, git=git, ctx=ctx, test=test,
                          receipt_sha256=None, session_id=None)
    if register:
        env.receipt_sha256 = register_pilot(env)
    if pt4_done:
        note(env, "pt_receipt", kind="PT4", receipt_sha256="d" * 64, registration_receipt_sha256=env.receipt_sha256,
             l_fields_verified=True, matched_settled_bets=1, profit_basis="gross")
    if arm:
        env.session_id = arm_session(env, until_ns=until_ns)
    return env


def note(env, record_type, *, journal=None, market_id=None, customer_order_ref=None, **fields):
    """Journal one pilot record of env's contract as a note (with the column its type needs)."""
    return (journal or env.journal).append("note", pilot.pilot_note(record_type, env.contract.sha256, **fields),
                                           market_id=market_id, customer_order_ref=customer_order_ref)


def write_receipt(env, **changes):
    """A registration receipt (note §7, Q4) in the state root -> the sha256 of its bytes."""
    head = env.journal.anchor()
    data = {"schema": pilot_gate.REGISTRATION_SCHEMA, "contract_sha256": env.contract.sha256, "code_commit": COMMIT,
            "code_tree": TREE, "code_hashes_digest": canonical_sha256(CODE_HASHES),
            "config_sha256": config_sha256(env.config), "registered_at": env.clock.utc_now(),
            "journal_seq": head.get("seq", 0)}
    data.update(changes)
    env.paths.pilot_dir.mkdir(mode=0o700, exist_ok=True)
    path = pilot_gate.registration_path(env.paths, env.contract.sha256, env.clock.time_ns())
    while path.exists():
        env.clock.advance(1)
        path = pilot_gate.registration_path(env.paths, env.contract.sha256, env.clock.time_ns())
    return atomic_write_json(path, data)


def register_pilot(env, **changes):
    sha = write_receipt(env, **changes)
    note(env, "registration", registration_receipt_sha256=sha, predecessor_contract_sha256=None)
    return sha


def arm_session(env, *, until_ns=UNTIL, session_id=None):
    day = pilot.utc_day(env.clock.time_ns())
    session_id = session_id or "s-" + day.replace("-", "")
    note(env, "session_start", session_id=session_id, registration_receipt_sha256=env.receipt_sha256, utc_day=day,
         until_ns=until_ns)
    return session_id


def add_market(env, off_ns=OFF, market_id=None, **changes):
    market_id = market_id or f"1.{200000 + len(env.router.markets) + 1}"
    market = Market(market_id, off_ns, **changes)
    env.router.markets[market_id] = market
    return market


def set_time(clock, target_ns):
    """Move both clocks forward to target_ns on the wall clock (never backwards)."""
    step = target_ns - clock.time_ns()
    if step < 0:
        raise AssertionError("set_time moves forward only")
    clock.advance(step / S)


def token(env):
    """Today's confirm token (it changes with the UTC day)."""
    env.token = expected_token(env.config, env.clock.time_ns())
    return env.token


def open_pilot(test, env, *, gateway=None):
    gateway = gateway or env.gateway
    report = gateway.open(command="pilot-place", confirm_token=token(env))
    test.addCleanup(gateway.close)
    return report


def place(env, market, *, gateway=None, scheduler_off_ns=None):
    gateway = gateway or env.gateway
    return gateway.pilot_place(market.market_id, scheduler_off_ns=market.off_ns if scheduler_off_ns is None
                               else scheduler_off_ns, confirm_token=token(env))


def events(env):
    return list(env.journal.events())


def pilot_notes(env, record_type=None):
    found = []
    for event in events(env):
        if event["kind"] == "note" and "pilot" in event["payload"]:
            if record_type is None or event["payload"]["pilot"]["type"] == record_type:
                found.append(event)
    return found


def fold(env, *, live_open_id=None, now_ns=None):
    return pilot.fold(events(env), contract=env.contract, now_ns=env.clock.time_ns() if now_ns is None else now_ns,
                      live_open_id=live_open_id)


def assert_no_violations(test, env):
    """Every legitimate flow ends with a fold that reports no protocol violation (P1-06 §6)."""
    state = fold(env)
    test.assertEqual(state.violations, (), state.violations)
    return state


def other_writer(env, clock=None):
    """Another process's journal connection on the same file (optionally with its own clock)."""
    writer = OrderJournal(env.paths.journal_path("live"), mode="live", clock=clock or env.clock)
    env.test.addCleanup(writer.close)
    return writer


def new_gateway(env, *, clock=None, config=None, pilot_context=True):
    """A restart, or another process: a new gateway object, journal connection, session and guard on the same state
    root (optionally its own clock, another config, or no PilotContext)."""
    clock = clock or env.clock
    config = config or env.config
    credentials = load_credentials(config, purpose="live_orders", profile=env.paths.profile, runner=env.runner)
    session = SessionManager(config, credentials, paths=env.paths, transport=env.transport, clock=clock)
    rpc = JsonRpcClient(config, session, clock=clock)
    read = ReadClient(rpc, paths=env.paths, clock=clock)
    journal = OrderJournal(env.paths.journal_path("live"), mode="live", clock=clock)
    env.test.addCleanup(journal.close)
    guard = RiskGuard(config, mode="live", clock=clock, paths=env.paths)
    return OrderGateway(config, paths=env.paths, mode="live", journal=journal, guard=guard, rpc=rpc, read=read,
                        confirmer=gw.FakeConfirmer(), clock=clock, env={}, keychain_runner=env.runner,
                        pilot=env.ctx if pilot_context else None)


class Death(BaseException):
    """A process death simulated right after one journal append (nothing after it runs)."""


def die_after(journal, kind, pilot_type=None):
    """Patch journal.append so the process 'dies' right after the first append of kind (and pilot record type)."""
    original = journal.append

    def append(event_kind, payload, **columns):
        event = original(event_kind, payload, **columns)
        inner = payload.get("pilot") if isinstance(payload, dict) else None
        if event_kind == kind and (pilot_type is None or (isinstance(inner, dict) and inner.get("type") == pilot_type)):
            raise Death(pilot_type or kind)
        return event

    return patch.object(journal, "append", append)


def raw_insert(env, kind, payload, *, recorded_at, **columns):
    """Append one event by a raw SQLite chain insert (no fold check, no fsync): bulk seeding only."""
    db = sqlite3.connect(str(env.paths.journal_path("live")), isolation_level=None)
    try:
        head = db.execute("SELECT seq, event_sha256 FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        event = {"seq": head[0] + 1, "event_id": "ev-" + f"{head[0] + 1:032x}", "recorded_at": recorded_at,
                 "monotonic_ns": env.clock.mono_ns, "kind": kind, "mode": "live", "intent_id": None,
                 "customer_order_ref": None, "customer_ref": None, "bet_id": None, "market_id": None,
                 "state_after": None, "payload": payload, "prev_sha256": head[1]}
        event.update(columns)
        event["event_sha256"] = journal_module._event_sha256(event)
        db.execute(journal_module._INSERT_SQL, tuple(
            canonical_json(payload).decode() if name == "payload_json" else event[name]
            for name in journal_module.EVENT_COLUMNS))
    finally:
        db.close()


def preload_place_records(env, count, *, first_off=None, journal=None):
    """count PLACE records journaled as other opens would leave them (reservation, then the record classify
    returns on fixture bytes, then its stops), each observed now with its off 296 s ahead, one second apart, and
    never submitted: definitely unsent once their opens end, their slots used (a capacity preload, P1-06 §6.1)."""
    journal = journal or env.journal
    placed = []
    for index in range(count):
        now = env.clock.time_ns()
        off = (first_off or now + 296 * S) if index == 0 else now + 296 * S
        market = Market(f"1.{900000 + len(env.router.markets) + 1}", off)
        env.router.markets[market.market_id] = market
        open_id = f"open-preload{len(env.router.markets):04d}"
        res = note(env, "reservation", journal=journal, market_id=market.market_id, session_id=env.session_id,
                   open_id=open_id)
        rows = [e for e in journal.events()]
        state = pilot.fold(rows, contract=env.contract, now_ns=env.clock.time_ns(), live_open_id=open_id)
        row_body = dumps({"jsonrpc": "2.0", "result": [market.row({"EVENT", "EVENT_TYPE", "MARKET_DESCRIPTION",
                                                                     "MARKET_START_TIME"})], "id": 1}).encode()
        book_body = dumps({"jsonrpc": "2.0", "result": [market.book()], "id": 1}).encode()
        result = pilot.classify(env.contract, state, reservation_seq=res["seq"], open_id=open_id,
                                session_id=env.session_id, registration_receipt_sha256=env.receipt_sha256,
                                customer_order_ref=f"bro-preload-{len(env.router.markets):04d}", row_body=row_body,
                                book_body=book_body, observed_wall_ns=now, observed_monotonic_ns=env.clock.mono_ns)
        if result["record"] is None:
            raise AssertionError(f"preload refused: {result['refusal']}")
        event = journal.append("note", {"pilot": result["record"]}, market_id=market.market_id)
        for stop in result["stops"]:
            journal.append("note", {"pilot": stop})
        placed.append((market, event, result["record"]))
        env.clock.advance(1)
    return placed


class FastJournal(OrderJournal):
    """A journal connection without fsync, for bulk-seeding a test history only (every append still validated
    and folded exactly as OrderJournal does)."""

    def _configure(self):
        super()._configure()
        self._db.execute("PRAGMA synchronous=OFF")
        self._db.execute("PRAGMA fullfsync=OFF")
        self._db.execute("PRAGMA checkpoint_fullfsync=OFF")


def build_history(env, *, place_records=494, skips_per_place=1, min_events=20_000, first_day_ns=None):
    """A representative pilot history (P1-06 critique 3): place_records PLACE records over consecutive armed days
    (5 on the PT4 day, then 25 a day), each with its reservation, decision, intent, funds read, risk decision,
    submit and report: four in five FAILED(transport_not_sent), one in five sent, lapsed unmatched and captured by a C
    read. SKIP decisions in between, and per-open funds and startup-reconcile events, bring the journal to at least
    min_events. Everything settles at £0, so no cap, pause or stop is reached. -> (journal events, last day ns)."""
    journal = FastJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
    env.test.addCleanup(journal.close)
    contract, registration = env.contract, env.receipt_sha256
    day_ns = first_day_ns or pilot.parse_utc_ns(pilot.utc_day(env.clock.time_ns()) + "T00:00:00Z")
    placed = market_no = 0
    funds = {"availableToBetBalance": 1000.0, "exposure": 0.0, "retainedCommission": 0.0, "exposureLimit": -10000.0,
             "discountRate": 0.0, "pointsBalance": 0}
    startup = {"exchange_orders_today": 0, "exchange_realized_today_pence": 0, "foreign": [], "exchange_open": [],
               "contradictions": [], "read_failures": [], "notes": []}
    day = 0
    while placed < place_records:
        session = f"h-{day:03d}"
        set_time(env.clock, day_ns + 9 * 3600 * S)
        if day == 1:
            note(env, "pt_receipt", journal=journal, kind="PT4", receipt_sha256="d" * 64,
                 registration_receipt_sha256=registration, l_fields_verified=True, matched_settled_bets=1,
                 profit_basis="gross")
        note(env, "session_start", journal=journal, session_id=session, registration_receipt_sha256=registration,
             utc_day=pilot.utc_day(env.clock.time_ns()), until_ns=day_ns + 20 * 3600 * S)
        limit = contract.per_pt4_session_day if day == 0 else contract.per_utc_day
        for index in range(limit):
            if placed >= place_records:
                break
            for skip in range(skips_per_place + 1):
                market_no += 1
                off = day_ns + 10 * 3600 * S + (index * (skips_per_place + 1) + skip) * 600 * S
                market = Market(f"1.{700000 + market_no}", off)
                open_id = f"open-h{market_no:06d}"
                set_time(env.clock, off - 300 * S)
                journal.append("startup_reconcile", startup)
                set_time(env.clock, off - 299 * S)
                res = note(env, "reservation", journal=journal, market_id=market.market_id, session_id=session,
                           open_id=open_id)
                row_body = dumps({"jsonrpc": "2.0", "result": [market.row({"EVENT", "EVENT_TYPE", "MARKET_DESCRIPTION",
                                                                             "MARKET_START_TIME"})], "id": 1}).encode()
                book_body = dumps({"jsonrpc": "2.0", "result": [market.book()], "id": 1}).encode()
                set_time(env.clock, off - 294 * S)
                if skip < skips_per_place:
                    record = pilot.decision_record(
                        contract, outcome="SKIP", code="evidence_invalid", registration_receipt_sha256=registration,
                        session_id=session, open_id=open_id, reservation_seq=res["seq"], market_id=market.market_id,
                        customer_order_ref="bro-skip", ev=None, row_body=row_body, book_body=book_body,
                        predicates=None, observed_wall_ns=off - 295 * S, observed_monotonic_ns=1, day_limit=limit)
                    journal.append("note", {"pilot": record}, market_id=market.market_id)
                    continue
                placed += 1
                ref = f"bro{market_no:012d}-{'0' * 15}{placed % 10}"[:32]
                ev = pilot.decision_evidence(row_body, book_body, market_id=market.market_id)
                predicates = pilot.evaluate_predicates(contract, ev, observed_wall_ns=off - 295 * S)
                record = pilot.decision_record(
                    contract, outcome="PLACE", code=None, registration_receipt_sha256=registration, session_id=session,
                    open_id=open_id, reservation_seq=res["seq"], market_id=market.market_id, customer_order_ref=ref,
                    ev=ev, row_body=row_body, book_body=book_body, predicates=predicates,
                    observed_wall_ns=off - 295 * S, observed_monotonic_ns=1, day_limit=limit, lifetime_slot=placed,
                    day_count=index + 1)
                journal.append("note", {"pilot": record}, market_id=market.market_id)
                digest = pilot.record_sha256(record)
                intent_id, cref = f"int-{market_no:016x}", f"brq{market_no:012d}-{'0' * 16}"[:32]
                set_time(env.clock, off - 293 * S)
                journal.append("funds_check", {"funds": funds})
                journal.append("intent", {"order": pilot.expected_instruction(record),
                                          "customer_strategy_ref": "p1-fav-t5", "is_probe": False, "parent_ref": None,
                                          "provisional_child": False, "source": "pilot"},
                               intent_id=intent_id, customer_order_ref=ref, customer_ref=cref,
                               market_id=market.market_id, state_after="RECORDED")
                journal.append("risk_decision", {"decision_id": f"dec-{market_no:024x}", "approved": True, "rule": None,
                                                 "action": "place", "detail": {}, "worst_case": {},
                                                 "evaluated_at": env.clock.utc_now(), "pilot_record_sha256": digest},
                               intent_id=intent_id, customer_ref=cref)
                journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": None,
                                          "decision_id": f"dec-{market_no:024x}", "probe": False,
                                          "params": {"customerStrategyRef": "p1-fav-t5"}, "pilot_record_sha256": digest,
                                          "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                               intent_id=intent_id, customer_ref=cref, market_id=market.market_id)
                set_time(env.clock, off - 292 * S)
                if placed % 5:
                    journal.append("report", {"action": "place", "report": None, "outcome": "FAILED_NOT_SENT",
                                              "transitions": [{"customer_order_ref": ref, "state": "FAILED",
                                                               "reason": "place_exception",
                                                               "fields": {"error_code": "transport_not_sent"}}]},
                                   intent_id=intent_id, market_id=market.market_id)
                    continue
                bet = f"{400000000000 + placed}"
                item = {"customer_order_ref": ref, "status": "SUCCESS", "error_code": None,
                        "order_status": "EXECUTION_COMPLETE", "bet_id": bet, "placed_date": None,
                        "average_price_matched_h": None, "size_matched_p": 0}
                journal.append("report", {"action": "place", "outcome": "reported",
                                          "report": {"status": "SUCCESS", "error_code": None,
                                                     "market_id": market.market_id, "instructions": [item],
                                                     "source": "betfair"},
                                          "transitions": [{"customer_order_ref": ref, "state": "EXECUTION_COMPLETE",
                                                           "reason": "place_report",
                                                           "fields": {"bet_id": bet, "size_matched_p": 0,
                                                                      "size_lapsed_p": 200, "size_remaining_p": 0}}]},
                               intent_id=intent_id, market_id=market.market_id)
                set_time(env.clock, off + 60 * S)
                row = {"status": "EXECUTION_COMPLETE", "sizeMatched": "0.00", "sizeCancelled": "0.00",
                       "sizeLapsed": "2.00", "sizeVoided": "0.00", "sizeRemaining": "0.00", "averagePriceMatched": None,
                       "selectionId": None, "marketId": None, "customerOrderRef": None, "customerStrategyRef": None}
                note(env, "capture_c", journal=journal, customer_order_ref=ref, bet_id=bet, raw_sha256="c" * 64,
                     row=row)
        day += 1
        day_ns += pilot.DAY_NS
    fillers = min_events - journal.anchor()["seq"]
    set_time(env.clock, day_ns - pilot.DAY_NS + 19 * 3600 * S)
    for index in range(max(0, fillers)):
        journal.append("funds_check" if index % 2 else "startup_reconcile", {"funds": funds} if index % 2 else startup)
    return journal.anchor()["seq"], day_ns


def manual_place(env, market, *, price_h=300, matched=False):
    """A live manual (cli_manual) BACK on a pilot fixture market through its own gateway, as the operator would type
    it; matched=True fills it in full at its price. -> the place result."""
    order = gw.instruction(selection_id=FAV, price_h=price_h)
    gateway = new_gateway(env)
    env.test.addCleanup(gateway.close)
    gateway.preconfirm("place", market_id=market.market_id, instructions=[order])
    confirm = expected_token(env.config, env.clock.time_ns())
    gateway.open(command="place", confirm_token=confirm)
    if matched:
        accept = env.exchange.place

        def filled(params):
            env.exchange.place_handler = None
            result = accept(params)
            for row in result["instructionReports"]:
                limit = row["instruction"]["limitOrder"]
                env.exchange.orders[row["betId"]].update(status="EXECUTION_COMPLETE", sizeMatched=limit["size"],
                                                         sizeRemaining=0.0, averagePriceMatched=limit["price"])
                row.update(orderStatus="EXECUTION_COMPLETE", sizeMatched=limit["size"],
                           averagePriceMatched=limit["price"])
            return result

        env.exchange.place_handler = filled
    result = gateway.place(market.market_id, [order], source="cli_manual", confirm_token=confirm)
    gateway.close()
    return result
