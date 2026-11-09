"""Pilot 2's executor (p2_run: p2-run and p2-summary), offline, synthetic, driven through cli.main.

These run the real executor, gateway, journal, risk guard, recording admission and JSON-RPC and read clients over the
simulated exchange (test_betfair_support_gateway) with Pilot 1's per-market fixtures (test_betfair_support_pilot),
config-p2.json beside the main config.json, a FakeClock whose sleep moves time, a FakeConfirmer that types exactly the
ARM line it is asked (it stands in for Yu), a FakeKeychainRunner, FakeGit, a fake Popen for caffeinate and a fake
process table. P2Betfair answers the day's catalogue list, a delayed key's book as delayed and a live key's as live.
One test per edge of the note's state machine (docs/betfair-pilot-2-engineering.md §8; build spec §3 and §7), the ARM
refusals and record (§5), and p2-summary with --cleared (§6). Exceptions and refusals are injected at the exchange,
the transport or one gateway method. No /usr/bin/security, git or other child process runs, nothing opens the network,
and no real caffeinate starts. They do not show Betfair's real behaviour.
"""

import contextlib
import copy
import io
import json
import os
import signal
import sqlite3
import subprocess
import unittest
from unittest.mock import Mock, patch

from betangel_research.betfair import admission, cli, p2_lp, p2_run, pilot, pilot_run
from betangel_research.betfair import config as cfg
from betangel_research.betfair.api import JsonRpcClient
from betangel_research.betfair.arming import expected_token
from betangel_research.betfair.errors import JournalError, LockBusy, NotArmed
from betangel_research.betfair.gateway import OrderGateway
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, file_lock
from betangel_research.betfair.receipts import utc_from_ns

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
import test_betfair_support_run as sr
from test_betfair_admission import FakeProcesses
from test_betfair_p2_gateway import (arm_record, fake_git, fill, in_force, manual, no_children, p2_orders,
                                     refuse_at_exchange, session_start)
from test_betfair_support import dumps

S = p2_lp.NS
OFF = sp.OFF                                      # 2027-01-04 10:00:00Z
ARM_AT = OFF - 900 * S                            # 09:45:00Z: the ARM, a minute before the first screen
MARKET = "1.234"
FAV = sp.FAV                                      # runner 11, the one the runner rule takes
REST_END = OFF + p2_lp.PULL_S * S


class P2Market(sp.Market):
    """A race whose MARKET_DESCRIPTION says turnInPlayEnabled (unless turn_in_play is set False)."""

    turn_in_play = True

    def row(self, projections):
        row = super().row(projections)
        if "description" in row:
            row["description"]["turnInPlayEnabled"] = self.turn_in_play
        return row


class P2Betfair(sp.PilotBetfair):
    """PilotBetfair plus the day's list (a listMarketCatalogue with no marketIds: every fixture market whose start lies
    in the filter's window, first to start first, with the projections asked), and isMarketDataDelayed true on books
    read with the delayed key."""

    def __call__(self, record):
        url = record["url"]
        if any(url.endswith(end) for end in ("/api/login", "/api/certlogin", "/api/keepAlive", "/api/logout")):
            return super().__call__(record)
        body = json.loads(record["body"])
        name, params = body["method"].split("/")[-1], body["params"]
        key = support.FakeHttpTransport.header_text(record, "X-Application")
        if name == sp.CATALOGUE and not (params.get("filter") or {}).get("marketIds") \
                and not self.next_response.get(name):
            self.reads.append({"method": name, "params": copy.deepcopy(params), "list": True,
                               "slot": "live" if key == gw.LIVE_KEY else "delayed"})
            window = params["filter"]["marketStartTime"]
            low, high = pilot.parse_utc_ns(window["from"]), pilot.parse_utc_ns(window["to"])
            rows = [market.row(set(params.get("marketProjection") or ()))
                    for market in sorted(self.markets.values(), key=lambda m: (m.off_ns, m.market_id))
                    if low <= market.off_ns <= high]
            self.rpc_methods.append(name)
            return support.http_response(body=dumps({"jsonrpc": "2.0", "result": rows, "id": body["id"]}).encode())
        if name == sp.BOOK and key != gw.LIVE_KEY and not self.next_response.get(name):
            delayed = [self.markets[m] for m in params.get("marketIds") or () if m in self.markets]
            for market in delayed:
                market.delayed = True
            try:
                return super().__call__(record)
            finally:
                for market in delayed:
                    market.delayed = False
        return super().__call__(record)


class FakePopen:
    """subprocess.Popen for caffeinate: records each call (and the journal head then) and its process's terminate and
    wait; starts nothing. fail: the OSError the call raises."""

    def __init__(self, env, fail=None):
        self.env, self.fail = env, fail
        self.calls, self.events = [], []

    def __call__(self, argv, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.calls.append((list(argv), kwargs, [e["kind"] for e in events(self.env)]))
        return self

    def terminate(self):
        self.events.append("terminate")

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        return 0


def build(test, *, start_ns=ARM_AT):
    """The p2-run world at start_ns: gw.build_live_gateway's armed parts (env.config is the main config.json, for
    manual bets) over P2Betfair, config-p2.json (env.p2), FakeGit, a fake Popen and a fake process table."""
    env = gw.build_live_gateway(test, clock=support.FakeClock(utc_from_ns(start_ns)))
    no_children(test)
    env.test = test
    env.router = P2Betfair(env.exchange)
    env.transport.router = env.router
    support.write_test_config(env.paths, support.deep_merge(gw.LIVE_OVERRIDES, p2_orders()),
                              path=env.paths.config_p2_path)
    env.p2 = cfg.load_config_p2(paths=env.paths)
    env.git = sp.FakeGit()
    env.popen = FakePopen(env)
    env.processes = FakeProcesses(pid=1)
    env.environ = {FORBID_NETWORK_ENV: "1"}
    env.runner.calls.clear()
    return env


def add_race(env, market_id=MARKET, off_ns=OFF, *, back="3.00", lay="3.15", **changes):
    """A race whose runner 11 is at back/lay (3 ticks: the BACK at 3.10, the LAY at 3.05); the others are out of band
    or one tick wide."""
    backs, lays = copy.deepcopy(sp.BASE_BACKS), copy.deepcopy(sp.BASE_LAYS)
    backs[FAV], lays[FAV] = ((back, "15.00"),), ((lay, "20.00"),)
    market = P2Market(market_id, off_ns, backs=backs, lays=lays, **changes)
    env.router.markets[market_id] = market
    return market


def deps(env):
    return {"keychain_runner": env.runner, "http_transport": env.transport, "clock": env.clock,
            "confirmer": env.confirmer, "env": env.environ, "processes": env.processes, "git_runner": env.git,
            "popen": env.popen}


def run_main(argv, deps_):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, deps=deps_)
    return code, out.getvalue(), err.getvalue()


