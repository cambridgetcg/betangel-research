"""Shared synthetic builders for the order-core tests (no TestCases here).

Every value is invented: markets 1.x, selections 11-14, prices on the CLASSIC ladder, the
synthetic clock from test_betfair_support. MemoryJournal reuses journal.prepare_event and
JournalFolder, so it validates and folds exactly like OrderJournal (an event that does not fold
is refused before it is stored) without SQLite's F_FULLFSYNC cost. OrderHarness drives
RiskGuard, the journal and lifecycle through seeded random sequences the way the gateway will:
intent, risk decision, capability issue, submit (journaled first), send through a fake
transport that records the exact bytes, report. The oracle_* functions measure exposure and
realised loss independently of risk.py and of the fold, for the property tests.
"""

import copy
import os
import random
from types import SimpleNamespace

from betangel_research.betfair import journal as journal_module
from betangel_research.betfair import lifecycle
from betangel_research.betfair.errors import OrderRefused, TransportError
from betangel_research.betfair.journal import JournalFolder, fold_events
from betangel_research.betfair.pnl import market_realized, order_pnl_pence
from betangel_research.betfair.receipts import bytes_sha256, canonical_json, parse_utc_ns, utc_from_ns, utc_ms_from_ns
from betangel_research.betfair.risk import RiskGuard
from betangel_research.betfair.schemas import ORDER_RECORD_KEYS, UNRESOLVED_STATES
from betangel_research.betfair.ticks import LADDER_CLASSIC, tick_index, tick_offset, wire_price, wire_size
from betangel_research.betfair.validate import make_customer_order_ref, make_customer_ref

import test_betfair_support as support

PLACE_METHOD = "SportsAPING/v1.0/" + "placeOrders"
CANCEL_METHOD = "SportsAPING/v1.0/" + "cancelOrders"
REPLACE_METHOD = "SportsAPING/v1.0/" + "replaceOrders"
ARMED = {"armed": True, "failures": [], "checked": []}
# Exposure caps tight enough that R8-R10 bind often within a property run.
TIGHT_CAPS = {"orders": {"caps": {"max_market_exposure": 6.0, "max_total_exposure": 12.0, "max_daily_loss": 9.0,
                                  "max_liability_per_order": 5.0}}}
# Rate caps small enough that the hour, day and transaction windows bind within a run (property only;
# a run accepts roughly 5-15 orders over two or three simulated hours).
HOUR_CAPS = {"orders": {"caps": {"max_orders_per_hour": 4}}}
DAY_CAPS = {"orders": {"caps": {"max_orders_per_day": 6}}}
TRANSACTION_CAPS = {"orders": {"caps": {"max_transactions_per_hour": 6}}}
SELECTIONS = (11, 12, 13, 14)
BASE_PRICES_H = {11: 200, 12: 350, 13: 600, 14: 1000}
REF = "bro200101120000-0123456789abcdef"
CREF = "brq200101120000-0123456789abcdef"
NOW_NS = support.FakeClock().time_ns()


def property_scale():
    """Seeds and steps for property tests: defaults 40 x 120, scaled by environment variables."""
    seeds = int(os.environ.get("BETANGEL_BETFAIR_PROPERTY_SEEDS", "40"))
    steps = int(os.environ.get("BETANGEL_BETFAIR_PROPERTY_STEPS", "120"))
    if seeds < 1 or steps < 1:
        raise ValueError("property seeds and steps must be positive")
    return seeds, steps


def order_config(overrides=None):
    """The example config (synthetic profile) with deep-merged overrides; well-formed, not path-checked."""
    return support.make_test_config(SimpleNamespace(profile="default"), overrides)


def runner(*, back=((3.5, 120.0), (3.45, 40.0)), lay=((3.55, 80.0),), ltp=3.5, traded=None, status="ACTIVE"):
    return {"status": status, "handicap": 0.0, "ltp": ltp, "back": [list(level) for level in back],
            "lay": [list(level) for level in lay], "traded": None if traded is None else dict(traded),
            "total_matched": None}


def ladder_runner(selection, *, depth=50.0, traded=None):
    """A runner whose best back is BASE_PRICES_H[selection] and best lay one tick above."""
    base = BASE_PRICES_H[selection]
    back = [[tick_offset(base, -i) / 100, depth] for i in range(3)]
    lay = [[tick_offset(base, i + 1) / 100, depth] for i in range(3)]
    return runner(back=back, lay=lay, ltp=base / 100, traded=traded)


def snapshot(**changes):
    """A synthetic paper book snapshot (market 1.1, one runner 12345) that passes every V9 gate."""
    value = {
        "market_id": support.MARKET_ID, "source": "fixture", "fetched_by": "fixture", "key_type": "synthetic",
        "app_key_slot": None, "observed_at": support.BASE_UTC, "monotonic_ns": 5_000_000_000, "pt": support.BASE_PT,
        "status": "OPEN", "inplay": False, "version": 4249587130, "bet_delay": 0, "is_market_data_delayed": None,
        "catalogue_present": True, "market_time": "2020-01-01T12:20:00.000Z", "number_of_winners": 1,
        "betting_type": "ODDS", "market_type": "WIN", "event_type_id": "7", "price_ladder_type": "CLASSIC",
        "invalid_reason": None, "runners": {"12345": runner()}}
    value.update(copy.deepcopy(changes))
    return value


