"""Live arming: checks A1-A19 (spec section 9.2), the daily confirm token, journal anchor checks and the
account registry.

Every check that applies to a command runs and every failure is listed, never just the first.
static_arming runs before the gateway lock and before any login or secret read (Keychain presence
only); dynamic_arming runs after the lock, the journal check, the login and the startup
reconciliation. The confirm token is a per-day friction value, not a secret: config_sha256 is
public. It comes only from --confirm-live; no environment variable is ever read for it. Paper mode
needs none of this (the gateway passes {"armed": True, "failures": []} to the risk guard).
"""

import hmac
import re
import sqlite3
import sys
from urllib.parse import quote

from .config import config_sha256, money_pence
from .constants import ABSOLUTE_CEILINGS, ACCOUNT_CURRENCY, CEILING_PATHS, LIVE_ACK_IDS
from .credentials import anchor_read, check_cert_files, keychain_present
from .errors import AuthError, BetfairError, ConfigError, CredentialError, NotArmed, NotPresent, OrderRefused
from .paths import is_inside_git_worktree, kill_engaged, validate_profile
from .receipts import atomic_write_json, parse_utc_ns, strict_json_loads, utc_from_ns
from .schemas import UNRESOLVED_STATES

ACK_IDS = LIVE_ACK_IDS
CLOUD_ENV_MARKERS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "CODESPACES", "KUBERNETES_SERVICE_HOST",
                     "AWS_EXECUTION_ENV", "FLY_APP_NAME", "FLY_MACHINE_ID")
CHECK_IDS = tuple(f"A{n}" for n in range(1, 20))
STATIC_CHECKS = CHECK_IDS[:13]
DYNAMIC_CHECKS = CHECK_IDS[13:]
# Failure code(s) per check (spec section 9.2). journal_anchor_unreadable is an addition: a Keychain
# anchor that cannot be read fails closed instead of passing for "no anchor".
FAILURE_CODES = {
    "A1": ("not_armed",), "A2": ("confirm_token_mismatch",), "A3": ("acknowledgements_incomplete",),
    "A4": ("keychain_credentials_missing",), "A5": ("cert_files_unsafe",), "A6": ("caps_invalid",),
    "A7": ("halted",), "A8": ("jurisdiction_or_currency_unsupported",), "A9": ("heartbeat_unsupported_v1",),
    "A10": ("egress_unattested_or_cloud",), "A11": ("operator_tty_required",),
    "A12": ("bet_angel_coexistence_unverified",),
    "A13": ("state_root_invalid", "journal_anchor_mismatch", "journal_anchor_missing", "journal_anchor_unreadable"),
    "A14": ("login_cannot_bet",), "A15": ("account_registered_to_other_profile", "account_registry_write_refused"),
    "A16": ("account_currency_not_gbp_or_unknown",), "A17": ("unresolved_or_foreign_orders",),
    "A18": ("market_data_delayed_or_stale",), "A19": ("ref_roundtrip_unverified",),
}
_LOGIN_ONLY = ("A4", "A5", "A8", "A10", "A12", "A13", "A14", "A15")
_OPERATOR_TYPED = tuple(sorted(set(_LOGIN_ONLY) | {"A1", "A2", "A11"}, key=CHECK_IDS.index))
# Spec section 9.2 command matrix. kill-account and reconcile-accept are the flagged variants of kill
# and reconcile. "replace" stays for paper open() (live replace was removed in v1 and is refused in
# the gateway before any check runs). settle-market (amendment A3, D3) has the reconcile-accept set:
# no A7, so it runs while halted or killed (it sends no order).
COMMAND_CHECKS = {
    "place": CHECK_IDS,
    "replace": CHECK_IDS,
    "live-probe": CHECK_IDS[:18],
    "cancel": _LOGIN_ONLY,
    "reconcile": _LOGIN_ONLY,
    "reconcile-accept": _OPERATOR_TYPED,
    "settle-market": _OPERATOR_TYPED,
    "kill": ("A4", "A13"),
    "kill-account": ("A4", "A11", "A13"),
    "resume": ("A2", "A11", "A13"),
}
# Commands whose static A13 checks the state root only (amendment A3, D4).
KILL_STATIC_A13_ROOT_ONLY = frozenset({"kill", "kill-account"})
# Commands whose A14 needs only a live session, not can_bet (cancel works on a LIMITED_ACCESS login).
LOGIN_ONLY_COMMANDS = frozenset({"cancel", "reconcile", "reconcile-accept", "settle-market", "kill", "kill-account"})
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_NS_PER_S = 1_000_000_000


