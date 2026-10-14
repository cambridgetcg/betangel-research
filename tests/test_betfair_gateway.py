"""OrderGateway: the single choke point, in paper and in live mode against a simulated exchange.
Offline and synthetic. These show the order of journal events, refusals, halts and the capability
binding, not that any order would be profitable or that Betfair behaves as simulated."""

import json
import os
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import sqlite3

from betangel_research.betfair import arming
from betangel_research.betfair import gateway as gateway_module
from betangel_research.betfair import journal as journal_module
from betangel_research.betfair.api import JsonRpcClient, ReadClient, normalise_market_book
from betangel_research.betfair.auth import SessionManager
from betangel_research.betfair.credentials import (ANCHOR_SERVICE, SERVICES, anchor_write, load_credentials,
                                                    publish_anchor)
from betangel_research.betfair.errors import (ApiError, ConfigError, LockBusy, NotArmed, OrderRefused, SecretLeak,
                                              TransportError)
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.paper import book_time_ms
from betangel_research.betfair.receipts import append_jsonl, canonical_json, parse_utc_ns
from betangel_research.betfair.gateway import OrderGateway, normalise_cancel_report, normalise_place_report
from betangel_research.betfair.killswitch import engage
from betangel_research.betfair.paths import file_lock, kill_engaged
from betangel_research.betfair.receipts import bytes_sha256
from betangel_research.betfair.risk import RiskGuard, exposure_by_market

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_orders as orders


def kinds(journal):
    return [e["kind"] for e in journal.events()]


def failure_report(code, *, exec_code=None, status="FAILURE"):
    def handler(params):
        instruction = params["instructions"][0]
        return {"customerRef": params["customerRef"], "status": status, "errorCode": exec_code,
                "marketId": params["marketId"],
                "instructionReports": [{"status": "FAILURE", "errorCode": code, "instruction": instruction}]}
    return handler


def keychain_anchor(env):
    return json.loads(env.runner.items[(ANCHOR_SERVICE, "default")])


class _Death(BaseException):
    """A process death simulated right after one journal append (nothing after it runs)."""


def die_after(journal, kind):
    """Patch journal.append so the process 'dies' right after the first append of kind."""
    original = journal.append

    def append(event_kind, payload, **columns):
        event = original(event_kind, payload, **columns)
        if event_kind == kind:
            raise _Death(kind)
        return event

    return patch.object(journal, "append", append)


def fully_matched_places(env):
    """Make SimExchange match every placed order in full at its limit price (Astra's C1 harness)."""
    accept = env.exchange.place

    def matched(params):
        env.exchange.place_handler = None
        result = accept(params)
        env.exchange.place_handler = matched
        for row in result["instructionReports"]:
            price = row["instruction"]["limitOrder"]["price"]
            size = row["instruction"]["limitOrder"]["size"]
            env.exchange.orders[row["betId"]].update(status="EXECUTION_COMPLETE", sizeMatched=size, sizeRemaining=0.0,
                                                    averagePriceMatched=price)
            row.update(orderStatus="EXECUTION_COMPLETE", sizeMatched=size, averagePriceMatched=price)
        return result

    env.exchange.place_handler = matched