def p2_run_cli(env, *, races=1, until="2000", extra=()):
    """p2-run --mode live with today's config-p2 token -> (exit code, stdout, stderr, the end summary or None)."""
    token = expected_token(env.p2, env.clock.time_ns())
    code, out, err = run_main(["p2-run", "--mode", "live", "--confirm-live", token, "--until", until, "--races",
                               str(races), *extra], deps(env))
    return code, out, err, ended(out)


def ended(out):
    """The p2_session_ended object printed at the end, or None."""
    decoder, at, found = json.JSONDecoder(), 0, None
    while True:
        start = out.find("{", at)
        if start < 0:
            return found
        try:
            value, at = decoder.raw_decode(out, start)
        except ValueError:
            at = start + 1
            continue
        if isinstance(value, dict) and value.get("status") == "p2_session_ended":
            found = value


def events(env):
    return list(env.journal.events())


def kinds(env):
    return [event["kind"] for event in events(env)]


def arm_events(env):
    return [event for event in events(env) if event["kind"] == "operator_confirm"]


def legs(env):
    """The p2-lp orders of the core fold, BACK first."""
    orders = env.journal.fold(now_ns=env.clock.time_ns())["orders"]
    return sorted((o for o in orders.values() if o["customer_strategy_ref"] == "p2-lp"),
                  key=lambda o: (o["market_id"], o["side"]))


def placed(env):
    return [r["params"] for r in env.router.order_requests if r["method"].endswith(gw.PLACE_SUFFIX)]


def cancelled(env):
    return [r["params"] for r in env.router.order_requests if r["method"].endswith(gw.CANCEL_SUFFIX)]


def live_key_requests(env):
    return [r for r in env.transport.requests
            if support.FakeHttpTransport.header_text(r, "X-Application") == gw.LIVE_KEY]


def at(env, when_ns, action):
    """action() once, in the first clock.sleep that starts at or after when_ns (before that sleep moves time)."""
    return sr.on_sleep(env, lambda clock: clock.time_ns() >= when_ns, action)


def recorded_ns(event):
    return pilot.parse_utc_ns(event["recorded_at"])


def submits(env, action):
    return [event for event in events(env) if event["kind"] == "submit" and event["payload"]["action"] == action]


class P2RunCase(unittest.TestCase):
    def setUp(self):
        in_force(self)
        fake_git(self)

    def assertEnded(self, result, code, *, exit_code, released):
        exit_, out, err, summary = result
        self.assertEqual(exit_, exit_code, err)
        self.assertEqual(summary["end_code"], code, err)
        if released:
            self.assertRegex(err, r"recording released \(generation [0-9]+\)")
        else:
            self.assertNotIn("recording released", err)

    def assertHandoff(self, err, *refs_or_bets):
        self.assertIn("These legs may still be open. Stop them now, in this order:", err)
        self.assertLess(err.index("1. the Betfair phone app"), err.index("2. the Betfair website"))
        self.assertIn(f"3. the own-scope kill from Pilot 2's checkout: cd \"{p2_run.CHECKOUT}\"", err)
        for name in refs_or_bets:
            self.assertIn(f"p2-lp leg {name}", err)
        # After a handoff no release is tried: recording stays off until Yu's typed recording-release.
        self.assertIn("recording stays off until Yu's typed recording-release (generation 1)", err)
        self.assertNotIn("release refused", err)