def checks_for(command):
    if command not in COMMAND_CHECKS:
        raise ValueError("unknown arming command")
    return COMMAND_CHECKS[command]


def expected_token(config, now_ns):
    """"live-" + config_sha256[:12] + "-" + UTC YYYYMMDD. A friction value, not a secret."""
    if type(now_ns) is not int:
        raise ValueError("now_ns must be an integer")
    return "live-" + config_sha256(config)[:12] + "-" + utc_from_ns(now_ns)[:10].replace("-", "")


def token_matches(config, confirm_token, now_ns):
    expected = expected_token(config, now_ns)
    return (isinstance(confirm_token, str) and confirm_token.isascii()
            and hmac.compare_digest(confirm_token, expected))


def _anchor_shape(anchor, *, keychain):
    keys = {"genesis_sha256", "seq", "last_sha256"} | ({"updated_at"} if keychain else set())
    if not isinstance(anchor, dict) or not keys <= set(anchor) or (keychain and set(anchor) != keys):
        return False
    return (isinstance(anchor["genesis_sha256"], str) and _SHA_RE.fullmatch(anchor["genesis_sha256"]) is not None
            and isinstance(anchor["last_sha256"], str) and _SHA_RE.fullmatch(anchor["last_sha256"]) is not None
            and type(anchor["seq"]) is int and anchor["seq"] >= 1)


def check_anchor(journal_anchor, keychain_anchor):
    """Spec section 9.9 A13 anchor rule: [] when consistent, else the failure codes.

    - A Keychain anchor with a missing or empty journal, another genesis, a lower seq, or the same
      seq with another last hash: journal_anchor_mismatch.
    - No Keychain anchor but a journal that has events: journal_anchor_missing.
    """
    has_journal = bool(journal_anchor)
    if has_journal and not _anchor_shape(journal_anchor, keychain=False):
        return ["journal_anchor_mismatch"]
    if keychain_anchor is None:
        return ["journal_anchor_missing"] if has_journal else []
    if not _anchor_shape(keychain_anchor, keychain=True) or not has_journal:
        return ["journal_anchor_mismatch"]
    if (journal_anchor["genesis_sha256"] != keychain_anchor["genesis_sha256"]
            or journal_anchor["seq"] < keychain_anchor["seq"]
            or (journal_anchor["seq"] == keychain_anchor["seq"]
                and journal_anchor["last_sha256"] != keychain_anchor["last_sha256"])):
        return ["journal_anchor_mismatch"]
    return []


def anchor_bootstrappable(journal_state):
    """True when a journal holds no order at all (only kill, halt, note, startup reads and the like).

    Such a journal may take its first Keychain anchor (an addition to spec section 9.9): it carries no
    cap state that a rollback could reset, and refusing it would leave live mode locked for good
    after a first session whose anchor write failed. An unknown state (None) never qualifies.
    """
    return (isinstance(journal_state, dict) and journal_state.get("orders") == {}
            and journal_state.get("ref_roundtrip_verified") is not True)


def anchor_failures(journal_anchor, keychain_anchor, *, journal=None, journal_state=None):
    """The whole A13 anchor rule: check_anchor, then (given the journal) the event at the Keychain
    anchor's seq must still carry the Keychain's last hash, so a journal rolled back and extended
    past the anchor's seq is a mismatch too. A journal with no order may bootstrap its first anchor."""
    failures = check_anchor(journal_anchor, keychain_anchor)
    if failures == ["journal_anchor_missing"] and anchor_bootstrappable(journal_state):
        return []
    if failures or keychain_anchor is None or journal is None:
        return failures
    try:
        events = journal.events(since_seq=keychain_anchor["seq"] - 1)
        try:
            event = next(events, None)
        finally:
            events.close()
    except (BetfairError, ValueError, sqlite3.Error):
        return ["journal_anchor_mismatch"]
    if (not isinstance(event, dict) or event.get("seq") != keychain_anchor["seq"]
            or event.get("event_sha256") != keychain_anchor["last_sha256"]):
        return ["journal_anchor_mismatch"]
    return []


