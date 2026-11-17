"""pilot-summary (s1, step 1): a read-only count of the registered pilot's live journal, offline and synthetic.

Driven through cli.main on the test_betfair_support_run harness only (build_run, run_cli, register, arm_run): a
temporary state root whose password-database home lookup is patched (unoverridden_paths), a fake clock and sleep, fake
Keychain, transport, git and process table, and a fake confirmer that stands in for Yu (nothing here types into a
terminal). Isolation (rule 17 of the s1 build brief): every CLI call goes through a helper whose first statement is
guard(env), which asserts that env.paths.root lies inside this process's temporary directory and nowhere near the real
state root, deciding that before any realpath of the given root; there is no subprocess of the package, no CLI call at
import, in a class or module setup, or after cleanup, and no direct call of a cli command function, cli.COMMANDS,
cli._paths or StatePaths (IsolationTests checks this file by AST). They show what the command reads, prints and leaves
behind on a synthetic journal; they do not show a real journal, a real terminal, SQLite's behaviour on another build,
or Betfair.
"""

import ast
import builtins
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from betangel_research.betfair import admission, auth, cli, credentials, pilot, pilot_gate, pilot_run, pilot_summary
from betangel_research.betfair import paths as paths_module
from betangel_research.betfair.journal import OrderJournal
from betangel_research.betfair.receipts import utc_ms_from_ns

import test_betfair_support_gateway as gw
import test_betfair_support_pilot as sp
import test_betfair_support_run as sr
from test_betfair_pilot_gateway import contract_bytes

S = sr.S
OFF = sr.OFF
DAY = sr.DAY_S * S
HERE = Path(__file__).resolve()
MODULE = sp.REPO / "betangel_research/betfair/pilot_summary.py"
CLI = sp.REPO / "betangel_research/betfair/cli.py"
# The output's fixed keys, pinned here as literals (not read from the module).
TOP_KEYS = ("schema", "contract_id", "contract_sha256_12", "journal", "place_records_used", "place_records_left",
            "place_records_today", "place_records_left_today", "placing_ended", "placing_ended_code",
            "session_armed_now", "sessions_total", "handoffs", "reads_owed", "review_stops_active", "due_stops",
            "sessions")
SESSION_KEYS = ("utc_day", "reservations", "place_records", "pt4_records", "sends", "fills", "cancels", "unknowns",
                "settles", "skips", "stops", "ended_reason", "ended_by_own_end_note")
# The end block's fields this command must equal, by name (Run.summary, pilot_run.py).
END_BLOCK_FIELDS = ("reservations", "place_records", "pt4_records", "sends", "fills", "cancels", "unknowns", "settles",
                    "skips")
MONEY_WORDS = ("price", "equity", "bsp", "stake", "profit", "balance", "exposure")
# The leaves' types, pinned: counts are ints, flags are bools.
TOP_COUNTS = ("place_records_used", "place_records_left", "place_records_today", "place_records_left_today",
              "sessions_total", "handoffs", "reads_owed", "review_stops_active", "due_stops")
TOP_FLAGS = ("placing_ended", "session_armed_now")
SESSION_COUNTS = ("reservations", "place_records", "pt4_records", "sends", "fills", "cancels", "unknowns", "settles")
DIGITS_REASON = "80808080808080"
DISTINCTIVE_BET = 918273645500
CLI_HELPERS = frozenset({"run_cli", "register", "arm_run", "end_session", "ack", "gap_resolved", "capture"})


# --- rule 17: the guard and the only ways into the CLI ------------------------------------------------------------


def guard(env):
    """Before every CLI call: the harness's state root is inside this process's temporary directory, the patched home
    lookup points there too, and the root is not, and is not inside, the real state root of the password database.
    Containment in the temporary directory is decided on the given path first, before any realpath of it, so a root
    elsewhere (the real state root included) is refused without a metadata call on that root."""
    temp = Path(os.path.realpath(tempfile.gettempdir()))
    given = Path(os.path.abspath(env.paths.root))
    if temp not in given.parents:
        raise AssertionError("not the harness's temporary state root: no CLI call")
    root = Path(os.path.realpath(given))
    real_root = Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)).joinpath(*paths_module.APP_PARTS)
    home = Path(os.path.realpath(paths_module.account_home()))
    if (temp not in root.parents or temp not in home.parents or root == real_root or real_root in root.parents
            or root in real_root.parents or Path(os.path.realpath(env.paths.config_path)).parents[2] != root):
        raise AssertionError("not the harness's temporary state root: no CLI call")


def summary_cli(env, *extra, **kw):
    guard(env)
    return sr.run_cli(env, "pilot-summary", *extra, **kw)


def register(env):
    guard(env)
    return sr.register(env)


def arm(env, *extra, **kw):
    guard(env)
    return sr.arm_run(env, "2000", *extra, **kw)


# --- the worlds -----------------------------------------------------------------------------------------------------


def places(env):
    return [r for r in sr.order_requests(env) if r["method"].endswith("placeOrders")]


def cancels(env):
    return [r for r in sr.order_requests(env) if r["method"].endswith("cancelOrders")]


def static_kill_at_open(env, from_ns):
    """The KILL file appears at the next placement open after from_ns (as test_a_static_kill_at_the_open)."""
    original, armed = cli._config, []
    original_loop = pilot_run.Run.loop

    def loop(self):
        armed.append(True)
        return original_loop(self)

    def config(args, paths):
        if armed and args.command == "pilot-run" and not paths.kill_file.exists() and env.clock.time_ns() >= from_ns:
            paths.kill_file.write_text("")
        return original(args, paths)

    return patch.object(cli, "_config", config), patch.object(pilot_run.Run, "loop", loop)


def distinctive_market(env, off_ns, market_id, **changes):
    """A market whose market id and selection ids are long and distinctive, so the id scan cannot meet them by chance
    inside a count, a date or the hash prefix."""
    remap = {selection: 48_151_600 + selection for selection in sp.BASE_BACKS}
    return sr.add_market(env, off_ns=off_ns, market_id=market_id,
                         backs={remap[s]: v for s, v in sp.BASE_BACKS.items()},
                         lays={remap[s]: v for s, v in sp.BASE_LAYS.items()}, **changes)


