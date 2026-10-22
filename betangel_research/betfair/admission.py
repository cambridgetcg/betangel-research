"""Recording admission: one recorder at a time, and no recorder while live work is admitted.

Recording design v3 section 2, as amended by v4 sections A and E and v5 (RD2-1, RD2-3, RD3-1). Every file
below lives in the profile's recordings directory, outside every checkout, so every worktree of this
profile sees the same state:

- admission.lock: an flock held only for the few milliseconds of each transition here.
- INHIBIT: present means no recorder may start. Only release() removes it; any directory entry of
  that name counts, as for the KILL file.
- ADMISSION: {schema_version, generation, owner, purpose, created_at, state}, written atomically under
  the lock. At most one live owner at a time, and INHIBIT is present whenever ADMISSION is. state is
  "pending" from the write until the drain and the step 8 check have passed, then "admitted"; a
  generation whose admit() died or was interrupted in between stays pending, and only a pending
  continuation that finishes both checks is admitted.
- admission-generation.json: the last generation issued, so generations only ever grow.
- recorder.pid: {schema_version, pid, start_time, invocation_id, started_at} of the running recorder,
  written under the lock and removed by that recorder before it lets go of .recorder.lock.
- .recorder.lock: the recorder's existing lock, held by every recorder for its whole run, its
  network activity and cleanup included.

Recorder start (recorder_admission; cmd_record enters it before any credential read or login, and
StreamRecorder.run enters it when not handed one): under admission.lock, refuse (outcome
"inhibited") while INHIBIT or ADMISSION exists, and refuse while the state cannot be read; take
.recorder.lock without waiting (outcome "recorder_running" when held); write recorder.pid; release
admission.lock. This holds however the recorder is started.

Live admission (admit): under the lock, refuse an ADMISSION held by a different owner ("admission
held by <owner>, generation N"); else create INHIBIT and write ADMISSION N+1 as pending, or let the same
owner continue its generation N. Then, outside the lock, drain any recorder: try .recorder.lock without
waiting; if it is held, check the pid in recorder.pid against that process's start time (pid reuse or
a mismatch refuses, never signalling a stranger), send it SIGTERM and wait, bounded, until the lock
is free. Then the step 8 login-ledger rule (auth.ledger_review), and ADMISSION becomes admitted under
the lock. Only then is the generation returned. Only an admitted generation's cleanup continuation
skips step 8. A refused pending generation takes its own ADMISSION back and leaves INHIBIT set.

Release: under the lock, only for the generation in force, and only when the live state is clean (the
live journal folded read-only: every order terminal or EXECUTION_COMPLETE with size_remaining_p 0, no
active HALT, no halted or unresolved market (stricter than design v4 section E), no KILL file; a
journal that is a symlink, not a regular file or cannot be inspected fails closed) and the step 8 rule
passes; then ADMISSION and INHIBIT are removed. An older generation can never release a newer one.

One ownership lifecycle (P1-08 §3.4; Astra P108-4 to P108-6). Every owning command admits through
live_ownership and releases through release_own, never by calling admit or release itself (pinned):
- .live-owner.lock, taken with LOCK_NB before any admission, credential read or login and held for the
  command's whole life, through its final logout and its own release. Busy refuses owner_live, an error
  owner_state_unknown, with nothing written; the kernel lets go of it when the process dies, so holding it
  proves the previous owner's process is gone. Lock order: the owner lock, then admission.lock (inside
  admit and release); .gateway.lock is never taken while admission.lock is held.
- Three phases under that one lock: the owner lock; a read-only before-admit phase (the caller's checks,
  which may refuse with nothing changed: no ADMISSION or INHIBIT written, no recorder drained); then the
  admission itself. A fresh ticket refuses any ADMISSION in force (admission_held) and notes whether INHIBIT
  was already set. A restricted continuation names its generation explicitly, needs it admitted, takes the
  retained owner's admission back (INHIBIT restored, any recorder drained) with cleanup chosen per command
  (only reconcile's cleanup skips step 8), never places and never releases.
- release_own releases only a fresh ticket whose INHIBIT did not predate it, under the extra checks that
  this process still holds the owner lock and that ADMISSION still names exactly its owner and generation.
owner_lock_state is the probe for a process that does not hold the lock (a lock this process holds would
conflict with itself, so an owner uses its ticket's holds() instead).

The emergency kill never calls anything here, and nothing here takes .gateway.lock.
"""

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

from .auth import ledger_review
from .errors import AdmissionRefused, AuthError, BetfairError, JournalError, LockBusy, RecorderRunning, safe_code
from .journal import read_only_fold
from .paths import file_lock, kill_engaged
from .receipts import SYSTEM_CLOCK, atomic_write_json, parse_utc_ns, strict_json_loads
from .schemas import TERMINAL_STATES

