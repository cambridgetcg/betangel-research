"""Clocks, timestamp parsing, canonical hashing and durable local writes for Betfair receipts.

Local clocks are unauthenticated: wall time is the machine's UTC clock and monotonic time is only
comparable within one process. Every write passes refuse_forbidden and assert_clean first, and is
flushed with F_FULLFSYNC where macOS provides it, because plain fsync does not flush the drive
cache there.
"""

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import time

from .secret import assert_clean, refuse_forbidden

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# [0-9], never \d: \d also matches non-ASCII digits, which int() would silently accept.
_TIMESTAMP_RE = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,6}))?(Z|[+-][0-9]{2}:[0-9]{2})")


def utc_from_ns(ns):
    """UTC text with microseconds and Z, the same shape as collector.utc_now()."""
    if type(ns) is not int:
        raise ValueError("timestamp nanoseconds must be an integer")
    value = _EPOCH + timedelta(microseconds=ns // 1000)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def utc_ms_from_ns(ns):
    """UTC text with milliseconds (floored) and Z, for Betfair request date ranges."""
    if type(ns) is not int:
        raise ValueError("timestamp nanoseconds must be an integer")
    value = _EPOCH + timedelta(milliseconds=ns // 1_000_000)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_compact_from_ns(ns):
    """YYYYMMDDTHHMMSSZ, for directory and session names."""
    if type(ns) is not int:
        raise ValueError("timestamp nanoseconds must be an integer")
    return (_EPOCH + timedelta(microseconds=ns // 1000)).strftime("%Y%m%dT%H%M%SZ")


def parse_utc_ns(text):
    """Nanoseconds since the epoch. Requires Z or an explicit offset; naive text is refused.

    Accepts second, millisecond or microsecond precision (1-6 fraction digits). Parsing avoids
    fromisoformat's fraction handling so Python 3.10 and later agree.
    """
    if not isinstance(text, str):
        raise ValueError("timestamp must be an ISO-8601 string with Z or an offset")
    match = _TIMESTAMP_RE.fullmatch(text)
    if not match:
        raise ValueError("timestamp must be ISO-8601 with Z or an offset and at most microsecond precision")
    base, fraction, offset = match.groups()
    if offset == "Z":
        offset = "+00:00"
    try:
        parsed = datetime.fromisoformat(base + offset)
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    delta = parsed - _EPOCH
    seconds = delta.days * 86400 + delta.seconds
    fraction_ns = int((fraction or "").ljust(9, "0")) if fraction else 0
    return seconds * 1_000_000_000 + fraction_ns


class SystemClock:
    """The real clocks. Tests inject a fake with the same four methods."""

    def time_ns(self):
        return time.time_ns()

    def monotonic_ns(self):
        return time.monotonic_ns()

    def sleep(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not seconds >= 0:
            raise ValueError("sleep seconds must be a non-negative number")
        time.sleep(seconds)

    def utc_now(self):
        return utc_from_ns(self.time_ns())


SYSTEM_CLOCK = SystemClock()


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def canonical_sha256(value):
    """Same digest as collection._digest."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def bytes_sha256(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_hashes(repo_root):
    """sha256 of betangel_research/*.py and betangel_research/betfair/*.py, keyed by relative path."""
    root = Path(repo_root)
    files = sorted((root / "betangel_research").glob("*.py")) + sorted((root / "betangel_research" / "betfair").glob("*.py"))
    return {p.relative_to(root).as_posix(): file_sha256(p) for p in files}


def _git_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git_state(repo_root, *, runner=subprocess.run):
    """{"git_revision", "working_tree_dirty"}; None for either value when git cannot answer."""
    state = {"git_revision": None, "working_tree_dirty": None}
    try:
        revision = runner(["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True,
                          timeout=10, env=_git_env())
        if revision.returncode == 0:
            state["git_revision"] = revision.stdout.strip() or None
        dirty = runner(["git", "status", "--porcelain"], cwd=str(repo_root), capture_output=True, text=True,
                       timeout=10, env=_git_env())
        if dirty.returncode == 0:
            state["working_tree_dirty"] = bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return state


def git_identity(repo_root, *, runner=subprocess.run):
    """{"commit", "tree", "dirty"} for the pilot's PA2 (P1-06): `git rev-parse HEAD HEAD^{tree}` in one call, then
    `git status --porcelain --untracked-files=normal` (an untracked file is dirty). None for any value git cannot
    give; the caller refuses an unknown identity."""
    state = {"commit": None, "tree": None, "dirty": None}
    try:
        parsed = runner(["git", "rev-parse", "HEAD", "HEAD^{tree}"], cwd=str(repo_root), capture_output=True,
                        text=True, timeout=10, env=_git_env())
        lines = parsed.stdout.split() if parsed.returncode == 0 and isinstance(parsed.stdout, str) else []
        if len(lines) == 2:
            state["commit"], state["tree"] = lines
        dirty = runner(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=str(repo_root),
                       capture_output=True, text=True, timeout=10, env=_git_env())
        if dirty.returncode == 0 and isinstance(dirty.stdout, str):
            state["dirty"] = bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return state


def full_fsync(fd):
    """F_FULLFSYNC where available (macOS), else fsync. Filesystems that refuse it fall back to fsync."""
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


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


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def atomic_write_json(path, value, *, mode=0o600):
    """Write indented canonical JSON atomically and durably; returns the sha256 of the bytes written."""
    path = Path(path)
    refuse_forbidden(value, where=path.name)
    data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    assert_clean(data, where=path.name)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, data)
            full_fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path, data, *, mode=0o600):
    """Write exact bytes atomically and durably (the pilot's raw evidence, P1-06 §5.3); returns their sha256.
    The bytes are checked for registered secrets (assert_clean) and never scrubbed: scrubbing would change the
    digest the journal binds."""
    path = Path(path)
    if not isinstance(data, bytes):
        raise TypeError("atomic_write_bytes needs bytes")
    assert_clean(data, where=path.name)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.fchmod(fd, mode)
            _write_all(fd, data)
            full_fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)
    return hashlib.sha256(data).hexdigest()


def end_torn_line(path):
    """Terminate a last line left without a newline (a torn append), so the next row stands on its own. Shared by
    the login ledger (auth) and the recordings run-ledger (recorder, recordings ledger-repair); the torn bytes stay
    as they are, on their own line. Nothing happens when the file does not exist."""
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_APPEND)
    except FileNotFoundError:
        return
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            os.write(fd, b"\n")
    finally:
        os.close(fd)


def append_jsonl(path, row, *, mode=0o600):
    """Append one compact JSON line and flush it durably before returning."""
    path = Path(path)
    if not isinstance(row, dict):
        raise ValueError("JSONL rows must be objects")
    refuse_forbidden(row, where=path.name)
    line = (json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            + "\n").encode("utf-8")
    assert_clean(line, where=path.name)
    existed = path.exists()
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, mode)
    try:
        if not existed:
            os.fchmod(fd, mode)
        _write_all(fd, line)
        full_fsync(fd)
    finally:
        os.close(fd)


def _refuse_constant(name):
    raise ValueError(f"non-finite JSON constant refused: {name}")


def _finite_float(text):
    # A literal such as 1e400 overflows to inf without ever reaching parse_constant.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number refused")
    return value


def _refuse_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key refused")
        result[key] = value
    return result


def strict_json_loads(text):
    """json.loads that refuses NaN, Infinity, -Infinity, overflowing numbers and duplicate object keys.

    Every refusal is a ValueError, including nesting too deep to parse (RecursionError).
    """
    try:
        return json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float,
                          object_pairs_hook=_refuse_duplicates)
    except RecursionError:
        pass
    # Raised outside the except block so the RecursionError is not chained.
    raise ValueError("JSON nesting too deep")
