"""Pilot 2's gateway path (OrderGateway with lp: lp_place, own-leg cancels, the kill's extra ref), offline, synthetic.

These drive the real OrderGateway, journal, risk guard, JSON-RPC and read clients over the simulated exchange
(test_betfair_support_gateway) with Pilot 1's per-market fixtures (test_betfair_support_pilot's PilotBetfair and
Market), on config-p2.json beside the main config.json, and show: the Pilot 2 context refused in paper and with
Pilot 1's config or context; place() refused on it and the typed place unchanged; the ARM record each leg needs; the
request body; a choice code journaling only funds_check; virtual prices on its books only; the pair rules on the
submit's own basis read; the pair pre-check (R1, R3, R10's room) and R5, R8 and R10 after settle-market; the race
counts; own-leg cancels and a partial fill at the pull; the kill's extra ref on an untrusted journal; that a leg whose
send or report raised is still listed; the screened start and country; and the three-UNKNOWN HALT after a round trip.
No /usr/bin/security, git or other child process runs, and nothing opens the network. They do not show Betfair's real
behaviour, the executor (p2_run) or the command line (test_betfair_p2_cli).
"""

import copy
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from betangel_research.betfair import api, p2_lp, pilot
from betangel_research.betfair import config as cfg
from betangel_research.betfair.arming import expected_token
from betangel_research.betfair.credentials import anchor_write
from betangel_research.betfair.errors import ConfigError, JournalError, OrderRefused
from betangel_research.betfair.gateway import UNTRUSTED_SWEEP_NOTE, OrderGateway
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.receipts import utc_from_ns
from betangel_research.betfair.risk import RiskGuard

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp

P2_EXAMPLE = support.REPO_ROOT / "research" / "betfair-config-p2.example.json"
S = p2_lp.NS
OFF = sp.OFF                                      # 2027-01-04 10:00:00Z
UNTIL = sp.UNTIL                                  # 20:00:00Z
START = OFF - 720 * S                             # the ARM record and any manual bet; the BACK goes at OFF - 590 s
NEXT_DAY_NS = sp.DAY_NS + pilot.DAY_NS
SESSION = "p2s-0a1b2c3d"
MARKET = "1.234"
FAV = sp.FAV                                      # runner 11, the one the runner rule takes
set_time = sp.set_time


def fake_git(test):
    """The worktree checks (config paths, A13's state root) answer "not inside" without running git."""
    for target in ("betangel_research.betfair.paths.is_inside_git_worktree",
                   "betangel_research.betfair.arming.is_inside_git_worktree"):
        patcher = patch(target, return_value=False)
        patcher.start()
        test.addCleanup(patcher.stop)


def no_children(test):
    """From here on no child process at all may start (after block_network, whose guard runs all but security)."""
    def refused(*args, **kwargs):
        raise support.OfflineViolation("a child process was started in a Pilot 2 test")

    for patcher in (patch.object(subprocess, "run", refused), patch.object(subprocess, "Popen", refused)):
        patcher.start()
        test.addCleanup(patcher.stop)


def p2_orders():
    """config-p2's orders values: the example's ref, caps and rules."""
    orders = json.loads(P2_EXAMPLE.read_text(encoding="utf-8"))["orders"]
    return {"orders": {key: orders[key] for key in ("customer_strategy_ref", "caps", "rules")}}


def build(test, *, start_ns=START, arm=True, until_ns=UNTIL, races=5):
    """gw.build_live_gateway's armed parts (env.gateway and env.config: the main config.json, for manual bets and
    settle-market) at start_ns, served by PilotBetfair, plus env.p2 (config-p2.json, loaded by load_config_p2), its
    risk guard and, with arm, p2-run's ARM record (env.arm_seq)."""
    env = gw.build_live_gateway(test, clock=support.FakeClock(utc_from_ns(start_ns)))
    no_children(test)
    env.test = test
    env.router = sp.PilotBetfair(env.exchange)
    env.transport.router = env.router
    support.write_test_config(env.paths, support.deep_merge(gw.LIVE_OVERRIDES, p2_orders()),
                              path=env.paths.config_p2_path)
    env.p2 = cfg.load_config_p2(paths=env.paths)
    test.assertEqual(p2_lp.config_problems(env.p2), [])
    env.p2_guard = RiskGuard(env.p2, mode="live", clock=env.clock, paths=env.paths)
    env.arm_seq = arm_record(env, until_ns=until_ns, races=races) if arm else None
    return env


def arm_record(env, *, until_ns=UNTIL, races=5, session_id=SESSION, config_sha256=None, day=None):
    """p2-run's ARM record (operator_confirm), journaled now -> its seq."""
    day = day or pilot.utc_day(env.clock.time_ns())
    sha = config_sha256 or cfg.config_sha256(env.p2)
    line = p2_lp.arm_line(sha[:12], day, utc_from_ns(until_ns)[11:16].replace(":", ""), races)
    payload = p2_lp.arm_payload(line, session_id=session_id, config_sha256=sha, utc_day=day, until_ns=until_ns,
                                races=races, code_commit="c" * 40)
    return env.journal.append("operator_confirm", payload)["seq"]


