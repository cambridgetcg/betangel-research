"""Pilot 1's unchanged code beside Pilot 2's journal entries (docs/betfair-pilot-2-engineering.md §7).

Without a gateway: an open or unsettled Pilot 2 leg is one of Pilot 1's open manual refs (pilot_gate.py:318-334), and a
P2 ARM record is no pilot note, so Pilot 1's journal readers ignore it (synthetic events and core-fold records). With
the real gateways over the simulated exchange (test_betfair_support_pilot's registered Pilot 1 world, config-p2.json
beside its config, and test_betfair_p2_gateway's race fixtures): Pilot 1's pilot.fold over a journal holding a Pilot 2
race (its ARM record, funds checks, intents, risk decisions, submits, reports and cancels, but no HALT, kill or
resume) equals the fold with each of those events replaced by a plain note; a HALT a Pilot 2 pull journals stops
Pilot 1 (its ARM refuses, pilot_run.py:907-908, and so does its pilot-place open); and a Pilot 2 leg left resting halts
a Pilot 1 open and refuses Pilot 1's ARM (pilot-run through cli.main, pilot_run.py:909-910). No /usr/bin/security,
git or other child process runs, and nothing opens the network. Pilot 1's files and tests are byte-identical to
c86764a's (sha256, by name; pilot_run.py also as PR #59 leaves it).
"""

import contextlib
import hashlib
import io
import json
import unittest

from betangel_research.betfair import cli, p2_lp, pilot, pilot_gate
from betangel_research.betfair import config as cfg
from betangel_research.betfair.arming import expected_token
from betangel_research.betfair.errors import NotArmed
from betangel_research.betfair.journal import fold_events
from betangel_research.betfair.paths import FORBID_NETWORK_ENV
from betangel_research.betfair.receipts import utc_from_ns
from betangel_research.betfair.risk import RiskGuard

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_orders as so
import test_betfair_support_pilot as sp
from test_betfair_admission import FakeProcesses
from test_betfair_p2_gateway import MARKET, arm_record, fake_git, leg, no_children, open_p2, p2_orders, race, set_time

S = pilot.NS
DAY = "2027-01-04"
DAY_NS = pilot.parse_utc_ns(DAY + "T00:00:00Z")
ARM_NS = DAY_NS + 9 * 3600 * pilot.NS

# Pilot 1's files at c86764a by sha256, listed by name (build spec §7; note §14), so a change to any of them fails here.
# pilot_run.py may also be as PR #59 (25c1341) leaves it: one handoff line (:270) says the lab Mac, not the Air. #59
# merges first; once it has, its hash alone stays.
PILOT_1_SHA256 = {
    "betangel_research/betfair/pilot.py": ("c0e76246241b6ba2aaf9667cbd859ddea8081e6e90d0014a1e2cda4411bd69dd",),
    "betangel_research/betfair/pilot_run.py": ("702cd4fb8825aa2f4dec05a99b0aecdb2d20eeff9ebbb2e8de70f225a05afb62",
                                               "2388181915c1f723b60c5449717f19bf505892ffcc8e96d82ef5ebc746c37ec2"),
    "betangel_research/betfair/pilot_gate.py": ("61a4e88365aa33e1f2fffe5c5f1e3d4f28b654b478aaa3036c31d108f62820e0",),
    "research/pilots/p1-fav-t5.json": ("4dbccb5a45f7a4779e0263d4df5048f582f10b8dc3bd22c5f2e93e791155de92",),
    "tests/test_betfair_pilot.py": ("c1c9e26ce2a894c11f9620dc6de7a2eb0e606dfac9cfc1a9c131642e7d0b63fc",),
    "tests/test_betfair_pilot_carriers.py": ("21a66e8949a9d7f06c788fbbd953b2b98af9cefe8a13655898fa19d353d6ac06",),
    "tests/test_betfair_pilot_gateway.py": ("8ac842f0c4e41234ab78cf75885193aa9ec107747ae0940808b22fe20c8e9758",),
    "tests/test_betfair_pilot_ownership.py": ("d8f379497a93bf79ac8ccaf30551c219b8f38f59f3a6f3582874e5a50c4eb8c7",),
    "tests/test_betfair_pilot_review_fixes.py": ("ac3f6affc80d9ee6c269cdf4518bfc5a60fff0ff2066ee77e594d2f4adeaa163",),
    "tests/test_betfair_pilot_run.py": ("e77dccdf83646e7dd722aaf284f88f5d270dacc0621e011e8f80bc6ce318030a",),
    "tests/test_betfair_pilot_run_gaps.py": ("f3ebb2b5d2b88d6cc3137bc10d773d6aabb1a2f4763ddbef8332185691e0ceb7",),
    "tests/test_betfair_pilot_scope.py": ("3efee26ff5fe6eb371d44bb49f934a2ed22a5cfda6fe77be114db8025eb25500",),
    "tests/test_betfair_support_pilot.py": ("8885f3a737214815cf1d6450967354b9e58873794e1df73c19c0a6f7495a9b65",),
    "tests/test_betfair_rollout_pilot_lines.py": ("30905b59f98f1f4507bb09ad2d107198a37b55c9b0897d3787faa1e424a11dfe",),
}