def _caps_ok(config):
    try:
        caps = config["orders"]["caps"]
        expected = ("max_stake_per_order", "max_liability_per_order", "max_market_exposure", "max_total_exposure",
                    "max_daily_loss", "max_orders_per_minute", "max_orders_per_hour", "max_orders_per_day",
                    "max_transactions_per_hour", "max_instructions_per_request", "max_price_deviation_ticks",
                    "max_book_age_s", "funds_reserve", "funds_max_age_s", "max_minutes_before_off")
        if any(key not in caps for key in expected):
            return False
        for key, path in CEILING_PATHS.items():
            value = config
            for part in path:
                value = value[part]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0 <= value <= ABSOLUTE_CEILINGS[key]):
                return False
            if key != "external_exposure_tolerance" and value <= 0:
                return False
        money_pence(caps["funds_reserve"], allow_zero=True)
    except (KeyError, TypeError, ConfigError):
        return False
    return True


def _static_check(check, config, *, command, paths, cli_mode, confirm_token, bet_angel_paused, env, confirmer,
                  keychain_runner, journal_anchor, journal_state, journal, now_ns):
    """Failure codes for one static check (empty when it passes)."""
    live = config["orders"]["live"]
    if check == "A1":
        return [] if cli_mode == "live" and live["armed"] is True else ["not_armed"]
    if check == "A2":
        return [] if token_matches(config, confirm_token, now_ns) else ["confirm_token_mismatch"]
    if check == "A3":
        acks = live["acknowledgements"]
        ok = isinstance(acks, list) and len(acks) == len(set(acks)) and set(acks) == set(ACK_IDS)
        return [] if ok else ["acknowledgements_incomplete"]
    if check == "A4":
        if sys.platform != "darwin" or config["credentials"]["source"] != "keychain":
            return ["keychain_credentials_missing"]
        kinds = ["username", "password", "app_key_live"]
        if config["credentials"]["key_passphrase_in_keychain"]:
            kinds.append("cert_key_passphrase")
        present = all(keychain_present(kind, paths.profile, runner=keychain_runner) for kind in kinds)
        return [] if present else ["keychain_credentials_missing"]
    if check == "A5":
        cred = config["credentials"]
        if cred["login_method"] != "cert" and cred["cert_path"] is None and cred["key_path"] is None:
            return []            # interactive login with no certificate files to protect
        try:
            check_cert_files(cred["cert_path"], cred["key_path"])
        except CredentialError:
            return ["cert_files_unsafe"]
        return []
    if check == "A6":
        return [] if _caps_ok(config) else ["caps_invalid"]
    if check == "A7":
        halted = kill_engaged(paths)
        if journal_state is not None:
            halted = halted or journal_state.get("halted") is not False or journal_state.get("clock_regression") is True
        return ["halted"] if halted else []
    if check == "A8":
        ok = config["jurisdiction"] == "com" and config["account_currency"] == ACCOUNT_CURRENCY
        return [] if ok else ["jurisdiction_or_currency_unsupported"]
    if check == "A9":
        return [] if live["heartbeat_api_enabled"] is False else ["heartbeat_unsupported_v1"]
    if check == "A10":
        if env is None:
            return ["egress_unattested_or_cloud"]          # an environment nobody showed us fails closed
        cloud = any(marker in env for marker in CLOUD_ENV_MARKERS)
        return [] if live["operator_attests_uk_egress"] is True and not cloud else ["egress_unattested_or_cloud"]
    if check == "A11":
        if confirmer is None:
            return ["operator_tty_required"]
        try:
            confirmer.require_present()
        except (NotPresent, OrderRefused):
            return ["operator_tty_required"]
        return []
    if check == "A12":
        ok = config["coexistence"]["bet_angel"] != "unverified" or bet_angel_paused is True
        return [] if ok else ["bet_angel_coexistence_unverified"]
    if check == "A13":
        failures = []
        if paths.overridden is not False or is_inside_git_worktree(paths.root):
            failures.append("state_root_invalid")
        if command in KILL_STATIC_A13_ROOT_ONLY:
            # The kill's static A13 is the state root only: no Keychain anchor, no journal head (amendment
            # A3, D4). Its one anchor check runs in the gateway and never stops the sweep.
            return failures
        try:
            keychain_anchor = anchor_read(paths.profile, runner=keychain_runner)
        except CredentialError:
            return failures + ["journal_anchor_unreadable"]
        if journal is not None:
            # The journal head is read after the Keychain: a concurrent appender (which writes the
            # journal first and the anchor after) can then only leave journal >= Keychain, never a false
            # journal_anchor_mismatch.
            try:
                journal_anchor = journal.anchor()
            except (BetfairError, sqlite3.Error):
                return failures + ["journal_anchor_mismatch"]
        return failures + anchor_failures(journal_anchor, keychain_anchor, journal=journal,
                                          journal_state=journal_state)
    raise ValueError("not a static check")


