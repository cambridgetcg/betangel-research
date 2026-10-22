"""Recording rollout §3.4.6 pilot_run.py fixes (design v9 §8 manual ownership: message text and the before-release
block), offline: the reconcile and settle-market lines of the "before recording-release" block carry
--continue-admission N only when N is the ticket's or a successful read shows ADMISSION N; a kept armed-session file
puts the pilot-end-session line first; ARM 11a names the real N; owner_live replaces pilot_session_running and
pilot_session_alive. Temporary state roots and the P1-08 harness (test_betfair_support_run) only.
"""

import unittest
from unittest.mock import patch

from betangel_research.betfair import admission, pilot_run

import test_betfair_support as support
import test_betfair_support_run as sr
from test_betfair_pilot_run import RunCase

HANDOFFS = [{"ref": "bro-unknown", "bet_id": None, "selection_id": 11, "market_id": "1.500", "state": "UNKNOWN"},
            {"ref": "bro-absent", "bet_id": None, "selection_id": 12, "market_id": "1.501",
             "state": "ABSENT_UNCONFIRMED"},
            {"ref": "bro-rest", "bet_id": "300000000009", "selection_id": 13, "market_id": "1.502",
             "state": "EXECUTABLE"}]


def write_admission(paths, generation=3, state="admitted"):
    admission.atomic_write_json(paths.admission_generation_file, {
        "schema_version": 1, "generation": generation, "updated_at": "2020-01-01T12:00:00.000000Z"})
    admission.atomic_write_json(paths.admission_file, {
        "schema_version": 1, "generation": generation, "owner": "pilot-session:s20200101-0a0b0c0d",
        "purpose": "RS-19 pilot synthetic", "created_at": "2020-01-01T12:00:00.000000Z", "state": state})
    paths.inhibit_file.write_text("")


class BeforeReleaseBlockTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock()

    def lines(self, **kw):
        return pilot_run.before_release_lines(self.paths, handoffs=HANDOFFS, flag=" --bet-angel-paused",
                                              clock=self.clock, **kw)

    def test_no_ticket_admission_readable_gives_the_flag(self):
        write_admission(self.paths)
        lines = self.lines()
        self.assertIn("then run reconcile --mode live --bet-angel-paused --continue-admission 3", lines)
        self.assertIn("then run reconcile --mode live --bet-angel-paused --continue-admission 3; if the Betfair website "
                      "and Bet Angel show no such bet, run reconcile --mode live --bet-angel-paused "
                      "--continue-admission 3 --accept bro-absent --confirm-live TOKEN", lines)
        self.assertIn("it stays EXECUTABLE until you settle market 1.502 with settle-market --bet-angel-paused "
                      "--continue-admission 3 after it closes", lines)
        self.assertEqual(lines[-1], "recording stays off until Yu's typed recording-release (generation 3)")

    def test_no_ticket_admission_absent_gives_the_fresh_command(self):
        self.paths.inhibit_file.write_text("")
        lines = self.lines()
        self.assertIn("then run reconcile --mode live --bet-angel-paused", lines)
        self.assertFalse([line for line in lines if "--continue-admission" in line])
        self.assertEqual(lines[-1], "recording stays off until Yu's typed recording-release in its INHIBIT-only form")

    def test_no_ticket_read_failing_prints_unknown_and_no_flag(self):
        write_admission(self.paths)
        self.paths.admission_file.write_text("garbage")
        lines = self.lines()
        self.assertFalse([line for line in lines if "--continue-admission" in line])
        self.assertEqual(lines[-1], "recording state unknown; run status before any release")

    def test_ticket_generation_wins_even_when_the_read_fails(self):
        self.paths.admission_file.write_text("garbage")
        lines = self.lines(generation=4)
        self.assertIn("then run reconcile --mode live --bet-angel-paused --continue-admission 4", lines)
        self.assertEqual(lines[-1], "recording stays off until Yu's typed recording-release (generation 4)")

    def test_kept_armed_file_puts_pilot_end_session_first(self):
        write_admission(self.paths, generation=1)
        lines = pilot_run.before_release_lines(self.paths, handoffs=[HANDOFFS[2]], generation=1, armed_file_kept=True,
                                               clock=self.clock)
        self.assertEqual(lines[0], pilot_run.handoff_text(HANDOFFS[2]))
        self.assertEqual(lines[1], "run pilot-end-session --mode live first; it prints this block again")
        self.assertTrue(lines[2].startswith("it stays EXECUTABLE until you settle market 1.502 with settle-market "
                                            "--continue-admission 1"))
        self.assertEqual(lines[-1], "recording stays off until Yu's typed recording-release (generation 1)")
        self.assertFalse([line for line in lines if line.startswith("run pilot-end-session, then")])

    def test_recorded_line_carries_the_flag(self):
        self.assertIn("{cont}", pilot_run.RECORDED_LINE)
        result = {"released": False, "outcome": "release_refused", "problems": ["order_recorded"], "owner": "manual:1",
                  "generation": 5}
        self.assertIn("then run reconcile --mode live --continue-admission 5; its open refuses the order recorded "
                      "before its submit (crash_before_submit)", pilot_run.release_lines(result))


