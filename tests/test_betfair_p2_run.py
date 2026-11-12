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

Edges with no test here: a list row that cannot be read, or the screen's own row missing; the handoff when the legs
cannot be listed at all, and a pull that raises inside the EXCEPTION edge; a stop between the Keychain read and the ARM
record; an admission refusal at the owner lock; and a terminal that is lost.
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
from betangel_research.betfair.credentials import anchor_read
from betangel_research.betfair.errors import CredentialError, JournalError, LockBusy, NotArmed
from betangel_research.betfair.gateway import OrderGateway
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.paths import FORBID_NETWORK_ENV, file_lock
from betangel_research.betfair.receipts import utc_from_ns
from betangel_research.betfair.risk import RiskGuard

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
import test_betfair_support_run as sr
from test_betfair_admission import FakeProcesses
from test_betfair_p2_gateway import (account, arm_record, fake_git, fill, in_force, leg, manual, no_children,
                                     open_p2, p2_orders, race, refuse_at_exchange, session_start, set_time, settle,
                                     states)
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


def exchange_order(env, side, market_id=MARKET):
    """The simulated exchange's own record of our resting leg of side."""
    return next(order for order in env.exchange.orders.values()
                if order["marketId"] == market_id and order["side"] == side)


def edit_config_p2(env, change):
    """config-p2.json rewritten with change(config) applied, and reloaded (env.p2: its sha256, so its own token)."""
    config = json.loads(env.paths.config_p2_path.read_text(encoding="utf-8"))
    change(config)
    env.paths.config_p2_path.write_text(json.dumps(config), encoding="utf-8")
    env.p2 = cfg.load_config_p2(paths=env.paths)


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
        # The terminal while the legs rest: one line after both sends, with the market and the pull's due time only.
        resting = f"{MARKET}: BACK and LAY placed; resting, pull due 09:55:00Z\n"
        self.assertEqual(result[2].count(resting), 1)
        self.assertLess(result[2].index(resting), result[2].index(f"{MARKET}: next\n"))
        for order in legs(env):
            self.assertNotIn(order["bet_id"], result[2])
            self.assertNotIn(order["customer_order_ref"], result[2])
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
        # Its start-time bounds in the millisecond shape every other Betfair date range of the suite is sent in.
        self.assertEqual(listed["params"]["filter"]["marketStartTime"],
                         {"from": "2027-01-04T09:45:00.000Z", "to": "2027-01-04T20:05:00.000Z"})
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

    def test_race_too_close_after_the_one_before_is_missed(self):
        # Day 2, RACES 2, two races a minute apart: the first holds the bot to its pull at off -300 s, by when the
        # second's screen window (off -840 to -780 s) has passed. It is a SKIP with nothing read for it.
        env = build(self)
        add_race(env)
        self.assertEnded(p2_run_cli(env), "races_done", exit_code=0, released=True)    # day 1: one race, both legs
        sr.set_day(env, 1, before_off_s=900)
        add_race(env, "1.300", OFF + pilot.DAY_NS)
        add_race(env, "1.301", OFF + pilot.DAY_NS + 60 * S)
        result = p2_run_cli(env, races=2)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["races"], result[3]["screened"], result[3]["codes"]), (1, 1, {"missed_window": 1}))
        self.assertIn("1.301: next (missed_window)\n", result[2])
        self.assertEqual((env.router.reads_of(sp.CATALOGUE, "1.301"), env.router.reads_of(sp.BOOK, "1.301")), ([], []))

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
        self.assertIn(f"{MARKET}: next (spread_out_of_band)\n", result[2])     # the code with its market, at once
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
        # The exchange refuses the LAY (FAILED); the LAY's fresh book shows the runner gone, or the best back fallen
        # since the BACK was priced (choice codes); a risk rule refuses it (R11: the funds left after the BACK).
        codes = {"exchange": "MARKET_NOT_OPEN_FOR_BETTING", "choice": "runner_not_active", "moved": "p2_book_moved",
                 "risk": "funds"}
        for case, code in codes.items():
            with self.subTest(case=case):
                env = build(self)
                market = add_race(env)

                def back_then(params):
                    env.exchange.place_handler = None
                    result = env.exchange.place(params)
                    if case == "exchange":
                        env.exchange.place_handler = refuse_at_exchange
                    elif case == "choice":
                        market.statuses[FAV] = "REMOVED"
                    elif case == "moved":
                        market.backs[FAV] = (("2.98", "15.00"),)       # 3.00 when the BACK was priced
                    else:
                        account(env, 6.09)                              # a penny short of the LAY's £4.10 on top
                    return result

                env.exchange.place_handler = back_then
                result = p2_run_cli(env)
                self.assertEnded(result, "races_done", exit_code=0, released=True)
                back = legs(env)[0]
                self.assertEqual([c["instructions"][0]["betId"] for c in cancelled(env)], [back["bet_id"]])
                self.assertEqual([recorded_ns(e) - OFF for e in submits(env, "cancel")], [-590 * S])
                self.assertEqual(len(placed(env)), 2 if case == "exchange" else 1)
                # The LAY's code is counted and printed with its market, after the pull; a choice code is journaled
                # nowhere. Codes only: no bet id and no ref.
                self.assertEqual(result[3]["codes"], {f"lay:{code}": 1})
                self.assertIn(f"{MARKET}: next (LAY not placed {code})\n", result[2])
                self.assertNotIn(back["bet_id"], result[2])
                self.assertNotIn(back["customer_order_ref"], result[2])

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
        self.assertEqual(result[3]["codes"], {"lay:exception_unknown_outcome": 1})
        self.assertIn(f"{MARKET}: lay_unresolved (LAY unresolved exception_unknown_outcome)\n", result[2])

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
        # A cancel the gateway refuses (nothing sent for that leg) is named with its rule; the other leg's still goes.
        env = build(self)
        add_race(env)
        refused = []

        def cancel(gateway, *args, **kwargs):
            if not refused:
                refused.append(True)
                return {"intent_id": None, "mode": "live", "decision": "refused",
                        "refusal": {"rule": "halted", "detail": {}}, "orders": [], "journal_seq": None}
            return original(gateway, *args, **kwargs)

        with patch.object(OrderGateway, "cancel", cancel):
            result = p2_run_cli(env)
        self.assertEnded(result, "pull_incomplete", exit_code=5, released=False)
        self.assertIn(f"{MARKET} pull: cancel_refused:halted", result[2])
        back, lay = legs(env)
        self.assertEqual((back["state"], lay["state"], len(cancelled(env))), ("EXECUTION_COMPLETE", "EXECUTABLE", 1))
        self.assertHandoff(result[2], lay["bet_id"])

    def test_suspended_at_pull(self):
        # Which report Betfair sends for a cancel in a suspended market is unverified, so each shape is run: the
        # execution report's MARKET_SUSPENDED with no instruction reports; with instruction reports that say
        # ERROR_IN_ORDER (the shape Pilot 1's tests name for a place); and with instruction reports that carry
        # MARKET_SUSPENDED themselves. Every shape ends the same for the legs (both EXECUTABLE, the handoff, one
        # cancel each). What differs is the shared journal: the second adds two UNKNOWNs toward the three-UNKNOWN
        # HALT, and the third HALTs, because that code is not in the error table for an instruction report
        # (unrecognized), so Pilot 1 waits for RESUME LIVE too.
        def suspended(instruction_code):
            def handler(params):
                reports = ([] if instruction_code is None else
                           [{"status": "FAILURE", "errorCode": instruction_code, "instruction": i}
                            for i in params["instructions"]])
                return {"customerRef": params["customerRef"], "status": "FAILURE", "errorCode": "MARKET_SUSPENDED",
                        "marketId": params["marketId"], "instructionReports": reports}
            return handler

        shapes = {None: (False, [], 0), "ERROR_IN_ORDER": (False, [], 2),
                  "MARKET_SUSPENDED": (True, ["unrecognized", "unrecognized"], 2)}
        for instruction_code, expected in shapes.items():
            with self.subTest(instruction_code=instruction_code):
                env = build(self)
                add_race(env)
                env.exchange.cancel_handler = suspended(instruction_code)
                result = p2_run_cli(env)
                self.assertEnded(result, "pull_incomplete", exit_code=5, released=False)
                back, lay = legs(env)
                self.assertEqual((back["state"], lay["state"]), ("EXECUTABLE", "EXECUTABLE"))
                self.assertEqual(len(cancelled(env)), 2)               # one cancel each, never a second
                self.assertHandoff(result[2], back["bet_id"], lay["bet_id"])
                state = env.journal.fold(now_ns=env.clock.time_ns())
                self.assertEqual((state["halted"], state["halt_reasons"], state["consecutive_unknown"]), expected)

    def test_leg_cancelled_by_the_exchange_in_the_rest(self):
        # A late non-runner: Betfair cancels our unmatched LAY at off -400 s (its rule, and how it then lists such a
        # bet, are unverified; three listings are run). The rest makes no Betfair call, so the bot is not told. At
        # the pull the LAY's cancel comes back BET_TAKEN_OR_LAPSED (UNKNOWN) and the in-line reconcile cannot show the
        # bet gone under the live evidence rule. The LAY is handed off although nothing of it is open, the BACK is
        # still pulled, and the session ends with exit 5 and no HALT. The next ARM refuses with the codes below. The
        # ways out are Yu's typed lines and are not run here: ACCEPT ABSENT for a leg left absent, settle-market
        # after the result for a leg kept EXECUTABLE at its worst case.
        def gone(env, order):
            del env.exchange.orders[order["betId"]]

        def listed_cancelled(env, order):
            order.update(status="EXECUTION_COMPLETE", sizeCancelled=order["sizeRemaining"], sizeRemaining=0.0)

        def cleared_only(env, order):
            del env.exchange.orders[order["betId"]]
            env.exchange.cleared["CANCELLED"].append(
                {"betId": order["betId"], "marketId": MARKET, "selectionId": order["selectionId"], "handicap": 0.0,
                 "side": "LAY", "priceRequested": order["priceSize"]["price"], "sizeCancelled": order["sizeRemaining"],
                 "customerOrderRef": order["customerOrderRef"], "customerStrategyRef": "p2-lp",
                 "settledDate": env.clock.utc_now(), "betCount": 1})

        # how Betfair lists it -> the LAY's end state and code, the BACK's cancel (s from the off; 15 s late after
        # a reconcile that finds nothing listed), and what refuses the next ARM
        cases = {gone: ("ABSENT_UNCONFIRMED", "BET_TAKEN_OR_LAPSED", -285,
                        ["order_absent_unconfirmed", "market_unresolved", "p2_other_bet_open"]),
                 listed_cancelled: ("EXECUTABLE", "exchange_reduction_withheld_until_settle", -300,
                                    ["order_executable", "p2_other_bet_open"]),
                 cleared_only: ("EXECUTABLE", "cleared_row_bet_exists_worst_case", -285,
                                ["order_executable", "p2_other_bet_open"])}
        for cancel, (lay_state, lay_code, back_cancel_s, next_arm) in cases.items():
            with self.subTest(listing=cancel.__name__):
                env = build(self)
                add_race(env)
                at(env, OFF - 400 * S, lambda: cancel(env, exchange_order(env, "LAY")))
                result = p2_run_cli(env)
                self.assertEnded(result, "pull_incomplete", exit_code=5, released=False)
                back, lay = legs(env)
                self.assertEqual((lay["state"], lay["error_code"], lay["size_matched_p"]), (lay_state, lay_code, 0))
                self.assertEqual((back["state"], back["size_cancelled_p"]), ("EXECUTION_COMPLETE", 200))
                self.assertEqual([c["instructions"][0]["betId"] for c in cancelled(env)],
                                 [lay["bet_id"], back["bet_id"]])
                self.assertEqual([(recorded_ns(e) - OFF) // S for e in submits(env, "cancel")], [-300, back_cancel_s])
                self.assertHandoff(result[2], lay["bet_id"])                # for a leg that is already gone
                self.assertNotIn(f"p2-lp leg {back['bet_id']}", result[2])
                state = env.journal.fold(now_ns=env.clock.time_ns())
                self.assertEqual((state["halted"], state["halt_reasons"], state["consecutive_unknown"]),
                                 (False, [], 0))                            # the BACK's clean cancel resets the count
                sr.set_day(env, 1, before_off_s=900)
                code, _, err, _ = p2_run_cli(env)
                self.assertEqual((code, err.strip().splitlines()[-1]),
                                 (2, "Betfair order refused: p2_arm_refused:" + ",".join(next_arm)))

    def test_back_failed_moves_on_with_no_handoff(self):
        env = build(self)
        add_race(env)

        def back_fails(params):
            env.exchange.place_handler = None
            return refuse_at_exchange(params)

        env.exchange.place_handler = back_fails
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)          # a race from its first submit
        (back,) = legs(env)
        self.assertEqual((back["state"], len(placed(env)), cancelled(env), result[3]["races"]), ("FAILED", 1, [], 1))
        self.assertNotIn("These legs may still be open", result[2])
        self.assertIn(f"{MARKET}: next (BACK failed MARKET_NOT_OPEN_FOR_BETTING)\n", result[2])

    def test_leg_matched_in_the_rest_is_found_at_the_pull(self):
        # The rest makes no Betfair call: a leg that matched while resting is found by its own cancel at the pull.
        for sides in (("LAY",), ("BACK", "LAY")):
            with self.subTest(matched=sides):
                env = build(self)
                add_race(env)
                add_race(env, "1.235", OFF + 900 * S)

                def match():
                    for side in sides:
                        order = exchange_order(env, side)
                        order.update(status="EXECUTION_COMPLETE", sizeMatched=order["priceSize"]["size"],
                                     sizeRemaining=0.0, averagePriceMatched=order["priceSize"]["price"])

                at(env, OFF - 400 * S, match)
                result = p2_run_cli(env)
                self.assertEnded(result, "matched_leg", exit_code=0, released=True)
                self.assertEqual([(o["side"], o["state"], o["size_matched_p"]) for o in legs(env)],
                                 [("BACK", "EXECUTION_COMPLETE", 200 if "BACK" in sides else 0),
                                  ("LAY", "EXECUTION_COMPLETE", 200)])
                self.assertEqual([(recorded_ns(e) - OFF) // S for e in submits(env, "cancel")], [-300, -300])
                self.assertIn(f"{MARKET}: a p2-lp leg matched; after its result run settle-market --market {MARKET}",
                              result[2])
                self.assertNotIn("These legs may still be open", result[2])
                self.assertEqual(env.router.reads_of(sp.CATALOGUE, "1.235"), [])      # the session ended

    def test_matched_leg_ends_session_with_settle_line(self):
        env = build(self)
        add_race(env)
        add_race(env, "1.235", OFF + 900 * S)
        fill(env)                                                       # both legs matched at once: a round trip
        result = p2_run_cli(env)
        self.assertEnded(result, "matched_leg", exit_code=0, released=True)
        self.assertIn(f"{MARKET}: a p2-lp leg matched; after its result run settle-market --market {MARKET}. Type "
                      f"it on the race's own UTC day if you can: the booking falls on the day it is typed, and more "
                      f"than £10 booked on a later day refuses that day's P2 ARM (p2_pair_precheck:daily_loss). The "
                      f"next P2 ARM refuses until it is settled.\n", result[2])
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

    def test_ctrl_c_during_the_lay_refuses_it(self):
        # Review round (time-4): Ctrl-C after BETWEEN's look, inside the LAY's lp_place. The signal is deferred (the
        # BACK was sent). In the LAY's funds read, the pair check sees it first and refuses the LAY before any intent
        # (safety-01); in the LAY's first-byte gate, a stop that came after that check, the LAY is FAILED with nothing
        # sent. Either way the BACK is pulled and the session ends.
        funds, send_bounds = OrderGateway._funds, OrderGateway._send_bounds
        for where in ("funds_read", "first_byte"):
            with self.subTest(where=where):
                env = build(self)
                add_race(env)
                calls = []

                def fire_on_the_lays(gateway, calls=calls):
                    calls.append(True)
                    if len(calls) == 2:
                        sr.fire()
                    return funds(gateway)

                def bounds(gateway, book, *, pilot=None, calls=calls):
                    send_by, gate = send_bounds(gateway, book, pilot=pilot)
                    calls.append(True)
                    if len(calls) != 2:                         # the LAY's is the second place send
                        return send_by, gate

                    def lays_gate():
                        sr.fire()
                        return gate()

                    return send_by, lays_gate

                patched = (patch.object(OrderGateway, "_funds", fire_on_the_lays) if where == "funds_read"
                           else patch.object(OrderGateway, "_send_bounds", bounds))
                with patched:
                    result = p2_run_cli(env)
                self.assertEnded(result, "stopped", exit_code=0, released=True)
                self.assertEqual((result[3]["stop_requested"], result[3]["signal"]), (True, "SIGINT"))
                found = legs(env)
                back = found[0]
                self.assertEqual((len(placed(env)), [c["instructions"][0]["betId"] for c in cancelled(env)],
                                  back["side"], back["state"]), (1, [back["bet_id"]], "BACK", "EXECUTION_COMPLETE"))
                exceptions = [e["payload"]["exception"]["code"] for e in events(env)
                              if e["kind"] == "report" and e["payload"].get("exception")]
                if where == "funds_read":
                    self.assertEqual((len(found), len(submits(env, "place")), result[3]["codes"]),
                                     (1, 1, {"lay:p2_stop_before_send": 1}))
                    self.assertNotIn("p2_stop_before_send", exceptions)
                    self.assertIn(f"{MARKET}: stopped (LAY not placed p2_stop_before_send)\n", result[2])
                else:
                    lay = found[1]
                    self.assertEqual((lay["state"], lay["error_code"], result[3]["codes"]),
                                     ("FAILED", "transport_not_sent", {"lay:transport_not_sent": 1}))
                    self.assertIn("p2_stop_before_send", exceptions)

    def test_ctrl_c_held_by_the_sends_own_mask_refuses_the_leg(self):
        # A real SIGINT that lands after a leg's send has blocked signals (pthread_sigmask) and before its first
        # byte: it stays pending at the OS, no Python handler runs, and neither the stop flag nor the guard's list
        # shows it. The first-byte gate reads the pending set too, so the leg is not sent. The signal is raised in
        # this process only, while the gateway's own mask holds it; it is delivered to the open's guard afterwards.
        # The LAY: the BACK is pulled and the session ends. The BACK: it is FAILED transport_not_sent and counts as a
        # race, as any FAILED BACK does, so RACES 1 ends races_done with nothing sent (note §8, Signals).
        send_bounds = OrderGateway._send_bounds
        for side, send in (("LAY", 2), ("BACK", 1)):
            with self.subTest(side=side):
                env = build(self)
                add_race(env)
                calls, seen = [], {}

                def bounds(gateway, book, *, pilot=None, calls=calls, seen=seen, send=send):
                    send_by, gate = send_bounds(gateway, book, pilot=pilot)
                    calls.append(True)
                    if len(calls) != send:                      # the BACK's is the first place send, the LAY's the second
                        return send_by, gate

                    def legs_gate():
                        seen["blocked"] = signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, set())
                        signal.raise_signal(signal.SIGINT)
                        seen["pending"] = signal.SIGINT in signal.sigpending()
                        seen["gate"] = gate()
                        return seen["gate"]

                    return send_by, legs_gate

                with patch.object(OrderGateway, "_send_bounds", bounds):
                    result = p2_run_cli(env)
                self.assertEqual(seen, {"blocked": True, "pending": True, "gate": "p2_stop_before_send"})
                self.assertEqual((result[3]["stop_requested"], result[3]["signal"]), (True, "SIGINT"))
                self.assertNotIn(signal.SIGINT, signal.sigpending())
                if side == "LAY":
                    self.assertEnded(result, "stopped", exit_code=0, released=True)
                    back, lay = legs(env)
                    self.assertEqual(([p["instructions"][0]["side"] for p in placed(env)],
                                      [c["instructions"][0]["betId"] for c in cancelled(env)]),
                                     (["BACK"], [back["bet_id"]]))
                    self.assertEqual((back["state"], lay["state"], lay["error_code"]),
                                     ("EXECUTION_COMPLETE", "FAILED", "transport_not_sent"))
                else:
                    self.assertEnded(result, "races_done", exit_code=0, released=True)
                    (back,) = legs(env)
                    self.assertEqual((back["side"], back["state"], back["error_code"], result[3]["races"],
                                      placed(env), cancelled(env)),
                                     ("BACK", "FAILED", "transport_not_sent", 1, [], []))
                    self.assertIn(f"{MARKET}: next (BACK failed transport_not_sent)\n", result[2])

    def test_ctrl_backslash_and_ctrl_z_inside_the_back_act_as_ctrl_c(self):
        # safety-01: Ctrl-\ and Ctrl-Z are not the open's guard's signals, so inside the BACK's lp_place (here its
        # funds read) they set only the stop flag. The pair check asks for Yu's stop before any intent and refuses
        # the BACK (p2_stop_before_send): nothing of it is journaled, sent or counted as a race, and the session ends
        # stopped, as it does for Ctrl-C there (which the guard raises).
        funds = OrderGateway._funds
        for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
            with self.subTest(signal=sig.name):
                env = build(self)
                add_race(env)

                def fire_on_the_backs(gateway, sig=sig):
                    sr.fire(sig)
                    return funds(gateway)

                with patch.object(OrderGateway, "_funds", fire_on_the_backs):
                    result = p2_run_cli(env)
                self.assertEnded(result, "stopped", exit_code=0, released=True)
                # Ctrl-C there is the guard's SignalStop, which sets the flag but leaves it unnamed.
                self.assertEqual((result[3]["stop_requested"], result[3]["signal"], result[3]["races"],
                                  result[3]["codes"], placed(env), cancelled(env), legs(env)),
                                 (True, None if sig == signal.SIGINT else sig.name, 0, {}, [], [], []))
                self.assertIn(f"{MARKET}: stopped\n", result[2])

    def test_a_stop_held_in_the_inherited_signal_mask_ends_the_session(self):
        # safety-05: p2-run started with SIGINT blocked and one pending, so it is never delivered. The BACK's pair
        # check reads it (sigpending) and refuses with nothing journaled, and the flag is set as if it had been
        # delivered: the session ends stopped at the first race instead of refusing BACK after BACK.
        env = build(self)
        for n in range(3):
            add_race(env, f"1.{240 + n}", OFF + n * 900 * S)
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            signal.raise_signal(signal.SIGINT)
            self.assertIn(signal.SIGINT, signal.sigpending())
            result = p2_run_cli(env)
        finally:
            ignore = signal.signal(signal.SIGINT, lambda signum, frame: None)
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)     # the pending SIGINT goes to the no-op
            finally:
                signal.signal(signal.SIGINT, ignore)
        self.assertNotIn(signal.SIGINT, signal.sigpending())
        self.assertEnded(result, "stopped", exit_code=0, released=True)
        self.assertEqual((result[3]["stop_requested"], result[3]["signal"], result[3]["races"], result[3]["codes"],
                          placed(env), legs(env)), (True, "SIGINT", 0, {}, [], []))
        self.assertIn("1.240: stopped\n", result[2])

    def test_ctrl_backslash_and_ctrl_z_stop_as_ctrl_c(self):
        # SIGQUIT's default ends the process with no pull, and SIGTSTP's freezes it holding .gateway.lock. Pilot 2's
        # stop flag takes both: fired in a slice of the rest (at off -400 s), each pulls both legs at the next look
        # and ends the session.
        for sig in (signal.SIGQUIT, signal.SIGTSTP):
            with self.subTest(signal=sig.name):
                env = build(self)
                add_race(env)
                before, seen = signal.getsignal(sig), {}

                def fire():
                    seen["handler"] = signal.getsignal(sig)
                    if callable(seen["handler"]):
                        sr.fire(sig)

                at(env, OFF - 400 * S, fire)
                result = p2_run_cli(env)
                self.assertTrue(callable(seen["handler"]), seen)
                self.assertEnded(result, "stopped", exit_code=0, released=True)
                self.assertEqual((result[3]["stop_requested"], result[3]["signal"]), (True, sig.name))
                self.assertEqual([(recorded_ns(e) - OFF) // S for e in submits(env, "cancel")], [-399, -399])
                self.assertTrue(all(o["state"] == "EXECUTION_COMPLETE" and o["size_cancelled_p"] == 200
                                    for o in legs(env)))
                self.assertEqual(signal.getsignal(sig), before)          # the earlier handler is back
        self.assertEqual(pilot_run.StopFlag.SIGNALS, (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))   # Pilot 1's

    def test_sighup_in_the_rest_pulls(self):
        # A closed terminal: SIGHUP sets the same flag, the legs are pulled and the session ends with no line lost.
        env = build(self)
        add_race(env)
        at(env, OFF - 400 * S, lambda: sr.fire(signal.SIGHUP))
        result = p2_run_cli(env)
        self.assertEnded(result, "stopped", exit_code=0, released=True)
        self.assertEqual((result[3]["signal"], len(cancelled(env))), ("SIGHUP", 2))

    def test_stop_or_kill_file_in_idle_and_pre_open_ends_with_no_live_read(self):
        # IDLE (before the screen, which can be hours before the next race) and PRE-OPEN (between the screen and the
        # open): the 1 s slices see the stop flag and the KILL file themselves. Nothing more is read: before the
        # screen on either key, after it on the live key.
        stops = {"ctrl_c": (lambda env: sr.fire(), "stopped", True),
                 "kill_file": (lambda env: engage(env.paths, "agent", clock=env.clock), "kill_file", False)}
        for name, (action, code, released) in stops.items():
            for when_s, screened in ((-850, 0), (-760, 1)):
                with self.subTest(case=name, at_s=when_s):
                    env = build(self)
                    add_race(env)
                    at(env, OFF + when_s * S, lambda: action(env))
                    result = p2_run_cli(env)
                    self.assertEnded(result, code, exit_code=0 if released else 5, released=released)
                    self.assertEqual((result[3]["screened"], len(env.router.reads_of(sp.CATALOGUE, MARKET)),
                                      live_key_requests(env), placed(env)), (screened, screened, [], []))

    def test_ctrl_c_inside_the_back_with_nothing_submitted_ends_the_session(self):
        # Ctrl-C inside the BACK's lp_place, in its funds read: no order request has begun, so the open's guard raises
        # it. Nothing of the BACK was submitted: no leg, nothing to pull, and the session ends stopped.
        env = build(self)
        add_race(env)
        funds = OrderGateway._funds

        def fire_on_the_backs(gateway):
            sr.fire()
            return funds(gateway)

        with patch.object(OrderGateway, "_funds", fire_on_the_backs):
            result = p2_run_cli(env)
        self.assertEnded(result, "stopped", exit_code=0, released=True)
        self.assertEqual((result[3]["stop_requested"], result[3]["races"], placed(env), cancelled(env), legs(env)),
                         (True, 0, [], [], []))

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

    def test_skip_streak_counts_every_open_that_ends_with_no_order(self):
        # The streak limits live-key reads that end with no order (F46): a failed send-book read and a decision
        # window missed after the open count as a refused BACK does. Five in a row end the session; the sixth race
        # is never screened.
        open_report = OrderGateway.open_report
        for code in ("book_fetch_failed", "missed_window"):
            with self.subTest(code=code):
                env = build(self)
                for n in range(6):
                    add_race(env, f"1.{240 + n}", OFF + n * 900 * S)
                    if code == "book_fetch_failed":                          # after the screen: the live send book
                        at(env, OFF + n * 900 * S - 741 * S, lambda: env.router.next_response.setdefault(
                            sp.BOOK, []).append(support.rpc_error_response(-32602, "DSC-0018")))

                def opened(gateway):
                    if code == "missed_window":
                        env.clock.advance(170)                               # the open runs past off -575 s
                    return open_report(gateway)

                with patch.object(OrderGateway, "open_report", opened):
                    result = p2_run_cli(env)
                self.assertEnded(result, "skip_streak", exit_code=0, released=True)
                self.assertEqual((result[3]["codes"], result[3]["flagged"], result[3]["races"]), ({code: 5}, 5, 0))
                self.assertIn(f"1.244: skip_streak ({code})\n", result[2])
                self.assertEqual((env.router.reads_of(sp.CATALOGUE, "1.245"), placed(env)), ([], []))
        # Skips before the open make no live-key read and stay out of the streak: six unflagged races.
        env = build(self)
        for n in range(6):
            add_race(env, f"1.{240 + n}", OFF + n * 900 * S, lay="3.10")
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["codes"], live_key_requests(env)), ({"spread_out_of_band": 6}, []))

    def test_low_balance_ends_on_the_skip_streak_with_no_order_and_no_race(self):
        # £6.09 is a penny short of one pair at a LAY of 3.05 (£2 and £4.10). Each BACK is refused by the pair
        # pre-check before any intent: no order goes, no race is used, and the streak ends the session.
        env = build(self)
        add_race(env)
        self.assertEnded(p2_run_cli(env), "races_done", exit_code=0, released=True)    # day 1: one race, both legs
        sr.set_day(env, 1, before_off_s=900)
        for n in range(6):
            add_race(env, f"1.{300 + n}", OFF + pilot.DAY_NS + n * 600 * S)
        account(env, 6.09)
        sent = len(placed(env))
        result = p2_run_cli(env, races=5)
        self.assertEnded(result, "skip_streak", exit_code=0, released=True)
        self.assertEqual((result[3]["races"], result[3]["codes"]), (0, {"p2_pair_precheck:funds": 5}))
        self.assertIn("1.300: next (p2_pair_precheck:funds)\n", result[2])
        orders = env.journal.fold(now_ns=env.clock.time_ns())["orders"]
        self.assertEqual((len(placed(env)) - sent, len(p2_lp.races(orders)), len(legs(env))), (0, 1, 2))

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
        # Each market's line carries only its own skip code: the race after a skip prints no note.
        self.assertIn("1.303: next (p2_country)\n", result[2])
        self.assertIn("1.304: next\n", result[2])

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
        add_race(env, "1.235", OFF + 900 * S)
        lp_place = OrderGateway.lp_place

        def lay_breaks(gateway, market_id, side, **kwargs):
            if side == "LAY":
                raise RuntimeError("before_any_lay_append")
            return lp_place(gateway, market_id, side, **kwargs)

        with patch.object(OrderGateway, "lp_place", lay_breaks):
            result = p2_run_cli(env)
        # As a BACK that raised: the BACK is pulled at once, and the session ends with the code, released, before
        # any later market is opened. The type's name is printed, never the exception's text.
        self.assertEnded(result, "lay_raised:RuntimeError", exit_code=0, released=True)
        (back,) = legs(env)
        self.assertEqual([c["instructions"][0]["betId"] for c in cancelled(env)], [back["bet_id"]])
        self.assertEqual([recorded_ns(e) - OFF for e in submits(env, "cancel")], [-590 * S])
        self.assertEqual(result[3]["codes"], {"lay:RuntimeError": 1})
        self.assertIn(f"{MARKET}: lay_raised:RuntimeError (LAY not placed RuntimeError)\n", result[2])
        self.assertNotIn("before_any_lay_append", result[1] + result[2])
        self.assertEqual(env.router.reads_of(sp.CATALOGUE, "1.235"), [])

    def test_lay_book_read_failed_pulls_back_and_moves_on(self):
        # Only the LAY's send-book read failed (nothing of the LAY exists): the BACK is pulled and, as for the BACK's
        # own book, the session goes on.
        env = build(self)
        add_race(env)

        def back_then(params):
            env.exchange.place_handler = None
            result = env.exchange.place(params)
            env.router.next_response[sp.BOOK] = [support.rpc_error_response(-32602, "DSC-0018")]    # the LAY's book
            return result

        env.exchange.place_handler = back_then
        result = p2_run_cli(env)
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        (back,) = legs(env)
        self.assertEqual(([c["instructions"][0]["betId"] for c in cancelled(env)], len(placed(env))),
                         ([back["bet_id"]], 1))
        self.assertEqual(result[3]["codes"], {"lay:book_fetch_failed": 1})
        self.assertIn(f"{MARKET}: next (LAY not placed book_fetch_failed)\n", result[2])

    def test_market_list_or_screen_read_failed(self):
        # The day's one list read fails: the session ends, released, with nothing on the live key. A screen read
        # that fails is that market's SKIP.
        env = build(self)
        add_race(env)
        env.router.next_response[sp.CATALOGUE] = [support.rpc_error_response(-32602, "DSC-0018")]
        result = p2_run_cli(env)
        self.assertEnded(result, "markets_read_failed", exit_code=0, released=True)
        self.assertEqual((result[3]["markets"], live_key_requests(env), len(arm_events(env))), (0, [], 1))
        env = build(self)
        add_race(env)
        at(env, OFF - 841 * S, lambda: env.router.next_response.__setitem__(
            sp.BOOK, [support.rpc_error_response(-32602, "DSC-0018")]))          # the screen's book, delayed key
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual((result[3]["screened"], result[3]["flagged"], result[3]["codes"]),
                         (1, 0, {"screen_read_failed": 1}))
        self.assertIn(f"{MARKET}: next (screen_read_failed)\n", result[2])
        self.assertEqual(live_key_requests(env), [])

    def test_admission_lost_before_the_open_ends_the_session(self):
        env = build(self)
        add_race(env)

        def lost_by_the_open(paths, ticket, *, clock):
            return ["admission_lost"] if clock.time_ns() >= OFF - 745 * S else []

        with patch.object(pilot_run, "admission_intact", lost_by_the_open):
            result = p2_run_cli(env)
        self.assertEqual(result[3]["end_code"], "admission_lost", result[2])
        self.assertEqual((live_key_requests(env), placed(env)), ([], []))

    def test_exception_outside_a_race_keeps_recording_off(self):
        env = build(self)
        add_race(env)

        def broken(*args, **kwargs):
            raise RuntimeError("market_list_broke")

        with patch.object(p2_run, "markets", broken):
            result = p2_run_cli(env)
        self.assertEnded(result, "exception", exit_code=5, released=False)
        self.assertIn("p2-run: RuntimeError outside a race", result[2])
        self.assertIn("recording stays off until Yu's typed recording-release (generation 1)", result[2])
        self.assertNotIn("market_list_broke", result[1] + result[2])

    def test_open_that_raises_ends_the_session_with_nothing_sent(self):
        env = build(self)
        add_race(env)
        add_race(env, "1.235", OFF + 900 * S)

        def not_armed(gateway, *args, **kwargs):
            raise NotArmed("journal_anchor_mismatch")

        with patch.object(OrderGateway, "open", not_armed):
            result = p2_run_cli(env)
        self.assertEnded(result, "open_failed:journal_anchor_mismatch", exit_code=0, released=True)
        self.assertEqual((placed(env), env.router.reads_of(sp.CATALOGUE, "1.235")), ([], []))
        # A signal raised out of the open itself (its login) is Yu's stop: nothing was sent.
        env = build(self)
        add_race(env)

        def interrupted(gateway, *args, **kwargs):
            raise cli.SignalStop("SIGINT")

        with patch.object(OrderGateway, "open", interrupted):
            result = p2_run_cli(env)
        self.assertEnded(result, "stopped", exit_code=0, released=True)
        self.assertEqual((result[3]["stop_requested"], placed(env)), (True, []))

    def test_each_keychain_item_is_read_once_a_session(self):
        # The banner's promise: the session's credentials are read once, after the line, and each race's open uses
        # them (ARM's static checks look only at whether an item is present).
        env = build(self)
        add_race(env)
        self.assertEnded(p2_run_cli(env), "races_done", exit_code=0, released=True)
        reads = sr.credential_reads(env)
        self.assertTrue(reads)
        self.assertEqual(sorted(reads), sorted(set(reads)), reads)


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

        # config-p2's own arming state: each passes ARM's token, terminal and anchor checks (A2, A11, A13) and every
        # pin. Without place's whole static set at ARM the record would be written, the first flagged race's open
        # would end the session (open_failed) and the day could not be armed again.
        def not_armed(env):
            edit_config_p2(env, lambda config: config["orders"]["live"].__setitem__("armed", False))

        def one_acknowledgement_short(env):
            edit_config_p2(env, lambda config: config["orders"]["live"]["acknowledgements"].pop())

        def egress_unattested(env):
            edit_config_p2(env, lambda config: config["orders"]["live"].__setitem__("operator_attests_uk_egress",
                                                                                   False))

        cases = {"p2_over_ssh": (ssh, {}), "p2_tree_dirty": (dirty, {}), "p2_code_unknown": (unknown, {}),
                 "p2_arm_already_today": (armed_today, {}), "kill_file_engaged": (kill, {}),
                 "p2_pilot_1_day": (pilot_1, {}), "p2_other_bet_open": (bet_open, {}),
                 "p2_config_max_daily_loss": (loosened, {}), "pilot_session_armed": (pilot_armed_file, {}),
                 "p2_until_invalid": (None, {"until": "0955"}), "p2_races_invalid": (None, {"races": 6}),
                 "not_armed": (not_armed, {}), "acknowledgements_incomplete": (one_acknowledgement_short, {}),
                 "egress_unattested_or_cloud": (egress_unattested, {})}
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

    def test_journal_ahead_of_the_wall_clock_refuses_the_arm(self):
        # The journal's last event is stamped 60 s after now (the wall clock stepped back since). A7 refuses it, as
        # Pilot 1's ARM does: no ARM record is stamped earlier than the event in front of it.
        env = build(self, start_ns=ARM_AT + 60 * S)
        env.journal.append("note", {"reason": "stamped_before_the_clock_stepped_back"})
        env.clock.advance(-60)
        add_race(env)
        self.assertTrue(env.journal.fold(now_ns=env.clock.time_ns())["clock_regression"])
        head = len(events(env))
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: p2_arm_refused:halted"))
        self.assertEqual((summary, env.confirmer.prompts, len(events(env))), (None, [], head))

    def test_no_room_in_todays_booked_loss_refuses_the_arm(self):
        # settle-market books each order at its worst case on the UTC day the line is typed. A round trip with the
        # LAY at 6.0, settled the next morning, books £12 on that day: every BACK's pair pre-check would refuse
        # (booked + £10 over the day's £20), so the ARM refuses before the line and the day's one session is not
        # used on live-key opens that can only refuse. A booking of exactly £10 (LAY 5.0) leaves the room.
        for (back, lay), booked in ((("5.9", "6.4"), 1200), (("4.9", "5.2"), 1000)):
            with self.subTest(booked=booked):
                env = build(self)
                add_race(env, back=back, lay=lay)
                fill(env)                                             # both legs matched at once: a round trip
                self.assertEnded(p2_run_cli(env), "matched_leg", exit_code=0, released=True)
                env.exchange.place_handler = None
                sr.set_day(env, 1, before_off_s=3600)
                self.assertEqual(settle(env, MARKET)["booked_realized_loss_pence"], booked)
                add_race(env, "1.300", OFF + pilot.DAY_NS)
                sr.set_day(env, 1, before_off_s=900)
                for seen in (env.confirmer.prompts, env.transport.requests, env.runner.calls, env.popen.calls):
                    seen.clear()
                head = len(events(env))
                result = p2_run_cli(env, races=5)
                if booked > 1000:
                    self.assertRefused(env, result, "p2_pair_precheck:daily_loss")
                    self.assertEqual(result[2].strip().splitlines()[-1],
                                     "Betfair order refused: p2_arm_refused:p2_pair_precheck:daily_loss")
                    self.assertEqual((len(events(env)), len(arm_events(env))), (head, 1))
                else:
                    self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
                    self.assertEqual((len(arm_events(env)), result[3]["races"], len(placed(env))), (2, 1, 4))

    def test_wrong_confirm_token_is_refused_before_any_journal_or_git_read(self):
        # Static arming on config-p2's token (A2), first: yesterday's token is refused with no banner, no git read,
        # no Keychain secret read and nothing journaled.
        env = build(self)
        add_race(env)
        head = len(events(env))
        code, out, err = run_main(["p2-run", "--mode", "live", "--confirm-live", "live-000000000000-20270104",
                                   "--until", "2000", "--races", "1"], deps(env))
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: confirm_token_mismatch"))
        self.assertEqual((ended(out), env.confirmer.prompts, env.git.calls, env.popen.calls, len(events(env))),
                         (None, [], [], [], head))
        self.assertEqual(sr.credential_reads(env), [])

    def test_ssh_refused_before_any_keychain_or_git_read(self):
        env = build(self)
        add_race(env)
        env.environ["SSH_TTY"] = "/dev/ttys003"
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: p2_arm_refused:p2_over_ssh"))
        self.assertEqual((env.runner.calls, env.git.calls, env.confirmer.prompts, summary), ([], [], [], None))
        self.assertEqual(env.transport.requests, [])
        # Each of the three variables alone is refused, SSH_CLIENT too.
        for name in p2_run.SSH_VARS:
            with self.subTest(variable=name):
                env = build(self)
                add_race(env)
                env.environ[name] = "192.168.50.2 50000 22"
                self.assertRefused(env, p2_run_cli(env), "p2_over_ssh")
        self.assertEqual(p2_run.SSH_VARS, ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))

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
        # The stops in order: at the Mac, Ctrl-C or the KILL file (the bot pulls its own legs); the phone app is for
        # away from it, and the banner says what a cancel there leaves to do.
        self.assertIn("To stop at this Mac: Ctrl-C here or the KILL file (the bot cancels its own legs). Away from "
                      "it: the Betfair phone app (the bot's pull then ends in a handoff, and each leg needs ACCEPT "
                      "ABSENT or settle-market).", banner)
        self.assertLess(banner.index("Ctrl-C here"), banner.index("the Betfair phone app"))
        self.assertLess(banner.index("the Betfair phone app"), banner.index("own-scope kill from"))
        # With one race run, both legs placed, the next day may ask for more (at most 5, and 10 in all).
        sr.set_day(env, 1, before_off_s=900)
        code, _, err, summary = p2_run_cli(env, races=6)
        self.assertEqual(code, 2)
        self.assertIn("p2_races_invalid", err)
        result = p2_run_cli(env, races=5)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        self.assertEqual([e["payload"]["p2_arm"]["races"] for e in arm_events(env)], [1, 5])
        self.assertIn("Races so far: 0 today, 1 of 10 in total; this session may run 5", env.confirmer.prompts[-1]
                      ["banner"])

    def test_races_1_until_one_race_has_had_both_legs_placed(self):
        # The first sessions put Yu's eyes on one live rest and pull. A race counts from its first submit, but a
        # race whose BACK the exchange rejected, or never left the Mac, or whose LAY was refused (the BACK pulled at
        # once) had no rest: N stays 1 until a LAY has been placed.
        send_bounds = OrderGateway._send_bounds

        def stale_at_the_first_byte(gateway, book, *, pilot=None):
            return send_bounds(gateway, book, pilot=pilot)[0], lambda: "book_stale_before_send"

        def lay_refused(book, pending):
            return None, "leg_not_passive"

        for how in ("back_rejected", "back_unsent", "lay_refused"):
            with self.subTest(day_1=how):
                env = build(self)
                add_race(env)
                if how == "back_rejected":
                    env.exchange.place_handler = refuse_at_exchange
                    first = p2_run_cli(env)
                    env.exchange.place_handler = None
                elif how == "back_unsent":
                    with patch.object(OrderGateway, "_send_bounds", stale_at_the_first_byte):
                        first = p2_run_cli(env)
                else:
                    with patch.object(p2_lp, "choose_lay", lay_refused):
                        first = p2_run_cli(env)
                self.assertEnded(first, "races_done", exit_code=0, released=True)
                orders = env.journal.fold(now_ns=env.clock.time_ns())["orders"]
                self.assertEqual((len(p2_lp.races(orders)), p2_lp.pair_placed(orders),
                                  [o["side"] for o in legs(env)]), (1, False, ["BACK"]))
                sr.set_day(env, 1, before_off_s=900)
                add_race(env, "1.300", OFF + pilot.DAY_NS)
                code, _, err, _ = p2_run_cli(env, races=5)
                self.assertEqual((code, err.strip().splitlines()[-1]),
                                 (2, "Betfair order refused: p2_arm_refused:p2_races_invalid"))
                self.assertEnded(p2_run_cli(env, races=1), "races_done", exit_code=0, released=True)   # both legs
                self.assertTrue(p2_lp.pair_placed(env.journal.fold(now_ns=env.clock.time_ns())["orders"]))
                sr.set_day(env, 2, before_off_s=900)
                self.assertEnded(p2_run_cli(env, races=5), "no_more_markets", exit_code=0, released=True)
                self.assertEqual([e["payload"]["p2_arm"]["races"] for e in arm_events(env)], [1, 1, 5])

    def test_n_is_limited_by_the_races_left_and_ten_races_refuse_the_arm(self):
        def gateway_races(env, day_ns, count, *, pair=0):
            """count races on the UTC day starting at day_ns through the Pilot 2 gateway, under an ARM record
            journaled at 09:00Z that day: the first `pair` with both legs placed and pulled, the rest a BACK the
            exchange rejects (FAILED: a race once submitted)."""
            set_time(env.clock, day_ns + 9 * 3600 * S)
            env.arm_seq = arm_record(env, until_ns=day_ns + 20 * 3600 * S)
            for index in range(count):
                off = day_ns + 10 * 3600 * S + index * 600 * S
                market = race(env, f"1.3{index:02d}{day_ns // pilot.DAY_NS % 100:02d}", off_ns=off)
                set_time(env.clock, off - 590 * S)
                gateway = open_p2(env)
                env.exchange.place_handler = None if index < pair else refuse_at_exchange
                try:
                    results = [leg(env, gateway, "BACK", market.market_id)]
                    if index < pair:
                        results.append(leg(env, gateway, "LAY", market.market_id))
                        for result in reversed(results):
                            gateway.cancel(market.market_id, [result["orders"][0]["bet_id"]], source="cli_manual")
                finally:
                    env.exchange.place_handler = None
                    gateway.close()
                self.assertEqual([states(result) for result in results],
                                 [["EXECUTABLE"]] * 2 if index < pair else [["FAILED"]])

        day = sp.DAY_NS
        env = build(self, start_ns=day + 8 * 3600 * S)
        env.p2_guard = RiskGuard(env.p2, mode="live", clock=env.clock, paths=env.paths)
        gateway_races(env, day, 5, pair=1)
        gateway_races(env, day + pilot.DAY_NS, 3)                                  # 8 of 10 run: 2 left
        set_time(env.clock, day + 2 * pilot.DAY_NS + 8 * 3600 * S)
        env.confirmer.prompts.clear()
        code, _, err, summary = p2_run_cli(env, races=3)
        self.assertEqual((code, summary, env.confirmer.prompts), (2, None, []), err)
        self.assertEqual(err.strip().splitlines()[-1], "Betfair order refused: p2_arm_refused:p2_races_invalid")
        gateway_races(env, day + 2 * pilot.DAY_NS, 2)                              # 10 of 10
        set_time(env.clock, day + 3 * pilot.DAY_NS + 8 * 3600 * S)
        code, _, err, summary = p2_run_cli(env, races=1)
        self.assertEqual((code, summary, env.confirmer.prompts), (2, None, []), err)
        self.assertEqual(err.strip().splitlines()[-1],
                         "Betfair order refused: p2_arm_refused:p2_races_total,p2_races_invalid")

    def test_smaller_arm_pre_checks(self):
        # The login ledger; UNTIL exactly 20 minutes ahead (it must be later), and one that is no HHMM; RACES 0.
        env = build(self)                                                         # 09:45:00Z
        add_race(env)
        with patch.object(admission, "ledger_problems", return_value=["login_banned"]):
            self.assertRefused(env, p2_run_cli(env), "login_banned")
        self.assertRefused(env, p2_run_cli(env, until="1005"), "p2_until_invalid")
        self.assertEqual(p2_run.UNTIL_LEAD_NS, 20 * 60 * S)
        for until in ("2460", "25:00", "noon"):
            with self.subTest(until=until):
                code, _, err, summary = p2_run_cli(env, until=until)
                self.assertEqual((code, err.strip().splitlines()[-1], summary, env.confirmer.prompts),
                                 (2, "Betfair order refused: p2_until_invalid", None, []))
        self.assertEnded(p2_run_cli(env, until="1006"), "races_done", exit_code=0, released=True)
        sr.set_day(env, 1, before_off_s=900)
        code, _, err, summary = p2_run_cli(env, races=0)
        self.assertEqual((code, summary, len(arm_events(env))), (2, None, 1), err)
        self.assertIn("p2_races_invalid", err)

    def test_checks_run_again_after_the_line(self):
        # The KILL file made while Yu types the line: the checks on the fresh view refuse, with no ARM record, no
        # Keychain secret read and no caffeinate, and this admission is released.
        env = build(self)
        add_race(env)
        confirm = env.confirmer.confirm_line

        def typed_then_kill_file(expected, *, banner):
            confirm(expected, banner=banner)
            engage(env.paths, "agent_saw_something", clock=env.clock)

        env.confirmer.confirm_line = typed_then_kill_file
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, arm_events(env), env.popen.calls), (2, None, [], []), err)
        refusal = err.strip().splitlines()[-1]
        self.assertTrue(refusal.startswith("Betfair order refused: p2_arm_refused:"), refusal)
        self.assertIn("kill_file_engaged", refusal.split(":", 2)[-1].split(","))
        self.assertEqual((sr.credential_reads(env), env.transport.requests), ([], []))

    def test_stop_or_keychain_failure_after_the_line_refuses_the_arm(self):
        # Ctrl-C between the typed line and the record (here in the fresh view's git read, once the stop flag is
        # installed): p2_arm_stopped, before any Keychain secret read.
        env = build(self)
        add_race(env)
        confirm, git, state = env.confirmer.confirm_line, env.git, {"typed": False, "fired": False}

        def typed(expected, *, banner):
            confirm(expected, banner=banner)
            state["typed"] = True

        def git_then_ctrl_c(argv, **kwargs):
            if state["typed"] and not state["fired"]:
                state["fired"] = True
                sr.fire()
            return git(argv, **kwargs)

        env.confirmer.confirm_line = typed
        env.git = git_then_ctrl_c
        code, _, err, summary = p2_run_cli(env)
        self.assertTrue(state["fired"])
        self.assertEqual((code, summary, arm_events(env), env.popen.calls), (2, None, [], []), err)
        self.assertIn("Betfair order refused: p2_arm_stopped", err)
        self.assertIn("recording released (generation 1)", err)
        self.assertEqual(sr.credential_reads(env), [])
        # The session's Keychain read fails: refused the same way, with no ARM record.
        env = build(self)
        add_race(env)
        with patch.object(pilot_run, "load_session_credentials", side_effect=CredentialError("keychain_locked")):
            code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, arm_events(env), env.popen.calls), (2, None, [], []), err)
        self.assertIn("Betfair order refused: p2_arm_credentials_unavailable", err)
        self.assertIn("recording released (generation 1)", err)
        self.assertEqual(env.transport.requests, [])

    def test_an_arm_check_that_raises_is_a_refusal(self):
        # safety-02: place's static set (pilot_run.gates opens the journal and reads its anchor outside its own try)
        # and the day's-room pre-check (RiskGuard) can raise. Each raise is a refusal, p2_arm_check_failed:<check>:
        # before the line exit 2 with nothing taken; after it through refuse(), which releases this admission, never
        # "ARM did not finish" with recording left off until Yu's typed recording-release.
        gates, evaluate = pilot_run.gates, RiskGuard.evaluate_pilot_reservation
        for check in ("static", "daily_loss"):
            for after_line in (False, True):
                with self.subTest(check=check, after_line=after_line):
                    env = build(self)
                    add_race(env)
                    confirm, line = env.confirmer.confirm_line, {"typed": False}

                    def typed(expected, *, banner, confirm=confirm, line=line):
                        confirm(expected, banner=banner)
                        line["typed"] = True

                    def raising_gates(gate_env, command, check=check, after_line=after_line, line=line):
                        if check == "static" and command == "place" and line["typed"] == after_line:
                            raise JournalError("journal_unreadable")
                        return gates(gate_env, command)

                    def raising_evaluate(guard, *args, check=check, after_line=after_line, line=line, **kwargs):
                        if check == "daily_loss" and line["typed"] == after_line:
                            raise sqlite3.OperationalError("disk I/O error")
                        return evaluate(guard, *args, **kwargs)

                    env.confirmer.confirm_line = typed
                    with patch.object(pilot_run, "gates", raising_gates), \
                            patch.object(RiskGuard, "evaluate_pilot_reservation", raising_evaluate):
                        code, _, err, summary = p2_run_cli(env)
                    self.assertEqual((code, summary, arm_events(env), env.popen.calls, line["typed"]),
                                     (2, None, [], [], after_line), err)
                    self.assertIn(f"Betfair order refused: p2_arm_refused:p2_arm_check_failed:{check}\n", err)
                    self.assertNotIn("ARM did not finish", err)
                    self.assertEqual("recording released (generation 1)" in err, after_line)
                    self.assertEqual(admission.admission_status(env.paths)["admission"], None)
                    self.assertEqual((sr.credential_reads(env), env.transport.requests), ([], []))

    def test_anchor_is_published_with_the_arm_record(self):
        env = build(self)                                                     # no market: no gateway open follows
        result = p2_run_cli(env)
        self.assertEnded(result, "no_more_markets", exit_code=0, released=True)
        (arm,) = arm_events(env)
        self.assertEqual(kinds(env)[-1], "operator_confirm")
        self.assertEqual(anchor_read(env.paths.profile, runner=env.runner)["seq"], arm["seq"])

    def test_pilot_1_matched_unsettled_bet_refuses_the_arm(self):
        # Pilot 1's own bet, matched yesterday and not yet settled, in Pilot 1's registered world: release's rule
        # lets it by (complete, nothing remaining), and today is no Pilot 1 day. ARM's check is given no events, so
        # it counts (with the journal's events, pilot_gate.open_manual_refs leaves out Pilot 1's own orders).
        env = sp.build_pilot_gateway(self, arm=False, start=pilot.utc_text_from_ns(OFF - pilot.DAY_NS - 3600 * S))
        no_children(self)
        env.test = self
        sp.arm_session(env, until_ns=sp.UNTIL - pilot.DAY_NS, session_id="s-20270103")
        market = sp.add_market(env, off_ns=OFF - pilot.DAY_NS)
        set_time(env.clock, OFF - pilot.DAY_NS - 299 * S)
        sp.open_pilot(self, env)
        fill(env)
        first = sp.place(env, market)
        env.exchange.place_handler = None
        env.gateway.close()
        self.assertEqual((first["decision"], first["pilot"]["state"]), ("accepted", "EXECUTION_COMPLETE"), first)
        self.assertEqual(admission.live_state_problems(env.paths, clock=env.clock), [])
        support.write_test_config(env.paths, support.deep_merge(gw.LIVE_OVERRIDES, p2_orders()),
                                  path=env.paths.config_p2_path)
        env.p2 = cfg.load_config_p2(paths=env.paths)
        env.git, env.popen, env.processes = sp.FakeGit(), FakePopen(env), FakeProcesses(pid=1)
        env.environ = {FORBID_NETWORK_ENV: "1"}
        set_time(env.clock, ARM_AT)                                           # the next UTC day, 09:45Z
        env.confirmer.prompts.clear()
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, env.confirmer.prompts), (2, None, []), err)
        self.assertEqual(err.strip().splitlines()[-1], "Betfair order refused: p2_arm_refused:p2_other_bet_open")

    def test_unverified_coexistence_arms_only_with_the_flag(self):
        # config-p2 as the example ships it (Bet Angel's coexistence unverified): the command line refuses before
        # the executor without --bet-angel-paused (test_betfair_p2_cli), and with it the flag reaches ARM's static
        # set (A12) and each race's open.
        env = build(self)
        add_race(env)
        edit_config_p2(env, lambda config: config["coexistence"].__setitem__("bet_angel", "unverified"))
        env.confirmer.prompts.clear()
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, env.confirmer.prompts, arm_events(env)), (2, None, [], []), err)
        self.assertIn("bet_angel_coexistence_unverified; add --bet-angel-paused", err)
        result = p2_run_cli(env, extra=("--bet-angel-paused",))
        self.assertEnded(result, "races_done", exit_code=0, released=True)
        self.assertEqual(len(placed(env)), 2)

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

    def test_gateway_lock_busy_at_the_arm_record(self):
        # Another process holds .gateway.lock when the record is due: refused after the wait, no record, released.
        env = build(self)
        add_race(env)
        confirm, held = env.confirmer.confirm_line, contextlib.ExitStack()
        self.addCleanup(held.close)

        def typed_then_locked(expected, *, banner):
            confirm(expected, banner=banner)
            held.enter_context(file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock))

        env.confirmer.confirm_line = typed_then_locked
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, arm_events(env)), (2, None, []), err)
        self.assertIn("Betfair order refused: lock_busy", err)
        self.assertIn("recording released (generation 1)", err)
        self.assertEqual(env.popen.events, ["terminate", ("wait", 5)])

    def test_failure_after_the_arm_record_keeps_recording_off(self):
        # Anything but a refusal once the line is typed (here the anchor's publication raising, after the record):
        # the session does not start, caffeinate and the read session are closed, and recording stays off until
        # Yu's typed release. Exit 4: nothing was sent.
        env = build(self)
        add_race(env)
        with patch.object(p2_run, "publish_anchor", side_effect=RuntimeError("anchor_path_broke")):
            code, out, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, len(arm_events(env)), placed(env)), (4, None, 1, []), err)
        self.assertIn("ARM did not finish (RuntimeError): recording stays off until Yu's typed recording-release "
                      "(generation 1)", err)
        self.assertNotIn("anchor_path_broke", out + err)
        self.assertEqual(env.popen.events, ["terminate", ("wait", 5)])
        self.assertIsNotNone(admission.admission_status(env.paths)["admission"])

    def test_owner_lock_held_by_another_session_refuses_after_the_line(self):
        # Another live owner (Pilot 1's session) takes the owner lock while Yu types: refused, nothing journaled.
        env = build(self)
        add_race(env)
        confirm, held = env.confirmer.confirm_line, contextlib.ExitStack()
        self.addCleanup(held.close)

        def typed_then_owned(expected, *, banner):
            confirm(expected, banner=banner)
            held.enter_context(admission.live_ownership(
                env.paths, owner="pilot-session:s20270104-0a1b2c3d", purpose="RS-19 pilot p1 20270104",
                config=env.config, clock=env.clock, processes=FakeProcesses(pid=2)))

        env.confirmer.confirm_line = typed_then_owned
        head = len(events(env))
        code, _, err, summary = p2_run_cli(env)
        self.assertEqual((code, summary, len(events(env)), env.popen.calls), (2, None, head, []), err)
        self.assertIn("Betfair order refused: owner_live", err)
        self.assertEqual(sr.credential_reads(env), [])

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
                                            " refused or failed BACK 0, LAY 0 (codes: none); matched 2 (2 in full; "
                                            "in the place report BACK 1, LAY 1); round trips 1, lone BACKs 0, lone "
                                            "LAYs 0;"), lines[0])
        self.assertTrue(lines[0].endswith("; races left 9"))
        self.assertTrue(lines[1].startswith("2027-01-04: sessions 1, races 1;"))
        self.assertIn("2 p2-lp legs are open or unsettled; the next ARM refuses until each is cancelled or settled "
                      "(settle-market after its result).", out)
        # Matched in full with nothing resting: each leg's matched size and price for the website check, and the
        # settle line; never the handoff's stops or the kill, and no bet id.
        self.assertIn(f"matched {MARKET}: BACK matched £2.00 @ 3.10, EXECUTION_COMPLETE; LAY matched £2.00 @ 3.05, "
                      f"EXECUTION_COMPLETE", lines)
        self.assertIn(f"{MARKET}: matched and not settled; the journal shows nothing resting, so there is nothing to "
                      f"cancel. After its result: settle-market --market {MARKET}", lines)
        self.assertNotIn("Stop them now", out)
        self.assertNotIn("kill", out)
        for order in legs(env):
            self.assertNotIn(order["bet_id"], out)
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

    def test_cleared_read_failed_still_prints_the_journals_counts(self):
        env = build(self)
        add_race(env)
        self.assertEnded(p2_run_cli(env), "races_done", exit_code=0, released=True)
        env.router.next_response["listClearedOrders"] = [support.rpc_error_response(-32602, "DSC-0018")]
        code, out, err = run_main(["p2-summary", "--mode", "live", "--cleared"], deps(env))
        self.assertEqual(code, 2, err)
        self.assertRegex(err.strip().splitlines()[-1],
                         r"^Betfair order refused: p2_cleared_read_failed:[A-Za-z0-9_.:-]+$")
        self.assertTrue(out.startswith("total: sessions 1, races 1;"), out)
        self.assertNotIn("cleared total", out)

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
