"""P1-08's executor commands (design v4 §4; §9.3 onwards), offline and synthetic, driven through cli.main.

These run pilot-register, pilot-run (PILOT ARM, the scheduler, one placement open per decision through the real
P1-06 gateway, the remainder cancel and its KILL cases, settle points, capture, the end path and release),
pilot-end-session, pilot-ack, pilot-gap-resolved and pilot-capture over the test_betfair_support_run world: a
temporary state root, a fake clock and sleep, fake Keychain, transport, git, catalogue and process table, and a fake
confirmer that types exactly what each prompt asks (it stands in for Yu; nothing here types a line into a terminal).
A simulated SIGKILL (sigkill_after) stops the process right after one durable append. They show what each command
journals, prints, admits and releases; they do not show Betfair's real behaviour or a real terminal, Keychain,
signal delivery or recorder.
"""

import dataclasses
import json
import os
import signal
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission, cli, pilot, pilot_gate, pilot_run
from betangel_research.betfair.config import load_config
from betangel_research.betfair.credentials import ANCHOR_SERVICE
from betangel_research.betfair.errors import AdmissionRefused, AuthError, JournalError, LoginBudgetExceeded
from betangel_research.betfair.journal import OrderJournal

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
import test_betfair_support_run as sr
from test_betfair_admission import FakeProcesses, ledger_rows
from test_betfair_pilot_gateway import aping_error, contract_bytes, failure_report

S = sr.S
OFF = sr.OFF
DAY = sr.DAY_S * S


class HookConfirmer(gw.FakeConfirmer):
    """FakeConfirmer that runs hooks[prefix]() right after Yu's line is typed for a prompt starting with prefix (before
    confirm_line returns: "between the prompt and the lock")."""

    def __init__(self, hooks=None, **kw):
        super().__init__(**kw)
        self.hooks = dict(hooks or {})

    def confirm_line(self, expected, *, banner):
        super().confirm_line(expected, banner=banner)
        for prefix in list(self.hooks):
            if expected.startswith(prefix):
                self.hooks.pop(prefix)()


class RunCase(unittest.TestCase):
    def build(self, **kw):
        self.env = sr.build_run(self, **kw)
        return self.env

    def registered(self, **kw):
        env = self.build(**kw)
        code, out, err = sr.register(env)
        self.assertEqual(code, 0, err)
        return env

    def one_market(self, **kw):
        env = self.registered(**kw)
        self.market = sr.add_market(env, off_ns=OFF)
        return env

    def refused(self, result, code):
        exit_code, out, err = result
        self.assertEqual(exit_code, 2, err + out)
        self.assertIn(f"Betfair order refused: {code}", err)
        return err

    def nothing_admitted(self, env):
        view = sr.admission_view(env)
        self.assertIsNone(view["admission"])
        self.assertFalse(view["inhibit"])

    def released(self, env):
        self.nothing_admitted(env)
        self.assertFalse(sr.armed_file(env).exists())

    def session_stops(self, env):
        return [s["stop_id"] for s in sr.stops(env) if s["scope"] == "session"]

    def kinds(self, env, since=0):
        return [(e["kind"], (e["payload"].get("pilot") or {}).get("type")) for e in sr.events(env) if e["seq"] > since]

    def head(self, env):
        return sr.events(env)[-1]["seq"]

    def crash_and_recover(self, env, *, release=True):
        """After a simulated SIGKILL: pilot-end-session (its stops), then Yu's typed recording-release."""
        code, out, err = sr.end_session(env)
        if release:
            generation = sr.admission_view(env)["admission"]["generation"]
            sr.typed_release(env, generation)
        return code, out, err

    def next_day_arm(self, env, *, cause=True, market=False):
        """The next UTC day: ARM refuses pilot_review_stop_active until the active review stops are ACKed (with a
        cause), then proceeds (no plannable market: a clean end)."""
        sr.set_day(env, 1)
        if market:
            sr.add_market(env, off_ns=OFF + DAY)
        active = sr.active_review(env)
        self.assertTrue(active)
        err = self.refused(sr.arm_run(env), "pilot_review_stop_active")
        for stop_id in active:
            self.assertIn(stop_id, err)
            code, out, err = sr.ack(env, stop_id, cause=cause)
            self.assertEqual(code, 0, err)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        return out


# --- §9.12 one complete ARM, §9.14 loop basics, §9.16 clean end -------------------------------------------------


class HappyPathTests(RunCase):
    def test_one_session_end_to_end(self):
        env = self.one_market()
        seen = sr.spy_run(self)
        signalled = sr.hold_recorder(env)
        self.assertIsNotNone(admission.admission_status(env.paths)["recorder_pid"])
        code, out, err = sr.arm_run(env)
        self.assertEqual(signalled, [4242])                                   # the recorder was drained
        self.assertEqual(code, 0, err + out)
        run = seen["run"]
        session_id = run.session["session_id"]
        self.assertRegex(session_id, r"^s\d{8}-[0-9a-f]{8}$")
        self.assertEqual(seen["admission"]["admission"]["owner"], f"pilot-session:{session_id}")
        self.assertEqual(seen["admission"]["admission"]["state"], "admitted")
        self.assertEqual(pilot_run.bytes_sha256(seen["armed_bytes"]), run.armed_sha)
        self.assertEqual(len(sr.order_requests(env)), 2, err)             # the place and its remainder cancel
        state = sr.fold(env)
        self.assertEqual(len(state.place_records), 1)
        self.assertEqual([s["session_id"] for s in state.sessions], [session_id])
        starts = [e for e in sr.events(env) if (e["payload"].get("pilot") or {}).get("type") == "session_start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["payload"]["pilot"]["until_ns"], OFF - 10 * 3600 * S + 20 * 3600 * S)
        self.assertEqual(self.session_stops(env), [pilot.session_stop_id("session_end", starts[0]["seq"])])
        self.released(env)
        self.assertIn(f"recording released (generation {seen['admission']['admission']['generation']})", err)
        reads = sr.credential_reads(env)
        self.assertEqual(sorted(reads), sorted(set(reads)))                # each Keychain item read once
        summary = json.loads(out)
        self.assertEqual((summary["place_records"], summary["sends"], summary["end_code"]),
                         (1, 1, "no_plannable_market"))
        for word in ("£", "profit", "equity", "nclv", "stake", "pnl"):
            self.assertNotIn(word, out.lower())
        self.assertEqual(len(sr.logins(env)), 2)                            # the scheduler read and the open

    def test_no_plannable_market_ends_clean_and_releases(self):
        env = self.registered()
        seen = sr.spy_run(self)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["end_code"], "no_plannable_market")
        self.assertEqual(sr.order_requests(env), [])
        self.released(env)
        self.assertEqual(seen["admission"]["admission"]["generation"], 1)

    def test_arm_disarm_arm_reproduces_the_registered_sha(self):
        """Q6: the session_start binds the registered receipt sha; a second day's ARM binds the same one."""
        env = self.registered()
        receipt = sr.notes(env, "registration")[0]["registration_receipt_sha256"]
        self.assertEqual(sr.arm_run(env)[0], 0)
        sr.set_day(env, 1)
        self.assertEqual(sr.arm_run(env)[0], 0)
        self.assertEqual([n["registration_receipt_sha256"] for n in sr.notes(env, "session_start")], [receipt] * 2)

    def test_banner_pt4_and_wording(self):
        env = self.registered()
        self.assertEqual(sr.arm_run(env)[0], 0)
        banner = env.confirmer.prompts[-1]["banner"]
        self.assertIn("at most 5 PLACE records today", banner)                       # no PT4 receipt yet
        self.assertIn("Until 20:00:00Z", banner)
        self.assertIn("No reservation starts after 19:54:00Z", banner)
        self.assertIn("Recording turns off now", banner)
        self.assertEqual(banner.count("£"), 1)
        self.assertIn("£2.00", banner)
        for word in ("equity", "profit", "nclv"):
            self.assertNotIn(word, banner.lower())
        non_pt4 = pilot_run.arm_banner(contract=env.contract, day="2027-01-05", profile="default",
                                       session_end_ns=OFF + 10 * 3600 * S, pt4=False)
        self.assertIn("at most 25 PLACE records today", non_pt4)


class ObservedClockTests(RunCase):
    """§9.5: each placement open gets its own ObservedClock, shared by every part of that gateway."""

    def test_two_opens_two_clocks(self):
        env = self.registered()
        sr.add_market(env, off_ns=OFF)
        sr.add_market(env, off_ns=OFF + 1800 * S)
        made = []
        real = pilot_gate.ObservedClock

        class Recording(real):
            __slots__ = ()

            def __init__(self, clock):
                super().__init__(clock)
                made.append(self)

        gateways = []
        original = cli._live_gateway

        def spy(*args, **kw):
            result = original(*args, **kw)
            gateways.append(kw["held"]["gateway"])
            return result

        with patch.object(pilot_gate, "ObservedClock", Recording), patch.object(cli, "_live_gateway", spy):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(made), 2)
        self.assertIsNot(made[0], made[1])
        self.assertEqual([g.clock for g in gateways], made)
        for gateway, clock in zip(gateways, made):
            for part in (gateway.journal, gateway.guard, gateway.rpc, gateway.read, gateway.rpc.session):
                self.assertIs(part.clock, clock)


# --- §9.11 pilot-register ------------------------------------------------------------------------------------


class RegisterTests(RunCase):
    def test_receipt_and_note(self):
        env = self.build()
        head = self.head(env)
        code, out, err = sr.register(env)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        path = env.paths.pilot_dir / os.path.basename(result["receipt_path"])
        data = path.read_bytes()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        receipt = json.loads(data)
        self.assertEqual(set(receipt), pilot_gate.REGISTRATION_KEYS)
        self.assertEqual(receipt["journal_seq"], head)
        self.assertEqual(pilot_run.head_of([]), {})                        # an empty journal: journal_seq 0
        note = sr.notes(env, "registration")[0]
        self.assertEqual(note["registration_receipt_sha256"], pilot_run.bytes_sha256(data))
        self.assertIsNone(note["predecessor_contract_sha256"])
        self.assertEqual(env.confirmer.prompts[-1]["expected"], f"REGISTER PILOT {env.contract.sha256[:12]} "
                                                                f"{sp.COMMIT[:12]}")
        self.assertEqual(sr.credential_reads(env), [])
        self.assertEqual(env.transport.requests, [])

    def refuses(self, env, code, **kw):
        before = self.head(env)
        self.refused(sr.register(env, **kw), code)
        self.assertEqual(self.head(env), before)
        self.assertEqual(sr.credential_reads(env), [])

    def test_refusals(self):
        cases = {
            "not frozen": (lambda env: setattr(env.state, "contract_raw", b"{}"), "pilot_contract_not_frozen"),
            "null frozen_on": (lambda env: setattr(env.state, "contract_raw", env.state.contract_raw.replace(
                b'"frozen_on": "', b'"frozen_on": null, "x": "')), "pilot_contract_not_frozen"),
            "dirty tree": (lambda env: setattr(env.git, "dirty", True), "pilot_code_identity"),
            "unknown identity": (lambda env: setattr(env.git, "fail", True), "pilot_code_identity"),
            "hash drift": (lambda env: env.state.disk_hashes.update({next(iter(env.state.disk_hashes)): "f" * 64}),
                           "pilot_code_identity:pa2_code_hashes_differ"),
        }
        for name, (change, code) in cases.items():
            with self.subTest(case=name):
                env = self.build()
                change(env)
                self.refuses(env, code)
                self.assertEqual(sr.notes(env, "registration"), [])

    def test_config_mismatch(self):
        env = self.build(overrides={"orders": {"customer_strategy_ref": "other-strategy"}})
        self.refuses(env, "pilot_config_differs")

    def test_existing_path_another_contract_predecessor_and_moved_head(self):
        env = self.registered()
        self.refuses(env, "pilot_registration_path_exists")                 # same instant: the same receipt name
        env = self.build()
        env.journal.append("note", pilot.pilot_note("registration", "f" * 64, registration_receipt_sha256="e" * 64,
                                                    predecessor_contract_sha256=None))
        self.refuses(env, "pilot_journal_other_contract")
        env = self.build()
        env.journal.append("note", pilot.pilot_note("registration", env.contract.sha256,
                                                    registration_receipt_sha256="e" * 64,
                                                    predecessor_contract_sha256="d" * 64))
        self.refuses(env, "pilot_journal_other_contract")
        env = self.build()
        confirmer = HookConfirmer({"REGISTER PILOT": lambda: env.journal.append("note", {"reason": "another_writer"})})
        err = self.refused(sr.register(env, confirmer=confirmer), "journal_changed_during_register")
        self.assertIn("left inert", err)
        self.assertEqual(sr.notes(env, "registration"), [])
        self.assertEqual(len(list(env.paths.pilot_dir.glob("registration-*.json"))), 1)   # the orphan, inert

    def test_wrong_profile(self):
        env = self.build(profile="other")
        result = sr.run_cli(env, "--profile", "other", "pilot-register", "--mode", "live", "--confirm-live",
                            sr.token(env))
        self.refused(result, "pilot_profile_not_recorder_profile")
        self.assertEqual(env.runner.calls, [])


# --- §9.12 ARM refusals --------------------------------------------------------------------------------------


