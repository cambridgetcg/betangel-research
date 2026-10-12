"""macOS Keychain access for Betfair credentials, certificate file checks and the journal anchor.

Secrets are read with the absolute /usr/bin/security path, a minimal environment, captured output
and an argv that holds only service and account names. Failures say which item kind was
unavailable and nothing else: stdout and stderr are never kept and exit codes are not interpreted,
with one fail-closed exception: anchor_read reports "no anchor" only when both lookups exit 44
(errSecItemNotFound; unverified on this Mac, first-live-test T0) and raises on anything else.
The journal anchor item holds non-secret JSON by design, and is written only by publish_anchor, which
serializes every publication under one per-profile lock, reads the journal head and the Keychain anchor
inside it and writes only a head that extends that anchor, so the anchor never moves backwards. Setup
commands are printed, never run.
Every runner defaults to subprocess.run looked up at call time, so one patch of subprocess.run
guards every call in tests.
"""

import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys

from .errors import ConfigError, CredentialError, LockBusy
from .paths import file_lock, require_outside_worktrees, validate_profile
from .receipts import SYSTEM_CLOCK, canonical_json, strict_json_loads
from .secret import Secret

SECURITY_BIN = "/usr/bin/security"
SERVICES = {
    "username": "betangel-research.betfair.username",
    "password": "betangel-research.betfair.password",
    "app_key_delayed": "betangel-research.betfair.app-key.delayed",
    "app_key_live": "betangel-research.betfair.app-key.live",
    "cert_key_passphrase": "betangel-research.betfair.cert-key-passphrase",
}
ANCHOR_SERVICE = "betangel-research.betfair.journal-anchor"
ANCHOR_KEYS = frozenset({"genesis_sha256", "seq", "last_sha256", "updated_at"})
PURPOSE_SLOTS = {"read": ("read", "app_key_slot"), "stream": ("stream", "app_key_slot"), "live_orders": None}
# Kinds kept out of assert_clean's substring scan (see Secret): the username, and the certificate key
# passphrase, which only decrypts the key file in this process and is never sent or written; a short
# user-chosen passphrase would otherwise match betIds, refs and hashes and refuse journal writes.
UNSCANNED_KINDS = frozenset({"username", "cert_key_passphrase"})
# security(1) exits with the low byte of the OSStatus: errSecItemNotFound (-25300) gives 44.
ITEM_NOT_FOUND_EXIT = 44
# How long publish_anchor waits for another publisher of the same profile (it holds the lock only for one
# journal head read, one anchor_read and one anchor_write). Only resume and a risk-adding command's gateway
# open() wait this long; the kill and every other gateway publication wait killswitch.JOURNAL_BUSY_TIMEOUT_S
# (gateway.OrderGateway._anchor_wait_s).
ANCHOR_PUBLISH_WAIT_S = 30
# How many times publish_anchor publishes (each under a freshly taken lock) when it finds its lock path was
# replaced during its Keychain write; it then raises journal_anchor_lock_unavailable.
ANCHOR_PUBLISH_ATTEMPTS = 3
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def minimal_env():
    entry = pwd.getpwuid(os.getuid())
    return {"PATH": "/usr/bin:/bin", "HOME": entry.pw_dir, "USER": entry.pw_name, "LOGNAME": entry.pw_name}


def _service(kind):
    if kind not in SERVICES:
        raise CredentialError("unknown_keychain_item_kind")
    return SERVICES[kind]


def _resolve(runner):
    # Looked up per call (not bound as a default argument) so a patched subprocess.run applies.
    return subprocess.run if runner is None else runner


