"""The fixed per-Mac state root, Git worktree detection, the fail-closed kill check and file locks.

All Betfair runtime state lives under <home>/Library/Application Support/betangel-research/betfair/,
with home taken from the password database rather than $HOME, so every checkout and working
directory sees the same KILL file, gateway lock and live journal. Overrides of the root exist only
for the test harness: they work only when BETANGEL_BETFAIR_FORBID_NETWORK=1 is set in the process
environment (and in any env passed in), and the process environment alone turns the network off,
so an override and a live network can never coexist in one process.
"""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import pwd
import re
import subprocess

from .errors import ConfigError, LockBusy
from .receipts import SYSTEM_CLOCK

FORBID_NETWORK_ENV = "BETANGEL_BETFAIR_FORBID_NETWORK"
APP_PARTS = ("Library", "Application Support", "betangel-research", "betfair")
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
JOURNAL_MODES = ("paper", "live")
LOCK_REOPEN_LIMIT = 20          # times file_lock re-opens a lock path that was replaced under it


def network_forbidden(env=None):
    """True when the process environment OR the given env sets the switch: either one turns the network off."""
    if os.environ.get(FORBID_NETWORK_ENV) == "1":
        return True
    return env is not None and env.get(FORBID_NETWORK_ENV) == "1"


def overrides_allowed(env=None):
    """Path overrides need the switch in BOTH the process environment and the given env.

    The process environment is what every transport consults, so whenever an override works the
    network is off for the whole process, whatever env a caller hands to a transport.
    """
    if os.environ.get(FORBID_NETWORK_ENV) != "1":
        return False
    return env is None or env.get(FORBID_NETWORK_ENV) == "1"