class ArmRefusalTests(RunCase):
    def refuses(self, env, code, *argv, confirmer=None, prompts=None, **kw):
        """ARM refuses code with nothing admitted, zero credential-item reads and logins, nothing journaled."""
        before = self.head(env)
        reads, logins = len(sr.credential_reads(env)), len(sr.logins(env))
        asked = len((confirmer or env.confirmer).prompts)
        if argv:
            result = sr.run_cli(env, "pilot-run", "--mode", "live", "--confirm-live", sr.token(env), *argv,
                                confirmer=confirmer)
        else:
            result = sr.arm_run(env, confirmer=confirmer, **kw)
        err = self.refused(result, code)
        try:
            self.assertIsNone(sr.admission_view(env)["admission"])
        except AdmissionRefused:
            self.assertEqual(code, "admission_state_unknown")
        self.assertEqual((len(sr.credential_reads(env)), len(sr.logins(env))), (reads, logins))
        self.assertEqual(self.head(env), before)
        if prompts is not None:
            self.assertEqual(len((confirmer or env.confirmer).prompts) - asked, prompts)
        return err

    def test_until(self):
        env = self.registered()
        for until in ("2460", "960", "25:00", "x"):
            with self.subTest(until=until):
                self.refuses(env, "pilot_arm_until_invalid", "--until", until, prompts=0)
        for until in ("0900", "0950"):                                   # past; within 6 min of now (09:50Z)
            with self.subTest(until=until):
                self.refuses(env, "pilot_arm_until_invalid", "--until", until, prompts=0)
        code, out, err = sr.run_cli(env, "pilot-run", "--mode", "live", "--confirm-live", sr.token(env))
        self.assertEqual(code, 2)                                         # argparse: --until has no default
        self.assertIn("--until", err)

    def test_wrong_line_typed(self):
        env = self.registered()
        confirmer = gw.FakeConfirmer(lines=["PILOT ARM 000000000000 20270104 UNTIL 2000Z"])
        self.refuses(env, "operator_echo_mismatch", confirmer=confirmer, prompts=1)

    def test_second_arm_and_no_registration(self):
        env = self.registered()
        self.assertEqual(sr.arm_run(env)[0], 0)
        self.refuses(env, "pilot_arm_already_today", prompts=0)
        env = self.build()
        self.refuses(env, "pilot_not_registered", prompts=0)

    def test_kill_halt_ledger_manual_and_armed_file(self):
        env = self.registered()
        env.paths.kill_file.write_text("")
        self.refuses(env, "pilot_arm_halted", prompts=0)
        self.assertFalse(sr.admission_view(env)["inhibit"])

        env = self.registered()
        ledger_rows(env.paths, {"kind": "login_attempt", "attempt_id": "a", "at": env.clock.utc_now(),
                                "method": "cert", "jurisdiction": "uk", "emergency": False, "coexistence_ack": True},
                    {"kind": "login_outcome", "attempt_id": "a", "at": env.clock.utc_now(), "method": "cert",
                     "jurisdiction": "uk", "outcome": "auth_error", "outcome_status": "auth_error",
                     "outcome_error": None, "emergency": False, "coexistence_ack": True, "ban_until": None})
        self.refuses(env, "pilot_arm_login_ledger", prompts=0)

        env = self.registered()
        pilot_run.write_armed_session(env.paths, dict.fromkeys(pilot_run.ARMED_KEYS, "x") | {
            "schema": pilot_run.ARMED_SCHEMA})
        err = self.refuses(env, "pilot_end_session_needed", prompts=0)
        self.assertIn("pilot-end-session", err)

        env = self.registered()
        market = sr.add_market(env, off_ns=OFF)
        sp.manual_place(env, market)
        self.refuses(env, "manual_order_open", prompts=0)

    def test_stray_inhibit_and_unknown_admission_state(self):
        env = self.registered()
        env.paths.inhibit_file.write_text("")
        err = self.refuses(env, "pilot_arm_release_would_refuse:inhibit_preexisting", prompts=0)
        self.assertIn("INHIBIT-only form", err)
        env = self.registered()
        env.paths.admission_file.write_text("{not json")
        err = self.refuses(env, "admission_state_unknown", prompts=0)
        self.assertNotIn("INHIBIT-only", err)

    def test_retained_admission_refused_before_the_prompt(self):
        for owner, state in (("pilot-session:s20270103-0a0b0c0d", "admitted"), ("arm-live:77", "pending")):
            with self.subTest(owner=owner, state=state):
                env = self.registered()
                admission.atomic_write_json(env.paths.admission_generation_file, {
                    "schema_version": 1, "generation": 3, "updated_at": "2027-01-03T12:00:00.000000Z"})
                admission.atomic_write_json(env.paths.admission_file, {
                    "schema_version": 1, "generation": 3, "owner": owner, "purpose": "earlier",
                    "created_at": "2027-01-03T12:00:00.000000Z", "state": state})
                env.paths.inhibit_file.write_text("")
                before = sr.recording_files(env)
                with patch.object(admission, "admit", side_effect=AssertionError("admit called")):
                    code, out, err = sr.arm_run(env)
                self.refused((code, out, err), "admission_held")
                self.assertEqual(env.confirmer.prompts[-1]["expected"][:8], "REGISTER")      # no ARM prompt
                self.assertEqual(sr.recording_files(env), before)
                self.assertEqual(sr.credential_reads(env), [])

    def test_identity_drift(self):
        cases = {
            "dirty tree": lambda env: setattr(env.git, "dirty", True),
            "commit drift": lambda env: setattr(env.git, "commit", "d" * 40),
            "hash drift": lambda env: env.state.disk_hashes.update({next(iter(env.state.disk_hashes)): "f" * 64}),
            "config drift": lambda env: setattr(env, "config", env.config),
            "contract file": lambda env: setattr(env.state, "contract_raw", env.state.contract_raw + b" "),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                env = self.registered()
                change(env)
                if name == "config drift":                     # a live.armed flip moves config_sha256 too
                    data = json.loads(env.paths.config_path.read_text())
                    data["http"]["timeout_s"] = data["http"]["timeout_s"] + 1
                    env.paths.config_path.write_text(json.dumps(data))
                    env.config = load_config(env.paths.config_path, paths=env.paths)
                err = self.refuses(env, "pilot_identity_differs", prompts=0)
                self.assertIn("note:87", err)

    def test_a_due_carrier_stop_is_the_only_write(self):
        """A refusal note is its own carrier: its review stop is due until journaled. The first ARM journals exactly
        that stop and refuses pilot_review_stop_active:<id>; a second ARM writes nothing. With an identity drift or a
        wrong profile beside the same due stop, ARM writes nothing."""
        env = self.registered()
        pilot_run.append_pilot(env.journal, pilot.pilot_note(
            "refusal", env.contract.sha256, session_id=None, open_id=None, reservation_seq=None,
            code="executor_exception", row_sha256=None, book_sha256=None))
        due = [s["stop_id"] for s in sr.fold(env).due_stops]
        self.assertEqual(len(due), 1)
        env.git.dirty = True
        self.refuses(env, "pilot_identity_differs", prompts=0)
        result = sr.run_cli(env, "--profile", "other", "pilot-run", "--mode", "live", "--confirm-live",
                            sr.token(env), "--until", "2000")
        self.refused(result, "pilot_profile_not_recorder_profile")
        self.assertEqual([s["stop_id"] for s in sr.fold(env).due_stops], due)
        env.git.dirty = False
        before = self.head(env)
        err = self.refused(sr.arm_run(env), "pilot_review_stop_active")
        self.assertIn(due[0], err)
        self.assertEqual([e["payload"]["pilot"]["stop_id"] for e in sr.events(env) if e["seq"] > before], due)
        self.assertEqual(sr.fold(env).due_stops, ())
        self.refuses(env, "pilot_review_stop_active", prompts=0)
        self.nothing_admitted(env)

    def test_wrong_profile(self):
        env = self.build(profile="other")
        result = sr.run_cli(env, "--profile", "other", "pilot-run", "--mode", "live", "--confirm-live",
                            sr.token(env), "--until", "2000")
        self.refused(result, "pilot_profile_not_recorder_profile")
        self.assertFalse(env.paths.admission_file.exists())
        self.assertEqual(env.runner.calls, [])


# --- §9.12 the lifecycle around ARM ---------------------------------------------------------------------------


class ArmLifecycleTests(RunCase):
    def armed_then(self, inject, code, *, owner_changed=False):
        """inject() runs while the session credential loader runs (between step 3 and step 6)."""
        env = self.one_market()
        env.runner.on_credential = inject
        err = self.refused(sr.arm_run(env), code)
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertEqual(sr.order_requests(env), [])
        return env, err

    def test_substitutions_between_step_3_and_step_6(self):
        def rewrite(env, **changes):
            data = json.loads(env.paths.admission_file.read_text())
            data.update(changes)
            admission.atomic_write_json(env.paths.admission_file, data)

        cases = {
            "other owner": (lambda env: rewrite(env, owner="pilot-session:s20270104-ffffffff"),
                            "pilot_arm_ownership_changed", "admission_owner_changed"),
            "generation N+1": (lambda env: rewrite(env, generation=2), "pilot_arm_ownership_changed", None),
            "pending": (lambda env: rewrite(env, state="pending"), "pilot_arm_ownership_changed", None),
            "removed": (lambda env: env.paths.admission_file.unlink(), "pilot_arm_ownership_changed", None),
            "inhibit removed": (lambda env: env.paths.inhibit_file.unlink(), "pilot_arm_ownership_changed", None),
            "unreadable": (lambda env: env.paths.admission_file.write_text("{"), "admission_state_unknown", None),
        }
        for name, (change, code, release) in cases.items():
            with self.subTest(case=name):
                env = self.one_market()
                env.runner.on_credential = lambda env=env, change=change: change(env)
                err = self.refused(sr.arm_run(env), code)
                self.assertEqual(sr.notes(env, "session_start"), [])
                self.assertIn("generation 1", err)
                if release:
                    self.assertIn(release, err)

    def test_armed_file_changed(self):
        def other_session(env):
            sha, record = pilot_run.read_armed_session(env.paths)
            pilot_run.write_armed_session(env.paths, dict(record, session_id="s20270104-00000000"))

        def edited(env):
            path = pilot_run.armed_session_path(env.paths)
            path.write_bytes(path.read_bytes() + b"\n")

        def symlink(env):
            path = pilot_run.armed_session_path(env.paths)
            target = path.with_name("elsewhere.json")
            os.replace(path, target)
            os.symlink(target, path)

        cases = {"another session's": (other_session, "pilot_arm_session_file_changed", True),
                 "edited": (edited, "pilot_arm_session_file_changed", True),
                 "removed": (lambda env: pilot_run.armed_session_path(env.paths).unlink(),
                             "pilot_session_state_unknown", False),
                 "symlink": (symlink, "pilot_session_state_unknown", True)}
        for name, (change, code, left) in cases.items():
            with self.subTest(case=name):
                env = self.one_market()
                env.runner.on_credential = lambda env=env, change=change: change(env)
                self.refused(sr.arm_run(env), code)
                self.assertEqual(sr.notes(env, "session_start"), [])
                self.assertEqual(os.path.lexists(pilot_run.armed_session_path(env.paths)), left)
                self.nothing_admitted(env)                         # ownership was ours: release_own succeeded

    def test_owner_lock_lost(self):
        env = self.one_market()
        seen = {}
        original = admission.OwnerTicket.holds

        def holds(self):
            return seen.get("lost") is None and original(self)

        env.runner.on_credential = lambda: seen.update(lost=True)
        with patch.object(admission.OwnerTicket, "holds", holds):
            err = self.refused(sr.arm_run(env), "owner_lock_lost")
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertIn("owner_lock_lost", err)

    def test_a_due_stop_that_appears_before_step_6_is_flushed_on_the_held_lock(self):
        """A due stop the fold implies only from the credential load on (as if another process's carrier landed then):
        step 6 journals it on the .gateway.lock it already holds (flush_due, which takes the lock, is never called)
        and refuses pilot_arm_due_stops."""
        env = self.one_market()
        due = pilot_run.session_stop(env.contract, "lock_busy", 1, source_seq=1)["pilot"]
        original = pilot.fold
        flag = {}

        def fold(events, **kw):
            state = original(events, **kw)
            if flag and not any((e["payload"].get("pilot") or {}).get("stop_id") == due["stop_id"] for e in events):
                state = dataclasses.replace(state, due_stops=(due,))
            return state

        env.runner.on_credential = lambda: flag.update(armed=True)
        with patch.object(pilot, "fold", fold), \
                patch.object(pilot_run, "flush_due", side_effect=AssertionError("a second .gateway.lock hold")):
            err = self.refused(sr.arm_run(env), "pilot_arm_due_stops")
        self.assertIn(due["stop_id"], err)
        self.assertEqual([n["stop_id"] for n in sr.notes(env, "stop")], [due["stop_id"]])
        self.assertEqual(sr.notes(env, "session_start"), [])

    def test_inhibit_race_at_step_2(self):
        env = self.one_market()
        confirmer = HookConfirmer({"PILOT ARM": lambda: env.paths.inhibit_file.write_text("")})
        err = self.refused(sr.arm_run(env, confirmer=confirmer), "pilot_arm_release_would_refuse:inhibit_preexisting")
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertIn("generation 1", err)
        self.assertNotIn("INHIBIT-only", err)

    def test_admission_appears_after_the_prompt(self):
        env = self.one_market()

        def retained():
            admission.atomic_write_json(env.paths.admission_file, {
                "schema_version": 1, "generation": 7, "owner": "arm-live:9", "purpose": "x",
                "created_at": "2027-01-04T09:00:00.000000Z", "state": "admitted"})

        confirmer = HookConfirmer({"PILOT ARM": retained})
        self.refused(sr.arm_run(env, confirmer=confirmer), "admission_held")
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertEqual(sr.credential_reads(env), [])

    def test_identity_drift_between_prompt_and_lock(self):
        env = self.one_market()
        env.runner.on_credential = lambda: setattr(env.git, "dirty", True)
        err = self.refused(sr.arm_run(env), "pilot_identity_differs")
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertFalse(sr.armed_file(env).exists())
        self.assertIn("recording released (generation 1)", err)
        self.released(env)

    def test_kill_during_the_drain_refuses_release_refused(self):
        """A KILL file that appears while the recorder drains: step 6 refuses, release_own refuses release_refused
        with every problem, the owner and generation N are printed, ADMISSION and INHIBIT stay, and a later ARM
        meets admission_held. Exit 2 'Betfair order refused', never 'Betfair input error'."""
        env = self.one_market()
        sr.hold_recorder(env)
        stop = env.processes.on_terminate

        def kill(pid):
            env.paths.kill_file.write_text("")
            stop(pid)

        env.processes.on_terminate = kill
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 2, err)
        self.assertIn("Betfair order refused: pilot_arm_halted", err)
        self.assertNotIn("Betfair input error", err)
        self.assertIn("release refused: release_refused (kill_file_engaged)", err)
        self.assertIn("generation 1", err)
        self.assertEqual(sr.notes(env, "session_start"), [])
        view = sr.admission_view(env)
        self.assertEqual((view["admission"]["generation"], view["inhibit"]), (1, True))
        env.paths.kill_file.unlink()
        self.refused(sr.arm_run(env), "admission_held")

    def test_a_moved_head_after_admit_refuses_and_releases(self):
        env = self.one_market()
        env.runner.on_credential = lambda: env.journal.append("note", {"reason": "another_writer"})
        err = self.refused(sr.arm_run(env), "journal_changed_during_arm")
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertIn("recording released (generation 1)", err)
        self.released(env)

    def test_signals_during_the_credential_load_and_the_lock_wait(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig, where="credential load"):
                env = self.one_market()
                env.runner.on_credential = lambda sig=sig: sr.fire(sig)
                err = self.refused(sr.arm_run(env), "pilot_arm_stopped")
                self.assertEqual(sr.notes(env, "session_start"), [])
                self.assertFalse(sr.armed_file(env).exists())
                self.assertIn("recording released (generation 1)", err)
                self.assertLessEqual(len(sr.credential_reads(env)), 1)
                self.assertEqual(sr.arm_run(env)[0], 0)                   # the UTC day is not used up
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig, where="step 6 lock wait"):
                env = self.one_market()
                holder = {}

                def hold(env=env, holder=holder, sig=sig):
                    lock = pilot_run.file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock)
                    holder["lock"] = lock.__enter__()
                    holder["ctx"] = lock

                    def release_soon(clock, holder=holder, sig=sig):
                        return "ctx" in holder

                    def let_go(holder=holder, sig=sig):
                        sr.fire(sig)
                        holder.pop("ctx").__exit__(None, None, None)

                    sr.on_sleep(env, release_soon, let_go)

                env.runner.on_credential = hold
                code, out, err = sr.arm_run(env)
                self.refused((code, out, err), "pilot_arm_stopped")
                self.assertEqual(sr.notes(env, "session_start"), [])
                self.assertFalse(sr.armed_file(env).exists())

    def test_credential_error(self):
        env = self.one_market(keychain_items={})
        err = self.refused(sr.arm_run(env), "pilot_arm_credentials_unavailable")
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertFalse(sr.armed_file(env).exists())
        self.assertEqual(len(sr.credential_reads(env)), 1)
        self.released(env)

    def test_clock_past_until_minus_6_min_before_the_append(self):
        env = self.one_market()
        env.runner.on_credential = lambda: env.clock.advance(10 * 3600 + 400)        # 19:56:40Z
        self.refused(sr.arm_run(env), "pilot_arm_until_invalid")
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertFalse(sr.armed_file(env).exists())

    def test_sigint_during_a_fake_drain(self):
        env = self.one_market()
        sr.hold_recorder(env)
        env.processes = FakeProcesses(pid=1, starts={4242: "start-A"}, on_terminate=lambda pid: sr.fire(signal.SIGINT))
        with patch.object(admission, "DRAIN_TIMEOUT_S", 0):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 2, err)
        self.assertEqual(sr.notes(env, "session_start"), [])
        self.assertEqual(sr.credential_reads(env), [])

    def test_an_exception_after_session_start_journals_a_review_stop_and_keeps_admission(self):
        env = self.one_market()
        with patch.object(pilot_run.Run, "loop", side_effect=RuntimeError("synthetic")):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["executor_exception"])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        self.assertIn("recording-release (generation 1)", err)

    def test_the_parser_has_no_continue_admission(self):
        env = self.registered()
        code, out, err = sr.arm_run(env, "2000", "--continue-admission", "1")
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments", err)