def _report(checks, failures_by_check):
    failures = []
    for check in checks:
        for code in failures_by_check.get(check, ()):
            if code not in failures:
                failures.append(code)
    return {"armed": not failures, "failures": failures, "checked": list(checks),
            "by_check": {check: list(codes) for check, codes in failures_by_check.items() if codes}}


def static_arming(config, *, command, paths, cli_mode, confirm_token, bet_angel_paused, env, confirmer,
                  keychain_runner, journal_anchor, now_ns, journal_state=None, journal=None):
    """Checks A1-A13 that apply to command -> {"armed", "failures", "checked", "by_check"}.

    Reads only Keychain item presence and the non-secret journal anchor, never a secret value.
    Additions: journal_state lets A7 see the journal HALT (without it A7 checks the kill file only
    and the risk guard's R1 still refuses a halted journal) and lets A13 bootstrap the first anchor
    of a journal that holds no order; journal (an OrderJournal) lets A13 check that the event at the
    Keychain anchor's seq still has the anchored hash (anchor_failures). Given a journal, A13 reads
    the Keychain anchor first and that journal's head after it (journal_anchor is then not used).
    kill and kill-account check only the state root under A13. env None fails A10.
    """
    checks = [c for c in checks_for(command) if c in STATIC_CHECKS]
    by_check = {}
    for check in checks:
        by_check[check] = _static_check(
            check, config, command=command, paths=paths, cli_mode=cli_mode, confirm_token=confirm_token,
            bet_angel_paused=bet_angel_paused, env=env, confirmer=confirmer, keychain_runner=keychain_runner,
            journal_anchor=journal_anchor, journal_state=journal_state, journal=journal, now_ns=now_ns)
    return _report(checks, by_check)


def read_registry(paths):
    """accounts.json as {fingerprint: profile} ({} when absent); NotArmed if unreadable or malformed."""
    try:
        text = paths.accounts_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError):
        raise NotArmed("accounts_registry_unreadable") from None
    try:
        registry = strict_json_loads(text)
    except ValueError:
        raise NotArmed("accounts_registry_unreadable") from None
    if not isinstance(registry, dict) or any(
            not isinstance(k, str) or not _FINGERPRINT_RE.fullmatch(k) or not isinstance(v, str) for k, v in
            registry.items()):
        raise NotArmed("accounts_registry_unreadable")
    return registry


def journal_fingerprints(paths):
    """{profile: account fingerprint} from every profile's live journal meta under the state root.

    accounts.json is a plain file; the live journals are the registry's backing record, so a lost
    or deleted accounts.json cannot let a second profile take an account another profile's
    journal already carries. A journal that exists but cannot be read raises
    NotArmed("account_registry_unverifiable") (fail closed). Journals are opened read-only and only
    existing ones: no journal is ever created here.
    """
    found = {}
    try:
        profiles = sorted(p for p in paths.profiles_dir.iterdir() if p.is_dir())
    except FileNotFoundError:
        return found
    except OSError:
        raise NotArmed("account_registry_unverifiable") from None
    for profile_dir in profiles:
        journal = profile_dir / "orders" / "journal-live.sqlite"
        if not journal.is_file():
            continue
        try:
            db = sqlite3.connect(f"file:{quote(str(journal))}?mode=ro", uri=True, timeout=5)
            try:
                row = db.execute("SELECT value FROM journal_meta WHERE key='account_fingerprint'").fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            raise NotArmed("account_registry_unverifiable") from None
        if row is not None:
            found[profile_dir.name] = row[0]
    return found


def register_account(paths, fingerprint, profile):
    """Map this account to this profile in accounts.json; another profile already mapped -> NotArmed.

    One person, one account (F76): a second profile on the same account would split the caps.
    Another profile's live journal carrying the same fingerprint refuses too, whatever
    accounts.json says (journal_fingerprints). Callers hold the global gateway lock, so
    registrations never race.
    """
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise NotArmed("account_fingerprint_invalid")
    validate_profile(profile)
    registry = read_registry(paths)
    current = registry.get(fingerprint)
    if current is not None and current != profile:
        raise NotArmed("account_registered_to_other_profile")
    for other, value in journal_fingerprints(paths).items():
        if other != profile and value == fingerprint:
            raise NotArmed("account_registered_to_other_profile")
    if current is None:
        registry[fingerprint] = profile
        paths.ensure()
        atomic_write_json(paths.accounts_file, registry)