class Race(P2RunCase):
    def test_happy_path(self):
        env = build(self)
        add_race(env)
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        summary = result[3]
        self.assertEqual({k: summary[k] for k in ("markets", "screened", "flagged", "races", "codes")},
                         {"markets": 1, "screened": 1, "flagged": 1, "races": 1, "codes": {}})
        (arm,) = arm_events(env)
        inner = arm["payload"]["p2_arm"]
        sha = cfg.config_sha256(env.p2)
        self.assertEqual(arm["payload"]["echo"], f"P2 ARM {sha[:12]} 20270104 UNTIL 2000Z RACES 1")
        self.assertEqual({k: inner[k] for k in ("config_sha256", "utc_day", "until_ns", "races", "code_commit")},
                         {"config_sha256": sha, "utc_day": "2027-01-04", "until_ns": sp.UNTIL, "races": 1,
                          "code_commit": sp.COMMIT})
        self.assertRegex(inner["session_id"], r"^p2s-[0-9a-f]{8}$")
        self.assertEqual([p["instructions"][0]["side"] for p in placed(env)], ["BACK", "LAY"])
        self.assertEqual([p["instructions"][0]["limitOrder"]["price"] for p in placed(env)], [3.1, 3.05])
        back, lay = legs(env)
        self.assertEqual([c["instructions"][0]["betId"] for c in cancelled(env)], [lay["bet_id"], back["bet_id"]])
        self.assertEqual([(o["side"], o["state"], o["size_cancelled_p"], o["size_matched_p"]) for o in (back, lay)],
                         [("BACK", "EXECUTION_COMPLETE", 200, 0), ("LAY", "EXECUTION_COMPLETE", 200, 0)])
        # The BACK decided at off - 590 s, the LAY at once, the pull at off - 300 s.
        intents = [event for event in events(env) if event["kind"] == "intent"]
        self.assertEqual([recorded_ns(e) - OFF for e in intents], [-590 * S, -590 * S])
        self.assertEqual([recorded_ns(e) - OFF for e in submits(env, "cancel")], [-300 * S, -300 * S])
        # The day's list and the screen (its fresh row, stored, and one book) on the delayed key; the two send books
        # on the live key, with virtual prices and the stored row (no live catalogue read).
        (listed,) = [r for r in env.router.reads if r.get("list")]
        self.assertEqual((listed["slot"], listed["params"]["filter"]["marketCountries"],
                          listed["params"]["filter"]["eventTypeIds"], listed["params"]["filter"]["marketTypeCodes"],
                          listed["params"]["marketProjection"], listed["params"]["sort"]),
                         ("delayed", ["GB", "IE"], ["7"], ["WIN"], ["MARKET_DESCRIPTION", "MARKET_START_TIME"],
                          "FIRST_TO_START"))
        self.assertEqual([r["slot"] for r in env.router.reads_of(sp.CATALOGUE, MARKET)], ["delayed"])
        books = env.router.reads_of(sp.BOOK, MARKET)
        self.assertEqual([(r["slot"], r["params"]["priceProjection"].get("virtualise")) for r in books],
                         [("delayed", None), ("live", True), ("live", True)])
        self.assertEqual(admission.admission_status(env.paths)["admission"], None)

    def test_unflagged_gets_no_live_read(self):
        env = build(self)
        add_race(env, lay="3.10")                     # two ticks: no runner the rule takes
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["flagged"], result[3]["codes"]), (0, {"spread_out_of_band": 1}))
        self.assertEqual(live_key_requests(env), [])
        self.assertEqual({r["slot"] for r in env.router.reads}, {"delayed"})
        self.assertEqual((placed(env), kinds(env)[-1]), ([], "operator_confirm"))

    def test_screen_window_rechecked_after_the_idle_wait(self):
        # Codex on #62: the Mac stalls or sleeps in IDLE's wait for the screen (180 s from off -850 s, so it wakes at
        # about off -670 s, past -780 s). The window is checked again after the wait: missed_window, nothing read live.
        env = build(self)
        add_race(env)
        at(env, OFF - 850 * S, lambda: env.clock.advance(180))
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["screened"], result[3]["codes"]), (0, {"missed_window": 1}))
        self.assertEqual((live_key_requests(env), placed(env), legs(env)), ([], [], []))
        self.assertEqual(kinds(env)[-1], "operator_confirm")

    def test_decide_window_rechecked_after_the_pre_open_wait(self):
        # Review round (time-1): the Mac stalls or sleeps in the wait for the open (200 s from off -760 s, so it wakes
        # at about off -559 s, past -575 s). The BACK could only be refused: missed_window, no open, no live-key read.
        env = build(self)
        add_race(env)
        at(env, OFF - 760 * S, lambda: env.clock.advance(200))
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual({k: result[3][k] for k in ("screened", "flagged", "codes")},
                         {"screened": 1, "flagged": 1, "codes": {"missed_window": 1}})
        self.assertEqual((live_key_requests(env), placed(env), legs(env)), ([], [], []))
        self.assertEqual(kinds(env)[-1], "operator_confirm")

    def test_slow_open_moves_on_before_the_back(self):
        # Review round (time-1): the open itself runs past off -575 s (170 s from -740 s). The WAIT returns at once and
        # the race moves on with no funds read and no live book for a BACK that could only be refused.
        env = build(self)
        add_race(env)
        open_report = OrderGateway.open_report

        def slow(gateway):
            env.clock.advance(170)
            return open_report(gateway)

        with patch.object(OrderGateway, "open_report", slow):
            result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual(result[3]["codes"], {"missed_window": 1})
        self.assertEqual((placed(env), legs(env)), ([], []))
        self.assertNotIn("funds_check", kinds(env)[kinds(env).index("operator_confirm"):])
        self.assertEqual([r["slot"] for r in env.router.reads_of(sp.BOOK, MARKET)], ["delayed"])

    def test_turn_in_play_off_is_left_out_of_the_list(self):
        # Review round (exec-filters-1): a market that does not turn in play (LAPSE would not act) is never screened.
        env = build(self)
        market = add_race(env)
        market.turn_in_play = False
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertIn("markets: 0 listed; left out: turn_in_play_not_enabled 1", result[2])
        self.assertEqual((result[3]["markets"], result[3]["screened"]), (0, 0))
        self.assertEqual((env.router.reads_of(sp.CATALOGUE, MARKET), env.router.reads_of(sp.BOOK, MARKET)), ([], []))
        self.assertEqual(live_key_requests(env), [])

    def test_off_moved_at_the_screen(self):
        # Review round (exec-filters-1): the start moves after the day's list and before the screen: the fresh row's
        # off differs, so off_moved, no book read and nothing on the live key.
        env = build(self)
        market = add_race(env)
        at(env, OFF - 841 * S, lambda: setattr(market, "off_ns", OFF + 60 * S))
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["screened"], result[3]["flagged"], result[3]["codes"]), (1, 0, {"off_moved": 1}))
        self.assertEqual(env.router.reads_of(sp.BOOK, MARKET), [])
        self.assertEqual(live_key_requests(env), [])

    def test_back_refused(self):
        env = build(self)
        market = add_race(env)
        at(env, OFF - 741 * S, lambda: market.lays.update({FAV: (("3.10", "20.00"),)}))   # after the screen
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["races"], result[3]["codes"]), (0, {"spread_out_of_band": 1}))
        self.assertEqual((placed(env), legs(env)), ([], []))
        self.assertEqual(kinds(env)[-1], "funds_check")         # the choice refused: only funds_check journaled

    def test_back_refusal_listing_live_orders_is_a_skip(self):
        # A refused result may list other bets as live (the gateway's resolved-at-open refusal lists the earlier
        # orders it found EXECUTABLE): still a refusal, so no race, no LAY and nothing sent.
        env = build(self)
        add_race(env)
        sides = []

        def resolved_at_open(gateway, market_id, side, **kwargs):
            sides.append(side)
            return {"intent_id": None, "mode": "live", "decision": "refused",
                    "refusal": {"rule": "earlier_unknown_resolved_at_open", "detail": {}},
                    "orders": [{"customer_order_ref": "bro-earlier", "state": "EXECUTABLE", "bet_id": "9"}],
                    "journal_seq": None}

        with patch.object(OrderGateway, "lp_place", resolved_at_open):
            result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["races"], result[3]["codes"]), (0, {"earlier_unknown_resolved_at_open": 1}))
        self.assertEqual((sides, placed(env), cancelled(env)), (["BACK"], [], []))

    def test_four_tick_spread_legs_inside_v13(self):
        # 3.00/3.20 is 4 ticks: the BACK at 3.15 and the LAY at 3.05 are each 3 ticks from the best price, V13's edge.
        env = build(self)
        add_race(env, back="3.00", lay="3.20")
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        self.assertEqual([p["instructions"][0]["limitOrder"]["price"] for p in placed(env)], [3.15, 3.05])
        self.assertEqual(len(cancelled(env)), 2)

    def test_back_unknown_found_and_absent(self):
        env = build(self)
        add_race(env)

        def lands_then_lost(record):
            env.exchange.place(json.loads(record["body"])["params"])
            return support.transport_failure("receive")

        env.router.next_response[gw.PLACE_SUFFIX] = [lands_then_lost]
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        self.assertEqual([o["reconcile_attempts"] > 0 for o in legs(env)], [True, False])
        self.assertEqual(len(cancelled(env)), 2)
        # Not on the exchange: ABSENT_UNCONFIRMED after the in-line reconcile, handed off; no LAY, no release.
        env = build(self)
        add_race(env)
        env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("receive")]
        result = p2_run_cli(env)
        self.assertEnded(result, "back_unresolved", exit_code=5, released=False)
        (back,) = legs(env)
        self.assertEqual(back["state"], "ABSENT_UNCONFIRMED")
        self.assertHandoff(result[2], back["customer_order_ref"])
        self.assertIn("recording stays off until Yu's typed recording-release (generation 1)", result[2])
        self.assertEqual((len(placed(env)), cancelled(env)), (1, []))

    def test_lay_refused_pulls_back(self):
        # The exchange refuses the LAY (FAILED), and the LAY's fresh book shows the runner gone (a choice code).
        for case in ("exchange", "choice"):
            with self.subTest(case=case):
                env = build(self)
                market = add_race(env)

                def back_then(params):
                    env.exchange.place_handler = None
                    result = env.exchange.place(params)
                    if case == "exchange":
                        env.exchange.place_handler = refuse_at_exchange
                    else:
                        market.statuses[FAV] = "REMOVED"
                    return result

                env.exchange.place_handler = back_then
                result = p2_run_cli(env)
                self.assertEnded(result, "races_done", exit_code=0, released=True)
                back = legs(env)[0]
                self.assertEqual([c["instructions"][0]["betId"] for c in cancelled(env)], [back["bet_id"]])
                self.assertEqual([recorded_ns(e) - OFF for e in submits(env, "cancel")], [-590 * S])
                self.assertEqual(len(placed(env)), 2 if case == "exchange" else 1)

    def test_lay_unknown(self):
        env = build(self)
        add_race(env)

        def lands_then_lost(record):
            env.exchange.place(json.loads(record["body"])["params"])
            return support.transport_failure("receive")

        def back_then(params):
            env.router.next_response[gw.PLACE_SUFFIX] = [lands_then_lost]       # the LAY's request
            env.exchange.place_handler = None
            return env.exchange.place(params)

        env.exchange.place_handler = back_then
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)        # found: rest, then the pull
        self.assertEqual(len(cancelled(env)), 2)
        # Never landed: ABSENT_UNCONFIRMED; the BACK is pulled at once and the LAY handed off.
        env = build(self)
        add_race(env)

        def back_then_lost(params):
            env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("receive")]
            env.exchange.place_handler = None
            return env.exchange.place(params)

        env.exchange.place_handler = back_then_lost
        result = p2_run_cli(env)
        self.assertEnded(result, "lay_unresolved", exit_code=5, released=False)
        back, lay = legs(env)
        self.assertEqual((back["state"], lay["state"]), ("EXECUTION_COMPLETE", "ABSENT_UNCONFIRMED"))
        self.assertEqual([c["instructions"][0]["betId"] for c in cancelled(env)], [back["bet_id"]])
        self.assertHandoff(result[2], lay["customer_order_ref"])
        self.assertNotIn(f"p2-lp leg {back['bet_id']}", result[2])

    def test_cancel_fails(self):
        # The LAY's cancel is not sent (connect failure): it stays EXECUTABLE; the BACK's goes; the LAY is handed off.
        env = build(self)
        add_race(env)
        at(env, REST_END - S, lambda: env.router.next_response.__setitem__(
            gw.CANCEL_SUFFIX, [support.transport_failure("connect")]))
        result = p2_run_cli(env)
        self.assertEnded(result, "pull_incomplete", exit_code=5, released=False)
        back, lay = legs(env)
        self.assertEqual((back["state"], lay["state"], lay["error_code"]),
                         ("EXECUTION_COMPLETE", "EXECUTABLE", "cancel_not_processed"))
        self.assertHandoff(result[2], lay["bet_id"])
        # A cancel that raises is not retried: the next leg's still goes, and the session ends with the handoff.
        env = build(self)
        add_race(env)
        original = OrderGateway.cancel
        raised = []

        def cancel(gateway, *args, **kwargs):
            if not raised:
                raised.append(True)
                raise RuntimeError("cancel_path_broke")
            return original(gateway, *args, **kwargs)

        with patch.object(OrderGateway, "cancel", cancel):
            result = p2_run_cli(env)
        self.assertEnded(result, "pull_incomplete", exit_code=5, released=False)
        self.assertIn(f"{MARKET} pull: cancel_raised:RuntimeError", result[2])
        back, lay = legs(env)
        self.assertEqual((back["state"], lay["state"]), ("EXECUTION_COMPLETE", "EXECUTABLE"))
        self.assertEqual(len(cancelled(env)), 1)

    def test_suspended_at_pull(self):
        env = build(self)
        add_race(env)

        def suspended(params):
            return {"customerRef": params["customerRef"], "status": "FAILURE", "errorCode": "MARKET_SUSPENDED",
                    "marketId": params["marketId"],
                    "instructionReports": [{"status": "FAILURE", "errorCode": "MARKET_SUSPENDED", "instruction": i}
                                           for i in params["instructions"]]}

        env.exchange.cancel_handler = suspended
        result = p2_run_cli(env)
        self.assertEnded(result, "pull_incomplete", exit_code=5, released=False)
        back, lay = legs(env)
        self.assertEqual((back["state"], lay["state"]), ("EXECUTABLE", "EXECUTABLE"))
        self.assertEqual(len(cancelled(env)), 2)                       # one cancel each, never a second
        self.assertHandoff(result[2], back["bet_id"], lay["bet_id"])

    def test_matched_leg_ends_session_with_settle_line(self):
        env = build(self)
        add_race(env)
        add_race(env, "1.235", OFF + 900 * S)
        fill(env)                                                       # both legs matched at once: a round trip
        result = p2_run_cli(env)
        self.assertEnded(result, "matched_leg", exit_code=0, released=True)
        self.assertIn(f"{MARKET}: a p2-lp leg matched; after its result run settle-market --market {MARKET}",
                      result[2])
        self.assertEqual(([o["size_matched_p"] for o in legs(env)], cancelled(env)), ([200, 200], []))
        self.assertEqual(env.router.reads_of(sp.CATALOGUE, "1.235"), [])          # the session ended
        # The next ARM refuses until the matched legs are settled.
        sr.set_day(env, 1, before_off_s=900)
        code, _, err, _ = p2_run_cli(env)
        self.assertEqual(code, 2)
        self.assertIn("p2_other_bet_open", err)


