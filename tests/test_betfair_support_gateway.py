"""Shared offline fakes for the gateway-layer tests (no TestCases here). Every value is synthetic.

SimExchange is a tiny in-memory exchange: orders placed through it can be listed, cancelled and
replaced (the replace route only records a request, which no v1 live path ever sends), and tests can
add foreign orders or script failures. FakeBetfair serves it over the
scripted FakeHttpTransport (the real transport preflight and digest gate still run), so the real
SessionManager, JsonRpcClient and ReadClient are exercised; FakeReadClient serves it directly to
the pure reconcile and kill-switch functions. FakeConfirmer stands in for the terminal echo.
build_live_gateway assembles a fully armed live gateway under a temporary state root whose home is
patched in (so StatePaths is not "overridden" and A13 can pass), with no real Keychain, network or
terminal anywhere.
"""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from betangel_research.betfair import paths as paths_module
from betangel_research.betfair.api import JsonRpcClient, ReadClient
from betangel_research.betfair.arming import ACK_IDS, expected_token
from betangel_research.betfair.auth import SessionManager
from betangel_research.betfair.config import load_config
from betangel_research.betfair.credentials import anchor_write, load_credentials
from betangel_research.betfair.errors import NotPresent, OrderRefused
from betangel_research.betfair.gateway import OrderGateway
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.paths import StatePaths
from betangel_research.betfair.receipts import parse_utc_ns, utc_from_ns, utc_ms_from_ns
from betangel_research.betfair.risk import RiskGuard

import test_betfair_support as support

LIVE_KEY = "synthetic-live-key-0001"
SESSION_TOKEN = "synthetic-session-token-0001"
SELECTION = 12345
OTHER_SELECTION = 23456
PLACE_SUFFIX = "place" + "Orders"
CANCEL_SUFFIX = "cancel" + "Orders"
REPLACE_SUFFIX = "replace" + "Orders"
LIVE_OVERRIDES = {
    "coexistence": {"bet_angel": "not_running_on_account"},
    "credentials": {"login_method": "interactive", "cert_path": None, "key_path": None},
    "orders": {"live": {"armed": True, "acknowledgements": list(ACK_IDS), "operator_attests_uk_egress": True}},
}


class FakeConfirmer:
    """The confirmer protocol (I15). lines None means the operator types exactly what is asked."""

    def __init__(self, lines=None, *, present=True, clock=None, delay_s=0.0):
        self.lines = None if lines is None else list(lines)
        self.present = present
        self.clock = clock
        self.delay_s = delay_s
        self.prompts = []

    def require_present(self):
        if not self.present:
            raise NotPresent("operator_tty_required")

    def confirm_line(self, expected, *, banner):
        self.require_present()
        self.prompts.append({"expected": expected, "banner": banner})
        if self.clock is not None and self.delay_s:
            self.clock.advance(self.delay_s)
        answer = expected if self.lines is None else (self.lines.pop(0) if self.lines else None)
        if answer is None or answer.strip() != expected:
            raise OrderRefused("operator_echo_mismatch")


def raises(exc):
    """An exchange handler that raises exc (the transport fake then raises it to the caller)."""
    def handler(params):
        raise exc
    return handler