def kill_after_first_place_report(env):
    """Another process's own-scope kill journals its kill event and HALT right after the first place report (as
    test_betfair_pilot_run's after_place_report and kill_event)."""
    original, done = OrderJournal.append, []

    def append(journal, kind, payload, **columns):
        event = original(journal, kind, payload, **columns)
        if kind == "report" and payload.get("action") == "place" and not done:
            done.append(True)
            other = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
            try:
                other.append("kill", {"reason": "kill", "scope": "own"})
                other.append("halt", {"reason": "kill", "scope": "global"})
            finally:
                other.close()
        return event

    return patch.object(OrderJournal, "append", append)


def malformed_pilot_note(env):
    """A pilot note that fails validation, on a separate writer: the fold implies a pilot_record_invalid review stop
    that nothing journals (a due stop)."""
    writer = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
    try:
        writer.append("note", {"pilot": {"type": "look"}})
    finally:
        writer.close()


def betfair_settles_first(env):
    """Betfair settles our matched bet right before the session's settle point is offered for its market: the bet
    leaves the current list for the cleared SETTLED list (as test_betfair_p110_measurement's settle_bet), so the
    in-session settle-market settles the market. The journal books it at its worst case, the stake lost."""
    original = pilot_run.Run.settle_one

    def settle_one(run, market_id, open_at):
        for bet_id, order in list(env.exchange.orders.items()):
            if order["marketId"] == market_id:
                env.exchange.orders.pop(bet_id)
                env.exchange.cleared["SETTLED"].append({
                    "betId": bet_id, "marketId": market_id, "selectionId": order["selectionId"],
                    "customerOrderRef": order["customerOrderRef"],
                    "settledDate": utc_ms_from_ns(env.clock.time_ns()), "sizeSettled": order["sizeMatched"],
                    "priceMatched": order["averagePriceMatched"], "priceReduced": False})
        return original(run, market_id, open_at)

    return patch.object(pilot_run.Run, "settle_one", settle_one)


def stop_note(env, stop_id, scope, reason):
    """Append one stop note on a separate writer, as another process would."""
    writer = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
    try:
        return pilot_run.append_pilot(writer, {"pilot": pilot.stop_payload(env.contract, stop_id, scope, reason)})
    finally:
        writer.close()


def walk(value):
    """Every (key or None, leaf) pair of a JSON value: dict keys and leaves alike."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, None
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    else:
        yield None, value


def known_ids(env):
    """Every market id, bet id, selection id, order ref, session id and stop id the harness's journal holds."""
    events = sr.events(env)
    state = sr.fold(env)
    core = pilot_run.recovery_projection(events, contract=env.contract, now_ns=env.clock.time_ns())["core"]
    found = {e["market_id"] for e in events if e.get("market_id")}
    found |= {e["customer_order_ref"] for e in events if e.get("customer_order_ref")}
    found |= {o["bet_id"] for o in core["orders"].values() if o.get("bet_id")}
    found |= {str(r.selection_id) for r in state.place_records} | {r.ref for r in state.place_records}
    found |= {str(s) for m in env.router.markets.values() for s in m.backs}
    found |= {s["session_id"] for s in state.sessions}
    found |= {s["stop_id"] for s in tuple(state.stops) + tuple(state.due_stops)}
    return {str(item) for item in found if item}