class Stops(P2RunCase):
    def test_ctrl_c_idle_wait_between_rest_and_second_in_pull(self):
        def idle(env):
            at(env, OFF - 850 * S, sr.fire)

        def wait(env):
            at(env, OFF - 700 * S, sr.fire)                     # in the open, before any send: SignalStop

        def between(env):
            def back(params):
                env.exchange.place_handler = None
                result = env.exchange.place(params)
                sr.fire()                                       # after the BACK's send: deferred
                return result
            env.exchange.place_handler = back

        def rest(env):
            at(env, OFF - 400 * S, sr.fire)

        def twice(env):
            rest(env)

            def cancel(params):
                env.exchange.cancel_handler = None
                sr.fire(signal.SIGTERM)                         # a second signal in the pull: deferred too
                return env.exchange.cancel(params)
            env.exchange.cancel_handler = cancel

        cases = {"idle": (idle, 0, 0), "wait": (wait, 0, 0), "between": (between, 1, 1), "rest": (rest, 2, 2),
                 "second_in_pull": (twice, 2, 2)}
        for name, (inject, sends, cancels) in cases.items():
            with self.subTest(case=name):
                env = build(self)
                add_race(env)
                inject(env)
                result = p2_run_cli(env)
                self.assertEnded(result, "stopped", exit_code=0, released=True)
                self.assertTrue(result[3]["stop_requested"])
                self.assertEqual((len(placed(env)), len(cancelled(env))), (sends, cancels))
                self.assertTrue(all(o["state"] == "EXECUTION_COMPLETE" for o in legs(env)))
                self.assertEqual(live_key_requests(env) == [], name == "idle")

    def test_ctrl_c_during_the_lay_refuses_it_at_the_first_byte(self):
        # Review round (time-4): Ctrl-C after BETWEEN's look, inside the LAY's lp_place (its funds read). The signal is
        # deferred, so the LAY's first-byte gate refuses it (FAILED, nothing sent); the BACK is pulled; the end.
        env = build(self)
        add_race(env)
        funds, calls = OrderGateway._funds, []

        def fire_on_the_lays(gateway):
            calls.append(True)
            if len(calls) == 2:
                sr.fire()
            return funds(gateway)

        with patch.object(OrderGateway, "_funds", fire_on_the_lays):
            result = p2_run_cli(env)
        self.assertEnded(result, "stopped", exit_code=0, released=True)
        self.assertTrue(result[3]["stop_requested"])
        back, lay = legs(env)
        self.assertEqual((len(placed(env)), [c["instructions"][0]["betId"] for c in cancelled(env)]),
                         (1, [back["bet_id"]]))
        self.assertEqual((back["state"], lay["state"], lay["error_code"]),
                         ("EXECUTION_COMPLETE", "FAILED", "transport_not_sent"))
        self.assertIn("p2_stop_before_send", [e["payload"]["exception"]["code"] for e in events(env)
                                              if e["kind"] == "report" and e["payload"].get("exception")])

    def test_kill_file_pulls(self):
        env = build(self)
        add_race(env)
        at(env, OFF - 400 * S, lambda: engage(env.paths, "agent_saw_something", clock=env.clock))
        result = p2_run_cli(env)
        self.assertEnded(result, "kill_file", exit_code=5, released=False)      # release refuses the KILL file
        self.assertIn("kill_file_engaged", result[2])
        self.assertEqual(len(cancelled(env)), 2)
        self.assertTrue(all(o["state"] == "EXECUTION_COMPLETE" for o in legs(env)))

    def test_kill_event_hands_off_no_cancel(self):
        env = build(self)
        add_race(env)

        def kill():
            engage(env.paths, "yu", clock=env.clock)
            sp.other_writer(env).append("kill", {"reason": "yu", "scope": "own"})

        at(env, OFF - 400 * S, kill)
        result = p2_run_cli(env)
        self.assertEnded(result, "kill_event", exit_code=5, released=False)
        self.assertEqual(cancelled(env), [])
        back, lay = legs(env)
        self.assertHandoff(result[2], back["bet_id"], lay["bet_id"])

    def test_unreadable_journal_hands_off_no_cancel(self):
        # Between the legs (the look after the BACK), in the rest, and at the pull's own kill check: never race a
        # kill the journal cannot show.
        unreadable = {"readable": False, "events": [], "kill_seq": None, "kill_incomplete_seq": None,
                      "place_submit": None}
        original_read, original_watch = pilot_run.pilot_events_after, p2_run.watch

        def between(env, broken):
            def back(params):
                env.exchange.place_handler = None
                result = env.exchange.place(params)
                broken.append(True)
                return result
            env.exchange.place_handler = back

        def rest(env, broken):
            at(env, OFF - 400 * S, lambda: broken.append(True))

        def pull(env, broken):
            def watch(env_, s, since_seq, *, until_ns):
                found = original_watch(env_, s, since_seq, until_ns=until_ns)
                if found is None and until_ns == REST_END:
                    broken.append(True)
                return found
            return patch.object(p2_run, "watch", watch)

        for name, (inject, sends) in {"between": (between, 1), "rest": (rest, 2), "pull": (pull, 2)}.items():
            with self.subTest(case=name):
                env = build(self)
                add_race(env)
                broken = []
                patcher = inject(env, broken) or contextlib.nullcontext()

                def read(path, since_seq):
                    return dict(unreadable) if broken else original_read(path, since_seq)

                with patcher, patch.object(pilot_run, "pilot_events_after", read):
                    result = p2_run_cli(env)
                self.assertEnded(result, "unreadable", exit_code=5, released=False)
                self.assertEqual((len(placed(env)), cancelled(env)), (sends, []))
                self.assertHandoff(result[2], *[o["bet_id"] for o in legs(env)])

    def test_foreign_event_pulls(self):
        env = build(self)
        add_race(env)
        at(env, OFF - 400 * S, lambda: sp.other_writer(env).append("note", {"reason": "another_writer"}))
        result = p2_run_cli(env)
        self.assertEnded(result, "foreign", exit_code=0, released=True)
        self.assertEqual(len(cancelled(env)), 2)
        self.assertEqual([recorded_ns(e) - OFF for e in submits(env, "cancel")], [-399 * S, -399 * S])

    def test_drift_and_late_wake(self):
        for name, action in (("drift", lambda env: env.clock.step_wall(3)),
                             ("late_wake", lambda env: env.clock.advance(10))):
            with self.subTest(case=name):
                env = build(self)
                add_race(env)
                at(env, OFF - 400 * S, lambda: action(env))
                result = p2_run_cli(env)
                self.assertEnded(result, name, exit_code=0, released=True)
                self.assertEqual(len(cancelled(env)), 2)

    def test_drift_before_the_open_is_seen_at_the_wait(self):
        # Review round (time-2): the wall clock steps back 120 s in the wait for the open. The drift baseline is the
        # race's (after the screen), so the WAIT's first look ends the session before any send.
        env = build(self)
        add_race(env)
        at(env, OFF - 760 * S, lambda: env.clock.step_wall(-120))
        result = p2_run_cli(env)
        self.assertEnded(result, "drift", exit_code=0, released=True)
        self.assertEqual((placed(env), legs(env)), ([], []))

    def test_kill_event_before_pull_hands_off(self):
        env = build(self)
        add_race(env)
        original = p2_run.watch

        def watch(env_, s, since_seq, *, until_ns):
            found = original(env_, s, since_seq, until_ns=until_ns)
            if found is None and until_ns == REST_END:                # the kill lands between REST and the PULL
                engage(env.paths, "yu", clock=env.clock)
                sp.other_writer(env).append("kill", {"reason": "yu", "scope": "own"})
            return found

        with patch.object(p2_run, "watch", watch):
            result = p2_run_cli(env)
        self.assertEnded(result, "kill_event", exit_code=5, released=False)
        self.assertEqual(cancelled(env), [])
        back, lay = legs(env)
        self.assertHandoff(result[2], back["bet_id"], lay["bet_id"])

    def test_no_betfair_call_during_rest(self):
        env = build(self)
        add_race(env)
        seen = []
        original = env.clock.sleep

        def sleep(seconds):
            if OFF - 590 * S <= env.clock.time_ns() < REST_END:
                seen.append(len(env.transport.requests))
            return original(seconds)

        env.clock.sleep = sleep
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        self.assertEqual(len(seen), 290)
        self.assertEqual(set(seen), {seen[0]})