# --- §9.3 projection, §9.17 pilot-end-session, §9.2 recovery of carriers --------------------------------------------


class RecoveryTests(RunCase):
    """A simulated SIGKILL mid-run, then pilot-end-session: the projection's handoffs and stop, zero transport and
    Keychain calls, the "before recording-release" block with the release line last, never a release."""

    def killed(self, kind, *, when=None, pilot_type=None, setup=None):
        env = self.one_market()
        if setup is not None:
            setup(env)
        with sr.sigkill_after(kind, pilot_type, when=when), self.assertRaises(sr.Death):
            sr.arm_run(env)
        self.assertTrue(sr.armed_file(env).exists())
        self.assertEqual(sr.admission_view(env)["admission"]["state"], "admitted")
        return env

    def recover(self, env):
        requests, calls = len(env.transport.requests), len(env.runner.calls)
        code, out, err = sr.end_session(env)
        self.assertEqual(len(env.transport.requests), requests)
        self.assertEqual(sr.credential_reads(env)[:0], [])
        self.assertEqual([c for c in env.runner.calls[calls:] if "-w" in c["argv"]], [])
        self.assertEqual(sr.admission_view(env)["admission"]["state"], "admitted")      # never a release
        self.assertFalse(sr.armed_file(env).exists())
        lines = [line for line in err.splitlines() if line and line != "MODE=LIVE"]
        self.assertIn("recording-release (generation 1)", lines[-1])
        return code, json.loads(out), lines

    def projection(self, env):
        return pilot_run.recovery_projection(sr.events(env), contract=env.contract, now_ns=env.clock.time_ns())

    def test_c12_cases(self):
        place = lambda payload: payload.get("action") == "place"            # noqa: E731
        cancel = lambda payload: payload.get("action") == "cancel"          # noqa: E731
        cases = {
            "EXECUTABLE 100p remainder": ("cancel_intent", None, lambda env: sr.fills(env, matched=1.0),
                                          "EXECUTABLE", "remainder_left_executable"),
            "SUBMITTING": ("submit", place, None, "UNKNOWN", "unknown_unresolved"),      # folded as UNKNOWN
            "CANCEL_PENDING": ("submit", cancel, None, "CANCEL_PENDING", "unknown_unresolved"),
            "only EXECUTION_COMPLETE": ("report", place, lambda env: sr.fills(env, complete=True), None,
                                        "session_abandoned"),
        }
        for name, (kind, when, setup, state, reason) in cases.items():
            with self.subTest(case=name):
                env = self.killed(kind, when=when, setup=setup)
                projection = self.projection(env)
                self.assertTrue(projection["armed"])
                self.assertEqual([h["state"] for h in projection["handoffs"]], [state] if state else [])
                self.assertEqual(projection["stop_reason"], reason)
                code, result, lines = self.recover(env)
                session = sr.fold(env).sessions[-1]
                self.assertEqual(result["session_stop"], reason)
                self.assertEqual(self.session_stops(env), [pilot.session_stop_id(reason, session["seq"])])
                self.assertEqual(code, 5 if state else 0)
                if state:
                    self.assertTrue(lines[0].startswith("pilot order "))         # the exact handoff first
                    self.assertIn("own-scope kill", lines[0])
                self.assertEqual(sr.arm_run(env)[0], 2)                          # today is used up
        env = self.one_market()
        self.assertEqual(sr.arm_run(env)[0], 0)
        projection = self.projection(env)
        self.assertEqual((projection["armed"], projection["handoffs"]), (False, []))
        before = self.head(env)
        code, result, lines = self.recover_ended(env)
        self.assertEqual((code, result["session_stop"], self.head(env)), (0, None, before))

    def recover_ended(self, env):
        code, out, err = sr.end_session(env)
        return code, json.loads(out), err.splitlines()

    def test_refuses_while_the_owner_lock_is_held_or_unknown(self):
        env = self.registered()
        with pilot_run.file_lock(env.paths.live_owner_lock, timeout_s=0, clock=env.clock):
            self.refused(sr.end_session(env), "pilot_session_alive")
        with patch.object(pilot_run, "file_lock", side_effect=OSError("unreadable")):
            self.refused(sr.end_session(env), "pilot_session_state_unknown")

    def test_a_carrier_after_until_and_the_next_day(self):
        """§9.2: FAILED_NOT_SENT(connect_failed), SIGKILL after the report and before the latch: due_stops holds the
        carried review stop; pilot-end-session journals it before and after UNTIL (no session stop after UNTIL); the
        next-day ARM refuses until it is ACKed with a cause."""
        for after_until in (False, True):
            with self.subTest(after_until=after_until):
                def connect_fails(env):
                    env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("connect",
                                                                                           "connect_failed")]
                env = self.killed("report", when=lambda p: p.get("action") == "place", setup=connect_fails)
                report = [e for e in sr.events(env) if e["kind"] == "report"][-1]
                res = sr.notes(env, "reservation")[-1]
                res_seq = [e["seq"] for e in sr.events(env)
                           if (e["payload"].get("pilot") or {}).get("type") == "reservation"][-1]
                carried = pilot.carried_review_stop_id("connect_failed", res_seq, report["seq"])
                self.assertEqual([s["stop_id"] for s in sr.fold(env).due_stops], [carried])
                if after_until:
                    env.clock.advance(11 * 3600)
                before = self.head(env)
                code, out, err = sr.end_session(env)
                written = [e["payload"]["pilot"]["stop_id"] for e in sr.events(env) if e["seq"] > before]
                self.assertEqual(written, [carried])           # the carrier ended the session: no session stop
                self.assertEqual(sr.fold(env).due_stops, ())
                sr.typed_release(env, 1)
                self.next_day_arm(env)
                self.assertIsNotNone(res)

    def test_the_review_stop_needs_its_cause(self):
        def connect_fails(env):
            env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("connect", "connect_failed")]
        env = self.killed("report", when=lambda p: p.get("action") == "place", setup=connect_fails)
        self.crash_and_recover(env)
        sr.set_day(env, 1)
        (stop_id,) = sr.active_review(env)
        self.refused(sr.ack(env, stop_id, cause=False), "ack_cause_required")
        self.assertEqual(sr.notes(env, "ack"), [])
        self.assertEqual(sr.ack(env, stop_id)[0], 0)
        self.assertEqual(sr.arm_run(env)[0], 0)


# --- §9.16 ends ------------------------------------------------------------------------------------------------


class EndTests(RunCase):
    def test_i_the_end_path_after_until_with_no_stop(self):
        env = self.registered()
        with patch.object(pilot_run.Run, "loop", lambda self: self.clock.advance(11 * 3600)):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.session_stops(env), [])
        self.released(env)

    def test_ii_until_crossed_before_the_session_end_append(self):
        env = self.registered()
        original = pilot_run.Run.owe

        def owe(self, payload, **columns):
            if (payload.get("pilot") or {}).get("reason") == "session_end":
                self.clock.advance(11 * 3600)
            return original(self, payload, **columns)

        with patch.object(pilot_run.Run, "owe", owe):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.session_stops(env)), 1)
        self.assertIsNone(sr.fold(env).sessions[-1]["ended_seq"])
        self.released(env)

    def test_iii_an_armed_session_with_no_stop_fails_the_stop_check(self):
        env = self.registered()
        seen = {}

        def loop(self):
            seen["problems"] = pilot_run.pilot_stop_check(self.paths, contract=self.contract,
                                                          session_id=self.session["session_id"],
                                                          clock=self.clock)(self.paths)

        with patch.object(pilot_run.Run, "loop", loop):
            self.assertEqual(sr.arm_run(env)[0], 0)
        self.assertEqual(seen["problems"], ["pilot_session_not_ended"])

    def test_release_refused_leaves_admission_and_inhibit(self):
        env = self.registered()
        original = admission.live_state_problems
        armed = []
        original_loop = pilot_run.Run.loop

        def loop(self):
            armed.append(True)
            return original_loop(self)

        with patch.object(admission, "live_state_problems",
                          lambda paths, **kw: original(paths, **kw) + (["order_EXECUTABLE"] if armed else [])), \
                patch.object(pilot_run.Run, "loop", loop):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("release refused", err)
        self.assertIn("order_EXECUTABLE", err)
        view = sr.admission_view(env)
        self.assertEqual((view["admission"]["generation"], view["inhibit"]), (1, True))
        self.assertIn("recording-release (generation 1)", err.strip().splitlines()[-1])

    def test_no_release_after_a_stop_in_session_or_an_open_read_session(self):
        env = self.registered()

        def loop(self):
            self.owe(pilot_run.session_stop(self.contract, "lock_busy", self.session["seq"], source_seq=1))

        with patch.object(pilot_run.Run, "loop", loop):
            self.assertEqual(sr.arm_run(env)[0], 5)
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        self.assertFalse(sr.armed_file(env).exists())                   # a stop was journaled: the file goes

        env = self.one_market()

        def broken_close(self):
            raise RuntimeError("logout failed")

        original = pilot_run.Run.read_client

        def read_client(self):
            client = original(self)
            session = self.scheduler[0]
            session.close = lambda: broken_close(session)
            return client

        with patch.object(pilot_run.Run, "read_client", read_client):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_the_defensive_inhibit_branch_makes_no_release_call(self):
        env = self.registered()
        ticket = admission.OwnerTicket(owner="pilot-session:s1", command_owner="pilot-session:s1", generation=4,
                                       kind="fresh", placement_allowed=True, inhibit_preexisting=True,
                                       recorder=None, held=None)
        with patch.object(admission, "release", side_effect=AssertionError("release called")):
            result = admission.release_own(ticket, env.paths, config=env.config, clock=env.clock)
        self.assertEqual((result["released"], result["outcome"]), (False, "inhibit_preexisting"))
        self.assertIn("generation 4", pilot_run.release_lines(result)[0])

    def test_the_next_arm_after_an_abnormal_end_is_admission_held(self):
        env = self.registered()
        with patch.object(pilot_run.Run, "loop", side_effect=RuntimeError("synthetic")):
            self.assertEqual(sr.arm_run(env)[0], 5)
        sr.set_day(env, 1)
        asked = len(env.confirmer.prompts)
        err = self.refused(sr.arm_run(env), "admission_held")
        self.assertEqual(len(env.confirmer.prompts), asked)
        self.assertIn("recording-release (generation 1)", err)
        sr.typed_release(env, 1)
        self.refused(sr.arm_run(env), "pilot_review_stop_active")        # then the executor_exception review stop
        (stop_id,) = sr.active_review(env)
        self.assertEqual(sr.ack(env, stop_id)[0], 0)
        self.assertEqual(sr.arm_run(env)[0], 0)