def files_under(env):
    """({relative path: sha256} of every file under the temporary home but the journal's -wal and -shm, the sorted
    directories, {side file name: sha256 or None for -shm}). -shm is SQLite's shared-memory index: a reader writes its
    read marks there, so only its presence is compared."""
    home = env.paths.home
    files, dirs, side = {}, [], {}
    for path in sorted(home.rglob("*")):
        relative = path.relative_to(home).as_posix()
        if path.is_dir():
            dirs.append(relative)
        elif path.name.endswith("-shm"):
            side[path.name] = None
        elif path.name.endswith("-wal"):
            side[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files, dirs, side


class FailingConfirmer:
    """A confirmer the command must never use: it records and refuses any use."""

    def __init__(self):
        self.calls = []

    def require_present(self):
        self.calls.append("require_present")
        raise AssertionError("confirmer used")

    def confirm_line(self, expected, *, banner):
        self.calls.append("confirm_line")
        raise AssertionError("confirmer used")


class SummaryCase(unittest.TestCase):
    def build(self, **kw):
        self.env = sr.build_run(self, **kw)
        return self.env

    def registered(self, **kw):
        env = self.build(**kw)
        code, out, err = register(env)
        self.assertEqual(code, 0, err)
        return env

    def summary_text(self, env, **kw):
        """-> (the printed summary, the printed text)."""
        code, out, err = summary_cli(env, **kw)
        self.assertEqual(code, 0, err + out)
        self.assertEqual(err, "")
        return json.loads(out), out

    def summary(self, env, **kw):
        return self.summary_text(env, **kw)[0]

    def day_limit_session(self, env, day=0):
        """Six markets on the day (distinctive ids): the PT4 limit (5) ends the session plainly, with its session_end
        stop."""
        for i in range(6):
            distinctive_market(env, OFF + day * DAY + i * 900 * S, f"1.2468136{day}{i}")
        code, out, err = arm(env)
        self.assertEqual(code, 0, err)
        block = json.loads(out)
        self.assertEqual((block["end_code"], block["place_records"]), ("day_limit", 5))
        return block

    def assert_no_ids(self, env, found, out):
        """Keys and leaves of one printed summary: the fixed keys; every count an int and every flag a bool; utc_day a
        date and the hash prefix 12 hex digits; every code in its closed list; no key or leaf equals or contains a
        market id, bet id, selection id, order ref, session id or stop id the harness's journal holds; no money key."""
        self.assertEqual(tuple(found), tuple(sorted(TOP_KEYS)))
        self.assertEqual(found["schema"], "pilot-summary/v1")
        self.assertIn(found["journal"], ("present", "absent"))
        self.assertIsNotNone(re.fullmatch(r"other|[a-z][a-z0-9-]{0,15}", found["contract_id"]))
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{12}", found["contract_sha256_12"]))
        for key in TOP_COUNTS:
            self.assertIs(type(found[key]), int, key)
        for key in TOP_FLAGS:
            self.assertIs(type(found[key]), bool, key)
        self.assertIn(found["placing_ended_code"], pilot_summary.ENDING_REASONS | {"other", None})
        self.assertIs(type(found["sessions"]), list)
        for session in found["sessions"]:
            self.assertEqual(tuple(session), tuple(sorted(SESSION_KEYS)))
            self.assertIsNotNone(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", session["utc_day"]))
            for key in SESSION_COUNTS:
                self.assertIs(type(session[key]), int, key)
            self.assertIs(type(session["ended_by_own_end_note"]), bool)
            self.assertLessEqual(set(session["skips"]), set(pilot.SKIP_CODES) | {"other"})
            self.assertTrue(all(type(n) is int for n in session["skips"].values()), session["skips"])
            self.assertLessEqual(set(session["stops"]), {"session", "review", "other"})
            for reasons in session["stops"].values():
                self.assertLessEqual(set(reasons), pilot_summary.STOP_REASONS | {"other"})
                self.assertTrue(all(type(n) is int for n in reasons.values()), reasons)
            self.assertIn(session["ended_reason"], pilot_summary.SESSION_END_REASONS | {"other", None})
        ids = known_ids(env)
        for key, leaf in walk(found):
            for item in (key, leaf):
                if item is None or isinstance(item, bool):
                    continue
                text = str(item)
                with self.subTest(item=text):
                    self.assertFalse(any(text == known or known in text for known in ids))
            if key is not None:
                self.assertFalse(key.endswith("_p"), key)
                self.assertFalse(any(word in key.lower() for word in MONEY_WORDS), key)
        for known in ids:
            self.assertNotIn(known, out)
        return ids


# --- 1, 2: the end block, and several days -------------------------------------------------------------------------


class EndBlockTests(SummaryCase):
    def assert_equal_to_end_block(self, env, block):
        """The latest session equals the end block on its nine fields; the whole output passes the id scan.
        -> (the latest session, the summary)."""
        found, out = self.summary_text(env)
        self.assert_no_ids(env, found, out)
        latest = found["sessions"][-1]
        for name in END_BLOCK_FIELDS:
            with self.subTest(field=name):
                self.assertEqual(latest[name], block[name])
        return latest, found

    def test_equal_to_the_end_block_after_a_day_limit_end(self):
        env = self.registered()
        block = self.day_limit_session(env)
        latest, found = self.assert_equal_to_end_block(env, block)
        self.assertEqual((latest["place_records"], latest["sends"], latest["pt4_records"]), (5, 5, 5))
        self.assertEqual((latest["ended_reason"], latest["ended_by_own_end_note"]), ("session_end", True))

    def test_equal_to_the_end_block_with_a_skip_a_refusal_and_an_unknown(self):
        """A market outside GB/IE is reserved and SKIPped (country_not_allowed); the next one is placed and its
        remainder cancel raises in the transport: the cancel is CANCEL_PENDING (an unknown), the executor journals
        executor_exception (a refusal note, so a review stop) and unknown_unresolved. The unknown is one handoff and
        one owed read; the projection's items carry the bet's ids, and none of them is printed."""
        env = self.registered()
        env.exchange.next_bet = DISTINCTIVE_BET
        distinctive_market(env, OFF, "1.246813570", country="FR")
        distinctive_market(env, OFF + 1800 * S, "1.246813571")

        def boom(record):
            raise RuntimeError("synthetic transport fault")

        env.router.next_response[gw.CANCEL_SUFFIX] = [boom]
        code, out, err = arm(env)
        self.assertEqual(code, 5, err)
        block = json.loads(out)
        self.assertEqual((block["skips"], block["unknowns"], block["refusals"]),
                         ({"country_not_allowed": 1}, 1, {"executor_exception": 1}))
        latest, found = self.assert_equal_to_end_block(env, block)
        self.assertEqual(latest["stops"], {"review": {"executor_exception": 1}, "session": {"unknown_unresolved": 1}})
        self.assertEqual(latest["ended_by_own_end_note"], False)
        self.assertEqual((found["handoffs"], found["reads_owed"]), (1, 1))
        self.assertTrue({"1.246813571", str(DISTINCTIVE_BET + 1), "48151611"} <= known_ids(env))


class SeveralDaysTests(SummaryCase):
    def test_totals_windows_and_the_own_end_note(self):
        """Day 0 ends at the day limit; day 1 meets the KILL file at its open (a kill_file session stop); day 2's process
        dies right after its session_start (no end note). Each session counts only the stops in its own window,
        which Run.summary's rule (every stop after the session's start) would not do for an earlier session."""
        env = self.registered()
        self.day_limit_session(env)
        sr.set_day(env, 1)
        sr.add_market(env, off_ns=OFF + DAY)
        config_patch, loop_patch = static_kill_at_open(env, OFF + DAY - 460 * S)
        with config_patch, loop_patch:
            code, out, err = arm(env)
        self.assertEqual(code, 5, err)
        env.paths.kill_file.unlink()                     # the test's own KILL file, in its temporary root
        sr.typed_release(env, sr.admission_view(env)["admission"]["generation"])
        sr.set_day(env, 2)
        with sr.sigkill_after("note", "session_start"), self.assertRaises(sr.Death):
            arm(env)
        found = self.summary(env)
        sessions = found["sessions"]
        self.assertEqual(found["sessions_total"], 3)
        self.assertEqual([s["utc_day"] for s in sessions], ["2027-01-04", "2027-01-05", "2027-01-06"])
        self.assertEqual(sum(s["place_records"] for s in sessions), found["place_records_used"])
        self.assertEqual(found["place_records_used"], 5)
        self.assertEqual(found["place_records_left"], env.contract.lifetime_place_records - 5)
        self.assertEqual(sum(s["sends"] for s in sessions), len(places(env)))
        self.assertEqual([s["stops"] for s in sessions],
                         [{"session": {"session_end": 1}}, {"session": {"kill_file": 1}}, {}])
        first_start = sr.fold(env).sessions[0]["seq"]
        self.assertEqual(sum(1 for s in sr.fold(env).stops if s["seq"] > first_start), 2)    # Run.summary's count
        self.assertEqual([s["ended_reason"] for s in sessions], ["session_end", "kill_file", None])
        self.assertEqual([s["ended_by_own_end_note"] for s in sessions], [True, False, False])
        self.assertTrue(found["session_armed_now"])              # the dead process's session, by the journal

    def test_a_daily_loss_stop_is_not_the_own_end_note(self):
        """A contract variant with a £2 day stop: the first bet is matched in full, the second open's risk decision
        refuses daily_loss, a session stop."""
        raw = contract_bytes(**{"caps.max_daily_loss_p": 200})
        env = self.registered(contract_raw=raw, overrides={"orders": {"caps": {"max_daily_loss": 2.0}}})
        sr.add_market(env, off_ns=OFF)
        sr.add_market(env, off_ns=OFF + 1800 * S)
        sr.fills(env, complete=True)
        code, out, err = arm(env)
        self.assertEqual(code, 5, err)
        latest = self.summary(env)["sessions"][-1]
        self.assertEqual((latest["place_records"], latest["fills"]), (1, 1))
        self.assertEqual(latest["stops"], {"session": {"daily_loss": 1}})
        self.assertEqual((latest["ended_reason"], latest["ended_by_own_end_note"]), ("daily_loss", False))


class FieldTests(SummaryCase):
    def test_left_today_before_the_first_session_is_the_pt4_limit(self):
        """No session yet and no PT4 receipt: the first session is a PT4 session (pilot._Folder.note_session_start),
        so what is left today is p1's PT4 limit (5), not its per-UTC-day limit (25); the session then places that."""
        env = self.registered()
        found = self.summary(env)
        self.assertNotEqual(env.contract.per_pt4_session_day, env.contract.per_utc_day)
        self.assertEqual((found["contract_id"], found["sessions_total"], found["place_records_left_today"]),
                         ("p1", 0, env.contract.per_pt4_session_day))
        block = self.day_limit_session(env)
        self.assertEqual(block["place_records"], found["place_records_left_today"])

    def test_a_journaled_kill_ends_the_session_as_kill(self):
        """A kill journaled by another process after the first place report: ended_reason is the fold's "kill" (not
        "other"), and the session was not ended by its own end note."""
        env = self.registered()
        sr.add_market(env, off_ns=OFF)
        sr.add_market(env, off_ns=OFF + 1800 * S)
        with kill_after_first_place_report(env):
            code, out, err = arm(env)
        self.assertEqual(code, 5, err)
        latest = self.summary(env)["sessions"][-1]
        self.assertEqual((latest["place_records"], latest["ended_reason"], latest["ended_by_own_end_note"]),
                         (1, "kill", False))

    def test_a_record_that_was_never_sent(self):
        """The process dies right after its first PLACE decision note: one reservation, one record, no send."""
        env = self.registered()
        sr.add_market(env, off_ns=OFF)
        with sr.sigkill_after("note", "decision"), self.assertRaises(sr.Death):
            arm(env)
        found = self.summary(env)
        latest = found["sessions"][-1]
        self.assertEqual((latest["reservations"], latest["place_records"], latest["sends"], latest["ended_reason"],
                          latest["ended_by_own_end_note"]), (1, 1, 0, None, False))
        self.assertTrue(found["session_armed_now"])

    def test_skips_and_reservations_stay_in_their_session(self):
        """Day 0 reserves a French market (SKIP) and a GB one (PLACE); day 1 reserves one GB market (PLACE). Day 1's
        session holds neither of day 0's reservations nor its skip; after day 1's plain end nothing is armed."""
        env = self.registered()
        sr.add_market(env, off_ns=OFF, country="FR")
        sr.add_market(env, off_ns=OFF + 1800 * S)
        code, out, err = arm(env)
        self.assertEqual(code, 0, err)
        sr.set_day(env, 1)
        sr.add_market(env, off_ns=OFF + DAY)
        code, out, err = arm(env)
        self.assertEqual(code, 0, err)
        found = self.summary(env)
        self.assertEqual([(s["reservations"], s["place_records"], s["sends"], s["skips"]) for s in found["sessions"]],
                         [(2, 1, 1, {"country_not_allowed": 1}), (1, 1, 1, {})])
        self.assertFalse(found["session_armed_now"])

    def test_handoffs_reads_owed_and_due_stops(self):
        """After a session with an unknown (its remainder cancel raised): reads_owed is the end block's pending_c_reads
        and handoffs is the projection's one handoff. A malformed pilot note then implies a pilot_record_invalid review
        stop that is not journaled: due_stops counts it, review_stops_active counts it too (journaled and due), and no
        session's stops change."""
        env = self.registered()
        sr.add_market(env, off_ns=OFF, country="FR")
        sr.add_market(env, off_ns=OFF + 1800 * S)

        def boom(record):
            raise RuntimeError("synthetic transport fault")

        env.router.next_response[gw.CANCEL_SUFFIX] = [boom]
        code, out, err = arm(env)
        self.assertEqual(code, 5, err)
        block = json.loads(out)
        before = self.summary(env)
        projection = pilot_run.recovery_projection(sr.events(env), contract=env.contract, now_ns=env.clock.time_ns())
        self.assertEqual((before["reads_owed"], before["handoffs"]), (block["pending_c_reads"], 1))
        self.assertEqual(before["handoffs"], len(projection["handoffs"]))
        self.assertEqual((before["due_stops"], before["review_stops_active"]), (0, 1))
        malformed_pilot_note(env)
        after = self.summary(env)
        self.assertEqual((after["due_stops"], after["review_stops_active"]), (1, 2))
        self.assertEqual([s["stops"] for s in after["sessions"]], [s["stops"] for s in before["sessions"]])


# --- 3, 4, 5: writes nothing, needs no terminal, runs beside an armed session ---------------------------------------


class WritesNothingTests(SummaryCase):
    def run_unchanged(self, env):
        """One summary; -> (side files before, side files after). Every other file and directory under the temporary
        home, the journal's main file and its -wal are byte for byte the same; no Keychain call (anchor included), no
        transport request, no process signal; no reader of the registration, admission, ledger or KILL file is called.
        While it runs, an open of any file but the config through builtins.open, io.open or os.open is refused, and so
        is a lock through fcntl.flock, fcntl.lockf or fcntl.fcntl. Opens or locks reached another way (for example a
        name bound before the patch, or os.fdopen of a file descriptor) are not seen here; the AST test of the module's
        calls covers those names. SQLite's own opens and read locks are in C and do not pass through these."""
        files, dirs, side = files_under(env)
        journal = env.paths.journal_path("live").read_bytes()
        calls, requests, signals = list(env.runner.calls), list(env.transport.requests), list(env.processes.signals)
        refuse = {"side_effect": AssertionError("called by pilot-summary")}
        forbidden = [patch.object(module, name, **refuse) for module, names in (
            (pilot_gate, ("read_registration", "code_identity")),
            (admission, ("admission_status", "owner_lock_state", "live_state_problems", "recorder_lock_held")),
            (auth, ("ledger_status",)), (paths_module, ("kill_engaged", "file_lock")),
            (credentials, ("keychain_read", "load_credentials", "publish_anchor")),
            (pilot_run, ("kill_engaged", "file_lock", "keychain_read", "load_credentials", "publish_anchor",
                         "ledger_status", "read_armed_session", "armed_session_check", "append_pilot",
                         "atomic_write_bytes", "atomic_write_json")),
            (OrderJournal, ("__init__", "append")),
            (fcntl, ("flock", "lockf", "fcntl"))) for name in names]
        config = os.path.realpath(env.paths.config_path)
        real_open, real_os_open, opened = io.open, os.open, []

        def only_the_config(file):
            if not isinstance(file, int):
                opened.append(os.path.realpath(file))
                if opened[-1] != config:
                    raise AssertionError("pilot-summary opened a file other than the config")

        def guarded_open(file, *args, **kwargs):
            only_the_config(file)
            return real_open(file, *args, **kwargs)

        def guarded_os_open(path, *args, **kwargs):
            only_the_config(path)
            return real_os_open(path, *args, **kwargs)

        forbidden += [patch.object(builtins, "open", guarded_open), patch.object(io, "open", guarded_open),
                      patch.object(os, "open", guarded_os_open)]
        for patcher in forbidden:
            patcher.start()
        try:
            found = self.summary(env)
        finally:
            for patcher in forbidden:
                patcher.stop()
        after_files, after_dirs, after_side = files_under(env)
        self.assertEqual((after_files, after_dirs), (files, dirs))
        self.assertEqual(env.paths.journal_path("live").read_bytes(), journal)
        wal = env.paths.journal_path("live").name + "-wal"
        if wal in side and wal in after_side:
            self.assertEqual(after_side[wal], side[wal])
        elif wal in after_side:                                    # a -wal the read-only open made holds nothing
            self.assertEqual(after_side[wal], hashlib.sha256(b"").hexdigest())
        self.assertEqual((env.runner.calls, env.transport.requests, env.processes.signals), (calls, requests, signals))
        self.assertEqual(opened, [config])                       # the config, once: the guard saw the one open
        self.assertEqual(found["place_records_used"], 5)
        return sorted(side), sorted(after_side)

    def test_run_twice_with_the_harness_journal_open_then_closed(self):
        env = self.registered()
        self.day_limit_session(env)
        name = env.paths.journal_path("live").name
        open_before, open_after = self.run_unchanged(env)          # (a) the harness's own connection is open
        self.assertEqual(open_before, open_after)
        head = env.journal.anchor()
        env.journal.close()                                        # (b) no connection open
        closed_before, closed_after = self.run_unchanged(env)
        self.assertEqual(closed_before, [])                        # SQLite removed them when the last one closed
        self.assertLessEqual(set(closed_after), {name + "-wal", name + "-shm"})
        writer = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        try:
            self.assertEqual(writer.verify(), {"status": "ok", "events": head["seq"], "first_bad_seq": None})
            self.assertEqual(writer.anchor(), head)
            event = writer.append("note", {"reason": "synthetic_after_pilot_summary"})
            self.assertEqual(event["seq"], head["seq"] + 1)
            self.assertEqual(writer.verify()["status"], "ok")
        finally:
            writer.close()


class NoTerminalTests(SummaryCase):
    def test_no_confirmer_is_asked(self):
        env = self.registered()
        self.day_limit_session(env)
        failing = FailingConfirmer()
        absent = gw.FakeConfirmer(present=False)
        first = self.summary(env, confirmer=failing)
        second = self.summary(env, confirmer=absent)
        self.assertEqual(failing.calls, [])
        self.assertEqual(absent.prompts, [])
        self.assertEqual(first, second)


class ArmedSessionTests(SummaryCase):
    def test_between_two_placement_opens(self):
        """While a session is armed, after its first placement open and before the second: the summary prints, and the
        second send still goes (nothing was appended, so the head the session saw has not moved)."""
        env = self.registered()
        sr.add_market(env, off_ns=OFF)
        sr.add_market(env, off_ns=OFF + 1800 * S)
        seen = []

        def look():
            before = sr.events(env)
            result = summary_cli(env)
            seen.append((result, before, sr.events(env)))

        sr.on_sleep(env, lambda clock: len(places(env)) == 1 and cancels(env) and clock.time_ns() >= OFF + 600 * S,
                    look)
        code, out, err = arm(env)
        self.assertEqual(code, 0, err)
        (result, before, after), = seen
        self.assertEqual((result[0], result[2]), (0, ""))
        self.assertEqual(after, before)                            # the summary appended nothing: the head is where it was
        during = json.loads(result[1])
        self.assertTrue(during["session_armed_now"])
        self.assertEqual((during["sessions"][-1]["place_records"], during["sessions"][-1]["sends"]), (1, 1))
        self.assertEqual(len(places(env)), 2)                       # the session's next send still went
        self.assertEqual(sr.notes(env, "refusal"), [])
        self.assertEqual(json.loads(out)["place_records"], 2)


# --- 6, 7, 8: no id, price or money; the refusals; another rule file -----------------------------------------------


class NoIdPriceOrMoneyTests(SummaryCase):
    def test_keys_codes_and_leaves(self):
        env = self.registered()
        env.exchange.next_bet = DISTINCTIVE_BET
        for i in range(6):
            distinctive_market(env, OFF + i * 900 * S, f"1.24681357{i}")
        code, out, err = arm(env)
        self.assertEqual(code, 0, err)
        stop_note(env, "digits-stop-1", "review", pilot.refusal_code(DIGITS_REASON))
        found, out = self.summary_text(env)
        self.assertEqual(TOP_KEYS, pilot_summary.TOP_KEYS)
        self.assertEqual(SESSION_KEYS, pilot_summary.SESSION_KEYS)
        ids = self.assert_no_ids(env, found, out)
        self.assertTrue({"1.246813570", str(DISTINCTIVE_BET + 1), "48151611"} <= ids, ids)
        self.assertEqual(found["sessions"][-1]["stops"], {"review": {"other": 1}, "session": {"session_end": 1}})
        self.assertEqual(found["review_stops_active"], 1)
        self.assertNotIn(DIGITS_REASON, out)
        value = pilot_summary.summarize(env.paths.journal_path("live"), clock=env.clock, config=env.config,
                                        read_contract=env.ctx.read_contract)
        self.assertEqual(json.loads(json.dumps(value, allow_nan=False)), found)       # strict: no default=


class PauseTests(SummaryCase):
    def test_a_pause_is_counted_by_its_stops_and_no_level_is_printed(self):
        """A p1 variant with a 400p pause step (as test_a_pause_ack_needs_no_cause): the first bet is matched in full,
        Betfair settles it, and the session's settle point settles its market, booked at its worst case (the stake
        lost, -200p). The next PLACE would reach -400p, so it is SKIP(pause), with one session and one review pause
        stop, and the session ends. The whole output is pinned. The count of pause stops (1), with the rule file's
        public step (400p), shows how many pause levels were reached, which the help and the body disclose; no pence
        figure, level or stop id is printed."""
        env = self.registered(contract_raw=contract_bytes(**{"pause.step_p": 400}))
        env.exchange.next_bet = DISTINCTIVE_BET
        first = distinctive_market(env, OFF, "1.246813570")
        distinctive_market(env, OFF + 3600 * S, "1.246813571")
        sr.fills(env, complete=True)
        sr.closes_after_off(env, first)
        with betfair_settles_first(env):
            code, out, err = arm(env)
        self.assertEqual(code, 5, err)
        block = json.loads(out)
        self.assertEqual((block["place_records"], block["settles"], block["skips"]), (1, 1, {"pause": 1}))
        state = sr.fold(env)
        self.assertEqual((state.equity_p, sorted(state.pause_fired)), (-200, [-400]))
        self.assertEqual(sorted(s["stop_id"] for s in state.stops), ["pause-400", "pause-400-session"])
        found, text = self.summary_text(env)
        self.assert_no_ids(env, found, text)
        self.assertEqual(found, {
            "schema": "pilot-summary/v1", "contract_id": "p1", "contract_sha256_12": env.contract.sha256[:12],
            "journal": "present", "place_records_used": 1,
            "place_records_left": env.contract.lifetime_place_records - 1, "place_records_today": 1,
            "place_records_left_today": env.contract.per_pt4_session_day - 1, "placing_ended": False,
            "placing_ended_code": None, "session_armed_now": False, "sessions_total": 1, "handoffs": 0,
            "reads_owed": 0, "review_stops_active": 1, "due_stops": 0,
            "sessions": [{"utc_day": "2027-01-04", "reservations": 2, "place_records": 1, "pt4_records": 1,
                          "sends": 1, "fills": 1, "cancels": 0, "unknowns": 0, "settles": 1, "skips": {"pause": 1},
                          "stops": {"review": {"pause": 1}, "session": {"pause": 1}}, "ended_reason": "pause",
                          "ended_by_own_end_note": False}]})
        for name in END_BLOCK_FIELDS:
            self.assertEqual(found["sessions"][-1][name], block[name], name)
        # What the disclosure says: the session pause stops, times the public step, are the deepest level reached.
        reached = sum(s["stops"].get("session", {}).get("pause", 0) for s in found["sessions"])
        self.assertEqual(-reached * env.contract.pause_step_p, min(state.pause_fired))
        leaves = {leaf for _, leaf in walk(found) if type(leaf) is int}
        self.assertFalse(leaves & {200, -200, 400, -400}, leaves)
        self.assertNotIn("pause-400", text)


class RefusalTests(SummaryCase):
    def refused(self, env, code):
        exit_code, out, err = summary_cli(env)
        self.assertEqual((exit_code, out, err), (2, "", f"Betfair input error: {code}\n"))

    def test_each_refusal_prints_one_code_and_no_value(self):
        env = self.registered()
        gw.live_config(env.paths, {})                       # the same config with no orders.pilot
        self.refused(env, "pilot_not_configured")
        for raw in (OSError("synthetic unreadable"), b"{}", b"not json"):
            with self.subTest(contract=repr(raw)[:20]):
                env = self.registered()
                env.state.contract_raw = raw
                self.refused(env, "pilot_contract_unreadable")
        env = self.registered()
        env.state.contract_raw = contract_bytes(**{"caps.max_daily_loss_p": 4000})
        self.refused(env, "pilot_contract_differs")
        env = self.registered()
        env.journal.close()
        path = env.paths.journal_path("live")
        for side in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if side.exists():
                side.unlink()
        path.write_bytes(b"synthetic: not a database " * 40)
        self.refused(env, "pilot_journal_unreadable")
        env = self.registered()
        other = pilot.contract_from_bytes(contract_bytes(**{"caps.max_daily_loss_p": 4000}))
        writer = OrderJournal(env.paths.journal_path("live"), mode="live", clock=env.clock)
        try:
            pilot_run.append_pilot(writer, {"pilot": pilot.stop_payload(other, "other-contract-1", "review", "x")})
        finally:
            writer.close()
        self.refused(env, "pilot_fold_failed")
        env = self.registered()
        env.paths.config_path.write_text("synthetic: not json")
        self.refused(env, "config_not_strict_json")
        env.paths.config_path.unlink()
        self.refused(env, "config_missing")

    def test_an_absent_journal_is_counted_not_refused(self):
        env = self.build()
        env.journal.close()
        path = env.paths.journal_path("live")
        for item in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if item.exists():
                item.unlink()
        found = self.summary(env)
        self.assertEqual((found["journal"], found["place_records_used"], found["sessions"]), ("absent", 0, []))
        self.assertFalse(path.exists())                     # the read-only check created nothing


class OtherRuleFileTests(SummaryCase):
    def test_left_today_and_in_all_follow_the_rule_file(self):
        """A variant of p1 with 4 PLACE records in all, 9 a UTC day and 3 a PT4 session day: every session here is a PT4
        session (no PT4 receipt), so the fold's capacity_and_pause picks 3."""
        raw = contract_bytes(**{"placement_limits.lifetime_place_records": 4,
                                "placement_limits.place_records_per_utc_day": 9,
                                "placement_limits.place_records_per_pt4_session_day": 3})
        env = self.registered(contract_raw=raw)
        for i in range(4):
            sr.add_market(env, off_ns=OFF + i * 900 * S)
        self.assertEqual(arm(env)[0], 0)
        found = self.summary(env)
        self.assertEqual((found["place_records_used"], found["place_records_left"], found["place_records_today"],
                          found["place_records_left_today"], found["placing_ended"]), (3, 1, 3, 0, False))
        sr.set_day(env, 1)
        found = self.summary(env)
        self.assertEqual((found["place_records_today"], found["place_records_left_today"], found["place_records_left"]),
                         (0, 1, 1))
        for i in range(2):
            sr.add_market(env, off_ns=OFF + DAY + i * 900 * S)
        self.assertEqual(arm(env)[0], 0)
        found = self.summary(env)
        self.assertEqual((found["place_records_used"], found["place_records_left"], found["place_records_left_today"],
                          found["placing_ended"], found["placing_ended_code"]), (4, 0, 0, True, "lifetime_cap"))
        self.assertEqual(found["contract_sha256_12"], env.contract.sha256[:12])
        self.assertNotEqual(found["contract_sha256_12"], sp.C.sha256[:12])


# --- 9, 10, 11, 12: not a live command; imports and calls; isolation; cli does not import it ------------------------


def function_named(tree, name):
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


def called_names(node):
    found = []
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            found.append(func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute)
                         else None)
    return found


class NotALiveCommandTests(SummaryCase):
    def test_flags_help_and_the_live_only_list(self):
        env = self.registered()
        for extra in (("--mode", "live"), ("--mode", "paper"), ("--confirm-live", "x"), ("--bet-angel-paused",)):
            with self.subTest(extra=extra):
                code, out, err = summary_cli(env, *extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("unrecognized arguments", err)
        code, out, err = summary_cli(env, "--help")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(" ".join(cli.HELP["pilot-summary"].split()[:8]), " ".join(out.split()))
        text = cli.HELP["pilot-summary"]
        self.assertFalse(text.startswith("Live only"))
        for phrase in ("Offline and read-only", "appends nothing", "takes neither the gateway lock nor the owner lock",
                       "logs in nowhere", "reads no Keychain item", "needs no terminal", "as status does",
                       "SQLite may create its own side files", "holds its own read locks while it reads",
                       "never an id, a price or a money figure",
                       "(a count of pause stops shows how many pause levels were reached)",
                       "ended_by_own_end_note does not show"):
            self.assertIn(phrase, text)
        self.assertNotIn("takes no lock", text)
        parser = function_named(ast.parse(CLI.read_text(encoding="utf-8")), "build_parser")
        loops = [node for node in ast.walk(parser) if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)
                 and "pilot-run" in [e.value for e in node.iter.elts if isinstance(e, ast.Constant)]]
        self.assertEqual(len(loops), 1)
        self.assertNotIn("pilot-summary", [e.value for e in loops[0].iter.elts])
        self.assertIs(cli.COMMANDS["pilot-summary"], cli.cmd_pilot_summary)


class ImportsAndCallsTests(unittest.TestCase):
    FORBIDDEN_CALLS = frozenset({
        "OrderJournal", "append", "append_pilot", "file_lock", "keychain_read", "load_credentials", "publish_anchor",
        "anchor_write", "confirm_line", "require_present", "confirmer", "_pilot_env", "_pilot_command",
        "live_confirmer", "ensure", "atomic_write_bytes", "atomic_write_json", "open", "write", "write_text",
        "write_bytes", "mkdir", "touch", "unlink", "rename", "chmod", "run", "Popen", "connect", "execute", "admit",
        "release", "release_own", "live_ownership", "_gateway", "_live_gateway", "_existing_journal",
        "_journal_summary", "_read_client", "flush_due", "session_stop", "refusal_note",
        # reads and locks that need no forbidden name, and the ways round a name (the run-time guard of test 3 too)
        "read_bytes", "read_text", "glob", "rglob", "iterdir", "listdir", "scandir", "flock", "lockf", "getattr",
        "__import__", "import_module", "exec", "eval"})

    def test_the_module_imports_and_calls(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports |= {(alias.name, 0, (alias.name,)) for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or "", node.level, tuple(alias.name for alias in node.names)))
        self.assertEqual(imports, {("os", 0, ("os",)), ("re", 0, ("re",)), ("", 1, ("pilot",)),
                                   ("config", 1, ("pilot_enabled",)), ("errors", 1, ("JournalError",)),
                                   ("pilot_run", 1, ("pilot_events_after", "recovery_projection")),
                                   ("schemas", 1, ("UNRESOLVED_STATES",))})
        self.assertEqual(sorted(set(called_names(tree)) & self.FORBIDDEN_CALLS), [])
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertEqual((names | attributes) & {"open", "subprocess", "sqlite3", "OrderJournal", "file_lock"}, set())

    def test_the_cli_function_calls(self):
        source = CLI.read_text(encoding="utf-8")
        function = function_named(ast.parse(source), "cmd_pilot_summary")
        self.assertEqual(sorted(set(called_names(function)) & self.FORBIDDEN_CALLS), [])
        imports = [(node.module, node.level, [a.name for a in node.names]) for node in ast.walk(function)
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [(None, 1, ["pilot_gate", "pilot_summary"])])


class IsolationTests(unittest.TestCase):
    def test_every_cli_call_is_guarded_and_nothing_bypasses_the_harness(self):
        tree = ast.parse(HERE.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        guarded = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in CLI_HELPERS
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "sr"):
                owner = parents[node]
                while not isinstance(owner, ast.FunctionDef):
                    owner = parents[owner]
                body = [s for s in owner.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
                with self.subTest(function=owner.name):
                    self.assertEqual(ast.unparse(body[0]), "guard(env)")
                guarded.append(owner.name)
            if isinstance(node, ast.Attribute) and node.attr == "main" and isinstance(node.value, ast.Name):
                self.assertNotEqual(node.value.id, "cli", "a bare cli.main( call")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self.assertNotIn("subprocess", ast.unparse(node))
            if isinstance(node, ast.Name):
                self.assertNotEqual(node.id, "subprocess")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "addCleanup":
                self.assertFalse({n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                                 & {"arm", "register", "summary_cli", "sr"}, "a CLI call after cleanup")
            if isinstance(node, ast.Call):                      # no command, and no state paths, outside the harness
                func = node.func.value if isinstance(node.func, ast.Subscript) else node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "cli":
                    self.assertFalse(func.attr.startswith("cmd_") or func.attr in ("COMMANDS", "_paths"),
                                     f"a direct call of cli.{func.attr}")
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
                self.assertNotEqual(name, "StatePaths", "StatePaths built outside the harness")
        self.assertEqual(sorted(set(guarded)), ["arm", "register", "summary_cli"])
        for scope in (tree, *[n for n in tree.body if isinstance(n, ast.ClassDef)]):
            for node in scope.body:
                if isinstance(node, ast.FunctionDef) and node.name in ("setUp", "setUpClass", "setUpModule",
                                                                       "tearDown", "tearDownClass", "doCleanups"):
                    self.fail(f"{node.name} defined: no CLI call in a setup or after cleanup")

    def test_the_guard_refuses_another_root(self):
        """The guard refuses the real state root, a path inside it and a path elsewhere, and decides it before any
        realpath of the given root: no metadata call reaches the real state root (rule 17). The recorder resolves only
        the temporary directory itself; any other path it is handed is recorded and only made absolute, so this test
        makes no metadata call outside the temporary directory even against a guard that would."""
        real = Path(pwd.getpwuid(os.getuid()).pw_dir).joinpath(*paths_module.APP_PARTS)
        resolved, original, temporary = [], os.path.realpath, tempfile.gettempdir()

        def realpath(path, *args, **kwargs):
            resolved.append(os.fspath(path))
            return original(path, *args, **kwargs) if resolved[-1] == temporary else os.path.abspath(path)

        with patch.object(os.path, "realpath", realpath):
            for root in (real, real / "profiles", Path("/") / "srv" / "elsewhere"):
                with self.subTest(root=str(root)):
                    fake = SimpleNamespace(paths=SimpleNamespace(root=root, config_path=root / "profiles" / "default"
                                                                 / "config.json"))
                    with self.assertRaises(AssertionError):
                        guard(fake)
        self.assertEqual(resolved, [temporary] * 3)       # only the temporary directory, once per refusal


class CliImportTests(unittest.TestCase):
    NAME = "betangel_research.betfair.pilot_summary"

    def test_only_cmd_pilot_summary_imports_the_module(self):
        found = []
        for path in sorted((sp.REPO / "betangel_research").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and "pilot_summary" in ast.unparse(node):
                    owner = parents.get(node)
                    while owner is not None and not isinstance(owner, ast.FunctionDef):
                        owner = parents.get(owner)
                    found.append((path.name, owner.name if owner is not None else None))
        self.assertEqual(found, [("cli.py", "cmd_pilot_summary")])

    def test_loading_cli_does_not_import_it(self):
        """cli.py's module body, executed afresh with pilot_summary gone from sys.modules and from the package, leaves it
        unimported (restored afterwards)."""
        package = sys.modules["betangel_research.betfair"]
        saved_module = sys.modules.pop(self.NAME, None)
        saved_attribute = package.__dict__.pop("pilot_summary", None)
        try:
            spec = importlib.util.spec_from_file_location("betangel_research.betfair._cli_import_probe", CLI)
            probe = importlib.util.module_from_spec(spec)
            with patch.object(sys, "stdout", io.StringIO()):
                spec.loader.exec_module(probe)
            self.assertNotIn(self.NAME, sys.modules)
            self.assertNotIn("pilot_summary", package.__dict__)
            self.assertIn("pilot-summary", probe.COMMANDS)
        finally:
            sys.modules.pop("betangel_research.betfair._cli_import_probe", None)
            if saved_module is not None:
                sys.modules[self.NAME] = saved_module
            if saved_attribute is not None:
                package.pilot_summary = saved_attribute


if __name__ == "__main__":
    unittest.main()