class Failures(P2RunCase):
    def test_exception_after_back_one_pull_then_handoff_releases_lock(self):
        env = build(self)
        add_race(env)
        original = OrderJournal.append

        def append(journal, kind, payload, **columns):
            if kind == "report" and payload.get("action") == "place" and len(placed(env)) == 2:
                raise JournalError("journal_busy")                     # the LAY's report, after its send
            return original(journal, kind, payload, **columns)

        with patch.object(OrderJournal, "append", append):
            result = p2_run_cli(env)
        self.assertEnded(result, "exception", exit_code=5, released=False)
        back, lay = legs(env)
        self.assertEqual([c["instructions"][0]["betId"] for c in cancelled(env)], [back["bet_id"]])   # one pull
        self.assertEqual((back["state"], lay["bet_id"]), ("EXECUTION_COMPLETE", None))
        self.assertIn(lay["state"], ("SUBMITTING", "UNKNOWN"))                    # its report never journaled
        self.assertHandoff(result[2], lay["customer_order_ref"])
        self.assertIn("recording stays off until Yu's typed recording-release (generation 1)", result[2])
        with file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock):       # .gateway.lock was let go
            pass

    def test_exception_after_send_before_keep_hands_off_leg(self):
        env = build(self)
        add_race(env)
        send = JsonRpcClient.send_order

        def sent_then_raised(rpc, *args, **kwargs):
            send(rpc, *args, **kwargs)
            raise RuntimeError("after_the_send")

        with patch.object(JsonRpcClient, "send_order", sent_then_raised):
            result = p2_run_cli(env)
        self.assertEnded(result, "exception", exit_code=5, released=False)
        (back,) = legs(env)
        self.assertEqual((len(placed(env)), cancelled(env), back["bet_id"]), (1, [], None))
        self.assertIn(back["state"], ("SUBMITTING", "UNKNOWN"))
        self.assertHandoff(result[2], back["customer_order_ref"])

    def test_skip_streak(self):
        env = build(self)
        for n in range(6):                              # FR: the BACK's country check refuses each, live key read
            add_race(env, f"1.{240 + n}", OFF + n * 600 * S, country="FR")
        result = p2_run_cli(env)
        self.assertEnded(result, "skip_streak", exit_code=0, released=True)
        self.assertEqual((result[3]["codes"], result[3]["flagged"]), ({"p2_country": 5}, 5))
        self.assertEqual(env.router.reads_of(sp.CATALOGUE, "1.245"), [])
        self.assertEqual(placed(env), [])

    def test_skip_streak_resets_after_a_race(self):
        # Day 1: the first session's one race. Day 2, RACES 2: four refused BACKs, a race, then five more refused.
        env = build(self)
        add_race(env)
        self.assertEnded(p2_run_cli(env), "races_done", exit_code=0, released=True)
        sr.set_day(env, 1, before_off_s=900)
        day2 = OFF + pilot.DAY_NS
        for n in range(10):
            add_race(env, f"1.{300 + n}", day2 + n * 600 * S, country="GB" if n == 4 else "FR")
        result = p2_run_cli(env, races=2)
        self.assertEnded(result, "skip_streak", exit_code=0, released=True)
        self.assertEqual((result[3]["races"], result[3]["codes"]), (1, {"p2_country": 9}))
        self.assertEqual(len(placed(env)), 4)

    def test_pull_that_raised_is_not_pulled_again(self):
        # The pull's first leg read raises: the exception edge hands both legs off and never pulls a second time.
        env = build(self)
        add_race(env)
        lp_legs, calls = OrderGateway.lp_legs, []

        def first_read_breaks(gateway):
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("legs_unreadable_once")
            return lp_legs(gateway)

        with patch.object(OrderGateway, "lp_legs", first_read_breaks):
            result = p2_run_cli(env)
        self.assertEnded(result, "exception", exit_code=5, released=False)
        self.assertIn(f"p2-run: RuntimeError after a submit in {MARKET}", result[2])
        self.assertEqual((len(placed(env)), cancelled(env)), (2, []))
        self.assertHandoff(result[2], *[o["bet_id"] for o in legs(env)])

    def test_open_not_armed_report_ends_at_once(self):
        env = build(self)
        add_race(env)
        at(env, OFF - 745 * S, lambda: env.exchange.add_order(selection_id=FAV, strategy_ref="p2-lp",
                                                              market_id=MARKET))       # a foreign p2-lp bet
        result = p2_run_cli(env)
        self.assertEnded(result, "not_armed:unresolved_or_foreign_orders", exit_code=5, released=False)
        self.assertEqual(placed(env), [])
        self.assertTrue(env.journal.fold(now_ns=env.clock.time_ns())["halted"])

    def test_back_raise_end_or_next_market(self):
        env = build(self)
        add_race(env)
        at(env, OFF - 741 * S, lambda: env.router.next_response.__setitem__(
            sp.BOOK, [support.rpc_error_response(-32602, "DSC-0018")]))
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["codes"], placed(env)), ({"book_fetch_failed": 1}, []))
        env = build(self)
        add_race(env)
        add_race(env, "1.235", OFF + 900 * S)

        def not_armed(gateway, *args, **kwargs):
            raise NotArmed("journal_anchor_mismatch")

        with patch.object(OrderGateway, "lp_place", not_armed):
            result = p2_run_cli(env)
        self.assertEnded(result, "back_raised:journal_anchor_mismatch", exit_code=0, released=True)
        self.assertEqual((placed(env), env.router.reads_of(sp.CATALOGUE, "1.235")), ([], []))

    def test_lay_raise_unsent_pulls_back(self):
        env = build(self)
        add_race(env)
        lp_place = OrderGateway.lp_place

        def lay_breaks(gateway, market_id, side, **kwargs):
            if side == "LAY":
                raise RuntimeError("before_any_lay_append")
            return lp_place(gateway, market_id, side, **kwargs)

        with patch.object(OrderGateway, "lp_place", lay_breaks):
            result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        (back,) = legs(env)
        self.assertEqual([c["instructions"][0]["betId"] for c in cancelled(env)], [back["bet_id"]])
        self.assertEqual([recorded_ns(e) - OFF for e in submits(env, "cancel")], [-590 * S])