class SimExchange:
    """In-memory orders keyed by betId, in Betfair's CurrentOrderSummary shape."""

    def __init__(self, clock, *, market_id=support.MARKET_ID):
        self.clock = clock
        self.market_id = market_id
        self.orders = {}
        self.cleared = {"SETTLED": [], "VOIDED": [], "LAPSED": [], "CANCELLED": []}
        self.next_bet = 300000000001
        self.delayed = False
        self.market_status = "OPEN"
        self.version = 4249587130
        self.place_handler = None         # callable(params) -> result dict or raise; None = accept
        self.cancel_handler = None
        self.replace_handler = None
        self.read_failures = {}           # method name -> list of exceptions raised by the next calls
        self.calls = []
        # Models the case seen in the first live probe (2026-10-21): a fully unmatched bet, once cancelled, was
        # absent from later listCurrentOrders reads. Off by default, so older tests keep their model.
        self.hide_cancelled_unmatched = False
        self.timeline = []                # (operation, betIds or refs) in call order, reads and orders alike

    # --- order book of our bets --------------------------------------------------------------

    def new_bet_id(self):
        self.next_bet += 1
        return str(self.next_bet)

    def add_order(self, *, selection_id=SELECTION, side="BACK", price=3.5, size=2.0, status="EXECUTABLE",
                  matched=0.0, ref=None, strategy_ref=None, market_id=None, bet_id=None, source_key=LIVE_KEY,
                  placed_date=None, average=0.0, cancelled=0.0, lapsed=0.0):
        bet_id = bet_id or self.new_bet_id()
        remaining = 0.0 if status == "EXECUTION_COMPLETE" else round(size - matched - cancelled - lapsed, 2)
        self.orders[bet_id] = {
            "betId": bet_id, "marketId": market_id or self.market_id, "selectionId": selection_id, "handicap": 0.0,
            "priceSize": {"price": price, "size": size}, "bspLiability": 0.0, "side": side, "status": status,
            "persistenceType": "LAPSE", "orderType": "LIMIT",
            "placedDate": placed_date or utc_ms_from_ns(self.clock.time_ns()), "averagePriceMatched": average,
            "sizeMatched": matched, "sizeRemaining": remaining, "sizeLapsed": lapsed, "sizeCancelled": cancelled,
            "sizeVoided": 0.0, "regulatorCode": "SYNTHETIC", "customerOrderRef": ref,
            "customerStrategyRef": strategy_ref, "_source": source_key}
        return bet_id

    def _visible(self, order, include_source_id):
        item = {k: v for k, v in order.items() if not k.startswith("_") and v is not None}
        if include_source_id and order["_source"] is not None:
            item["sourceIdKey"] = order["_source"]
        return copy.deepcopy(item)

    def _fail(self, method):
        queue = self.read_failures.get(method) or []
        if queue:
            raise queue.pop(0)

    def current(self, **kw):
        self.calls.append(("listCurrentOrders", copy.deepcopy(kw)))
        self.timeline.append(("listCurrentOrders", tuple(kw.get("customer_order_refs") or ())))
        self._fail("listCurrentOrders")
        items = []
        for order in sorted(self.orders.values(), key=lambda o: o["betId"]):
            if (self.hide_cancelled_unmatched and order["status"] == "EXECUTION_COMPLETE"
                    and order["sizeMatched"] == 0 and order["sizeCancelled"] > 0):
                continue
            if kw.get("bet_ids") is not None and order["betId"] not in kw["bet_ids"]:
                continue
            if kw.get("market_ids") is not None and order["marketId"] not in kw["market_ids"]:
                continue
            if kw.get("customer_order_refs") is not None and order["customerOrderRef"] not in kw["customer_order_refs"]:
                continue
            if (kw.get("customer_strategy_refs") is not None
                    and order["customerStrategyRef"] not in kw["customer_strategy_refs"]):
                continue
            if kw.get("order_projection") == "EXECUTABLE" and order["status"] != "EXECUTABLE":
                continue
            if kw.get("order_projection") == "EXECUTION_COMPLETE" and order["status"] != "EXECUTION_COMPLETE":
                continue
            items.append(self._visible(order, kw.get("include_source_id")))
        return items

    def cleared_orders(self, status, **kw):
        self.calls.append(("listClearedOrders", dict(copy.deepcopy(kw), bet_status=status)))
        self._fail("listClearedOrders")
        out = []
        since = kw.get("settled_from_utc")
        for item in self.cleared[status]:
            # settledDateRange.from (inclusive) applies to rows that carry a settledDate.
            if since is not None and item.get("settledDate") is not None and (
                    parse_utc_ns(item["settledDate"]) < parse_utc_ns(since)):
                continue
            if kw.get("bet_ids") is not None and item.get("betId") not in kw["bet_ids"]:
                continue
            if kw.get("customer_order_refs") is not None and item.get("customerOrderRef") not in kw["customer_order_refs"]:
                continue
            if kw.get("market_ids") is not None and item.get("marketId") not in kw["market_ids"]:
                continue
            if (kw.get("customer_strategy_refs") is not None
                    and item.get("customerStrategyRef") not in kw["customer_strategy_refs"]):
                continue
            # Items marked _group MARKET answer groupBy=MARKET queries only; the rest are bet-level.
            if (item.get("_group") == "MARKET") != (kw.get("group_by", "BET") == "MARKET"):
                continue
            out.append({k: v for k, v in item.items() if not k.startswith("_")})
        return copy.deepcopy(out)

    # --- order operations --------------------------------------------------------------------

    def place(self, params):
        self.timeline.append(("placeOrders", tuple(i.get("customerOrderRef") for i in params.get("instructions", []))))
        if self.place_handler is not None:
            return self.place_handler(params)
        reports = []
        for instruction in params["instructions"]:
            limit = instruction["limitOrder"]
            bet_id = self.add_order(selection_id=instruction["selectionId"], side=instruction["side"],
                                    price=limit["price"], size=limit["size"], ref=instruction["customerOrderRef"],
                                    strategy_ref=params.get("customerStrategyRef"), market_id=params["marketId"])
            reports.append({"status": "SUCCESS", "instruction": copy.deepcopy(instruction), "betId": bet_id,
                            "placedDate": utc_ms_from_ns(self.clock.time_ns()), "averagePriceMatched": 0.0,
                            "sizeMatched": 0.0, "orderStatus": "EXECUTABLE"})
        return {"customerRef": params["customerRef"], "status": "SUCCESS", "marketId": params["marketId"],
                "instructionReports": reports}

    def _cancel_one(self, bet_id, reduction=None):
        order = self.orders.get(bet_id)
        if order is None or order["status"] != "EXECUTABLE":
            return {"status": "FAILURE", "errorCode": "BET_TAKEN_OR_LAPSED",
                    "instruction": {"betId": bet_id, "sizeReduction": reduction}}
        amount = order["sizeRemaining"] if reduction is None else min(reduction, order["sizeRemaining"])
        order["sizeCancelled"] = round(order["sizeCancelled"] + amount, 2)
        order["sizeRemaining"] = round(order["sizeRemaining"] - amount, 2)
        if order["sizeRemaining"] == 0:
            order["status"] = "EXECUTION_COMPLETE"
        return {"status": "SUCCESS", "instruction": {"betId": bet_id, "sizeReduction": reduction},
                "sizeCancelled": amount, "cancelledDate": utc_ms_from_ns(self.clock.time_ns())}

    def cancel(self, params):
        self.timeline.append(("cancelOrders", tuple(i.get("betId") for i in params.get("instructions", []))))
        if self.cancel_handler is not None:
            return self.cancel_handler(params)
        if "instructions" not in params:
            for bet_id, order in list(self.orders.items()):
                if order["status"] == "EXECUTABLE":
                    self._cancel_one(bet_id)
            return {"customerRef": params["customerRef"], "status": "SUCCESS", "instructionReports": []}
        reports = [self._cancel_one(i["betId"], i.get("sizeReduction")) for i in params["instructions"]]
        status = "SUCCESS" if all(r["status"] == "SUCCESS" for r in reports) else "FAILURE"
        return {"customerRef": params["customerRef"], "status": status, "marketId": params["marketId"],
                "instructionReports": reports}

    def replace(self, params):
        if self.replace_handler is not None:
            return self.replace_handler(params)
        reports = []
        for instruction in params["instructions"]:
            original = copy.deepcopy(self.orders.get(instruction["betId"]))
            cancel = self._cancel_one(instruction["betId"])
            if cancel["status"] != "SUCCESS":
                reports.append({"status": "FAILURE", "errorCode": cancel["errorCode"], "cancelInstructionReport": cancel})
                continue
            new_bet = self.add_order(selection_id=original["selectionId"], side=original["side"],
                                     price=instruction["newPrice"], size=cancel["sizeCancelled"],
                                     ref=original["customerOrderRef"], strategy_ref=original["customerStrategyRef"],
                                     market_id=params["marketId"])
            place = {"status": "SUCCESS", "instruction": {"orderType": "LIMIT", "selectionId": original["selectionId"],
                                                          "handicap": 0.0, "side": original["side"],
                                                          "limitOrder": {"size": cancel["sizeCancelled"],
                                                                         "price": instruction["newPrice"],
                                                                         "persistenceType": "LAPSE"}},
                     "betId": new_bet, "placedDate": utc_ms_from_ns(self.clock.time_ns()),
                     "averagePriceMatched": 0.0, "sizeMatched": 0.0, "orderStatus": "EXECUTABLE"}
            reports.append({"status": "SUCCESS", "cancelInstructionReport": cancel, "placeInstructionReport": place})
        status = "SUCCESS" if all(r["status"] == "SUCCESS" for r in reports) else "FAILURE"
        return {"customerRef": params["customerRef"], "status": status, "marketId": params["marketId"],
                "instructionReports": reports}

    # --- market data -------------------------------------------------------------------------

    def market_time(self):
        return utc_ms_from_ns(self.clock.time_ns() + 20 * 60 * 10**9)

    def catalogue_row(self, market_id):
        return {"marketId": market_id, "marketName": "Synthetic race", "totalMatched": 0.0,
                "marketStartTime": self.market_time(),
                "description": {"bettingType": "ODDS", "marketType": "WIN", "marketTime": self.market_time(),
                                "priceLadderDescription": {"type": "CLASSIC"}, "persistenceEnabled": True},
                "eventType": {"id": "7", "name": "Horse Racing"},
                "runners": [{"selectionId": SELECTION, "runnerName": "A", "handicap": 0.0, "sortPriority": 1},
                            {"selectionId": OTHER_SELECTION, "runnerName": "B", "handicap": 0.0, "sortPriority": 2}]}

    def book_row(self, market_id):
        def runner(selection, back, lay, status="ACTIVE"):
            return {"selectionId": selection, "handicap": 0.0, "status": status, "lastPriceTraded": back,
                    "ex": {"availableToBack": [{"price": back, "size": 100.0}, {"price": round(back - 0.05, 2),
                                                                                 "size": 40.0}],
                           "availableToLay": [{"price": lay, "size": 80.0}], "tradedVolume": []}}
        runners = [runner(SELECTION, 3.5, 3.55), runner(OTHER_SELECTION, 2.0, 2.02)]
        if self.market_status == "CLOSED":
            runners = [dict(runners[0], status="WINNER"), dict(runners[1], status="LOSER")]
        return {"marketId": market_id, "isMarketDataDelayed": self.delayed, "status": self.market_status,
                "betDelay": 0, "bspReconciled": False, "complete": True, "inplay": False, "numberOfWinners": 1,
                "numberOfRunners": 2, "numberOfActiveRunners": 2, "totalMatched": 0.0, "totalAvailable": 0.0,
                "crossMatching": True, "runnersVoidable": False, "version": self.version, "runners": runners}

    def funds(self):
        return {"availableToBetBalance": 1000.0, "exposure": 0.0, "retainedCommission": 0.0,
                "exposureLimit": -10000.0, "discountRate": 0.0, "pointsBalance": 0, "wallet": "UK"}

    def details(self):
        return {"currencyCode": "GBP", "firstName": "Firstname-never-kept", "lastName": "Lastname-never-kept",
                "localeCode": "en",
                "region": "GBR", "timezone": "GMT", "discountRate": 0.0, "pointsBalance": 0, "countryCode": "GB"}


