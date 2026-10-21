"""Exception hierarchy and the (surface, code) error taxonomy for the Betfair client.

str() of every exception here is safe code/category text only: never a token, header, request or
response body, username or app key. The same code can mean different things on different
surfaces (UNEXPECTED_ERROR is unknown_outcome for the betting API but stream_reconnect on the
stream), so every lookup names its surface. Anything not listed is "unrecognized".
"""

import re

_SAFE_CODE_RE = re.compile(r"[A-Za-z0-9_\-.:]{1,80}")
MALFORMED_CODE = "malformed_code"


def safe_code(value):
    """Code text as it may appear in str(): short [A-Za-z0-9_-.:] text, else "malformed_code".

    Callers sometimes hold server text (an error.message, a stream errorMessage); anything that
    is not plainly a code, or that contains a registered secret value, is replaced, so free text
    never reaches exception output.
    """
    if type(value) is int:
        value = str(value)
    if isinstance(value, str) and _SAFE_CODE_RE.fullmatch(value):
        from .secret import contains_registered     # imported here: secret.py imports this module
        if not contains_registered(value):
            return value
    return MALFORMED_CODE


class BetfairError(Exception):
    """Base class; the message is a safe code, never response or credential text."""


class ConfigError(BetfairError, ValueError):
    pass


class CredentialError(BetfairError):
    def __init__(self, message, *, failures=None):
        super().__init__(message)
        self.failures = list(failures or [])


class SecretLeak(BetfairError):
    pass


class LockBusy(BetfairError):
    pass


class AdmissionRefused(BetfairError):
    """A recorder start, a live admission or a release refused (admission.py). str() is the outcome code,
    then ":" and a safe detail code when there is one; owner and generation name the admission in force."""

    def __init__(self, outcome, *, detail=None, owner=None, generation=None, problems=()):
        outcome = safe_code(outcome)
        detail = safe_code(detail) if detail is not None else None
        super().__init__(outcome if detail is None else f"{outcome}:{detail}")
        self.outcome = outcome
        self.detail = detail
        self.owner = safe_code(owner) if owner is not None else None
        self.generation = generation if type(generation) is int else None
        self.problems = [safe_code(problem) for problem in problems]


class RecorderRunning(AdmissionRefused, LockBusy):
    """Another recorder holds recordings/.recorder.lock (a LockBusy too, as the lock refusal always was)."""


TRANSPORT_PHASES = ("preflight", "connect", "send", "receive", "decode")


class TransportError(BetfairError):
    """phase says how far the request got; may_have_arrived is True once any request byte was written.

    When may_have_arrived is not given it is derived conservatively: False only for preflight and
    connect failures, True otherwise, so an unknown outcome is never mistaken for "not sent".
    """

    def __init__(self, code, *, phase="preflight", may_have_arrived=None):
        if phase not in TRANSPORT_PHASES:
            raise ValueError("unknown transport phase")
        if may_have_arrived is None:
            may_have_arrived = phase not in ("preflight", "connect")
        if type(may_have_arrived) is not bool:
            raise ValueError("may_have_arrived must be boolean")
        code = safe_code(code)
        super().__init__(f"{phase}:{code}")
        self.code = code
        self.phase = phase
        self.may_have_arrived = may_have_arrived


class ApiError(BetfairError):
    def __init__(self, code, *, surface, category=None, request_uuid=None):
        if surface not in SURFACES:
            raise ValueError("unknown error surface")
        code = safe_code(code)
        if category is None:
            category = classify(code, surface=surface)
        elif category not in CATEGORIES:
            # A mistyped category would bypass the table that decides FAILED vs UNKNOWN.
            raise ValueError("unknown error category")
        if request_uuid is not None:
            request_uuid = safe_code(request_uuid)
        super().__init__(f"{surface}:{code}:{category}")
        self.surface = surface
        self.code = code
        self.category = category
        self.request_uuid = request_uuid


class AuthError(BetfairError):
    def __init__(self, status, *, error_code=None):
        status = safe_code(status)
        if error_code is not None:
            error_code = safe_code(error_code)
        super().__init__(status if error_code is None else f"{status}:{error_code}")
        self.status = status
        self.error_code = error_code


class LoginBanned(AuthError):
    def __init__(self, until_utc):
        super().__init__("login_banned", error_code="TEMPORARY_BAN_TOO_MANY_REQUESTS")
        self.until_utc = until_utc
        self.args = (f"login_banned_until:{safe_code(until_utc)}",)


class LoginBudgetExceeded(AuthError):
    def __init__(self, status="login_budget_exceeded", *, error_code=None):
        super().__init__(status, error_code=error_code)