class Arm(P2RunCase):
    def assertRefused(self, env, result, code):
        exit_, out, err, summary = result
        self.assertEqual(exit_, 2, err)
        self.assertIn("Betfair order refused: p2_arm_refused:", err)
        self.assertIn(code, err.strip().splitlines()[-1].split(":", 2)[-1].split(","))
        self.assertEqual((summary, env.confirmer.prompts, env.popen.calls), (None, [], []))
        self.assertEqual(sr.credential_reads(env), [])
        self.assertEqual(env.transport.requests, [])
        self.assertEqual(admission.admission_status(env.paths)["admission"], None)

    def test_arm_refusals_each_code(self):
        def ssh(env):
            env.environ["SSH_CONNECTION"] = "192.168.50.2 50000 192.168.50.88 22"

        def dirty(env):
            env.git.dirty = True

        def unknown(env):
            env.git.fail = True

        def armed_today(env):
            arm_record(env, until_ns=sp.UNTIL, races=1)

        def kill(env):
            engage(env.paths, "agent", clock=env.clock)

        def pilot_1(env):
            session_start(env, until_ns=env.clock.time_ns() - 60 * S)

        def bet_open(env):
            manual(env)

        def loosened(env):
            config = json.loads(env.paths.config_p2_path.read_text(encoding="utf-8"))
            config["orders"]["caps"]["max_daily_loss"] = 30.0
            env.paths.config_p2_path.write_text(json.dumps(config), encoding="utf-8")
            env.p2 = cfg.load_config_p2(paths=env.paths)              # its own token

        def pilot_armed_file(env):
            env.paths.pilot_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            pilot_run.armed_session_path(env.paths).write_text("{}", encoding="utf-8")

        cases = {"p2_over_ssh": (ssh, {}), "p2_tree_dirty": (dirty, {}), "p2_code_unknown": (unknown, {}),
                 "p2_arm_already_today": (armed_today, {}), "kill_file_engaged": (kill, {}),
                 "p2_pilot_1_day": (pilot_1, {}), "p2_other_bet_open": (bet_open, {}),
                 "p2_config_max_daily_loss": (loosened, {}), "pilot_session_armed": (pilot_armed_file, {}),
                 "p2_until_invalid": (None, {"until": "0955"}), "p2_races_invalid": (None, {"races": 6})}
        for code, (prepare, options) in cases.items():
            with self.subTest(code=code):
                env = build(self)
                add_race(env)
                if prepare is not None:
                    prepare(env)
                env.confirmer.prompts.clear()
                env.transport.requests.clear()
                env.runner.calls.clear()
                head = len(events(env))
                self.assertRefused(env, p2_run_cli(env, **options), code)
                self.assertEqual(len(events(env)), head)
        # The line itself: anything but exactly the ARM line refuses, nothing journaled, nothing read.
        env = build(self)
        add_race(env)
        env.confirmer.lines = ["P2 ARM wrong"]
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, arm_events(env), env.popen.calls), (2, None, [], []))
        self.assertIn("operator_echo_mismatch", err)
        self.assertEqual(sr.credential_reads(env), [])

    def test_ssh_refused_before_any_keychain_or_git_read(self):
        env = build(self)
        add_race(env)
        env.environ["SSH_TTY"] = "/dev/ttys003"
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: p2_arm_refused:p2_over_ssh"))
        self.assertEqual((env.runner.calls, env.git.calls, env.confirmer.prompts, summary), ([], [], [], None))
        self.assertEqual(env.transport.requests, [])

    def test_login_cap_at_arm_and_pre_open(self):
        env = build(self)
        add_race(env)
        with patch.object(pilot_run, "cap_blocks", return_value=True):
            self.assertRefused(env, p2_run_cli(env), "p2_login_cap")
        # Reached after the ARM: the race is skipped before its open, with no live-key read.
        env = build(self)
        add_race(env)
        with patch.object(pilot_run, "cap_blocks", lambda config, paths, *, clock: clock.time_ns() >= OFF - 745 * S):
            result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual(result[3]["codes"], {"login_cap": 1})
        self.assertEqual((live_key_requests(env), placed(env)), ([], []))

    def test_first_session_races_1(self):
        env = build(self)
        add_race(env)
        self.assertRefused(env, p2_run_cli(env, races=2), "p2_races_invalid")
        result = p2_run_cli(env, races=1)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        banner = env.confirmer.prompts[0]["banner"]
        self.assertIn("Races so far: 0 today, 0 of 10 in total; this session may run 1 until 2000Z", banner)
        # Review round (banner-1): the page's banner items (Arming a session): cfg12, commit, stops, warnings.
        for text in (f"config-p2 {cfg.config_sha256(env.p2)[:12]}", f"code commit {sp.COMMIT[:12]}",
                     "the Betfair phone app", "Ctrl-C here", "the KILL file", f"own-scope kill from {p2_run.CHECKOUT}",
                     "typed cancel fails", "the session ends after any race with a matched leg", "Nothing heavy runs",
                     "mains power with the lid open"):
            with self.subTest(text=text):
                self.assertIn(text, banner)
        # With one race run, the next day may ask for more (at most 5, and 10 in all).
        sr.set_day(env, 1, before_off_s=900)
        code, _, err, summary = p2_run_cli(env, races=6)
        self.assertEqual(code, 2)
        self.assertIn("p2_races_invalid", err)
        result = p2_run_cli(env, races=5)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual([e["payload"]["p2_arm"]["races"] for e in arm_events(env)], [1, 5])
        self.assertIn("Races so far: 0 today, 1 of 10 in total; this session may run 5", env.confirmer.prompts[-1]
                      ["banner"])

    def test_caffeinate_started_and_ended(self):
        env = build(self)
        add_race(env)
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        ((argv, kwargs, seen),) = env.popen.calls
        self.assertEqual(argv, ["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())])
        self.assertEqual(kwargs, {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL})
        self.assertNotIn("operator_confirm", seen)                     # after the line, before the ARM record
        self.assertEqual(env.popen.events, ["terminate", ("wait", 5)])
        # It cannot start: refused before the ARM record, and this admission released.
        env = build(self)
        add_race(env)
        env.popen.fail = OSError(2, "no caffeinate")
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, arm_events(env)), (2, None, []))
        self.assertIn("Betfair order refused: p2_caffeinate_failed", err)
        self.assertIn("recording released (generation 1)", err)

    def test_arm_record_under_gateway_lock(self):
        env = build(self)
        add_race(env)
        original = OrderJournal.append
        held = []

        def append(journal, kind, payload, **columns):
            if kind == "operator_confirm":
                try:
                    with file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock):
                        held.append(False)
                except LockBusy:
                    held.append(True)
                held.append(columns.get("expect_head"))
            return original(journal, kind, payload, **columns)

        with patch.object(OrderJournal, "append", append):
            result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)    # the opens then took the lock
        before = [e for e in events(env) if e["kind"] != "operator_confirm"][0]
        self.assertEqual(held, [True, {"seq": before["seq"], "last_sha256": before["event_sha256"]}])
        # A head moved after Yu saw it refuses the record, and the admission is released.
        env = build(self)
        add_race(env)
        confirm = env.confirmer.confirm_line

        def typed_then_moved(expected, *, banner):
            confirm(expected, banner=banner)
            sp.other_writer(env).append("note", {"reason": "another_writer"})

        env.confirmer.confirm_line = typed_then_moved
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, arm_events(env)), (2, None, []))
        self.assertIn("Betfair order refused: journal_changed_during_arm", err)
        self.assertIn("recording released (generation 1)", err)

    def test_commit_moved_since_the_banner_refuses_the_arm(self):
        # Review round (code-identity-1): the checkout moves to another clean commit while Yu types the line. The ARM
        # refuses before any Keychain read or record, and the admission is released.
        env = build(self)
        add_race(env)
        confirm = env.confirmer.confirm_line

        def typed_then_pulled(expected, *, banner):
            confirm(expected, banner=banner)
            env.git.commit = "d" * 40

        env.confirmer.confirm_line = typed_then_pulled
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, arm_events(env), env.popen.calls), (2, None, [], []))
        self.assertIn("Betfair order refused: p2_code_changed", err)
        self.assertIn("recording released (generation 1)", err)
        self.assertEqual((sr.credential_reads(env), env.transport.requests), ([], []))

    def test_owner_lock_blocks_pilot_run_and_manual_place(self):
        env = build(self)
        add_race(env)
        seen = {}

        def others():
            try:
                with admission.live_ownership(env.paths, owner="pilot-session:s20270104-0a1b2c3d",
                                              purpose="RS-19 pilot p1 20270104", config=env.config, clock=env.clock,
                                              processes=FakeProcesses(pid=2)):
                    seen["pilot"] = "admitted"
            except admission.OwnershipRefused as exc:
                seen["pilot"] = exc.code
            token = expected_token(env.config, env.clock.time_ns())
            seen["place"] = run_main(["place", "--mode", "live", "--confirm-live", token, "--market", "1.1",
                                      "--selection", "11", "--side", "BACK", "--price", "3.5", "--size", "2"],
                                     dict(deps(env), processes=FakeProcesses(pid=3)))

        at(env, OFF - 400 * S, others)                                  # legs resting
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        self.assertEqual(seen["pilot"], "owner_live")
        code, _, err = seen["place"]
        self.assertEqual(code, 2)
        self.assertIn("Betfair order refused: owner_live", err)
        self.assertEqual(len(placed(env)), 2)                           # the manual BACK never went