class PaperGatewayTests(unittest.TestCase):
    def setUp(self):
        self.env = gw.build_paper_gateway(self)
        self.env.gateway.open(command="place")
        self.addCleanup(self.env.gateway.close)

    def test_paper_sequence_intent_decision_submit_report(self):
        book = gw.paper_book(self.env.clock)
        # A BACK above the best displayed back price rests (paper-v1 fills only displayed size at or better).
        result = self.env.gateway.place("1.1", [gw.instruction(price_h=360)], book=book, source="api")
        self.assertEqual(result["decision"], "accepted")
        self.assertEqual(result["mode"], "paper")
        self.assertEqual(kinds(self.env.journal), ["intent", "risk_decision", "submit", "report"])
        order = result["orders"][0]
        self.assertEqual(order["state"], "EXECUTABLE")
        self.assertIsNotNone(order["bet_id"])
        events = list(self.env.journal.events())
        self.assertEqual(result["journal_seq"], [events[0]["seq"], events[-1]["seq"]])
        self.assertIsNone(events[2]["payload"]["body_sha256"])
        self.assertIsNone(events[2]["payload"]["params"])           # paper sends nothing on the wire
        self.assertEqual(events[3]["payload"]["report"]["source"], "paper-v1")
        # Paper cancel, replace and step use the same path and journal.
        cancel = self.env.gateway.cancel("1.1", [order["bet_id"]], source="api")
        self.assertEqual(cancel["orders"][0]["state"], "EXECUTION_COMPLETE")
        placed = self.env.gateway.place("1.1", [gw.instruction(price_h=370)], book=book, source="api")
        replaced = self.env.gateway.replace("1.1", [{"bet_id": placed["orders"][0]["bet_id"], "new_price_h": 380}],
                                            book=book, source="api")
        self.assertEqual(replaced["decision"], "accepted")
        self.assertEqual(sorted(o["state"] for o in replaced["orders"]), ["EXECUTABLE", "REPLACED"])
        closed = gw.paper_book(self.env.clock, status="CLOSED",
                               runners={str(gw.SELECTION): dict(orders.runner(), status="WINNER"),
                                        str(gw.OTHER_SELECTION): dict(orders.runner(), status="LOSER")})
        stepped = self.env.gateway.advance("1.1", book=closed)
        self.assertEqual(stepped["decision"], "advanced")
        states = {o["state"] for o in self.env.journal.fold(now_ns=self.env.clock.time_ns())["orders"].values()}
        self.assertNotIn("EXECUTABLE", states)
        self.assertEqual(self.env.journal.verify()["status"], "ok")
        # R3#12: the paper state rides in every paper report and settle; paper-state.json is no longer written.
        self.assertFalse((self.env.paths.orders_dir / "paper-state.json").exists())
        events = list(self.env.journal.events())
        carried = [e["kind"] for e in events if "paper_state" in e["payload"]]
        self.assertEqual(carried, [e["kind"] for e in events if e["kind"] in ("report", "settle")] + ["note"])
        self.assertEqual(events[-2]["kind"], "report")                   # the lapses at CLOSED (nothing matched)
        self.assertEqual(events[-2]["payload"]["paper_state"]["books"]["1.1"]["status"], "CLOSED")
        # Round 4: every order in 1.1 is final, so the closed market leaves the paper state, in a note.
        self.assertEqual((events[-1]["kind"], events[-1]["payload"]["reason"]), ("note", "paper_advance_state"))
        self.assertEqual(self.env.journal.last_paper_state(), events[-1]["payload"]["paper_state"])
        self.assertNotIn("1.1", self.env.journal.last_paper_state()["books"])

    def test_paper_status_keeps_hedge_credit(self):
        """Amendment A3, C1 and D5: paper status and the paper journal summary count exposure as paper R8-R10 do,
        with hedge credit (live counts it without: LiveGatewayTests.test_live_status_counts_matched_hedges_...)."""
        from betangel_research.betfair.cli import _journal_summary
        self.env.gateway.place("1.1", [gw.instruction(price_h=360)], book=gw.paper_book(self.env.clock), source="api")
        status = self.env.gateway.status()
        summary = _journal_summary(self.env.paths.journal_path("paper"), "paper", self.env.clock)
        self.assertEqual((status["exposure_basis"], summary["exposure_basis"]),
                         ("worst_case_with_hedge_credit", "worst_case_with_hedge_credit"))
        self.assertEqual((status["exposure_pence_total"], summary["exposure_pence_total"]), (200, 200))

    def test_paper_replace_child_keeps_the_callers_source(self):
        book = gw.paper_book(self.env.clock)
        placed = self.env.gateway.place("1.1", [gw.instruction(price_h=370)], book=book, source="cli_manual")
        self.env.gateway.replace("1.1", [{"bet_id": placed["orders"][0]["bet_id"], "new_price_h": 380}], book=book,
                                 source="cli_manual")
        child = [e for e in self.env.journal.events() if e["kind"] == "intent"][-1]
        self.assertTrue(child["payload"]["provisional_child"])
        self.assertEqual(child["payload"]["source"], "cli_manual")

    def test_paper_rpc_must_be_none(self):
        env = self.env
        with self.assertRaises(ConfigError) as caught:
            OrderGateway(env.config, paths=env.paths, mode="paper", journal=env.journal, guard=env.guard,
                         rpc=object(), clock=env.clock)
        self.assertEqual(str(caught.exception), "paper_gateway_must_not_hold_rpc")
        live_read = gw.FakeReadClient(gw.SimExchange(env.clock), app_key_slot="live")
        with self.assertRaises(ConfigError):
            OrderGateway(env.config, paths=env.paths, mode="paper", journal=env.journal, guard=env.guard,
                         read=live_read, clock=env.clock)
        live_guard = RiskGuard(env.config, mode="live", clock=env.clock, paths=env.paths)
        with self.assertRaises(ConfigError):
            OrderGateway(env.config, paths=env.paths, mode="paper", journal=env.journal, guard=live_guard,
                         clock=env.clock)
        self.assertIsNone(env.gateway.rpc)
        with self.assertRaises(OrderRefused):
            env.gateway.probe("1.1", gw.SELECTION, confirm_token="x")
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.place("1.1", [gw.instruction()], source="api")
        self.assertEqual(caught.exception.rule, "paper_book_required")
        self.assertEqual(env.gateway.preconfirm("place", market_id="1.1", instructions=[gw.instruction()]), [])

    def test_paper_lock_one_process_per_profile(self):
        second = OrderGateway(self.env.config, paths=self.env.paths, mode="paper", journal=self.env.journal,
                              guard=self.env.guard, clock=self.env.clock)
        with self.assertRaises(LockBusy):
            second.open(command="place")

    def test_paper_takes_persist_across_processes(self):
        """Regression (audit): each CLI process rebuilt the paper exchange with nothing taken, so three
        separate `paper place` runs on one book each took the 2.00 a 4.00 level allows (6.00 matched)."""
        env = self.env
        env.gateway.close()
        book = gw.paper_book(env.clock, runners={str(gw.SELECTION): orders.runner(back=((3.5, 4.0),),
                                                                                 lay=((3.55, 80.0),))})
        matched = []
        for _ in range(3):
            gateway = OrderGateway(env.config, paths=env.paths, mode="paper", journal=env.journal,
                                   guard=RiskGuard(env.config, mode="paper", clock=env.clock, paths=env.paths),
                                   clock=env.clock, env={})
            gateway.open(command="place")
            result = gateway.place("1.1", [gw.instruction(price_h=350, size_p=200)], book=book, source="cli_manual")
            gateway.close()
            matched.append(result["orders"][0]["size_matched"])
        self.assertEqual(matched, [2.0, 0.0, 0.0])

    def test_paper_traded_baseline_never_moves_back(self):
        """Regression (audit): an older book stored as the baseline, then a newer one, credited the same
        traded volume twice (a passive fill with no new trading)."""
        env, clock = self.env, self.env.clock

        def book(traded, observed_at=None):
            value = gw.paper_book(clock, runners={str(gw.SELECTION): orders.runner(lay=((3.55, 1.0),),
                                                                                   traded={"3.55": traded})})
            if observed_at is not None:
                value["observed_at"] = observed_at
            return value

        first = book(100.0)
        env.gateway.place("1.1", [gw.instruction(price_h=355, size_p=200)], book=first, source="cli_manual")
        clock.advance(2)
        env.gateway.advance("1.1", book=book(101.0))                # the first interval after activation
        clock.advance(2)
        env.gateway.advance("1.1", book=book(103.0))                # 2.00 traded: the 1.00 queue, then 0.00
        older = book(102.0, observed_at=gw.utc_from_ns(clock.time_ns() - 3 * 10**9))
        clock.advance(1)
        ignored = env.gateway.advance("1.1", book=older)
        self.assertEqual((ignored["decision"], ignored["refusal"]["rule"]),
                         ("ignored", "paper_book_older_than_previous"))
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.place("1.1", [gw.instruction(price_h=360)], book=older, source="cli_manual")
        self.assertEqual(caught.exception.rule, "paper_book_older_than_last_seen")
        clock.advance(1)
        same = env.gateway.advance("1.1", book=book(103.0))       # no new trading since 103
        self.assertEqual(same["orders"][0]["size_matched"], 0.0)
        state = env.journal.last_paper_state()
        self.assertEqual(set(state["exchange"]), {"taken", "activated_ms", "traded_hwm", "activation", "sequence"})
        self.assertEqual(state["books"]["1.1"]["runners"][str(gw.SELECTION)]["traded"], {"3.55": 103.0})

    def test_paper_step_on_delayed_self_fetched_books_is_flagged_not_a_silent_zero(self):
        """T8a end to end through the paper gateway's own delayed-key fetch and the real normaliser: tradedVolume
        [] while lastPriceTraded and totalMatched are present stepped to flags {} and 0 matched,
        indistinguishable from a quiet market. Each interval is now flagged; the queue never moves."""
        env, clock = self.env, self.env.clock
        exchange = gw.SimExchange(clock)
        exchange.delayed = True
        matched = [4957.8]                                            # made up

        def fetch(_self, market_id, fetched_by):
            row = exchange.book_row(market_id)
            matched[0] += 500.0                                       # made up: totals grow, the ladder stays []
            for runner in row["runners"]:
                runner["totalMatched"] = matched[0]
            return normalise_market_book(row, catalogue_item=exchange.catalogue_row(market_id), key_type="delayed",
                                         app_key_slot="delayed", observed_at=clock.utc_now(),
                                         monotonic_ns=clock.mono_ns, fetched_by=fetched_by)

        self.assertEqual(fetch(None, "1.1", "gateway_self_fetch")["runners"][str(gw.SELECTION)]["traded"], {})
        env.gateway.read = type("Read", (), {"app_key_slot": "delayed", "market_snapshot": fetch})()
        # A LAY below the best back rests, queued behind the displayed 40.00 at 3.45 (a known queue).
        placed = env.gateway.place("1.1", [gw.instruction(side="LAY", price_h=345, size_p=200)], source="api")
        self.assertEqual((placed["decision"], placed["orders"][0]["state"]), ("accepted", "EXECUTABLE"))
        ref = placed["orders"][0]["customer_order_ref"]
        clock.advance(180)
        self.assertEqual(env.gateway.advance("1.1")["flags"], {ref: ["passive_fill_skipped_pre_activation_interval"]})
        for _ in range(3):
            clock.advance(180)
            seen = len(list(env.journal.events()))
            stepped = env.gateway.advance("1.1")
            self.assertEqual(stepped["flags"], {ref: ["passive_fill_unavailable_delayed_rest_trd_empty"]})
            self.assertEqual(stepped["orders"][0]["size_matched"], 0.0)
            # Nothing moves, so the flag rides in the step's paper_advance_state note, not only in stdout.
            self.assertEqual([(e["kind"], e["payload"].get("reason"), e["payload"].get("flags"))
                              for e in list(env.journal.events())[seen:]],
                             [("note", "paper_advance_state", stepped["flags"])])
        self.assertEqual(env.gateway.paper.queue_state()[ref], 4000)

    def test_paper_step_flags_ride_in_the_events_it_journals_anyway(self):
        """Bounded flag retention: a step whose orders did not move journaled its flags nowhere (its
        paper_advance_state note had none), so they were only in that step's stdout. Its non-empty flags now
        ride in the note, or in the closing lapses report; a move report keeps every order's flags, and no
        event is ever added for flags, so a step that journals nothing keeps them only in its result."""
        env, clock = self.env, self.env.clock

        def book(traded, **changes):
            return gw.paper_book(clock, runners={str(gw.SELECTION): orders.runner(traded={"3.55": traded})},
                                 **changes)

        def step(value):
            seen = len(list(env.journal.events()))
            result = env.gateway.advance("1.1", book=value)
            return result["flags"], [(e["kind"], e["payload"].get("flags")) for e in list(env.journal.events())[seen:]]

        # BACK 2.00 @ 3.55 rests behind the 80.00 shown there.
        ref = env.gateway.place("1.1", [gw.instruction(price_h=355)], book=book(100.0),
                                source="cli_manual")["orders"][0]["customer_order_ref"]
        clock.advance(2)
        early = {ref: ["passive_fill_skipped_pre_activation_interval"]}
        self.assertEqual(step(book(100.0)), (early, [("note", early)]))
        clock.advance(2)
        self.assertEqual(step(book(100.0)), ({}, [("note", None)]))           # no flag: the note has no key
        clock.advance(2)
        invalid = book(100.0, invalid_reason="catalogue_missing")
        blind = {ref: ["book_invalid_no_fills"]}
        self.assertEqual(step(invalid), (blind, [("note", blind)]))
        self.assertEqual(step(invalid), (blind, []))                          # same state: nothing journaled
        clock.advance(2)
        self.assertEqual(step(book(100.0)), ({}, [("note", None)]))
        clock.advance(2)
        # 162.00 traded at 3.55 counts 81.00: 80.00 ahead, then 1.00 of the 2.00 fills. The report keeps the flags.
        filled = step(book(262.0))
        self.assertEqual(filled, ({ref: ["passive_fill"]}, [("report", {ref: ["passive_fill"]})]))
        clock.advance(2)
        # CLOSED with no catalogue row (F70): the step moves nothing, so its flag rides in the closing lapses.
        closed = gw.paper_book(clock, status="CLOSED", invalid_reason="catalogue_missing",
                               runners={str(gw.SELECTION): dict(orders.runner(traded={"3.55": 262.0}),
                                                                status="WINNER")})
        flags, journaled = step(closed)
        self.assertEqual(flags, blind)
        self.assertEqual([flags for _, flags in journaled if flags is not None], [blind])
        lapses = [e for e in env.journal.events() if e["payload"].get("flags") == blind][-1]
        self.assertEqual([(t["reason"], t["state"]) for t in lapses["payload"]["transitions"]],
                         [("paper_market_closed", "EXECUTION_COMPLETE")])
        order = env.journal.fold(now_ns=clock.time_ns())["orders"][ref]
        self.assertEqual((order["size_matched_p"], order["size_lapsed_p"]), (100, 100))

    def test_paper_step_that_moves_and_closes_journals_each_flag_once(self):
        """A step that both moves an order and closes the market: the move report carries the step's flags and
        the closing lapses report does not carry them again (flags are pending only when nothing moved)."""
        env, clock = self.env, self.env.clock
        base = gw.paper_book(clock)
        gone = env.gateway.place("1.1", [gw.instruction(selection_id=gw.OTHER_SELECTION, price_h=202)], book=base,
                                 source="cli_manual")["orders"][0]["customer_order_ref"]
        env.gateway.place("1.1", [gw.instruction(price_h=355)], book=base, source="cli_manual")
        clock.advance(2)
        seen = len(list(env.journal.events()))
        # CLOSED without the other runner: its order lapses in the step (a move); the other lapses at the close.
        closed = gw.paper_book(clock, status="CLOSED",
                               runners={str(gw.SELECTION): dict(orders.runner(), status="WINNER")})
        flags = env.gateway.advance("1.1", book=closed)["flags"]
        self.assertEqual(flags, {gone: ["lapsed_runner_removed"]})
        journaled = [e["payload"] for e in list(env.journal.events())[seen:]]
        self.assertEqual([p["flags"] for p in journaled if "flags" in p], [flags])
        self.assertEqual([sorted({t["reason"] for t in p.get("transitions", [])}) for p in journaled
                          if p.get("transitions")], [["paper_advance"], ["paper_market_closed"]])

    def test_paper_step_refuses_a_book_of_another_market(self):
        """Regression R3#6: paper-step --market 1.1 with a book of market 1.2 filled 1.1's resting order from
        1.2's traded volume, stored 1.2's book as 1.1's baseline and settled 1.1 with 1.2's runner results."""
        env, clock = self.env, self.env.clock
        first = gw.paper_book(clock, runners={str(gw.SELECTION): orders.runner(lay=((3.55, 1.0),),
                                                                               traded={"3.55": 100.0})})
        env.gateway.place("1.1", [gw.instruction(price_h=355)], book=first, source="cli_manual")
        clock.advance(2)
        env.gateway.advance("1.1", book=dict(first, observed_at=clock.utc_now()))
        before = [e["seq"] for e in env.journal.events()]
        stored = json.dumps(env.gateway._paper_books, sort_keys=True)
        clock.advance(2)
        busy = gw.paper_book(clock, market_id="1.2", runners={str(gw.SELECTION): orders.runner(
            lay=((3.55, 1.0),), traded={"3.55": 400.0})})
        closed = gw.paper_book(clock, market_id="1.2", status="CLOSED",
                               runners={str(gw.SELECTION): dict(orders.runner(), status="WINNER")})
        for book in (busy, closed, "not a book"):
            with self.subTest(book=str(book)[:40]), self.assertRaises(OrderRefused) as caught:
                env.gateway.advance("1.1", book=book)
            self.assertEqual(caught.exception.rule, "paper_book_market_mismatch")
        self.assertEqual([e["seq"] for e in env.journal.events()], before)       # nothing journaled
        self.assertEqual(json.dumps(env.gateway._paper_books, sort_keys=True), stored)
        order = list(env.journal.fold(now_ns=clock.time_ns())["orders"].values())[0]
        self.assertEqual((order["state"], order["size_matched_p"]), ("EXECUTABLE", 0))
        # The paper model refuses it too, whoever calls it.
        with self.assertRaisesRegex(ValueError, "same market"):
            env.gateway.paper.advance([order], prev_book=first, book=busy, now_ms=clock.time_ns() // 10**6)

    def test_paper_state_survives_death_after_the_report(self):
        """Regression R3#12: the fills were journaled first and paper-state.json written afterwards, so a death
        in between (SIGKILL, a closed terminal) forgot the takes and the traded baseline: a later order took
        the same displayed size again (OVERFILL 100 -> 200 in the repro) and a later step counted the same
        traded volume twice (DOUBLE-COUNTED 50 -> 100). The state now rides in the report itself."""
        env, clock = self.env, self.env.clock
        env.gateway.close()

        def gateway():
            value = OrderGateway(env.config, paths=env.paths, mode="paper", journal=env.journal,
                                 guard=RiskGuard(env.config, mode="paper", clock=clock, paths=env.paths),
                                 clock=clock, env={})
            value.open(command="place")
            return value

        def dies_after_report(call):
            dying = gateway()
            with die_after(env.journal, "report"), self.assertRaises(_Death):
                call(dying)
            dying._release()                 # the process is gone: its lock goes, nothing else runs

        # Takes: 2.00 shown at 3.50, so paper-v1 may take 1.00 in all.
        shown = gw.paper_book(clock, runners={str(gw.SELECTION): orders.runner(back=((3.5, 2.0),),
                                                                               lay=((3.55, 80.0),))})
        dies_after_report(lambda g: g.place("1.1", [gw.instruction(price_h=350, size_p=200)], book=shown,
                                            source="cli_manual"))
        second = gateway()
        result = second.place("1.1", [gw.instruction(price_h=350, size_p=200)], book=shown, source="cli_manual")
        second.close()
        matched = [o["size_matched_p"] for o in env.journal.fold(now_ns=clock.time_ns())["orders"].values()]
        self.assertEqual((matched, result["orders"][0]["size_matched"]), ([100, 0], 0.0))      # control: 100 + 0

        # Passive: BACK 2.00 @ 3.50 queued behind 1.00; 3.00 traded at 3.50 (1.50 after halving) fills 0.50 once.
        def stepped(offset_s, traded):
            return gw.paper_book(clock, market_id="1.2", observed_at=gw.utc_from_ns(t0 + offset_s * 10**9),
                                 runners={str(gw.SELECTION): orders.runner(back=((3.45, 40.0),),
                                                                           lay=((3.5, 1.0), (3.55, 80.0)),
                                                                           traded={"3.5": traded})})

        t0 = clock.time_ns()
        first = gateway()
        placed = first.place("1.2", [gw.instruction(price_h=350, size_p=200)], book=stepped(0, 0.0),
                             source="cli_manual")["orders"][0]
        first.close()
        clock.advance(5)
        step = gateway()
        step.advance("1.2", book=stepped(5, 0.0))           # no order moves: the new baseline rides in a note
        step.close()
        note = list(env.journal.events())[-1]
        self.assertEqual((note["kind"], note["payload"]["reason"], note["payload"]["paper_state"]["books"]["1.2"]),
                         ("note", "paper_advance_state", stepped(5, 0.0)))
        clock.advance(1)
        dies_after_report(lambda g: g.advance("1.2", book=stepped(6, 3.0)))
        # The report that records the fill also holds this step's book as the market's new baseline.
        self.assertEqual(env.journal.last_paper_state()["books"]["1.2"]["observed_at"], stepped(6, 3.0)["observed_at"])
        clock.advance(1)
        last = gateway()
        last.advance("1.2", book=stepped(7, 3.0))
        last.close()
        order = env.journal.fold(now_ns=clock.time_ns())["orders"][placed["customer_order_ref"]]
        self.assertEqual((order["size_matched_p"], order["size_remaining_p"]), (50, 150))      # control: 50
        self.assertFalse((env.paths.orders_dir / "paper-state.json").exists())

    def reopened(self):
        """Another paper process's gateway on the same journal (the earlier one died or closed)."""
        env = self.env
        value = OrderGateway(env.config, paths=env.paths, mode="paper", journal=env.journal,
                             guard=RiskGuard(env.config, mode="paper", clock=env.clock, paths=env.paths),
                             clock=env.clock, env={})
        report = value.open(command="place")
        self.addCleanup(value.close)
        return value, report

    def test_paper_request_interrupted_before_its_report_is_resolved_at_open(self):
        """Round 4: a paper cancel (or replace) of a part-matched bet interrupted between its submit and its report
        (Ctrl-C or a crash) left the order UNKNOWN for good: paper reconcile has no exchange, reconcile --accept
        refuses an order with matched size, every later paper place on the market was refused and a CLOSED step
        never settled it. The paper state is journaled only with a report (R3#12), so the paper exchange rebuilt
        at the next open never saw the request: open() now resolves it exactly (paper_interrupted_before_report):
        a cancel or replace never happened (EXECUTABLE as recorded; a replace child REFUSED), a place never
        happened (FAILED)."""
        env, clock = self.env, self.env.clock
        shown = gw.paper_book(clock, runners={str(gw.SELECTION): orders.runner(back=((3.5, 2.0),),
                                                                               lay=((3.55, 80.0),))})
        placed = env.gateway.place("1.1", [gw.instruction(price_h=350, size_p=200)], book=shown,
                                   source="cli_manual")["orders"][0]
        self.assertEqual((placed["state"], placed["size_matched"], placed["size_remaining"]), ("EXECUTABLE", 1.0, 1.0))
        with die_after(env.journal, "submit"), self.assertRaises(_Death):
            env.gateway.cancel("1.1", [placed["bet_id"]], source="cli_manual")
        env.gateway._release()                      # the process is gone: its lock goes, nothing else runs
        gateway, report = self.reopened()
        self.assertEqual(report["interrupted_resolved"], [placed["customer_order_ref"]])
        order = env.journal.fold(now_ns=clock.time_ns())["orders"][placed["customer_order_ref"]]
        self.assertEqual((order["state"], order["size_matched_p"], order["size_remaining_p"], order["error_code"]),
                         ("EXECUTABLE", 100, 100, "paper_interrupted_before_report"))
        self.assertEqual(env.journal.fold(now_ns=clock.time_ns())["unresolved_markets"], [])
        # The market takes new paper orders, and the cancel can simply be made again.
        rested = gateway.place("1.1", [gw.instruction(price_h=370)], book=shown, source="cli_manual")
        self.assertEqual(rested["decision"], "accepted")
        cancelled = gateway.cancel("1.1", [placed["bet_id"]], source="cli_manual")["orders"][0]
        self.assertEqual((cancelled["state"], cancelled["size_matched"]), ("EXECUTION_COMPLETE", 1.0))
        # An interrupted replace: the parent rests as before, its provisional child never existed.
        parent = rested["orders"][0]
        with die_after(env.journal, "submit"), self.assertRaises(_Death):
            gateway.replace("1.1", [{"bet_id": parent["bet_id"], "new_price_h": 380}], book=shown, source="api")
        gateway._release()
        gateway, report = self.reopened()
        state = env.journal.fold(now_ns=clock.time_ns())
        child = [o for o in state["orders"].values() if o["parent_ref"] == parent["customer_order_ref"]][0]
        self.assertEqual(sorted(report["interrupted_resolved"]),
                         sorted([parent["customer_order_ref"], child["customer_order_ref"]]))
        self.assertEqual((state["orders"][parent["customer_order_ref"]]["state"], child["state"],
                          child["provisional_child"]), ("EXECUTABLE", "REFUSED", False))
        # An interrupted place never happened.
        with die_after(env.journal, "submit"), self.assertRaises(_Death):
            gateway.place("1.1", [gw.instruction(price_h=375)], book=shown, source="cli_manual")
        gateway._release()
        gateway, report = self.reopened()
        state = env.journal.fold(now_ns=clock.time_ns())
        (failed,) = report["interrupted_resolved"]
        self.assertEqual((state["orders"][failed]["state"], state["orders"][failed]["error_code"]),
                         ("FAILED", "paper_interrupted_before_report"))
        # The market settles at CLOSED: the matched part reaches paper P&L.
        clock.advance(1)
        closed = gw.paper_book(clock, status="CLOSED",
                               runners={str(gw.SELECTION): dict(orders.runner(), status="WINNER"),
                                        str(gw.OTHER_SELECTION): dict(orders.runner(), status="LOSER")})
        stepped = gateway.advance("1.1", book=closed)
        self.assertEqual(stepped["settlement"]["orders"][placed["customer_order_ref"]]["state"], "SETTLED")
        self.assertEqual(env.journal.fold(now_ns=clock.time_ns())["orders"][placed["customer_order_ref"]]["state"],
                         "SETTLED")

    def test_closed_markets_leave_the_paper_state(self):
        """Round 4: the paper state journaled with every paper event kept every market ever placed on or stepped
        (its book, takes and traded marks, and its settled orders' queues and priorities), so each event, and the
        journal, grew with every market ever touched (about quadratic). Once a CLOSED book leaves every order in a
        market final, the market leaves the paper state but for its compact closed marker (re-review R2): after five
        closed markets the rest of it is as small as after one."""
        env, clock = self.env, self.env.clock
        sizes = []
        for n in range(1, 6):
            market = f"1.{n}"
            shown = gw.paper_book(clock, market_id=market, runners={
                str(gw.SELECTION): orders.runner(back=((3.5, 2.0),), lay=((3.55, 80.0),), traded={"3.5": 10.0}),
                str(gw.OTHER_SELECTION): orders.runner()})
            placed = env.gateway.place(market, [gw.instruction(price_h=350, size_p=200)], book=shown,
                                       source="cli_manual")
            self.assertEqual(placed["orders"][0]["size_matched"], 1.0)                  # part matched, part resting
            clock.advance(1)
            closed = gw.paper_book(clock, market_id=market, status="CLOSED",
                                   runners={str(gw.SELECTION): dict(orders.runner(), status="WINNER"),
                                            str(gw.OTHER_SELECTION): dict(orders.runner(), status="LOSER")})
            env.gateway.advance(market, book=closed)
            state = env.journal.last_paper_state()
            self.assertEqual((state["books"], state["queues"], state["exchange"]["taken"],
                              state["exchange"]["traded_hwm"], state["exchange"]["activated_ms"],
                              state["exchange"]["activation"]), ({}, {}, {}, {}, {}, {}))
            self.assertEqual(sorted(state["closed"]["markets"]), [f"1.{i}" for i in range(1, n + 1)])
            sizes.append(len(canonical_json(dict(state, closed=None))))
            clock.advance(1)
        self.assertLessEqual(max(sizes) - min(sizes), 2)              # only the sequence counter's digits grow
        fold = env.journal.fold(now_ns=clock.time_ns())
        self.assertEqual({o["state"] for o in fold["orders"].values()}, {"SETTLED"})
        # A market still holding a non-final order keeps its state.
        shown = gw.paper_book(clock, market_id="1.9")
        env.gateway.place("1.9", [gw.instruction(price_h=370)], book=shown, source="cli_manual")
        self.assertIn("1.9", env.journal.last_paper_state()["books"])

    def closed_book(self, market="1.1"):
        return gw.paper_book(self.env.clock, market_id=market, status="CLOSED",
                             runners={str(gw.SELECTION): dict(orders.runner(), status="WINNER"),
                                      str(gw.OTHER_SELECTION): dict(orders.runner(), status="LOSER")})

    def assert_market_closed(self, gateway, market, books, rule="paper_market_closed"):
        """Every order on market, and every step with books, is refused with rule, and nothing is journaled."""
        head = self.env.journal.anchor()
        for book in books:
            with self.subTest(book=book["observed_at"]):
                with self.assertRaises(OrderRefused) as caught:
                    gateway.place(market, [gw.instruction(price_h=350)], book=book, source="api")
                self.assertEqual(caught.exception.rule, rule)
                with self.assertRaises(OrderRefused) as caught:
                    gateway.replace(market, [{"bet_id": "1", "new_price_h": 360}], book=book, source="api")
                self.assertEqual(caught.exception.rule, rule)
                stepped = gateway.advance(market, book=book)
                self.assertEqual((stepped["decision"], stepped["refusal"]["rule"], stepped["settlement"]),
                                 ("ignored", rule, None))
        self.assertEqual(self.env.journal.anchor(), head)

    def settled(self):
        return sorted((o["market_id"], o["state"], o["size_matched_p"], o["settled_profit_own_p"])
                      for o in self.env.journal.fold(now_ns=self.env.clock.time_ns())["orders"].values())

    def test_closed_market_takes_no_later_order_or_book(self):
        """Re-review R2 (Astra's repro): round 4 dropped a closed market's book from the paper state, and with it the
        market's chronology barrier. An OPEN book from before the close was then taken again: BACK 2.00 @ 3.50
        matched 2.0 after the result was known, re-stepping the same CLOSED book settled a second 200p bet for
        500p, and the takes that had consumed that liquidity were gone too. A compact closed marker now outlives the
        dropped state: no order and no later book but a CLOSED re-step is used (paper_market_closed), nothing is
        journaled, and the market settles one bet once."""
        env, clock = self.env, self.env.clock
        book = gw.paper_book(clock)
        placed = env.gateway.place("1.1", [gw.instruction(price_h=350)], book=book, source="api")
        self.assertEqual(placed["orders"][0]["size_matched"], 2.0)
        clock.advance(1)
        closed = self.closed_book()
        self.assertEqual(env.gateway.advance("1.1", book=closed)["decision"], "advanced")
        self.assertNotIn("1.1", env.gateway._paper_books)                    # round 4's cleanup still happens
        state = env.journal.last_paper_state()
        self.assertEqual((state["schema_version"], state["closed"], state["exchange"]["taken"]),
                         (2, {"markets": {"1.1": book_time_ms(closed)}, "floor_ms": None}, {}))
        clock.advance(1)
        later_open = gw.paper_book(clock)                  # Betfair reopening a CLOSED market is not in the register
        older_closed = dict(closed, observed_at=book["observed_at"])
        self.assert_market_closed(env.gateway, "1.1", [book, later_open, older_closed])
        # A CLOSED re-step (Betfair keeps listing a closed market as CLOSED, F77) is taken and changes nothing.
        head = env.journal.anchor()
        for again in (closed, dict(closed, observed_at=clock.utc_now())):
            stepped = env.gateway.advance("1.1", book=again)
            self.assertEqual((stepped["decision"], stepped["settlement"]), ("advanced", None))
        self.assertEqual(env.journal.last_paper_state()["closed"]["markets"], {"1.1": clock.time_ns() // 10**6})
        self.assertEqual(len(list(env.journal.events())), head["seq"] + 1)          # the later close, in a note
        self.assertEqual(self.settled(), [("1.1", "SETTLED", 200, 500)])
        # Another market is untouched by 1.1's marker.
        other = env.gateway.place("1.2", [gw.instruction(price_h=350)], book=gw.paper_book(clock, market_id="1.2"),
                                  source="api")
        self.assertEqual(other["orders"][0]["size_matched"], 2.0)

    def test_closed_market_stays_closed_after_a_reopen_or_a_death(self):
        """Re-review R2, restart: the marker rides in the closing step's own events, so a new process refuses the
        pre-close book too, also after a death right after the settle (before the note that drops the book)."""
        env, clock = self.env, self.env.clock
        book = gw.paper_book(clock)
        env.gateway.place("1.1", [gw.instruction(price_h=350)], book=book, source="api")
        clock.advance(1)
        closed = self.closed_book()
        with die_after(env.journal, "settle"), self.assertRaises(_Death):
            env.gateway.advance("1.1", book=closed)
        env.gateway._release()                      # the process is gone: its lock goes, nothing else runs
        settle = list(env.journal.events())[-1]
        self.assertEqual((settle["kind"], settle["payload"]["paper_state"]["closed"]["markets"]),
                         ("settle", {"1.1": book_time_ms(closed)}))
        gateway, _ = self.reopened()
        self.assert_market_closed(gateway, "1.1", [book])
        self.assertEqual(gateway.advance("1.1", book=closed)["decision"], "advanced")    # drops the book now
        gateway.close()
        state = env.journal.last_paper_state()
        self.assertEqual((sorted(state["books"]), state["closed"]["markets"]), ([], {"1.1": book_time_ms(closed)}))
        gateway, _ = self.reopened()
        self.assert_market_closed(gateway, "1.1", [book, gw.paper_book(clock)])
        self.assertEqual(self.settled(), [("1.1", "SETTLED", 200, 500)])

    def test_closed_markers_are_bounded_by_a_floor(self):
        """Re-review R2, growth: each marker rides in every paper event, so at most PAPER_CLOSED_MARKERS_MAX are
        kept; the earliest close evicted becomes the floor. The evicted market's pre-close book is still refused
        (paper_book_at_or_before_closed_floor), as is any book at or before the floor of a market holding no timed
        book, also after a reopen; a market holding a timed book keeps its own chronology below the floor."""
        env, clock = self.env, self.env.clock
        t0 = clock.time_ns()
        env.gateway.place("1.9", [gw.instruction(price_h=370)], book=gw.paper_book(clock, market_id="1.9"),
                          source="api")                                            # rests, older than every close
        pre, closes = {}, {}
        with patch.object(gateway_module, "PAPER_CLOSED_MARKERS_MAX", 2):
            for market in ("1.1", "1.2", "1.3"):
                pre[market] = gw.paper_book(clock, market_id=market)
                env.gateway.place(market, [gw.instruction(price_h=350)], book=pre[market], source="api")
                clock.advance(1)
                closed = self.closed_book(market)
                env.gateway.advance(market, book=closed)
                closes[market] = book_time_ms(closed)
                clock.advance(1)
            self.assertEqual(env.journal.last_paper_state()["closed"],
                             {"markets": {"1.2": closes["1.2"], "1.3": closes["1.3"]}, "floor_ms": closes["1.1"]})
            env.gateway.close()
            gateway, _ = self.reopened()
            self.assertEqual((gateway._paper_closed, gateway._paper_closed_floor_ms),
                             ({"1.2": closes["1.2"], "1.3": closes["1.3"]}, closes["1.1"]))
            floor = "paper_book_at_or_before_closed_floor"
            self.assert_market_closed(gateway, "1.1", [pre["1.1"]], rule=floor)
            self.assert_market_closed(gateway, "1.2", [pre["1.2"]])
            self.assert_market_closed(gateway, "1.4", [gw.paper_book(clock, market_id="1.4",
                                                                      observed_at=gw.utc_from_ns(t0))], rule=floor)
            # A kept book without a usable time gives no chronology, so it does not lift the floor either.
            kept = gateway._paper_books.get("1.4")
            gateway._paper_books["1.4"] = dict(pre["1.1"], market_id="1.4", observed_at="not a time")
            self.assert_market_closed(gateway, "1.4", [gw.paper_book(clock, market_id="1.4",
                                                                      observed_at=gw.utc_from_ns(t0))], rule=floor)
            self.assertIsNone(kept)
            del gateway._paper_books["1.4"]
            # 1.9 holds a timed book below the floor: a later one, still below the floor, steps it.
            below = gw.paper_book(clock, market_id="1.9", observed_at=gw.utc_from_ns(t0 + 5 * 10**8))
            self.assertEqual(gateway.advance("1.9", book=below)["decision"], "advanced")
            # A new market's book after the floor is taken.
            fresh = gateway.place("1.4", [gw.instruction(price_h=350)], book=gw.paper_book(clock, market_id="1.4"),
                                  source="api")
            self.assertEqual(fresh["orders"][0]["size_matched"], 2.0)
        self.assertEqual(sorted(s for s in self.settled() if s[1] == "SETTLED"),
                         [(m, "SETTLED", 200, 500) for m in ("1.1", "1.2", "1.3")])

    def test_a_kept_closed_book_closes_its_market(self):
        """Re-review R2: a market whose kept book is CLOSED is closed without a marker too. A paper state from before
        round 4 kept every closed market whole (schema_version 1, nothing dropped, so nothing to migrate); there a
        later OPEN book (Betfair reopening a CLOSED market is not in the register) is refused like an earlier one."""
        env, clock = self.env, self.env.clock
        original = OrderGateway._paper_state

        def before_round_four(gateway):
            state = original(gateway)
            del state["closed"]
            return dict(state, schema_version=1)

        book = gw.paper_book(clock)
        with patch.object(OrderGateway, "_paper_state", before_round_four), \
                patch.object(OrderGateway, "_close_paper_market", lambda gateway, market_id: None):
            env.gateway.place("1.1", [gw.instruction(price_h=350)], book=book, source="api")
            clock.advance(1)
            env.gateway.advance("1.1", book=self.closed_book())
            env.gateway.close()
        state = env.journal.last_paper_state()
        self.assertEqual((state["schema_version"], state["books"]["1.1"]["status"]), (1, "CLOSED"))
        clock.advance(1)
        gateway, _ = self.reopened()
        self.assertEqual(gateway._paper_closed, {})
        self.assert_market_closed(gateway, "1.1", [book, gw.paper_book(clock)])
        self.assertEqual(self.settled(), [("1.1", "SETTLED", 200, 500)])

    def test_a_paper_state_from_before_the_markers_keeps_closed_markets_closed(self):
        """Re-review R2, migration: a paper state journaled before the markers (schema_version 1, round 4) lost a
        closed market's book in the closing step's note. A reopen marks every market such a note dropped as
        closed (at the note's recorded_at), and the next event journals schema_version 2 with the marker."""
        env, clock = self.env, self.env.clock
        original = OrderGateway._paper_state

        def round_four(gateway):
            state = original(gateway)
            del state["closed"]
            return dict(state, schema_version=1)

        book = gw.paper_book(clock)
        with patch.object(OrderGateway, "_paper_state", round_four):
            env.gateway.place("1.1", [gw.instruction(price_h=350)], book=book, source="api")
            env.gateway.place("1.2", [gw.instruction(price_h=370)], book=gw.paper_book(clock, market_id="1.2"),
                              source="api")
            clock.advance(1)
            env.gateway.advance("1.1", book=self.closed_book())
            env.gateway.close()
        note = list(env.journal.events())[-1]
        self.assertEqual((note["kind"], note["market_id"], note["payload"]["paper_state"]["schema_version"],
                          sorted(note["payload"]["paper_state"]["books"])), ("note", "1.1", 1, ["1.2"]))
        clock.advance(1)
        gateway, _ = self.reopened()
        at_ms = parse_utc_ns(note["recorded_at"]) // 10**6
        self.assertEqual((gateway._paper_closed, gateway._paper_closed_floor_ms), ({"1.1": at_ms}, None))
        self.assert_market_closed(gateway, "1.1", [book, gw.paper_book(clock)])
        resting = gateway.place("1.2", [gw.instruction(price_h=375)], book=gw.paper_book(clock, market_id="1.2"),
                                source="api")
        self.assertEqual(resting["decision"], "accepted")                          # an open market is not marked
        state = env.journal.last_paper_state()
        self.assertEqual((state["schema_version"], state["closed"]), (2, {"markets": {"1.1": at_ms}, "floor_ms": None}))

    def test_a_closed_book_a_place_or_replace_refuses_closes_its_market(self):
        """Re-review R2 follow-up (verify-4-0): a CLOSED book handed to place or replace, or self-fetched, was refused
        (validation:V9, market_not_open) but closed nothing, so a pre-close OPEN book handed in afterwards, inside
        max_snapshot_age_s, was taken: BACK 2.00 @ 3.50 matched 2.0 and settled for 500p against the result the
        CLOSED book had shown, and a replace's child did the same. The refused CLOSED book now marks the market at
        its time, in a note journaled before the request's own events; the operator still sees validation:V9, and
        later orders and books but a CLOSED re-step are refused (paper_market_closed) with nothing journaled."""
        env, clock = self.env, self.env.clock
        pre = gw.paper_book(clock)
        clock.advance(1)
        closed = self.closed_book()
        refused = env.gateway.place("1.1", [gw.instruction(price_h=350)], book=closed, source="api")
        self.assertEqual((refused["decision"], refused["refusal"]["rule"]), ("refused", "validation:V9"))
        events = list(env.journal.events())
        self.assertEqual([e["kind"] for e in events], ["note", "intent", "risk_decision"])
        self.assertEqual((events[0]["market_id"], events[0]["payload"]["reason"],
                          events[0]["payload"]["paper_state"]["closed"], events[0]["payload"]["paper_state"]["books"]),
                         ("1.1", "paper_market_closed", {"markets": {"1.1": book_time_ms(closed)}, "floor_ms": None},
                          {}))                                       # 1.1 held no book: the marker alone
        self.assertEqual(refused["journal_seq"], [events[0]["seq"], events[-1]["seq"]])
        clock.advance(1)
        self.assert_market_closed(env.gateway, "1.1", [pre])
        head = env.journal.anchor()
        with self.assertRaises(OrderRefused) as caught:               # the same CLOSED book again: closed already
            env.gateway.place("1.1", [gw.instruction(price_h=350)], book=closed, source="api")
        self.assertEqual((caught.exception.rule, env.journal.anchor()), ("paper_market_closed", head))
        stepped = env.gateway.advance("1.1", book=closed)
        self.assertEqual((stepped["decision"], stepped["settlement"]), ("advanced", None))
        # replace: 1.2 holds a resting bet, so its kept book is replaced by the CLOSED one (as a CLOSED step leaves it).
        first = gw.paper_book(clock, market_id="1.2")
        resting = env.gateway.place("1.2", [gw.instruction(price_h=400)], book=first, source="api")["orders"][0]
        self.assertEqual((resting["state"], resting["size_matched"]), ("EXECUTABLE", 0.0))
        clock.advance(1)
        between = gw.paper_book(clock, market_id="1.2")              # pre-close, newer than the kept book
        clock.advance(1)
        closed2 = self.closed_book("1.2")
        count = len(list(env.journal.events()))
        refused = env.gateway.replace("1.2", [{"bet_id": resting["bet_id"], "new_price_h": 350}], book=closed2,
                                      source="api")
        self.assertEqual((refused["decision"], refused["refusal"]["rule"]), ("refused", "validation:V9"))
        new = list(env.journal.events())[count:]
        self.assertEqual([e["kind"] for e in new], ["note", "replace_intent"])
        state = new[0]["payload"]["paper_state"]
        self.assertEqual((state["closed"]["markets"]["1.2"], state["books"]["1.2"]["status"],
                          state["books"]["1.2"]["observed_at"]),
                         (book_time_ms(closed2), "CLOSED", closed2["observed_at"]))
        clock.advance(1)
        self.assert_market_closed(env.gateway, "1.2", [between, first])
        self.assertEqual(env.gateway.advance("1.2", book=closed2)["decision"], "advanced")
        # A CLOSED book the gateway fetches itself (place or replace without a book) closes its market the same way.
        pre3 = gw.paper_book(clock, market_id="1.3")
        clock.advance(1)
        closed3 = self.closed_book("1.3")
        env.gateway.read = type("Read", (), {"app_key_slot": "delayed",
                                             "market_snapshot": lambda _self, market_id, fetched_by: closed3})()
        refused = env.gateway.place("1.3", [gw.instruction(price_h=350)], source="api")
        self.assertEqual(refused["refusal"]["rule"], "validation:V9")
        env.gateway.read = None
        self.assert_market_closed(env.gateway, "1.3", [pre3])
        self.assertEqual(self.settled(), [("1.1", "REFUSED", 0, None), ("1.2", "LAPSED", 0, None),
                                          ("1.3", "REFUSED", 0, None)])

    def test_a_closed_book_a_place_refuses_keeps_its_market_closed_after_a_reopen_or_a_death(self):
        """Re-review R2 follow-up, restart: the note carrying the marker is the request's first journal write, so a
        new process refuses the pre-close book too, also after a death right after that note (before the intent),
        and after a clean close of a process whose replace saw the CLOSED book."""
        env, clock = self.env, self.env.clock
        pre = gw.paper_book(clock)
        clock.advance(1)
        closed = self.closed_book()
        with die_after(env.journal, "note"), self.assertRaises(_Death):
            env.gateway.place("1.1", [gw.instruction(price_h=350)], book=closed, source="api")
        env.gateway._release()                      # the process is gone: its lock goes, nothing else runs
        self.assertEqual(kinds(env.journal), ["note"])
        clock.advance(1)
        gateway, _ = self.reopened()
        self.assertEqual((gateway._paper_closed, gateway._paper_closed_floor_ms), ({"1.1": book_time_ms(closed)}, None))
        self.assert_market_closed(gateway, "1.1", [pre])
        first = gw.paper_book(clock, market_id="1.2")
        resting = gateway.place("1.2", [gw.instruction(price_h=400)], book=first, source="api")["orders"][0]
        clock.advance(1)
        closed2 = self.closed_book("1.2")
        refused = gateway.replace("1.2", [{"bet_id": resting["bet_id"], "new_price_h": 350}], book=closed2,
                                  source="api")
        self.assertEqual(refused["refusal"]["rule"], "validation:V9")
        gateway.close()
        clock.advance(1)
        again, _ = self.reopened()
        self.assertEqual(again._paper_closed, {"1.1": book_time_ms(closed), "1.2": book_time_ms(closed2)})
        self.assert_market_closed(again, "1.1", [pre])
        self.assert_market_closed(again, "1.2", [first])
        self.assertEqual(again.advance("1.2", book=closed2)["decision"], "advanced")
        self.assertEqual(self.settled(), [("1.2", "LAPSED", 0, None)])

    def test_a_closed_book_paper_step_ignores_or_an_evicted_marker_still_closes_its_market(self):
        """Re-review R2 follow-up, the other paths: a CLOSED book paper-step ignores (older than the market's last
        book) closed nothing either; it now marks the market too. A market holding a kept book keeps the refused
        CLOSED book in its place, so evicting its marker for the floor leaves it closed (the floor spares a market
        holding a timed book, and its kept OPEN book's own chronology would take a pre-close book again). The
        market whose ignored CLOSED book was older than its kept OPEN book (1.1 here) keeps its marker instead:
        later closes evict the others (test_an_older_closed_book_keeps_its_market_closed_when_markers_are_evicted)."""
        env, clock = self.env, self.env.clock
        closed = self.closed_book()
        clock.advance(1)
        later = gw.paper_book(clock)
        env.gateway.place("1.1", [gw.instruction(price_h=400)], book=later, source="api")
        count = len(list(env.journal.events()))
        stepped = env.gateway.advance("1.1", book=closed)
        self.assertEqual((stepped["decision"], stepped["refusal"]["rule"]),
                         ("ignored", "paper_book_older_than_previous"))
        new = list(env.journal.events())[count:]
        self.assertEqual([(e["kind"], e["payload"]["reason"]) for e in new], [("note", "paper_market_closed")])
        self.assertEqual(new[0]["payload"]["paper_state"]["closed"]["markets"], {"1.1": book_time_ms(closed)})
        clock.advance(1)
        self.assert_market_closed(env.gateway, "1.1", [gw.paper_book(clock)])
        with patch.object(gateway_module, "PAPER_CLOSED_MARKERS_MAX", 1):
            first = gw.paper_book(clock, market_id="1.3")
            resting = env.gateway.place("1.3", [gw.instruction(price_h=400)], book=first, source="api")["orders"][0]
            clock.advance(1)
            between = gw.paper_book(clock, market_id="1.3")          # pre-close, newer than the kept book
            clock.advance(1)
            closed3 = self.closed_book("1.3")
            env.gateway.place("1.3", [gw.instruction(price_h=350)], book=closed3, source="api")
            clock.advance(1)
            closed4 = self.closed_book("1.4")
            env.gateway.place("1.4", [gw.instruction(price_h=350)], book=closed4, source="api")
            state = env.journal.last_paper_state()
            # 1.3's and 1.4's markers were evicted (the floor is 1.4's close); 1.1's stays (its kept book is OPEN).
            self.assertEqual((state["closed"], state["books"]["1.3"]["status"], state["books"]["1.1"]["status"]),
                             ({"markets": {"1.1": book_time_ms(closed)}, "floor_ms": book_time_ms(closed4)},
                              "CLOSED", "OPEN"))
            self.assert_market_closed(env.gateway, "1.3", [between])
            self.assert_market_closed(env.gateway, "1.1", [gw.paper_book(clock)])
            env.gateway.close()
            gateway, _ = self.reopened()
            self.assert_market_closed(gateway, "1.3", [between])
            self.assert_market_closed(gateway, "1.1", [gw.paper_book(clock)])
            self.assertEqual(gateway.advance("1.3", book=closed3)["decision"], "advanced")
        order = env.journal.fold(now_ns=clock.time_ns())["orders"][resting["customer_order_ref"]]
        self.assertEqual((order["state"], order["size_matched_p"]), ("LAPSED", 0))

    def test_an_older_closed_book_keeps_its_market_closed_when_markers_are_evicted(self):
        """Finding (attack2-bypass A, R2 follow-up): a CLOSED book older than the market's kept OPEN book (paper-step
        ignores it, place refuses it) marks the market but cannot replace the kept book. Evicting that marker (the
        earliest close goes first) reopened the market: the floor spares a market holding a timed book, and the kept
        OPEN book's chronology took any later book, so the BACK resting from before the close filled passively and
        settled for 500p after the CLOSED book had been seen. When the floor had already passed the CLOSED book, the
        marker was evicted in the same call: nothing changed, nothing was journaled, the market stayed open. Such a
        marker is never evicted now; later closes evict the others, and the market stays closed after a reopen too
        until a CLOSED step newer than its kept book lapses the resting bet."""
        env, clock = self.env, self.env.clock
        t0 = clock.time_ns()

        def shown(market, at_ns, traded):
            return gw.paper_book(clock, market_id=market, observed_at=gw.utc_from_ns(at_ns), runners={
                str(gw.SELECTION): orders.runner(back=((3.45, 40.0),), lay=((3.5, 1.0), (3.55, 80.0)),
                                                 traded={"3.5": traded}),
                str(gw.OTHER_SELECTION): orders.runner()})

        def closed_at(market, at_ns):
            return dict(self.closed_book(market), observed_at=gw.utc_from_ns(at_ns))

        clock.advance(2)
        for market in ("1.1", "1.2"):                              # BACK 2.00 @ 3.50 rests on a book at t0 + 1 s
            resting = env.gateway.place(market, [gw.instruction(price_h=350, size_p=200)],
                                        book=shown(market, t0 + 10**9, 0.0), source="api")["orders"][0]
            self.assertEqual((resting["state"], resting["size_matched"]), ("EXECUTABLE", 0.0))
        stepped = env.gateway.advance("1.1", book=closed_at("1.1", t0))   # CLOSED at t0: older than the kept book
        self.assertEqual(stepped["refusal"]["rule"], "paper_book_older_than_previous")
        with patch.object(gateway_module, "PAPER_CLOSED_MARKERS_MAX", 2):
            for market in ("1.5", "1.6"):                             # two later closes: the floor passes t0 + 2 s
                self.assertEqual(env.gateway.advance(market, book=self.closed_book(market))["decision"], "advanced")
                clock.advance(1)
            self.assertEqual(env.gateway._paper_closed_floor_ms, (t0 + 2 * 10**9) // 10**6)
            count = len(list(env.journal.events()))
            with self.assertRaises(OrderRefused) as caught:            # CLOSED at t0 + 0.5 s, before the floor
                env.gateway.place("1.2", [gw.instruction(price_h=350)], book=closed_at("1.2", t0 + 5 * 10**8),
                                  source="api")
            self.assertEqual(caught.exception.rule, "paper_book_older_than_last_seen")
            note = list(env.journal.events())[count:]
            self.assertEqual([(e["kind"], e["payload"]["reason"]) for e in note], [("note", "paper_market_closed")])
            closed = {"markets": {"1.1": t0 // 10**6, "1.2": (t0 + 5 * 10**8) // 10**6},
                      "floor_ms": (t0 + 3 * 10**9) // 10**6}            # 1.5's and 1.6's markers went
            state = env.journal.last_paper_state()
            self.assertEqual((state["closed"], state["books"]["1.1"]["status"], state["books"]["1.2"]["status"]),
                             (closed, "OPEN", "OPEN"))
            # Books after the kept one, traded volume rising at 3.50: no fill, no order, nothing journaled.
            window = [shown(m, t0 + 15 * 10**8, 500.0) for m in ("1.1", "1.2")]
            later = [shown(m, t0 + 18 * 10**8, 1000.0) for m in ("1.1", "1.2")]
            for market, books in (("1.1", [window[0], later[0]]), ("1.2", [window[1], later[1]])):
                self.assert_market_closed(env.gateway, market, books)
            env.gateway.close()
            gateway, _ = self.reopened()
            self.assertEqual((dict(gateway._paper_closed), gateway._paper_closed_floor_ms),
                             (closed["markets"], closed["floor_ms"]))
            for market, books in (("1.1", [window[0], later[0]]), ("1.2", [window[1], later[1]])):
                self.assert_market_closed(gateway, market, books + [gw.paper_book(clock, market_id=market)])
            for market in ("1.1", "1.2"):                             # a CLOSED step newer than the kept book
                self.assertEqual(gateway.advance(market, book=self.closed_book(market))["decision"], "advanced")
            self.assertEqual(env.journal.last_paper_state()["books"], {})
        self.assertEqual(self.settled(), [("1.1", "LAPSED", 0, None), ("1.2", "LAPSED", 0, None)])

    def test_a_paper_gateway_that_lost_its_lock_journals_no_paper_state(self):
        """Findings (attack2-bypass B, attack2-regressions): the paper state is last-writer-wins and open() loads it
        once. After .paper.lock was unlinked under gateway A and B opened on a new lock file, A still journaled its
        stale paper state: in the closed-seen note of a place or replace on a CLOSED book (before V9, with no lock
        check, where place had journaled one only after a submit passed _submit_refusal), and in a paper-step, a
        cancel or a replace's refusal report. Every later open then lost B's takes (C took the same displayed size
        again: 200p matched on a 2.0 display) and B's closed markers (the pre-close book was taken, then settled for
        500p). A paper gateway now journals a paper state only while it holds its paper lock: each of these refuses
        gateway_lock_lost before anything is journaled, and a place on an OPEN book is refused at its submit as
        before (its refusal carries no paper state)."""
        env, clock = self.env, self.env.clock
        a = env.gateway
        os.unlink(env.paths.paper_lock)                           # a "stale lock" clean-up under A
        b, _ = self.reopened()                                    # B locks the new file
        shown = gw.paper_book(clock, runners={str(gw.SELECTION): orders.runner(back=((3.5, 2.0),), lay=((3.55, 80.0),)),
                                              str(gw.OTHER_SELECTION): orders.runner()})
        placed = b.place("1.1", [gw.instruction(price_h=350, size_p=200)], book=shown, source="api")["orders"][0]
        self.assertEqual((placed["state"], placed["size_matched"]), ("EXECUTABLE", 1.0))
        resting = b.place("1.3", [gw.instruction(price_h=400)], book=gw.paper_book(clock, market_id="1.3"),
                          source="api")["orders"][0]
        pre = gw.paper_book(clock, market_id="1.2")
        clock.advance(1)
        refused = b.place("1.2", [gw.instruction(price_h=350)], book=self.closed_book("1.2"), source="api")
        self.assertEqual(refused["refusal"]["rule"], "validation:V9")
        state = env.journal.last_paper_state()
        self.assertEqual((state["exchange"]["taken"]["1.1"], state["closed"]["markets"]),
                         ({f"{gw.SELECTION}:back:350": 100}, {"1.2": book_time_ms(self.closed_book("1.2"))}))
        self.assertEqual((a._lock.held(), b._lock.held()), (False, True))
        head = env.journal.anchor()
        attempts = {
            "place on a CLOSED book": lambda: a.place("1.4", [gw.instruction()], book=self.closed_book("1.4"),
                                                      source="api"),
            "replace on a CLOSED book": lambda: a.replace("1.3", [{"bet_id": resting["bet_id"], "new_price_h": 350}],
                                                          book=self.closed_book("1.3"), source="api"),
            "replace on an OPEN book": lambda: a.replace("1.3", [{"bet_id": resting["bet_id"], "new_price_h": 350}],
                                                         book=gw.paper_book(clock, market_id="1.3"), source="api"),
            "paper-step, CLOSED": lambda: a.advance("1.4", book=self.closed_book("1.4")),
            "paper-step, OPEN": lambda: a.advance("1.1", book=gw.paper_book(clock)),
            "cancel": lambda: a.cancel("1.3", [resting["bet_id"]], source="api"),
        }
        for name, attempt in attempts.items():
            with self.subTest(name):
                with self.assertRaises(OrderRefused) as caught:
                    attempt()
                self.assertEqual(caught.exception.rule, "gateway_lock_lost")
                self.assertEqual(env.journal.anchor(), head)              # nothing journaled
        result = a.place("1.1", [gw.instruction(price_h=350, size_p=200)], book=shown, source="api")
        self.assertEqual((result["decision"], result["refusal"]["rule"]), ("refused", "gateway_lock_lost"))
        self.assertEqual(env.journal.last_paper_state(), state)
        a.close()
        b.close()
        c, _ = self.reopened()
        again = c.place("1.1", [gw.instruction(price_h=350, size_p=200)], book=shown, source="api")["orders"][0]
        self.assertEqual(again["size_matched"], 0.0)                      # B's take still counts
        with self.assertRaises(OrderRefused) as caught:
            c.place("1.2", [gw.instruction(price_h=350)], book=pre, source="api")
        self.assertEqual(caught.exception.rule, "paper_market_closed")
        # The lock replaced in the middle of a request (after its submit passed): the report and its paper state
        # are refused, and the next open resolves the place exactly (paper_interrupted_before_report).
        original = c.paper.place

        def place_then_lose_the_lock(*args, **kwargs):
            os.unlink(env.paths.paper_lock)
            return original(*args, **kwargs)

        state = env.journal.last_paper_state()
        count = len(list(env.journal.events()))
        with patch.object(c.paper, "place", place_then_lose_the_lock), self.assertRaises(OrderRefused) as caught:
            c.place("1.5", [gw.instruction(price_h=350)], book=gw.paper_book(clock, market_id="1.5"), source="api")
        self.assertEqual(caught.exception.rule, "gateway_lock_lost")
        self.assertEqual([e["kind"] for e in list(env.journal.events())[count:]], ["intent", "risk_decision", "submit"])
        self.assertEqual(env.journal.last_paper_state(), state)
        c.close()
        d, report = self.reopened()
        self.assertEqual(len(report["interrupted_resolved"]), 1)
        orders_now = env.journal.fold(now_ns=clock.time_ns())["orders"]
        self.assertEqual({(o["market_id"], o["state"], o["error_code"]) for o in orders_now.values()
                          if o["market_id"] == "1.5"}, {("1.5", "FAILED", "paper_interrupted_before_report")})
        self.assertEqual(sum(o["size_matched_p"] for o in orders_now.values() if o["market_id"] == "1.1"), 100)

    def test_paper_state_from_an_older_profile_loads_until_journaled(self):
        """R3#12 legacy: paper-state.json written before R3#12 is read while no journal event carries a paper
        state, and never written again; a malformed one stops the open."""
        env, clock = self.env, self.env.clock
        book = gw.paper_book(clock, runners={str(gw.SELECTION): orders.runner(back=((3.5, 4.0),),
                                                                             lay=((3.55, 80.0),))})
        env.gateway.close()
        legacy = env.paths.orders_dir / "paper-state.json"
        legacy.write_text(json.dumps({"schema_version": 1, "queues": {}, "books": {}, "exchange": {
            "taken": {"1.1": {f"{gw.SELECTION}:back:350": 200}}, "activated_ms": {}, "traded_hwm": {}}}))
        env.gateway.open(command="place")
        result = env.gateway.place("1.1", [gw.instruction(price_h=350, size_p=200)], book=book, source="api")
        self.assertEqual(result["orders"][0]["size_matched"], 0.0)          # the legacy takes still count
        env.gateway.close()
        self.assertEqual(env.journal.last_paper_state()["exchange"]["taken"],
                         {"1.1": {f"{gw.SELECTION}:back:350": 200}})
        legacy.write_text("{not json")
        env.gateway.open(command="place")                                    # the journal's state wins now
        env.gateway.close()
        empty = gw.build_paper_gateway(self)
        empty.gateway.close()
        (empty.paths.orders_dir).mkdir(parents=True, exist_ok=True)
        (empty.paths.orders_dir / "paper-state.json").write_text("{not json")
        with self.assertRaises(gateway_module.JournalError) as caught:
            empty.gateway.open(command="place")
        self.assertEqual(str(caught.exception), "paper_state_unreadable")


class LiveGatewayTests(unittest.TestCase):
    def setUp(self):
        self.env = gw.build_live_gateway(self)
        self.gateway = self.env.gateway

    def open(self, command="place", **lines):
        return gw.open_live(self, self.env, command, **lines)

    def place(self, **changes):
        return self.gateway.place("1.1", [gw.instruction(**changes)], source="cli_manual", confirm_token=self.env.token)

    def state(self, env=None):
        env = env or self.env
        return env.journal.fold(now_ns=env.clock.time_ns())

    def test_api_source_refused_live(self):
        self.open()
        with self.assertRaises(OrderRefused) as caught:
            self.gateway.place("1.1", [gw.instruction()], source="api", confirm_token=self.env.token)
        self.assertEqual(caught.exception.rule, "api_source_paper_only")
        with self.assertRaises(OrderRefused):
            self.gateway.cancel("1.1", ["300000000002"], source="api")
        with self.assertRaises(ValueError):
            self.gateway.place("1.1", [gw.instruction()], source="script", confirm_token=self.env.token)
        self.assertEqual(self.env.router.order_requests, [])

    def test_live_place_body_shape(self):
        self.open(instructions=[gw.instruction(side="LAY", price_h=355, size_p=200)])
        result = self.place(side="LAY", price_h=355, size_p=200)
        self.assertEqual(result["decision"], "accepted")
        request = self.env.router.order_requests[0]
        self.assertTrue(request["method"].endswith(gw.PLACE_SUFFIX))
        params = request["params"]
        self.assertEqual(set(params), {"marketId", "customerRef", "customerStrategyRef", "marketVersion", "instructions"})
        self.assertEqual(params["marketId"], "1.1")
        self.assertRegex(params["customerRef"], r"^brq[0-9]{12}-[0-9a-f]{16}$")
        self.assertEqual(params["customerStrategyRef"], "bar-manual")
        self.assertEqual(params["marketVersion"], {"version": self.env.exchange.version})
        instruction = params["instructions"][0]
        self.assertEqual(set(instruction), {"orderType", "selectionId", "handicap", "side", "customerOrderRef",
                                            "limitOrder"})
        self.assertEqual(instruction["orderType"], "LIMIT")
        self.assertEqual(instruction["limitOrder"], {"size": 2.0, "price": 3.55, "persistenceType": "LAPSE"})
        self.assertEqual(instruction["customerOrderRef"], result["orders"][0]["customer_order_ref"])
        body = request["body"]
        self.assertEqual(request["digest"], bytes_sha256(body))
        envelope = json.loads(body)
        self.assertEqual(set(envelope), {"jsonrpc", "method", "params", "id"})
        for absent in ("async", "timeInForce", "minFillSize", "betTargetType"):
            self.assertNotIn(absent.encode(), body)
        submit = [e for e in self.env.journal.events() if e["kind"] == "submit"][0]
        self.assertEqual(submit["payload"]["body_sha256"], request["digest"])
        self.assertEqual(submit["customer_ref"], params["customerRef"])
        # The exact params sent are journaled with the submit, so the body digest can be audited.
        self.assertEqual(submit["payload"]["params"], params)
        self.assertEqual(self.env.confirmer.prompts[0]["expected"], "1.1 12345 LAY 3.55 2.00")
        self.assertTrue(self.env.confirmer.prompts[0]["banner"].startswith("MODE=LIVE"))
        self.assertEqual(kinds(self.env.journal)[-6:], ["operator_confirm", "funds_check", "intent", "risk_decision",
                                                        "submit", "report"])
        confirm = [e for e in self.env.journal.events() if e["kind"] == "operator_confirm"][-1]
        self.assertEqual(confirm["payload"], {"echo": "1.1 12345 LAY 3.55 2.00", "typed_before_lock": True})
        intent = [e for e in self.env.journal.events() if e["kind"] == "intent"][-1]
        self.assertEqual(intent["payload"]["source"], "cli_manual")

    def test_operator_prompt_never_holds_the_global_lock(self):
        env = self.env
        seen = []

        class LockProbingConfirmer(gw.FakeConfirmer):
            def confirm_line(inner, expected, *, banner):
                try:
                    with file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock):
                        seen.append("lock_free")
                except LockBusy:
                    seen.append("lock_held")
                return super().confirm_line(expected, banner=banner)

        env.gateway.confirmer = LockProbingConfirmer()
        self.open(instructions=[gw.instruction()])
        self.assertEqual(self.place()["decision"], "accepted")
        self.assertEqual(seen, ["lock_free"])
        # Typing after open() would hold the lock during the prompt: refused.
        with self.assertRaises(OrderRefused) as caught:
            self.gateway.preconfirm("place", market_id="1.1", instructions=[gw.instruction(price_h=360)])
        self.assertEqual(caught.exception.rule, "preconfirm_after_open")
        # An order nobody typed is refused before any read or request.
        before = len(self.env.transport.requests)
        with self.assertRaises(OrderRefused) as caught:
            self.place(price_h=360)
        self.assertEqual(caught.exception.rule, "operator_confirmation_missing")
        self.assertEqual(len(self.env.transport.requests), before)
        self.assertEqual(len(self.env.router.order_requests), 1)
        # Each typed line is used once.
        with self.assertRaises(OrderRefused):
            self.place()

    def test_live_book_argument_refused(self):
        self.open()
        before = len(self.env.transport.requests)
        with self.assertRaises(OrderRefused) as caught:
            self.gateway.place("1.1", [gw.instruction()], book=orders.live_book(self.env.clock), source="cli_manual",
                               confirm_token=self.env.token)
        self.assertEqual(caught.exception.rule, "book_source_not_allowed_live")
        with self.assertRaises(OrderRefused) as caught:
            self.gateway.replace("1.1", [{"bet_id": "1", "new_price_h": 360}], book=orders.live_book(self.env.clock),
                                 source="cli_manual", confirm_token=self.env.token)
        self.assertEqual(caught.exception.rule, "live_replace_removed_v1")
        self.assertEqual(len(self.env.transport.requests), before)

    def test_live_self_fetch_with_live_key(self):
        self.open(instructions=[gw.instruction(), gw.instruction()])
        before = len(self.env.transport.requests)
        self.place()
        book_requests = [r for r in self.env.transport.requests[before:] if b"listMarketBook" in (r["body"] or b"")]
        self.assertEqual(len(book_requests), 1)
        self.assertEqual(self.env.transport.header_text(book_requests[0], "X-Application"), gw.LIVE_KEY)
        decision = [e for e in self.env.journal.events() if e["kind"] == "risk_decision"][-1]
        self.assertTrue(decision["payload"]["approved"])
        # A delayed book (an unfunded or overridden live key) fails A18 and V10.
        self.env.exchange.delayed = True
        refused = self.place()
        self.assertEqual(refused["decision"], "refused")
        self.assertEqual(refused["refusal"]["rule"], "not_armed")
        reasons = {f["reason"] for f in refused["refusal"]["detail"]["failures"]}
        self.assertIn("market_data_delayed_or_unknown", reasons)
        with self.assertRaises(OrderRefused) as caught:
            self.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token="live-wrong")
        self.assertEqual(caught.exception.rule, "confirm_token_mismatch")

    def test_book_fetched_after_slow_confirmation(self):
        # The operator takes longer than max_book_age_s (2.0) to type; the book is only fetched afterwards.
        self.env.confirmer.clock, self.env.confirmer.delay_s = self.env.clock, 3.0
        self.open(instructions=[gw.instruction(price_h=360)])
        before = len(self.env.transport.requests)
        result = self.place(price_h=360)
        books = [r for r in self.env.transport.requests[before:] if b"listMarketBook" in (r["body"] or b"")]
        self.assertEqual(len(books), 1)
        self.assertEqual(result["decision"], "accepted")

    def test_refusal_no_transport_call(self):
        self.open(instructions=[gw.instruction(size_p=500)])
        result = self.place(size_p=500)
        self.assertEqual(result["decision"], "refused")
        self.assertEqual(result["refusal"]["rule"], "stake_cap")
        self.assertEqual(self.env.router.order_requests, [])
        events = list(self.env.journal.events())
        self.assertEqual([e["kind"] for e in events][-2:], ["intent", "risk_decision"])
        self.assertEqual(result["orders"][0]["state"], "REFUSED")
        self.assertNotIn("submit", kinds(self.env.journal))
        # A mistyped line is refused at the terminal, before the lock, any read or any order.
        self.gateway.close()
        self.env.confirmer.lines = ["1.1 12345 BACK 3.50 2.01"]
        with self.assertRaises(OrderRefused) as caught:
            self.gateway.preconfirm("place", market_id="1.1", instructions=[gw.instruction()])
        self.assertEqual(caught.exception.rule, "operator_echo_mismatch")
        self.assertEqual(self.env.router.order_requests, [])

    def test_guard_raises_no_transport_call(self):
        self.open(instructions=[gw.instruction(), gw.instruction()])
        with patch.object(self.env.guard, "issue", side_effect=OrderRefused("capability_decision_stale")):
            result = self.place()
        self.assertEqual(result["decision"], "refused")
        self.assertEqual(result["refusal"]["rule"], "capability_decision_stale")
        self.assertEqual(result["orders"][0]["state"], "REFUSED")
        with patch.object(self.env.guard, "evaluate_place", side_effect=RuntimeError("guard failure")):
            with self.assertRaises(RuntimeError):
                self.place()
        self.assertEqual(self.env.router.order_requests, [])

    def test_signals_masked_around_send(self):
        """SIGINT, SIGTERM and SIGHUP are held from just before the send until the report is journaled, and the
        on_send hook (the CLI defers signals from then on, R3#16) runs under the mask right before the request
        (after any cancel mint, so a mint refusal is never reported as a request sent: round 4)."""
        self.open(instructions=[gw.instruction()])
        order_of_events = []
        real_mask = signal.pthread_sigmask
        self.gateway.on_send = lambda: order_of_events.append(("on_send", None))

        def recording_mask(how, mask):
            order_of_events.append(("block" if how == signal.SIG_BLOCK else "restore",
                                    sorted(int(s) for s in mask) if how == signal.SIG_BLOCK else None))
            return real_mask(how, mask)

        router = self.env.transport.router

        def routed(record):
            if b"place" + b"Orders" in (record["body"] or b""):
                order_of_events.append(("send", None))
            return router(record)

        self.env.transport.router = routed
        original = self.env.journal.append

        def append(kind, payload, **columns):
            if kind == "report":
                order_of_events.append(("report", None))
            return original(kind, payload, **columns)

        with patch.object(gateway_module.signal, "pthread_sigmask", recording_mask), \
                patch.object(self.env.journal, "append", append):
            self.place()
        self.assertEqual([e[0] for e in order_of_events], ["block", "on_send", "send", "report", "restore"])
        self.assertEqual(order_of_events[0][1], sorted([int(signal.SIGINT), int(signal.SIGTERM), int(signal.SIGHUP)]))

    def test_startup_unknown_blocks(self):
        journal = self.env.journal
        ref, cref = "bro200101120000-0000000000000abc", "brq200101120000-0000000000000abc"
        journal.append("intent", orders.intent_payload(gw.instruction(), source="cli_manual"), intent_id="int-crash",
                       customer_order_ref=ref, customer_ref=cref, market_id="1.1", state_after="RECORDED")
        journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                  "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                       intent_id="int-crash", market_id="1.1")
        from betangel_research.betfair.credentials import anchor_write
        anchor_write("default", dict(journal.anchor(), updated_at=self.env.clock.utc_now()), runner=self.env.runner)
        report = self.open()
        self.assertEqual(report["stale_to_unknown"], [ref])
        self.assertFalse(report["armed"])
        self.assertIn("unresolved_or_foreign_orders", report["failures"])
        state = journal.fold(now_ns=self.env.clock.time_ns())
        self.assertEqual(state["orders"][ref]["state"], "ABSENT_UNCONFIRMED")
        prompts = len(self.env.confirmer.prompts)
        with self.assertRaises(NotArmed) as caught:        # refused before any prompt, read or order
            self.place()
        self.assertEqual(caught.exception.rule, "unresolved_or_foreign_orders")
        self.assertEqual(len(self.env.confirmer.prompts), prompts)
        self.assertEqual(self.env.router.order_requests, [])

    def test_terminal_account_halts(self):
        self.open(instructions=[gw.instruction(), gw.instruction()])
        self.env.exchange.place_handler = failure_report("INSUFFICIENT_FUNDS")
        result = self.place()
        self.assertEqual(result["orders"][0]["state"], "FAILED")
        state = self.state()
        self.assertTrue(state["halted"])
        self.assertIn("terminal_account", state["halt_reasons"])
        self.env.exchange.place_handler = None
        self.assertEqual(self.place()["refusal"]["rule"], "halted")
        self.assertEqual(len(self.env.router.order_requests), 1)

    def test_terminal_input_validator_gap_halt(self):
        self.open(instructions=[gw.instruction()])
        self.env.exchange.place_handler = failure_report("INVALID_BET_SIZE")
        result = self.place()
        self.assertEqual(result["orders"][0]["state"], "FAILED")
        self.assertIn("validator_gap", self.state()["halt_reasons"])

    def test_market_state_no_halt(self):
        self.open(instructions=[gw.instruction(), gw.instruction()])
        self.env.exchange.place_handler = failure_report("MARKET_NOT_OPEN_FOR_BETTING", exec_code="MARKET_SUSPENDED")
        result = self.place()
        self.assertEqual(result["orders"][0]["state"], "FAILED")
        self.assertFalse(self.state()["halted"])
        self.env.exchange.place_handler = None
        self.assertEqual(self.place()["decision"], "accepted")

    def test_unknown_outcomes_and_unrecognized_codes(self):
        self.open()
        cases = [
            (gw.raises(TransportError("connection_lost", phase="receive")), "ABSENT_UNCONFIRMED", False),
            (gw.raises(TransportError("connect_failed", phase="connect")), "FAILED", False),
            (failure_report("SOMETHING_NEW_FROM_BETFAIR"), "ABSENT_UNCONFIRMED", True),
        ]
        for index, (handler, expected, halts) in enumerate(cases):
            with self.subTest(case=index):
                env = gw.build_live_gateway(self)
                gw.open_live(self, env, "place", instructions=[gw.instruction()])
                env.exchange.place_handler = handler
                result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
                self.assertEqual(result["orders"][0]["state"], expected)
                state = env.journal.fold(now_ns=env.clock.time_ns())
                self.assertEqual("unrecognized" in state["halt_reasons"], halts)
                self.assertEqual(len(env.router.order_requests), 1)
        # An ApiError on the order call with category app_key halts; one never resent.
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        env.router.next_response[gw.PLACE_SUFFIX] = [lambda record: support.rpc_error_response(
            -32099, "x", data={"exceptionname": "APINGException", "APINGException": {"errorCode": "INVALID_APP_KEY"}},
            rpc_id=json.loads(record["body"])["id"])]
        result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual(result["orders"][0]["state"], "FAILED")
        self.assertIn("app_key", env.journal.fold(now_ns=env.clock.time_ns())["halt_reasons"])

    def test_non_betfair_exception_from_send_is_unknown_and_halts(self):
        self.open(instructions=[gw.instruction()])
        with patch.object(self.env.rpc, "send_order", side_effect=RuntimeError("synthetic bug")):
            with self.assertRaises(RuntimeError):
                self.place()
        state = self.state()
        self.assertIn("unrecognized", state["halt_reasons"])
        report = [e for e in self.env.journal.events() if e["kind"] == "report"][-1]
        self.assertEqual(report["payload"]["exception"]["type"], "RuntimeError")
        self.assertEqual([o["state"] for o in state["orders"].values()], ["UNKNOWN"])

    def test_three_consecutive_unknown_outcomes_halt(self):
        """R3#13: the halt rides in the third UNKNOWN report's own payload (no separate halt event that a death
        could lose), and every later UNKNOWN report halts again while the count stays at 3 or more, so a fourth
        unknown outcome after a resume halts again (resume does not reset the count)."""
        self.open(instructions=[gw.instruction(), gw.instruction(price_h=360)])
        bet_id = self.place()["orders"][0]["bet_id"]
        self.env.exchange.cancel_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        for _ in range(2):                      # each cancel outcome is unknown, then found EXECUTABLE again
            self.gateway.cancel("1.1", [bet_id], source="cli_manual")
        self.assertEqual(self.state()["consecutive_unknown"], 2)
        self.assertFalse(self.state()["halted"])
        reports = [e for e in self.env.journal.events() if e["kind"] == "report"]
        self.assertNotIn("halts", reports[-1]["payload"])
        self.env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        self.place(price_h=360)
        state = self.state()
        self.assertTrue(state["halted"])
        self.assertIn("consecutive_unknown_outcomes", state["halt_reasons"])
        third = [e for e in self.env.journal.events() if e["kind"] == "report"][-1]
        self.assertEqual(third["payload"]["halts"], ["consecutive_unknown_outcomes"])
        self.assertEqual(third["payload"]["transitions"][0]["state"], "UNKNOWN")
        self.assertFalse(any(e["kind"] == "halt" for e in self.env.journal.events()))
        # Resume (token and typed phrase), then a fourth unknown outcome halts again.
        self.gateway.close()
        self.assertFalse(self.gateway.resume(confirm_token=self.env.token)["halted"])
        self.open("cancel")
        self.gateway.cancel("1.1", [bet_id], source="cli_manual")
        state = self.state()
        self.assertEqual(state["consecutive_unknown"], 4)
        self.assertEqual((state["halted"], state["halt_reasons"]), (True, ["consecutive_unknown_outcomes"]))
        fourth = [e for e in self.env.journal.events() if e["kind"] == "report"][-1]
        self.assertEqual(fourth["payload"]["halts"], ["consecutive_unknown_outcomes"])

    def test_report_halt_rides_in_the_report_event(self):
        """Regression R3#13: the automatic HALTs used to be journaled as a separate halt event after the report
        (or risk decision) whose moves triggered them, so a process death between the two appends lost the HALT
        for good and the next live place was armed. They now ride in that same event: a death right after it
        still leaves the journal halted. Cases: a terminal_account report code (the order is FAILED), the third
        consecutive unknown outcome, and R12's unexplained account exposure (a refusing risk_decision)."""
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        env.exchange.place_handler = failure_report("LOSS_LIMIT_EXCEEDED")
        with die_after(env.journal, "report"), self.assertRaises(_Death):
            env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        state = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertEqual([o["state"] for o in state["orders"].values()], ["FAILED"])
        self.assertEqual((state["halted"], state["halt_reasons"]), (True, ["terminal_account"]))
        self.assertEqual(kinds(env.journal)[-1], "report")
        # Third consecutive unknown outcome (three cancels whose outcome is unknown): the report that makes it
        # third carries the halt.
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        bet = env.gateway.place("1.1", [gw.instruction()], source="cli_manual",
                                confirm_token=env.token)["orders"][0]["bet_id"]
        env.exchange.cancel_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        for _ in range(2):
            env.gateway.cancel("1.1", [bet], source="cli_manual")
        self.assertFalse(env.journal.fold(now_ns=env.clock.time_ns())["halted"])
        with die_after(env.journal, "report"), self.assertRaises(_Death):
            env.gateway.cancel("1.1", [bet], source="cli_manual")
        state = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertEqual((state["consecutive_unknown"], state["halted"], state["halt_reasons"]),
                         (3, True, ["consecutive_unknown_outcomes"]))
        # R12: the refusing risk_decision carries the halt; nothing is sent.
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        funds = dict(env.exchange.funds(), exposure=-5.0)          # Betfair shows 5.00 we cannot explain
        with patch.object(env.exchange, "funds", return_value=funds), die_after(env.journal, "risk_decision"), \
                self.assertRaises(_Death):
            env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        decision = [e for e in env.journal.events() if e["kind"] == "risk_decision"][-1]
        self.assertEqual((decision["payload"]["rule"], decision["payload"]["halts"]),
                         ("unexplained_account_exposure", ["unexplained_account_exposure"]))
        state = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertEqual((state["halted"], state["halt_reasons"]), (True, ["unexplained_account_exposure"]))
        self.assertEqual(env.router.order_requests, [])

    def test_recorded_orphans_refused_at_open(self):
        """Regression R3#14: an order left RECORDED by a death between its intent and its submit stayed RECORDED
        for good; a provisional replace child (paper) kept its whole size as adverse exposure every day. Every
        open now journals each RECORDED order of an earlier process as REFUSED(crash_before_submit): safe,
        because a submit is journaled before any byte is sent."""
        # Live: a plain place intent, then death before its submit.
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        with die_after(env.journal, "intent"), self.assertRaises(_Death):
            env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        env.gateway.close()
        orphan = [o for o in env.journal.fold(now_ns=env.clock.time_ns())["orders"].values()][0]
        self.assertEqual(orphan["state"], "RECORDED")
        report = gw.open_live(self, env, "place")
        self.assertEqual((report["armed"], report["recorded_refused"], report["stale_to_unknown"]),
                         (True, [orphan["customer_order_ref"]], []))
        order = env.journal.fold(now_ns=env.clock.time_ns())["orders"][orphan["customer_order_ref"]]
        self.assertEqual((order["state"], order["error_code"]), ("REFUSED", "crash_before_submit"))
        self.assertEqual(env.router.order_requests, [])
        # Paper: a replace child journaled, then death before the submit; the parent is cancelled afterwards.
        paper = gw.build_paper_gateway(self)
        paper.gateway.open(command="place")
        book = gw.paper_book(paper.clock)
        bet = paper.gateway.place("1.1", [gw.instruction(price_h=370)], book=book, source="api")["orders"][0]["bet_id"]
        with die_after(paper.journal, "intent"), self.assertRaises(_Death):
            paper.gateway.replace("1.1", [{"bet_id": bet, "new_price_h": 380}], book=book, source="api")
        paper.gateway.close()
        state = paper.journal.fold(now_ns=paper.clock.time_ns())
        child = [o for o in state["orders"].values() if o["provisional_child"] is True][0]
        self.assertEqual((child["state"], exposure_by_market(list(state["orders"].values()))),
                         ("RECORDED", {"1.1": 200}))
        report = paper.gateway.open(command="place")
        self.addCleanup(paper.gateway.close)
        self.assertEqual(report["recorded_refused"], [child["customer_order_ref"]])
        paper.gateway.cancel("1.1", [bet], source="api")
        state = paper.journal.fold(now_ns=paper.clock.time_ns())
        child = state["orders"][child["customer_order_ref"]]
        self.assertEqual((child["state"], child["provisional_child"], child["error_code"]),
                         ("REFUSED", False, "crash_before_submit"))
        self.assertEqual(exposure_by_market(list(state["orders"].values())), {"1.1": 0})
        reopened = paper.gateway
        reopened.close()
        self.assertEqual(reopened.open(command="place")["recorded_refused"], [])          # nothing left to refuse

    def test_live_partial_cancel_refused(self):
        """Amendment A3 (D1, A23): live cancels are full cancels; a size reduction is refused before anything is
        checked, read or journaled, and a full cancel's wire body always carries sizeReduction null."""
        self.open(instructions=[gw.instruction()])
        bet_id = self.place()["orders"][0]["bet_id"]
        before = (len(list(self.env.journal.events())), len(self.env.transport.requests))
        for reductions in ([100], [None, 100]):
            with self.subTest(reductions=reductions):
                with self.assertRaises(OrderRefused) as caught:
                    self.gateway.cancel("1.1", [bet_id] * len(reductions), source="cli_manual",
                                        size_reductions=reductions)
                self.assertEqual(caught.exception.rule, "live_partial_cancel_removed_v1")
        self.assertEqual((len(list(self.env.journal.events())), len(self.env.transport.requests)), before)
        self.assertEqual(self.env.exchange.orders[bet_id]["sizeRemaining"], 2.0)
        cancelled = self.gateway.cancel("1.1", [bet_id], source="cli_manual", size_reductions=[None])
        self.assertEqual(cancelled["orders"][0]["state"], "EXECUTION_COMPLETE")
        self.assertEqual(self.env.router.order_requests[-1]["params"]["instructions"],
                         [{"betId": bet_id, "sizeReduction": None}])

    def test_kill_during_a_send_leaves_the_cancel_to_the_kill(self):
        """Spec step 16 was deleted (amendment A3, D4): a holder whose bytes were already sent when the KILL
        file appeared journals its report and cancels nothing itself (its duplicate cancel only left an
        UNKNOWN behind). The kill, which needs no lock, cancels the bet while the holder is still open."""
        self.open(instructions=[gw.instruction()])
        exchange = self.env.exchange

        def place_then_kill(params):
            exchange.place_handler = None
            try:
                return exchange.place(params)
            finally:
                engage(self.env.paths, "kill during send", clock=self.env.clock)

        exchange.place_handler = place_then_kill
        result = self.place()
        names = [r["method"].split("/")[-1] for r in self.env.router.order_requests]
        self.assertEqual(names, [gw.PLACE_SUFFIX])
        bet = result["orders"][0]["bet_id"]
        self.assertEqual(result["orders"][0]["state"], "EXECUTABLE")
        killer = AuditRegressionTests.second_gateway(self, self.env)
        killer.open(command="kill")
        self.addCleanup(killer.close)
        self.assertIsNotNone(self.gateway._lock)              # the holder is still open, holding its lock
        self.assertEqual(killer.kill(scope="own", reason="operator kill")["status"], "kill_complete")
        self.assertEqual(exchange.orders[bet]["status"], "EXECUTION_COMPLETE")

    def test_foreign_bet_cancel_refused(self):
        report = self.open("cancel")
        self.assertIn("startup_reconcile", kinds(self.env.journal))       # every live open reads the exchange
        self.assertTrue(report["armed"])
        foreign = self.env.exchange.add_order(strategy_ref=None, source_key="vendor-key")
        result = self.gateway.cancel("1.1", [foreign], source="cli_manual")
        self.assertEqual(result["decision"], "refused")
        self.assertTrue(result["refusal"]["rule"].startswith("validation:V2"))
        self.assertEqual(self.env.router.order_requests, [])
        self.assertEqual(self.env.exchange.orders[foreign]["status"], "EXECUTABLE")

    def test_cancel_refused_when_open_failed_a_check(self):
        from betangel_research.betfair.auth import account_fingerprint_for
        from betangel_research.betfair.secret import Secret
        fingerprint = account_fingerprint_for(Secret("synthetic-user"))
        self.env.paths.accounts_file.write_text(json.dumps({fingerprint: "elsewhere"}))
        report = self.open("cancel")
        self.assertFalse(report["armed"])
        with self.assertRaises(NotArmed) as caught:
            self.gateway.cancel("1.1", ["300000000002"], source="cli_manual")
        self.assertEqual(caught.exception.rule, "account_registered_to_other_profile")
        self.assertEqual(self.env.router.order_requests, [])

    def test_cancel_own_bet_live(self):
        self.open(instructions=[gw.instruction()])
        placed = self.place()
        bet_id = placed["orders"][0]["bet_id"]
        cancelled = self.gateway.cancel("1.1", [bet_id], source="cli_manual")
        self.assertEqual(cancelled["decision"], "accepted")
        self.assertEqual(cancelled["orders"][0]["state"], "EXECUTION_COMPLETE")
        request = self.env.router.order_requests[-1]
        self.assertEqual(request["params"]["instructions"], [{"betId": bet_id, "sizeReduction": None}])
        report = [e for e in self.env.journal.events() if e["kind"] == "report"][-1]
        self.assertEqual(report["payload"]["transactions"], 0)
        submit = [e for e in self.env.journal.events() if e["kind"] == "submit"][-1]
        self.assertEqual(submit["payload"]["params"], request["params"])

    def test_live_replace_refused_before_lock_login_or_network(self):
        """Regression R3#2 (moot by D2, amendment A3): a live replace whose parent partly matched in flight
        left its child UNKNOWN for good. Live replace is now removed: every entry point refuses with
        live_replace_removed_v1 before any lock, Keychain read, login, journal write or request, and no
        replaceOrders request can be built or minted."""
        self.open(instructions=[gw.instruction()])
        bet_id = self.place()["orders"][0]["bet_id"]
        self.gateway.close()
        env = self.env
        before = (len(list(env.journal.events())), len(env.transport.requests), len(env.runner.calls))
        replaces = [{"bet_id": bet_id, "new_price_h": 360}]
        with patch.object(gateway_module, "file_lock", side_effect=AssertionError("lock taken")):
            with self.assertRaises(ValueError):                         # nothing to type for a replace
                self.gateway.preconfirm("replace", market_id="1.1")
            with self.assertRaises(OrderRefused) as caught:
                self.gateway.open(command="replace", confirm_token=env.token)
            self.assertEqual(caught.exception.rule, "live_replace_removed_v1")
            self.assertIsNone(self.gateway._command)
            with self.assertRaises(OrderRefused) as caught:             # not even open: refused first
                self.gateway.replace("1.1", replaces, source="cli_manual", confirm_token=env.token)
            self.assertEqual(caught.exception.rule, "live_replace_removed_v1")
            with self.assertRaises(OrderRefused) as caught:
                env.guard.evaluate_replace("1.1", replaces, book=orders.live_book(env.clock), state=self.state(),
                                           arming=orders.ARMED, now_ns=env.clock.time_ns(), kill_engaged=False)
            self.assertEqual(caught.exception.rule, "live_replace_removed_v1")
        self.assertEqual((len(list(env.journal.events())), len(env.transport.requests), len(env.runner.calls)),
                         before)
        # Through a gateway opened (and armed) for place, replace is refused too and journals nothing.
        self.open()
        opened = len(list(env.journal.events()))
        with self.assertRaises(OrderRefused) as caught:
            self.gateway.replace("1.1", replaces, source="cli_manual", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "live_replace_removed_v1")
        self.assertEqual(len(list(env.journal.events())), opened)
        self.gateway.close()
        self.assertEqual([r["method"].split("/")[-1] for r in env.router.order_requests], [gw.PLACE_SUFFIX])
        self.assertNotIn(gw.REPLACE_SUFFIX, env.router.rpc_methods)
        self.assertFalse(any(e["kind"] == "replace_intent" for e in env.journal.events()))
        self.assertFalse(hasattr(gateway_module, "REPLACE_METHOD"))
        self.assertEqual(env.exchange.orders[bet_id]["status"], "EXECUTABLE")

    def test_no_secret_in_journal_result_or_errors(self):
        self.open(instructions=[gw.instruction(), gw.instruction(price_h=360)])
        results = [self.place()]
        self.env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        results.append(self.place(price_h=360))
        self.gateway.close()
        secrets = [gw.SESSION_TOKEN, gw.LIVE_KEY, "synthetic-user", "synthetic-password-0001",
                   "synthetic-delayed-key-0001"]
        blobs = [json.dumps(r) for r in results]
        for path in self.env.paths.root.rglob("*"):
            if path.is_file():
                blobs.append(path.read_bytes().decode("utf-8", "replace"))
        for secret in secrets:
            for blob in blobs:
                self.assertNotIn(secret, blob)
        for exc in (ApiError("INVALID_APP_KEY", surface="aping"), OrderRefused(gw.SESSION_TOKEN)):
            self.assertNotIn(gw.SESSION_TOKEN, str(exc))

    def test_global_lock_blocks_second_live_gateway_from_other_cwd(self):
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            with file_lock(self.env.paths.gateway_lock):
                started = self.env.clock.time_ns()
                with self.assertRaises(LockBusy):
                    self.gateway.open(command="place", confirm_token=self.env.token)
                waited = self.env.clock.time_ns() - started
            self.assertGreaterEqual(waited, 15 * 10**9)              # order_deadline_s + 5 on the fake clock
            self.assertFalse(any(b"login" in (r["url"] or "").encode() for r in self.env.transport.requests))
            report = self.gateway.open(command="place", confirm_token=self.env.token)
            self.addCleanup(self.gateway.close)
            self.assertTrue(report["armed"])
            other = OrderGateway(self.env.config, paths=self.env.paths, mode="live", journal=self.env.journal,
                                 guard=self.env.guard, rpc=self.env.rpc, read=self.env.read,
                                 confirmer=self.env.confirmer, clock=self.env.clock, env={},
                                 keychain_runner=self.env.runner)
            with self.assertRaises(LockBusy):
                other.open(command="cancel")

    def test_kill_sweeps_while_an_unknown_outcome_holder_keeps_the_lock(self):
        """Regression (audit, then amendment A3 D4): after an UNKNOWN outcome the holder reconciles under the
        global lock, and a kill used to wait for that lock and exit kill_incomplete_no_session unswept. The kill
        now takes no lock: it sweeps while the holder is still polling, and the holder's order stays counted
        at its worst case (UNKNOWN or ABSENT_UNCONFIRMED, never zero)."""
        env = self.env
        resting = gw.instruction(price_h=360)
        self.open(instructions=[resting, gw.instruction()])
        earlier = self.place(price_h=360)["orders"][0]["bet_id"]
        env.exchange.place_handler = gw.raises(TransportError("deadline_exceeded", phase="receive",
                                                              may_have_arrived=True))
        killer = AuditRegressionTests.second_gateway(self, env)
        sleep = env.clock.sleep
        seen = {}

        def kill_from_another_process(seconds):
            if not seen:
                seen["holder_lock_held"] = self.gateway._lock.held()
                engage(env.paths, "operator kill", clock=env.clock)
                killer.open(command="kill")
                seen["kill"] = killer.kill(scope="own", reason="operator kill")
                killer.close()
            sleep(seconds)

        with patch.object(env.clock, "sleep", side_effect=kill_from_another_process):
            result = self.place()
        self.assertTrue(seen["holder_lock_held"])
        self.assertEqual(seen["kill"]["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[earlier]["status"], "EXECUTION_COMPLETE")
        self.assertIn(result["orders"][0]["state"], ("UNKNOWN", "ABSENT_UNCONFIRMED"))
        state = self.state()
        self.assertGreater(exposure_by_market(list(state["orders"].values()))["1.1"], 0)
        self.assertNotIn("reconcile_abandoned_for_kill", json.dumps(list(env.journal.events())))

    def test_place_holder_stops_reconciling_when_the_kill_file_appears(self):
        """Amendment A3, D4: a place or live-probe holder stops reconciling once a KILL file exists (A7 refused
        one at open, so it is new), so it does not compete with the kill's reads (F18); its orders stay
        UNKNOWN at their worst case. Both places: after its own send, and while open() resolves old UNKNOWNs."""
        env = self.env
        self.open(instructions=[gw.instruction()])

        def lost_then_killed(params):
            engage(env.paths, "operator kill", clock=env.clock)
            raise TransportError("deadline_exceeded", phase="receive", may_have_arrived=True)

        env.exchange.place_handler = lost_then_killed
        env.exchange.calls.clear()
        result = self.place()
        self.assertEqual(result["orders"][0]["state"], "UNKNOWN")
        self.assertEqual(env.exchange.calls, [])                   # no poll: the kill gets the reads
        self.assertNotIn("reconcile", kinds(env.journal)[-2:])
        self.assertTrue(self.gateway._yields_to_kill("place"))
        self.assertTrue(self.gateway._yields_to_kill("live-probe"))
        self.assertFalse(self.gateway._yields_to_kill("cancel"))       # a cancel holder simply finishes
        self.gateway.close()
        # open(): two orders a crash left mid-request; a KILL file appears while the newest is polled. The
        # newest ref ends in f * 16, so it sorts above the place's own UNKNOWN (same stamp, random hex).
        os.unlink(env.paths.kill_file)
        journal = env.journal
        crashed = []
        for stamp in ("191231000000", "200101120000"):
            ref, cref = f"bro{stamp}-{'f' * 16}", f"brq{stamp}-{'f' * 16}"
            journal.append("intent", orders.intent_payload(gw.instruction(), source="cli_manual"),
                           intent_id=f"int-{stamp}", customer_order_ref=ref, customer_ref=cref, market_id="1.1",
                           state_after="RECORDED")
            journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                      "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                           intent_id=f"int-{stamp}", market_id="1.1")
            crashed.append(ref)
        anchor_write("default", dict(journal.anchor(), updated_at=env.clock.utc_now()), runner=env.runner)
        sleep = env.clock.sleep

        def kill_appears(seconds):
            if not kill_engaged(env.paths):
                engage(env.paths, "operator kill", clock=env.clock)
            sleep(seconds)

        env.exchange.calls.clear()
        with patch.object(env.clock, "sleep", side_effect=kill_appears):
            self.open()
        polled = {kw["customer_order_refs"][0] for name, kw in env.exchange.calls
                  if name == "listCurrentOrders" and kw.get("customer_order_refs")}
        self.assertEqual(polled, {crashed[1]})                    # newest first; then it stopped for the kill
        self.assertEqual(self.state()["orders"][crashed[0]]["state"], "UNKNOWN")

    def test_resolving_unknowns_on_open_folds_per_unknown_not_per_order(self):
        """Regression (audit): open() re-folded the whole journal once per journal order (any state), so a
        long journal used up the 120 s budget before the newest UNKNOWN order (whose ref sorts last) was
        ever polled, and live trading stayed blocked with no tool able to reach it."""
        journal = self.env.journal
        for i in range(40):
            ref, cref = f"bro191231000000-{i:016x}", f"brq191231000000-{i:016x}"
            journal.append("intent", orders.intent_payload(gw.instruction(), source="cli_manual"),
                           intent_id=f"int-{i}", customer_order_ref=ref, customer_ref=cref, market_id="1.2",
                           state_after="RECORDED")
            journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                      "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                           intent_id=f"int-{i}", market_id="1.2")
            journal.append("report", {"action": "place", "transitions": [
                {"customer_order_ref": ref, "state": "LAPSED", "fields": {
                    "bet_id": str(200000000000 + i), "size_remaining_p": 0, "size_lapsed_p": 200}}]},
                           market_id="1.2")
        crashed = []
        for stamp in ("191231000000", "200101120000"):           # an old and a recent crash-left order
            ref, cref = f"bro{stamp}-{'f' * 16}", f"brq{stamp}-{'f' * 16}"
            journal.append("intent", orders.intent_payload(gw.instruction(), source="cli_manual"),
                           intent_id=f"int-{stamp}", customer_order_ref=ref, customer_ref=cref, market_id="1.1",
                           state_after="RECORDED")
            journal.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                      "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                           intent_id=f"int-{stamp}", market_id="1.1")
            crashed.append(ref)
        anchor_write("default", dict(journal.anchor(), updated_at=self.env.clock.utc_now()), runner=self.env.runner)
        folds = []
        real_fold = journal.fold

        def counted_fold(**kwargs):
            folds.append(1)
            return real_fold(**kwargs)

        with patch.object(journal, "fold", side_effect=counted_fold), \
                patch.object(gateway_module, "OPEN_RECONCILE_BUDGET_S", 0):
            self.open()
        self.assertLess(len(folds), 20)                    # was one fold per journal order (> 40)
        reconciled = {e["payload"]["transitions"][0]["customer_order_ref"] for e in journal.events()
                      if e["kind"] == "reconcile" and e["payload"].get("transitions")
                      and e["payload"].get("reason") != "crash_between_submit_and_report"}
        self.assertEqual(reconciled, {crashed[1]})         # newest first: the recent order is polled

    def test_book_older_than_a_sleep_is_never_traded(self):
        """Regression (audit): book age was measured only on the monotonic clock, which stops while a Mac
        sleeps, so an order went out on prices from before a sleep. Funds are read before the book now
        (amendment A3, C2), so funds_max_age_s is raised here to keep the funds fresh across the sleep."""
        env = gw.build_live_gateway(self, overrides={"orders": {"caps": {"funds_max_age_s": 300}}})
        gw.open_live(self, env, "place", instructions=[gw.instruction(), gw.instruction()])
        real_book_row = env.exchange.book_row
        sleeps = {"left": 1}
        fetches = []

        def lid_closed_during_fetch(market_id):
            row = real_book_row(market_id)
            fetches.append(market_id)
            if sleeps["left"]:
                sleeps["left"] -= 1
                env.clock.step_wall(200)          # the wall clock moves on; the monotonic clock does not
            return row

        env.exchange.book_row = lid_closed_during_fetch
        result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual(len(fetches), 2)               # the pre-sleep book was discarded and fetched again
        self.assertEqual(result["decision"], "accepted")
        # Every fetch straddles a sleep: nothing is sent.
        sleeps["left"] = 10
        sent = len(env.router.order_requests)
        refused = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual(refused["decision"], "refused")
        reasons = {f["reason"] for f in refused["refusal"]["detail"]["failures"]}
        self.assertTrue(reasons & {"book_stale_wall_clock", "market_data_delayed_or_stale"}, reasons)
        self.assertEqual(len(env.router.order_requests), sent)

    def test_sleep_after_the_decision_refuses_the_send(self):
        """The capability's TTL is monotonic, so a sleep between the decision and the send is caught at the
        first byte (amendment A3, C2): the transport's gate finds the book stale on the wall clock, writes
        nothing, and the order is FAILED (transport_not_sent) with the gate's code journaled."""
        env = self.env
        self.open(instructions=[gw.instruction()])
        real_prepare = env.rpc.prepare_order

        def prepare_then_sleep(method, params):
            prepared = real_prepare(method, params)
            env.clock.step_wall(600)
            return prepared

        with patch.object(env.rpc, "prepare_order", side_effect=prepare_then_sleep):
            result = self.place()
        self.assert_not_sent_at_first_byte(result, "book_stale_before_send")

    def assert_not_sent_at_first_byte(self, result, code, env=None):
        env = env or self.env
        self.assertEqual(result["decision"], "accepted")          # the risk decision (spec meaning)
        self.assertEqual([(o["state"], o["error_code"]) for o in result["orders"]], [("FAILED", "transport_not_sent")])
        self.assertEqual(env.router.order_requests, [])
        self.assertEqual((env.transport.refused[-1]["code"], env.transport.bytes_written[-1]), (code, 0))
        report = [e for e in env.journal.events() if e["kind"] == "report"][-1]
        self.assertEqual((report["payload"]["exception"]["code"], report["payload"]["exception"]["phase"],
                          report["payload"]["exception"]["may_have_arrived"], report["payload"]["outcome"]),
                         (code, "send", False, "FAILED_NOT_SENT"))

    def test_both_clocks_advanced_after_submit_refuses_at_first_byte(self):
        """Astra C2 case (a), with Astra's own hook: both clocks move 3 s right after _append_submit while
        max_book_age_s is 2 s. Before the fix the place was accepted and the exchange got the order; now the
        send-by (book time + max_book_age_s) has passed at the first byte: nothing is written and the order is
        FAILED (transport_not_sent, definitely unsent)."""
        env = self.env
        self.assertEqual(env.config["orders"]["caps"]["max_book_age_s"], 2.0)
        self.open(instructions=[gw.instruction()])
        original = env.gateway._append_submit

        def slow(*args, **kwargs):
            result = original(*args, **kwargs)
            env.clock.advance(3)
            return result

        with patch.object(env.gateway, "_append_submit", side_effect=slow):
            result = self.place()
        self.assert_not_sent_at_first_byte(result, "send_window_expired")
        self.assertEqual(env.exchange.orders, {})

    def test_send_window_expires_during_a_slow_connect(self):
        """Astra C2 case (b): the capability is consumed before the connection is made; a 6 s connect inside
        the 10 s HTTP budget used to send the order after its book and capability had expired. The transport
        now checks min(book bound, capability expiry) right before the first byte."""
        env = self.env
        self.open(instructions=[gw.instruction()])
        real_send = env.rpc.send_order

        def slow_connect(capability, prepared, **bounds):
            env.transport.connect_delay_s = 6
            return real_send(capability, prepared, **bounds)

        with patch.object(env.rpc, "send_order", side_effect=slow_connect):
            result = self.place()
        self.assert_not_sent_at_first_byte(result, "send_window_expired")

    def test_kill_file_checked_at_the_first_byte(self):
        """Amendment A3, D4: a KILL file engaged after the post-submit check (step 10) but before the first
        request byte stops the place there: kill_engaged_before_send, nothing written, FAILED."""
        env = self.env
        self.open(instructions=[gw.instruction()])
        real_send = env.rpc.send_order

        def kill_then_send(capability, prepared, **bounds):
            engage(env.paths, "kill at the first byte", clock=env.clock)
            return real_send(capability, prepared, **bounds)

        with patch.object(env.rpc, "send_order", side_effect=kill_then_send):
            result = self.place()
        self.assert_not_sent_at_first_byte(result, "kill_engaged_before_send")

    def test_place_reads_funds_before_the_book(self):
        """Amendment A3, C2: getAccountFunds comes before the gateway's own book, so the book's age window,
        which bounds the send, covers only the decision, the journal appends and the connect."""
        env = self.env
        self.open(instructions=[gw.instruction()])
        env.router.rpc_methods.clear()
        self.assertEqual(self.place()["decision"], "accepted")
        methods = env.router.rpc_methods
        self.assertLess(methods.index("getAccountFunds"), methods.index("listMarketBook"))
        self.assertEqual(methods[-1], gw.PLACE_SUFFIX)
        request = [r for r in env.transport.requests if r["order_digest"]][-1]
        self.assertTrue(request["send_gate"])
        self.assertIsInstance(request["send_by_monotonic_ns"], int)

    def test_cancel_capability_minted_after_the_submit_append(self):
        """Amendment A3, C2: a cancel's capability is minted inside _send, after the submit append, so a slow
        append (a whole-journal fold behind another writer) never expires it; a refusal from the mint is
        journaled FAILED_NOT_SENT and the bet stays EXECUTABLE (cancel_not_processed)."""
        env = self.env
        self.open(instructions=[gw.instruction(), gw.instruction()])
        first = self.place()["orders"][0]["bet_id"]
        second = self.place()["orders"][0]["bet_id"]
        self.gateway.close()
        self.open("cancel")
        original = env.journal.append

        def slow_submit(kind, payload, **columns):
            event = original(kind, payload, **columns)
            if kind == "submit":
                env.clock.advance(8)              # longer than the 5 s capability TTL
            return event

        with patch.object(env.journal, "append", side_effect=slow_submit):
            cancelled = self.gateway.cancel("1.1", [first], source="cli_manual")
        self.assertEqual(cancelled["orders"][0]["state"], "EXECUTION_COMPLETE")
        self.assertEqual(env.exchange.orders[first]["status"], "EXECUTION_COMPLETE")
        sent = len(env.router.order_requests)
        with patch.object(env.guard, "issue", side_effect=OrderRefused("capability_decision_unknown")):
            refused = self.gateway.cancel("1.1", [second], source="cli_manual")
        self.assertEqual(len(env.router.order_requests), sent)
        self.assertEqual((refused["orders"][0]["state"], refused["orders"][0]["error_code"]),
                         ("EXECUTABLE", "cancel_not_processed"))
        report = [e for e in env.journal.events() if e["kind"] == "report"][-1]
        self.assertEqual((report["payload"]["outcome"], report["payload"]["exception"]["code"]),
                         ("FAILED_NOT_SENT", "capability_decision_unknown"))
        self.assertEqual(env.exchange.orders[second]["status"], "EXECUTABLE")

    def test_live_exposure_gives_no_hedge_credit(self):
        """Astra C1, end to end with Astra's harness: a fully matched BACK 2.00 @ 3.50; Betfair then lowers its
        average matched price to 2.00 (a non-runner reduction) and bumps the market version; the gateway reopens
        and still arms (the journal keeps 3.50). Before the fix a LAY 2.00 @ 3.50 was accepted: the stale 3.50 payoff
        was credited (journal 200p against a 200p cap; 300p at the real price). Now live gives a matched portion no
        hedge credit, so the LAY is refused at R8 (500p > 200p) whatever the average price, and nothing is sent."""
        env = gw.build_live_gateway(self, overrides={"orders": {"caps": {"max_market_exposure": 2.0,
                                                                         "max_total_exposure": 2.0}}})
        fully_matched_places(env)
        back = gw.instruction()
        self.assertTrue(gw.open_live(self, env, instructions=[back])["armed"])
        first = env.gateway.place("1.1", [back], source="cli_manual", confirm_token=env.token)
        self.assertEqual((first["decision"], first["orders"][0]["state"]), ("accepted", "EXECUTION_COMPLETE"))
        env.gateway.close()
        bet = first["orders"][0]["bet_id"]
        env.exchange.orders[bet]["averagePriceMatched"] = 2.0
        env.exchange.version += 1
        env.exchange.place_handler = None
        lay = gw.instruction(side="LAY")
        self.assertTrue(gw.open_live(self, env, instructions=[lay])["armed"])
        sent = len(env.router.order_requests)
        second = env.gateway.place("1.1", [lay], source="cli_manual", confirm_token=env.token)
        self.assertEqual((second["decision"], second["refusal"]["rule"]), ("refused", "market_exposure"))
        decision = [e for e in env.journal.events() if e["kind"] == "risk_decision"][-1]["payload"]
        self.assertEqual((decision["worst_case"]["market_pence"], decision["worst_case"]["total_pence"]), (500, 500))
        self.assertEqual(len(env.router.order_requests), sent)
        self.assertEqual([o["state"] for o in second["orders"]], ["REFUSED"])
        # The journal still says 3.50; neither that price nor the exchange's 2.00 changes the no-credit figure.
        state = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertEqual(state["orders"][first["orders"][0]["customer_order_ref"]]["average_price_matched_h"], 350)
        pseudo_lay = dict(state["orders"][first["orders"][0]["customer_order_ref"]], customer_order_ref="lay",
                          side="LAY", state="SUBMITTING", size_matched_p=0, average_price_matched_h=None,
                          size_remaining_p=200, bet_id=None)
        for average, credited in ((350, 200), (200, 300)):     # Astra: stale 200p, real 300p, cap 200p
            orders_now = [dict(o, average_price_matched_h=average) for o in state["orders"].values()] + [pseudo_lay]
            self.assertEqual(exposure_by_market(orders_now, hedge_credit=True)["1.1"], credited)
            self.assertEqual(exposure_by_market(orders_now)["1.1"], 500)
        status = env.gateway.status()
        self.assertEqual((status["exposure_basis"], status["exposure_pence_total"]),
                         ("worst_case_no_hedge_credit", 200))

    def test_live_status_counts_matched_hedges_without_credit(self):
        """Amendment A3, C1: live status and the journal summary count exposure as live R8-R10 do. A matched
        BACK 2.00 @ 3.50 and a matched LAY 2.00 @ 3.10 would be fully hedged with credit (0p); live counts each
        leg's loss: 420p if the selection wins. The LAY itself was accepted (both legs' losses fit the caps)."""
        from betangel_research.betfair.cli import _journal_summary
        env = self.env
        fully_matched_places(env)
        lay = gw.instruction(side="LAY", price_h=310)
        self.open(instructions=[gw.instruction(), lay])
        self.assertEqual(self.place()["decision"], "accepted")
        self.assertEqual(self.place(side="LAY", price_h=310)["decision"], "accepted")
        orders_now = list(self.state()["orders"].values())
        self.assertEqual([o["state"] for o in orders_now], ["EXECUTION_COMPLETE", "EXECUTION_COMPLETE"])
        self.assertEqual(exposure_by_market(orders_now, hedge_credit=True), {"1.1": 0})
        status = self.gateway.status()
        self.assertEqual((status["exposure_basis"], status["exposure_pence_by_market"]),
                         ("worst_case_no_hedge_credit", {"1.1": 420}))
        summary = _journal_summary(env.paths.journal_path("live"), "live", env.clock)
        self.assertEqual((summary["exposure_basis"], summary["exposure_pence_total"]),
                         ("worst_case_no_hedge_credit", 420))

    def test_every_order_rpc_matches_one_capability_and_submit(self):
        self.open(instructions=[gw.instruction(), gw.instruction(price_h=360)])
        first = self.place()
        second = self.place(price_h=360)
        self.gateway.cancel("1.1", [first["orders"][0]["bet_id"], second["orders"][0]["bet_id"]],
                            source="cli_manual")
        requests = self.env.router.order_requests
        self.assertEqual(len(requests), 3)
        submits = [e["payload"]["body_sha256"] for e in self.env.journal.events() if e["kind"] == "submit"]
        for request in requests:
            digest = bytes_sha256(request["body"])
            self.assertEqual(request["digest"], digest)
            self.assertEqual(submits.count(digest), 1)
        self.assertEqual(len(submits), len(requests))
        # A body the transport sees without a matching digest is refused before any byte is sent.
        refused_before = len(self.env.transport.refused)
        prepared = self.env.rpc.prepare_order(gateway_module.PLACE_METHOD, {"marketId": "1.1"})
        with self.assertRaises(TransportError):
            self.env.transport.request("POST", "https://api.betfair.com/exchange/betting/json-rpc/v1", {},
                                       prepared["body"], deadline_s=5, idempotent=False, order_digest=None)
        self.assertEqual(len(self.env.transport.refused), refused_before + 1)
        self.assertEqual(len(self.env.router.order_requests), 3)

    def test_probe_sets_ref_roundtrip_marker(self):
        env = gw.build_live_gateway(self, roundtrip=False)
        report = gw.open_live(self, env, "live-probe", selection_id=gw.SELECTION)
        self.assertTrue(report["armed"], report)
        result = env.gateway.probe("1.1", gw.SELECTION, confirm_token=env.token)
        self.assertTrue(result["found_by_ref"])
        self.assertTrue(result["ref_roundtrip_verified"])
        names = [r["method"].split("/")[-1] for r in env.router.order_requests]
        self.assertEqual(names, [gw.PLACE_SUFFIX, gw.CANCEL_SUFFIX])
        limit = env.router.order_requests[0]["params"]["instructions"][0]["limitOrder"]
        self.assertEqual((env.router.order_requests[0]["params"]["instructions"][0]["side"], limit["price"],
                          limit["size"]), ("LAY", 1.01, 1.0))
        self.assertEqual([p["expected"] for p in env.confirmer.prompts], ["PROBE 1.1 12345 LAY 1.01 1.00"])
        self.assertTrue(env.journal.fold(now_ns=env.clock.time_ns())["ref_roundtrip_verified"])
        self.assertEqual(kinds(env.journal)[-2:], ["probe_result", "ref_roundtrip_verified"])
        files = sorted(p.name for p in env.paths.live_tests_dir.rglob("*.json"))
        self.assertEqual(files, ["cleared_cancelled.json", "cleared_lapsed.json", "current_orders.json"])
        # Place then cancel only: the replace step was removed with live replace in v1 (amendment A3, D2).
        self.assertNotIn("replace", result)
        probe_result = [e for e in env.journal.events() if e["kind"] == "probe_result"][-1]
        self.assertNotIn("replace_step", probe_result["payload"])
        with self.assertRaises(TypeError):
            env.gateway.probe("1.1", gw.SELECTION, confirm_token=env.token, replace_step=True)
        # Not found by ref: no marker.
        env3 = gw.build_live_gateway(self, roundtrip=False)
        gw.open_live(self, env3, "live-probe", selection_id=gw.SELECTION)
        original3 = env3.exchange.current
        env3.exchange.current = lambda **kw: [] if kw.get("customer_order_refs") else original3(**kw)
        missed = env3.gateway.probe("1.1", gw.SELECTION, confirm_token=env3.token)
        self.assertFalse(missed["ref_roundtrip_verified"])
        self.assertNotIn("ref_roundtrip_verified", kinds(env3.journal)[-1:])
        # A place (not the probe) still needs A19.
        self.assertFalse(env3.journal.fold(now_ns=env3.clock.time_ns())["ref_roundtrip_verified"])

    def test_env_none_reads_the_process_environment(self):
        env = self.env
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            gateway = OrderGateway(env.config, paths=env.paths, mode="live", journal=env.journal, guard=env.guard,
                                   rpc=env.rpc, read=env.read, confirmer=env.confirmer, clock=env.clock,
                                   keychain_runner=env.runner)
        with self.assertRaises(NotArmed) as caught:
            gateway.open(command="place", confirm_token=env.token)
        self.assertIn("egress_unattested_or_cloud", caught.exception.detail["failures"])

    def test_open_writes_the_anchor_before_anything_is_sent(self):
        self.open()
        head = self.env.journal.anchor()
        self.assertEqual((keychain_anchor(self.env)["seq"], keychain_anchor(self.env)["last_sha256"]),
                         (head["seq"], head["last_sha256"]))
        self.gateway.close()
        # Without a writable anchor no risk-adding command opens; a cancel still does (and says so).
        self.env.runner.add_fails = True
        with self.assertRaises(NotArmed) as caught:
            self.gateway.open(command="place", confirm_token=self.env.token)
        self.assertEqual(caught.exception.rule, "journal_anchor_write_failed")
        report = self.open("cancel")
        self.assertIn("journal_anchor_write_failed", report["notes"])
        self.assertIn("anchor_error", self.gateway.close())

    def test_anchor_changed_under_the_lock_refuses(self):
        env = self.env
        key = (ANCHOR_SERVICE, "default")
        reads = []

        def racing_runner(argv, **kwargs):
            if argv[1] == "find-generic-password" and ANCHOR_SERVICE in argv and "-w" in argv:
                reads.append(1)
                if len(reads) == 2:         # the read under the lock, after static arming passed
                    forged = json.loads(env.runner.items[key])
                    forged["last_sha256"] = "f" * 64
                    env.runner.items[key] = json.dumps(forged)
            return env.runner(argv, **kwargs)

        env.gateway.keychain_runner = racing_runner
        with self.assertRaises(NotArmed) as caught:
            env.gateway.open(command="place", confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "journal_anchor_mismatch")
        self.assertEqual(len(reads), 2)
        self.assertFalse(any(r["url"].endswith("/api/login") for r in env.transport.requests))


class AuditRegressionTests(unittest.TestCase):
    """Live gateway regressions from the 2026-10-10 audit (all offline, against SimExchange)."""

    def second_gateway(self, env):
        """Another process's gateway: same state root, its own journal connection, session and guard."""
        credentials = load_credentials(env.config, purpose="live_orders", profile=env.paths.profile, runner=env.runner)
        session = SessionManager(env.config, credentials, paths=env.paths, transport=env.transport, clock=env.clock)
        rpc = JsonRpcClient(env.config, session, clock=env.clock)
        read = ReadClient(rpc, paths=env.paths, clock=env.clock)
        journal = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        self.addCleanup(journal.close)
        guard = RiskGuard(env.config, mode="live", clock=env.clock, paths=env.paths)
        return OrderGateway(env.config, paths=env.paths, mode="live", journal=journal, guard=guard, rpc=rpc,
                            read=read, confirmer=gw.FakeConfirmer(), clock=env.clock, env={},
                            keychain_runner=env.runner)

    def test_open_moves_never_refuse_an_order_another_gateway_submitted(self):
        """Round 4: the open-time pass computed RECORDED -> REFUSED(crash_before_submit) from one fold and appended
        it with no head check, so a gateway whose .gateway.lock was replaced under it could journal its submit in
        between: the order going out became REFUSED (zero exposure) and its report's move was dropped with no
        HALT. The pass is now a compare-and-append on the head it folded at: refused, the order keeps its submit.
        And a live report whose moves the fold must skip halts globally in the same event."""
        env = gw.build_live_gateway(self)
        journal = env.journal
        ref, cref = "bro200101120000-0000000000000abd", "brq200101120000-0000000000000abd"
        journal.append("intent", orders.intent_payload(gw.instruction(), source="cli_manual"), intent_id="int-a",
                       customer_order_ref=ref, customer_ref=cref, market_id="1.1", state_after="RECORDED")
        other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        self.addCleanup(other.close)
        real_fold = journal.fold
        raced = []

        def fold_then_submit(**kwargs):
            state = real_fold(**kwargs)
            if not raced:                    # gateway A journals its submit right after this open's fold
                raced.append(1)
                other.append("submit", {"action": "place", "instruction_count": 1, "body_sha256": "a" * 64,
                                        "transitions": [{"customer_order_ref": ref, "state": "SUBMITTING"}]},
                             intent_id="int-a", market_id="1.1")
            return state

        with patch.object(journal, "fold", side_effect=fold_then_submit):
            moved = env.gateway._stale_to_unknown()
        self.assertEqual((raced, moved), ([1], ([], [])))
        state = journal.fold(now_ns=env.clock.time_ns(), submitting_as_unknown=False)
        self.assertEqual(state["orders"][ref]["state"], "SUBMITTING")
        self.assertFalse(any(e["kind"] == "reconcile" for e in journal.events()))
        # A's report then moves it as reported (Betfair took the bet).
        env.gateway._append_moves("report", {"action": "place", "outcome": "reported"},
                                  {ref: ("EXECUTABLE", {"bet_id": "300000000099"})}, reason="place_report",
                                  market_id="1.1")
        state = journal.fold(now_ns=env.clock.time_ns())
        self.assertEqual((state["orders"][ref]["state"], state["halted"]), ("EXECUTABLE", False))
        self.assertEqual(exposure_by_market(list(state["orders"].values())), {"1.1": 200})
        # A live report whose move the fold cannot apply is journaled with a global HALT, never dropped silently.
        env.gateway._append_moves("report", {"action": "place", "outcome": "reported"},
                                  {ref: ("SUBMITTING", {})}, reason="place_report", market_id="1.1")
        last = list(journal.events())[-1]
        self.assertEqual((last["kind"], last["payload"]["skipped_moves"], last["payload"]["halts"]),
                         ("report", [ref], ["report_moves_skipped"]))
        self.assertEqual(journal.fold(now_ns=env.clock.time_ns())["halt_reasons"], ["report_moves_skipped"])

    def test_a_place_whose_order_another_gateway_refused_first_ends_refused(self):
        """Round 4: when another gateway (its lock file replaced) refuses this request's RECORDED order before the
        submit, the submit is refused (journal_changed_since_decision) and the refusing risk_decision used to
        fail on REFUSED -> REFUSED (an exception instead of a result). It now leaves that ref as it is: refused,
        nothing sent, the order REFUSED once."""
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        other = self.second_gateway(env)
        real_prepare = env.rpc.prepare_order

        def prepare_then_other_open(method, params):
            prepared = real_prepare(method, params)
            if method.endswith("placeOrders"):
                self.assertEqual(other._stale_to_unknown()[1], [params["instructions"][0]["customerOrderRef"]])
            return prepared

        with patch.object(env.rpc, "prepare_order", side_effect=prepare_then_other_open):
            result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual((result["decision"], result["refusal"]["rule"]), ("refused", "journal_changed_since_decision"))
        self.assertEqual((result["orders"][0]["state"], result["orders"][0]["error_code"]),
                         ("REFUSED", "crash_before_submit"))
        self.assertEqual(env.router.order_requests, [])

    def test_unlinked_gateway_lock_cannot_admit_two_orders(self):
        """Regression (audit): .gateway.lock was deleted while gateway A held it; B created a new file and
        locked it too, and both orders passed the 3.00 market cap (4.00 sent). Now A's lock no longer
        names the path, nothing but A's own events may land between its decision and its submit, and A
        refuses (with a HALT) while B's order alone is sent."""
        caps = {"orders": {"caps": {"max_stake_per_order": 2.0, "max_market_exposure": 3.0,
                                    "max_total_exposure": 3.0, "max_daily_loss": 3.0}}}
        env = gw.build_live_gateway(self, overrides=caps)
        ins = gw.instruction()
        other = self.second_gateway(env)
        gw.open_live(self, env, instructions=[ins])
        os.unlink(env.paths.gateway_lock)                              # a "stale lock" clean-up
        other.preconfirm("place", market_id="1.1", instructions=[ins])
        other.open(command="place", confirm_token=env.token)
        self.addCleanup(other.close)
        results = {}
        original = env.guard.evaluate_place

        def evaluate_then_other_places(*args, **kwargs):
            decision = original(*args, **kwargs)
            results["b"] = other.place("1.1", [ins], source="cli_manual", confirm_token=env.token)
            return decision

        env.guard.evaluate_place = evaluate_then_other_places
        results["a"] = env.gateway.place("1.1", [ins], source="cli_manual", confirm_token=env.token)
        self.assertEqual(results["b"]["decision"], "accepted")
        self.assertEqual((results["a"]["decision"], results["a"]["refusal"]["rule"]), ("refused", "gateway_lock_lost"))
        self.assertEqual(len(env.router.order_requests), 1)
        state = env.journal.fold(now_ns=env.clock.time_ns())
        self.assertLessEqual(exposure_by_market(list(state["orders"].values()))["1.1"], 300)
        self.assertIn("gateway_lock_lost", state["halt_reasons"])
        self.assertEqual({o["state"] for o in state["orders"].values()}, {"EXECUTABLE", "REFUSED"})

    def test_events_from_another_writer_void_the_decision(self):
        """A halt (or any event) another process journals between the risk decision and the submit is not
        in that decision: the submit is a compare-and-append on the head the decision was taken at."""
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, instructions=[gw.instruction()])
        original = env.guard.evaluate_place

        def evaluate_then_other_writer(*args, **kwargs):
            decision = original(*args, **kwargs)
            other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
            try:
                other.append("note", {"reason": "another_process"})
            finally:
                other.close()
            return decision

        env.guard.evaluate_place = evaluate_then_other_writer
        result = env.gateway.place("1.1", [gw.instruction()], source="cli_manual", confirm_token=env.token)
        self.assertEqual((result["decision"], result["refusal"]["rule"]), ("refused", "journal_changed_since_decision"))
        self.assertEqual(env.router.order_requests, [])
        self.assertEqual([o["state"] for o in result["orders"]], ["REFUSED"])
        events = list(env.journal.events())
        self.assertEqual([e["kind"] for e in events][-4:], ["note", "intent", "risk_decision", "risk_decision"])
        self.assertEqual((events[-1]["payload"]["approved"], events[-1]["payload"]["rule"]),
                         (False, "journal_changed_since_decision"))
        self.assertNotIn("submit", [e["kind"] for e in events])

    def test_kill_sweeps_when_the_journal_cannot_be_folded(self):
        """Regression (audit): a chain-valid journal the fold refuses (written by older code, or tampered)
        made open(command="kill") raise before any sweep, so no cancel was sent although the strategy-ref
        and sourceIdKey legs need no fold."""
        env = gw.build_live_gateway(self)
        bet = env.exchange.add_order(ref="bro200101120000-00000000000000aa", strategy_ref="bar-manual")
        db = sqlite3.connect(str(env.paths.journal_path("live")), isolation_level=None)
        try:
            head = db.execute("SELECT seq, event_sha256 FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            payload = {"transitions": [{"customer_order_ref": "bro200101120000-00000000000000ff", "state": "UNKNOWN"}]}
            event = {"seq": head[0] + 1, "event_id": "ev-" + "0" * 32, "recorded_at": env.clock.utc_now(),
                     "monotonic_ns": env.clock.mono_ns, "kind": "note", "mode": "live", "intent_id": None,
                     "customer_order_ref": None, "customer_ref": None, "bet_id": None, "market_id": None,
                     "state_after": None, "payload": payload, "prev_sha256": head[1]}
            event["event_sha256"] = journal_module._event_sha256(event)
            db.execute(journal_module._INSERT_SQL, (event["seq"], event["event_id"], event["recorded_at"],
                                                    event["monotonic_ns"], "note", "live", None, None, None, None,
                                                    None, None, canonical_json(payload).decode(), head[1],
                                                    event["event_sha256"]))
        finally:
            db.close()
        self.assertEqual(env.journal.verify()["status"], "ok")
        anchor_write(env.paths.profile, dict(env.journal.anchor(), updated_at=env.clock.utc_now()), runner=env.runner)
        report = env.gateway.open(command="kill")
        self.addCleanup(env.gateway.close)
        self.assertEqual(report["notes"], ["journal_unfoldable", gateway_module.UNTRUSTED_SWEEP_NOTE])
        out = env.gateway.kill(scope="own", reason="test")
        # Swept on the config strategy ref, but an untrusted journal cannot list all our bets (amendment A3, D4).
        self.assertEqual(out["status"], "kill_incomplete")
        self.assertIn(gateway_module.UNTRUSTED_SWEEP_NOTE, out["notes"])
        self.assertEqual(env.exchange.orders[bet]["status"], "EXECUTION_COMPLETE")
        self.assertEqual(len(env.router.order_requests), 1)
        events = list(env.journal.events())[2:]
        self.assertEqual([e["kind"] for e in events][:3], ["kill", "halt", "note"])
        self.assertEqual(events[2]["payload"], {"reason": gateway_module.UNTRUSTED_SWEEP_NOTE})
        self.assertIn("submit", [e["kind"] for e in events])
        self.assertEqual(events[-1]["payload"], {"reason": "kill_incomplete", "scope": "global"})
        self.assertEqual(env.journal.verify()["status"], "ok")
        self.assertFalse(env.gateway.close()["anchor_written"])          # an untrusted journal is never blessed

    def test_registry_write_refusal_is_a_named_failure(self):
        """Regression (audit): a SecretLeak from the account registry escaped open(); it is now a named arming
        failure. The kill no longer writes the registry at all (amendment A3, D4), so it sweeps regardless."""
        env = gw.build_live_gateway(self)
        bet = env.exchange.add_order(strategy_ref="bar-manual")
        leak = SecretLeak("secret_value_in_output:accounts.json")
        with patch.object(gateway_module, "register_account", side_effect=leak):
            report = env.gateway.open(command="cancel")
            self.assertIn("account_registry_write_refused", report["failures"])
            self.assertFalse(report["armed"])
            env.gateway.close()
            env.gateway.open(command="kill")
            self.addCleanup(env.gateway.close)
            self.assertEqual(env.gateway.kill(scope="own", reason="test")["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[bet]["status"], "EXECUTION_COMPLETE")

    def test_profile_named_after_the_username_works(self):
        """Regression (audit): the username was a substring-scanned secret, so a profile (or home path)
        containing it made accounts.json and the recorder ledger raise SecretLeak; live open failed."""
        profile = "audituser7"
        items = support.default_keychain_items(profile)
        items[(SERVICES["username"], profile)] = profile
        env = gw.build_live_gateway(self, profile=profile, keychain_items=items)
        bet = env.exchange.add_order(strategy_ref="bar-manual")
        report = env.gateway.open(command="cancel")
        self.assertNotIn("account_registry_write_refused", report["failures"])
        self.assertIn(profile, env.paths.accounts_file.read_text())
        env.gateway.close()
        env.gateway.open(command="kill")
        self.addCleanup(env.gateway.close)
        self.assertEqual(env.gateway.kill(scope="own", reason="test")["status"], "kill_complete")
        self.assertEqual(env.exchange.orders[bet]["status"], "EXECUTION_COMPLETE")
        ledger = env.paths.recordings_dir / "run-ledger.jsonl"
        append_jsonl(ledger, {"receipt_path": str(env.paths.recordings_dir / "rec-x" / "receipt.json")})
        self.assertIn(profile, ledger.read_text())

    def test_accept_refused_when_startup_found_a_foreign_bet_in_the_market(self):
        """Regression (audit): reconcile --accept zeroed an order even when the same open's startup
        reconcile had found a foreign bet (or failed a read) in that market."""
        env = gw.build_live_gateway(self)
        env.exchange.place_handler = gw.raises(TransportError("connection_lost", phase="receive"))
        gw.open_live(self, env, "place", instructions=[gw.instruction()])
        ref = env.gateway.place("1.1", [gw.instruction()], source="cli_manual",
                                confirm_token=env.token)["orders"][0]["customer_order_ref"]
        env.gateway.close()
        self.assertEqual(env.journal.fold(now_ns=env.clock.time_ns())["orders"][ref]["state"], "ABSENT_UNCONFIRMED")
        env.exchange.place_handler = None
        foreign = env.exchange.add_order(price=4.0, size=3.0)        # with our key, no ref: not this order's terms
        gw.open_live(self, env, "reconcile-accept", accept_ref=ref)
        self.assertEqual([f["bet_id"] for f in env.gateway._startup["foreign"]], [foreign])
        with self.assertRaises(OrderRefused) as caught:
            env.gateway.reconcile(accept_ref=ref, confirm_token=env.token)
        self.assertEqual(caught.exception.rule, "accept_startup_not_clean")
        self.assertEqual(env.journal.fold(now_ns=env.clock.time_ns())["orders"][ref]["state"], "ABSENT_UNCONFIRMED")


    def test_anchor_never_moves_backwards_under_concurrent_publishers(self):
        """Regression (re-review R1): a publisher that read the journal head, paused, then wrote the Keychain after
        another writer had appended and published a newer head moved the anchor backwards, so a superseded journal
        snapshot passed A13. A holder's close() (A) is paused between its head read and its Keychain write; writer
        B (another process: its own connection) journals a HALT and publishes. Every publication now holds
        paths.anchor_lock and reads the head inside it: B waits for A, publishes after it, the anchor only grows,
        and the snapshot without B's HALT fails the anchor check."""
        env = gw.build_live_gateway(self)
        gw.open_live(self, env, "cancel")
        head_a = env.journal.anchor()
        snapshot = env.paths.orders_dir / "snapshot-before-race.sqlite"
        db = sqlite3.connect(str(snapshot))
        try:
            env.journal._db.backup(db)                  # the journal as A reads it, without B's HALT
        finally:
            db.close()
        written = []                                    # the seq of every Keychain anchor write, in order
        b_waiting, a_done, b_done = threading.Event(), threading.Event(), threading.Event()
        b_result = {}

        class GatedClock(support.FakeClock):
            """B's clock: a poll of the busy anchor lock waits (for real) until A's publication has returned."""

            def sleep(self, seconds):
                b_waiting.set()
                a_done.wait(30)
                super().sleep(seconds)

        def runner(argv, **kwargs):
            if argv[1] == "add-generic-password" and ANCHOR_SERVICE in argv and not writer.ident:
                writer.start()                          # A has read its head; its Keychain write waits for B
                deadline = time.monotonic() + 30
                while not (b_waiting.is_set() or b_done.is_set()) and time.monotonic() < deadline:
                    time.sleep(0.001)
            answer = env.runner(argv, **kwargs)
            if argv[1] == "add-generic-password" and ANCHOR_SERVICE in argv:
                written.append(json.loads(env.runner.items[(ANCHOR_SERVICE, "default")])["seq"])
            return answer

        def writer_b():
            journal = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
            try:
                journal.append("halt", {"reason": "newer_safety_history", "scope": "global"})
                b_result["published"] = publish_anchor(journal, env.paths, runner=runner, clock=GatedClock())
            except BaseException as exc:                # reported to the main thread's assertions
                b_result["error"] = repr(exc)
            finally:
                journal.close()
                b_done.set()

        writer = threading.Thread(target=writer_b, name="writer-b")
        env.gateway.keychain_runner = runner
        try:
            closed = env.gateway.close()
        finally:
            a_done.set()
            if writer.ident:
                writer.join(60)
        self.assertNotIn("error", b_result)
        head_b = env.journal.anchor()
        self.assertEqual(head_b["seq"], head_a["seq"] + 1)
        self.assertEqual(written, [head_a["seq"], head_b["seq"]])          # A first, then B: never backwards
        self.assertTrue(closed["anchor_written"])
        self.assertTrue(b_waiting.is_set())             # B was held off while A was mid-publication
        keychain = keychain_anchor(env)
        self.assertEqual((keychain["seq"], keychain["last_sha256"]), (head_b["seq"], head_b["last_sha256"]))
        now_ns = env.clock.time_ns()
        current = env.journal.fold(now_ns=now_ns)
        self.assertTrue(current["halted"])
        self.assertEqual(arming.anchor_failures(head_b, keychain, journal=env.journal, journal_state=current), [])
        with OrderJournal(snapshot, mode="live", clock=env.clock) as old:
            old_state = old.fold(now_ns=now_ns)
            self.assertFalse(old_state["halted"])                           # the superseded snapshot lacks it
            self.assertEqual(arming.anchor_failures(old.anchor(), keychain, journal=old, journal_state=old_state),
                             ["journal_anchor_mismatch"])

    def test_a_journal_rolled_back_under_the_session_is_never_anchored(self):
        """Regression (re-review of R1): a journal restored in place to an older copy while a live session has it
        open (sqlite3 ".restore", which every open connection then reads) was published by that session, both by
        close() and by open() (restored after its A13 check), so the Keychain anchor moved backwards and the
        rolled-back journal, its HALT gone, passed A13 at the next open. The publication now writes only a head
        that extends the Keychain anchor: the anchor stays ahead and the next open refuses the rolled-back journal
        (journal_anchor_mismatch). The cancel itself still opens (a note) and close() reports the code."""

        def copy(source, target):
            src, dst = sqlite3.connect(str(source)), sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()

        def refuses_next_open(env):
            with self.assertRaises(NotArmed) as caught:
                env.gateway.open(command="cancel", confirm_token=env.token)
            self.assertIn("journal_anchor_mismatch", caught.exception.detail["failures"])

        # close(): restored while the cancel holder is open.
        env = gw.build_live_gateway(self)
        backup = env.paths.orders_dir / "journal-before-session.sqlite"
        copy(env.paths.journal_path("live"), backup)                    # seq 1
        gw.open_live(self, env, "cancel")
        env.journal.append("halt", {"reason": "session_safety_history", "scope": "global"})
        before = keychain_anchor(env)
        self.assertGreater(before["seq"], 1)
        copy(backup, env.paths.journal_path("live"))
        self.assertEqual(env.journal.anchor()["seq"], 1)                # the session reads the rolled-back journal
        closed = env.gateway.close()
        self.assertEqual((closed["anchor_written"], closed.get("anchor_error")),
                         (False, "journal_anchor_not_extended"))
        self.assertEqual(keychain_anchor(env), before)                  # never moved backwards
        refuses_next_open(env)

        # open(): restored during the startup reconcile, after the A13 check under the lock.
        env = gw.build_live_gateway(self)
        backup = env.paths.orders_dir / "journal-before-session.sqlite"
        copy(env.paths.journal_path("live"), backup)                    # seq 1
        env.journal.append("halt", {"reason": "earlier_safety_history", "scope": "global"})
        anchor_write(env.paths.profile, dict(env.journal.anchor(), updated_at=env.clock.utc_now()), runner=env.runner)
        before = keychain_anchor(env)
        startup = env.gateway._startup_reconcile

        def restored_first():
            copy(backup, env.paths.journal_path("live"))
            return startup()

        with patch.object(env.gateway, "_startup_reconcile", restored_first):
            report = env.gateway.open(command="cancel", confirm_token=env.token)
        self.addCleanup(env.gateway.close)
        self.assertIn("journal_anchor_write_failed", report["notes"])
        self.assertEqual(keychain_anchor(env), before)
        self.assertEqual(env.gateway.close().get("anchor_error"), "journal_anchor_not_extended")
        self.assertEqual(keychain_anchor(env), before)
        refuses_next_open(env)

    def test_non_risk_commands_wait_briefly_for_another_anchor_publisher(self):
        """Regression (re-review of R1, liveness): every non-kill live command waited up to
        credentials.ANCHOR_PUBLISH_WAIT_S (30 s) for the anchor lock in open(), before its cancel was sent, and
        again in close(), holding .gateway.lock throughout. A publisher holding the anchor lock without
        .gateway.lock (the kill's close, resume; say one stuck in a Keychain dialog or suspended) delayed a
        manual cancel 30 s and held .gateway.lock ~60 s, so a second cancel (which waits order_deadline_s + 5 s)
        exited lock_busy with its bet live. cancel and reconcile, whose publication failure is only a note, and
        every close() now wait killswitch.JOURNAL_BUSY_TIMEOUT_S; only a RISK_COMMANDS open(), which refuses
        when it cannot publish, still waits the full 30 s."""
        from betangel_research.betfair.credentials import ANCHOR_PUBLISH_WAIT_S
        from betangel_research.betfair.killswitch import JOURNAL_BUSY_TIMEOUT_S
        env = gw.build_live_gateway(self)
        deadline_ns = (env.config["http"]["order_deadline_s"] + 5) * 10**9
        brief_ns = JOURNAL_BUSY_TIMEOUT_S * 10**9
        lock_held = []
        with file_lock(env.paths.anchor_lock) as held:                  # another publisher, mid-publication
            started = env.clock.mono_ns
            report = env.gateway.open(command="cancel", confirm_token=env.token)
            self.addCleanup(env.gateway.close)
            lock_held.append(env.gateway._lock.held())
            opened = env.clock.mono_ns
            closed = env.gateway.close()
            ended = env.clock.mono_ns
            self.assertTrue(held.held())
        self.assertEqual(lock_held, [True])                             # the wait is under .gateway.lock
        self.assertIn("journal_anchor_write_failed", report["notes"])
        self.assertEqual(closed.get("anchor_error"), "journal_anchor_publish_busy")
        self.assertEqual(opened - started, brief_ns)                    # the cancel is sent after this
        self.assertEqual(ended - opened, brief_ns)
        self.assertLess(ended - started, deadline_ns)                   # a second cancel gets .gateway.lock
        with file_lock(env.paths.anchor_lock):
            started = env.clock.mono_ns
            with self.assertRaises(NotArmed) as caught:
                env.gateway.open(command="reconcile-accept", confirm_token=env.token)
            self.assertEqual(caught.exception.rule, "journal_anchor_write_failed")
            self.assertEqual(env.clock.mono_ns - started, ANCHOR_PUBLISH_WAIT_S * 10**9)


class NormaliserTests(unittest.TestCase):
    def test_place_report_normalised_or_none(self):
        sent = [{"customer_order_ref": orders.REF, "side": "LAY"}]
        raw = {"status": "SUCCESS", "marketId": "1.1", "instructionReports": [
            {"status": "SUCCESS", "instruction": {"customerOrderRef": orders.REF}, "betId": "300000000001",
             "placedDate": "2020-01-01T12:00:00.000Z", "averagePriceMatched": 3.4567, "sizeMatched": 1.5,
             "orderStatus": "EXECUTABLE"}]}
        normalised = normalise_place_report(raw, market_id="1.1", sent=sent)
        item = normalised["instructions"][0]
        self.assertEqual((item["size_matched_p"], item["average_price_matched_h"]), (150, 346))
        for broken in ({"status": "WEIRD"}, {"status": "SUCCESS", "marketId": "1.2", "instructionReports": []},
                       dict(raw, instructionReports=[dict(raw["instructionReports"][0], betId=12)]),
                       dict(raw, instructionReports=[dict(raw["instructionReports"][0], sizeMatched=float("nan"))]),
                       dict(raw, instructionReports=[dict(raw["instructionReports"][0], status="PENDING")]),
                       "not a dict"):
            with self.subTest(broken=str(broken)[:40]):
                self.assertIsNone(normalise_place_report(broken, market_id="1.1", sent=sent))
        cancel = normalise_cancel_report({"status": "SUCCESS", "marketId": "1.1", "instructionReports": [
            {"status": "SUCCESS", "instruction": {"betId": "1", "sizeReduction": None}, "sizeCancelled": 2.0,
             "cancelledDate": "2020-01-01T12:00:00.000Z"}]}, market_id="1.1")
        self.assertEqual(cancel["instructions"][0]["size_cancelled_p"], 200)
        # Live replace was removed in v1: there is no replace report normaliser any more.
        self.assertFalse(hasattr(gateway_module, "normalise_replace_report"))


if __name__ == "__main__":
    unittest.main()