# --- §9.18 pilot-ack, §9.19 pilot-gap-resolved ------------------------------------------------------------------


class AckTests(RunCase):
    def with_review_stop(self):
        env = self.registered()
        with patch.object(pilot_run.Run, "loop", side_effect=RuntimeError("synthetic")):
            self.assertEqual(sr.arm_run(env)[0], 5)
        sr.typed_release(env, 1)
        (stop_id,) = sr.active_review(env)
        return env, stop_id

    def test_refusals_write_nothing(self):
        env, stop_id = self.with_review_stop()
        before = self.head(env)
        self.refused(sr.ack(env, "no-such-stop-9"), "ack_unknown_stop")
        self.refused(sr.ack(env, stop_id, cause=False), "ack_cause_required")
        self.refused(sr.ack(env, stop_id, cause="/nonexistent/cause.txt"), "ack_cause_unreadable")
        self.refused(sr.ack(env, f"pause-{env.contract.pause_step_p}"), "ack_level_not_fired")
        self.assertEqual(self.head(env), before)
        self.assertEqual(list(env.paths.pilot_dir.glob("ack-cause-*")), [])
        self.assertEqual(sr.ack(env, stop_id)[0], 0)
        self.refused(sr.ack(env, stop_id), "ack_already_acked")
        self.assertEqual(len(sr.notes(env, "ack")), 1)

    def test_an_existing_cause_file_is_never_overwritten(self):
        env, stop_id = self.with_review_stop()
        target = env.paths.pilot_dir / (f"ack-cause-{stop_id}-"
                                        f"{pilot_run.utc_compact_from_ns(env.clock.time_ns())}.txt")
        target.write_text("earlier cause")
        self.refused(sr.ack(env, stop_id), "ack_cause_copy_failed")
        self.assertEqual(target.read_text(), "earlier cause")
        self.assertEqual(sr.notes(env, "ack"), [])

    def test_the_due_stop_flow_and_no_reopen(self):
        env, stop_id = self.with_review_stop()

        def carrier():
            pilot_run.append_pilot(env.journal, pilot.pilot_note(
                "refusal", env.contract.sha256, session_id=None, open_id=None, reservation_seq=None,
                code="admission_lost", row_sha256=None, book_sha256=None))

        err = self.refused(sr.ack(env, stop_id, confirmer=HookConfirmer({"PILOT ACK": carrier})), "pilot_ack_due_stops")
        self.assertIn("retype", err)
        self.assertEqual(sr.notes(env, "ack"), [])
        self.assertEqual(sr.fold(env).due_stops, ())
        env.clock.advance(1)                                   # a new second: a new cause-copy name (O_EXCL)
        self.assertEqual(sr.ack(env, stop_id)[0], 0)
        for other in sr.active_review(env):                    # the stop the carrier added
            self.assertEqual(sr.ack(env, other)[0], 0)
        self.assertEqual(sr.active_review(env), [])
        self.refused(sr.arm_run(env), "pilot_arm_already_today")          # the ACK never reopens today

    def test_a_pause_ack_needs_no_cause(self):
        """Note §10's pause, scaled down (step 400p): a filled first bet at worst case -200p makes the next PLACE
        SKIP(pause) with its stops; the pause ACK passes with no cause."""
        env = self.build(contract_raw=contract_bytes(**{"pause.step_p": 400}))
        self.assertEqual(sr.register(env)[0], 0)
        sr.add_market(env, off_ns=OFF)
        sr.add_market(env, off_ns=OFF + 1800 * S)
        sr.fills(env, complete=True)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(sorted(s["stop_id"] for s in sr.stops(env)), ["pause-400", "pause-400-session"])
        self.assertEqual(len([r for r in sr.order_requests(env) if r["method"].endswith("placeOrders")]), 1)
        sr.typed_release(env, 1)
        self.assertEqual(sr.ack(env, "pause-400", cause=False)[0], 0)
        self.assertEqual(sr.active_review(env), [])


class GapTests(RunCase):
    def with_gap(self):
        env = self.registered()
        self.assertEqual(sr.arm_run(env)[0], 0)
        pilot_run.append_pilot(env.journal, {"pilot": pilot.stop_payload(env.contract, "commission-gap-3", "review",
                                                                         "commission_gap", source_seq=3)})
        return env

    def test_binds_the_occurrence_never_acknowledges(self):
        env = self.with_gap()
        self.refused(sr.ack(env, "commission-gap-3"), "ack_ledger_gap_open")
        before = self.head(env)
        self.refused(sr.gap_resolved(env, "no-such-9"), "gap_unknown_stop")
        self.refused(sr.gap_resolved(env, pilot.session_stop_id("session_end", sr.notes(env, "session_start") and
                                                                 [e["seq"] for e in sr.events(env) if (
                                                                     e["payload"].get("pilot") or {}).get("type")
                                                                  == "session_start"][0])), "gap_unknown_stop")
        self.assertEqual(self.head(env), before)
        code, out, err = sr.gap_resolved(env, "commission-gap-3")
        self.assertEqual(code, 0, err)
        (note,) = sr.notes(env, "gap_resolved")
        self.assertEqual((note["stop_id"], note["decision_seq"], note["cause_written"]), ("commission-gap-3", 3, True))
        self.assertEqual(sr.active_review(env), ["commission-gap-3"])            # never an acknowledgement
        self.refused(sr.gap_resolved(env, "commission-gap-3"), "gap_already_resolved")
        self.assertEqual(sr.ack(env, "commission-gap-3")[0], 0)
        self.assertEqual(sr.active_review(env), [])

    def test_a_non_gap_stop_and_a_moved_head(self):
        env = self.with_gap()
        pilot_run.append_pilot(env.journal, {"pilot": pilot.stop_payload(env.contract, "manual-check-4", "review",
                                                                         "manual_check", source_seq=None)})
        self.refused(sr.gap_resolved(env, "manual-check-4"), "gap_not_a_ledger_gap")
        moved = HookConfirmer({"PILOT GAP": lambda: env.journal.append("note", {"reason": "another_writer"})})
        self.refused(sr.gap_resolved(env, "commission-gap-3", confirmer=moved), "journal_changed_during_gap_resolved")
        self.assertEqual(sr.notes(env, "gap_resolved"), [])


# --- §9.6 signals -------------------------------------------------------------------------------------------------


def places(env):
    return [r for r in sr.order_requests(env) if r["method"].endswith("placeOrders")]


def cancels(env):
    return [r for r in sr.order_requests(env) if r["method"].endswith("cancelOrders")]


def not_logout(env, since=0):
    return [r for r in env.transport.requests[since:] if not r["url"].endswith("/api/logout")]


class SignalTests(RunCase):
    def test_a_sigint_during_the_scheduler_wait(self):
        env = self.one_market()
        seen = {}

        def stop():
            seen["requests"] = len(env.transport.requests)
            seen["logins"] = len(sr.logins(env))
            seen["at"] = env.clock.time_ns()
            sr.fire(signal.SIGINT)

        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 520 * S, stop)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(not_logout(env, seen["requests"]), [])            # no open, no read, no login after it
        self.assertEqual(len(sr.logins(env)), seen["logins"])
        self.assertEqual(sr.notes(env, "reservation"), [])
        self.assertLessEqual(env.clock.time_ns() - seen["at"], 1 * S)
        self.assertEqual(len(self.session_stops(env)), 1)
        self.assertIn(pilot_run.STOP_LINE, err)
        self.assertEqual(json.loads(out)["signal"], "SIGINT")
        self.released(env)

    def test_b_a_sigint_during_the_lock_held_wait(self):
        env = self.one_market()
        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 340 * S, lambda: sr.fire(signal.SIGINT))
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual((sr.notes(env, "reservation"), places(env)), ([], []))
        self.assertEqual(len(self.session_stops(env)), 1)
        self.released(env)

    def test_c_a_sigint_between_the_reservation_and_its_record(self):
        env = self.one_market()
        original = OrderJournal.append

        def append(journal, kind, payload, **columns):
            event = original(journal, kind, payload, **columns)
            if (payload.get("pilot") or {}).get("type") == "reservation":
                sr.fire(signal.SIGINT)
            return event

        with patch.object(OrderJournal, "append", append):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        state = sr.fold(env)
        self.assertEqual([r["interrupted"] for r in state.reservations.values()], [True])
        self.assertEqual([k for k in self.kinds(env) if k[0] == "submit"], [])
        self.assertEqual(len(self.session_stops(env)), 1)
        self.released(env)

    def test_signals_deferred_during_a_send(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                env = self.one_market()
                sr.add_market(env, off_ns=OFF + 1800 * S)
                env.transport.before_gate = lambda sig=sig: sr.fire(sig)
                code, out, err = sr.arm_run(env)
                self.assertEqual(code, 0, err)
                self.assertEqual((len(places(env)), len(cancels(env))), (1, 1))     # report, cancel, then no more
                self.assertEqual(len(self.session_stops(env)), 1)
                summary = json.loads(out)
                self.assertEqual(summary["stop_requested"], True)
                self.released(env)

    def test_a_deferred_signal_with_a_handoff_never_releases(self):
        env = self.one_market()
        env.transport.before_gate = lambda: sr.fire(signal.SIGHUP)
        env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("receive", "deadline_exceeded")]
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("own-scope kill", err)
        self.assertIn("unknown_unresolved", [s["reason"] for s in sr.stops(env)])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        self.assertEqual(json.loads(out)["signal"], "SIGHUP")

    def test_a_signalstop_between_the_submit_append_and_the_mask(self):
        env = self.one_market()
        original = OrderJournal.append

        def append(journal, kind, payload, **columns):
            event = original(journal, kind, payload, **columns)
            if kind == "submit" and payload.get("action") == "place":
                raise cli.SignalStop("SIGINT")
            return event

        with patch.object(OrderJournal, "append", append):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("own-scope kill", err)
        reasons = [s["reason"] for s in sr.stops(env)]
        self.assertIn("unknown_unresolved", reasons)
        self.assertNotIn("executor_exception", [n["code"] for n in sr.notes(env, "refusal")])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_a_signal_during_close_still_stops_the_loop(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        from betangel_research.betfair.gateway import OrderGateway
        original = OrderGateway.close
        fired = []

        def close(gateway):
            if not fired:
                fired.append(True)
                sr.fire(signal.SIGTERM)
            return original(gateway)

        with patch.object(OrderGateway, "close", close):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(places(env)), 1)
        self.assertEqual(json.loads(out)["signal"], "SIGTERM")

    def test_sighup_no_prompt_after_it(self):
        env = self.one_market()
        sr.closes_after_off(env, self.market)
        sr.add_market(env, off_ns=OFF + 3600 * S)
        env.exchange.hide_cancelled_unmatched = True
        env.transport.before_gate = lambda: sr.fire(signal.SIGHUP)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertFalse(env.confirmer.prompts[-1]["expected"].startswith("SETTLE"))
        self.assertEqual(json.loads(out)["signal"], "SIGHUP")

    def test_a_sigint_during_the_too_early_retry_sleep(self):
        env = self.one_market()
        original = pilot_run.Run.sleep_until
        calls = []

        def sleep_until(self, target_ns, *, outside_open=True):
            if not outside_open:
                calls.append(target_ns)
                if len(calls) == 1:
                    return True                                  # the open wakes early: pilot_reserve_too_early
                sr.fire(signal.SIGINT)
            return original(self, target_ns, outside_open=outside_open)

        with patch.object(pilot_run.Run, "sleep_until", sleep_until):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 2)
        self.assertEqual(places(env), [])
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.released(env)

    def test_a_flag_set_inside_the_factory_setup(self):
        """SIGINT during the gateway's _config (outer handler: the flag only), then a second SIGINT during the end
        path's session_end append: the factory's stop_requested raises before any login; the outer handler is back
        after the factory; the end is the orderly one."""
        env = self.one_market()
        original = cli._config
        armed, fired = [], []
        original_loop = pilot_run.Run.loop

        def loop(self):
            armed.append(True)
            return original_loop(self)

        def config(args, paths):
            if armed and not fired:
                fired.append(True)
                sr.fire(signal.SIGINT)
            return original(args, paths)

        original_owe = pilot_run.Run.owe
        handlers = []

        def owe(self, payload, **columns):
            if (payload.get("pilot") or {}).get("reason") == "session_end":
                handlers.append(signal.getsignal(signal.SIGINT))
                sr.fire(signal.SIGINT)
            return original_owe(self, payload, **columns)

        with patch.object(cli, "_config", config), patch.object(pilot_run.Run, "loop", loop), \
                patch.object(pilot_run.Run, "owe", owe):
            logins = len(sr.logins(env))
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(sr.notes(env, "reservation"), [])
        self.assertEqual(getattr(handlers[0], "__self__", None).__class__, pilot_run.StopFlag)
        self.assertEqual(len(self.session_stops(env)), 1)
        self.released(env)
        self.assertGreaterEqual(len(sr.logins(env)), logins)

    def test_the_handler_while_the_terminal_is_gone(self):
        """out and err raise EIO from the ARM line on: a SIGHUP in the scheduler wait still gives session_end, the
        orderly end and the release; nothing escapes the handler; no executor_exception or login_failed."""
        env = self.one_market()
        out, err = sr.BrokenStream(), sr.BrokenStream()
        confirmer = HookConfirmer({"PILOT ARM": lambda: (setattr(out, "broken", OSError(5, "EIO")),
                                                         setattr(err, "broken", OSError(5, "EIO")))})
        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 520 * S, lambda: sr.fire(signal.SIGHUP))
        code, _, _ = sr.arm_run(env, confirmer=confirmer, out=out, err=err)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.session_stops(env)), 1)
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.released(env)

    def test_d_a_sigint_during_the_case_4_wait_is_deferred(self):
        env = self.one_market()
        asked = []

        def kill():
            kill_event(env)
            asked.append(len(env.confirmer.prompts))
            sr.on_sleep(env, lambda clock: True, lambda: sr.fire(signal.SIGINT))

        with after_place_report(env, kill):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("own-scope kill", err)                              # polling went on to the handoff
        self.assertEqual(cancels(env), [])
        self.assertEqual(len(env.confirmer.prompts), asked[0])
        self.assertNotIn("session_end", [s["reason"] for s in sr.stops(env)])
        self.assertEqual(json.loads(out)["signal"], "SIGINT")
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_a_sigint_during_a_step_5_review_write_is_deferred(self):
        env = self.one_market()
        original = OrderJournal.append

        def append(journal, kind, payload, **columns):
            inner = payload.get("pilot") if isinstance(payload, dict) else None
            if isinstance(inner, dict) and inner.get("type") == "refusal" \
                    and inner.get("code") == "pilot_market_already_reserved":
                sr.fire(signal.SIGINT)
            return original(journal, kind, payload, **columns)

        def race():
            other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
            try:
                session_id = sr.fold(env).sessions[-1]["session_id"]
                pilot_run.append_pilot(other, pilot.pilot_note("reservation", env.contract.sha256,
                                                               session_id=session_id, open_id="other-open-1"),
                                       market_id=self.market.market_id)
            finally:
                other.close()

        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 310 * S, race)
        with patch.object(OrderJournal, "append", append):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["pilot_market_already_reserved"])
        self.assertEqual(json.loads(out)["signal"], "SIGINT")

    def test_b_sighup_during_a_read_session_login(self):
        env = self.one_market()
        out, err = sr.BrokenStream(), sr.BrokenStream()
        original = env.transport.request
        fired = []

        def request(method, url, headers, body, **kw):
            if (url.endswith("/api/certlogin") or url.endswith("/api/login")) and not fired:
                fired.append(True)
                out.broken = err.broken = OSError(5, "EIO")
                sr.fire(signal.SIGHUP)                                   # the outer handler: the flag only
            return original(method, url, headers, body, **kw)

        env.transport.request = request
        code, _, _ = sr.arm_run(env, out=out, err=err)
        self.assertEqual(code, 0)
        self.assertEqual(pilot_run.consecutive_failures(env.config, env.paths, clock=env.clock), 0)
        self.assertEqual(admission.ledger_problems(env.config, env.paths, clock=env.clock), [])
        self.assertEqual(places(env), [])
        self.released(env)

    def test_signalstop_inside_the_login_request(self):
        env = self.one_market()
        original = env.transport.request
        fired = []

        def request(method, url, headers, body, **kw):
            if url.endswith("/api/certlogin") or url.endswith("/api/login"):
                if not fired:
                    fired.append(True)
                    raise cli.SignalStop("SIGINT")
            return original(method, url, headers, body, **kw)

        env.transport.request = request
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn(pilot_run.LEDGER_LINE, err)
        self.assertIn(pilot_run.LOGIN_RESET_LINE, err)
        self.assertIn("login_attempt_unresolved", err)
        self.assertEqual(len(self.session_stops(env)), 1)                 # session_end only
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)


