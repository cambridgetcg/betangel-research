"""Betfair SSO session: certificate and interactive login, keepAlive, logout and the persistent login ledger.

The session token lives in memory only, as a Secret. Nothing here writes the token, username,
password or app key anywhere: the ledger holds times, methods, jurisdictions and status codes.
Every login attempt is reserved in auth/login-ledger.jsonl under a file lock before any byte is
sent, and its outcome is appended afterwards, so the local limits hold across restarts, crashes
and concurrent processes:
- a persisted TEMPORARY_BAN lockout stops every caller until it expires;
- max_consecutive_failed_logins failures with no success between them stop every caller, emergency
  logins included, until the operator appends a reset row (login-reset);
- the successful-login cap per 600 s (far below Betfair's 100 per minute) stops everything except
  the emergency logins that kill, cancel and reconcile use.
An attempt whose outcome is unknown (the request may have arrived, the response was unreadable, or
the process died mid-login) counts as a failure for the failure limit and as a success for the
cap. An attempt with no outcome row yet counts toward the cap at once but as a failure only once
it is older than a login could take (2 x http.timeout_s + 10 s), so another process's login in
flight never blocks an emergency login. Logins are never resent; a relogin is a new, counted attempt.

A ledger line that is not a valid row (a torn append, garbage, a hand edit) fails every login
closed until login-reset, which appends a repair reset: lines before it are skipped from then on,
and because a skipped line could have been a ban, the repair row carries a ban of LOGIN_BAN_S.
"""

import hashlib
import secrets
from urllib.parse import urlencode

from .config import allowed_http_hosts, hosts_for
from .constants import APP_KEY_SLOTS, LOGIN_BAN_S, SSO_PATHS
from .errors import (AuthError, BetfairError, ConfigError, CredentialError, LoginBanned, LoginBudgetExceeded,
                     LoginStopped, TransportError, classify_login, safe_code)
from .paths import file_lock
from .receipts import SYSTEM_CLOCK, append_jsonl, end_torn_line, parse_utc_ns, strict_json_loads, utc_from_ns
from .secret import Secret
from .transport import HttpTransport, make_tls_context

SUCCESS_WINDOW_S = 600
MIN_KEEPALIVE_INTERVAL_S = 60
# After a keepAlive that did not succeed, maybe_keep_alive waits this long (at most one interval).
KEEPALIVE_RETRY_S = 60
# An attempt with no outcome yet is in flight, not failed, for 2 x http.timeout_s plus this margin.
LOGIN_PENDING_MARGIN_S = 10
LEDGER_LOCK_NAME = ".login-ledger.lock"
LEDGER_LOCK_TIMEOUT_S = 10
LEDGER_KINDS = ("login_attempt", "login_outcome", "login_reset", "note")
# classify_login outcomes plus the outcomes a request can have without a parsed answer.
SUCCESS_OUTCOMES = frozenset({"proceed", "reads_only"})
NOT_SENT_OUTCOME = "not_sent"
# Betfair issued a token that is not kept (certlogin other than SUCCESS, or a refusal code beside a
# token): a failure, but a session was created, so it counts toward the cap too.
SESSION_REFUSED_OUTCOME = "session_refused"
# Outcomes that may have created a session; an unresolved attempt (None) is counted the same way.
_MAY_HAVE_SUCCEEDED = frozenset({"proceed", "reads_only", "unknown", "invalid_response", SESSION_REFUSED_OUTCOME,
                                 None})
_SESSION_KEYS = ("username", "password", "app_key")


def _json_object(body):
    """The body as a strictly parsed JSON object, or None. Parser messages (which quote the body) are dropped."""
    try:
        value = strict_json_loads(bytes(body or b"").decode("utf-8"))
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _code_or_none(value):
    return safe_code(value) if isinstance(value, str) and value else None


def _issued_token(raw):
    return Secret(raw) if isinstance(raw, str) and raw else None