def lp_of(env, seq=None, session_id=SESSION):
    return {"arm_seq": env.arm_seq if seq is None else seq, "session_id": session_id, "races": 5}


def p2_token(env):
    return expected_token(env.p2, env.clock.time_ns())


def gateway_for(env, config, *, guard=None, lp=None):
    return OrderGateway(config, paths=env.paths, mode="live", journal=env.journal, guard=guard or env.guard,
                        rpc=env.rpc, read=env.read, confirmer=env.confirmer, clock=env.clock, env={},
                        keychain_runner=env.runner, lp=lp)


def open_p2(env, lp=None):
    """A Pilot 2 gateway opened for place on config-p2 (closed at cleanup)."""
    gateway = gateway_for(env, env.p2, guard=env.p2_guard, lp=lp_of(env) if lp is None else lp)
    report = gateway.open(command="place", confirm_token=p2_token(env))
    env.test.addCleanup(gateway.close)
    env.test.assertTrue(report["armed"], report["failures"])
    return gateway


def race(env, market_id=MARKET, *, off_ns=OFF, country="GB", back="3.00", lay="3.15", store=True):
    """One race whose runner 11 is at back/lay (the others are out of band or one tick wide), and, with store,
    the fresh catalogue row p2-run's screen stores (without it the send book's row is refetched without EVENT)."""
    backs, lays = copy.deepcopy(sp.BASE_BACKS), copy.deepcopy(sp.BASE_LAYS)
    backs[FAV], lays[FAV] = ((back, "15.00"),), ((lay, "20.00"),)
    market = sp.Market(market_id, off_ns, country=country, backs=backs, lays=lays)
    env.router.markets[market_id] = market
    if store:
        screen_row(env, market)
    return market


def screen_row(env, market):
    env.read.store_catalogue_row(market.market_id,
                                 json.loads(support.dumps(market.row(set(api.PILOT_ROW_PROJECTIONS)))))


def leg(env, gateway, side, market_id=MARKET):
    return gateway.lp_place(market_id, side, confirm_token=p2_token(env))


def events(env):
    return list(env.journal.events())


def kinds_after(env, seq):
    return [event["kind"] for event in events(env) if event["seq"] > seq]


def head(env):
    return env.journal.anchor()["seq"]


def placed(env):
    return [request for request in env.router.order_requests if request["method"].endswith(gw.PLACE_SUFFIX)]


def cancels(env):
    return [request for request in env.router.order_requests if request["method"].endswith(gw.CANCEL_SUFFIX)]


def fold(env):
    return env.journal.fold(now_ns=env.clock.time_ns())


def rule(result):
    return (result["decision"], (result["refusal"] or {}).get("rule"))


def states(result):
    return [order["state"] for order in result["orders"]]


def fill(env):
    """The exchange matches every order placed from now on in full, at its price."""
    def filled(params):
        env.exchange.place_handler = None
        try:
            result = env.exchange.place(params)
        finally:
            env.exchange.place_handler = filled
        for row in result["instructionReports"]:
            limit = row["instruction"]["limitOrder"]
            env.exchange.orders[row["betId"]].update(status="EXECUTION_COMPLETE", sizeMatched=limit["size"],
                                                     sizeRemaining=0.0, averagePriceMatched=limit["price"])
            row.update(orderStatus="EXECUTION_COMPLETE", sizeMatched=limit["size"],
                       averagePriceMatched=limit["price"])
        return result

    env.exchange.place_handler = filled


def refuse_at_exchange(params, code="MARKET_NOT_OPEN_FOR_BETTING"):
    """An exchange answer refusing the one instruction (FAILED, nothing rests)."""
    return {"customerRef": params["customerRef"], "status": "FAILURE", "errorCode": code,
            "marketId": params["marketId"],
            "instructionReports": [{"status": "FAILURE", "errorCode": code, "instruction": params["instructions"][0]}]}


def manual(env, market_id="1.1", *, cancel=False):
    """A typed manual BACK under the main config (ref bar-manual) through its own gateway; cancel: pulled in that
    open. -> the place result."""
    gateway = gateway_for(env, env.config)
    order = gw.instruction()
    token = expected_token(env.config, env.clock.time_ns())
    gateway.preconfirm("place", market_id=market_id, instructions=[order])
    gateway.open(command="place", confirm_token=token)
    try:
        result = gateway.place(market_id, [order], source="cli_manual", confirm_token=token)
        if cancel:
            gateway.cancel(market_id, [result["orders"][0]["bet_id"]], source="cli_manual")
    finally:
        gateway.close()
    return result


def settle(env, market_id):
    """Yu's settle-market of one race once it is CLOSED and Betfair no longer lists its bets (main config)."""
    env.router.markets[market_id].status = "CLOSED"
    for bet_id in [bet for bet, order in env.exchange.orders.items() if order["marketId"] == market_id]:
        del env.exchange.orders[bet_id]
    gateway = gateway_for(env, env.config)
    token = expected_token(env.config, env.clock.time_ns())
    gateway.preconfirm("settle-market", market_id=market_id)
    gateway.open(command="settle-market", confirm_token=token)
    try:
        return gateway.settle_market(market_id, confirm_token=token)
    finally:
        gateway.close()