# --- §9.7 exceptions after a submit, §9.8 login, §9.10 admission intact ------------------------------------------


class ExceptionTests(RunCase):
    def abnormal(self, env, *, stops, refusals, handoff=True):
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        if handoff:
            self.assertIn("own-scope kill", err)
            lines = err.splitlines()
            first_handoff = next(i for i, line in enumerate(lines) if "own-scope kill" in line)
            self.assertNotIn("recording-release", "".join(lines[:first_handoff]))
        self.assertEqual(sorted(s["reason"] for s in sr.stops(env, "session")), sorted(stops))
        self.assertEqual(sorted(n["code"] for n in sr.notes(env, "refusal")), sorted(refusals))
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)     # no release
        self.assertIn("recording-release (generation 1)", err.strip().splitlines()[-1])   # the release line last
        return err

    def test_a_transport_runtime_error_after_the_request(self):
        env = self.one_market()
        env.router.next_response[gw.PLACE_SUFFIX] = [support.transport_failure("receive", "deadline_exceeded")]
        self.abnormal(env, stops=["unknown_unresolved"], refusals=[])
        self.assertEqual(len(places(env)), 1)
        self.assertEqual(cancels(env), [])

    def test_the_cancel_send_raising(self):
        env = self.one_market()

        def boom(record):
            raise RuntimeError("synthetic transport fault")

        env.router.next_response[gw.CANCEL_SUFFIX] = [boom]
        # The cancel was submitted (CANCEL_PENDING): §3.7 3(c) takes unknown_unresolved before remainder_cancel_failed.
        self.abnormal(env, stops=["unknown_unresolved"], refusals=["executor_exception"])
        self.assertEqual(len(cancels(env)), 1)

    def test_a_cancel_with_an_unknown_outcome(self):
        env = self.one_market()
        env.router.next_response[gw.CANCEL_SUFFIX] = [support.transport_failure("receive", "deadline_exceeded")]
        self.abnormal(env, stops=["remainder_left_executable"], refusals=[])

    def test_the_cancel_intent_append_raising(self):
        env = self.one_market()
        original = OrderJournal.append

        def append(journal, kind, payload, **columns):
            if kind == "cancel_intent":
                raise JournalError("journal_write_failed")
            return original(journal, kind, payload, **columns)

        with patch.object(OrderJournal, "append", append):
            self.abnormal(env, stops=["remainder_cancel_failed"], refusals=["executor_exception"])

    def test_an_auth_error_in_the_remainder_cancel(self):
        from betangel_research.betfair.gateway import OrderGateway
        env = self.one_market()
        with patch.object(OrderGateway, "cancel", side_effect=AuthError("login_failed")):
            self.abnormal(env, stops=["remainder_cancel_failed", "login_failed"], refusals=[])

    def test_a_login_budget_in_the_remainder_cancel(self):
        from betangel_research.betfair.gateway import OrderGateway
        env = self.one_market()
        with patch.object(OrderGateway, "cancel", side_effect=LoginBudgetExceeded("login_budget")):
            self.abnormal(env, stops=["remainder_cancel_failed"], refusals=["login_budget"])

    def test_the_journal_unreadable_in_the_handler(self):
        from betangel_research.betfair.gateway import OrderGateway
        env = self.one_market()
        original = pilot_run.pilot_events_after
        handling = []

        def events_after(path, since):
            if handling:
                return {"readable": False, "events": [], "kill_seq": None, "kill_incomplete_seq": None,
                        "place_submit": None}
            return original(path, since)

        original_after = pilot_run.Run.after_exception

        def after(self, exc, ctx, *, in_cancel=False):
            handling.append(True)
            try:
                return original_after(self, exc, ctx, in_cancel=in_cancel)
            finally:
                handling.clear()

        with patch.object(OrderGateway, "cancel", side_effect=RuntimeError("synthetic")), \
                patch.object(pilot_run, "pilot_events_after", events_after), \
                patch.object(pilot_run.Run, "after_exception", after):
            err = self.abnormal(env, stops=["unknown_unresolved"], refusals=["executor_exception"])
        self.assertIn("in " + self.market.market_id, err)                 # the case-1 handoff names the market

    def test_a_journal_error_after_an_execution_complete_report_then_next_day_authority(self):
        """_state() raising after an EXECUTION_COMPLETE place report: no handoff, an executor_exception review stop,
        no remainder_* stop; after Yu's recording-release the next-day ARM refuses until a caused ACK."""
        from betangel_research.betfair.gateway import OrderGateway
        env = self.one_market()
        sr.fills(env, complete=True)
        reported = []
        original_append, original_state = OrderJournal.append, OrderGateway._state

        def append(journal, kind, payload, **columns):
            event = original_append(journal, kind, payload, **columns)
            if kind == "report" and payload.get("action") == "place":
                reported.append(event["seq"])
            return event

        def state(gateway):
            if reported:
                raise JournalError("journal_unreadable")
            return original_state(gateway)

        with patch.object(OrderJournal, "append", append), patch.object(OrderGateway, "_state", state):
            self.abnormal(env, stops=[], refusals=["executor_exception"], handoff=False)
        self.assertTrue(reported)
        sr.typed_release(env, 1)
        self.next_day_arm(env)

    def test_a_raw_runtime_error_from_the_place_transport(self):
        env = self.one_market()

        def boom(record):
            raise RuntimeError("synthetic transport fault")

        env.router.next_response[gw.PLACE_SUFFIX] = [boom]
        self.abnormal(env, stops=["unknown_unresolved"], refusals=["executor_exception"])


class LoginTests(RunCase):
    def test_a_login_failure_ends_the_session(self):
        env = self.one_market()
        env.router.login_status = "FAIL"
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([s["reason"] for s in sr.stops(env, "session")], ["login_failed"])
        self.assertIn(pilot_run.LOGIN_RESET_LINE, err)
        self.assertEqual(len(sr.logins(env)), 1)
        self.assertEqual(places(env), [])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        sr.set_day(env, 1)
        self.refused(sr.arm_run(env), "pilot_arm_login_ledger")             # one unreset failure refuses ARM

    def test_the_budget_pre_check_skips_the_open(self):
        env = self.one_market()
        blocked = []
        original = pilot_run.cap_blocks

        def cap_blocks(config, paths, *, clock):
            if clock.time_ns() >= OFF - 460 * S:
                blocked.append(clock.time_ns())
                return True
            return original(config, paths, clock=clock)

        with patch.object(pilot_run, "cap_blocks", cap_blocks):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertTrue(blocked)
        self.assertEqual((sr.notes(env, "reservation"), places(env), sr.notes(env, "refusal")), ([], [], []))
        self.assertEqual(len(sr.logins(env)), 1)                           # the scheduler's read only
        self.released(env)

    def test_a_thrown_login_budget_at_the_open(self):
        env = self.one_market()
        from betangel_research.betfair import auth
        original = auth.SessionManager.login
        calls = []

        def login(session, *a, **kw):
            calls.append(True)
            if len(calls) == 2:
                raise LoginBudgetExceeded("login_budget")
            return original(session, *a, **kw)

        with patch.object(auth.SessionManager, "login", login):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["login_budget"])
        self.assertNotIn("login_failed", [s["reason"] for s in sr.stops(env)])
        self.assertEqual(places(env), [])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        sr.typed_release(env, 1)
        self.next_day_arm(env)


class AdmissionIntactTests(RunCase):
    def test_each_failure_ends_placing(self):
        cases = {
            "recorder running": (lambda env: sr.old_recorder(env), "recorder_running"),
            "admission deleted": (lambda env: env.paths.admission_file.unlink(), "admission_lost"),
            "admission unreadable": (lambda env: env.paths.admission_file.write_text("{"), "admission_lost"),
        }
        for name, (change, code) in cases.items():
            with self.subTest(case=name):
                env = self.one_market()
                seen = {}

                def act(env=env, change=change):
                    seen["logins"] = len(sr.logins(env))
                    change(env)

                sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 520 * S, act)
                code_, out, err = sr.arm_run(env)
                self.assertEqual(code_, 5, err)
                self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], [code])
                self.assertEqual(len(sr.logins(env)), seen["logins"])
                self.assertEqual(places(env), [])
                self.assertIn("generation 1", err)


# --- §9.9 yield to kill, §9.15 remainder and KILL cases ---------------------------------------------------------


def after_place_report(env, action):
    """Run action() right after the place report is journaled (before the remainder cancel)."""
    original = OrderJournal.append
    done = []

    def append(journal, kind, payload, **columns):
        event = original(journal, kind, payload, **columns)
        if kind == "report" and payload.get("action") == "place" and not done:
            done.append(True)
            action()
        return event

    return patch.object(OrderJournal, "append", append)