def live_book(clock, **changes):
    """A book as the live gateway's own fetch produces it: fresh, live key, not delayed."""
    now = clock.time_ns()
    value = snapshot(source="rest_listMarketBook", fetched_by="gateway_self_fetch", key_type="live",
                     app_key_slot="live", is_market_data_delayed=False, observed_at=utc_from_ns(now),
                     monotonic_ns=clock.mono_ns, pt=None, market_time=utc_ms_from_ns(now + 20 * 60 * 10**9))
    value.update(copy.deepcopy(changes))
    return value


def instruction(**changes):
    value = {"selection_id": 12345, "handicap": 0, "side": "BACK", "price_h": 350, "size_p": 200,
             "persistence_type": "LAPSE"}
    value.update(changes)
    return value


def order_record(**changes):
    """A valid order record (EXECUTABLE BACK 2.00 @ 3.50 by default); exposure_basis is recomputed."""
    value = {key: None for key in ORDER_RECORD_KEYS}
    value.update({
        "intent_id": "intent-1", "customer_order_ref": REF, "customer_ref": CREF, "customer_strategy_ref": "bar-manual",
        "mode": "paper", "market_id": support.MARKET_ID, "selection_id": 12345, "handicap": 0, "side": "BACK",
        "price_h": 350, "size_p": 200, "persistence_type": "LAPSE", "state": "EXECUTABLE", "bet_id": "312345678901",
        "size_matched_p": 0, "size_remaining_p": 200, "size_cancelled_p": 0, "size_lapsed_p": 0, "size_voided_p": 0,
        "successful_polls": 0, "provisional_child": None, "is_probe": False, "reconcile_attempts": 0,
        "exposure_basis": "none"})
    value.update(changes)
    if "size_remaining_p" not in changes:
        open_states = ("EXECUTABLE", "SUBMITTING", "UNKNOWN", "ABSENT_UNCONFIRMED", "CANCEL_PENDING", "REPLACE_PENDING")
        value["size_remaining_p"] = (value["size_p"] - value["size_matched_p"] - value["size_cancelled_p"]
                                     - value["size_lapsed_p"]) if value["state"] in open_states else 0
    value["exposure_basis"] = lifecycle.exposure_basis(value)
    return value


def empty_state(mode="paper", now_ns=NOW_NS):
    return fold_events([], mode=mode, now_ns=now_ns)


def state_with(orders, mode="paper", now_ns=NOW_NS, **changes):
    """A fold-shaped state holding the given order records (unresolved markets derived)."""
    state = empty_state(mode, now_ns)
    state["orders"] = {o["customer_order_ref"]: o for o in orders}
    state["unresolved_markets"] = sorted({o["market_id"] for o in orders if o["state"] in UNRESOLVED_STATES
                                          or (o["provisional_child"] is True and o["state"] != "RECORDED")})
    state.update(changes)
    return state


def intent_payload(order=None, **changes):
    value = {"order": order or instruction(), "customer_strategy_ref": "bar-manual", "is_probe": False,
             "parent_ref": None, "provisional_child": False, "source": "api"}
    value.update(changes)
    return value


def place_report(ref, *, status="SUCCESS", order_status="EXECUTABLE", matched=0, avg=None, bet_id="312345678901",
                 code=None, exec_status=None, exec_code=None, source="betfair", market_id=support.MARKET_ID,
                 placed_date="2020-01-01T12:00:00.000Z"):
    item = {"customer_order_ref": ref, "status": status, "error_code": code, "order_status": order_status,
            "bet_id": bet_id, "placed_date": placed_date, "average_price_matched_h": avg, "size_matched_p": matched}
    return {"status": exec_status or status, "error_code": exec_code, "market_id": market_id, "instructions": [item],
            "source": source}


def cancel_item(bet_id, *, status="SUCCESS", cancelled=None, reduction=None, code=None):
    return {"bet_id": bet_id, "size_reduction_p": reduction, "status": status, "error_code": code,
            "size_cancelled_p": cancelled,
            "cancelled_date": "2020-01-01T12:00:01.000Z" if status == "SUCCESS" else None}


def report_envelope(items, *, status="SUCCESS", code=None, source="betfair", market_id=support.MARKET_ID):
    return {"status": status, "error_code": code, "market_id": market_id, "instructions": items, "source": source}


class MemoryJournal:
    """OrderJournal's validation, chaining and fold without SQLite (events kept as canonical JSON)."""

    def __init__(self, mode, clock):
        self.mode = mode
        self.clock = clock
        self.stored = []
        self.folder = JournalFolder(mode)

    def append(self, kind, payload, *, intent_id=None, customer_order_ref=None, customer_ref=None, bet_id=None,
               market_id=None, state_after=None):
        columns, _, payload = journal_module.prepare_event(
            kind, payload, intent_id=intent_id, customer_order_ref=customer_order_ref, customer_ref=customer_ref,
            bet_id=bet_id, market_id=market_id, state_after=state_after)
        previous = self.stored[-1]["event_sha256"] if self.stored else journal_module.GENESIS_PREV
        event = {"seq": len(self.stored) + 1, "event_id": f"ev-mem-{len(self.stored) + 1}",
                 "recorded_at": self.clock.utc_now(), "monotonic_ns": self.clock.monotonic_ns(), "kind": kind,
                 "mode": self.mode, **columns, "payload": payload, "prev_sha256": previous}
        event["event_sha256"] = journal_module._event_sha256(event)
        candidate = self.folder.copy()
        try:
            candidate.apply(event)     # raises before anything is stored, like OrderJournal.append
        except journal_module.JournalError:
            raise
        except (TypeError, KeyError, ValueError, ArithmeticError):
            raise journal_module.JournalError("journal_event_unfoldable") from None
        self.folder = candidate
        self.stored.append(event)
        return copy.deepcopy(event)

    def event_count(self):
        return len(self.stored)

    def events(self, since_seq=0):
        for event in self.stored[since_seq:]:
            yield copy.deepcopy(event)

    def fold(self, *, now_ns, submitting_as_unknown=True):
        return self.folder.state(now_ns=now_ns, submitting_as_unknown=submitting_as_unknown)

    def close(self):
        pass