SCHEMA_VERSION = 1
# admission.lock is held for milliseconds; a holder this slow means something is stuck, so refuse.
ADMISSION_LOCK_TIMEOUT_S = 10
# How long admission waits for a SIGTERMed recorder to let go of .recorder.lock (design v3: "for example 90 s").
DRAIN_TIMEOUT_S = 90
DRAIN_POLL_S = 0.5
OWNER_RE = re.compile(r"[a-z][a-z0-9-]{0,31}:[A-Za-z0-9][A-Za-z0-9._-]{0,63}")   # pilot-session:<id>, manual:<pid>
PURPOSE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:,()/-]{0,119}")
INVOCATION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
ADMISSION_KEYS = frozenset({"schema_version", "generation", "owner", "purpose", "created_at", "state"})
ADMISSION_STATES = ("pending", "admitted")
GENERATION_KEYS = frozenset({"schema_version", "generation", "updated_at"})
PID_KEYS = frozenset({"schema_version", "pid", "start_time", "invocation_id", "started_at"})
PS_BIN = "/bin/ps"


class ProcessTable:
    """The process facts admission needs. Tests pass a fake with the same three methods."""

    def current_pid(self):
        return os.getpid()

    def start_time(self, pid):
        """Opaque text naming when process pid started (it differs once the pid is reused), or None when no
        such process exists or its start cannot be read."""
        if type(pid) is not int or pid <= 0:
            return None
        if sys.platform.startswith("linux"):
            return _proc_stat_start(pid)
        return _ps_start(pid)

    def terminate(self, pid):
        os.kill(pid, signal.SIGTERM)