def account_home():
    """The login user's home from the password database; $HOME is deliberately ignored."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def validate_profile(profile):
    if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
        raise ConfigError("invalid_profile")
    return profile


class StatePaths:
    def __init__(self, *, profile="default", home=None, root_override=None, env=None):
        self.env = os.environ if env is None else env
        self.profile = validate_profile(profile)
        if (root_override is not None or home is not None) and not overrides_allowed(env):
            raise ConfigError("state_root_override_not_allowed")
        self.overridden = root_override is not None or home is not None
        self.home = Path(home).absolute() if home is not None else account_home()
        if root_override is not None:
            self.root = Path(root_override).absolute()
        else:
            self.root = self.home.joinpath(*APP_PARTS)
        if self.overridden:
            # Even harness state never lands inside a repository checkout.
            require_outside_worktrees(self.root, what="state_root")
        self.kill_file = self.root / "KILL"
        self.gateway_lock = self.root / ".gateway.lock"
        self.accounts_file = self.root / "accounts.json"
        self.profiles_dir = self.root / "profiles"
        self.profile_dir = self.profiles_dir / self.profile
        self.config_path = self.profile_dir / "config.json"
        self.auth_dir = self.profile_dir / "auth"
        self.login_ledger = self.auth_dir / "login-ledger.jsonl"
        self.orders_dir = self.profile_dir / "orders"
        self.paper_lock = self.orders_dir / ".paper.lock"
        # Serializes every Keychain anchor publication of this profile (credentials.publish_anchor).
        self.anchor_lock = self.orders_dir / ".journal-anchor.lock"
        self.live_tests_dir = self.profile_dir / "live-tests"
        self.reads_dir = self.profile_dir / "reads"
        self.catalogue_cache_dir = self.profile_dir / "catalogue-cache"
        self.recordings_dir = self.profile_dir / "recordings"
        self.recorder_lock = self.recordings_dir / ".recorder.lock"
        # Recording admission (admission.py): shared by every checkout and worktree of this profile.
        self.admission_lock = self.recordings_dir / "admission.lock"
        self.inhibit_file = self.recordings_dir / "INHIBIT"
        self.admission_file = self.recordings_dir / "ADMISSION"
        self.admission_generation_file = self.recordings_dir / "admission-generation.json"
        self.recorder_pid_file = self.recordings_dir / "recorder.pid"
        self.recordings_index_dir = self.recordings_dir / "index"

    def journal_path(self, mode):
        if mode not in JOURNAL_MODES:
            raise ConfigError("invalid_journal_mode")
        return self.orders_dir / f"journal-{mode}.sqlite"

    def _private_dirs(self):
        return [self.root, self.profiles_dir, self.profile_dir, self.auth_dir, self.orders_dir,
                self.live_tests_dir, self.reads_dir, self.catalogue_cache_dir, self.recordings_dir]

    def ensure(self):
        """Create the state tree; every directory from the root down is mode 0700."""
        for directory in self._private_dirs():
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    def describe(self):
        names = ("root", "kill_file", "gateway_lock", "accounts_file", "profile_dir", "config_path",
                 "login_ledger", "paper_lock", "anchor_lock", "live_tests_dir", "reads_dir", "catalogue_cache_dir",
                 "recordings_dir", "recorder_lock", "admission_lock", "inhibit_file", "admission_file",
                 "recorder_pid_file", "recordings_index_dir")
        described = {name: str(getattr(self, name)) for name in names}
        described["journal_paper"] = str(self.journal_path("paper"))
        described["journal_live"] = str(self.journal_path("live"))
        described["profile"] = self.profile
        described["overridden"] = self.overridden
        return described


def _git_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def is_inside_git_worktree(path, *, runner=subprocess.run):
    """True if path lies inside any Git worktree or repository.

    Any existing ancestor holding a .git entry (directory, or the file a linked worktree uses)
    counts. Otherwise git itself is asked about the nearest existing ancestor (5 s timeout). A
    missing git binary skips that check; a git that hangs or cannot start counts as inside
    (fail closed).
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.realpath(candidate))
    nearest = None
    for ancestor in (candidate, *candidate.parents):
        try:
            exists = ancestor.exists()
        except OSError:
            exists = False
        if not exists:
            continue
        if nearest is None:
            nearest = ancestor if ancestor.is_dir() else ancestor.parent
        if os.path.lexists(ancestor / ".git"):
            return True
    if nearest is None:
        return False
    try:
        result = runner(["git", "-C", str(nearest), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                        text=True, timeout=5, env=_git_env())
    except FileNotFoundError:
        return False
    except (subprocess.TimeoutExpired, OSError):
        return True
    return result.returncode == 0 and (result.stdout or "").strip() == "true"


def require_outside_worktrees(path, *, what):
    """Resolved absolute path, or ConfigError(f"{what}_inside_git_worktree")."""
    resolved = Path(os.path.realpath(Path(path).expanduser()))
    if is_inside_git_worktree(resolved):
        raise ConfigError(f"{what}_inside_git_worktree")
    return resolved


def kill_engaged(paths):
    """Fail closed: only a definite "no such file" means the kill switch is off.

    lstat, not stat: any directory entry named KILL engages the switch, a dangling symlink included.
    """
    try:
        os.lstat(paths.kill_file)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


class HeldLock:
    """What file_lock yields: held() is True while the lock path still names the locked file.

    flock locks an inode, not a name. If the path is unlinked or replaced while the lock is held
    (for example a "stale lock" clean-up), a second process can create a new file there and lock
    it too; held() then turns False, so a holder can refuse to act on a lock that no longer
    excludes anyone.
    """

    __slots__ = ("path", "_fd")

    def __init__(self, path, fd):
        self.path = path
        self._fd = fd

    def held(self):
        return self._fd is not None and _names_same_file(self._fd, self.path)


def _names_same_file(fd, path):
    try:
        mine, named = os.fstat(fd), os.stat(path)
    except OSError:
        return False
    return (mine.st_dev, mine.st_ino) == (named.st_dev, named.st_ino)


@contextmanager
def file_lock(path, *, timeout_s=0.0, clock=SYSTEM_CLOCK, poll_s=0.2):
    """Exclusive flock, polled until timeout_s on the injected clock; raises LockBusy.

    After flock succeeds the open file must still be the one the path names (st_dev, st_ino);
    if the path was unlinked or replaced meanwhile, the lock is dropped and taken again on the
    file the path names now, the standard discipline for flock lock files. Yields a HeldLock.
    """
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not timeout_s >= 0:
        raise ValueError("lock timeout must be a non-negative number")
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = clock.monotonic_ns()
    fd = None
    for _ in range(LOCK_REOPEN_LIMIT):
        candidate = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if clock.monotonic_ns() - started >= timeout_s * 1_000_000_000:
                        raise LockBusy(f"lock_busy:{path.name}") from None
                    clock.sleep(poll_s)
            if _names_same_file(candidate, path):
                fd = candidate
                break
            fcntl.flock(candidate, fcntl.LOCK_UN)
        except BaseException:
            os.close(candidate)
            raise
        os.close(candidate)
    if fd is None:
        raise LockBusy(f"lock_busy:{path.name}")
    handle = HeldLock(path, fd)
    try:
        yield handle
    finally:
        handle._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