def session_start(env, *, until_ns):
    """A Pilot 1 session_start note today (contract p1)."""
    return env.journal.append("note", pilot.pilot_note(
        "session_start", sp.C.sha256, session_id=sp.SESSION, registration_receipt_sha256="d" * 64,
        utc_day=pilot.utc_day(env.clock.time_ns()), until_ns=until_ns))


class P2Case(unittest.TestCase):
    def setUp(self):
        fake_git(self)

    def back_placed(self, env, gateway, market_id=MARKET):
        result = leg(env, gateway, "BACK", market_id)
        self.assertEqual((rule(result), states(result)), (("accepted", None), ["EXECUTABLE"]))
        return result


class Context(P2Case):
    def test_lp_refused_in_paper_and_with_pilot_1_config(self):
        lp = {"arm_seq": 2, "session_id": SESSION, "races": 1}
        paper = gw.build_paper_gateway(self)
        no_children(self)
        with self.assertRaisesRegex(ConfigError, "paper_gateway_must_not_hold_lp"):
            OrderGateway(paper.config, paths=paper.paths, mode="paper", journal=paper.journal, guard=paper.guard,
                         clock=paper.clock, env={}, lp=lp)
        with self.assertRaisesRegex(OrderRefused, "pilot_lp_source_live_only"):
            paper.gateway.place("1.1", [gw.instruction()], book=gw.paper_book(paper.clock), source="pilot_lp")
        intent = {"order": gw.instruction(), "customer_strategy_ref": "p2-lp", "is_probe": False, "parent_ref": None,
                  "provisional_child": False, "source": "pilot_lp"}
        with self.assertRaisesRegex(JournalError, "pilot_intent_in_paper_journal"):
            paper.journal.append("intent", intent, intent_id="i-1", customer_order_ref="ref-1", customer_ref="c-1",
                                 market_id="1.1", state_after="RECORDED")
        self.assertEqual(list(paper.journal.events()), [])
        env = build(self)
        loosened = copy.deepcopy(env.p2)
        loosened["orders"]["caps"]["max_market_exposure"] = 20.0
        for name, config in (("pilot_1_config", env.config), ("a_pin_loosened", loosened)):
            with self.subTest(config=name), self.assertRaisesRegex(ConfigError, "p2_gateway_needs_config_p2"):
                gateway_for(env, config, lp=lp)
        # A Pilot 1 context refuses it even on config-p2 (every part shares the pilot's observed clock, so the
        # observed-clock check passes first).
        pilot_env = sp.build_pilot_gateway(self, register=False, arm=False)
        no_children(self)
        with self.assertRaisesRegex(ConfigError, "p2_gateway_needs_config_p2"):
            OrderGateway(env.p2, paths=pilot_env.paths, mode="live", journal=pilot_env.journal, guard=pilot_env.guard,
                         rpc=pilot_env.rpc, read=pilot_env.read, confirmer=pilot_env.confirmer,
                         clock=pilot_env.gateway.clock, env={}, keychain_runner=pilot_env.runner, pilot=pilot_env.ctx,
                         lp=lp)
        self.assertEqual(gateway_for(env, env.p2, guard=env.p2_guard, lp=lp).lp_legs(), {})      # config-p2: built

    def test_place_refused_on_lp_gateway(self):
        env = build(self)
        race(env)
        gateway = open_p2(env)
        seq = head(env)
        with self.assertRaisesRegex(OrderRefused, "p2_gateway_refuses_place"):
            gateway.place(MARKET, [gw.instruction(selection_id=FAV, price_h=310)], source="cli_manual",
                          confirm_token=p2_token(env))
        self.assertEqual((head(env), placed(env), env.confirmer.prompts), (seq, [], []))
        gateway.close()
        # The source only through lp_place, which needs the context.
        plain = gateway_for(env, env.p2, guard=env.p2_guard)
        plain.open(command="place", confirm_token=p2_token(env))
        self.addCleanup(plain.close)
        seq = head(env)
        with self.assertRaisesRegex(OrderRefused, "pilot_lp_source_needs_lp_path"):
            plain.place(MARKET, [gw.instruction(selection_id=FAV, price_h=310)], source="pilot_lp",
                        confirm_token=p2_token(env))
        with self.assertRaisesRegex(OrderRefused, "lp_place_needs_p2_gateway"):
            plain.lp_place(MARKET, "BACK", confirm_token=p2_token(env))
        self.assertEqual((head(env), placed(env)), (seq, []))

    def test_plain_place_still_needs_typed_line(self):
        env = build(self)
        env.gateway.open(command="place", confirm_token=env.token)
        with self.assertRaisesRegex(OrderRefused, "operator_confirmation_missing"):
            env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        env.gateway.close()
        self.assertEqual(placed(env), [])
        result = manual(env)
        self.assertEqual((rule(result), states(result)), (("accepted", None), ["EXECUTABLE"]))
        typed = [event["payload"] for event in events(env) if event["kind"] == "operator_confirm"
                 and "p2_arm" not in event["payload"]]
        self.assertEqual(typed, [{"echo": env.confirmer.prompts[0]["expected"], "typed_before_lock": True}])
        (intent,) = [event["payload"] for event in events(env) if event["kind"] == "intent"]
        self.assertEqual((intent["source"], intent["customer_strategy_ref"]), ("cli_manual", "bar-manual"))
        self.assertEqual(placed(env)[0]["params"]["customerStrategyRef"], "bar-manual")