class LoginStopped(AuthError):
    """The session's login guard refused a login before any attempt was reserved (a recorder that must stop or
    whose end instant passed); nothing was sent and the login ledger is unchanged."""

    def __init__(self, reason):
        super().__init__("login_stopped", error_code=reason)
        self.reason = self.error_code


class StreamError(BetfairError):
    def __init__(self, code, *, connection_closed=True):
        if type(connection_closed) is not bool:
            connection_closed = True
        code = safe_code(code)
        super().__init__(code)
        self.code = code
        self.connection_closed = connection_closed


class OrderRefused(BetfairError):
    """rule is a snake_case refusal code; detail is a safe dict (codes, counts, ids, pence)."""

    def __init__(self, rule, detail=None):
        rule = safe_code(rule)
        super().__init__(rule)
        self.rule = rule
        self.detail = dict(detail or {})


class NotArmed(OrderRefused):
    pass


class Halted(OrderRefused):
    pass


class NotPresent(OrderRefused):
    pass


class JournalError(BetfairError):
    pass


SURFACES = ("aping", "account_aping", "execution_report", "instruction_report", "sso_login", "sso_session",
            "stream_status", "jsonrpc", "angx", "dsc", "http", "transport", "local")

CATEGORIES = ("session", "app_key", "rate_limited", "login_ban", "planner_bug", "terminal_account",
              "terminal_input", "market_state", "unknown_outcome", "partial", "replace_split", "no_change",
              "stream_fatal", "stream_backoff", "stream_reconnect", "stream_clock",
              "stream_subscription_limit", "unrecognized")

_TABLE = (
    ("session", "aping", ("INVALID_SESSION_INFORMATION", "NO_SESSION")),
    ("session", "account_aping", ("INVALID_SESSION_INFORMATION", "NO_SESSION")),
    ("session", "sso_session", ("NO_SESSION",)),
    ("session", "stream_status", ("INVALID_SESSION_INFORMATION", "NO_SESSION")),
    ("app_key", "aping", ("NO_APP_KEY", "INVALID_APP_KEY")),
    ("app_key", "account_aping", ("NO_APP_KEY", "INVALID_APP_KEY")),
    ("app_key", "stream_status", ("NO_APP_KEY", "INVALID_APP_KEY")),
    ("rate_limited", "aping", ("TOO_MANY_REQUESTS",)),
    # AccountAPINGException carries the same transient and input codes as APINGException (Accounts
    # Exceptions page, DD/2687902); they classify alike, so Accounts reads get the read retry too.
    ("rate_limited", "account_aping", ("TOO_MANY_REQUESTS",)),
    ("login_ban", "sso_login", ("TEMPORARY_BAN_TOO_MANY_REQUESTS",)),
    ("planner_bug", "aping", ("TOO_MUCH_DATA", "REQUEST_SIZE_EXCEEDS_LIMIT", "INVALID_INPUT_DATA")),
    ("planner_bug", "account_aping", ("INVALID_INPUT_DATA",)),
    ("planner_bug", "jsonrpc", ("-32700", "-32601", "-32602")),
    ("planner_bug", "dsc", ("DSC-0008", "DSC-0009", "DSC-0018", "DSC-0021", "DSC-0023", "DSC-0024")),
    ("terminal_account", "execution_report", (
        "INSUFFICIENT_FUNDS", "LOSS_LIMIT_EXCEEDED", "INVALID_ACCOUNT_STATE", "INVALID_WALLET_STATUS",
        "PERMISSION_DENIED", "REJECTED_BY_REGULATOR", "REGULATOR_IS_NOT_AVAILABLE", "NO_CHASING")),
    ("terminal_account", "instruction_report", ("INSUFFICIENT_FUNDS", "LOSS_LIMIT_EXCEEDED")),
    ("terminal_account", "aping", ("ACCESS_DENIED",)),
    ("terminal_account", "dsc", ("DSC-0015", "DSC-0034", "DSC-0035", "DSC-0036", "DSC-0037", "DSC-0038")),
    ("terminal_input", "instruction_report", (
        "INVALID_BET_SIZE", "INVALID_RUNNER", "INVALID_PRICE_EDIT", "INVALID_ODDS", "INVALID_PERSISTENCE_TYPE",
        "INVALID_BACK_LAY_COMBINATION", "INVALID_BID_TYPE", "INVALID_BET_ID", "TIME_IN_FORCE_CONFLICT",
        "UNEXPECTED_PERSISTENCE_TYPE", "INVALID_ORDER_TYPE", "UNEXPECTED_MIN_FILL_SIZE",
        "INVALID_CUSTOMER_ORDER_REF", "INVALID_MIN_FILL_SIZE")),
    ("terminal_input", "execution_report", (
        "INVALID_ORDER", "INVALID_MARKET_ID", "DUPLICATE_BETIDS", "TOO_MANY_INSTRUCTIONS",
        "INVALID_MARKET_VERSION", "INVALID_PROFIT_RATIO")),
    ("market_state", "execution_report", ("MARKET_SUSPENDED", "MARKET_NOT_OPEN_FOR_BETTING")),
    ("market_state", "instruction_report", (
        "MARKET_NOT_OPEN_FOR_BETTING", "MARKET_NOT_OPEN_FOR_BSP_BETTING", "BET_TAKEN_OR_LAPSED",
        "RUNNER_REMOVED", "BET_LAPSED_PRICE_IMPROVEMENT_TOO_LARGE")),
    ("unknown_outcome", "execution_report", ("TIMEOUT", "ERROR_IN_MATCHER", "SERVICE_UNAVAILABLE",
                                             "DUPLICATE_TRANSACTION")),
    ("unknown_outcome", "instruction_report", ("TIMEOUT", "BET_IN_PROGRESS", "ERROR_IN_MATCHER")),
    ("unknown_outcome", "aping", ("TIMEOUT_ERROR", "UNEXPECTED_ERROR", "SERVICE_BUSY")),
    ("unknown_outcome", "account_aping", ("TIMEOUT_ERROR", "UNEXPECTED_ERROR", "SERVICE_BUSY")),
    ("unknown_outcome", "dsc", ("DSC-0019",)),
    ("unknown_outcome", "jsonrpc", ("-32603",)),
    ("unknown_outcome", "http", tuple(f"HTTP_{n}" for n in range(500, 600))),
    ("unknown_outcome", "transport", ("may_have_arrived",)),
    ("partial", "execution_report", ("PROCESSED_WITH_ERRORS", "BET_ACTION_ERROR")),
    ("replace_split", "instruction_report", ("CANCELLED_NOT_PLACED", "RELATED_ACTION_FAILED", "ERROR_IN_ORDER")),
    ("no_change", "execution_report", ("NO_ACTION_REQUIRED", "NO_CHANGE")),
    ("no_change", "instruction_report", ("NO_ACTION_REQUIRED",)),
    ("stream_fatal", "stream_status", ("NOT_AUTHORIZED", "INVALID_INPUT", "INVALID_REQUEST", "TIMEOUT")),
    ("stream_backoff", "stream_status", ("MAX_CONNECTION_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS")),
    ("stream_reconnect", "stream_status", ("UNEXPECTED_ERROR", "CONNECTION_FAILED")),
    ("stream_reconnect", "local", ("liveness_timeout", "line_too_long")),
    ("stream_clock", "stream_status", ("INVALID_CLOCK",)),
    ("stream_subscription_limit", "stream_status", ("SUBSCRIPTION_LIMIT_EXCEEDED",)),
)