def intent(seq, ref, source):
    return {"seq": seq, "kind": "intent", "customer_order_ref": ref, "recorded_at": utc_from_ns(ARM_NS + seq),
            "payload": {"source": source}}


def p2_leg(ref, **changes):
    return so.order_record(**{"customer_order_ref": ref, "customer_strategy_ref": "p2-lp", "mode": "live",
                              "submitted_at": utc_from_ns(ARM_NS + 60 * pilot.NS), **changes})


class PilotOneFiles(unittest.TestCase):
    def test_pilot_1_files_sha256_at_c86764a(self):
        for name, allowed in PILOT_1_SHA256.items():
            with self.subTest(file=name):
                self.assertIn(hashlib.sha256((support.REPO_ROOT / name).read_bytes()).hexdigest(), allowed)


class PilotOneSeesPilotTwo(unittest.TestCase):
    def test_open_or_unsettled_p2_leg_is_a_pilot_1_manual_ref(self):
        events = [intent(1, "p2-back", "pilot_lp"), intent(2, "p2-lay", "pilot_lp"), intent(3, "p1", "pilot")]
        cases = {
            "resting": ({"state": "EXECUTABLE"}, ["p2-back"]),
            "unknown": ({"state": "UNKNOWN", "bet_id": None}, ["p2-back"]),
            "matched, not settled": ({"state": "EXECUTION_COMPLETE", "size_matched_p": 200}, ["p2-back"]),
            "pulled, nothing matched": ({"state": "EXECUTION_COMPLETE", "size_cancelled_p": 200}, []),
            "settled": ({"state": "SETTLED", "size_matched_p": 200}, []),
            "lapsed": ({"state": "LAPSED", "size_lapsed_p": 200}, []),
            "refused before its submit": ({"state": "REFUSED", "bet_id": None, "submitted_at": None}, []),
        }
        for label, (changes, expected) in cases.items():
            with self.subTest(case=label):
                orders = {"p2-back": p2_leg("p2-back", **changes),
                          "p1": so.order_record(customer_order_ref="p1", customer_strategy_ref="p1-fav-t5")}
                self.assertEqual(pilot_gate.open_manual_refs(events, orders), expected)
        both = {"p2-back": p2_leg("p2-back"), "p2-lay": p2_leg("p2-lay", side="LAY", price_h=305)}
        self.assertEqual(pilot_gate.open_manual_refs(events, both), ["p2-back", "p2-lay"])

    def test_p2_arm_record_is_no_pilot_note(self):
        payload = p2_lp.arm_payload(p2_lp.arm_line("c" * 12, DAY, "2000", 1), session_id="p2s-0a1b2c3d",
                                    config_sha256="c" * 64, utc_day=DAY, until_ns=DAY_NS + 20 * 3600 * pilot.NS,
                                    races=1, code_commit="d" * 40)
        events = [{"seq": 1, "kind": "operator_confirm", "recorded_at": utc_from_ns(ARM_NS), "payload": payload}]
        self.assertEqual(len(p2_lp.arm_records(events)), 1)
        self.assertNotIn("pilot", payload)
        self.assertIsNone(pilot_gate.latest_note(events, "session_start"))
        self.assertIsNone(pilot_gate.journal_contract_sha256(events))
        self.assertFalse(pilot_gate.session_window_open(events, now_ns=ARM_NS + pilot.NS))


def pilot_world(test):
    """Pilot 1's registered world (contract p1, its config.json) on the day before the race, with config-p2.json
    beside it (env.p2, env.p2_guard): Pilot 1's session on 2027-01-03; the clock then moved to the race day."""
    env = sp.build_pilot_gateway(test, arm=False, start=pilot.utc_text_from_ns(sp.OFF - pilot.DAY_NS - 3600 * S))
    no_children(test)
    sp.arm_session(env, until_ns=sp.UNTIL - pilot.DAY_NS, session_id="s-20270103")
    support.write_test_config(env.paths, support.deep_merge(gw.LIVE_OVERRIDES, p2_orders()),
                              path=env.paths.config_p2_path)
    env.p2 = cfg.load_config_p2(paths=env.paths)
    env.p2_guard = RiskGuard(env.p2, mode="live", clock=env.clock, paths=env.paths)
    set_time(env.clock, sp.OFF - 720 * S)
    env.arm_seq = arm_record(env)
    return env


def p2_race(env, *, matched=False):
    """One Pilot 2 race in its own open: the BACK and the LAY at off - 590 s (with matched, the LAY's report is lost
    after it landed and both legs match while resting), then the pull at off - 300 s, LAY first."""
    race(env)
    gateway = open_p2(env)
    set_time(env.clock, sp.OFF - 590 * S)
    leg(env, gateway, "BACK")
    if matched:
        def lands_then_lost(record):
            env.exchange.place(json.loads(record["body"])["params"])
            return support.transport_failure("receive")
        env.router.next_response[gw.PLACE_SUFFIX] = [lands_then_lost]
    leg(env, gateway, "LAY")
    if matched:
        for order in env.exchange.orders.values():
            order.update(status="EXECUTION_COMPLETE", sizeMatched=2.0, sizeRemaining=0.0,
                         averagePriceMatched=order["priceSize"]["price"])
    set_time(env.clock, sp.OFF - 300 * S)
    bets = {order["side"]: order["bet_id"] for order in gateway.lp_legs().values()}
    for side in ("LAY", "BACK"):
        gateway.cancel(MARKET, [bets[side]], source="cli_manual")
    gateway.close()