def _proc_stat_start(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        fields = text[text.rindex(")") + 2:].split()
        return "proc-stat-starttime:" + fields[19]           # field 22 of proc(5), counted after "(comm) "
    except (OSError, ValueError, IndexError):
        return None


def _ps_start(pid):
    try:
        # lstart is printed in the zone of TZ (else /etc/localtime): pin UTC, so a change of the Mac's zone
        # between the recorder's start and an admission never turns the same process into a "stranger".
        result = subprocess.run([PS_BIN, "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                timeout=5, env={"LC_ALL": "C", "TZ": "UTC0", "PATH": "/bin:/usr/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join((result.stdout or "").split())
    return "ps-lstart:" + text if result.returncode == 0 and text else None


SYSTEM_PROCESSES = ProcessTable()


# --- state files -------------------------------------------------------------------------------------


def check_invocation_id(value):
    """value when None or a safe invocation id ([A-Za-z0-9][A-Za-z0-9._:-]{0,63}); else ValueError."""
    if value is not None and (not isinstance(value, str) or not INVOCATION_ID_RE.fullmatch(value)):
        raise ValueError("invocation_id_invalid")
    return value


def _present(path):
    """lstat, fail closed: only a definite "no such file" means absent (as paths.kill_engaged)."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _load(path, keys, check):
    """The JSON object in path, or None when absent; AdmissionRefused(admission_state_unknown) when it
    cannot be read, is not strict JSON, lacks exactly these keys or fails check."""
    try:
        if path.is_symlink():
            raise OSError("symlink")
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise AdmissionRefused("admission_state_unknown", detail=path.name) from None
    try:
        value = strict_json_loads(data.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != keys or value.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("shape")
        check(value)
    except ValueError:
        raise AdmissionRefused("admission_state_unknown", detail=path.name) from None
    return value


def _positive_int(value):
    if type(value) is not int or value < 1:
        raise ValueError("not a positive integer")


def _check_admission(value):
    _positive_int(value["generation"])
    if not isinstance(value["owner"], str) or not OWNER_RE.fullmatch(value["owner"]):
        raise ValueError("owner")
    if not isinstance(value["purpose"], str) or not PURPOSE_RE.fullmatch(value["purpose"]):
        raise ValueError("purpose")
    if value["state"] not in ADMISSION_STATES:
        raise ValueError("state")
    parse_utc_ns(value["created_at"])


def _check_generation(value):
    _positive_int(value["generation"])
    parse_utc_ns(value["updated_at"])


def _check_pid(value):
    _positive_int(value["pid"])
    if not isinstance(value["start_time"], str) or not value["start_time"]:
        raise ValueError("start_time")
    check_invocation_id(value["invocation_id"])
    parse_utc_ns(value["started_at"])


def _state(paths):
    """{"inhibit", "admission", "last_generation"}; AdmissionRefused(admission_state_unknown) when unreadable."""
    admission = _load(paths.admission_file, ADMISSION_KEYS, _check_admission)
    counter = _load(paths.admission_generation_file, GENERATION_KEYS, _check_generation)
    last = max(counter["generation"] if counter else 0, admission["generation"] if admission else 0)
    return {"inhibit": _present(paths.inhibit_file), "admission": admission, "last_generation": last}


def _read_pid(paths):
    """recorder.pid, or None when it is absent or unreadable (a recorder between writes, or an older one)."""
    try:
        return _load(paths.recorder_pid_file, PID_KEYS, _check_pid)
    except AdmissionRefused:
        return None


def _write_inhibit(paths, *, set_by, generation, clock):
    atomic_write_json(paths.inhibit_file, {"schema_version": SCHEMA_VERSION, "reason": "live_admission",
                                           "set_by": set_by, "generation": generation, "set_at": clock.utc_now()})


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        raise AdmissionRefused("admission_state_unknown", detail=Path(path).name) from None


def _fsync_dir(directory):
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def _admission_lock(paths, clock):
    stack = ExitStack()
    try:
        stack.enter_context(file_lock(paths.admission_lock, timeout_s=ADMISSION_LOCK_TIMEOUT_S, clock=clock))
    except LockBusy:
        raise AdmissionRefused("admission_lock_busy") from None
    with stack:
        yield


def recorder_lock_held(paths, *, clock=SYSTEM_CLOCK):
    """True while some process holds .recorder.lock (tried with LOCK_NB, released at once if free)."""
    try:
        with file_lock(paths.recorder_lock, timeout_s=0, clock=clock):
            return False
    except LockBusy:
        return True


def recorder_pid_status(paths):
    """recorder.pid for status, in one strict read: "absent" only when the file does not exist, "unreadable" when it
    exists but cannot be read or parsed (a symlink, the wrong shape), else its record. _read_pid's None for both stays
    as it is for the drain."""
    try:
        record = _load(paths.recorder_pid_file, PID_KEYS, _check_pid)
    except AdmissionRefused:
        return "unreadable"
    return "absent" if record is None else record


def admission_status(paths):
    """Read-only view for status lines and the release prompt: no lock taken, nothing written or probed.
    Raises AdmissionRefused(admission_state_unknown) when a state file cannot be read."""
    state = _state(paths)
    return {"inhibit": state["inhibit"], "admission": state["admission"], "last_generation": state["last_generation"],
            "recorder_pid": _read_pid(paths)}


# --- recorder side -----------------------------------------------------------------------------------


class RecorderTicket:
    """What recorder_admission yields: the held .recorder.lock and the recorder.pid it wrote."""

    def __init__(self, held, record):
        self._held = held
        self.record = dict(record)

    @property
    def invocation_id(self):
        return self.record["invocation_id"]

    def holds(self, paths):
        """True while this ticket's lock is still the one at paths.recorder_lock."""
        return Path(self._held.path) == Path(paths.recorder_lock) and self._held.held()


def _remove_own_pid(paths, record):
    """Remove recorder.pid while .recorder.lock is still held, only if it is still this recorder's."""
    try:
        if _read_pid(paths) == record:
            os.unlink(paths.recorder_pid_file)
    except OSError:
        pass


@contextmanager
def recorder_admission(paths, *, invocation_id=None, clock=SYSTEM_CLOCK, processes=None):
    """C5 steps 2-6 for one recorder run; yields a RecorderTicket and holds .recorder.lock until exit.

    Raises AdmissionRefused: "inhibited" (INHIBIT or ADMISSION present), "admission_state_unknown",
    "admission_lock_busy", "recorder_identity_unknown" (this process's start time cannot be read, so
    admission could never check it) or RecorderRunning ("recorder_running", also a LockBusy). Nothing
    here reads a credential or touches the network.
    """
    check_invocation_id(invocation_id)
    processes = processes if processes is not None else SYSTEM_PROCESSES
    pid = processes.current_pid()
    start_time = processes.start_time(pid)
    if not start_time:
        raise AdmissionRefused("recorder_identity_unknown")
    paths.ensure()
    with ExitStack() as stack:
        with _admission_lock(paths, clock):
            state = _state(paths)
            if state["inhibit"] or state["admission"] is not None:
                current = state["admission"] or {}
                raise AdmissionRefused("inhibited", owner=current.get("owner"), generation=current.get("generation"))
            try:
                held = stack.enter_context(file_lock(paths.recorder_lock, timeout_s=0, clock=clock))
            except LockBusy:
                raise RecorderRunning("recorder_running") from None
            record = {"schema_version": SCHEMA_VERSION, "pid": pid, "start_time": start_time,
                      "invocation_id": invocation_id, "started_at": clock.utc_now()}
            atomic_write_json(paths.recorder_pid_file, record)
            stack.callback(_remove_own_pid, paths, record)       # runs before the lock is let go
        yield RecorderTicket(held, record)


# --- live side ---------------------------------------------------------------------------------------


def _check_owner(owner, purpose):
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        raise ValueError("admission_owner_invalid")
    if purpose is not None and (not isinstance(purpose, str) or not PURPOSE_RE.fullmatch(purpose)):
        raise ValueError("admission_purpose_invalid")


def _drain(paths, *, clock, processes, timeout_s):
    """"none" when no recorder holds .recorder.lock, else "stopped" once the SIGTERMed recorder let go of it.

    The pid is signalled only when recorder.pid names a live process whose start time is the recorded
    one; pid reuse or any mismatch refuses (recorder_pid_mismatch) rather than signal a stranger. A
    lock held with no readable recorder.pid (an older recorder) is waited for, never signalled."""
    if not recorder_lock_held(paths, clock=clock):
        return "none"
    deadline = clock.monotonic_ns() + int(timeout_s * 1_000_000_000)
    signalled = False
    seen_pid = False
    while True:
        if not recorder_lock_held(paths, clock=clock):
            return "stopped"
        record = None if signalled else _read_pid(paths)
        if record is not None:
            seen_pid = True
            if processes.start_time(record["pid"]) != record["start_time"]:
                if not recorder_lock_held(paths, clock=clock):
                    return "stopped"                  # it exited meanwhile
                raise AdmissionRefused("recorder_pid_mismatch")
            try:
                processes.terminate(record["pid"])
            except ProcessLookupError:
                pass
            except OSError:
                raise AdmissionRefused("recorder_signal_failed") from None
            signalled = True
        if clock.monotonic_ns() >= deadline:
            raise AdmissionRefused("recorder_did_not_stop" if seen_pid else "recorder_lock_held_without_pid")
        clock.sleep(DRAIN_POLL_S)


def ledger_problems(config, paths, *, clock=SYSTEM_CLOCK):
    """Step 8 (v4 section A, v5 RD2-3): problem codes from auth.ledger_review, [] when clean."""
    try:
        review = ledger_review(config, paths, clock=clock)
    except AuthError:
        return ["login_ledger_unreadable"]
    problems = []
    if review["banned_until"] is not None:
        problems.append("login_banned")
    if review["unresolved_attempts_after_barrier"]:
        problems.append("login_attempt_unresolved")
    if review["consecutive_failures"] or review["failures_after_barrier"]:
        problems.append("login_failure_after_barrier")      # auth counts an old unresolved attempt as one too
    return problems


def live_state_problems(paths, *, clock=SYSTEM_CLOCK):
    """Release's clean-state rule (v4 section E, v5 RD2-3) as problem codes, [] when clean.

    The live journal is folded read-only (journal.read_only_fold, sqlite mode=ro); a busy or unreadable
    one fails closed. Every order must be terminal, or EXECUTION_COMPLETE with size_remaining_p 0 (so no
    SUBMITTING, UNKNOWN, ABSENT_UNCONFIRMED, CANCEL_PENDING or other open order); no folded HALT may be
    active (a kill row a resume has since cleared does not count) and no market may be halted or
    unresolved (market_unresolved: a market-scope halt or an unresolved order there; stricter than v4
    section E); and there must be no KILL file. Only a definite absence of the journal's directory entry
    means no orders: a symlink, a non-regular file or a path that cannot be inspected is unreadable."""
    problems = ["kill_file_engaged"] if kill_engaged(paths) else []
    try:
        state = read_only_fold(paths.journal_path("live"), mode="live", now_ns=clock.time_ns())
    except JournalError:
        return problems + ["live_journal_unreadable"]
    if state is None:
        return problems
    if state["halted"]:
        problems.append("halt_active")
    open_states = sorted({order["state"] for order in state["orders"].values()
                          if order["state"] not in TERMINAL_STATES
                          and not (order["state"] == "EXECUTION_COMPLETE" and order["size_remaining_p"] == 0)})
    problems += ["order_" + name.lower() for name in open_states]
    if state["unresolved_markets"]:
        problems.append("market_unresolved")
    return problems


def _withdraw(paths, record, clock):
    """Take back a refused pending generation's own ADMISSION (INHIBIT stays). Best effort: if this fails the
    ADMISSION stays pending, which only keeps recording off until a continuation or an explicit release."""
    try:
        with _admission_lock(paths, clock):
            if _state(paths)["admission"] == record:
                _unlink(paths.admission_file)
                _fsync_dir(paths.recordings_dir)
    except AdmissionRefused:
        pass


def _grant(paths, record, clock):
    """Under the lock, turn this pending ADMISSION into admitted; AdmissionRefused(admission_changed) if it is no
    longer the one this call wrote or continued (released or replaced meanwhile)."""
    with _admission_lock(paths, clock):
        if _state(paths)["admission"] != record:
            raise AdmissionRefused("admission_changed")
        granted = dict(record, state="admitted")
        atomic_write_json(paths.admission_file, granted)
    return granted


def admit(paths, *, owner, purpose, config, generation=None, cleanup=False, clock=SYSTEM_CLOCK, processes=None,
          drain_timeout_s=DRAIN_TIMEOUT_S):
    """Admit one live owner before it logs in; returns {"generation", "owner", "purpose", "continued", "state",
    "ledger_checked", "recorder"}. The caller keeps the generation for the whole live command or session and its
    cleanup, and gives it to release() at its orderly end.

    owner is "<kind>:<id>" (pilot-session:<id>, pilot-capture:<pid>, manual:<pid>); purpose is a short safe
    text. With generation None this is a new admission: refused (admission_held) while any ADMISSION
    exists, whoever owns it; otherwise INHIBIT and a pending ADMISSION N+1 are written under the lock,
    any recorder is drained, the step 8 ledger rule must pass, and only then is ADMISSION marked
    admitted. With generation N the same owner continues its own admission (continued True): a
    different owner is refused (admission_held), and so is any generation but the one in force
    (admission_generation_mismatch, or admission_not_held once it was released); INHIBIT is restored if
    missing and any recorder drained. A pending generation (its first admit died or was interrupted
    after the write) must pass step 8 like a new one before it is admitted. Only an admitted
    generation continuing with cleanup=True skips step 8, so its cleanup is never blocked by the
    owner's own logins; without cleanup step 8 runs again. A refused pending generation removes its
    ADMISSION and leaves INHIBIT set; a refused admitted one changes nothing.
    """
    _check_owner(owner, purpose)
    if generation is not None and (type(generation) is not int or generation < 1):
        raise ValueError("admission_generation_invalid")
    if type(cleanup) is not bool:
        raise ValueError("admission_cleanup_must_be_boolean")
    processes = processes if processes is not None else SYSTEM_PROCESSES
    paths.ensure()
    with _admission_lock(paths, clock):
        state = _state(paths)
        current = state["admission"]
        if current is not None and (current["owner"] != owner or generation is None):
            raise AdmissionRefused("admission_held", owner=current["owner"], generation=current["generation"])
        if current is not None:
            if generation != current["generation"]:
                raise AdmissionRefused("admission_generation_mismatch", owner=current["owner"],
                                       generation=current["generation"])
            record, continued = current, True
            if not state["inhibit"]:
                _write_inhibit(paths, set_by=owner, generation=generation, clock=clock)
        else:
            if generation is not None:
                raise AdmissionRefused("admission_not_held")
            number = state["last_generation"] + 1
            atomic_write_json(paths.admission_generation_file, {"schema_version": SCHEMA_VERSION, "generation": number,
                                                               "updated_at": clock.utc_now()})
            if not state["inhibit"]:
                _write_inhibit(paths, set_by=owner, generation=number, clock=clock)
            record = {"schema_version": SCHEMA_VERSION, "generation": number, "owner": owner, "purpose": purpose,
                      "created_at": clock.utc_now(), "state": "pending"}
            atomic_write_json(paths.admission_file, record)
            continued = False
    pending = record["state"] == "pending"
    check_ledger = pending or not cleanup
    try:
        recorder = _drain(paths, clock=clock, processes=processes, timeout_s=drain_timeout_s)
        if check_ledger:
            problems = ledger_problems(config, paths, clock=clock)
            if problems:
                raise AdmissionRefused("login_ledger_not_clean", detail=problems[0], problems=problems)
        if pending:
            record = _grant(paths, record, clock)
    except AdmissionRefused:
        if pending:
            _withdraw(paths, record, clock)
        raise
    return {"generation": record["generation"], "owner": owner, "purpose": record["purpose"], "continued": continued,
            "state": record["state"], "ledger_checked": check_ledger, "recorder": recorder}


def release(paths, *, config, generation, clock=SYSTEM_CLOCK, extra_checks=()):
    """Remove ADMISSION and INHIBIT, under admission.lock, only for the generation in force and a clean
    state; returns {"released", "generation", "owner", "status"}.

    generation must equal ADMISSION's (release_generation_mismatch otherwise, which is how an older
    generation's orderly end is refused). With no ADMISSION, a generation is refused (admission_not_held)
    and None releases an INHIBIT left without one (a refused admission, an interrupted release), which
    is the operator's typed release; with neither file there is nothing to release. The clean state
    (live_state_problems), the step 8 ledger rule and every extra check (callables taking paths and
    returning problem codes; P1-08's read-only armed-session check goes here) must all pass, else
    AdmissionRefused("release_refused") lists every problem and nothing changes.
    """
    if generation is not None and (type(generation) is not int or generation < 1):
        raise ValueError("admission_generation_invalid")
    paths.ensure()
    with _admission_lock(paths, clock):
        state = _state(paths)
        current = state["admission"]
        owner = current["owner"] if current else None
        if current is not None and generation != current["generation"]:
            raise AdmissionRefused("release_generation_mismatch", owner=owner, generation=current["generation"])
        if current is None and generation is not None:
            raise AdmissionRefused("admission_not_held")
        if current is None and not state["inhibit"]:
            return {"released": False, "generation": None, "owner": None, "status": "nothing_to_release"}
        problems = live_state_problems(paths, clock=clock) + ledger_problems(config, paths, clock=clock)
        for check in extra_checks:
            problems += list(check(paths))
        if problems:
            raise AdmissionRefused("release_refused", detail=problems[0], owner=owner, generation=generation,
                                   problems=problems)
        if current is not None:
            _unlink(paths.admission_file)        # ADMISSION first: a crash between leaves INHIBIT alone, still safe
        _unlink(paths.inhibit_file)
        _fsync_dir(paths.recordings_dir)
    return {"released": True, "generation": generation, "owner": owner, "status": "released"}


# --- the ownership lifecycle (P1-08 §3.4) -------------------------------------------------------------


class OwnershipRefused(BetfairError):
    """live_ownership refused before (or instead of) an admission: owner_live, owner_state_unknown, a before-admit
    code, admission_held, admission_state_unknown, continuation_generation_mismatch or
    continuation_needs_admitted_generation. Nothing was admitted, written or drained, and the owner lock is let go.
    str() is the code; problems lists every code (a before-admit check may give several)."""

    def __init__(self, code, *, problems=(), owner=None, generation=None, state=None):
        code = safe_code(code)
        super().__init__(code)
        self.code = code
        self.problems = [safe_code(problem) for problem in problems] or [code]
        self.owner = safe_code(owner) if owner is not None else None
        self.generation = generation if type(generation) is int else None
        self.state = state if state in ADMISSION_STATES else None


class OwnerTicket:
    """What live_ownership yields: owner (the ADMISSION owner: the command's own for a fresh ticket, the retained one
    for a continuation), command_owner (the name the command gave), generation N, kind ("fresh" or "continuation"),
    placement_allowed (False for a continuation), inhibit_preexisting (INHIBIT was set before a fresh admit),
    recorder (admit's drain result) and holds() (this process still holds the owner lock, at the path)."""

    def __init__(self, *, owner, command_owner, generation, kind, placement_allowed, inhibit_preexisting, recorder,
                 held):
        self.owner = owner
        self.command_owner = command_owner
        self.generation = generation
        self.kind = kind
        self.placement_allowed = placement_allowed
        self.inhibit_preexisting = inhibit_preexisting
        self.recorder = recorder
        self._held = held

    def holds(self):
        return self._held is not None and self._held.held()


def owner_lock_state(paths, *, clock=SYSTEM_CLOCK):
    """"free", "held" or "unknown": .live-owner.lock tried with LOCK_NB and let go at once. Any OSError is unknown,
    which every pilot caller treats as held. A lock file that does not exist is free and is not created: a probe
    leaves no file behind (P1-08 residual 3). Never call it while this process holds the lock (flock conflicts within
    one process too): an owner checks its ticket's holds() instead (Q3's self-lock exception)."""
    try:
        os.lstat(paths.live_owner_lock)
    except FileNotFoundError:
        return "free"
    except OSError:
        return "unknown"
    try:
        with file_lock(paths.live_owner_lock, timeout_s=0, clock=clock):
            return "free"
    except LockBusy:
        return "held"
    except OSError:
        return "unknown"


@contextmanager
def live_ownership(paths, *, owner, purpose, config, continue_generation=None, cleanup=False, before_admit=None,
                   clock=SYSTEM_CLOCK, processes=None, drain_timeout_s=DRAIN_TIMEOUT_S):
    """Own the live recording state for one command and yield its OwnerTicket; the owner lock is held until exit.

    Phase 1, the owner lock (LOCK_NB): busy refuses owner_live, an error owner_state_unknown, nothing written.
    Phase 2, before_admit(paths) when given: read only, it returns [] or refusal codes; codes refuse (the first
    one) with ADMISSION, INHIBIT, the recorder and the journal exactly as before the call. Phase 3, the admission:
    fresh (continue_generation None) refuses an ADMISSION in force (admission_held, with its owner and generation),
    notes INHIBIT set before it (inhibit_preexisting) and admits owner; a restricted continuation needs ADMISSION at
    exactly generation N and admitted (else continuation_generation_mismatch or continuation_needs_admitted_
    generation) and continues the retained owner's generation with cleanup as the command chooses (True only for
    the cleanup that makes release possible, reconcile; False runs step 8, so a dirty login ledger refuses
    login_ledger_not_clean with ADMISSION and INHIBIT unchanged). An unreadable state is admission_state_unknown.
    admit's own refusals propagate as AdmissionRefused. A continuation never places and never releases."""
    if continue_generation is not None and (type(continue_generation) is not int or continue_generation < 1):
        raise ValueError("continue_generation_invalid")
    if type(cleanup) is not bool:
        raise ValueError("admission_cleanup_must_be_boolean")
    _check_owner(owner, purpose)
    if continue_generation is None and purpose is None:
        raise ValueError("admission_purpose_invalid")          # a fresh ADMISSION records its purpose
    paths.ensure()
    stack = ExitStack()
    try:
        held = stack.enter_context(file_lock(paths.live_owner_lock, timeout_s=0, clock=clock))
    except LockBusy:
        raise OwnershipRefused("owner_live") from None
    except OSError:
        raise OwnershipRefused("owner_state_unknown") from None
    with stack:
        if before_admit is not None:
            codes = list(before_admit(paths))
            if codes:
                raise OwnershipRefused(codes[0], problems=codes)
        try:
            status = admission_status(paths)
        except AdmissionRefused:
            raise OwnershipRefused("admission_state_unknown") from None
        current = status["admission"]
        if continue_generation is None:
            if current is not None:
                raise OwnershipRefused("admission_held", owner=current["owner"], generation=current["generation"],
                                       state=current["state"])
            result = admit(paths, owner=owner, purpose=purpose, config=config, clock=clock, processes=processes,
                           drain_timeout_s=drain_timeout_s)
            ticket = OwnerTicket(owner=owner, command_owner=owner, generation=result["generation"], kind="fresh",
                                 placement_allowed=True, inhibit_preexisting=bool(status["inhibit"]),
                                 recorder=result["recorder"], held=held)
        else:
            if current is None or current["generation"] != continue_generation:
                raise OwnershipRefused("continuation_generation_mismatch",
                                       owner=None if current is None else current["owner"],
                                       generation=None if current is None else current["generation"],
                                       state=None if current is None else current["state"])
            if current["state"] != "admitted":
                raise OwnershipRefused("continuation_needs_admitted_generation", owner=current["owner"],
                                       generation=current["generation"], state=current["state"])
            result = admit(paths, owner=current["owner"], purpose=None, config=config, generation=continue_generation,
                           cleanup=cleanup, clock=clock, processes=processes, drain_timeout_s=drain_timeout_s)
            ticket = OwnerTicket(owner=current["owner"], command_owner=owner, generation=continue_generation,
                                 kind="continuation", placement_allowed=False,
                                 inhibit_preexisting=bool(status["inhibit"]), recorder=result["recorder"], held=held)
        yield ticket


def release_own(ticket, paths, *, config, extra_checks=(), clock=SYSTEM_CLOCK):
    """Release the ticket's own generation at its orderly end -> {"released", "outcome", "problems", "owner",
    "generation"}; never raises an admission refusal (the caller prints the result).

    Only a fresh ticket whose INHIBIT did not predate it may release (else nothing is called: outcome
    "continuation_never_releases" or "inhibit_preexisting"). The release runs the clean-state and step 8 rules and
    every extra check under admission.lock, plus two of its own: owner_lock_lost unless this process still holds the
    owner lock (its ticket, never a probe), and admission_owner_changed unless ADMISSION names exactly this owner,
    generation and "admitted" (release itself checks the generation only), admission_state_unknown when it cannot
    be read. On a refusal nothing changes: ADMISSION and INHIBIT stay, and the generation is named for Yu's typed
    recording-release."""
    out = {"released": False, "outcome": None, "problems": [], "owner": ticket.owner, "generation": ticket.generation}
    if ticket.kind != "fresh":
        return dict(out, outcome="continuation_never_releases", problems=["continuation_never_releases"])
    if ticket.inhibit_preexisting:
        return dict(out, outcome="inhibit_preexisting", problems=["inhibit_preexisting"])

    def owner_held(_paths):
        return [] if ticket.holds() else ["owner_lock_lost"]

    def own_admission(checked_paths):
        try:
            current = admission_status(checked_paths)["admission"]
        except AdmissionRefused:
            return ["admission_state_unknown"]
        named = None if current is None else (current["owner"], current["generation"], current["state"])
        return [] if named == (ticket.owner, ticket.generation, "admitted") else ["admission_owner_changed"]

    try:
        result = release(paths, config=config, generation=ticket.generation, clock=clock,
                         extra_checks=[*extra_checks, owner_held, own_admission])
    except AdmissionRefused as exc:
        return dict(out, outcome=exc.outcome, problems=list(exc.problems) or [exc.detail or exc.outcome],
                    owner=exc.owner or ticket.owner)
    return dict(out, released=bool(result["released"]), outcome=result["status"])


# --- Yu's typed recording-release (recording design v9 §3.5) -------------------------------------------------------

RELEASE_GENERATION_LINE = "RELEASE RECORDING GENERATION {generation}"
RELEASE_INHIBIT_LINE = "RELEASE RECORDING INHIBIT"


def recording_release(paths, *, config, show, confirm, extra_checks, clock=SYSTEM_CLOCK):
    """Yu's typed recording-release (design v9 §3.5; P1-08 spec §8 :778; ruling 9) -> {"outcome", "generation",
    "owner", "problems"}; outcome is "released_generation", "released_inhibit", "nothing_to_release" or
    "release_refused" (problems lists every code). The CLI runs static arming (A2, A11, A13) before calling it.

    1. .live-owner.lock with LOCK_NB, held to the very end: busy refuses owner_live, an OSError owner_state_unknown
       (OwnershipRefused); never probed and then taken.
    2. admission_status under the held lock; unreadable refuses admission_state_unknown (OwnershipRefused).
    3. extra_checks (read-only callables paths -> codes; the CLI passes the armed-session check and the lock-free
       fold check) run before any prompt.
    4. The form: ADMISSION present -> the generation form (a pending generation too), typed
       RELEASE RECORDING GENERATION <N>; ADMISSION absent and INHIBIT present -> the INHIBIT-only form, typed
       RELEASE RECORDING INHIBIT; neither -> nothing_to_release, no prompt.
    5. Any check code refuses before the prompt (release_refused).
    6. show(view) prints the codes only (owner, generation, state, INHIBIT, check codes); confirm(line, banner) takes
       Yu's typed line and raises on any other text; then release() runs the clean-state rule, step 8, the checks
       again and admission_unchanged (exactly the owner, generation and state shown) under admission.lock.
    Nothing here reads a credential, logs in or touches the network."""
    stack = ExitStack()
    try:
        paths.ensure()
        stack.enter_context(file_lock(paths.live_owner_lock, timeout_s=0, clock=clock))
    except LockBusy:
        raise OwnershipRefused("owner_live") from None
    except OSError:
        raise OwnershipRefused("owner_state_unknown") from None
    with stack:
        try:
            status = admission_status(paths)
        except AdmissionRefused:
            raise OwnershipRefused("admission_state_unknown") from None
        current = status["admission"]
        shown = None if current is None else (current["owner"], current["generation"], current["state"])
        generation = None if current is None else current["generation"]
        out = {"outcome": None, "generation": generation, "owner": None if current is None else current["owner"],
               "problems": []}
        if current is None and not status["inhibit"]:
            return dict(out, outcome="nothing_to_release")
        codes = []
        for check in extra_checks:
            codes += list(check(paths))
        view = {"form": "generation" if current is not None else "inhibit_only",
                "owner": out["owner"], "generation": generation,
                "state": None if current is None else current["state"],
                "inhibit": "present" if status["inhibit"] else "absent", "check_codes": list(codes)}
        show(view)
        if codes:
            return dict(out, outcome="release_refused", problems=codes)
        if current is not None:
            line = RELEASE_GENERATION_LINE.format(generation=generation)
            banner = (f"Release recording generation {generation} (owner {current['owner']}, state "
                      f"{current['state']}). Release still refuses unless the live journal is clean, the login "
                      f"ledger is clean and no pilot session is armed.")
        else:
            line = RELEASE_INHIBIT_LINE
            banner = ("Release the INHIBIT left without an ADMISSION. Release still refuses unless the live journal is "
                      "clean, the login ledger is clean and no pilot session is armed.")
        confirm(line, banner)

        def admission_unchanged(checked_paths):
            try:
                now = admission_status(checked_paths)["admission"]
            except AdmissionRefused:
                return ["admission_state_unknown"]
            named = None if now is None else (now["owner"], now["generation"], now["state"])
            return [] if named == shown else ["admission_changed"]

        try:
            release(paths, config=config, generation=generation, clock=clock,
                    extra_checks=[*extra_checks, admission_unchanged])
        except AdmissionRefused as exc:
            return dict(out, outcome="release_refused",
                        problems=list(exc.problems) or [exc.detail or exc.outcome])
        return dict(out, outcome="released_generation" if current is not None else "released_inhibit")