class FakeBetfair:
    """Routes FakeHttpTransport requests to SimExchange: SSO login/keepAlive/logout and JSON-RPC."""

    def __init__(self, exchange):
        self.exchange = exchange
        self.order_requests = []          # {"method", "params", "body", "digest"} for every order call
        self.rpc_methods = []
        self.login_status = "SUCCESS"
        self.next_response = {}           # method suffix -> list of script items returned instead

    def __call__(self, record):
        url = record["url"]
        if url.endswith("/api/login") or url.endswith("/api/certlogin"):
            if self.login_status != "SUCCESS":
                return support.http_response({"token": "", "product": "x", "status": self.login_status,
                                              "error": "INVALID_USERNAME_OR_PASSWORD"})
            return support.http_response({"token": SESSION_TOKEN, "product": "synthetic", "status": "SUCCESS",
                                          "error": ""})
        if url.endswith("/api/keepAlive") or url.endswith("/api/logout"):
            return support.http_response({"token": SESSION_TOKEN, "product": "synthetic", "status": "SUCCESS",
                                          "error": ""})
        body = json.loads(record["body"])
        method, params, rpc_id = body["method"], body["params"], body["id"]
        name = method.split("/")[-1]
        self.rpc_methods.append(name)
        if name in (PLACE_SUFFIX, CANCEL_SUFFIX, REPLACE_SUFFIX):
            self.order_requests.append({"method": method, "params": copy.deepcopy(params), "body": record["body"],
                                        "digest": record["order_digest"]})
        scripted = self.next_response.get(name)
        if scripted:
            item = scripted.pop(0)
            return item(record) if callable(item) else item
        exchange = self.exchange
        try:
            if name == "listEventTypes":
                result = [{"eventType": {"id": "7", "name": "Horse Racing"}, "marketCount": 3}]
            elif name == "listMarketCatalogue":
                ids = params["filter"].get("marketIds") or [exchange.market_id]
                result = [exchange.catalogue_row(m) for m in ids]
            elif name == "listMarketBook":
                result = [exchange.book_row(m) for m in params["marketIds"]]
            elif name == "getAccountDetails":
                result = exchange.details()
            elif name == "getAccountFunds":
                result = exchange.funds()
            elif name == "listCurrentOrders":
                kw = {"bet_ids": params.get("betIds"), "market_ids": params.get("marketIds"),
                      "customer_order_refs": params.get("customerOrderRefs"),
                      "customer_strategy_refs": params.get("customerStrategyRefs"),
                      "order_projection": params.get("orderProjection"),
                      "include_source_id": params.get("includeSourceId", False)}
                items = exchange.current(**kw)
                result = {"currentOrders": items, "moreAvailable": False}
            elif name == "listClearedOrders":
                kw = {"bet_ids": params.get("betIds"), "market_ids": params.get("marketIds"),
                      "customer_order_refs": params.get("customerOrderRefs"),
                      "customer_strategy_refs": params.get("customerStrategyRefs"),
                      "group_by": params.get("groupBy", "BET"),
                      "settled_from_utc": (params.get("settledDateRange") or {}).get("from")}
                result = {"clearedOrders": exchange.cleared_orders(params["betStatus"], **kw), "moreAvailable": False}
            elif name == PLACE_SUFFIX:
                result = exchange.place(params)
            elif name == CANCEL_SUFFIX:
                result = exchange.cancel(params)
            elif name == REPLACE_SUFFIX:
                result = exchange.replace(params)
            else:
                return support.rpc_error_response(-32601, "method not found", rpc_id=rpc_id)
        except BaseException as exc:          # scripted exceptions travel as transport-level raises
            return exc
        return support.rpc_response(result, rpc_id)