class Legs(P2Case):
    def test_lp_place_needs_arm_record(self):
        cases = {"p2_arm_missing": {"seq": 1}, "p2_arm_other_config": {"config_sha256": "e" * 64},
                 "p2_arm_other_session": {"session_id": "p2s-ffffffff"}, "p2_arm_not_today": {"day": "2027-01-03"},
                 "p2_arm_expired": {"until_ns": OFF - 595 * S}}
        for code, case in cases.items():
            with self.subTest(code=code):
                env = build(self, arm=False)
                seq = arm_record(env, until_ns=case.get("until_ns", UNTIL), config_sha256=case.get("config_sha256"),
                                 day=case.get("day"))
                race(env)
                gateway = open_p2(env, lp_of(env, case.get("seq", seq), case.get("session_id", SESSION)))
                set_time(env.clock, OFF - 590 * S)
                before = head(env)
                self.assertEqual(rule(leg(env, gateway, "BACK")), ("refused", code))
                self.assertEqual((kinds_after(env, before), placed(env), gateway.lp_legs()), (["funds_check"], [], {}))

    def test_body_one_instruction_lapse_p2_ref_version(self):
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, gateway)
        lay = leg(env, gateway, "LAY")
        self.assertEqual((rule(lay), states(lay)), (("accepted", None), ["EXECUTABLE"]))
        bodies = [request["params"] for request in placed(env)]
        self.assertEqual(len(bodies), 2)
        for params, (side, price) in zip(bodies, (("BACK", 3.1), ("LAY", 3.05))):
            with self.subTest(side=side):
                self.assertEqual({key: params[key] for key in ("marketId", "customerStrategyRef", "marketVersion")},
                                 {"marketId": MARKET, "customerStrategyRef": "p2-lp", "marketVersion": {"version": 1}})
                (instruction,) = params["instructions"]
                self.assertEqual({key: value for key, value in instruction.items() if key != "customerOrderRef"},
                                 {"orderType": "LIMIT", "selectionId": FAV, "handicap": 0, "side": side,
                                  "limitOrder": {"size": 2.0, "price": price, "persistenceType": "LAPSE"}})
        intents = [event for event in events(env) if event["kind"] == "intent"]
        self.assertEqual([(e["payload"]["source"], e["payload"]["customer_strategy_ref"], e["payload"]["order"]["side"])
                          for e in intents], [("pilot_lp", "p2-lp", "BACK"), ("pilot_lp", "p2-lp", "LAY")])
        self.assertEqual(set(gateway.lp_legs()), {event["customer_order_ref"] for event in intents})
        # No typed line per order: the session's one operator_confirm is the ARM record.
        self.assertEqual([e["seq"] for e in events(env) if e["kind"] == "operator_confirm"], [env.arm_seq])
        self.assertEqual(env.confirmer.prompts, [])

    def test_code_refusal_journals_only_funds(self):
        env = build(self)
        race(env, lay="3.05")                      # runner 11 one tick wide: no eligible runner
        gateway = open_p2(env)
        for at_ns, code in ((OFF - 600 * S, "p2_window"), (OFF - 590 * S, "spread_out_of_band")):
            with self.subTest(code=code):
                set_time(env.clock, at_ns)
                before = head(env)
                result = leg(env, gateway, "BACK")
                self.assertEqual((rule(result), result["intent_id"], result["orders"]), (("refused", code), None, []))
                self.assertEqual(kinds_after(env, before), ["funds_check"])
        self.assertEqual((placed(env), gateway.lp_legs()), ([], {}))

    def test_virtualise_only_on_lp_books(self):
        env = build(self)
        manual(env, cancel=True)                   # a typed place's own book, before the session's legs
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, gateway)
        books = env.router.reads_of(sp.BOOK)
        projections = {market_id: [read["params"]["priceProjection"] for read in books
                                   if read["params"]["marketIds"] == [market_id]] for market_id in ("1.1", MARKET)}
        self.assertEqual(projections["1.1"], [{"priceData": ["EX_BEST_OFFERS", "EX_TRADED"]}])
        self.assertEqual(projections[MARKET],
                         [{"priceData": ["EX_BEST_OFFERS", "EX_TRADED"], "virtualise": True}])
        self.assertEqual({read["slot"] for read in books}, {"live"})
        # The read client adds the key only when asked, and refuses anything but a bool before any request.
        env.read.list_market_book([MARKET])
        env.read.list_market_book([MARKET], virtualise=True)
        self.assertEqual([read["params"]["priceProjection"] for read in env.router.reads_of(sp.BOOK)[-2:]],
                         [{"priceData": ["EX_BEST_OFFERS"]}, {"priceData": ["EX_BEST_OFFERS"], "virtualise": True}])
        count = len(env.router.reads)
        with self.assertRaisesRegex(ValueError, "virtualise must be a boolean"):
            env.read.list_market_book([MARKET], virtualise=1)
        self.assertEqual(len(env.router.reads), count)

    def test_lp_check_runs_on_submit_basis(self):
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        original = gateway._lp_check

        def check(*args, **kwargs):
            code = original(*args, **kwargs)
            sp.other_writer(env).append("note", {"reason": "another_writer_after_the_check"})
            return code

        gateway._lp_check = check
        result = leg(env, gateway, "BACK")
        self.assertEqual((rule(result), states(result)), (("refused", "journal_changed_since_decision"), ["REFUSED"]))
        self.assertEqual(placed(env), [])
        self.assertEqual([order["state"] for order in gateway.lp_legs().values()], ["REFUSED"])
        gateway._lp_check = original
        self.assertEqual(rule(leg(env, gateway, "BACK")), ("refused", "p2_market_used"))
        self.assertEqual(placed(env), [])

    def test_second_back_and_lay_first_refused(self):
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        before = head(env)
        self.assertEqual(rule(leg(env, gateway, "LAY")), ("refused", "p2_lay_without_back"))
        self.assertEqual(kinds_after(env, before), ["funds_check"])
        self.back_placed(env, gateway)
        self.assertEqual(rule(leg(env, gateway, "BACK")), ("refused", "p2_market_used"))
        self.assertEqual(len(placed(env)), 1)

    def test_lay_needs_placed_back_same_open(self):
        env = build(self)
        race(env)
        first = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, first)
        first.close()
        second = open_p2(env)
        self.assertEqual(rule(leg(env, second, "LAY")), ("refused", "p2_lay_without_back"))
        # A BACK the exchange refused is no pair either.
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        env.exchange.place_handler = refuse_at_exchange
        back = leg(env, gateway, "BACK")
        self.assertEqual((rule(back), states(back)), (("accepted", None), ["FAILED"]))
        env.exchange.place_handler = None
        self.assertEqual(rule(leg(env, gateway, "LAY")), ("refused", "p2_lay_without_back"))
        self.assertEqual(len(placed(env)), 1)

    def test_other_bet_open_refuses_back(self):
        env = build(self)
        manual(env)                                # a manual BACK resting in 1.1
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        before = head(env)
        self.assertEqual(rule(leg(env, gateway, "BACK")), ("refused", "p2_other_bet_open"))
        self.assertEqual((kinds_after(env, before), len(placed(env))), (["funds_check"], 1))

    def test_pilot_1_day_and_window_refuse(self):
        env = build(self)
        session_start(env, until_ns=env.clock.time_ns() - 60 * S)          # today's Pilot 1 session, ended
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.assertEqual(rule(leg(env, gateway, "BACK")), ("refused", "p2_pilot_1_day"))
        # A Pilot 1 window still open refuses the open, and a leg once one starts after the open.
        env = build(self)
        session_start(env, until_ns=UNTIL)
        with self.assertRaisesRegex(OrderRefused, "manual_order_during_pilot_session"):
            gateway_for(env, env.p2, guard=env.p2_guard, lp=lp_of(env)).open(command="place",
                                                                                confirm_token=p2_token(env))
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        session_start(env, until_ns=UNTIL)
        with self.assertRaisesRegex(OrderRefused, "manual_order_during_pilot_session"):
            leg(env, gateway, "BACK")
        self.assertEqual((placed(env), gateway.lp_legs()), ([], {}))