# --- random sequences ------------------------------------------------------------------------

OPS = (("place", 30), ("cancel", 7), ("replace", 8), ("fill", 8), ("partial_fill", 5), ("advance", 12),
       ("clock_step", 3), ("restart", 3), ("kill", 1), ("resume", 4), ("timeout", 5), ("transport_failure", 5),
       ("resolve", 7), ("settle", 4), ("forge", 2))
PLACE_OUTCOMES = ("executable", "executable", "matched", "partial", "failed", "bet_id_failure")


def random_sequence(seed, steps):
    """A seeded list of step dicts; choices that depend on state are seeded per step too."""
    rng = random.Random(seed)
    names = [name for name, _ in OPS]
    weights = [weight for _, weight in OPS]
    sequence = []
    for index in range(steps):
        op = rng.choices(names, weights)[0]
        step = {"op": op, "index": index, "seed": rng.randrange(2**32)}
        if op == "place":
            step["outcome"] = rng.choice(PLACE_OUTCOMES)
        elif op == "timeout":
            step.update(op="place", outcome="timeout")
        elif op == "transport_failure":
            step.update(op="place", outcome=rng.choice(("transport_unknown", "transport_not_sent")))
        elif op == "advance":
            step["seconds"] = rng.choice((0.5, 1, 5, 20, 61, 300, 3700))
        elif op == "clock_step":
            step["seconds"] = rng.choice((-120, -30, -6, 30, 4000))
        sequence.append(step)
    return sequence


# --- the harness -----------------------------------------------------------------------------