class RoutingTransport(support.FakeHttpTransport):
    """FakeHttpTransport whose every request is answered by one router callable (preflight still runs)."""

    def __init__(self, router, **kwargs):
        super().__init__(**kwargs)
        self.router = router

    def request(self, method, url, headers, body, *, deadline_s, idempotent, tls_context=None, order_digest=None,
                send_by_monotonic_ns=None, send_gate=None):
        self.responses = [self.router]
        return super().request(method, url, headers, body, deadline_s=deadline_s, idempotent=idempotent,
                               tls_context=tls_context, order_digest=order_digest,
                               send_by_monotonic_ns=send_by_monotonic_ns, send_gate=send_gate)


class FakeReadClient:
    """The ReadClient methods the pure reconcile and kill-switch functions use, over a SimExchange."""

    def __init__(self, exchange, *, app_key_slot="live", rpc=None):
        self.exchange = exchange
        self.app_key_slot = app_key_slot
        self.key_type = "live" if app_key_slot == "live" else "delayed"
        self.rpc = rpc

    def iter_current_orders(self, **kw):
        return iter(self.exchange.current(**kw))

    def list_current_orders(self, **kw):
        return {"currentOrders": self.exchange.current(**kw), "moreAvailable": False}

    def iter_cleared_orders(self, bet_status, **kw):
        return iter(self.exchange.cleared_orders(bet_status, **kw))

    def list_cleared_orders(self, bet_status, **kw):
        return {"clearedOrders": self.exchange.cleared_orders(bet_status, **kw), "moreAvailable": False}

    def list_market_book(self, market_ids, price_data=("EX_BEST_OFFERS",), **kw):
        self.exchange.calls.append(("listMarketBook", list(market_ids)))
        self.exchange._fail("listMarketBook")
        return [self.exchange.book_row(m) for m in market_ids]

    def get_account_funds(self):
        return self.exchange.funds()

    def get_account_details(self):
        return {k: self.exchange.details()[k] for k in ("currencyCode", "countryCode", "discountRate", "pointsBalance")}