def kill_event(env, *, incomplete=False):
    """Another process's kill journals its kill event (and, when incomplete, its HALT kill_incomplete)."""
    other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
    try:
        other.append("kill", {"reason": "kill", "scope": "own"})
        other.append("halt", {"reason": "kill", "scope": "global"})
        if incomplete:
            other.append("halt", {"reason": "kill_incomplete", "scope": "global"})
    finally:
        other.close()


class KillCaseTests(RunCase):
    def test_case_2_a_file_only_kill_after_a_partial_fill_cancels_at_once(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        sr.fills(env, matched=1.0)
        with after_place_report(env, lambda: env.paths.kill_file.write_text("")):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual((len(places(env)), len(cancels(env))), (1, 1))
        self.assertNotIn("own-scope kill", err)
        self.assertIn(pilot_run.KILL_CAPTURE_LINE, err)
        self.assertEqual(json.loads(out)["abnormal"], "kill")

    def test_case_3_a_kill_incomplete_cancels(self):
        env = self.one_market()
        with after_place_report(env, lambda: kill_event(env, incomplete=True)):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(len(cancels(env)), 1)
        self.assertEqual(json.loads(out)["abnormal"], "kill")

    def test_case_4_a_kill_with_no_outcome_hands_off_after_30_s(self):
        env = self.one_market()
        seen = {}

        def kill():
            kill_event(env)
            seen["requests"] = len(env.transport.requests)
            seen["mono"] = env.clock.monotonic_ns()

        with after_place_report(env, kill):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(cancels(env), [])
        self.assertEqual(not_logout(env, seen["requests"]), [])                # no Betfair read for the 30 s
        self.assertIn("own-scope kill", err)
        self.assertIn("remainder_left_executable", [s["reason"] for s in sr.stops(env)])
        self.assertGreaterEqual(env.clock.monotonic_ns() - seen["mono"], pilot_run.CASE4_WAIT_NS)
        self.assertFalse(any(p["expected"].startswith("SETTLE") for p in env.confirmer.prompts))

    def test_case_4_timer_drift(self):
        for label, step in (("wall back 60 s", -60), ("wall forward", 5)):
            with self.subTest(case=label):
                env = self.one_market()
                marks = {}

                def kill(env=env, marks=marks, step=step):
                    kill_event(env)
                    marks["mono"] = env.clock.monotonic_ns()
                    original = env.clock.sleep
                    stepped = []

                    def sleep(seconds):
                        if not stepped:
                            stepped.append(True)
                            env.clock.step_wall(step)
                        return original(seconds)

                    env.clock.sleep = sleep

                with after_place_report(env, kill):
                    code, out, err = sr.arm_run(env)
                self.assertEqual(code, 5, err)
                self.assertEqual(cancels(env), [])
                waited = env.clock.monotonic_ns() - marks["mono"]
                self.assertLess(waited, 5 * S)                       # drift over 2 s hands off at once
                self.assertIn("own-scope kill", err)

    def test_case_1_an_unreadable_journal_hands_off(self):
        env = self.one_market()
        original = pilot_run.Run.kill_case
        with patch.object(pilot_run.Run, "kill_case", lambda self, seq: "unreadable"):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(cancels(env), [])
        self.assertIn("own-scope kill", err)
        self.assertIsNotNone(original)

    def test_unknown_under_a_file_only_kill_hands_off(self):
        env = self.one_market()

        def sent_then_kill(record):
            env.paths.kill_file.write_text("")              # the KILL lands while the request is in flight
            return support.transport_failure("receive", "deadline_exceeded")

        env.router.next_response[gw.PLACE_SUFFIX] = [sent_then_kill]
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("own-scope kill", err)
        self.assertEqual(cancels(env), [])
        self.assertIn("unknown_unresolved", [s["reason"] for s in sr.stops(env)])

    def test_a_failed_remainder_cancel(self):
        env = self.one_market()
        env.exchange.cancel_handler = lambda params: {
            "customerRef": params["customerRef"], "status": "FAILURE", "errorCode": "BET_ACTION_ERROR",
            "marketId": params["marketId"], "instructionReports": [
                {"status": "FAILURE", "errorCode": "ERROR_IN_ORDER", "instruction": params["instructions"][0]}]}
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("own-scope kill", err)
        reasons = [s["reason"] for s in sr.stops(env, "session")]
        self.assertTrue(set(reasons) & {"remainder_cancel_failed", "remainder_left_executable"}, reasons)

    def test_a_kill_between_opens_names_the_pending_reads(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        seen = {}

        def engage():
            env.paths.kill_file.write_text("")
            seen["requests"] = len(env.transport.requests)

        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 200 * S and cancels(env), engage)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(len(places(env)), 1)
        self.assertEqual(not_logout(env, seen["requests"]), [])
        self.assertIn(pilot_run.KILL_CAPTURE_LINE, err)
        self.assertIn("pending captures for bets", err)

    def test_the_kill_path_never_takes_the_admission_or_owner_lock(self):
        """Static pin: the kill command path never names the owner lock or admission.lock."""
        source = (sp.REPO / "betangel_research/betfair/killswitch.py").read_text()
        for word in ("live_owner_lock", "admission_lock", "live_ownership", "admission."):
            self.assertNotIn(word, source)


# --- §9.13 scheduler, §9.14 loop ----------------------------------------------------------------------------------


class SchedulerTests(RunCase):
    def planner(self, env, *, now_ns, end_ns):
        from types import SimpleNamespace
        return SimpleNamespace(session={"session_end_ns": end_ns}, config=env.config, contract=env.contract,
                               now=lambda: now_ns)

    def test_end_margin_boundary_reserved_market_and_lead(self):
        env = self.registered()
        state = sr.fold(env)
        end = OFF + 10 * 3600 * S
        planner = self.planner(env, now_ns=OFF - 600 * S, end_ns=end)
        for delta, planned in ((-61, True), (-60, False), (-59, False)):
            with self.subTest(off_minus_end_s=delta):
                found = pilot_run.Run.plan(planner, state, [("1.9", end + delta * S)])
                self.assertEqual(found is not None, planned)
        reserved = dataclasses.replace(state, reservations={"1.9": {"session_id": "x"}})
        self.assertIsNone(pilot_run.Run.plan(planner, reserved, [("1.9", OFF)]))
        late = self.planner(env, now_ns=OFF - 300 * S - pilot_run.OPEN_LEAD_NS + 1, end_ns=end)
        self.assertIsNone(pilot_run.Run.plan(late, state, [("1.9", OFF)]))      # its planned open has passed

    def test_the_open_starts_at_the_lead(self):
        env = self.one_market()
        logins = []
        original = env.transport.request

        def request(method, url, headers, body, **kw):
            if url.endswith("/api/certlogin") or url.endswith("/api/login"):
                logins.append(env.clock.time_ns())
            return original(method, url, headers, body, **kw)

        env.transport.request = request
        self.assertEqual(sr.arm_run(env)[0], 0)
        self.assertEqual(logins[1], OFF - 300 * S - pilot_run.OPEN_LEAD_NS)    # the placement open's login

    def test_the_day_limit_ends_the_session(self):
        env = self.registered()
        for i in range(6):
            sr.add_market(env, off_ns=OFF + i * 900 * S)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        summary = json.loads(out)
        self.assertEqual((summary["end_code"], summary["place_records"]), ("day_limit", 5))   # PT4: 5
        self.assertEqual(len(places(env)), 5)

    def test_a_catalogue_read_waits_for_the_budget(self):
        env = self.one_market()
        state = {"until": OFF - 590 * S}
        original = pilot_run.cap_blocks

        def cap_blocks(config, paths, *, clock):
            if clock.time_ns() < state["until"]:
                return True
            return original(config, paths, clock=clock)

        with patch.object(pilot_run, "cap_blocks", cap_blocks):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(places(env)), 1)
        state["until"] = OFF                               # a wait past the planned open: that market is skipped
        env2 = self.one_market()
        with patch.object(pilot_run, "cap_blocks", cap_blocks):
            code, out, err = sr.arm_run(env2)
        self.assertEqual(code, 0, err)
        self.assertEqual(places(env2), [])

    def test_a_thrown_login_budget_from_the_scheduler_read(self):
        env = self.one_market()
        from betangel_research.betfair import auth
        with patch.object(auth.SessionManager, "login", side_effect=LoginBudgetExceeded("login_budget")):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["login_budget"])
        self.assertNotIn("login_failed", [s["reason"] for s in sr.stops(env)])