def pilot_run_cli(env):
    """Pilot 1's pilot-run --mode live through cli.main with the world's deps -> (exit code, stderr)."""
    deps = {"keychain_runner": env.runner, "http_transport": env.transport, "clock": env.clock,
            "confirmer": env.confirmer, "env": {FORBID_NETWORK_ENV: "1"}, "pilot_context": env.ctx,
            "processes": FakeProcesses(pid=1)}
    argv = ["pilot-run", "--mode", "live", "--confirm-live", expected_token(env.config, env.clock.time_ns()),
            "--until", "2000"]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, deps=deps)
    return code, err.getvalue()


def neutral(event):
    """The event as a plain note that carries nothing (its seq and time kept)."""
    return dict(event, kind="note", payload={"reason": "neutral"}, intent_id=None, customer_order_ref=None,
                customer_ref=None, bet_id=None, market_id=None, state_after=None)


class PilotOneBesidePilotTwo(unittest.TestCase):
    def setUp(self):
        fake_git(self)

    def test_pilot_1_fold_ignores_p2_events_without_halt_kill_or_resume(self):
        env = pilot_world(self)
        first = env.arm_seq
        p2_race(env)
        last = env.journal.anchor()["seq"]
        set_time(env.clock, sp.OFF + pilot.DAY_NS - 3600 * S)
        sp.arm_session(env, until_ns=sp.UNTIL + pilot.DAY_NS, session_id="s-20270105")
        events = list(env.journal.events())
        p2 = [event for event in events if first <= event["seq"] <= last]
        self.assertTrue({"operator_confirm", "funds_check", "intent", "risk_decision", "submit", "report",
                         "cancel_intent"} <= {event["kind"] for event in p2})
        self.assertEqual({event["payload"]["source"] for event in p2 if event["kind"] == "intent"}, {"pilot_lp"})
        self.assertFalse(any(event["kind"] in ("halt", "kill", "resume") or "halts" in event["payload"]
                             for event in p2))
        now = env.clock.time_ns()
        state = pilot.fold(events, contract=sp.C, now_ns=now)
        self.assertEqual(state, pilot.fold([neutral(e) if first <= e["seq"] <= last else e for e in events],
                                           contract=sp.C, now_ns=now))
        self.assertEqual([session["session_id"] for session in state.sessions], ["s-20270103", "s-20270105"])
        self.assertIsNone(pilot.placement_authority(state, phase="reserve", now_ns=now, session_id="s-20270105"))

    def test_p2_halt_stops_pilot_1(self):
        env = pilot_world(self)
        p2_race(env, matched=True)                       # an UNKNOWN LAY found, then two short cancels: HALT
        core = fold_events(list(env.journal.events()), mode="live", now_ns=env.clock.time_ns())
        self.assertIn("consecutive_unknown_outcomes", core["halt_reasons"])
        set_time(env.clock, sp.OFF + pilot.DAY_NS - 3600 * S)
        code, err = pilot_run_cli(env)                     # Pilot 1's ARM refuses the HALT (until RESUME LIVE)
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: pilot_arm_halted"), err)
        sp.arm_session(env, until_ns=sp.UNTIL + pilot.DAY_NS, session_id="s-20270105")
        with self.assertRaises(NotArmed) as caught:          # and so does a Pilot 1 open (A7)
            sp.open_pilot(self, env)
        self.assertIn("halted", caught.exception.detail["failures"])

    def test_p2_leg_open_halts_pilot_1_open_and_refuses_its_arm(self):
        env = pilot_world(self)
        race(env)
        gateway = open_p2(env)
        set_time(env.clock, sp.OFF - 590 * S)
        leg(env, gateway, "BACK")                         # left resting: the executor never got to its pull
        gateway.close()
        set_time(env.clock, sp.OFF + pilot.DAY_NS - 3600 * S)
        code, err = pilot_run_cli(env)
        self.assertEqual((code, err.strip().splitlines()[-1]), (2, "Betfair order refused: manual_order_open"), err)
        self.assertEqual(env.confirmer.prompts, [])                      # refused before the PILOT ARM line
        sp.arm_session(env, until_ns=sp.UNTIL + pilot.DAY_NS, session_id="s-20270105")
        report = sp.open_pilot(self, env)
        self.assertFalse(report["armed"])
        self.assertIn(pilot_gate.MANUAL_BET_OPEN, report["failures"])
        core = fold_events(list(env.journal.events()), mode="live", now_ns=env.clock.time_ns())
        self.assertIn(pilot_gate.MANUAL_BET_OPEN, core["halt_reasons"])


if __name__ == "__main__":
    unittest.main()