def _parse_cert_login(body):
    """(parsed, issued): parse_cert_login's dict and any token the body carried, kept or not."""
    data = _json_object(body)
    if data is None or not isinstance(data.get("loginStatus"), str) or not data["loginStatus"]:
        raise AuthError("login_response_invalid")
    issued = _issued_token(data.get("sessionToken"))
    status = safe_code(data["loginStatus"])
    if status == "SUCCESS" and issued is None:
        raise AuthError("login_response_invalid")
    return {"status": status, "token": issued if status == "SUCCESS" else None}, issued


def parse_cert_login(body):
    """{"status": loginStatus, "token": Secret|None} from a certlogin response body.

    Success is {"sessionToken", "loginStatus": "SUCCESS"}; any other loginStatus is a failure and
    returns no token. Any token-shaped value is wrapped in a Secret (and so registered for leak
    scans) even when it is not returned. A body without a loginStatus, or SUCCESS without a token,
    raises AuthError("login_response_invalid").
    """
    return _parse_cert_login(body)[0]


def _parse_interactive_login(body):
    """(parsed, issued): parse_interactive_login's dict and any token the body carried, kept or not."""
    data = _json_object(body)
    if data is None or not isinstance(data.get("status"), str) or not data["status"]:
        raise AuthError("login_response_invalid")
    issued = _issued_token(data.get("token"))
    status = safe_code(data["status"])
    return {"status": status, "error": _code_or_none(data.get("error")),
            "token": issued if status in ("SUCCESS", "LIMITED_ACCESS") else None,
            "product_present": bool(data.get("product"))}, issued


def parse_interactive_login(body):
    """{"status", "error", "token": Secret|None, "product_present": bool} from an /api/login body.

    The response is {token, product, status, error}. product names the app key, so only its
    presence is kept. A token is returned only for SUCCESS and LIMITED_ACCESS.
    """
    return _parse_interactive_login(body)[0]


def _parse_sso_status(body):
    """{"status", "error"} from a keepAlive or logout body; the echoed token and product are never kept."""
    data = _json_object(body)
    if data is None or not isinstance(data.get("status"), str) or not data["status"]:
        return {"status": "FAIL", "error": "invalid_response"}
    return {"status": safe_code(data["status"]), "error": _code_or_none(data.get("error"))}


# --- Login ledger ----------------------------------------------------------------------------


def _ledger_row(line):
    """One ledger line (bytes) as a row dict, or None when it is not a valid row."""
    try:
        row = strict_json_loads(line.decode("utf-8"))
        if not isinstance(row, dict) or row.get("kind") not in LEDGER_KINDS or parse_utc_ns(row.get("at")) < 0:
            return None
        if row.get("ban_until") is not None:
            parse_utc_ns(row["ban_until"])
    except ValueError:            # UnicodeDecodeError included
        return None
    skipped = row.get("skipped_lines")
    if skipped is not None and (type(skipped) is not int or skipped < 0):
        return None
    return row