def _build_categories():
    table = {}
    for category, surface, codes in _TABLE:
        if category not in CATEGORIES or surface not in SURFACES:
            raise RuntimeError("error taxonomy names an unknown category or surface")
        for code in codes:
            if (surface, code) in table:
                raise RuntimeError("error taxonomy assigns one (surface, code) twice")
            table[(surface, code)] = category
    return table


ERROR_CATEGORIES = _build_categories()


def classify(code, *, surface):
    """Category for one (surface, code); unknown pairs are "unrecognized", never a guess."""
    if surface not in SURFACES:
        raise ValueError("unknown error surface")
    if type(code) is int:
        code = str(code)
    if not isinstance(code, str):
        return "unrecognized"
    return ERROR_CATEGORIES.get((surface, code), "unrecognized")


LOGIN_OUTCOMES = ("proceed", "reads_only", "cert_setup_required", "operator_action_on_website", "login_ban",
                  "auth_error")


def classify_login(status, error=None):
    """Login outcome. The ban and *_REQUIRED codes are matched on either field, whatever status says."""
    codes = [c for c in (status, error) if isinstance(c, str) and c]
    if "TEMPORARY_BAN_TOO_MANY_REQUESTS" in codes:
        return "login_ban"
    if error == "STRONG_CODE_FAIL":
        return "auth_error"
    if error == "MULTI_FACTOR_AUTHENTICATION_REQUIRED":
        return "operator_action_on_website"
    if status == "SUCCESS":
        return "proceed"
    if status == "LIMITED_ACCESS":
        return "reads_only"
    if "CERT_AUTH_REQUIRED" in codes:
        return "cert_setup_required"
    if any(c.endswith("_REQUIRED") for c in codes):
        return "operator_action_on_website"
    return "auth_error"
