"""The daily launchd wrapper for the delayed-key recorder (recording design v9 §4): one London recording day, 07:00 to
23:00 Europe/London, of `record` children started from the pinned worktree.

python3 -E -s -m betangel_research.betfair.recording_day --launchd     (the production plist; Yu loads it once)
python3 -E -s -m betangel_research.betfair.recording_day --attended --end-at <UTC ISO>   (Yu's hand start, or the
                                                                                          test label)
add --dry-run to either: every preflight read, the planned argv, the end instant and the interpreter, no child.

What it does, each start (§4.3): a read-only preflight (the interpreter, version and package against pinned.json; the
pinned worktree's HEAD and a clean tree; the config read through load_config with the delayed key and no live key;
the disk floor recorder.max_bytes + 5 GB on the recording root's volume; admission_status, waiting while INHIBIT or
ADMISSION exists; the login ledger's step 8 rule; the run-ledger without a torn line; the day file), then, in
production, the attempt reserved in the day file by a strict checked atomic write before the spawn (temp file,
F_FULLFSYNC, rename, directory open and fsync, raising on any failure), then the child
[sys.executable, -E, -s, -m, betangel_research.betfair, record, --require-delayed-key, --end-at, <end>,
--invocation-id, <id>] in the pinned worktree, stdin /dev/null, stdout a pipe drained by a thread that keeps only the
refused outcome and the stop_requested_before_start marker, stderr a 0600 file under recording-wrapper/logs. A
post-start check reads the session's receipt.partial.json (key type, freshness label, git revision, clean tree,
code_sha256 against pinned.json's frozen dict, python version, config_sha256). After the child exits the outcome is
classified from disk (recordings.classify_invocation, then the touched gate of §4.4 rule 1), never from the exit
code; the attempt is finished in the same strict write that closes the day when the outcome ends or stops it.
Restarts (reconnect_budget_exhausted and exactly three transient precount codes, Astra Q14) are capped at 2 a day,
15 min apart; resumptions after live work (Astra Q9) at 8 a day, 5 min after the observed release; nothing starts
with less than 30 min to the end instant. Every spacing is wall time (time_ns): the monotonic clock stops in sleep.

What it never does (§4.7): take admission.lock or .recorder.lock, write recorder.pid, call recorder_lock_held, write
config.json or pinned.json, run login-reset, clear INHIBIT, delete or move a recording file, or read segment content.
Its only effect under recordings/ is the pinned CLI's `recordings index` subprocess, run after the child has exited.
Its own writes: the day file <state root>/recording-wrapper/<London date>.json (0600) or an attended run's record
<London date>-attended-<invocation id>.json, the child's stderr file under recording-wrapper/logs, and its log lines
on stdout (the plist sends them to recording-wrapper/logs). Codes, counts, ids and times only.

Install note (Yu, before any plist load, test label or production): mkdir -m 0700 -p "<state root>/recording-wrapper/logs".
pinned.json (0600, written once with Yu's OK when the pinned worktree is made; never by this module) holds
{"commit", "worktree", "interpreter", "python", "code_sha256"}.
"""

import argparse
from datetime import date as date_type, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import zoneinfo

from .admission import admission_status, ledger_problems
from .config import config_sha256, load_config
from .paths import StatePaths
from .receipts import atomic_write_json, git_state
from .recordings import classify_invocation, run_ledger_problem

LONDON = zoneinfo.ZoneInfo("Europe/London")
NS = 1_000_000_000
START_HOUR = 7                       # §4.2: launchd fires at 07:00; an earlier start waits until 07:00 London
END_HOUR = 23                        # the end instant: 23:00 on the recording date in Europe/London
WINDOW_FLOOR_NS = 30 * 60 * NS       # never start with less than 30 min to the end (v4Δ §D :49)
RESTART_SPACING_NS = 15 * 60 * NS
RESUMPTION_SPACING_NS = 5 * 60 * NS
MAX_RESTARTS = 2
MAX_RESUMPTIONS = 8
ADMISSION_POLL_S = 60
CHILD_POLL_S = 1.0
POST_START_TIMEOUT_NS = 120 * NS
DNS_WAIT_NS = 120 * NS
DNS_HOST = "api.betfair.com"
RESERVE_BYTES = 5_000_000_000        # RV7-5: a margin, not a guarantee against ENOSPC
RECORDER_PROFILE = "default"         # pilot_run.RECORDER_PROFILE: the recorder's profile (design v3:27)
DAY_SCHEMA = 1
KINDS = ("first", "restart", "resumption")
DAY_STATES = ("open", "done", "stopped_for_review", "window_exhausted")
DONE_STOPS = frozenset({"end_at_reached", "max_duration_reached", "max_seconds_reached"})
RESTART_STOPS = frozenset({"reconnect_budget_exhausted", "market_lister_failed:connect_failed",
                           "market_lister_failed:tls_failed", "market_lister_failed:deadline_exceeded"})