class Limits(P2Case):
    def test_pair_precheck_r10_room_and_r3(self):
        # R1: a KILL file after the open.
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        engage(env.paths, "drill", clock=env.clock)
        self.assertEqual(rule(leg(env, gateway, "BACK")), ("refused", "p2_pair_precheck:halted"))
        # R10's room: a race settled at £10 leaves exactly £10 of the day, which the next BACK may take.
        env = build(self)
        race(env, back="5.9", lay="6.4")
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        back = self.back_placed(env, gateway)
        fill(env)
        lay = leg(env, gateway, "LAY")
        self.assertEqual(states(lay), ["EXECUTION_COMPLETE"])
        env.exchange.place_handler = None
        gateway.cancel(MARKET, [back["orders"][0]["bet_id"]], source="cli_manual")
        gateway.close()
        self.assertEqual(settle(env, MARKET)["booked_realized_loss_pence"], 1000)
        race(env, "1.235", off_ns=OFF + 600 * S)
        set_time(env.clock, OFF + 10 * S)
        self.back_placed(env, open_p2(env), "1.235")
        # R3: this open's own leg left unresolved (its outcome unknown and not found) blocks a BACK elsewhere.
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("receive")]
        lost = leg(env, gateway, "BACK")
        self.assertEqual(states(lost), ["ABSENT_UNCONFIRMED"])
        race(env, "1.235", off_ns=env.clock.time_ns() + 585 * S)
        self.assertEqual(rule(leg(env, gateway, "BACK", "1.235")), ("refused", "p2_pair_precheck:unresolved_orders"))
        self.assertEqual(len(placed(env)), 1)

    def test_r5_lay_600_passes_620_refused(self):
        env = build(self)
        race(env, back="5.9", lay="6.4")
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, gateway)
        lay = leg(env, gateway, "LAY")
        self.assertEqual((rule(lay), states(lay)), (("accepted", None), ["EXECUTABLE"]))
        self.assertEqual(placed(env)[-1]["params"]["instructions"][0]["limitOrder"]["price"], 6.0)
        gateway.close()
        # Config-p2's R5 refuses a £2 LAY at 6.2 (£10.40 liability), here as a typed order on a clean market.
        race(env, "1.235", back="5.9", lay="6.4")
        set_time(env.clock, OFF - 520 * S)
        order = gw.instruction(selection_id=FAV, side="LAY", price_h=620)
        plain = gateway_for(env, env.p2, guard=env.p2_guard)
        plain.preconfirm("place", market_id="1.235", instructions=[order])
        plain.open(command="place", confirm_token=p2_token(env))
        self.addCleanup(plain.close)
        refused = plain.place("1.235", [order], source="cli_manual", confirm_token=p2_token(env))
        self.assertEqual(rule(refused), ("refused", "stake_cap"))
        self.assertEqual(len(placed(env)), 2)

    def test_r8_pair_worst_1000(self):
        env = build(self)
        state = fold(env)
        now = env.clock.time_ns()
        # The pair pre-check's £10 BACK fits R8's £10 exactly; a penny more does not.
        self.assertEqual(env.p2_guard.evaluate_pilot_reservation(MARKET, state=state, now_ns=now,
                                                                 size_p=p2_lp.PAIR_WORST_P), [])
        self.assertEqual([failure["rule"] for failure in env.p2_guard.evaluate_pilot_reservation(
            MARKET, state=state, now_ns=now, size_p=p2_lp.PAIR_WORST_P + 1)], ["market_exposure"])
        race(env, back="5.9", lay="6.4")
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, gateway)
        self.assertEqual(states(leg(env, gateway, "LAY")), ["EXECUTABLE"])
        # No hedge credit: the race's worst case is the LAY's £10, the BACK's win not set against it.
        worst = [event["payload"]["worst_case"]["market_pence"] for event in events(env)
                 if event["kind"] == "risk_decision" and event["payload"]["approved"]]
        self.assertEqual(worst, [200, 1000])

    def test_r10_with_settle_market_books_1200(self):
        env = build(self)
        race(env, back="5.9", lay="6.4")
        fill(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        results = [leg(env, gateway, side) for side in p2_lp.SIDES]
        self.assertEqual([states(result) for result in results], [["EXECUTION_COMPLETE"]] * 2)
        gateway.close()
        env.exchange.place_handler = None
        self.assertEqual(settle(env, MARKET)["booked_realized_loss_pence"], 1200)
        self.assertEqual(fold(env)["realized_today_pence"], -1200)
        # The session's next race: £8 of the day is left, under the pair's £10.
        race(env, "1.235", off_ns=OFF + 600 * S)
        set_time(env.clock, OFF + 10 * S)
        gateway = open_p2(env)
        self.assertEqual(rule(leg(env, gateway, "BACK", "1.235")), ("refused", "p2_pair_precheck:daily_loss"))
        self.assertEqual(len(placed(env)), 2)

    def test_sixth_today_and_eleventh_ever_refused(self):
        env = build(self)

        def race_at(index, day_ns):
            off = day_ns + 10 * 3600 * S + index * 600 * S
            market = race(env, f"1.3{index:02d}{day_ns // pilot.DAY_NS % 100:02d}", off_ns=off)
            set_time(env.clock, off - 590 * S)
            gateway = open_p2(env)
            env.exchange.place_handler = refuse_at_exchange
            try:
                return leg(env, gateway, "BACK", market.market_id)
            finally:
                env.exchange.place_handler = None
                gateway.close()

        for index in range(5):                        # five races today, each a FAILED BACK (a race once submitted)
            self.assertEqual(states(race_at(index, sp.DAY_NS)), ["FAILED"])
        self.assertEqual(rule(race_at(5, sp.DAY_NS)), ("refused", "p2_races_today"))
        set_time(env.clock, NEXT_DAY_NS + 9 * 3600 * S)
        env.arm_seq = arm_record(env, until_ns=NEXT_DAY_NS + 20 * 3600 * S)
        for index in range(5):
            self.assertEqual(states(race_at(index, NEXT_DAY_NS)), ["FAILED"])
        set_time(env.clock, NEXT_DAY_NS + pilot.DAY_NS + 9 * 3600 * S)
        env.arm_seq = arm_record(env, until_ns=NEXT_DAY_NS + pilot.DAY_NS + 20 * 3600 * S)
        self.assertEqual(rule(race_at(0, NEXT_DAY_NS + pilot.DAY_NS)), ("refused", "p2_races_total"))
        self.assertEqual((len(placed(env)), len(p2_lp.races(fold(env)["orders"]))), (10, 10))


class Pull(P2Case):
    def test_cancel_own_legs_only(self):
        env = build(self)
        manual_bet = manual(env, cancel=True)["orders"][0]["bet_id"]
        race(env, "1.230")
        first = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        other_open_bet = self.back_placed(env, first, "1.230")["orders"][0]["bet_id"]
        pulled = first.cancel("1.230", [other_open_bet], source="cli_manual")
        self.assertEqual((rule(pulled), states(pulled)), (("accepted", None), ["EXECUTION_COMPLETE"]))
        first.close()
        race(env, off_ns=OFF + 600 * S)
        set_time(env.clock, OFF + 10 * S)
        gateway = open_p2(env)
        own = self.back_placed(env, gateway)["orders"][0]["bet_id"]
        pilot_1_bet = env.exchange.add_order(selection_id=FAV, strategy_ref="p1-fav-t5", market_id=MARKET)
        before, sent = head(env), len(cancels(env))
        for market_id, bet_ids in (("1.230", [other_open_bet]), ("1.1", [manual_bet]), (MARKET, [pilot_1_bet]),
                                   (MARKET, [own, pilot_1_bet])):
            with self.subTest(bet_ids=bet_ids), self.assertRaisesRegex(OrderRefused, "p2_cancel_not_own_leg"):
                gateway.cancel(market_id, bet_ids, source="cli_manual")
        self.assertEqual((head(env), len(cancels(env))), (before, sent))
        self.assertEqual(env.exchange.orders[pilot_1_bet]["status"], "EXECUTABLE")
        result = gateway.cancel(MARKET, [own], source="cli_manual")
        self.assertEqual((rule(result), states(result)), (("accepted", None), ["EXECUTION_COMPLETE"]))

    def test_pull_partial_fill_reconciles(self):
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        bet = self.back_placed(env, gateway)["orders"][0]["bet_id"]
        env.exchange.orders[bet].update(sizeMatched=1.0, sizeRemaining=1.0, averagePriceMatched=3.1)
        gateway.cancel(MARKET, [bet], source="cli_manual")
        short = [move for event in events(env) if event["kind"] == "report" and event["payload"]["action"] == "cancel"
                 for move in event["payload"]["transitions"]]
        self.assertEqual([(move["state"], move["fields"]["error_code"]) for move in short],
                         [("UNKNOWN", "cancel_size_short_refresh_needed")])
        (order,) = gateway.lp_legs().values()
        self.assertEqual([order[key] for key in ("state", "size_matched_p", "size_cancelled_p", "size_remaining_p")],
                         ["EXECUTION_COMPLETE", 100, 100, 0])

    def test_kill_extra_ref_only_with_config_p2(self):
        for extra in ((), ("p2-lp",)):
            with self.subTest(extra=extra):
                env = gw.build_live_gateway(self)
                no_children(self)
                ours = env.exchange.add_order(strategy_ref="bar-manual")
                leg_bet = env.exchange.add_order(selection_id=FAV, strategy_ref="p2-lp")
                anchor = env.journal.anchor()                        # the Keychain anchor ahead: an untrusted journal
                anchor_write(env.paths.profile, dict(anchor, seq=anchor["seq"] + 5, last_sha256="e" * 64,
                                                     updated_at=env.clock.utc_now()), runner=env.runner)
                env.gateway.open(command="kill")
                self.addCleanup(env.gateway.close)
                result = env.gateway.kill(reason="drill", extra_strategy_refs=extra)
                self.assertEqual(result["status"], "kill_incomplete")
                self.assertIn(UNTRUSTED_SWEEP_NOTE, result["notes"])
                self.assertEqual(env.exchange.orders[ours]["status"], "EXECUTION_COMPLETE")
                self.assertEqual(env.exchange.orders[leg_bet]["status"],
                                 "EXECUTION_COMPLETE" if extra else "EXECUTABLE")

    def test_round_trip_pull_after_unknown_lay_halts(self):
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, gateway)

        def lands_then_lost(record):
            env.exchange.place(json.loads(record["body"])["params"])
            return support.transport_failure("receive")

        env.router.next_response[gw.PLACE_SUFFIX] = [lands_then_lost]
        self.assertEqual(states(leg(env, gateway, "LAY")), ["EXECUTABLE"])      # UNKNOWN, found in line
        self.assertEqual(fold(env)["consecutive_unknown"], 1)
        for order in env.exchange.orders.values():                              # both matched while resting
            order.update(status="EXECUTION_COMPLETE", sizeMatched=2.0, sizeRemaining=0.0,
                         averagePriceMatched=order["priceSize"]["price"])
        bets = {order["side"]: order["bet_id"] for order in gateway.lp_legs().values()}
        gateway.cancel(MARKET, [bets["LAY"]], source="cli_manual")              # the pull, LAY first
        self.assertFalse(fold(env)["halted"])
        gateway.cancel(MARKET, [bets["BACK"]], source="cli_manual")
        state = fold(env)
        self.assertTrue(state["halted"])
        self.assertIn("consecutive_unknown_outcomes", state["halt_reasons"])
        self.assertEqual({order["state"] for order in gateway.lp_legs().values()}, {"EXECUTION_COMPLETE"})


