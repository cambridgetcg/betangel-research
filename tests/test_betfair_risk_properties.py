"""Seeded property tests of the order core (RiskGuard + journal + lifecycle), synthetic and offline.

Each seed drives a gateway-shaped harness through a random sequence of places, cancels,
replaces, fills, partial fills, time advances, wall-clock steps, restarts, kills (with the
spec section 9.8 sweep evaluated), timeouts, transport failures, reconciliations, settlements and
capability forgeries. Odd seeds run in live mode (capabilities issued, then consumed by a fake
transport that records the bytes it is handed), even seeds in paper mode; half the seeds use
tight exposure caps so R8-R10 bind often; seeds 2 and 7 (mod 10) and 3 (mod 5) use small
hour, day and transaction caps so every rate window binds; seeds 0 and 1 (mod 20) use the real SQLite
journal, one paper and one live, so restarts reopen the file. Exposure and realised loss are
measured by the independent oracle in test_betfair_support_orders, not by risk.py or the fold.
Defaults are 40 seeds x 120 steps; BETANGEL_BETFAIR_PROPERTY_SEEDS and
BETANGEL_BETFAIR_PROPERTY_STEPS scale them. These are safety-property tests of local
accounting, not tests of any strategy or of Betfair itself.
"""

import tempfile
import unittest
from pathlib import Path

from betangel_research.betfair.config import money_pence
from betangel_research.betfair.errors import OrderRefused
from betangel_research.betfair.journal import DAY_NS, OrderJournal, fold_events
from betangel_research.betfair.receipts import bytes_sha256
from betangel_research.betfair.risk import _adverse_size, exposure_by_market, worst_case_market_loss
from betangel_research.betfair.schemas import UNRESOLVED_STATES, validate_order_record

import test_betfair_support as support
import test_betfair_support_orders as orders

SEEDS, STEPS = orders.property_scale()
FULL_SCALE = SEEDS * STEPS >= 2000
_RUNS = {}
_TEMP = []


def tearDownModule():
    for harness in _RUNS.values():
        harness.journal.close()
    for temp in _TEMP:
        temp.cleanup()
    _RUNS.clear()


def overrides_for(seed):
    overrides = support.deep_merge({}, orders.TIGHT_CAPS) if seed % 4 < 2 else {}
    if seed % 10 == 2:
        overrides = support.deep_merge(overrides, orders.HOUR_CAPS)
    elif seed % 10 == 7:
        overrides = support.deep_merge(overrides, orders.DAY_CAPS)
    elif seed % 5 == 3:
        overrides = support.deep_merge(overrides, orders.TRANSACTION_CAPS)
    return overrides or None


def harness_for(seed):
    """One cached run per seed, shared by every property below (the sequence is deterministic)."""
    if seed not in _RUNS:
        mode = "live" if seed % 2 else "paper"
        overrides = overrides_for(seed)
        factory = None
        if seed % 20 in (0, 1):
            temp = tempfile.TemporaryDirectory()
            _TEMP.append(temp)
            path = Path(temp.name) / f"journal-{mode}.sqlite"
            holder = {}

            def factory(path=path, holder=holder, mode=mode):
                return OrderJournal(path, mode=mode, clock=holder["clock"])

            harness = orders.OrderHarness(mode=mode, seed=seed, config_overrides=overrides,
                                          journal=orders.MemoryJournal(mode, support.FakeClock()))
            holder["clock"] = harness.clock
            harness.journal_factory = factory
            harness.journal = factory()
        else:
            harness = orders.OrderHarness(mode=mode, seed=seed, config_overrides=overrides)
        harness.run(orders.random_sequence(seed, STEPS))
        _RUNS[seed] = harness
    return _RUNS[seed]


def caps_pence(harness):
    caps = harness.config["orders"]["caps"]
    return {key: money_pence(caps[key]) for key in ("max_market_exposure", "max_total_exposure", "max_daily_loss")}


class RiskPropertyTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def each(self):
        for seed in range(SEEDS):
            yield seed, harness_for(seed)

    def test_property_market_exposure_never_exceeded(self):
        accepted = 0
        for seed, harness in self.each():
            cap = caps_pence(harness)["max_market_exposure"]
            for entry in harness.log:
                by_market, _ = orders.oracle_exposure(entry["state"])
                guard_view = exposure_by_market(list(entry["state"]["orders"].values()))
                for market, loss in by_market.items():
                    self.assertLessEqual(loss, cap, (seed, entry["index"], entry["op"], market))
                    # The guard's own measure never undercounts the independent one.
                    self.assertGreaterEqual(guard_view[market], loss, (seed, entry["index"], market))
                if entry["accepted"]:
                    accepted += 1
                    # The guard's worst case bounds what the market can lose once the report is in.
                    self.assertLessEqual(by_market.get(entry["market_id"], 0),
                                         entry["decision"]["worst_case"]["market_pence"], (seed, entry["index"]))
        if FULL_SCALE:
            self.assertGreater(accepted, 100)

    def test_property_total_and_daily_budgets_hold_adverse_settlement(self):
        settled = 0
        for seed, harness in self.each():
            caps = caps_pence(harness)
            for entry in harness.log:
                _, total = orders.oracle_exposure(entry["state"])
                self.assertLessEqual(total, caps["max_total_exposure"], (seed, entry["index"]))
                realized_loss = orders.oracle_realized_loss(harness, entry)
                settled += entry["op"] == "settle" and "settled" in entry
                self.assertLessEqual(realized_loss, max(0, -entry["state"]["realized_today_pence"]),
                                     (seed, entry["index"]))
                if entry["accepted"]:
                    # Settle every open position adversely: today's loss would still fit the daily budget.
                    self.assertLessEqual(realized_loss + total, caps["max_daily_loss"], (seed, entry["index"]))
        if FULL_SCALE:
            self.assertGreater(settled, 0)

    def test_property_rate_caps_hold_every_window(self):
        refused = {"rate_minute": 0, "rate_hour": 0, "rate_day": 0, "transactions_hour": 0}
        for seed, harness in self.each():
            for entry in harness.log:
                if entry["decision"] is not None:
                    for failure in entry["decision"]["detail"]["failures"]:
                        if failure["rule"] in refused:
                            refused[failure["rule"]] += 1
            caps = harness.config["orders"]["caps"]
            final = harness.log[-1]["state"] if harness.log else orders.empty_state()
            times = sorted(final["submitted_instruction_wall_ns"])
            for t in times:
                self.assertLessEqual(sum(1 for u in times if t - 60 * 10**9 < u <= t), caps["max_orders_per_minute"])
                self.assertLessEqual(sum(1 for u in times if t - 3600 * 10**9 < u <= t), caps["max_orders_per_hour"])
                day = t - t % DAY_NS
                self.assertLessEqual(sum(1 for u in times if day <= u < day + DAY_NS), caps["max_orders_per_day"])
            for entry in harness.log:
                if entry["accepted"]:
                    hour = entry["decision"]["evaluated_at"][:13]
                    used = sum(n for key, n in entry["state"]["transactions_by_clock_hour"].items() if key >= hour)
                    self.assertLessEqual(used, caps["max_transactions_per_hour"], (seed, entry["index"]))
        if FULL_SCALE:
            # Every window actually bound somewhere, so deleting any one check would fail a property.
            for rule, count in refused.items():
                self.assertGreater(count, 0, rule)

    def test_property_kill_blocks_all_later_places(self):
        blocked = 0
        for seed, harness in self.each():
            for entry in harness.log:
                if entry["op"] in ("place", "replace") and entry["kill_before"] and entry["decision"] is not None:
                    blocked += 1
                    self.assertFalse(entry["accepted"], (seed, entry["index"]))
                    self.assertEqual(entry["decision"]["rule"], "halted")
        if FULL_SCALE:
            self.assertGreater(blocked, 0)

    def test_property_kill_sweep_may_cancel_every_own_open_bet(self):
        swept = unresolved = 0
        for seed, harness in self.each():
            for entry in harness.log:
                for request in entry.get("sweep", ()):
                    swept += len(request["bets"])
                    self.assertLessEqual(len(request["bets"]), 60)
                    self.assertTrue(request["approved"], (seed, entry["index"], request["failures"]))
                    states = {o["bet_id"]: o["state"] for o in entry["state"]["orders"].values()}
                    unresolved += sum(states.get(bet) in UNRESOLVED_STATES for bet in request["bets"])
        if FULL_SCALE:
            self.assertGreater(swept, 0)

    def test_property_restart_from_journal_same_decisions(self):
        restarts = 0
        for seed, harness in self.each():
            for entry in harness.log:
                if entry["op"] == "restart":
                    restarts += 1
                    self.assertTrue(entry["restart_equal"], (seed, entry["index"]))
            now = harness.now()
            rebuilt = fold_events(list(harness.journal.events()), mode=harness.mode, now_ns=now)
            self.assertEqual(rebuilt, harness.journal.fold(now_ns=now), seed)
            if isinstance(harness.journal, OrderJournal):
                self.assertEqual(harness.journal.verify()["status"], "ok")
        if FULL_SCALE:
            self.assertGreater(restarts, 0)

    def test_property_replace_cannot_raise_liability_over_cap(self):
        replaced = 0
        for seed, harness in self.each():
            cap = caps_pence(harness)
            for entry in harness.log:
                if entry["op"] == "replace" and entry["accepted"]:
                    replaced += 1
                    decision = entry["decision"]
                    self.assertLessEqual(decision["worst_case"]["market_pence"], cap["max_market_exposure"])
                    by_market, total = orders.oracle_exposure(entry["state"])
                    self.assertLessEqual(by_market.get(entry["market_id"], 0), decision["worst_case"]["market_pence"],
                                         (seed, entry["index"], entry.get("outcome")))
                    self.assertLessEqual(total, cap["max_total_exposure"])
        if FULL_SCALE:
            self.assertGreater(replaced, 0)

    def test_property_unknown_never_zero_exposure(self):
        seen = 0
        for seed, harness in self.each():
            for entry in harness.log:
                for order in entry["state"]["orders"].values():
                    validate_order_record(order)
                    unresolved = order["state"] in UNRESOLVED_STATES or (
                        order["provisional_child"] is True and order["state"] != "RECORDED")
                    if not unresolved:
                        continue
                    seen += 1
                    self.assertEqual(order["exposure_basis"], "adverse_unknown")
                    self.assertGreater(worst_case_market_loss([order], []), 0, (seed, entry["index"]))
                    unaccounted = (order["size_p"] - order["size_matched_p"] - order["size_cancelled_p"]
                                   - order["size_lapsed_p"])
                    self.assertGreaterEqual(_adverse_size(order), unaccounted)
                    self.assertIn(order["market_id"], entry["state"]["unresolved_markets"])
        if FULL_SCALE:
            self.assertGreater(seen, 0)

    def test_property_capability_unforgeable_and_single_use(self):
        issued = 0
        for seed, harness in self.each():
            if harness.mode == "paper":
                self.assertEqual(harness.capabilities, [])
                self.assertEqual(harness.sent, [])
            submits = [event for event in harness.journal.events() if event["kind"] == "submit"]
            # Every body the fake transport received matches exactly one consumed capability (method and
            # digest of the bytes actually sent) and exactly one journaled submit with that digest.
            for method, body in harness.sent:
                digest = bytes_sha256(body)
                matching = [cap for cap in harness.capabilities if cap.method == method and cap.body_sha256 == digest]
                self.assertEqual(len(matching), 1, seed)
                self.assertTrue(matching[0].consumed)
                self.assertEqual(sum(1 for event in submits if event["payload"]["body_sha256"] == digest), 1, seed)
            sent_digests = [bytes_sha256(body) for _, body in harness.sent]
            self.assertEqual(len(set(sent_digests)), len(sent_digests))
            # Nothing was minted that was not either sent or deliberately not sent (transport_not_sent).
            unsent = [cap for cap in harness.capabilities if cap.body_sha256 not in sent_digests]
            for cap in unsent:
                self.assertFalse(cap.consumed)
                self.assertEqual(sum(1 for event in submits if event["payload"]["body_sha256"] == cap.body_sha256), 1)
            for capability in harness.capabilities:
                issued += 1
                if not capability.consumed:
                    continue
                with self.assertRaisesRegex(OrderRefused, "capability_reused"):
                    capability.consume(method=capability.method, body_sha256=capability.body_sha256,
                                       now_monotonic_ns=harness.clock.mono_ns)
            for entry in harness.log:
                if entry["op"] == "forge" and "forge_refused" in entry:
                    self.assertIsNotNone(entry["forge_refused"], (seed, entry["index"]))
                    expected = {"capability_mode"} if harness.mode == "paper" else {
                        "capability_decision_unknown", "capability_decision_reused", "capability_unapproved"}
                    self.assertIn(entry["forge_refused"], expected)
            accepted_live = sum(1 for e in harness.log if harness.mode == "live" and (
                e["accepted"] or (e["op"] == "cancel" and e["decision"] is not None and e["decision"]["approved"])))
            self.assertEqual(len(harness.capabilities), accepted_live, seed)
        if FULL_SCALE:
            self.assertGreater(issued, 0)


if __name__ == "__main__":
    unittest.main()