class LoopTests(RunCase):
    def test_a_second_too_early_in_one_open_is_a_review_end(self):
        env = self.one_market()
        original = pilot_run.Run.sleep_until
        with patch.object(pilot_run.Run, "sleep_until",
                          lambda self, t, outside_open=True: True if not outside_open
                          else original(self, t, outside_open=outside_open)):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["pilot_reserve_too_early"])
        self.assertEqual(places(env), [])
        sr.typed_release(env, 1)
        self.next_day_arm(env)

    def test_a_raced_reservation_is_a_review_end(self):
        """pilot_market_already_reserved, forced past the scheduler's pre-check by a race (another writer reserves the
        market while the open waits for off - 300 s): one review refusal note, no second pilot_place, no release, and
        the next-day ARM refuses until a caused ACK."""
        env = self.one_market()
        calls = []
        from betangel_research.betfair.gateway import OrderGateway
        original_place = OrderGateway.pilot_place

        def pilot_place(gateway, *a, **kw):
            calls.append(True)
            return original_place(gateway, *a, **kw)

        def race():
            other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
            try:
                session_id = sr.fold(env).sessions[-1]["session_id"]
                pilot_run.append_pilot(other, pilot.pilot_note("reservation", env.contract.sha256,
                                                               session_id=session_id, open_id="other-open-1"),
                                       market_id=self.market.market_id)
            finally:
                other.close()

        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 310 * S, race)
        with patch.object(OrderGateway, "pilot_place", pilot_place):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["pilot_market_already_reserved"])
        self.assertEqual((len(calls), places(env)), (1, []))
        sr.typed_release(env, 1)
        self.next_day_arm(env)

    def test_one_reservation_per_open_and_close_on_every_path(self):
        from betangel_research.betfair.gateway import OrderGateway
        env = self.registered()
        sr.add_market(env, off_ns=OFF)
        sr.add_market(env, off_ns=OFF + 1800 * S)
        opened, closed = [], []
        original_open, original_close = OrderGateway.open, OrderGateway.close

        def open_(gateway, *a, **kw):
            opened.append(gateway)
            return original_open(gateway, *a, **kw)

        def close(gateway):
            closed.append(gateway)
            return original_close(gateway)

        with patch.object(OrderGateway, "open", open_), patch.object(OrderGateway, "close", close):
            self.assertEqual(sr.arm_run(env)[0], 0)
        self.assertEqual(len(sr.notes(env, "reservation")), 2)
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(g in closed for g in opened))

    def test_a_static_kill_at_the_open(self):
        env = self.one_market()
        original = cli._config
        armed = []
        original_loop = pilot_run.Run.loop

        def loop(self):
            armed.append(True)
            return original_loop(self)

        def config(args, paths):
            if armed and args.command == "pilot-run" and not paths.kill_file.exists() \
                    and env.clock.time_ns() >= OFF - 460 * S:
                paths.kill_file.write_text("")
            return original(args, paths)

        with patch.object(cli, "_config", config), patch.object(pilot_run.Run, "loop", loop):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        session = [e["seq"] for e in sr.events(env) if (e["payload"].get("pilot") or {}).get("type")
                   == "session_start"][0]
        self.assertEqual([s["stop_id"] for s in sr.stops(env, "session")],
                         [pilot.session_stop_id("kill_file", session)])
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual(places(env), [])

    def test_an_identity_drift_between_opens_is_one_refusal_note(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        sr.on_sleep(env, lambda clock: cancels(env) and clock.time_ns() >= OFF - 200 * S,
                    lambda: setattr(env.git, "dirty", True))
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(len(places(env)), 1)
        self.assertEqual(len(sr.notes(env, "refusal")), 1)
        self.assertEqual(len(sr.active_review(env)), 1)

    def test_a_failed_pre_entry_head_read_opens_nothing(self):
        env = self.one_market()
        from betangel_research.betfair.gateway import OrderGateway
        original = pilot_run.Run.read
        with patch.object(pilot_run.Run, "read", lambda self, since=0: {"readable": False, "events": [],
                                                                         "kill_seq": None,
                                                                         "kill_incomplete_seq": None,
                                                                         "place_submit": None}
                          if since == 0 and self.now() >= OFF - 460 * S else original(self, since)), \
                patch.object(OrderGateway, "open", side_effect=AssertionError("opened")):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(places(env), [])
        self.assertEqual(json.loads(out)["abnormal"], "journal_unreadable")

    def test_a17_with_a_halt_writes_nothing(self):
        """A17 (d): a foreign order the open's reconcile HALTs on: armed False with A17, and nothing written by
        pilot-run (the HALT carries it)."""
        env = self.one_market()

        def foreign():
            env.exchange.add_order(market_id=self.market.market_id, ref=None, strategy_ref="someone-else")

        sr.on_sleep(env, lambda clock: clock.time_ns() >= OFF - 520 * S, foreign)
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("unresolved_or_foreign_orders", err)
        self.assertEqual(places(env), [])
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertIn(("halt", None), self.kinds(env))


# --- §9.2 FAILED outcomes end to end ----------------------------------------------------------------------------


class FailedOutcomeTests(RunCase):
    def test_each_failed_class(self):
        cases = {
            "instruction RUNNER_REMOVED": (lambda env: setattr(env.exchange, "place_handler",
                                                               failure_report("RUNNER_REMOVED")), "RUNNER_REMOVED"),
            "execution code, no instructions": (lambda env: setattr(env.exchange, "place_handler", lambda params: {
                "customerRef": params["customerRef"], "status": "FAILURE", "errorCode": "ERROR_IN_MATCHER",
                "marketId": params["marketId"], "instructionReports": []}), "ERROR_IN_MATCHER"),
            "rejected, session": (lambda env: env.router.next_response.__setitem__(
                gw.PLACE_SUFFIX, [aping_error("INVALID_SESSION_INFORMATION")]), "INVALID_SESSION_INFORMATION"),
            "rejected, rate limited": (lambda env: env.router.next_response.__setitem__(
                gw.PLACE_SUFFIX, [aping_error("TOO_MANY_REQUESTS")]), "TOO_MANY_REQUESTS"),
        }
        for name, (script, code) in cases.items():
            with self.subTest(case=name):
                env = self.one_market()
                sr.add_market(env, off_ns=OFF + 1800 * S)
                script(env)
                exit_code, out, err = sr.arm_run(env)
                self.assertEqual(exit_code, 5, err)
                review = [s for s in sr.stops(env, "review")]
                self.assertEqual(len(review), 1, review)
                self.assertEqual(review[0]["reason"], pilot.refusal_code(code))
                self.assertEqual(len(places(env)), 1)
                self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
                if sr.fold(env) and pilot_run.recovery_projection(sr.events(env), contract=env.contract,
                                                                  now_ns=env.clock.time_ns())["handoffs"]:
                    continue                          # an unresolved order: reconcile comes before any ARM
                sr.typed_release(env, 1)
                self.next_day_arm(env)

    def test_insufficient_funds_halts_and_adds_no_stop(self):
        env = self.one_market()
        env.exchange.place_handler = failure_report("INSUFFICIENT_FUNDS")
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertEqual(sr.stops(env, "review"), [])


# --- §9.20 settle points, §9.21 in-session capture ---------------------------------------------------------------


class SettleTests(RunCase):
    def two_markets(self, gap_s=3600):
        env = self.one_market()
        self.second = sr.add_market(env, off_ns=OFF + gap_s * S)
        sr.settling(env, self.market)
        return env

    def settles(self, env):
        return [e for e in sr.events(env) if e["kind"] == "settle"]

    def test_a_settle_point_after_c_and_the_closed_book(self):
        env = self.two_markets()
        code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual([p["expected"] for p in env.confirmer.prompts if p["expected"].startswith("SETTLE")],
                         [f"SETTLE MARKET {self.market.market_id}"])
        kinds = [(e["kind"], (e["payload"].get("pilot") or {}).get("type")) for e in sr.events(env)]
        self.assertLess(kinds.index(("note", "capture_c")), kinds.index(("note", "capture_book")))
        self.assertLess(kinds.index(("note", "capture_book")), kinds.index(("settle", None)))
        self.assertEqual(json.loads(out)["settles"], 1)
        self.released(env)

    def offers(self, *, orders, closed=True, now_ns=0, open_at=10 ** 15):
        """Run.settle_points on a stand-in run: the markets it would offer."""
        from types import SimpleNamespace
        records = [SimpleNamespace(ref=ref) for ref in orders]
        projection = {"state": SimpleNamespace(place_records=records),
                      "core": {"orders": {ref: dict(o, market_id="1.5") for ref, o in orders.items()}}}
        events = [{"kind": "note", "market_id": "1.5", "payload": {"pilot": {"type": "capture_book",
                                                                             "status": "CLOSED"}}}] if closed else []
        offered = []
        run = SimpleNamespace(fold=lambda: projection, read=lambda since=0: {"events": events}, offered=set(),
                              settle_orders_clear=pilot_run.Run.settle_orders_clear,
                              settle_allowed=lambda at: open_at - now_ns > 330 * S,
                              settle_one=lambda market, at: offered.append(market))
        pilot_run.Run.settle_points(run, open_at)
        return offered

    def test_offer_conditions(self):
        done = {"state": "EXECUTION_COMPLETE", "size_remaining_p": 0}
        self.assertEqual(self.offers(orders={"a": done}), ["1.5"])
        self.assertEqual(self.offers(orders={"a": done}, closed=False), [])
        self.assertEqual(self.offers(orders={"a": done, "b": {"state": "EXECUTABLE", "size_remaining_p": 100}}), [])
        self.assertEqual(self.offers(orders={"a": done, "b": {"state": "UNKNOWN", "size_remaining_p": 0}}), [])
        self.assertEqual(self.offers(orders={"a": done}, now_ns=0, open_at=330 * S), [])   # a decision within 3 min

    def test_rechecked_after_the_typed_line(self):
        env = self.two_markets()
        confirmer = HookConfirmer({"SETTLE MARKET": lambda: env.paths.kill_file.write_text("")})
        code, out, err = sr.arm_run(env, confirmer=confirmer)
        self.assertEqual(code, 5, err)
        self.assertEqual(self.settles(env), [])
        self.assertEqual(json.loads(out)["abnormal"], "kill")

    def test_not_closed_is_skipped_and_offered_again(self):
        from betangel_research.betfair.errors import OrderRefused
        from betangel_research.betfair.gateway import OrderGateway
        env = self.two_markets(gap_s=4200)
        original = OrderGateway.settle_market
        calls = []

        def settle_market(gateway, market_id, **kw):
            calls.append(len(sr.events(env)))
            if len(calls) == 1:
                raise OrderRefused("settle_market_not_closed")
            return original(gateway, market_id, **kw)

        with patch.object(OrderGateway, "settle_market", settle_market):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(calls), 2)
        self.assertIn("settle_market_not_closed", err)
        self.assertEqual(len(self.settles(env)), 1)

    def test_settle_open_failures(self):
        from betangel_research.betfair.gateway import OrderGateway
        cases = {
            "static NotArmed (A13)": ("anchor", None, "not_armed_before_lock"),
            "RuntimeError in settle_market": ("runtime", None, "executor_exception"),
            "LockBusy at the open": ("lock", "lock_busy", None),
        }
        for name, (how, stop, refusal) in cases.items():
            with self.subTest(case=name):
                env = self.two_markets()
                original = pilot_run.Run.settle_one
                held = []

                def settle_one(run, market_id, open_at, env=env, how=how, held=held):
                    if how == "anchor":                          # A13 refuses the settle open's static arming
                        env.runner.items.pop((ANCHOR_SERVICE, "default"), None)
                    elif how == "lock":
                        lock = pilot_run.file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock)
                        lock.__enter__()
                        held.append(lock)
                    try:
                        return original(run, market_id, open_at)
                    finally:
                        for lock in held:
                            lock.__exit__(None, None, None)
                        held.clear()

                patches = [patch.object(pilot_run.Run, "settle_one", settle_one)]
                if how == "runtime":
                    patches.append(patch.object(OrderGateway, "settle_market", side_effect=RuntimeError("synthetic")))
                for item in patches:
                    item.start()
                try:
                    code, out, err = sr.arm_run(env)
                finally:
                    for item in patches:
                        item.stop()
                self.assertEqual(code, 5, err)
                self.assertEqual(self.settles(env), [])
                if stop:
                    self.assertIn(stop, [s["reason"] for s in sr.stops(env, "session")])
                if refusal:
                    self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], [refusal])
                self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def standalone_settle(self, env):
        return sr.run_cli(env, "settle-market", "--market", self.market.market_id, "--confirm-live", sr.token(env))

    def test_a_second_process_settle(self):
        """During an armed session (here: pilot-run died before UNTIL) the cli refuses with the advice; after the
        session's end with pilot-run exited, before UNTIL, the standalone settle is allowed (built with pilot=None)."""
        env = self.one_market()
        sr.settling(env, self.market)
        with sr.sigkill_after("report", when=lambda p: p.get("action") == "cancel"), self.assertRaises(sr.Death):
            sr.arm_run(env)
        env.clock.advance(600)                                       # the market is off and closed
        err = self.refused(self.standalone_settle(env), "pilot_session_armed")
        self.assertIn(cli.PILOT_SETTLE_ADVICE, err)
        self.assertEqual(sr.end_session(env)[0], 0)
        built = []
        original = cli._live_gateway

        def spy(*args, **kw):
            built.append({k: kw.get(k) for k in ("pilot", "in_session_settle", "clock", "credentials")})
            return original(*args, **kw)

        with patch.object(cli, "_live_gateway", spy):
            code, out, err = self.standalone_settle(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(built, [{"pilot": None, "in_session_settle": False, "clock": None, "credentials": None}])
        self.assertEqual(json.loads(out)["status"], "settled")

    def test_refused_while_the_owner_lock_is_held_in_the_end_path(self):
        env = self.one_market()
        sr.settling(env, self.market)
        self.assertEqual(sr.arm_run(env)[0], 0)
        env.clock.advance(600)
        admission.atomic_write_json(env.paths.admission_file, {
            "schema_version": 1, "generation": 2, "owner": "pilot-session:s20270104-0a0b0c0d", "purpose": "x",
            "created_at": env.clock.utc_now(), "state": "admitted"})
        with pilot_run.file_lock(env.paths.live_owner_lock, timeout_s=0, clock=env.clock):
            err = self.refused(self.standalone_settle(env), "pilot_session_armed")
        self.assertIn(cli.PILOT_SETTLE_ADVICE, err)

    def test_an_unreadable_contract_before_until_is_refused(self):
        env = self.one_market()
        self.assertEqual(sr.arm_run(env)[0], 0)
        env.state.contract_raw = OSError("unreadable")
        self.refused(self.standalone_settle(env), "pilot_session_armed")


class CaptureTests(RunCase):
    def test_raw_bytes_and_matching_notes(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        self.assertEqual(sr.arm_run(env)[0], 0)
        raw = list((env.paths.pilot_dir / "raw").iterdir())
        self.assertTrue(raw)
        for path in raw:
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(path.name, pilot_run.bytes_sha256(path.read_bytes()))
        (c,) = sr.notes(env, "capture_c")[:1]
        self.assertEqual(c["row"]["status"], "EXECUTION_COMPLETE")

    def capturing(self):
        """A flag that is set while a capture C read (ReadClient.raw_current_order) runs."""
        from betangel_research.betfair.api import ReadClient
        flag = []
        original = ReadClient.raw_current_order

        def raw(client, bet_id, **kw):
            flag.append(True)
            try:
                return original(client, bet_id, **kw)
            finally:
                flag.clear()

        patcher = patch.object(ReadClient, "raw_current_order", raw)
        patcher.start()
        self.addCleanup(patcher.stop)
        return flag

    def test_a_row_that_differs_writes_no_note(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        flag = self.capturing()
        original = env.exchange.current
        env.exchange.current = lambda **kw: [dict(r, selectionId=99) for r in original(**kw)] if flag else \
            original(**kw)
        self.assertEqual(sr.arm_run(env)[0], 0)
        self.assertEqual(sr.notes(env, "capture_c"), [])

    def test_a_failed_c_read_retries_in_the_next_gap(self):
        from betangel_research.betfair.errors import TransportError
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 3600 * S)
        flag = self.capturing()
        failures = []
        original = env.exchange.current

        def current(**kw):
            if flag and not failures:
                failures.append(env.clock.time_ns())
                raise TransportError("connection_lost", phase="receive", may_have_arrived=True)
            return original(**kw)

        env.exchange.current = current
        self.assertEqual(sr.arm_run(env)[0], 0)
        self.assertEqual(len(failures), 1)
        (first,) = [n for n in sr.notes(env, "capture_c")][:1]
        c_seqs = [e["seq"] for e in sr.events(env) if (e["payload"].get("pilot") or {}).get("type") == "capture_c"]
        self.assertTrue(c_seqs)
        self.assertEqual(first["row"]["status"], "EXECUTION_COMPLETE")

    def test_no_capture_read_inside_a_placement_open(self):
        from betangel_research.betfair.api import ReadClient
        from betangel_research.betfair.gateway import OrderGateway
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        inside, reads = [], []
        original_open, original_close = OrderGateway.open, OrderGateway.close
        original_raw = ReadClient.raw_current_order

        def open_(gateway, *a, **kw):
            inside.append(True)
            return original_open(gateway, *a, **kw)

        def close(gateway):
            inside.clear()
            return original_close(gateway)

        def raw(client, bet_id, **kw):
            reads.append(bool(inside))
            return original_raw(client, bet_id, **kw)

        with patch.object(OrderGateway, "open", open_), patch.object(OrderGateway, "close", close), \
                patch.object(ReadClient, "raw_current_order", raw):
            self.assertEqual(sr.arm_run(env)[0], 0)
        self.assertTrue(reads)
        self.assertEqual(set(reads), {False})

    def test_a_capture_write_lock_timeout_is_lock_busy(self):
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        original = pilot_run.capture

        def capture(env_, entries, **kw):
            lock = pilot_run.file_lock(env.paths.gateway_lock, timeout_s=0, clock=env.clock)
            lock.__enter__()
            try:
                return original(env_, entries, **kw)
            finally:
                lock.__exit__(None, None, None)

        with patch.object(pilot_run, "capture", capture):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertIn("lock_busy", [s["reason"] for s in sr.stops(env, "session")])


# --- §9.22 pilot-capture, §9.23 profile --------------------------------------------------------------------------


class PilotCaptureTests(RunCase):
    def after_a_clean_session(self):
        env = self.one_market()
        sr.closes_after_off(env, self.market)
        self.assertEqual(sr.arm_run(env)[0], 0)
        env.clock.advance(3600)
        return env

    def test_no_tty_and_a_wrong_token(self):
        env = self.after_a_clean_session()
        before, requests = self.head(env), len(env.transport.requests)
        with patch.object(admission, "live_ownership", side_effect=AssertionError("ownership taken")):
            result = sr.capture(env, confirmer=gw.FakeConfirmer(present=False))
        self.refused(result, "operator_tty_required") if "refused" in result[2] else \
            self.assertIn("operator_tty_required", result[2])
        self.assertEqual((self.head(env), len(env.transport.requests)), (before, requests))
        self.assertEqual(admission.owner_lock_state(env.paths), "free")
        code, out, err = sr.run_cli(env, "pilot-capture", "--mode", "live", "--confirm-live", "live-000000")
        self.assertEqual(code, 2)
        self.assertIn("confirm_token", err)

    def test_the_clean_end_case(self):
        env = self.after_a_clean_session()
        signalled = sr.hold_recorder(env)
        reads = len(sr.credential_reads(env))
        code, out, err = sr.capture(env)
        self.assertEqual(code, 0, err)
        self.assertEqual(signalled, [4242])
        self.assertIn("generation 2 (fresh)", err)
        self.assertIn("recording released (generation 2)", err)
        self.nothing_admitted(env)
        self.assertGreater(len(sr.notes(env, "capture_book")), 0)
        self.assertNotIn("betangel-research.betfair.app-key.live", sr.credential_reads(env)[reads:])
        ticket = admission.recorder_admission(env.paths, invocation_id="rec-2", clock=env.clock,
                                              processes=FakeProcesses())
        ticket.__enter__()                                               # a recorder can start after it
        ticket.__exit__(None, None, None)

    def test_the_retained_owner_case(self):
        env = self.one_market()
        with patch.object(pilot_run.Run, "loop", side_effect=RuntimeError("synthetic")):
            self.assertEqual(sr.arm_run(env)[0], 5)
        env.clock.advance(11 * 3600)
        err = self.refused(sr.capture(env), "admission_held")
        self.assertIn("--continue-admission 1", err)
        before = sr.recording_files(env)
        code, out, err = sr.capture(env, "--continue-admission", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("(continuation)", err)
        self.assertEqual(sr.recording_files(env), before)

    def test_refused_while_a_session_is_armed(self):
        env = self.one_market()
        sr.hold_recorder(env)
        with sr.sigkill_after("report", when=lambda p: p.get("action") == "cancel"), self.assertRaises(sr.Death):
            sr.arm_run(env)
        before = sr.recording_files(env)
        with patch.object(admission, "admit", side_effect=AssertionError("admit called")):
            self.refused(sr.capture(env, "--continue-admission", "1"), "pilot_session_armed")
        self.assertEqual(sr.recording_files(env), before)
        self.assertEqual(admission.owner_lock_state(env.paths), "free")

    def test_kill_in_progress_and_after_a_finished_kill(self):
        env = self.after_a_clean_session()
        kill_event(env)
        env.paths.kill_file.write_text("")
        sr.hold_recorder(env)
        before, reads = sr.recording_files(env), len(sr.credential_reads(env))
        with patch.object(admission, "admit", side_effect=AssertionError("admit called")):
            err = self.refused(sr.capture(env), "kill_in_progress")
        self.assertIn("at least 90 s after it began", err)
        self.assertEqual((sr.recording_files(env), len(sr.credential_reads(env))), (before, reads))
        self.assertTrue(admission.recorder_lock_held(env.paths, clock=env.clock))
        env.clock.advance(120)
        os.utime(env.paths.kill_file, ns=(env.clock.time_ns() - 120 * S, env.clock.time_ns() - 120 * S))
        code, out, err = sr.capture(env)
        result = json.loads(out)
        self.assertEqual(result["status"], "captured")
        self.assertGreater(result["written"], 0)

    def test_a_kill_mid_capture_skips_the_rest(self):
        for how in ("event", "file"):
            with self.subTest(kill=how):
                env = self.one_market()
                sr.closes_after_off(env, self.market)
                env.exchange.hide_cancelled_unmatched = True          # C pending into pilot-capture
                self.assertEqual(sr.arm_run(env)[0], 0)
                env.clock.advance(3600)
                original = env.exchange.current

                def current(env=env, how=how, **kw):
                    rows = original(**kw)
                    if kw.get("bet_ids"):
                        kill_event(env) if how == "event" else env.paths.kill_file.write_text("")
                    return rows

                env.exchange.current = current
                code, out, err = sr.capture(env)
                result = json.loads(out)
                self.assertEqual(result["stopped"], "skipped_kill")
                self.assertEqual(code, 5)

    def test_login_failure_then_continuation_and_login_reset(self):
        from betangel_research.betfair.auth import append_login_reset
        env = self.one_market()
        env.router.login_status = "FAIL"
        self.assertEqual(sr.arm_run(env)[0], 5)
        env.router.login_status = "SUCCESS"
        env.clock.advance(11 * 3600)
        before, reads, logins = sr.recording_files(env), len(sr.credential_reads(env)), len(sr.logins(env))
        self.refused(sr.capture(env, "--continue-admission", "1"), "login_ledger_not_clean")
        self.assertEqual((sr.recording_files(env), len(sr.credential_reads(env)), len(sr.logins(env))),
                         (before, reads, logins))
        append_login_reset(env.paths, clock=env.clock)                   # Yu's typed login-reset
        code, out, err = sr.capture(env, "--continue-admission", "1")
        self.assertEqual(code, 0, err)
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_a_preset_inhibit_is_never_released(self):
        env = self.after_a_clean_session()
        env.paths.inhibit_file.write_text("")
        code, out, err = sr.capture(env)
        self.assertIn("INHIBIT predates this capture", err)
        self.assertNotIn("recording released", err)
        self.assertTrue(sr.admission_view(env)["inhibit"])

    def test_an_unreadable_journal_at_start_refuses(self):
        env = self.after_a_clean_session()
        with patch.object(pilot_run, "pilot_events_after", return_value={
                "readable": False, "events": [], "kill_seq": None, "kill_incomplete_seq": None,
                "place_submit": None}):
            code, out, err = sr.capture(env)
        self.assertEqual(code, 2, err)
        self.nothing_admitted(env)


class ProfileTests(RunCase):
    def test_other_profile_refuses_register_arm_and_capture(self):
        env = self.build(profile="other")
        for argv in (("pilot-register", "--mode", "live", "--confirm-live", "x"),
                     ("pilot-run", "--mode", "live", "--confirm-live", "x", "--until", "2000"),
                     ("pilot-capture", "--mode", "live", "--confirm-live", "x")):
            with self.subTest(command=argv[0]):
                with patch.object(admission, "admit", side_effect=AssertionError("admit called")):
                    self.refused(sr.run_cli(env, "--profile", "other", *argv), "pilot_profile_not_recorder_profile")
                self.assertEqual(env.runner.calls, [])
                self.assertEqual(env.transport.requests, [])
                self.assertFalse(env.paths.admission_file.exists())
                self.assertFalse(env.paths.inhibit_file.exists())

    def test_recorder_profile_pin(self):
        """RECORDER_PROFILE is the profile the recorder runs in when none is named: the CLI's --profile default and
        the integration guide's Keychain account default (docs/betfair-integration.md:109)."""
        self.assertEqual(pilot_run.RECORDER_PROFILE, cli.build_parser().parse_args(["record"]).profile)
        guide = (sp.REPO / "docs/betfair-integration.md").read_text()
        self.assertIn(f"（預設 `{pilot_run.RECORDER_PROFILE}`）", guide)

    def test_pilot_commands_are_live_only(self):
        env = self.registered()
        for name in ("pilot-register", "pilot-run", "pilot-ack", "pilot-gap-resolved", "pilot-capture",
                     "pilot-end-session"):
            with self.subTest(command=name):
                extra = {"pilot-run": ("--until", "2000"), "pilot-ack": ("--stop-id", "x"),
                         "pilot-gap-resolved": ("--stop-id", "x", "--cause-file", "x")}.get(name, ())
                before = self.head(env)
                self.refused(sr.run_cli(env, name, *extra), "pilot_live_only")
                self.assertEqual(self.head(env), before)


# --- §9.24 clock regression between opens --------------------------------------------------------------------------


class ClockRegressionTests(RunCase):
    def test_b_between_two_opens(self):
        """The wall steps back 300 s after the first open's events, right as the second open starts (inside the
        factory, before the gateway's static arming: the scheduler's own waits are on the wall clock, so a step back
        earlier is waited out): static A7 refuses, pilot-run notes the review occurrence, nothing is released, and the
        next-day ARM refuses until a caused ACK."""
        env = self.one_market()
        sr.add_market(env, off_ns=OFF + 1800 * S)
        original = cli._config
        stepped = []

        def config(args, paths):
            if args.command == "pilot-run" and len(places(env)) == 1 and not stepped:
                stepped.append(True)
                env.clock.step_wall(-300)
            return original(args, paths)

        with patch.object(cli, "_config", config):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 5, err)
        self.assertTrue(stepped)
        self.assertEqual(len(places(env)), 1)
        self.assertEqual([n["code"] for n in sr.notes(env, "refusal")], ["not_armed_before_lock"])
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)
        env.clock.step_wall(300)
        sr.typed_release(env, 1)
        self.next_day_arm(env)


# --- boundaries and pins ----------------------------------------------------------------------------------------


class BoundaryTests(unittest.TestCase):
    def test_pilot_run_never_imports_the_gateway_and_admission_words_stay_out_of_the_order_path(self):
        import ast
        source = (sp.REPO / "betangel_research/betfair/pilot_run.py").read_text()
        tree = ast.parse(source)
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                    for alias in node.names} | {node.module for node in ast.walk(tree)
                                                if isinstance(node, ast.ImportFrom) and node.module}
        self.assertNotIn("gateway", imported)
        self.assertNotIn("OrderGateway", source)
        cli_source = (sp.REPO / "betangel_research/betfair/cli.py").read_text()
        cli_tree = ast.parse(cli_source)
        kill_helpers = [node for node in cli_tree.body if isinstance(node, ast.FunctionDef)
                        and (node.name == "cmd_kill" or node.name.startswith("_kill"))]
        self.assertTrue(kill_helpers)
        texts = {"gateway.py": (sp.REPO / "betangel_research/betfair/gateway.py").read_text(),
                 "killswitch.py": (sp.REPO / "betangel_research/betfair/killswitch.py").read_text()}
        texts.update({f"cli.{node.name}": ast.get_source_segment(cli_source, node) for node in kill_helpers})
        for name, text in texts.items():
            with self.subTest(source=name):
                for word in ("admission", "live_owner_lock", "live_ownership", "release_own", "OwnerTicket"):
                    self.assertNotIn(word, text)

    def test_help_pins(self):
        from betangel_research.betfair.cli import HELP
        self.assertIn("by typing REGISTER PILOT", HELP["pilot-register"])
        self.assertIn("after Yu types PILOT ARM; --until HHMM is required", HELP["pilot-run"])
        self.assertIn("never unattended", HELP["pilot-run"])
        self.assertIn("no typed line, login, order or release", HELP["pilot-end-session"])
        self.assertIn("by typing PILOT ACK", HELP["pilot-ack"])
        self.assertIn("never acknowledges", HELP["pilot-gap-resolved"])
        self.assertIn("--continue-admission N", HELP["pilot-capture"])
        for name in ("pilot-register", "pilot-run", "pilot-end-session", "pilot-ack", "pilot-gap-resolved",
                     "pilot-capture"):
            self.assertTrue(HELP[name].startswith("Live only"), name)

    def test_non_pilot_wiring_is_unchanged(self):
        """Without P1-08's options, _gateway hands _live_gateway today's values: no clock, credentials, pilot or read
        guard, in_session_settle False. place, cancel, settle-market, kill, live-probe and reconcile call it without
        any of them (resume does not call _gateway). The behaviour itself is pinned against 2d3b092 by
        test_betfair_pilot_gateway.LegacyPinTests.test_non_pilot_commands_unchanged_from_base (NP-3)."""
        import inspect
        signature = inspect.signature(cli._gateway)
        for name, default in (("clock", None), ("credentials", None), ("pilot", None), ("in_session_settle", False),
                              ("stop_requested", None), ("read_guard", None)):
            self.assertEqual(signature.parameters[name].default, default)
        source = inspect.getsource(cli)
        for command in ("cmd_place", "cmd_cancel", "cmd_settle_market", "cmd_kill", "cmd_live_probe",
                        "cmd_reconcile"):
            body = inspect.getsource(getattr(cli, command))
            for word in ("pilot=", "in_session_settle=", "credentials=", "stop_requested=", "read_guard="):
                self.assertNotIn(word, body, command)
        self.assertIn("def _pilot_factory", source)

    def test_in_session_settle_is_set_only_by_the_settle_point(self):
        """§4.13: in_session_settle=True appears once in betangel_research/, at pilot_run's settle-point factory
        call; in cli.py the keyword appears only inside _pilot_factory, _gateway and _live_gateway."""
        import ast
        package = sp.REPO / "betangel_research"
        trues, cli_sites = [], []
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text())
            parents = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "in_session_settle":
                    owner = parents.get(node)
                    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        owner = parents.get(owner)
                    name = owner.name if owner is not None else None
                    if isinstance(node.value, ast.Constant) and node.value.value is True:
                        trues.append((path.name, name))
                    if path.name == "cli.py":
                        cli_sites.append(name)
        self.assertEqual(trues, [("pilot_run.py", "settle_one")])
        self.assertTrue(cli_sites)
        self.assertLessEqual(set(cli_sites), {"_pilot_factory", "factory", "_gateway", "_live_gateway"})


if __name__ == "__main__":
    unittest.main()