def _book_fresh(config, book, now_monotonic_ns, now_ns=None):
    """A18 on the monotonic clock and, given now_ns, on the wall clock too: the monotonic clock stops
    while a Mac sleeps, so a book taken before a sleep must not pass on it alone."""
    if not isinstance(book, dict) or type(now_monotonic_ns) is not int:
        return False
    if (book.get("fetched_by") != "gateway_self_fetch" or book.get("app_key_slot") != "live"
            or book.get("is_market_data_delayed") is not False or book.get("catalogue_present") is not True):
        return False
    taken = book.get("monotonic_ns")
    if type(taken) is not int:
        return False
    limit = int(config["orders"]["caps"]["max_book_age_s"] * _NS_PER_S)
    age = now_monotonic_ns - taken
    if not 0 <= age <= limit:
        return False
    if now_ns is None:
        return True
    try:
        wall_age = now_ns - parse_utc_ns(book.get("observed_at"))
    except (TypeError, ValueError):
        return False
    return 0 <= wall_age <= limit


def _unresolved(journal_state):
    for order in journal_state.get("orders", {}).values():
        if order["state"] in UNRESOLVED_STATES:
            return True
        if order.get("provisional_child") is True and order["state"] not in ("RECORDED", "REFUSED", "FAILED"):
            return True
    return bool(journal_state.get("unresolved_markets"))


def dynamic_arming(config, *, command, session, account_details, journal_state, startup, book, paths=None,
                   journal_fingerprint=None, now_monotonic_ns=None, now_ns=None):
    """Checks A14-A19 that apply to command -> {"armed", "failures", "checked", "by_check"}.

    book None defers A18 (it is checked when the gateway's own fresh book exists); A18 is then left
    out of "checked". paths (accounts.json) and journal_fingerprint (journal meta) are additions for
    A15, which fails closed without paths. now_monotonic_ns measures A18's book age; now_ns (an
    addition; the live gateway always passes it) also measures it on the wall clock.
    """
    checks = [c for c in checks_for(command) if c in DYNAMIC_CHECKS and not (c == "A18" and book is None)]
    by_check = {}
    for check in checks:
        failed = False
        if check == "A14":
            if command in LOGIN_ONLY_COMMANDS:
                failed = session is None or session.current_token() is None
            else:
                failed = session is None or session.can_bet() is not True
        elif check == "A15":
            try:
                fingerprint = session.account_fingerprint()
                registry = read_registry(paths) if paths is not None else None
            except (AuthError, NotArmed, AttributeError):
                fingerprint, registry = None, None
            failed = (fingerprint is None or registry is None
                      or registry.get(fingerprint, paths.profile) != paths.profile
                      or (journal_fingerprint is not None and journal_fingerprint != fingerprint))
        elif check == "A16":
            failed = not isinstance(account_details, dict) or account_details.get("currencyCode") != ACCOUNT_CURRENCY
        elif check == "A17":
            failed = (not isinstance(startup, dict) or startup.get("foreign") != []
                      or startup.get("contradictions", []) != []
                      or not isinstance(journal_state, dict) or _unresolved(journal_state))
        elif check == "A18":
            failed = not _book_fresh(config, book, now_monotonic_ns, now_ns)
        elif check == "A19":
            failed = not isinstance(journal_state, dict) or journal_state.get("ref_roundtrip_verified") is not True
        by_check[check] = list(FAILURE_CODES[check][:1]) if failed else []
    return _report(checks, by_check)


def combine(*reports):
    """One arming result from several (static, dynamic): armed only if every part is armed."""
    failures, checked = [], []
    by_check = {}
    for report in reports:
        for code in report["failures"]:
            if code not in failures:
                failures.append(code)
        for check in report["checked"]:
            if check not in checked:
                checked.append(check)
        by_check.update(report.get("by_check", {}))
    armed = all(report["armed"] is True for report in reports) and not failures
    return {"armed": armed, "failures": failures, "checked": checked, "by_check": by_check}