def unoverridden_paths(test, *, profile="default"):
    """StatePaths that are not "overridden": the home lookup is patched to a temporary directory."""
    support.block_network(test)
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    home = Path(temp.name).resolve() / "home"
    home.mkdir()
    patcher = patch.object(paths_module, "account_home", lambda: home)
    patcher.start()
    test.addCleanup(patcher.stop)
    paths = StatePaths(profile=profile)
    paths.ensure()
    return paths


def live_config(paths, overrides=None):
    support.write_test_config(paths, support.deep_merge(LIVE_OVERRIDES, overrides or {}))
    return load_config(paths.config_path, paths=paths)


def build_live_gateway(test, *, overrides=None, roundtrip=True, confirmer=None, clock=None, keychain_items=None,
                       env=None, profile="default"):
    """A fully armed live gateway over SimExchange (not yet opened). Returns a namespace of all parts."""
    paths = unoverridden_paths(test, profile=profile)
    config = live_config(paths, overrides)
    clock = clock or support.FakeClock()
    runner = support.FakeKeychainRunner(items=keychain_items)
    exchange = SimExchange(clock)
    router = FakeBetfair(exchange)
    transport = RoutingTransport(router, clock=clock)
    credentials = load_credentials(config, purpose="live_orders", profile=paths.profile, runner=runner)
    session = SessionManager(config, credentials, paths=paths, transport=transport, clock=clock)
    rpc = JsonRpcClient(config, session, clock=clock)
    read = ReadClient(rpc, paths=paths, clock=clock)
    journal = OrderJournal(paths.journal_path("live"), mode="live", clock=clock)
    test.addCleanup(journal.close)
    if roundtrip:
        journal.append("ref_roundtrip_verified", {"customer_order_ref_found": True})
        anchor_write(paths.profile, dict(journal.anchor(), updated_at=clock.utc_now()), runner=runner)
    guard = RiskGuard(config, mode="live", clock=clock, paths=paths)
    confirmer = confirmer if confirmer is not None else FakeConfirmer()
    gateway = OrderGateway(config, paths=paths, mode="live", journal=journal, guard=guard, rpc=rpc, read=read,
                           confirmer=confirmer, clock=clock, env=env if env is not None else {},
                           keychain_runner=runner)
    token = expected_token(config, clock.time_ns())
    return SimpleNamespace(paths=paths, config=config, clock=clock, runner=runner, exchange=exchange, router=router,
                           transport=transport, session=session, rpc=rpc, read=read, journal=journal, guard=guard,
                           confirmer=confirmer, gateway=gateway, token=token)