POST_START_FIELDS = ("key_type", "label", "git_revision", "working_tree_dirty", "code_sha256", "python",
                     "config_sha256")


class DayFileError(Exception):
    """The day file cannot be read or is malformed (day_file_unreadable)."""


class DayFileWriteFailed(Exception):
    """A strict checked write failed at some step, the directory open or fsync included (day_file_write_failed)."""


class Stop(Exception):
    """The day ends here: code is the closing code, state the day state it leaves."""

    def __init__(self, code, state="stopped_for_review", invocation_id=None):
        super().__init__(code)
        self.code = code
        self.state = state
        self.invocation_id = invocation_id


# --- dates and the end instant (§4.2) ----------------------------------------------------------------------------


def utc_iso(ns):
    return datetime.fromtimestamp(ns // NS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def london_date(now_ns):
    """The recording date: the Europe/London calendar date of now, whatever the process TZ."""
    return datetime.fromtimestamp(now_ns / NS, tz=timezone.utc).astimezone(LONDON).date()


def london_instant_ns(day, hour):
    """hour:00 on day in Europe/London as UTC ns, from the zone at that date (never an offset taken earlier)."""
    local = datetime(day.year, day.month, day.day, hour, 0, tzinfo=LONDON)
    return int(local.astimezone(timezone.utc).timestamp()) * NS


def end_instant_ns(day):
    return london_instant_ns(day, END_HOUR)


def start_bound_ns(day):
    return london_instant_ns(day, START_HOUR)


def parse_end_at(text):
    """A UTC ISO instant (Z or an offset) -> ns; ValueError otherwise."""
    if not isinstance(text, str):
        raise ValueError("end_at_invalid")
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("end_at_invalid")
    return int(value.timestamp()) * NS


# --- the world it touches, injectable for tests -------------------------------------------------------------------


class SystemClock:
    def time_ns(self):
        return time.time_ns()

    def sleep(self, seconds):
        time.sleep(seconds)


class StrictWriter:
    """The day file's strict checked atomic write (RV8-1): temp file, F_FULLFSYNC (fsync where it is refused),
    rename, then open and fsync the directory, raising DayFileWriteFailed on any failure. receipts.atomic_write_json
    swallows directory-sync errors, so it is never used for the day file. Tests replace the os-level calls."""

    open = staticmethod(os.open)
    fsync = staticmethod(os.fsync)
    replace = staticmethod(os.replace)

    def full_fsync(self, fd):
        if hasattr(fcntl, "F_FULLFSYNC"):
            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
                return
            except OSError:
                pass
        self.fsync(fd)

    def write(self, path, value):
        path = Path(path)
        data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            fd = self.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                self.full_fsync(fd)
            finally:
                os.close(fd)
            self.replace(str(tmp), str(path))
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise DayFileWriteFailed(type(exc).__name__) from None
        try:
            directory = self.open(str(path.parent), os.O_RDONLY)
        except OSError as exc:
            raise DayFileWriteFailed("directory_open:" + type(exc).__name__) from None
        try:
            self.fsync(directory)
        except OSError as exc:
            raise DayFileWriteFailed("directory_fsync:" + type(exc).__name__) from None
        finally:
            os.close(directory)


class Child:
    """A spawned record child: its stdout drained by a thread that keeps two codes only (§4.3 step 3)."""

    def __init__(self, process):
        self.process = process
        self.pid = process.pid
        self.refused_outcome = None
        self.stop_before_start = False
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        for raw in self.process.stdout:
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            refused = event.get("refused")
            if isinstance(refused, dict) and isinstance(refused.get("outcome"), str):
                self.refused_outcome = refused["outcome"]
            summary = event.get("summary")
            if isinstance(summary, dict) and summary.get("stop_reason") == "stop_requested_before_start":
                self.stop_before_start = True

    def poll(self):
        return self.process.poll()

    def terminate(self):
        try:
            self.process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

    def wait(self):
        code = self.process.wait()
        self._thread.join(timeout=10)
        return code


class System:
    """The real world. Every method here is replaced by a fake in the tests."""

    def __init__(self):
        self.clock = SystemClock()
        self.writer = StrictWriter()

    def spawn(self, argv, *, cwd, stderr_path):
        fd = os.open(str(stderr_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
        try:
            process = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=fd, text=True, bufsize=1)
        finally:
            os.close(fd)
        return Child(process)

    def run(self, argv, *, cwd):
        try:
            return subprocess.run(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=1800).returncode
        except (OSError, subprocess.SubprocessError):
            return None

    def disk_free(self, path):
        return shutil.disk_usage(str(path)).free

    def git_state(self, root):
        return git_state(root)

    def resolves(self, host):
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            return False

    def interpreter(self):
        return sys.executable, platform.python_version()

    def package_path(self):
        """betangel_research/__init__.py of the code running now (the package this module belongs to)."""
        return (Path(__file__).resolve().parents[1] / "__init__.py").resolve()

    def install_signals(self, handler):
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                previous[sig] = signal.signal(sig, handler)
            except ValueError:
                pass
        return previous

    def restore_signals(self, previous):
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    def log(self, line):
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass


# --- the wrapper ---------------------------------------------------------------------------------------------------


class Wrapper:
    """One wrapper run. mode "production" (--launchd: the day file, restarts, resumptions) or "attended" (its own
    record, one attempt, no restart or resumption, never the production day file)."""

    def __init__(self, paths, *, mode, end_at_ns=None, dry_run=False, system=None):
        if mode not in ("production", "attended"):
            raise ValueError("mode")
        self.paths = paths
        self.mode = mode
        self.attended_end_ns = end_at_ns
        self.dry_run = dry_run
        self.sys = system or System()
        self.clock = self.sys.clock
        self.dir = Path(paths.root) / "recording-wrapper"
        self.logs = self.dir / "logs"
        self.signalled = None
        self.child = None
        self.forwarded = False
        self.day = None
        self.day_path = None
        self.record_path = None
        self.pinned = None
        self.config = None
        self.config_sha = None
        self.last_generation = None
        self.release_observed_ns = None
        self.first_start_done = False

    # -- logging and signals --

    def log(self, event, **fields):
        row = {"at": utc_iso(self.clock.time_ns()), "event": event}
        row.update(fields)
        self.sys.log(json.dumps(row, sort_keys=True, separators=(",", ":")))

    def handler(self, signum, frame):
        self.signalled = signum
        if self.child is not None and not self.forwarded:
            self.forwarded = True
            self.child.terminate()

    # -- the day file (§4.6) --

    def load_day(self, path):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise DayFileError() from None
        try:
            day = json.loads(data.decode("utf-8"))
            if (not isinstance(day, dict) or day.get("schema_version") != DAY_SCHEMA
                    or day.get("state") not in DAY_STATES or not isinstance(day.get("attempts"), list)
                    or not isinstance(day.get("counts"), dict) or set(day["counts"]) != set(KINDS)):
                raise ValueError("shape")
            for entry in day["attempts"]:
                if (not isinstance(entry, dict) or entry.get("kind") not in KINDS
                        or entry.get("state") not in ("reserved", "finished")
                        or not isinstance(entry.get("invocation_id"), str)
                        or type(entry.get("spawn_wall_ns")) is not int or type(entry.get("kind_count")) is not int):
                    raise ValueError("entry")
            for kind in KINDS:
                if type(day["counts"][kind]) is not int or day["counts"][kind] != sum(
                        1 for e in day["attempts"] if e["kind"] == kind):
                    raise ValueError("counts")
        except (ValueError, UnicodeDecodeError, KeyError, TypeError):
            raise DayFileError() from None
        return day

    def new_day(self, day):
        return {"schema_version": DAY_SCHEMA, "date": day.isoformat(), "state": "open", "close_code": None,
                "close_invocation_id": None, "counts": {kind: 0 for kind in KINDS}, "attempts": [],
                "summary": {"outcomes": [], "finalized": [], "review": []}}

    def write_day(self):
        """The strict checked write; DayFileWriteFailed propagates (the caller stops for review)."""
        self.sys.writer.write(self.day_path, self.day)

    def close_day(self, state, code, invocation_id=None):
        self.day["state"] = state
        self.day["close_code"] = code
        self.day["close_invocation_id"] = invocation_id

    # -- preflight (§4.3 step 1) --

    def read_pinned(self):
        path = self.dir / "pinned.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            raise Stop("pinned_values_unreadable") from None
        keys = {"commit", "worktree", "interpreter", "python", "code_sha256"}
        if not isinstance(value, dict) or not keys <= set(value) or not isinstance(value["code_sha256"], dict):
            raise Stop("pinned_values_unreadable")
        return value

    def preflight(self, *, first):
        """None when a child may start, "wait" while INHIBIT or ADMISSION exists; raises Stop(code) otherwise. Read only
        (the optional DNS wait sends no Betfair request)."""
        pinned = self.pinned = self.read_pinned()
        interpreter, version = self.sys.interpreter()
        if interpreter != pinned["interpreter"]:
            raise Stop("interpreter_mismatch")
        if version != pinned["python"]:
            raise Stop("python_version_mismatch")
        worktree = Path(pinned["worktree"]).resolve()
        package = self.sys.package_path()
        if worktree not in package.parents:
            raise Stop("package_not_in_pinned_worktree")
        git = self.sys.git_state(worktree)
        if git.get("git_revision") is None or git.get("working_tree_dirty") is None:
            raise Stop("pinned_git_state_unknown")
        if git["git_revision"] != pinned["commit"]:
            raise Stop("pinned_head_mismatch")
        if git["working_tree_dirty"]:
            raise Stop("pinned_tree_dirty")
        try:
            config = load_config(self.paths.config_path, paths=self.paths)
        except Exception:
            raise Stop("config_unreadable") from None
        if config["stream"]["app_key_slot"] != "delayed" or config["recorder"]["allow_live_key"] is not False:
            raise Stop("config_live_key")
        self.config, self.config_sha = config, config_sha256(config)
        root = (Path(config["recorder"]["root"]) if config["recorder"]["root"] is not None
                else Path(self.paths.recordings_dir))
        try:
            free = self.sys.disk_free(root)
        except OSError:
            raise Stop("disk_usage_unknown") from None
        if free < config["recorder"]["max_bytes"] + RESERVE_BYTES:
            raise Stop("disk_reserve_low")
        if not self.logs.is_dir() or self.logs.is_symlink():
            raise Stop("logs_dir_missing")
        try:
            status = admission_status(self.paths)
        except Exception:
            raise Stop("admission_state_unknown") from None
        if status["inhibit"] or status["admission"] is not None:
            self.release_observed_ns = None
            return "wait"
        if self.release_observed_ns is None:
            self.release_observed_ns = self.clock.time_ns()
        self.last_generation = status["last_generation"]
        if ledger_problems(config, self.paths, clock=self.clock):
            raise Stop("login_ledger_not_clean")
        problem = run_ledger_problem(self.paths)
        if problem is not None:
            raise Stop(problem.partition(":")[0])
        if self.mode == "production" and not self.dry_run:
            self.check_day_allows()
        if first and not self.first_start_done:
            self.dns_wait()
        return None

    def check_day_allows(self):
        """The durable day file allows another attempt: none unfinished, the day open (§4.3 step 1)."""
        fresh = self.load_day(self.day_path) if self.day_path.exists() else None
        if fresh is not None and fresh != self.day:
            raise Stop("day_file_changed")
        if any(entry["state"] == "reserved" for entry in self.day["attempts"]):
            raise Stop("unfinished_attempt")
        if self.day["state"] != "open":
            raise Stop("day_already_closed", state=self.day["state"])

    def dns_wait(self):
        """Optional, first start of the day only: wait up to about 2 min for a local DNS answer for the API host; start
        anyway when it never comes. No Betfair request, no budget."""
        deadline = self.clock.time_ns() + DNS_WAIT_NS
        while not self.sys.resolves(DNS_HOST) and self.clock.time_ns() < deadline and self.signalled is None:
            self.clock.sleep(10)

    # -- one attempt --

    def child_argv(self, end_ns, invocation_id):
        return [self.sys.interpreter()[0], "-E", "-s", "-m", "betangel_research.betfair", "record",
                "--require-delayed-key", "--end-at", utc_iso(end_ns), "--invocation-id", invocation_id]

    def invocation_id(self, day, ordinal):
        return f"d{day.strftime('%Y%m%d')}-a{ordinal}-{secrets.token_hex(4)}"

    def find_partial(self, invocation_id):
        root = (Path(self.config["recorder"]["root"]) if self.config["recorder"]["root"] is not None
                else Path(self.paths.recordings_dir))
        try:
            entries = sorted(root.iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.name.startswith("rec-") or entry.is_symlink() or not entry.is_dir():
                continue
            path = entry / "receipt.partial.json"
            if path.is_symlink() or not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(value, dict) and value.get("invocation_id") == invocation_id:
                return value
        return None

    def post_start_mismatch(self, partial):
        """The first field of the session's receipt.partial.json that differs from what was pinned (§4.3 step 5)."""
        freshness = partial.get("data_freshness") if isinstance(partial.get("data_freshness"), dict) else {}
        expected = {"key_type": (partial.get("key_type"), "delayed"),
                    "label": (freshness.get("label"), "delayed_conflated"),
                    "git_revision": (partial.get("git_revision"), self.pinned["commit"]),
                    "working_tree_dirty": (partial.get("working_tree_dirty"), False),
                    "code_sha256": (partial.get("code_sha256"), self.pinned["code_sha256"]),
                    "python": (partial.get("python"), self.pinned["python"]),
                    "config_sha256": (partial.get("config_sha256"), self.config_sha)}
        for field in POST_START_FIELDS:
            found, wanted = expected[field]
            if found != wanted:
                return field
        return None

    def run_child(self, argv, invocation_id, spawn_ns):
        """Spawn, the post-start check, the wait -> (child, mismatch field or None)."""
        stderr_path = self.logs / f"child-{invocation_id}.stderr"
        child = self.child = self.sys.spawn(argv, cwd=Path(self.pinned["worktree"]), stderr_path=stderr_path)
        if self.signalled is not None and not self.forwarded:
            self.forwarded = True
            child.terminate()
        mismatch, checked = None, False
        while child.poll() is None:
            if not checked:
                partial = self.find_partial(invocation_id)
                if partial is not None:
                    checked = True
                    mismatch = self.post_start_mismatch(partial)
                elif self.clock.time_ns() - spawn_ns >= POST_START_TIMEOUT_NS:
                    checked, mismatch = True, "no_partial"
                if mismatch is not None:
                    self.log("post_start_mismatch", field=mismatch, invocation_id=invocation_id)
                    if not self.forwarded:
                        self.forwarded = True
                        child.terminate()
                    break
            self.clock.sleep(CHILD_POLL_S)
        child.wait()
        self.child = None
        return child, mismatch

    def classify(self, invocation_id, spawn_ns, child):
        """§4.4: the disk outcome, then the touched gate (rule 1) -> (decision, entry fields)."""
        try:
            after = admission_status(self.paths)
        except Exception:
            after = None
        disk = classify_invocation(self.paths, self.config, invocation_id, spawn_ns)
        fields = {"outcome": disk["outcome"], "stop_reason": disk.get("stop_reason"), "session_id": disk["session_id"],
                  "codes": list(disk["codes"]), "refusal_code": None, "receipt_sha256": disk.get("receipt_sha256")}
        if after is None:
            fields["codes"].append("admission_state_unknown")
            return "review", fields
        present = after["inhibit"] or after["admission"] is not None
        touched = present or after["last_generation"] > (self.last_generation or 0)
        fields["touched"] = touched
        if not present:
            self.release_observed_ns = self.clock.time_ns()     # "the release was observed": the post-exit read
        else:
            self.release_observed_ns = None
        outcome = disk["outcome"]
        if outcome == "finalized":
            stop = disk["stop_reason"]
            if stop in DONE_STOPS:
                return "done", fields
            if stop in RESTART_STOPS:
                return "restart", fields
            if stop == "stop_requested" and touched:
                return "wait", fields                                     # rule 1 (a)
            return "review", fields
        if outcome == "no_session":
            fields["refusal_code"] = child.refused_outcome                # diagnostic only (rule 2)
            if touched and (child.refused_outcome == "inhibited" or child.stop_before_start):
                return "wait", fields                                     # rule 1 (b)
            fields["outcome"] = "preflight_refusal"
            return "review", fields
        return "review", fields                                          # rules 2 (attributed), 3, 5, 6

    def index_days(self, spawn_ns, session_id):
        """verify-day and recordings index for each UTC day the session spans, as pinned-CLI subprocesses."""
        if session_id is None:
            return
        first = datetime.fromtimestamp(spawn_ns // NS, tz=timezone.utc).date()
        last = datetime.fromtimestamp(self.clock.time_ns() // NS, tz=timezone.utc).date()
        day = first
        while day <= last:
            text = day.isoformat()
            base = [self.sys.interpreter()[0], "-E", "-s", "-m", "betangel_research.betfair"]
            verified = self.sys.run(base + ["verify-day", "--day", text], cwd=Path(self.pinned["worktree"]))
            indexed = self.sys.run(base + ["recordings", "index", "--day", text], cwd=Path(self.pinned["worktree"]))
            self.log("day_checked", day=text, verify_day_exit=verified, index_exit=indexed)
            day += timedelta(days=1)

    # -- production -----------------------------------------------------------------------------------------------

    def next_kind_on_entry(self):
        """Re-entry on the same London date (§4.6) -> the next attempt's kind; Stop when nothing may start."""
        attempts = self.day["attempts"]
        reserved = [entry for entry in attempts if entry["state"] == "reserved"]
        if reserved:
            self.close_day("stopped_for_review", "unfinished_attempt", reserved[0]["invocation_id"])
            self.write_day()
            raise Stop("unfinished_attempt", invocation_id=reserved[0]["invocation_id"])
        if self.day["state"] != "open":
            raise Stop("day_already_closed", state=self.day["state"])
        if not attempts:
            return "first"
        last = attempts[-1]
        kind = self.kind_after(last["decision"])
        if kind is None:
            self.close_day(self.state_for(last["decision"]), last.get("close_code") or last["outcome"],
                           last["invocation_id"])
            self.write_day()
            raise Stop("day_already_closed", state=self.day["state"])
        return kind

    @staticmethod
    def kind_after(decision):
        return {"restart": "restart", "wait": "resumption"}.get(decision)

    @staticmethod
    def state_for(decision):
        return {"done": "done", "window_exhausted": "window_exhausted"}.get(decision, "stopped_for_review")

    def wait_for_start(self, kind, end_ns):
        """§4.5, §4.6: wait (wall time, polling) until a child of this kind may start -> None; Stop otherwise."""
        while True:
            if self.signalled is not None:
                raise Stop("wrapper_signalled")
            now = self.clock.time_ns()
            if end_ns - now < WINDOW_FLOOR_NS:
                raise Stop("window_exhausted", state="window_exhausted")
            if self.mode == "production":
                if kind == "restart" and self.day["counts"]["restart"] >= MAX_RESTARTS:
                    raise Stop("restart_cap_reached")
                if kind == "resumption" and self.day["counts"]["resumption"] >= MAX_RESUMPTIONS:
                    raise Stop("resumption_cap_reached")
            if self.preflight(first=kind == "first") == "wait":
                self.log("waiting_for_release")
                self.clock.sleep(ADMISSION_POLL_S)
                continue
            now = self.clock.time_ns()
            if end_ns - now < WINDOW_FLOOR_NS:
                raise Stop("window_exhausted", state="window_exhausted")
            last = self.day["attempts"][-1] if self.mode == "production" and self.day["attempts"] else None
            waits = []
            if kind == "restart" and last is not None:
                waits.append(last["spawn_wall_ns"] + RESTART_SPACING_NS - now)
            if kind in ("restart", "resumption") and self.release_observed_ns is not None and (
                    kind == "resumption" or last is not None and last.get("decision") == "restart"
                    and last.get("touched")):
                waits.append(self.release_observed_ns + RESUMPTION_SPACING_NS - now)
            wait_ns = max(waits, default=0)
            if wait_ns > 0:
                self.clock.sleep(min(wait_ns / NS, ADMISSION_POLL_S))
                continue
            return

    def attempt(self, kind, end_ns, day):
        """Reserve (production), spawn, check, wait, classify and finish one attempt -> its decision."""
        ordinal = len(self.day["attempts"]) + 1 if self.mode == "production" else 1
        invocation_id = self.invocation_id(day, ordinal)
        spawn_ns = self.clock.time_ns()
        entry = {"kind": kind, "kind_count": None, "invocation_id": invocation_id, "spawn_wall_ns": spawn_ns,
                 "state": "reserved", "outcome": None, "stop_reason": None, "refusal_code": None, "decision": None,
                 "codes": [], "session_id": None, "receipt_sha256": None, "touched": None}
        if self.mode == "production":
            self.day["counts"][kind] += 1
            entry["kind_count"] = self.day["counts"][kind]
            self.day["attempts"].append(entry)
            try:
                self.write_day()                                  # RV7-1: charged before the spawn, never refunded
            except DayFileWriteFailed:
                raise Stop("day_file_write_failed", invocation_id=invocation_id) from None
        else:
            entry["kind_count"] = 1
            self.record = {"schema_version": DAY_SCHEMA, "date": day.isoformat(), "mode": "attended", "attempt": entry}
            atomic_write_json(self.record_path_for(day, invocation_id), self.record)
        self.first_start_done = True
        self.log("attempt_reserved", kind=kind, invocation_id=invocation_id)
        if self.signalled is not None:
            self.finish(entry, "review", {"outcome": "wrapper_signalled_before_spawn"},
                        close=("stopped_for_review", "wrapper_signalled_before_spawn"))
            raise Stop("wrapper_signalled_before_spawn", invocation_id=invocation_id)
        argv = self.child_argv(end_ns, invocation_id)
        child, mismatch = self.run_child(argv, invocation_id, spawn_ns)
        if mismatch is not None:
            decision, fields = "review", {"outcome": f"post_start_mismatch:{mismatch}", "codes": [mismatch]}
            try:
                disk = classify_invocation(self.paths, self.config, invocation_id, spawn_ns)
                fields["session_id"] = disk["session_id"]
            except Exception:
                pass
        else:
            decision, fields = self.classify(invocation_id, spawn_ns, child)
        close = self.closing(decision, fields, end_ns)
        self.finish(entry, decision, fields, close=close)
        self.index_days(spawn_ns, fields.get("session_id"))
        if close is not None:
            raise Stop(close[1], state=close[0], invocation_id=invocation_id)
        return decision

    def closing(self, decision, fields, end_ns):
        """The day state this outcome sets in the finish write itself (§4.3 step 6), or None when the day stays open."""
        code = fields.get("outcome")
        if self.mode == "attended":
            return ("done" if decision == "done" else "stopped_for_review", code if decision != "done" else "done")
        if decision == "done":
            return ("done", fields.get("stop_reason") or "done")
        if decision == "review":
            return ("stopped_for_review", code if not fields.get("stop_reason") else
                    f"{code}:{fields['stop_reason']}")
        if decision == "restart":
            if ledger_problems(self.config, self.paths, clock=self.clock):
                return ("stopped_for_review", "login_ledger_not_clean")
            if self.day["counts"]["restart"] >= MAX_RESTARTS:
                return ("stopped_for_review", "restart_cap_reached")
            if end_ns - self.clock.time_ns() < WINDOW_FLOOR_NS:
                return ("window_exhausted", "window_exhausted")
            return None
        if decision == "wait":
            if self.day["counts"]["resumption"] >= MAX_RESUMPTIONS:
                return ("stopped_for_review", "resumption_cap_reached")
            if end_ns - self.clock.time_ns() < WINDOW_FLOOR_NS:
                return ("window_exhausted", "window_exhausted")
            return None
        return ("stopped_for_review", code)

    def finish(self, entry, decision, fields, *, close):
        """One write: the entry finished with its outcome and, when close is given, the day's state (§4.3 step 6).
        A failed write starts nothing more; whichever atomic version is on disk stays (RV8-2)."""
        entry.update({key: value for key, value in fields.items() if key in entry})
        entry["state"] = "finished"
        entry["decision"] = decision
        if close is not None:
            entry["close_code"] = close[1]
        self.log("attempt_finished", invocation_id=entry["invocation_id"], decision=decision,
                 outcome=entry["outcome"], stop_reason=entry["stop_reason"], refusal_code=entry["refusal_code"])
        if self.mode == "attended":
            atomic_write_json(self.record_path, self.record)
            return
        summary = self.day["summary"]
        summary["outcomes"].append(entry["outcome"] if entry["stop_reason"] is None
                                   else f"{entry['outcome']}:{entry['stop_reason']}")
        if entry["outcome"] == "finalized" and entry["receipt_sha256"]:
            summary["finalized"].append({"session_id": entry["session_id"], "receipt_sha256": entry["receipt_sha256"]})
        elif entry["session_id"] is not None:
            summary["review"].append({"session_id": entry["session_id"], "outcome": entry["outcome"]})
        if close is not None:
            self.close_day(close[0], close[1], entry["invocation_id"])
        try:
            self.write_day()
        except DayFileWriteFailed:
            raise Stop("day_file_write_failed", invocation_id=entry["invocation_id"]) from None

    def record_path_for(self, day, invocation_id):
        self.record_path = self.dir / f"{day.isoformat()}-attended-{invocation_id}.json"
        return self.record_path

    # -- the run --------------------------------------------------------------------------------------------------

    def run(self):
        previous = self.sys.install_signals(self.handler)
        try:
            return self._run()
        finally:
            self.sys.restore_signals(previous)

    def _run(self):
        now = self.clock.time_ns()
        day = london_date(now)
        if self.mode == "attended":
            return self.run_attended(day)
        self.day_path = self.dir / f"{day.isoformat()}.json"
        try:
            loaded = self.load_day(self.day_path)
        except DayFileError:
            self.log("stopped", code="day_file_unreadable", date=day.isoformat())
            return 2
        self.day = loaded if loaded is not None else self.new_day(day)
        end_ns = end_instant_ns(day)
        if self.dry_run:
            return self.dry(day, end_ns)
        try:
            kind = self.next_kind_on_entry()
            bound = start_bound_ns(day)
            while self.clock.time_ns() < bound:                    # a fire coalesced past midnight waits for 07:00
                if self.signalled is not None:
                    raise Stop("wrapper_signalled")
                self.clock.sleep(min((bound - self.clock.time_ns()) / NS, ADMISSION_POLL_S))
            while True:
                self.wait_for_start(kind, end_ns)
                decision = self.attempt(kind, end_ns, day)
                kind = self.kind_after(decision)
        except Stop as stop:
            return self.stopped(stop)
        except DayFileWriteFailed:
            self.log("stopped", code="day_file_write_failed")
            return 2

    def stopped(self, stop):
        """Log the closing code; write the day's state in its own write when nothing has written it yet."""
        if self.mode == "production" and self.day is not None and self.day["state"] == "open" and stop.code not in (
                "day_file_write_failed", "day_already_closed", "day_file_changed"):
            self.close_day(stop.state, stop.code, stop.invocation_id)
            try:
                self.write_day()
            except DayFileWriteFailed:
                self.log("stopped", code="day_file_write_failed")
                return 2
        if self.mode == "production" and self.day is not None:
            self.log("daily_summary", date=self.day["date"], state=self.day["state"], close_code=self.day["close_code"],
                     counts=self.day["counts"], outcomes=self.day["summary"]["outcomes"],
                     finalized=self.day["summary"]["finalized"])
        self.log("stopped", code=stop.code, state=stop.state, invocation_id=stop.invocation_id)
        return 0 if stop.state in ("done", "window_exhausted") or stop.code == "day_already_closed" else 2

    def run_attended(self, day):
        end_ns = self.attended_end_ns
        try:
            if end_ns - self.clock.time_ns() < WINDOW_FLOOR_NS:
                raise Stop("end_at_too_soon")
            if self.dry_run:
                return self.dry(day, end_ns)
            self.wait_for_start("first", end_ns)
            self.attempt("first", end_ns, day)
        except Stop as stop:
            return self.stopped(stop)
        return 0

    def dry(self, day, end_ns):
        """--dry-run: every preflight read, then the plan as codes; no child, no write."""
        kind = None
        try:
            if self.mode == "production":
                kind = self.dry_entry_kind()
            else:
                kind = "first"
            result = self.preflight(first=False)
            preflight = "wait" if result == "wait" else "ok"
        except Stop as stop:
            preflight = stop.code
        interpreter, version = self.sys.interpreter()
        self.log("dry_run", date=day.isoformat(), next_kind=kind, preflight=preflight, end_at=utc_iso(end_ns),
                 interpreter=interpreter, python=version,
                 argv=self.child_argv(end_ns, self.invocation_id(day, len(self.day["attempts"]) + 1
                                                                 if self.day else 1)))
        return 0 if preflight in ("ok", "wait") else 2

    def dry_entry_kind(self):
        attempts = self.day["attempts"]
        if any(entry["state"] == "reserved" for entry in attempts):
            raise Stop("unfinished_attempt")
        if self.day["state"] != "open":
            raise Stop("day_already_closed")
        return "first" if not attempts else (self.kind_after(attempts[-1]["decision"]) or "none")


# --- the command line ---------------------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -E -s -m betangel_research.betfair.recording_day",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--launchd", action="store_true", help="the production plist's run (the day file)")
    parser.add_argument("--attended", action="store_true", help="a hand start or the test label: its own record")
    parser.add_argument("--end-at", dest="end_at", help="UTC instant; only with --attended")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    return parser


def main(argv=None, *, paths=None, system=None):
    """-> exit code: 0 for a day done, window_exhausted, already closed or a clean dry run; 2 for a refusal or a stop
    for review."""
    args = build_parser().parse_args(argv)
    out = (system or System())
    if args.launchd and args.attended:
        out.log(json.dumps({"event": "refused", "code": "attended_with_launchd"}))
        return 2
    if args.end_at is not None and not args.attended:
        out.log(json.dumps({"event": "refused", "code": "end_at_needs_attended"}))
        return 2
    if not (args.launchd or args.attended):
        out.log(json.dumps({"event": "refused", "code": "launchd_or_attended_required"}))
        return 2
    end_ns = None
    if args.attended:
        if args.end_at is None:
            out.log(json.dumps({"event": "refused", "code": "attended_needs_end_at"}))
            return 2
        try:
            end_ns = parse_end_at(args.end_at)
        except ValueError:
            out.log(json.dumps({"event": "refused", "code": "end_at_invalid"}))
            return 2
    paths = paths or StatePaths(profile=RECORDER_PROFILE)
    wrapper = Wrapper(paths, mode="attended" if args.attended else "production", end_at_ns=end_ns,
                      dry_run=args.dry_run, system=system)
    return wrapper.run()


if __name__ == "__main__":
    sys.exit(main())
