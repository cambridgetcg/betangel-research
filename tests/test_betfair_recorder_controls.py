"""Recorder controls of the recording design (v4/v5 code list): C1 SIGHUP, C4 the pre-count snapshot, C6
--require-delayed-key, C7 --invocation-id and C8 --end-at. Offline, against the scripted fake stream and the
fake Betfair router; synthetic data only."""

import contextlib
import json
import os
from pathlib import Path
import random
import signal
import tempfile
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission as admission_module
from betangel_research.betfair import recorder as recorder_module
from betangel_research.betfair.cli import main
from betangel_research.betfair.errors import ConfigError
from betangel_research.betfair.paths import StatePaths
from betangel_research.betfair.receipts import file_sha256, parse_utc_ns, strict_json_loads, utc_from_ns
from betangel_research.betfair.recorder import PRECOUNT_NAME, PRECOUNT_ROW_KEYS, StreamRecorder, verify_recording

import test_betfair_support as support
import test_betfair_support_gateway as gw
import test_betfair_support_stream as fx

HOUR_NS = 3600 * 10**9


def image_at(offset_s, **kwargs):
    return fx.change(lambda sid: fx.image(sid, fx.pt_at(offset_s), **kwargs))


def heartbeat_at(offset_s, nth=-1):
    return fx.change(lambda sid: fx.heartbeat(sid, fx.pt_at(offset_s)), nth=nth)


def after(item, action):
    """A script item that runs action() once its wrapped item is produced (when the client reads it)."""
    def build(sock):
        produced = item(sock) if callable(item) else item
        if produced is not None:
            action()
        return produced
    return build


def read_jsonl(path):
    return [json.loads(text) for text in Path(path).read_text().splitlines()]


def run_main(argv, deps=None):
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, deps=deps)
    return code, out.getvalue(), err.getvalue()


class RecorderControlTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)
        self.paths = support.temp_state_paths(self)
        self.clock = support.FakeClock()
        self.session = fx.FakeSession()
        self.events = []

    def recorder(self, connections, *, overrides=None, lister=None, session=None, seed=7, **kwargs):
        self.server = fx.FakeStreamServer(connections, self.clock)
        session = session or self.session
        self.lister = lister or fx.lister()
        return StreamRecorder(fx.recorder_config(self.paths, overrides), paths=self.paths, session=session,
                              stream_factory=lambda: fx.make_client(self.server, session, self.clock),
                              market_lister=self.lister, clock=self.clock, rng=random.Random(seed),
                              emit=self.events.append, **kwargs)

    def run_recorder(self, connections, *, stop=None, run_kwargs=None, **kwargs):
        recorder = self.recorder(connections, **kwargs)
        receipt = recorder.run(stop_requested=stop or self.server.exhausted, **(run_kwargs or {}))
        self.dir = self.paths.recordings_dir / receipt["session_id"]
        return receipt

    # --- C4: the pre-count snapshot ----------------------------------------------------------------

    def test_precount_snapshot_per_segment_whitelisted(self):
        pairs = [("1.1", "2020-01-01T12:00:00Z"), ("1.2", None), ("1.3", "not-a-time"), ("1.1", "2020-01-02T00:00:00Z"),
                 ["1.4", "2020-01-01T13:30:00.5+01:00"]]
        calls = []

        def lister(rest_filter):
            calls.append(rest_filter)
            return list(pairs)
        beats = []
        for i in range(14):
            beats += [fx.wait(5), heartbeat_at(-299 + 5 * i, nth=0)]
        script = (fx.handshake() + [image_at(-300)] + beats
                  + [fx.reply("marketSubscription", nth=1), fx.change(lambda sid: fx.image(sid, fx.pt_at(-199)), nth=1)])
        receipt = self.run_recorder([script], lister=lister, overrides={"recorder": {"refresh_s": 60}})
        rows = read_jsonl(self.dir / PRECOUNT_NAME)
        self.assertEqual([row["segment"] for row in rows], [0, 1])
        for row in rows:
            self.assertEqual(set(row), PRECOUNT_ROW_KEYS)
            self.assertEqual(row["markets"], [
                {"market_id": "1.1", "market_start_time": "2020-01-01T12:00:00.000Z"},
                {"market_id": "1.2", "market_start_time": None}, {"market_id": "1.3", "market_start_time": None},
                {"market_id": "1.4", "market_start_time": "2020-01-01T12:30:00.500Z"}])
            self.assertEqual((row["count"], row["possibly_more"], row["lookback_s"], row["subscription_mode"],
                              row["requested_count"]), (4, False, 1800, "coarse", 4))
        self.assertEqual(rows[0]["taken_at"], support.BASE_UTC)
        self.assertEqual(receipt["precount"], {"path": PRECOUNT_NAME, "rows": 2,
                                               "sha256": file_sha256(self.dir / PRECOUNT_NAME)})
        self.assertEqual(oct((self.dir / PRECOUNT_NAME).stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(verify_recording(self.dir)["status"], "verified")
        # Plain ids (the earlier lister shape) still work: no start times are invented.
        self.clock.advance(5)
        receipt = self.run_recorder([fx.handshake() + [image_at(-300)]], seed=8)
        self.assertEqual(read_jsonl(self.dir / PRECOUNT_NAME)[0]["markets"],
                         [{"market_id": support.MARKET_ID, "market_start_time": None}])
        # A session that never subscribed has no snapshot.
        self.clock.advance(5)
        receipt = self.run_recorder([fx.handshake()], lister=lambda f: (_ for _ in ()).throw(ValueError("x")), seed=9)
        self.assertEqual((receipt["precount"], (self.dir / PRECOUNT_NAME).exists()),
                         ({"path": PRECOUNT_NAME, "rows": 0, "sha256": None}, False))

    def test_precount_rejects_malformed_items(self):
        for bad in ([("1.1",)], [("1.1", None, "x")], [("2.x", None)], [{"marketId": "1.1"}]):
            with self.subTest(bad=bad):
                self.clock.advance(2)
                receipt = self.run_recorder([fx.handshake()], lister=lambda f, bad=bad: bad,
                                            seed=10 + len(self.events))
                self.assertEqual(receipt["stop_reason"], "market_lister_failed:ValueError")

    # --- C6: --require-delayed-key -----------------------------------------------------------------

    def test_require_delayed_key_refuses_a_live_slot(self):
        live = {"stream": {"app_key_slot": "live"}, "recorder": {"allow_live_key": True}}
        with self.assertRaisesRegex(ConfigError, "recorder_delayed_key_required"):
            self.recorder([fx.handshake() + [image_at(-300)]], overrides=live, session=fx.FakeSession(slot="live"),
                          require_delayed_key=True)
        self.assertEqual((self.server.calls, self.lister.calls), ([], []))
        self.assertFalse(any(p.name.startswith("rec-") for p in self.paths.recordings_dir.iterdir()))
        # Without the flag the same live configuration is what allow_live_key permits; with it, delayed runs.
        self.recorder([], overrides=live, session=fx.FakeSession(slot="live"))
        receipt = self.run_recorder([fx.handshake() + [image_at(-300)]], require_delayed_key=True)
        self.assertEqual(receipt["key_type"], "delayed")
        with self.assertRaises(TypeError):
            self.recorder([], require_delayed_key="yes")

    # --- C7: --invocation-id -----------------------------------------------------------------------

    def test_invocation_id_in_pid_file_receipts_and_ledger(self):
        seen = {}

        def emit(event):
            if event.get("event") == "segment_closed" and not seen:
                directory = next(p for p in self.paths.recordings_dir.iterdir() if p.name.startswith("rec-"))
                seen["partial"] = strict_json_loads((directory / "receipt.partial.json").read_text())
                seen["pid"] = json.loads(self.paths.recorder_pid_file.read_text())
        self.events = []
        recorder = self.recorder([fx.handshake() + [image_at(-300)]], invocation_id="wrapper-20201101-1")
        recorder.emit = emit
        receipt = recorder.run(stop_requested=self.server.exhausted)
        self.dir = self.paths.recordings_dir / receipt["session_id"]
        self.assertEqual(seen["partial"]["invocation_id"], "wrapper-20201101-1")
        self.assertIs(seen["partial"]["complete"], False)
        self.assertEqual(seen["pid"]["invocation_id"], "wrapper-20201101-1")
        self.assertEqual(receipt["invocation_id"], "wrapper-20201101-1")
        self.assertEqual(strict_json_loads((self.dir / "receipt.json").read_text())["invocation_id"], "wrapper-20201101-1")
        (row,) = read_jsonl(self.paths.recordings_dir / "run-ledger.jsonl")
        self.assertEqual(row["invocation_id"], "wrapper-20201101-1")
        self.assertEqual(row["receipt_sha256"], file_sha256(self.dir / "receipt.json"))
        # Without one, the receipt and the ledger row keep their earlier shape.
        self.clock.advance(5)
        receipt = self.run_recorder([fx.handshake() + [image_at(-300)]], seed=12)
        self.assertNotIn("invocation_id", receipt)
        self.assertNotIn("invocation_id", read_jsonl(self.paths.recordings_dir / "run-ledger.jsonl")[-1])
        for bad in ("two words", "", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.recorder([], invocation_id=bad)

    def test_ticket_must_match_the_recorder(self):
        recorder = self.recorder([fx.handshake() + [image_at(-300)]], invocation_id="a-1")
        with admission_module.recorder_admission(self.paths, invocation_id="b-2", clock=self.clock) as ticket:
            with self.assertRaisesRegex(ConfigError, "recorder_admission_not_held"):
                recorder.run(stop_requested=self.server.exhausted, admission=ticket)
        with admission_module.recorder_admission(self.paths, invocation_id="a-1", clock=self.clock) as ticket:
            pass
        with self.assertRaisesRegex(ConfigError, "recorder_admission_not_held"):     # released: no longer held
            recorder.run(stop_requested=self.server.exhausted, admission=ticket)

    # --- C8: --end-at --------------------------------------------------------------------------------

    def test_end_at_stops_after_a_wall_clock_jump(self):
        """The Air's monotonic clock stops during sleep, so a 16 h --max-seconds cannot bound the day: a wall-clock
        jump of two hours (the Mac asleep) with almost no monotonic time passing must still end the run."""
        end_at = self.clock.time_ns() + HOUR_NS
        mono_start = self.clock.mono_ns
        script = fx.handshake() + [image_at(-300), fx.wait(5), heartbeat_at(-295),
                                   after(heartbeat_at(-290), lambda: self.clock.step_wall(2 * 3600)),
                                   heartbeat_at(-285), heartbeat_at(-280)]
        receipt = self.run_recorder([script], stop=lambda: False, run_kwargs={"end_at_ns": end_at,
                                                                              "max_seconds": 16 * 3600})
        self.assertEqual(receipt["stop_reason"], "end_at_reached")
        self.assertEqual(receipt["segments"][0]["close_reason"], "end_at_reached")
        self.assertEqual(receipt["segments"][0]["lines"], 3)
        self.assertLess(self.clock.mono_ns - mono_start, 60 * 10**9)
        self.assertEqual(len(self.lister.calls), 1)
        self.assertTrue(receipt["complete"])
        self.assertIn("logout", self.session.calls)

    def test_end_at_checked_before_relogin_login_and_precount(self):
        end_at = self.clock.time_ns() + HOUR_NS
        jump = lambda: self.clock.step_wall(2 * 3600)                                           # noqa: E731
        # A session error after the end instant: no relogin.
        no_session = after(fx.reply("authentication", status_code="FAILURE", errorCode="NO_SESSION",
                                    connectionClosed=True), jump)
        receipt = self.run_recorder([[fx.connection_line(), no_session], fx.handshake()], stop=lambda: False,
                                    run_kwargs={"end_at_ns": end_at})
        self.assertEqual(receipt["stop_reason"], "end_at_reached")
        self.assertNotIn("invalidate_and_relogin", self.session.calls)
        self.assertEqual(len(self.server.calls), 1)
        # The end instant passing while connecting: authenticate (which may log in) is never called.
        self.clock.advance(5)
        session = fx.FakeSession()
        end_at = self.clock.time_ns() + HOUR_NS
        receipt = self.run_recorder([[after(fx.connection_line(), jump), fx.auth_ok()]], session=session,
                                    stop=lambda: False, run_kwargs={"end_at_ns": end_at}, seed=21)
        self.assertEqual(receipt["stop_reason"], "end_at_reached")
        self.assertNotIn("token", session.calls)
        # A pre-count after the end instant is never asked for (the session-level guard).
        self.clock.advance(5)
        recorder = self.recorder([], seed=22)
        root = self.paths.recordings_dir
        state = recorder_module._Session(recorder, root, lambda: False, None, self.clock.time_ns() + 10)
        self.clock.step_wall(1)
        self.assertEqual(state._precount(), (None, "end_at_reached"))
        self.assertEqual(self.lister.calls, [])

    def test_non_positive_window_refused(self):
        recorder = self.recorder([fx.handshake()])
        for end_at in (self.clock.time_ns(), self.clock.time_ns() - 1, float(self.clock.time_ns() + HOUR_NS)):
            with self.subTest(end_at=end_at), self.assertRaises(ValueError):
                recorder.run(stop_requested=self.server.exhausted, end_at_ns=end_at)
        self.assertEqual((self.server.calls, self.lister.calls), ([], []))


class RecordCliControlTests(unittest.TestCase):
    """record's flags and signals through the real CLI, session, JSON-RPC and stream clients over fakes."""

    def setUp(self):
        support.block_network(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / "home"
        self.home.mkdir()
        self.clock = support.FakeClock()
        self.exchange = gw.SimExchange(self.clock)
        self.router = gw.FakeBetfair(self.exchange)
        self.transport = gw.RoutingTransport(self.router)
        self.keychain = support.FakeKeychainRunner()
        self.deps = {"home": self.home, "keychain_runner": self.keychain, "clock": self.clock,
                     "http_transport": self.transport}
        self.paths = StatePaths(home=self.home, env=dict(support.FORBID_ENV))
        support.write_test_config(self.paths, {"credentials": {"login_method": "interactive", "cert_path": None,
                                                               "key_path": None}})

    def safe_signals(self, *signums):
        def safety(signum, frame):
            pass
        for signum in signums:
            self.addCleanup(signal.signal, signum, signal.signal(signum, safety))
        return safety

    def record(self, script, *extra):
        server = fx.FakeStreamServer([script], self.clock)
        deps = dict(self.deps, stream_socket_factory=server.factory)
        return run_main(["record", "--max-seconds", "600", "--bet-angel-paused", *extra], deps)

    def lines(self, out):
        return [json.loads(line) for line in out.splitlines()]

    def test_sighup_stops_record_cleanly(self):
        """C1: a closed terminal (SIGHUP) stops record like SIGTERM: segment closed, receipt written, logout,
        exit 0, and the handler in place before record ran is restored."""
        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=signum.name):
                safety = self.safe_signals(signum)
                script = fx.handshake() + [image_at(-300), after(heartbeat_at(-295),
                                                                 lambda s=signum: os.kill(os.getpid(), s)),
                                           fx.wait(30), heartbeat_at(-265)]
                code, out, err = self.record(script)
                self.assertEqual(code, 0, err)
                summary = self.lines(out)[-1]["summary"]
                self.assertEqual((summary["stop_reason"], summary["complete"]), ("stop_requested", True))
                self.assertIs(signal.getsignal(signum), safety)
                self.clock.advance(5)

    def test_signal_before_any_credential_read_records_nothing(self):
        real = admission_module.recorder_admission

        @contextlib.contextmanager
        def signalled(*args, **kwargs):
            with real(*args, **kwargs) as ticket:
                os.kill(os.getpid(), signal.SIGTERM)
                yield ticket
        self.safe_signals(signal.SIGTERM)
        with patch.object(admission_module, "recorder_admission", signalled):
            code, out, err = self.record(fx.handshake())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.lines(out)[-1]["summary"]["status"], "not_started")
        self.assertEqual(self.keychain.calls, [])
        self.assertEqual(list(self.paths.recordings_dir.glob("rec-*")), [])

    def test_record_flags_end_to_end(self):
        end_at = utc_from_ns(self.clock.time_ns() + HOUR_NS)
        script = fx.handshake() + [image_at(-300)]
        code, out, err = self.record(script, "--invocation-id", "cli-7", "--end-at", end_at, "--require-delayed-key")
        self.assertEqual(code, 0, err)
        summary = self.lines(out)[-1]["summary"]
        self.assertEqual(summary["invocation_id"], "cli-7")
        (directory,) = self.paths.recordings_dir.glob("rec-*")
        receipt = strict_json_loads((directory / "receipt.json").read_text())
        self.assertEqual((receipt["invocation_id"], receipt["key_type"]), ("cli-7", "delayed"))
        self.assertEqual(read_jsonl(self.paths.recordings_dir / "run-ledger.jsonl")[0]["invocation_id"], "cli-7")
        # The pre-count went through ReadClient.precount_markets: catalogue start times are kept (C4).
        markets = read_jsonl(directory / PRECOUNT_NAME)[0]["markets"]
        self.assertTrue(markets and all(parse_utc_ns(m["market_start_time"]) for m in markets))
        self.assertFalse(self.paths.recorder_pid_file.exists())

    def test_record_flag_refusals_before_any_credential_read(self):
        cases = [(["--end-at", "2020-01-01T12:00:00"], "end_at_invalid"),
                 (["--end-at", "2019-12-31T23:00:00Z"], "end_at_not_after_now"),
                 (["--end-at", support.BASE_UTC], "end_at_not_after_now"),
                 (["--invocation-id", "two words"], "invocation_id_invalid")]
        for extra, code_text in cases:
            with self.subTest(extra=extra):
                code, out, err = self.record(fx.handshake(), *extra)
                self.assertEqual(code, 2)
                self.assertIn(code_text, err)
        support.write_test_config(self.paths, {"credentials": {"login_method": "interactive", "cert_path": None,
                                                               "key_path": None},
                                               "stream": {"app_key_slot": "live"}, "recorder": {"allow_live_key": True}})
        code, out, err = self.record(fx.handshake(), "--require-delayed-key")
        self.assertEqual((code, err.strip()), (2, "Betfair input error: recorder_delayed_key_required; "
                                                  "stream.app_key_slot is not delayed"))
        self.assertEqual(self.keychain.calls, [])
        self.assertEqual(list(self.paths.recordings_dir.glob("rec-*")) if self.paths.recordings_dir.exists() else [], [])

    def test_record_refused_while_inhibited(self):
        from betangel_research.betfair.config import load_config
        config = load_config(self.paths.config_path, paths=self.paths)
        admitted = admission_module.admit(self.paths, owner="pilot-session:x", purpose="test", config=config,
                                          clock=self.clock)
        code, out, err = self.record(fx.handshake() + [image_at(-300)], "--invocation-id", "inh-1")
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "Betfair recording refused: inhibited")
        refused = self.lines(out)[-1]["refused"]
        self.assertEqual(refused, {"outcome": "inhibited", "detail": None, "owner": "pilot-session:x",
                                   "generation": admitted["generation"], "invocation_id": "inh-1"})
        self.assertEqual(self.keychain.calls, [])
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(list(self.paths.recordings_dir.glob("rec-*")), [])


if __name__ == "__main__":
    unittest.main()