class MessageTextTests(RunCase):
    def test_arm_11a_names_the_real_generation(self):
        """ARM 11a's handoff branch, reached with a projection whose handoffs read empty at 7a and present at 11a.
        (In a real fold 7a refuses pilot_order_unresolved first, so the branch is reached only this way; the literal
        N is substituted all the same.)"""
        env = self.registered()
        write_admission(env.paths, generation=6)

        class Flip(list):
            seen = 0

            def __bool__(self):
                Flip.seen += 1
                return Flip.seen > 1
        original = pilot_run._view

        def view(env_, now_ns):
            found = original(env_, now_ns)
            found["projection"] = dict(found["projection"], handoffs=Flip([{"x": 1}]))
            return found
        with patch.object(pilot_run, "_view", view):
            code, out, err = sr.arm_run(env)
        self.assertEqual(code, 2)
        self.assertIn("next: reconcile --continue-admission 6 first", err)
        self.assertNotIn("continue-admission N", err)

    def test_owner_live_replaces_pilot_session_running_and_alive(self):
        env = self.registered()
        with pilot_run.file_lock(env.paths.live_owner_lock, timeout_s=0, clock=env.clock):
            for run in (sr.arm_run, sr.end_session):
                code, out, err = run(env)
                with self.subTest(command=run.__name__):
                    self.assertEqual(code, 2)
                    self.assertIn("Betfair order refused: owner_live", err)
                    self.assertIn("owner_live: another live owner holds the owner lock", err)
                    self.assertNotIn("pilot_session_running", err)
                    self.assertNotIn("pilot_session_alive", err)

    def test_owner_live_names_a_readable_admission_owner(self):
        env = self.registered()
        write_admission(env.paths, generation=2)
        admission.atomic_write_json(env.paths.admission_file, dict(
            admission.admission_status(env.paths)["admission"], owner="manual:77"))
        with pilot_run.file_lock(env.paths.live_owner_lock, timeout_s=0, clock=env.clock):
            code, out, err = sr.end_session(env)
        self.assertIn("owner_live: another live owner holds the owner lock (ADMISSION owner manual:77, generation 2, "
                      "state admitted)", err)


class RecordingReleasePilotTests(RunCase):
    """recording-release's read-only pilot checks (design v9 §3.5 step 3): the armed-session file, and the lock-free
    fold check, each refuse before any prompt."""

    def died_armed(self):
        env = self.one_market()
        with sr.sigkill_after("report", when=lambda p: p.get("action") == "cancel"), self.assertRaises(sr.Death):
            sr.arm_run(env)
        return env

    def release(self, env):
        return sr.run_cli(env, "recording-release", "--confirm-live", sr.token(env))

    def test_an_armed_session_in_the_fold_refuses(self):
        env = self.died_armed()
        sr.armed_file(env).unlink()                       # only the fold says armed now
        prompts = len(env.confirmer.prompts)
        code, out, err = self.release(env)
        self.assertEqual(code, 2)
        self.assertIn("release_refused (pilot_session_armed)", err)
        self.assertEqual(len(env.confirmer.prompts), prompts)          # refused before the prompt
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_the_armed_session_file_refuses_too(self):
        env = self.died_armed()
        code, out, err = self.release(env)
        self.assertEqual(code, 2)
        self.assertIn("pilot_session_armed", err)
        self.assertEqual(sr.admission_view(env)["admission"]["generation"], 1)

    def test_after_pilot_end_session_the_generation_form_releases(self):
        env = self.one_market()
        self.assertEqual(sr.arm_run(env)[0], 0)          # a clean end released its own generation
        self.assertIsNone(sr.admission_view(env)["admission"])
        code, out, err = self.release(env)
        self.assertEqual(code, 0, err)
        self.assertIn('"status": "nothing_to_release"', out)


if __name__ == "__main__":
    unittest.main()