class SwitchedOff(unittest.TestCase):
    """The committed default (IN_FORCE False, note §1): p2-run refuses at its very top, before the SSH check and any
    Keychain, journal or git read; p2-summary (offline, read only) still works."""

    def setUp(self):
        fake_git(self)

    def test_p2_run_refused_before_any_read_and_p2_summary_works(self):
        self.assertIs(p2_lp.IN_FORCE, False)
        env = build(self)
        add_race(env)
        env.environ["SSH_TTY"] = "/dev/ttys003"                      # the SSH check, were it reached, would refuse
        head = len(events(env))
        connect = Mock(wraps=sqlite3.connect)
        with patch.object(sqlite3, "connect", connect):
            code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: p2_not_in_force"))
        self.assertEqual(connect.call_args_list, [])                    # no journal read
        self.assertEqual((env.runner.calls, env.git.calls, env.confirmer.prompts, env.popen.calls, summary),
                         ([], [], [], [], None))                         # no /usr/bin/security or git call
        self.assertEqual((env.transport.requests, len(events(env))), ([], head))
        self.assertEqual(admission.admission_status(env.paths)["admission"], None)
        code, out, err = run_main(["p2-summary", "--mode", "live"], deps(env))
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("total: sessions 0, races 0;"), out)


class Summary(P2RunCase):
    def test_p2_summary_offline_and_cleared_mismatch(self):
        env = build(self)
        add_race(env)
        fill(env)
        self.assertEnded(p2_run_cli(env), "matched_leg", exit_code=0, released=True)
        arm = arm_events(env)[0]
        code, out, err = run_main(["p2-summary", "--mode", "live"], deps(env))
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("total: sessions 1, races 1; legs sent BACK 1, LAY 1; placed BACK 1, LAY 1;"
                                            " refused or failed BACK 0, LAY 0 (codes: none); matched 2 (2 in full); "
                                            "round trips 1, lone BACKs 0, lone LAYs 0;"), lines[0])
        self.assertTrue(lines[0].endswith("; races left 9"))
        self.assertTrue(lines[1].startswith("2027-01-04: sessions 1, races 1;"))
        self.assertIn("2 p2-lp legs are open or unsettled; the next ARM refuses until each is cancelled or settled "
                      "(settle-market after its result).", out)
        self.assertNotIn("cleared", out)
        reads = len(env.transport.requests)
        # --cleared: Betfair's figures for p2-lp grouped by MARKET since the first ARM record, on the delayed key.
        env.exchange.cleared["SETTLED"].append({"marketId": "1.999", "profit": 1.5, "commission": 0.08,
                                                "customerStrategyRef": "p2-lp", "settledDate": utc_from_ns(OFF),
                                                "_group": "MARKET"})
        code, out, err = run_main(["p2-summary", "--mode", "live", "--cleared"], deps(env))
        self.assertEqual(code, 0, err)
        self.assertIn("cleared 1.999: profit £1.50, commission £0.08", out)
        self.assertIn("check the website: 1.999 is cleared but has no matched p2-lp leg in the journal", out)
        self.assertIn(f"check the website: {MARKET} has a matched p2-lp leg but no cleared figure", out)
        self.assertIn("cleared total: profit £1.50, commission £0.08", out)
        (request,) = [r for r in env.transport.requests[reads:] if b"listClearedOrders" in (r["body"] or b"")]
        params = json.loads(request["body"])["params"]
        self.assertEqual((params["groupBy"], params["customerStrategyRefs"], params["betStatus"]),
                         ("MARKET", ["p2-lp"], "SETTLED"))
        self.assertEqual(pilot.parse_utc_ns(params["settledDateRange"]["from"]), recorded_ns(arm))
        self.assertEqual(support.FakeHttpTransport.header_text(request, "X-Application"),
                         "synthetic-delayed-key-0001")
        self.assertEqual(len(env.router.order_requests), 2)             # the race's two legs only

    def test_cleared_refuses_a_read_slot_other_than_delayed(self):
        # Review round (f46-summary-1): config-p2 edited to read with the live key. --cleared refuses before any
        # Keychain read or login (F46: the live key only in a placing session).
        env = build(self)
        arm_record(env, until_ns=sp.UNTIL, races=1)
        config = json.loads(env.paths.config_p2_path.read_text(encoding="utf-8"))
        config["read"]["app_key_slot"] = "live"
        env.paths.config_p2_path.write_text(json.dumps(config), encoding="utf-8")
        env.transport.requests.clear()
        code, out, err = run_main(["p2-summary", "--mode", "live", "--cleared"], deps(env))
        self.assertEqual(code, 2, err)
        self.assertIn("Betfair order refused: p2_cleared_needs_delayed_key", err)
        self.assertEqual((env.runner.calls, env.transport.requests), ([], []))


if __name__ == "__main__":
    unittest.main()