def _run(runner, argv, timeout_s):
    """Run security; returns (returncode, stdout bytes) or None when it could not run at all."""
    try:
        result = _resolve(runner)(argv, env=minimal_env(), capture_output=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = result.stdout
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    return result.returncode, stdout or b""


def keychain_read(kind, profile, *, runner=None, timeout_s=120):
    """One secret as a Secret. The 120 s timeout leaves time to answer a Keychain dialog."""
    service = _service(kind)
    validate_profile(profile)
    outcome = _run(runner, [SECURITY_BIN, "find-generic-password", "-s", service, "-a", profile, "-w"], timeout_s)
    value = None
    if outcome is not None and outcome[0] == 0:
        try:
            text = outcome[1].decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        value = text[:-1] if text.endswith("\n") else text
    if not value:
        # Raised outside any except block so no subprocess output rides along as __context__.
        raise CredentialError(f"keychain_item_unavailable:{kind}")
    return Secret(value, register_value=kind not in UNSCANNED_KINDS)


def keychain_present(kind, profile, *, runner=None):
    """True if the item exists. Uses no -w, so the value is never printed."""
    service = _service(kind)
    validate_profile(profile)
    outcome = _run(runner, [SECURITY_BIN, "find-generic-password", "-s", service, "-a", profile], 30)
    return outcome is not None and outcome[0] == 0


def _validate_anchor(anchor):
    if not isinstance(anchor, dict) or set(anchor) != ANCHOR_KEYS:
        raise CredentialError("journal_anchor_invalid")
    if (not isinstance(anchor["genesis_sha256"], str) or not _SHA256_RE.fullmatch(anchor["genesis_sha256"])
            or not isinstance(anchor["last_sha256"], str) or not _SHA256_RE.fullmatch(anchor["last_sha256"])
            or type(anchor["seq"]) is not int or anchor["seq"] < 1
            or not isinstance(anchor["updated_at"], str) or not anchor["updated_at"]):
        raise CredentialError("journal_anchor_invalid")
    return anchor


def anchor_read(profile, *, runner=None):
    """The Keychain journal anchor, or None only when the anchor item definitely does not exist.

    None needs both lookups (with -w, then attributes only) to run to completion and exit
    ITEM_NOT_FOUND_EXIT. A timeout, a security that cannot start, a locked or denied Keychain (any
    other exit), an item that exists but cannot be read, or a malformed value all raise
    CredentialError, so a failed read can never pass for "no anchor yet" (A13, spec section 9.9).
    """
    validate_profile(profile)
    outcome = _run(runner, [SECURITY_BIN, "find-generic-password", "-s", ANCHOR_SERVICE, "-a", profile, "-w"], 120)
    if outcome is not None and outcome[0] == 0:
        try:
            anchor = strict_json_loads(outcome[1].decode("utf-8").strip())
        except (UnicodeDecodeError, ValueError):
            anchor = None
        return _validate_anchor(anchor)
    if outcome is None or outcome[0] != ITEM_NOT_FOUND_EXIT:
        raise CredentialError("journal_anchor_unreadable")
    present = _run(runner, [SECURITY_BIN, "find-generic-password", "-s", ANCHOR_SERVICE, "-a", profile], 30)
    if present is None or present[0] != ITEM_NOT_FOUND_EXIT:
        raise CredentialError("journal_anchor_unreadable")
    return None


def anchor_write(profile, anchor, *, runner=None):
    """Store the anchor JSON in its own item. The value is on argv because it is not secret."""
    validate_profile(profile)
    value = canonical_json(_validate_anchor(anchor)).decode("utf-8")
    outcome = _run(runner, [SECURITY_BIN, "add-generic-password", "-U", "-s", ANCHOR_SERVICE, "-a", profile,
                            "-w", value], 120)
    if outcome is None or outcome[0] != 0:
        raise CredentialError("journal_anchor_write_failed")


def publish_anchor(journal, paths, *, runner=None, clock=SYSTEM_CLOCK, wait_s=ANCHOR_PUBLISH_WAIT_S):
    """Write the journal's current head to the Keychain anchor; the only caller of anchor_write.

    Every publication of a profile (gateway open and close, the kill's close, resume) takes
    paths.anchor_lock and, inside it, reads journal.anchor() and the Keychain anchor it would replace.
    It writes the head only if that head extends the Keychain anchor (the A13 rule: same genesis, seq not
    lower, and the journal's event at the anchor's seq still carries the anchor's last hash), so the
    Keychain anchor never moves backwards. Two ways it once could:
    - re-review R1: a writer that read the head, paused, then overwrote a newer anchor (hence the lock,
      and the head read inside it; comparing against the Keychain without the lock would still race);
    - a journal rolled back under a live session (sqlite3 ".restore" of an older backup, which every
      open connection then reads): that session's next publication wrote the older head, after which the
      rolled-back journal passed A13. It is now refused (journal_anchor_not_extended) and the anchor stays
      ahead, so the next open refuses that journal (journal_anchor_mismatch).
    held() is checked before the Keychain write and again after it: a lock path unlinked or replaced while
    the write ran (security may take up to 120 s) lets a second publisher lock a new file and write a newer
    head, which this write may then have overwritten, so the publication is made again under a freshly
    taken lock (it re-reads both and writes the newest head), at most ANCHOR_PUBLISH_ATTEMPTS times in all.
    Appends never take this lock: nothing waits for a Keychain write except another publisher.

    -> the anchor written, or None for an empty journal (nothing is written). Raises CredentialError:
    journal_anchor_publish_busy (another publisher held the lock for wait_s), journal_anchor_lock_unavailable
    (the lock file cannot be opened, its path was replaced before the write, or after each of
    ANCHOR_PUBLISH_ATTEMPTS writes), journal_anchor_not_extended (the head does not extend the Keychain
    anchor, or that could not be checked; nothing is written), journal_anchor_unreadable or
    journal_anchor_invalid (the Keychain anchor could not be read; nothing is written), or
    journal_anchor_write_failed. A failed publication leaves the anchor at or below the journal head
    (A13 accepts that) or, for journal_anchor_not_extended, ahead of a journal that A13 then refuses.
    """
    for _ in range(ANCHOR_PUBLISH_ATTEMPTS):
        try:
            with file_lock(paths.anchor_lock, timeout_s=wait_s, clock=clock) as held:
                head = journal.anchor()                 # inside the lock: never older than an earlier publication
                if not head:
                    return None
                current = anchor_read(paths.profile, runner=runner)
                if current is not None and not _head_extends(journal, head, current):
                    raise CredentialError("journal_anchor_not_extended")
                if not held.held():
                    raise CredentialError("journal_anchor_lock_unavailable")
                anchor = dict(head, updated_at=clock.utc_now())
                anchor_write(paths.profile, anchor, runner=runner)
                if held.held():
                    return anchor
                # The lock path was replaced during the write: publish again under the lock the path names now.
        except LockBusy:
            raise CredentialError("journal_anchor_publish_busy") from None
        except OSError:
            raise CredentialError("journal_anchor_lock_unavailable") from None
    raise CredentialError("journal_anchor_lock_unavailable")


def _head_extends(journal, head, current):
    """True when the journal head extends the Keychain anchor current under the A13 rule (arming.anchor_failures).
    Anything reading the journal raises counts as not extended: a head that cannot be checked is never written."""
    from .arming import anchor_failures     # imported here: arming imports this module
    try:
        return anchor_failures(head, current, journal=journal) == []
    except Exception:
        return False


def load_credentials(config, *, purpose, profile, runner=None, loader=None):
    """Everything a session needs for one purpose; "live_orders" always uses the live app key.

    loader(kind, profile) -> Secret replaces the Keychain in tests; nothing falls back to files
    or environment variables.
    """
    if sys.platform != "darwin":
        raise CredentialError("keychain_requires_macos")
    validate_profile(profile)
    if config["profile"] != profile:
        raise CredentialError("credential_profile_does_not_match_config")
    creds = config["credentials"]
    if creds["source"] != "keychain":
        raise CredentialError("credentials_source_must_be_keychain")
    if purpose not in PURPOSE_SLOTS:
        raise CredentialError("unknown_credential_purpose")
    location = PURPOSE_SLOTS[purpose]
    slot = "live" if location is None else config[location[0]][location[1]]
    if slot not in ("delayed", "live"):
        raise CredentialError("invalid_app_key_slot")
    if loader is None:
        def loader(kind, account):
            return keychain_read(kind, account, runner=runner)

    def load(kind):
        value = loader(kind, profile)
        if isinstance(value, str):
            value = Secret(value, register_value=kind not in UNSCANNED_KINDS)
        if not isinstance(value, Secret):
            raise CredentialError(f"keychain_item_unavailable:{kind}")
        return value

    result = {"username": load("username"), "password": load("password"), "app_key": load(f"app_key_{slot}"),
              "app_key_slot": slot, "cert_path": None, "key_path": None, "key_passphrase": None,
              "source": "keychain"}
    if creds["login_method"] == "cert":
        result["cert_path"] = Path(creds["cert_path"])
        result["key_path"] = Path(creds["key_path"])
        if creds["key_passphrase_in_keychain"]:
            result["key_passphrase"] = load("cert_key_passphrase")
    return result


def check_cert_files(cert_path, key_path):
    """Both regular files, outside every Git worktree, and the key has no group/world bits.

    Raises CredentialError("cert_files_unsafe:<reasons>") listing every failed check.
    """
    failures = []
    resolved = {}
    for name, value in (("cert", cert_path), ("key", key_path)):
        if value is None:
            failures.append(f"{name}_path_missing")
            continue
        path = Path(os.path.realpath(Path(value).expanduser()))
        resolved[name] = path
        try:
            info = os.stat(path)
        except FileNotFoundError:
            failures.append(f"{name}_file_missing")
            continue
        except OSError:
            failures.append(f"{name}_file_unreadable")
            continue
        if not stat.S_ISREG(info.st_mode):
            failures.append(f"{name}_not_regular_file")
        try:
            require_outside_worktrees(path, what=name)
        except ConfigError:
            failures.append(f"{name}_inside_git_worktree")
        if name == "key" and info.st_mode & 0o077:
            failures.append("key_group_or_world_accessible")
    if failures:
        raise CredentialError("cert_files_unsafe:" + ",".join(failures), failures=failures)
    return {"status": "cert_files_ok", "cert_path": str(resolved["cert"]), "key_path": str(resolved["key"]),
            "key_mode": oct(os.stat(resolved["key"]).st_mode & 0o777)}


def keychain_setup_commands(profile, *, include_passphrase=False):
    """Commands for the operator to run by hand. "-w" is last so security prompts for each value;
    the live key gets -T "" so no application is trusted to read it silently (effect unverified, T0)."""
    validate_profile(profile)
    kinds = ["username", "password", "app_key_delayed", "app_key_live"]
    if include_passphrase:
        kinds.append("cert_key_passphrase")
    lines = []
    for kind in kinds:
        trust = ' -T ""' if kind == "app_key_live" else ""
        lines.append(f"security add-generic-password -U -s {SERVICES[kind]} -a {profile}{trust} -w")
    return lines