def open_live(test, env, command="place", *, instructions=None, selection_id=None, accept_ref=None,
              market_id="1.1", token=None, settle=False):
    """Type the operator lines (as the CLI does, before the lock), then open env.gateway for command.

    Nothing is typed when no lines are given (settle=True types settle-market's line for market_id).
    The gateway is closed at cleanup (close is idempotent)."""
    if instructions is not None or selection_id is not None or accept_ref is not None or settle:
        env.gateway.preconfirm(command, market_id=market_id, instructions=instructions, selection_id=selection_id,
                               accept_ref=accept_ref)
    report = env.gateway.open(command=command, confirm_token=env.token if token is None else token)
    test.addCleanup(env.gateway.close)
    return report


def build_paper_gateway(test, *, overrides=None, clock=None):
    """A paper gateway (opened) on a temporary state root; the book comes from the caller."""
    support.block_network(test)
    paths = support.temp_state_paths(test)
    support.write_test_config(paths, overrides)
    config = load_config(paths.config_path, paths=paths)
    clock = clock or support.FakeClock()
    journal = OrderJournal(paths.journal_path("paper"), mode="paper", clock=clock)
    test.addCleanup(journal.close)
    guard = RiskGuard(config, mode="paper", clock=clock, paths=paths)
    gateway = OrderGateway(config, paths=paths, mode="paper", journal=journal, guard=guard, clock=clock, env={})
    return SimpleNamespace(paths=paths, config=config, clock=clock, journal=journal, guard=guard, gateway=gateway)


def paper_book(clock, **changes):
    """A paper book snapshot for market 1.1 (two runners) observed now."""
    from test_betfair_support_orders import runner, snapshot
    value = snapshot(observed_at=clock.utc_now(), monotonic_ns=clock.mono_ns, pt=None,
                     runners={str(SELECTION): runner(), str(OTHER_SELECTION): runner(back=((2.0, 50.0),),
                                                                                         lay=((2.02, 50.0),),
                                                                                         ltp=2.0)})
    value.update(copy.deepcopy(changes))
    return value


def instruction(**changes):
    value = {"selection_id": SELECTION, "handicap": 0, "side": "BACK", "price_h": 350, "size_p": 200,
             "persistence_type": "LAPSE"}
    value.update(changes)
    return value


def order_items(journal, kind=None):
    return [e for e in journal.events() if kind is None or e["kind"] == kind]


def now_utc(clock):
    return utc_from_ns(clock.time_ns())