class Evidence(P2Case):
    def test_refs_kept_before_intent_so_raise_after_send_lists_leg(self):
        # The send raised after the order went out: the leg is listed, UNKNOWN.
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        send = env.rpc.send_order

        def sent_then_raised(*args, **kwargs):
            send(*args, **kwargs)
            raise RuntimeError("after_the_send")

        with patch.object(env.rpc, "send_order", sent_then_raised), self.assertRaises(RuntimeError):
            leg(env, gateway, "BACK")
        self.assertEqual(len(placed(env)), 1)
        self.assertEqual([order["state"] for order in gateway.lp_legs().values()], ["UNKNOWN"])
        # The report's append raised after the send: listed too; and with an unreadable journal, by ref alone.
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        append = env.journal.append

        def busy_at_report(kind, payload, **columns):
            if kind == "report":
                raise JournalError("journal_busy")
            return append(kind, payload, **columns)

        with patch.object(env.journal, "append", busy_at_report), self.assertRaisesRegex(JournalError, "journal_busy"):
            leg(env, gateway, "BACK")
        legs = gateway.lp_legs()
        self.assertEqual(len(placed(env)), 1)
        self.assertEqual([order["state"] for order in legs.values()], ["UNKNOWN"])
        with patch.object(OrderJournal, "fold", side_effect=sqlite3.DatabaseError("damaged")):
            self.assertEqual(gateway.lp_legs(), dict.fromkeys(legs))

    def test_send_book_market_time_is_screened_start(self):
        env = build(self)
        market = race(env, store=False)
        screen_row(env, sp.Market(MARKET, OFF + 60 * S, backs=market.backs, lays=market.lays))   # an older row
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.assertEqual(rule(leg(env, gateway, "BACK")), ("refused", "p2_window"))
        screen_row(env, market)                    # the screen's fresh row: the send book's off is the race's
        self.back_placed(env, gateway)
        self.assertEqual(env.read.catalogue_row(MARKET)["marketStartTime"], sp.off_text(OFF))

    def test_country_gb_ie_only_and_missing_refuses(self):
        for country, store, code in (("GB", True, None), ("IE", True, None), ("FR", True, "p2_country"),
                                     ("GB", False, "p2_country")):
            with self.subTest(country=country, stored=store):
                env = build(self)
                race(env, country=country, store=store)
                gateway = open_p2(env)
                set_time(env.clock, OFF - 590 * S)
                before = head(env)
                result = leg(env, gateway, "BACK")
                if code is None:
                    self.assertEqual(states(result), ["EXECUTABLE"])
                    continue
                self.assertEqual(rule(result), ("refused", code))
                self.assertEqual((kinds_after(env, before), placed(env)), (["funds_check"], []))
                if not store:                      # the refetched row has no EVENT
                    projections = env.router.reads_of(sp.CATALOGUE)[-1]["params"]["marketProjection"]
                    self.assertNotIn("EVENT", projections)

    def test_lay_book_problems_bet_delay(self):
        env = build(self)
        market = race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, gateway)
        market.book = lambda: dict(sp.Market.book(market), betDelay=1)
        self.assertEqual(rule(leg(env, gateway, "LAY")), ("refused", "bet_delay_not_zero"))
        del market.book
        market.delayed = True
        self.assertEqual(rule(leg(env, gateway, "LAY")), ("refused", "book_delayed"))
        market.delayed = False
        set_time(env.clock, OFF - 531 * S)
        self.assertEqual(rule(leg(env, gateway, "LAY")), ("refused", "p2_send_deadline"))
        self.assertEqual(len(placed(env)), 1)

    def test_lay_already_sent_from_fold(self):
        env = build(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, gateway)
        send = env.rpc.send_order

        def sent_then_raised(*args, **kwargs):
            send(*args, **kwargs)
            raise RuntimeError("after_the_send")

        with patch.object(env.rpc, "send_order", sent_then_raised), self.assertRaises(RuntimeError):
            leg(env, gateway, "LAY")
        self.assertEqual(rule(leg(env, gateway, "LAY")), ("refused", "p2_lay_already_sent"))
        self.assertEqual(len(placed(env)), 2)

    def test_until_before_pull_refuses_back(self):
        for until_ns, code in ((OFF - 301 * S, "p2_until_before_pull"), (OFF - 300 * S, None)):
            with self.subTest(until=utc_from_ns(until_ns)):
                env = build(self, until_ns=until_ns)
                race(env)
                gateway = open_p2(env)
                set_time(env.clock, OFF - 590 * S)
                result = leg(env, gateway, "BACK")
                self.assertEqual(rule(result), ("refused", code) if code else ("accepted", None))

    def test_second_arm_today_refused(self):
        env = build(self)
        second = arm_record(env, session_id="p2s-0e0f0a0b")
        race(env)
        gateway = open_p2(env, lp_of(env, second, "p2s-0e0f0a0b"))
        set_time(env.clock, OFF - 590 * S)
        self.assertEqual(rule(leg(env, gateway, "BACK")), ("refused", "p2_arm_already_today"))
        # An ARM record of another UTC day is no bar.
        env = build(self, arm=False)
        arm_record(env, session_id="p2s-0e0f0a0b", day="2027-01-03")
        env.arm_seq = arm_record(env)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, OFF - 590 * S)
        self.back_placed(env, gateway)


if __name__ == "__main__":
    unittest.main()