class OrderHarness:
    """Gateway-shaped driver of RiskGuard + journal + lifecycle over a synthetic exchange.

    The synthetic exchange is physically consistent: fills never exceed what is open, an unknown
    replace resolves to either the parent or the child (never both), so post-acceptance exposure
    can be compared with the caps.
    """

    def __init__(self, *, mode, seed, journal=None, journal_factory=None, config_overrides=None):
        self.mode = mode
        self.clock = support.FakeClock()
        self.config = order_config(config_overrides)
        self.guard = RiskGuard(self.config, mode=mode, clock=self.clock)
        self.journal_factory = journal_factory
        self.journal = journal if journal is not None else (journal_factory() if journal_factory
                                                           else MemoryJournal(mode, self.clock))
        self.rng = random.Random(seed)
        self.seed = seed
        self.kill = False
        self.markets = ["1.101", "1.102", "1.103"]
        self.next_market = 104
        self.counter = 0
        self.log = []                 # one entry per step
        self.capabilities = []        # every capability minted, in order
        self.sent = []                # (method, body bytes) as the fake transport received them
        self.violations = []
        self.restart_checks = 0

    # --- plumbing ----------------------------------------------------------------------------

    def now(self):
        return self.clock.time_ns()

    def state(self):
        return self.journal.fold(now_ns=self.now())

    def book(self, market_id):
        runners = {str(s): ladder_runner(s) for s in SELECTIONS}
        if self.mode == "live":
            return live_book(self.clock, market_id=market_id, runners=runners)
        return snapshot(market_id=market_id, runners=runners, observed_at=self.clock.utc_now(),
                        monotonic_ns=self.clock.mono_ns, pt=None)

    def funds(self):
        return {"availableToBetBalance": 1000.0, "exposure": 0.0, "retainedCommission": 0.0,
                "exposureLimit": -10000.0, "discountRate": 0.0, "pointsBalance": 0} if self.mode == "live" else None

    def funds_checked_at(self):
        """The funds above are fetched just now, as the gateway's own getAccountFunds would be."""
        return self.clock.utc_now() if self.mode == "live" else None

    def event_count(self):
        if hasattr(self.journal, "event_count"):
            return self.journal.event_count()
        return self.journal.anchor().get("seq", 0)

    def refs(self):
        self.counter += 1
        hexes = f"{self.seed % 65536:04x}{self.counter:012x}"
        return (make_customer_order_ref(self.now(), rng=lambda n: hexes),
                make_customer_ref(self.now(), rng=lambda n: hexes), f"intent-{self.seed}-{self.counter}")

    def bet_id(self):
        self.counter += 1
        return str(10**11 + self.seed * 10**5 + self.counter)

    def _issue(self, decision, method, params):
        """Live only: prepare the exact body and mint its capability; (capability, prepared) or (None, None)."""
        if self.mode != "live":
            return None, None
        body = canonical_json({"jsonrpc": "2.0", "method": method, "params": params, "id": self.counter})
        prepared = {"method": method, "body": body, "body_sha256": bytes_sha256(body), "rpc_id": self.counter}
        cap = self.guard.issue(decision, prepared, now_monotonic_ns=self.clock.monotonic_ns())
        self.capabilities.append(cap)
        return cap, prepared

    def _submit(self, action, intent_id, prepared, transitions, market_id):
        digest = prepared["body_sha256"] if prepared is not None else None
        self.journal.append("submit", {"action": action, "instruction_count": 1 if action != "cancel_all" else 0,
                                       "body_sha256": digest, "transitions": transitions},
                            intent_id=intent_id, market_id=market_id)

    def _send(self, cap, prepared):
        """The fake transport: it hashes the bytes it is handed and consumes the capability with that
        digest (as the transport's digest gate does), then records exactly what was sent."""
        if cap is None:
            return
        sent_sha = bytes_sha256(prepared["body"])
        cap.consume(method=prepared["method"], body_sha256=sent_sha, now_monotonic_ns=self.clock.monotonic_ns())
        self.sent.append((prepared["method"], bytes(prepared["body"])))

    def _open(self, state, market_id=None, states=("EXECUTABLE",)):
        return sorted((o for o in state["orders"].values()
                       if o["state"] in states and (market_id is None or o["market_id"] == market_id)
                       and o["provisional_child"] is not True and o["size_remaining_p"] > 0),
                      key=lambda o: o["customer_order_ref"])

    # --- steps -------------------------------------------------------------------------------

    def run(self, sequence, on_step=None):
        for step in sequence:
            self.rng = random.Random(step["seed"])
            before_kill = self.kill
            entry = {"op": step["op"], "index": step["index"], "accepted": False, "decision": None,
                     "kill_before": before_kill}
            getattr(self, "op_" + step["op"])(step, entry)
            entry["state"] = self.state()
            entry["now_ns"] = self.now()
            entry["events"] = self.event_count()
            self.log.append(entry)
            if on_step is not None:
                on_step(self, entry)
        return self.log

    def op_place(self, step, entry):
        market = self.rng.choice(self.markets)
        selection = self.rng.choice(SELECTIONS)
        side = "BACK" if (selection + self.rng.randrange(4)) % 3 else "LAY"
        base = BASE_PRICES_H[selection] if side == "BACK" else tick_offset(BASE_PRICES_H[selection], 1)
        offset = self.rng.choice((-3, -2, -1, 0, 0, 1, 2, 3, 15))
        index = min(max(tick_index(base) + offset, 0), len(LADDER_CLASSIC) - 1)
        ins = instruction(selection_id=selection, side=side, price_h=LADDER_CLASSIC[index],
                          size_p=self.rng.choice((100, 100, 150, 200, 200, 250, 500)))
        ref, cref, intent_id = self.refs()
        state = self.state()
        decision = self.guard.evaluate_place(market, [ins], book=self.book(market), state=state, arming=ARMED,
                                             funds=self.funds(), funds_checked_at=self.funds_checked_at(),
                                             now_ns=self.now(), now_monotonic_ns=self.clock.mono_ns,
                                             kill_engaged=self.kill, customer_ref=cref, customer_order_refs=[ref])
        entry.update(decision=decision, market_id=market)
        self.journal.append("intent", intent_payload(ins), intent_id=intent_id, customer_order_ref=ref,
                            customer_ref=cref, market_id=market, state_after="RECORDED")
        risk_payload = {"decision_id": decision["decision_id"], "approved": decision["approved"],
                        "rule": decision["rule"]}
        if not decision["approved"]:
            risk_payload["transitions"] = [{"customer_order_ref": ref, "state": "REFUSED",
                                            "reason": "risk_refused"}]
            self.journal.append("risk_decision", risk_payload, intent_id=intent_id)
            return
        self.journal.append("risk_decision", risk_payload, intent_id=intent_id)
        params = {"marketId": market, "customerRef": cref, "customerStrategyRef": "bar-manual",
                  "marketVersion": {"version": decision["market_version"]},
                  "instructions": [{"orderType": "LIMIT", "selectionId": selection, "handicap": 0, "side": side,
                                    "customerOrderRef": ref, "limitOrder": {"size": wire_size(ins["size_p"]),
                                                                            "price": wire_price(ins["price_h"]),
                                                                            "persistenceType": "LAPSE"}}]}
        cap, prepared = self._issue(decision, PLACE_METHOD, params)
        self._submit("place", intent_id, prepared, [{"customer_order_ref": ref, "state": "SUBMITTING"}], market)
        entry["accepted"] = True
        outcome = step["outcome"]
        bet_id = self.bet_id()
        better = tick_offset(ins["price_h"], 1 if side == "BACK" else -1) if self.rng.random() < 0.3 else ins["price_h"]
        if outcome != "transport_not_sent":
            self._send(cap, prepared)
        if outcome in ("transport_unknown", "transport_not_sent"):
            exc = TransportError("scripted", phase="send" if outcome == "transport_unknown" else "connect")
            verdict = lifecycle.outcome_from_exception(exc)
            state_after = "UNKNOWN" if verdict == "UNKNOWN" else "FAILED"
            moves = {ref: (state_after, {"error_code": "transport_" + verdict.lower()})}
        else:
            if outcome == "executable":
                report = place_report(ref, bet_id=bet_id, market_id=market)
            elif outcome == "matched":
                report = place_report(ref, order_status="EXECUTION_COMPLETE", matched=ins["size_p"], avg=better,
                                      bet_id=bet_id, market_id=market)
            elif outcome == "partial":
                report = place_report(ref, matched=ins["size_p"] // 2, avg=better, bet_id=bet_id, market_id=market)
            elif outcome == "failed":
                report = place_report(ref, status="FAILURE", order_status=None, bet_id=None, code="INSUFFICIENT_FUNDS",
                                      market_id=market)
            elif outcome == "bet_id_failure":
                report = place_report(ref, status="FAILURE", order_status=None, bet_id=bet_id,
                                      code="BET_TAKEN_OR_LAPSED", exec_code="BET_ACTION_ERROR", market_id=market)
            else:
                report = place_report(ref, status="TIMEOUT", order_status=None, bet_id=None, market_id=market)
            moves = lifecycle.classify_place_report(report, sent_refs=[ref])
        self.journal.append("report", {"action": "place", "outcome": outcome, "transitions": [
            {"customer_order_ref": r, "state": s, "reason": "place_report", "fields": f}
            for r, (s, f) in sorted(moves.items())]}, intent_id=intent_id, market_id=market)
        entry["outcome"] = outcome

    def op_cancel(self, step, entry):
        state = self.state()
        candidates = [o for o in self._open(state) if o["bet_id"] is not None]
        if not candidates:
            return
        order = self.rng.choice(candidates)
        ref, cref, intent_id = self.refs()
        decision = self.guard.evaluate_cancel(order["market_id"], [order["bet_id"]], state=state, now_ns=self.now(),
                                              customer_ref=cref)
        entry["decision"] = decision
        if not decision["approved"]:
            return
        params = {"marketId": order["market_id"], "customerRef": cref,
                  "instructions": [{"betId": order["bet_id"], "sizeReduction": None}]}
        cap, prepared = self._issue(decision, CANCEL_METHOD, params)
        self._submit("cancel", intent_id, prepared, [{"customer_order_ref": order["customer_order_ref"],
                                                      "state": "CANCEL_PENDING"}], order["market_id"])
        self._send(cap, prepared)
        kind = self.rng.choice(("ok", "ok", "failed", "timeout"))
        if kind == "ok":
            item = cancel_item(order["bet_id"], cancelled=order["size_remaining_p"])
            report = report_envelope([item], market_id=order["market_id"])
        elif kind == "failed":
            report = report_envelope([cancel_item(order["bet_id"], status="FAILURE", code="INVALID_BET_ID")],
                                     status="FAILURE", market_id=order["market_id"])
        else:
            report = report_envelope([cancel_item(order["bet_id"], status="TIMEOUT")], status="TIMEOUT",
                                     market_id=order["market_id"])
        pending = dict(order, state="CANCEL_PENDING")
        moves = lifecycle.classify_cancel_report(report, sent_bet_ids=[order["bet_id"]],
                                                 orders={order["bet_id"]: pending})
        state_after, fields = moves[order["bet_id"]]
        self.journal.append("report", {"action": "cancel", "transactions": lifecycle.report_transactions(
            "cancel", report, sent_count=1), "transitions": [{"customer_order_ref": order["customer_order_ref"],
                                                              "state": state_after, "fields": fields}]},
            intent_id=intent_id, market_id=order["market_id"])

    def op_replace(self, step, entry):
        if self.mode == "live":
            return                    # live replace was removed in v1: evaluate_replace refuses live mode
        state = self.state()
        candidates = [o for o in self._open(state) if o["bet_id"] is not None]
        if not candidates:
            return
        parent = self.rng.choice(candidates)
        new_price = tick_offset(parent["price_h"], self.rng.choice((-2, -1, 1, 2)))
        child_ref, cref, intent_id = self.refs()
        decision = self.guard.evaluate_replace(parent["market_id"], [{"bet_id": parent["bet_id"],
                                                                      "new_price_h": new_price}],
                                               book=self.book(parent["market_id"]), state=state, arming=ARMED,
                                               funds=self.funds(), funds_checked_at=self.funds_checked_at(),
                                               now_ns=self.now(),
                                               now_monotonic_ns=self.clock.mono_ns, kill_engaged=self.kill,
                                               customer_ref=cref)
        entry.update(decision=decision, market_id=parent["market_id"])
        if not decision["approved"]:
            return
        market = parent["market_id"]
        child_order = instruction(selection_id=parent["selection_id"], side=parent["side"], price_h=new_price,
                                  size_p=parent["size_remaining_p"])
        self.journal.append("intent", intent_payload(child_order, parent_ref=parent["customer_order_ref"],
                                                     provisional_child=True),
                            intent_id=intent_id, customer_order_ref=child_ref, customer_ref=cref, market_id=market,
                            state_after="RECORDED")
        params = {"marketId": market, "customerRef": cref, "marketVersion": {"version": decision["market_version"]},
                  "instructions": [{"betId": parent["bet_id"], "newPrice": wire_price(new_price)}]}
        cap, prepared = self._issue(decision, REPLACE_METHOD, params)
        self._submit("replace", intent_id, prepared, [
            {"customer_order_ref": parent["customer_order_ref"], "state": "REPLACE_PENDING"},
            {"customer_order_ref": child_ref, "state": "SUBMITTING"}], market)
        self._send(cap, prepared)
        entry["accepted"] = True
        kind = self.rng.choice(("ok", "ok", "timeout", "cancel_failed", "not_placed"))
        cancel = cancel_item(parent["bet_id"], cancelled=parent["size_remaining_p"])
        place = place_report(None, bet_id=self.bet_id(), market_id=market)["instructions"][0]
        item = {"bet_id": parent["bet_id"], "new_price_h": new_price, "status": "SUCCESS", "error_code": None,
                "cancel": cancel, "place": place}
        if kind == "timeout":
            item = dict(item, status="TIMEOUT", cancel=None, place=None)
            report = report_envelope([item], status="TIMEOUT", market_id=market)
        elif kind == "cancel_failed":
            item = dict(item, status="FAILURE", error_code="INVALID_BET_ID", place=None,
                        cancel=cancel_item(parent["bet_id"], status="FAILURE", code="INVALID_BET_ID"))
            report = report_envelope([item], status="FAILURE", market_id=market)
        elif kind == "not_placed":
            item = dict(item, status="FAILURE", error_code="CANCELLED_NOT_PLACED",
                        place=dict(place, status="FAILURE", bet_id=None, order_status=None,
                                   error_code="CANCELLED_NOT_PLACED"))
            report = report_envelope([item], status="FAILURE", market_id=market)
        else:
            report = report_envelope([item], market_id=market)
        pending = dict(parent, state="REPLACE_PENDING")
        moves = lifecycle.classify_replace_report(report, sent=[{"bet_id": parent["bet_id"], "new_price_h": new_price,
                                                                 "order": pending}],
                                                  provisional_children={parent["customer_order_ref"]: child_ref})
        self.journal.append("report", {"action": "replace", "transactions": lifecycle.report_transactions(
            "replace", report, sent_count=1), "transitions": [
            {"customer_order_ref": r, "state": s, "fields": f} for r, (s, f) in sorted(moves.items())]},
            intent_id=intent_id, market_id=market)
        entry["outcome"] = kind

    def _fill(self, partial):
        state = self.state()
        candidates = self._open(state)
        if not candidates:
            return
        order = self.rng.choice(candidates)
        amount = order["size_remaining_p"] if not partial else max(1, order["size_remaining_p"] // 2)
        price = order["price_h"]
        if self.rng.random() < 0.3:
            price = tick_offset(price, 1 if order["side"] == "BACK" else -1)
        fills = [(amount, price)] + ([(order["size_matched_p"], order["average_price_matched_h"])]
                                     if order["size_matched_p"] else [])
        total = sum(size for size, _ in fills)
        weighted = sum(size * p for size, p in fills)
        average = weighted // total if order["side"] == "BACK" else -((-weighted) // total)
        remaining = order["size_remaining_p"] - amount
        self.journal.append("report", {"action": "place", "exchange_update": "fill", "transitions": [
            {"customer_order_ref": order["customer_order_ref"],
             "state": "EXECUTABLE" if remaining else "EXECUTION_COMPLETE",
             "fields": {"size_matched_p": order["size_matched_p"] + amount, "average_price_matched_h": average,
                        "size_remaining_p": remaining}}]}, market_id=order["market_id"])

    def op_fill(self, step, entry):
        self._fill(partial=False)

    def op_partial_fill(self, step, entry):
        self._fill(partial=True)

    def op_advance(self, step, entry):
        self.clock.advance(step["seconds"])

    def op_clock_step(self, step, entry):
        self.clock.step_wall(step["seconds"])

    def op_kill(self, step, entry):
        """Engage, then evaluate the spec section 9.8 sweep: every own bet that may still be unmatched,
        unresolved ones included, market by market, at most 60 per request."""
        self.kill = True
        self.journal.append("kill", {"reason": "property_drill", "scope": "own"})
        self.journal.append("halt", {"reason": "kill"})
        state = self.state()
        targets = {}
        for order in state["orders"].values():
            sweepable = (order["state"] in UNRESOLVED_STATES or order["provisional_child"] is True
                         or (order["state"] == "EXECUTABLE" and order["size_remaining_p"] > 0))
            if sweepable and order["bet_id"] is not None and order["state"] not in ("RECORDED",) + tuple(
                    lifecycle.TERMINAL_STATES):
                targets.setdefault(order["market_id"], []).append(order["bet_id"])
        entry["sweep"] = []
        for market in sorted(targets):
            bets = sorted(targets[market])
            for start in range(0, len(bets), 60):
                _, cref, _ = self.refs()
                decision = self.guard.evaluate_cancel(market, bets[start:start + 60], state=state, now_ns=self.now(),
                                                      customer_ref=cref)
                entry["sweep"].append({"market_id": market, "bets": bets[start:start + 60],
                                       "approved": decision["approved"], "failures": decision["detail"]["failures"]})

    def op_resume(self, step, entry):
        if self.kill:
            self.kill = False
            self.journal.append("resume", {"scope": "global"})

    def op_resolve(self, step, entry):
        state = self.state()
        unknown = sorted((o for o in state["orders"].values()
                          if o["state"] in ("UNKNOWN", "ABSENT_UNCONFIRMED") and o["provisional_child"] is not True),
                         key=lambda o: o["customer_order_ref"])
        if not unknown:
            return
        order = self.rng.choice(unknown)
        transitions = []
        child = state["orders"].get(order["child_ref"]) if order["child_ref"] else None
        child_open = child is not None and child["provisional_child"] is True
        if order["state"] == "ABSENT_UNCONFIRMED":
            transitions.append({"customer_order_ref": order["customer_order_ref"], "state": "ABSENT_ACCEPTED"})
            kind = "operator_accept"
        elif child_open and self.rng.random() < 0.5:
            # The replace happened: parent's remainder cancelled, the child is on the exchange.
            transitions.append({"customer_order_ref": order["customer_order_ref"], "state": "EXECUTION_COMPLETE",
                                "fields": {"size_cancelled_p": order["size_cancelled_p"] + order["size_remaining_p"],
                                           "size_remaining_p": 0}})
            transitions.append({"customer_order_ref": child["customer_order_ref"], "state": "EXECUTABLE",
                                "fields": {"bet_id": self.bet_id()}})
            kind = "reconcile"
        else:
            choice = self.rng.choice(("found", "complete", "absent"))
            if child_open:
                choice = "found"
                transitions.append({"customer_order_ref": child["customer_order_ref"], "state": "ABSENT_UNCONFIRMED"})
            if choice == "found":
                fields = {} if order["bet_id"] else {"bet_id": self.bet_id()}
                transitions.insert(0, {"customer_order_ref": order["customer_order_ref"], "state": "EXECUTABLE",
                                       "fields": fields})
            elif choice == "complete":
                fields = {"size_matched_p": order["size_p"] - order["size_cancelled_p"] - order["size_lapsed_p"],
                          "average_price_matched_h": order["price_h"], "size_remaining_p": 0}
                if not order["bet_id"]:
                    fields["bet_id"] = self.bet_id()
                transitions.insert(0, {"customer_order_ref": order["customer_order_ref"],
                                       "state": "EXECUTION_COMPLETE", "fields": fields})
            else:
                transitions.insert(0, {"customer_order_ref": order["customer_order_ref"],
                                       "state": "ABSENT_UNCONFIRMED"})
            kind = "reconcile"
        self.journal.append(kind, {"transitions": transitions}, market_id=order["market_id"])
        for move in transitions:
            if move["state"] == "ABSENT_UNCONFIRMED" and move["customer_order_ref"] != order["customer_order_ref"]:
                self.journal.append("operator_accept", {"transitions": [
                    {"customer_order_ref": move["customer_order_ref"], "state": "ABSENT_ACCEPTED"}]},
                    market_id=order["market_id"])

    def op_settle(self, step, entry):
        state = self.state()
        for market in list(self.markets):
            orders = [o for o in state["orders"].values() if o["market_id"] == market]
            if any(o["state"] in UNRESOLVED_STATES or o["provisional_child"] is True and o["state"] != "RECORDED"
                   for o in orders):
                continue
            if not any(o["size_matched_p"] > 0 for o in orders):
                continue
            lapses = []
            for order in orders:
                if order["state"] == "EXECUTABLE":
                    lapses.append({"customer_order_ref": order["customer_order_ref"],
                                   "state": "EXECUTION_COMPLETE" if order["size_matched_p"] else "LAPSED",
                                   "fields": {"size_lapsed_p": order["size_lapsed_p"] + order["size_remaining_p"],
                                              "size_remaining_p": 0}})
            if lapses:
                self.journal.append("report", {"action": "place", "exchange_update": "market_close",
                                               "transitions": lapses}, market_id=market)
            state = self.state()
            orders = [o for o in state["orders"].values() if o["market_id"] == market
                      and o["state"] in ("EXECUTION_COMPLETE", "REPLACED") and o["size_matched_p"] > 0]
            winner = self.rng.choice(SELECTIONS + (99,))
            results = {s: ("WINNER" if s == winner else "LOSER") for s in SELECTIONS + (99,)}
            rate = self.config["orders"]["paper"]["commission_rate"]
            realized = market_realized(orders, results, commission_rate=rate)
            transitions = [{"customer_order_ref": o["customer_order_ref"], "state": "SETTLED",
                            "fields": {"settled_profit_own_p": order_pnl_pence(
                                o["side"], o["average_price_matched_h"], o["size_matched_p"],
                                results[o["selection_id"]])}}
                           for o in sorted(orders, key=lambda o: o["customer_order_ref"])]
            if type(realized["commission_pence"]) is not int:
                raise AssertionError(f"synthetic settlement excluded: {realized['excluded_reason']}")
            self.journal.append("settle", {"commission_p": realized["commission_pence"], "transitions": transitions},
                                market_id=market)
            self.markets.remove(market)
            self.markets.append(f"1.{self.next_market}")
            self.next_market += 1
            entry["settled"] = market
            return

    def op_forge(self, step, entry):
        """Capability drills: forged and replayed decisions must never mint (live); paper never mints."""
        decisions = [e["decision"] for e in self.log if e["decision"] is not None]
        if not decisions:
            return
        forged = copy.deepcopy(self.rng.choice(decisions))
        forged["approved"] = True
        forged["rule"] = None
        if forged["instructions"] and "size_p" in forged["instructions"][0]:
            forged["instructions"][0]["size_p"] = 5000
        body = canonical_json({"jsonrpc": "2.0", "method": PLACE_METHOD, "params": {}, "id": 1})
        prepared = {"method": PLACE_METHOD, "body": body, "body_sha256": bytes_sha256(body), "rpc_id": 1}
        try:
            self.guard.issue(forged, prepared, now_monotonic_ns=self.clock.monotonic_ns())
        except OrderRefused as exc:
            entry["forge_refused"] = exc.rule
        else:
            entry["forge_refused"] = None

    def op_restart(self, step, entry):
        """Rebuild the guard (and the journal object) from persisted events; decisions must not change."""
        now = self.now()
        market = self.rng.choice(self.markets)
        candidate = instruction(selection_id=12, side="BACK", price_h=BASE_PRICES_H[12], size_p=100)
        book = self.book(market)
        old_state = self.state()
        if self.journal_factory is not None:
            self.journal.close()
            self.journal = self.journal_factory()
            new_state = self.journal.fold(now_ns=now)
        else:
            new_state = fold_events(list(self.journal.events()), mode=self.mode, now_ns=now)
        fresh = RiskGuard(self.config, mode=self.mode, clock=self.clock)
        ref, cref, _ = self.refs()
        kwargs = dict(book=book, arming=ARMED, funds=self.funds(), funds_checked_at=self.funds_checked_at(), now_ns=now,
                      now_monotonic_ns=self.clock.mono_ns, kill_engaged=self.kill, customer_ref=cref,
                      customer_order_refs=[ref])
        before = self.guard.evaluate_place(market, [candidate], state=old_state, **kwargs)
        after = fresh.evaluate_place(market, [candidate], state=new_state, **kwargs)
        entry["restart_equal"] = (before == after and old_state == new_state)
        self.guard = fresh
        self.restart_checks += 1


# --- independent oracle -----------------------------------------------------------------------
#
# Written from the spec section 10 definitions without risk.py or the fold: every winner in turn, the
# matched part of each order at its average price (its limit if no average is known), and each
# unit of size that may still fill - an open or unresolved order's unaccounted size, or a
# replace parent and its provisional child, of which at most one can exist - filled in full or
# not at all, whichever is worse for that winner. Choices are independent given the winner, so
# the per-winner minimum over each unit's options is the brute-force worst case over every
# combination of fills.

_ORACLE_OPEN_STATES = frozenset({"SUBMITTING", "UNKNOWN", "ABSENT_UNCONFIRMED", "CANCEL_PENDING", "REPLACE_PENDING",
                                 "EXECUTABLE"})


def _oracle_pnl_cp(order, size, price_h, winner):
    """Hundredths of a penny won (+) or lost (-) by `size` of the order at `price_h` if `winner` wins."""
    odds_part = size * (price_h - 100)
    if order["side"] == "BACK":
        return odds_part if winner == order["selection_id"] else -size * 100
    return -odds_part if winner == order["selection_id"] else size * 100


def _oracle_open_size(order):
    """Size that was sent and is not known to be matched, cancelled or lapsed (0 for RECORDED or done)."""
    if order["state"] not in _ORACLE_OPEN_STATES:
        return 0
    return order["size_p"] - order["size_matched_p"] - order["size_cancelled_p"] - order["size_lapsed_p"]


def oracle_market_loss(market_orders):
    """Worst-case loss in pence (rounded up) of one market's orders under adversarial settlement."""
    winners = {order["selection_id"] for order in market_orders} | {"other"}
    by_ref = {order["customer_order_ref"]: order for order in market_orders}
    units, paired = [], set()
    for child in market_orders:
        parent = by_ref.get(child["parent_ref"])
        if child["provisional_child"] is True and parent is not None:
            units.append((parent, child))
            paired.update({id(parent), id(child)})
    units.extend((order,) for order in market_orders if id(order) not in paired)
    worst = 0
    for winner in winners:
        total = 0
        for order in market_orders:
            if order["size_matched_p"] > 0 and order["state"] not in ("SETTLED", "VOIDED"):
                price = order["average_price_matched_h"] or order["price_h"]
                total += _oracle_pnl_cp(order, order["size_matched_p"], price, winner)
        for unit in units:
            options = [0] + [_oracle_pnl_cp(o, _oracle_open_size(o), o["price_h"], winner) for o in unit]
            total += min(options)
        worst = max(worst, -total)
    return -((-worst) // 100)


def oracle_exposure(state):
    """({market_id: worst-case loss}, total) over every market holding orders."""
    grouped = {}
    for order in state["orders"].values():
        grouped.setdefault(order["market_id"], []).append(order)
    by_market = {market: oracle_market_loss(market_orders) for market, market_orders in grouped.items()}
    return by_market, sum(by_market.values())


def oracle_realized_loss(harness, entry):
    """Loss realised on the entry's UTC day, from the settle events themselves (own P&L less commission)."""
    day_start = entry["now_ns"] - entry["now_ns"] % (86_400 * 10**9)
    net = 0
    for event in list(harness.journal.events())[:entry["events"]]:
        if event["kind"] != "settle" or parse_utc_ns(event["recorded_at"]) < day_start:
            continue
        net -= event["payload"]["commission_p"]
        for move in event["payload"]["transitions"]:
            if move["state"] == "SETTLED":
                net += move["fields"]["settled_profit_own_p"]
    return max(0, -net)