def _scan_ledger(path):
    """(rows, bad): valid rows as (line_index, row) in file order, and the indexes of invalid lines."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [], []
    except OSError:
        raise AuthError("login_ledger_unreadable") from None
    rows, bad = [], []
    for index, line in enumerate(data.split(b"\n")):
        if not line.strip():
            continue
        row = _ledger_row(line)
        if row is None:
            bad.append(index)
        else:
            rows.append((index, row))
    return rows, bad


def _repair_barrier(rows):
    """Line index of the last repair reset (a reset that skipped lines), or -1."""
    return max((index for index, row in rows if row["kind"] == "login_reset" and row.get("skipped_lines")),
               default=-1)


def _read_ledger(path):
    """Every ledger row in file order. An invalid line fails closed (it could hide a ban) unless a
    repair reset written after it skipped it."""
    rows, bad = _scan_ledger(path)
    if bad and max(bad) > _repair_barrier(rows):
        raise AuthError("login_ledger_unreadable")
    return [row for _, row in rows]


# The torn-line guard lives in receipts.py, shared with the recordings run-ledger (recording design v9 §3.8 step 1).
_end_torn_line = end_torn_line


def _append_ledger(path, row):
    _end_torn_line(path)
    append_jsonl(path, row)


def _pending_s(config):
    """How long an attempt without an outcome row may still be in flight in another process."""
    return 2 * config["http"]["timeout_s"] + LOGIN_PENDING_MARGIN_S


def _summarise(rows, now_ns, pending_s):
    """{"banned_until_ns", "consecutive_failures", "successes_in_window"} from ledger rows.

    Failures are counted in event order: a resolved attempt sits where its outcome was written, an
    unresolved one where it was reserved. A success or a reset clears the count; not_sent changes
    nothing; every other outcome, unknown included, adds one. An unresolved attempt reserved less
    than pending_s ago may still be in flight and adds nothing yet (an older one, or one dated in
    the future, adds one). Every attempt that may have created a session counts toward the cap.
    """
    outcomes = {}
    banned_until_ns = None
    for position, row in enumerate(rows):
        if row["kind"] == "login_outcome":
            outcomes[row.get("attempt_id")] = (position, row.get("outcome"))
        if row.get("ban_until") is not None:
            until = parse_utc_ns(row["ban_until"])
            banned_until_ns = until if banned_until_ns is None else max(banned_until_ns, until)
    events = []
    successes = 0
    window_start = now_ns - SUCCESS_WINDOW_S * 1_000_000_000
    for position, row in enumerate(rows):
        if row["kind"] == "login_reset":
            events.append((position, "reset"))
        elif row["kind"] == "login_attempt":
            resolved = outcomes.get(row.get("attempt_id"))
            outcome = resolved[1] if resolved is not None else None
            if resolved is None:
                age_ns = now_ns - parse_utc_ns(row["at"])
                effect = "neutral" if 0 <= age_ns < pending_s * 1_000_000_000 else "failure"
            elif outcome in SUCCESS_OUTCOMES:
                effect = "success"
            elif outcome == NOT_SENT_OUTCOME:
                effect = "neutral"
            else:
                effect = "failure"
            events.append((resolved[0] if resolved is not None else position, effect))
            if outcome in _MAY_HAVE_SUCCEEDED and parse_utc_ns(row["at"]) > window_start:
                successes += 1
    failures = 0
    for _, effect in sorted(events):
        if effect in ("reset", "success"):
            failures = 0
        elif effect == "failure":
            failures += 1
    return {"banned_until_ns": banned_until_ns, "consecutive_failures": failures, "successes_in_window": successes}


def _ledger_lock(paths, clock):
    return file_lock(paths.auth_dir / LEDGER_LOCK_NAME, timeout_s=LEDGER_LOCK_TIMEOUT_S, clock=clock)


def ledger_status(config, paths, *, clock=SYSTEM_CLOCK):
    """What the ledger allows now, for status/doctor: no secrets, no network."""
    now_ns = clock.time_ns()
    summary = _summarise(_read_ledger(paths.login_ledger), now_ns, _pending_s(config))
    banned = summary["banned_until_ns"] is not None and summary["banned_until_ns"] > now_ns
    failures_block = summary["consecutive_failures"] >= config["session"]["max_consecutive_failed_logins"]
    cap_blocks = summary["successes_in_window"] >= config["session"]["max_successful_logins_per_10min"]
    return {"banned_until": utc_from_ns(summary["banned_until_ns"]) if banned else None,
            "consecutive_failures": summary["consecutive_failures"],
            "successes_last_600s": summary["successes_in_window"],
            "login_allowed": not (banned or failures_block or cap_blocks),
            "emergency_login_allowed": not (banned or failures_block)}


def ledger_review(config, paths, *, clock=SYSTEM_CLOCK):
    """What recording admission's step 8 reads (admission.py): no lock, no write, no network.

    The barrier is the last login_reset row or the last outcome row whose outcome is a success (the rows
    _summarise lets clear the failure count). After it, every failure outcome counts (not_sent changes
    nothing, as in _summarise) and so does every login_attempt with no outcome row anywhere in the
    ledger, whatever its age: unlike _summarise, an attempt still in flight is not given the benefit of
    the doubt. A ban in force counts whatever the barrier. An invalid line raises
    AuthError("login_ledger_unreadable") exactly as ledger_status does (a repair reset still skips the
    lines before it); the repair reset itself is a login_reset row, so it is a barrier, and its ban
    counts while in force. Clean means no ban, no consecutive failure, and nothing after the barrier.
    """
    now_ns = clock.time_ns()
    rows = _read_ledger(paths.login_ledger)
    summary = _summarise(rows, now_ns, _pending_s(config))
    banned = summary["banned_until_ns"] is not None and summary["banned_until_ns"] > now_ns
    barrier, barrier_row = -1, None
    for position, row in enumerate(rows):
        if row["kind"] == "login_reset" or (row["kind"] == "login_outcome" and row.get("outcome") in SUCCESS_OUTCOMES):
            barrier, barrier_row = position, row
    resolved = {row.get("attempt_id") for row in rows if row["kind"] == "login_outcome"}
    failures = sum(1 for position, row in enumerate(rows) if position > barrier and row["kind"] == "login_outcome"
                   and row.get("outcome") not in SUCCESS_OUTCOMES and row.get("outcome") != NOT_SENT_OUTCOME)
    unresolved = sum(1 for position, row in enumerate(rows) if position > barrier and row["kind"] == "login_attempt"
                     and row.get("attempt_id") not in resolved)
    clean = not banned and summary["consecutive_failures"] == 0 and failures == 0 and unresolved == 0
    return {"banned_until": utc_from_ns(summary["banned_until_ns"]) if banned else None,
            "consecutive_failures": summary["consecutive_failures"],
            "barrier": None if barrier_row is None else {
                "kind": "login_reset" if barrier_row["kind"] == "login_reset" else "success", "at": barrier_row["at"]},
            "failures_after_barrier": failures, "unresolved_attempts_after_barrier": unresolved, "clean": clean}


def append_login_reset(paths, *, clock=SYSTEM_CLOCK):
    """Append the operator's reset row (the CLI's login-reset, after its tty echo). A ban is not cleared.

    Invalid lines written since the last repair are skipped from now on (skipped_lines counts them;
    the bytes stay in the file). One of them could have been a ban row, so a repair carries
    ban_until = now + LOGIN_BAN_S (Betfair's ban length, counted from now, so it covers any ban
    issued before). A reset with nothing to skip has ban_until null.
    """
    paths.ensure()
    with _ledger_lock(paths, clock):
        rows, bad = _scan_ledger(paths.login_ledger)
        barrier = _repair_barrier(rows)
        skipped = sum(1 for index in bad if index > barrier)
        row = {"kind": "login_reset", "at": clock.utc_now(), "reason": "operator_login_reset",
               "skipped_lines": skipped,
               "ban_until": utc_from_ns(clock.time_ns() + LOGIN_BAN_S * 1_000_000_000) if skipped else None}
        _append_ledger(paths.login_ledger, row)
    return row


def account_fingerprint_for(username):
    """sha256("betfair-account:" + normalised username)[:16]; username is a Secret.

    The username is stripped and casefolded first, so "Name", "name" and " name" are one account to
    the registry. Whether Betfair treats usernames case-insensitively is unverified; folding can
    only make the one-profile-per-account rule refuse more, never less.
    """
    if not isinstance(username, Secret):
        raise CredentialError("username_must_be_secret")
    normalised = username.reveal().strip().casefold()
    return hashlib.sha256(("betfair-account:" + normalised).encode("utf-8")).hexdigest()[:16]


# --- Session manager -------------------------------------------------------------------------


def _check_credentials(credentials, config):
    if not isinstance(credentials, dict):
        raise CredentialError("credentials_must_be_object")
    for key in _SESSION_KEYS:
        if not isinstance(credentials.get(key), Secret):
            raise CredentialError(f"credential_missing:{key}")
    if credentials.get("app_key_slot") not in APP_KEY_SLOTS:
        raise CredentialError("invalid_app_key_slot")
    passphrase = credentials.get("key_passphrase")
    if passphrase is not None and not isinstance(passphrase, Secret):
        raise CredentialError("key_passphrase_must_be_secret")
    if config["credentials"]["login_method"] == "cert" and (credentials.get("cert_path") is None
                                                             or credentials.get("key_path") is None):
        raise CredentialError("certificate_login_needs_cert_and_key")


class SessionManager:
    """One Betfair session for one process. transport defaults to an HttpTransport for this config's hosts.

    coexistence_ack is the operator's Bet Angel statement (--bet-angel-paused), checked by the CLI
    before any login; it is recorded on every login this object makes, lazy logins and relogins
    included. An explicit login(coexistence_ack=True) sets it for the rest of the process.
    """

    def __init__(self, config, credentials, *, paths, transport=None, clock=SYSTEM_CLOCK, coexistence_ack=False):
        if type(coexistence_ack) is not bool:
            raise ValueError("coexistence_ack must be a boolean")
        if config.get("profile") != paths.profile:
            raise ConfigError("session_profile_does_not_match_state_profile")
        _check_credentials(credentials, config)
        self.config = config
        self.paths = paths
        self.clock = clock
        self._credentials = dict(credentials)
        self._hosts = hosts_for(config)
        self._method = config["credentials"]["login_method"]
        self._owns_transport = transport is None
        if transport is None:
            transport = HttpTransport(allowed_hosts=allowed_http_hosts(config),
                                      max_response_bytes=config["http"]["max_response_bytes"],
                                      idle_reconnect_s=config["http"]["idle_reconnect_s"], clock=clock)
        self.transport = transport
        self._tls_context = None
        self._token = None
        self._status = None
        self._can_bet = False
        self._logged_in_at = None
        self._fingerprint = None
        self._last_alive_ns = None
        self._keepalive_retry_ns = None
        self._keepalive_interval_s = config["session"]["keepalive_interval_s"]
        # emergency: the flag of the last successful login, which a relogin reuses.
        # coexistence_ack: once given (constructor or any login), recorded on every later login.
        self._emergency = False
        self._coexistence_ack = coexistence_ack
        self._relogin_active = False
        self._login_guard = None
        self._login_observer = None

    # -- properties and accessors --

    @property
    def app_key_slot(self):
        return self._credentials["app_key_slot"]

    @property
    def keepalive_interval_s(self):
        return self._keepalive_interval_s

    def app_key(self):
        """The app key loaded with the credentials (delayed or live, see app_key_slot)."""
        return self._credentials["app_key"]

    def current_token(self):
        """The session token, or None; never logs in (order sends use this, not token())."""
        return self._token

    def token(self):
        """The session token, logging in lazily when there is none, with the flags of the last successful
        login, as invalidate_and_relogin does (non-emergency before any login). A kill, cancel or reconcile
        session whose emergency relogin failed therefore logs in again as an emergency login, which skips the
        successful-login cap (amendment A3 section 4 step 7) but never a ban or the failure cap."""
        if self._token is None:
            self.login(emergency=self._emergency, coexistence_ack=self._coexistence_ack)
        return self._token

    def can_bet(self):
        """True only after a SUCCESS login; LIMITED_ACCESS, no session or anything unknown is False."""
        return self._token is not None and self._can_bet

    def account_fingerprint(self):
        if self._fingerprint is None:
            raise AuthError("not_logged_in")
        return self._fingerprint

    def describe(self):
        """Session state without secrets, for status output."""
        return {"logged_in": self._token is not None, "status": self._status, "method": self._method,
                "can_bet": self.can_bet(), "logged_in_at": self._logged_in_at, "app_key_slot": self.app_key_slot,
                "keepalive_interval_s": self._keepalive_interval_s}

    # -- login --

    def guard_logins(self, guard, on_outcome=None):
        """Install guard (None removes it, and its on_outcome with it): a callable checked at the start of every
        login, lazy logins and every relogin included (stream session errors, the REST retry, keepAlive's
        NO_SESSION), before any attempt is reserved or sent. A non-empty return refuses that login with
        LoginStopped(<return>). on_outcome, when given, is called with the ledger outcome of every attempt this
        session reserved, right after its outcome row is written (the recorder's per-run login stop, design v9 §3.7
        R3: its run refuses every later login after one failed or unknown outcome). The recorder installs both for
        its run (its stop flag and wall-clock end); logout, keepAlive itself and every caller that installs none
        are unchanged."""
        if guard is not None and not callable(guard):
            raise TypeError("login guard must be callable or None")
        if on_outcome is not None and (guard is None or not callable(on_outcome)):
            raise TypeError("login outcome observer must be callable and needs a guard")
        self._login_guard = guard
        self._login_observer = on_outcome

    def _url(self, host_kind, path_name):
        return f"https://{self._hosts[host_kind]}{SSO_PATHS[path_name]}"

    def _login_request(self):
        """(url, headers, body, tls_context). Building it may fail locally; nothing has been reserved yet."""
        body = urlencode({"username": self._credentials["username"].reveal(),
                          "password": self._credentials["password"].reveal()}).encode("ascii")
        headers = {"X-Application": self.app_key(), "Content-Type": "application/x-www-form-urlencoded"}
        if self._method == "cert":
            if self._tls_context is None:
                self._tls_context = make_tls_context(self._credentials["cert_path"], self._credentials["key_path"],
                                                     self._credentials.get("key_passphrase"))
            return self._url("sso_cert", "certlogin"), headers, body, self._tls_context
        headers["Accept"] = "application/json"
        return self._url("sso", "login"), headers, body, None

    def _reserve(self, *, emergency, coexistence_ack):
        """Check the persisted limits and reserve one attempt, atomically across processes."""
        self.paths.ensure()
        session_cfg = self.config["session"]
        with _ledger_lock(self.paths, self.clock):
            now_ns = self.clock.time_ns()
            summary = _summarise(_read_ledger(self.paths.login_ledger), now_ns, _pending_s(self.config))
            if summary["banned_until_ns"] is not None and summary["banned_until_ns"] > now_ns:
                raise LoginBanned(utc_from_ns(summary["banned_until_ns"]))
            if summary["consecutive_failures"] >= session_cfg["max_consecutive_failed_logins"]:
                raise AuthError("login_failures_need_operator")
            if not emergency and summary["successes_in_window"] >= session_cfg["max_successful_logins_per_10min"]:
                raise LoginBudgetExceeded()
            attempt_id = secrets.token_hex(8)
            _append_ledger(self.paths.login_ledger, {
                "kind": "login_attempt", "attempt_id": attempt_id, "at": self.clock.utc_now(), "method": self._method,
                "jurisdiction": self.config["jurisdiction"], "emergency": emergency,
                "coexistence_ack": coexistence_ack})
        return attempt_id

    def _record(self, attempt_id, *, outcome, outcome_status, outcome_error=None, emergency, coexistence_ack,
                ban_until=None):
        _append_ledger(self.paths.login_ledger, {
            "kind": "login_outcome", "attempt_id": attempt_id, "at": self.clock.utc_now(), "method": self._method,
            "jurisdiction": self.config["jurisdiction"], "outcome": outcome, "outcome_status": outcome_status,
            "outcome_error": outcome_error, "emergency": emergency, "coexistence_ack": coexistence_ack,
            "ban_until": ban_until})
        if self._login_observer is not None:
            self._login_observer(outcome)

    def _drop_session(self):
        self._token = None
        self._status = None
        self._can_bet = False
        self._logged_in_at = None
        self._last_alive_ns = None
        self._keepalive_retry_ns = None

    def _discard_issued(self, issued):
        """Log out (best effort) a token Betfair issued that this login does not keep."""
        if issued is None:
            return
        try:
            self._sso_call("logout", issued)
        except BetfairError:
            pass

    def login(self, *, emergency=False, coexistence_ack=False):
        """Log in once -> {"status", "method", "can_bet", "logged_in_at"}; never returns the token.

        Raises LoginBanned, LoginBudgetExceeded or AuthError (status codes only) when refused,
        LoginStopped when the login guard (guard_logins) refuses it before anything is reserved, and
        TransportError when the request failed; a login is never resent.
        """
        if type(emergency) is not bool or type(coexistence_ack) is not bool:
            raise ValueError("emergency and coexistence_ack must be booleans")
        if self._login_guard is not None:
            reason = self._login_guard()
            if reason:
                raise LoginStopped(reason)
        self._coexistence_ack = coexistence_ack = self._coexistence_ack or coexistence_ack
        url, headers, body, tls_context = self._login_request()
        attempt_id = self._reserve(emergency=emergency, coexistence_ack=coexistence_ack)
        self._drop_session()
        flags = {"emergency": emergency, "coexistence_ack": coexistence_ack}
        try:
            response = self.transport.request("POST", url, headers, body, deadline_s=self.config["http"]["timeout_s"],
                                              idempotent=False, tls_context=tls_context)
        except TransportError as exc:
            self._record(attempt_id, outcome="unknown" if exc.may_have_arrived else NOT_SENT_OUTCOME,
                         outcome_status=f"transport_{exc.phase}", outcome_error=exc.code, **flags)
            raise
        parsed = issued = None
        try:
            if self._method == "cert":
                parsed, issued = _parse_cert_login(response["body"])
                parsed["error"] = None
            else:
                parsed, issued = _parse_interactive_login(response["body"])
        except AuthError:
            parsed = None
        if parsed is None:
            self._record(attempt_id, outcome="invalid_response", outcome_status="invalid_response", **flags)
            raise AuthError("login_response_invalid")
        status, error = parsed["status"], parsed["error"]
        outcome = classify_login(status, error)
        if self._method == "cert" and status != "SUCCESS" and outcome in SUCCESS_OUTCOMES:
            outcome = "auth_error"     # spec section 6: certlogin is SUCCESS or a failure (LIMITED_ACCESS included)
        if outcome in SUCCESS_OUTCOMES and parsed["token"] is None:
            self._record(attempt_id, outcome="invalid_response", outcome_status=status, outcome_error=error, **flags)
            raise AuthError("login_response_invalid")
        if outcome == "login_ban":
            until = utc_from_ns(self.clock.time_ns() + self.config["session"]["ban_lockout_s"] * 1_000_000_000)
            self._record(attempt_id, outcome=outcome, outcome_status=status, outcome_error=error, ban_until=until,
                         **flags)
            raise LoginBanned(until)
        if outcome not in SUCCESS_OUTCOMES and issued is not None:
            outcome = SESSION_REFUSED_OUTCOME
        self._record(attempt_id, outcome=outcome, outcome_status=status, outcome_error=error, **flags)
        if outcome not in SUCCESS_OUTCOMES:
            self._discard_issued(issued)
            raise AuthError(status, error_code=error)
        self._token = parsed["token"]
        self._status = status
        self._can_bet = outcome == "proceed"
        self._logged_in_at = self.clock.utc_now()
        self._last_alive_ns = self.clock.monotonic_ns()
        self._keepalive_retry_ns = None
        self._fingerprint = account_fingerprint_for(self._credentials["username"])
        self._emergency = emergency
        return {"status": status, "method": self._method, "can_bet": self._can_bet, "logged_in_at": self._logged_in_at}

    def invalidate_and_relogin(self):
        """Drop the token and log in again with the last login's flags. Never nested: a relogin that
        itself meets an invalid session raises instead of looping."""
        if self._relogin_active:
            raise AuthError("relogin_already_in_progress")
        self._relogin_active = True
        try:
            self._drop_session()
            return self.login(emergency=self._emergency, coexistence_ack=self._coexistence_ack)
        finally:
            self._relogin_active = False

    # -- keepAlive and logout --

    def _sso_call(self, path_name, token):
        headers = {"Accept": "application/json", "X-Authentication": token, "X-Application": self.app_key()}
        response = self.transport.request(self.config["session"]["sso_http_method"], self._url("sso", path_name),
                                          headers, None, deadline_s=self.config["http"]["timeout_s"], idempotent=True)
        return _parse_sso_status(response["body"])

    def _keepalive_failed(self):
        """Hold maybe_keep_alive off for KEEPALIVE_RETRY_S (at most one interval) after a failed keepAlive."""
        delay_s = min(KEEPALIVE_RETRY_S, self._keepalive_interval_s)
        self._keepalive_retry_ns = self.clock.monotonic_ns() + delay_s * 1_000_000_000

    def keep_alive(self):
        """keepAlive -> {"status", "error"}. Success needs status SUCCESS; a missing error is not an error.

        Anything else (and a raised error) holds maybe_keep_alive off for KEEPALIVE_RETRY_S, so an
        answer that keeps failing (a 405 from the wrong sso_http_method, say) is not resent on every
        poll. On NO_SESSION the session was shorter than configured (Logout Preferences can shorten
        it): a note goes to the ledger, the interval halves for this process (not below 60 s) and
        the session is replaced by one relogin.
        """
        token = self._token
        if token is None:
            raise AuthError("not_logged_in")
        try:
            result = self._sso_call("keep_alive", token)
        except BetfairError:
            self._keepalive_failed()
            raise
        if result["status"] == "SUCCESS":
            self._last_alive_ns = self.clock.monotonic_ns()
            self._keepalive_retry_ns = None
            return result
        self._keepalive_failed()
        if result["error"] == "NO_SESSION":
            self._keepalive_interval_s = max(MIN_KEEPALIVE_INTERVAL_S, self._keepalive_interval_s // 2)
            self.paths.ensure()
            _append_ledger(self.paths.login_ledger, {"kind": "note", "at": self.clock.utc_now(),
                                                     "note": "session_shorter_than_configured",
                                                     "keepalive_interval_s": self._keepalive_interval_s})
            self.invalidate_and_relogin()
        return result

    def maybe_keep_alive(self):
        """keep_alive() when the current interval has passed since the last success (or login) and no
        failed keepAlive is waiting out its retry delay; else None."""
        if self._token is None or self._last_alive_ns is None:
            return None
        now_ns = self.clock.monotonic_ns()
        if self._keepalive_retry_ns is not None and now_ns < self._keepalive_retry_ns:
            return None
        if now_ns - self._last_alive_ns < self._keepalive_interval_s * 1_000_000_000:
            return None
        return self.keep_alive()

    def logout(self):
        """logout -> {"status", "error"}. The token is dropped first and never used again, whatever the outcome."""
        token = self._token
        if token is None:
            return {"status": "not_logged_in", "error": None}
        self._drop_session()
        return self._sso_call("logout", token)

    def close(self):
        """Log out if logged in (best effort), then close a transport this object created."""
        result = None
        try:
            if self._token is not None:
                result = self.logout()
        except BetfairError as exc:
            result = {"status": "logout_failed", "error": getattr(exc, "code", None) or type(exc).__name__}
        finally:
            if self._owns_transport:
                self.transport.close()
        return result
