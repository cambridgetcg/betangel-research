"""Betfair client configuration: defaults, exact-key validation, absolute ceilings and hosts.

Production reads only STATE_ROOT/profiles/<profile>/config.json. Unknown keys are refused, a bool
is never a number, money has at most 2 dp, and every cap must sit at or below its compiled
ceiling. There is no mode key: live mode is chosen only by --mode live on each command, plus
arming. Keychain service names are compiled constants and cannot be configured.
"""

import copy
from decimal import Decimal, InvalidOperation
import math
import os
from pathlib import Path
import re

from .constants import (ABSOLUTE_CEILINGS, API_HOSTS, APP_KEY_SLOTS, CEILING_PATHS, COEXISTENCE_STATES,
                        COMMISSION_SOURCES, CUSTOMER_STRATEGY_REF_PATTERN, DEFAULT_API_HOST, IDLE_CLOSE_S,
                        JURISDICTIONS, KEEPALIVE_BOUNDS_S, LIVE_ACK_IDS, LOGIN_BAN_S, ORDER_VISIBILITY_S,
                        SSO_CERT_HOSTS, SSO_HOSTS, STREAM_HOSTS, STREAM_PORT)
from .errors import ConfigError
from .filters import validate_market_data_filter, validate_neutral_filter
from .paths import overrides_allowed, require_outside_worktrees, validate_profile
from .receipts import canonical_sha256, strict_json_loads

EXAMPLE_CONFIG_RELPATH = "research/betfair-config.example.json"

DEFAULTS = {
    "schema_version": 1,
    "profile": "default",
    "jurisdiction": "com",
    "api_host": "api.betfair.com",
    "account_currency": "GBP",
    "coexistence": {"bet_angel": "unverified"},
    "credentials": {
        "source": "keychain",
        "login_method": "cert",
        "cert_path": "~/.config/betangel-research/betfair/client-2048.crt",
        "key_path": "~/.config/betangel-research/betfair/client-2048.key",
        "key_passphrase_in_keychain": False,
    },
    "session": {"keepalive_interval_s": 3600, "sso_http_method": "POST", "max_successful_logins_per_10min": 30,
                "max_consecutive_failed_logins": 2, "ban_lockout_s": 1260},
    "http": {"timeout_s": 10, "order_deadline_s": 10, "max_response_bytes": 33554432, "idle_reconnect_s": 170},
    "read": {"app_key_slot": "delayed", "max_book_calls_per_second_per_market": 4, "order_projection_inflight": 2,
             "cleared_inflight": 2, "retry_backoff_s": [1, 4, 16], "out_root": None,
             "catalogue_cache_max_age_s": 21600},
    "stream": {"host": "stream-api.betfair.com", "port": 443, "app_key_slot": "delayed", "heartbeat_ms": 5000,
               "conflate_ms": None, "connect_timeout_s": 15, "max_line_bytes": 16777216,
               "reconnect_backoff_s": [5, 30, 120, 300], "max_reconnects_per_hour": 12},
    "recorder": {"root": None,
                 "market_filter": {"event_type_ids": ["7"], "country_codes": ["GB", "IE"], "market_types": ["WIN"]},
                 "market_data_filter": {"fields": ["EX_BEST_OFFERS", "EX_TRADED", "EX_TRADED_VOL", "EX_LTP",
                                                   "EX_MARKET_DEF"], "ladderLevels": 3},
                 "motif_compatible": True, "max_markets": 150, "refresh_s": 1800,
                 "max_duration_s": 57600, "max_bytes": 2000000000, "fsync_interval_s": 5, "allow_live_key": False},
    "orders": {
        "customer_strategy_ref": "bar-manual",
        "caps": {"max_stake_per_order": 2.0, "max_liability_per_order": 10.0, "max_market_exposure": 20.0,
                 "max_total_exposure": 50.0, "max_daily_loss": 20.0, "max_orders_per_minute": 6,
                 "max_orders_per_hour": 60, "max_orders_per_day": 200, "max_transactions_per_hour": 300,
                 "max_instructions_per_request": 1, "max_price_deviation_ticks": 10, "max_book_age_s": 2.0,
                 "funds_reserve": 0.0, "funds_max_age_s": 60, "max_minutes_before_off": 30},
        "rules": {"market_types": ["WIN"], "event_type_ids": ["7"], "min_stake": 1.0, "external_exposure_tolerance": 0.0},
        # commission_rate and commission_source are required and deliberately have no default.
        "paper": {"model": "paper-v1", "submit_latency_ms": 500, "displayed_size_fraction": 0.5,
                  "passive_fills": "trd_queue_conservative", "traded_volume_divisor": 2, "max_snapshot_age_s": 10},
        "reconcile": {"poll_interval_s": 1.0, "window_s": 15, "grace_s": 5, "min_successful_polls": 3,
                      "cleared_lookback_s": 3600},
        "live": {"armed": False, "acknowledgements": [], "operator_attests_uk_egress": False,
                 "heartbeat_api_enabled": False, "kill_cancels_own_orders": True},
    },
}

# Present in a loaded file but not in DEFAULTS (required, no default).
_REQUIRED_WITHOUT_DEFAULT = {("orders", "paper"): ("commission_rate", "commission_source")}
# Optional sections with no default (P1-06): a config without one keeps its config_sha256. orders.pilot is the
# registered pilot executor's section (RS-19); its keys are PILOT_KEYS, every one required when it is present.
_OPTIONAL_WITHOUT_DEFAULT = {("orders",): ("pilot",)}
PILOT_KEYS = ("contract_path", "contract_sha256", "evidence_deadline_s")
# A contract file in the repository's research/pilots directory, resolved against the repository root (never
# _local_path, which refuses any path inside a worktree).
PILOT_CONTRACT_PATH_RE = re.compile(r"research/pilots/[a-z0-9-]{1,40}\.json")
PILOT_EVIDENCE_DEADLINE_MAX_S = 5
# The fixed customerStrategyRef of every order that is not the pilot's under a pilot-enabled config (Astra's ruling
# on P1-06 critique 13): the config's own ref stays the pilot's (p1-fav-t5) through settlement.
MANUAL_STRATEGY_REF = "bar-manual"
# Values replaced wholesale instead of merged key by key.
_LEAF_OBJECTS = {("recorder", "market_filter"), ("recorder", "market_data_filter")}
_MONEY_CAPS = ("max_stake_per_order", "max_liability_per_order", "max_market_exposure", "max_total_exposure",
               "max_daily_loss")
_COUNT_CAPS = ("max_orders_per_minute", "max_orders_per_hour", "max_orders_per_day", "max_transactions_per_hour",
               "max_instructions_per_request", "max_price_deviation_ticks")
_SECONDS_CAPS = ("max_book_age_s", "funds_max_age_s", "max_minutes_before_off")
# Money above this is refused before any Decimal arithmetic (every money ceiling is far below it).
_MONEY_LIMIT = Decimal(10) ** 12
_KEY_TEXT_RE = re.compile(r"[A-Za-z0-9_\-.]{1,80}")


def _safe_key(key):
    """A config key as it may appear in an error message; anything else (a pasted secret) is masked."""
    return key if isinstance(key, str) and _KEY_TEXT_RE.fullmatch(key) else "<malformed_key>"


def _dotted(path):
    return ".".join(_safe_key(part) for part in path)


def _finite(value):
    """math.isfinite without the OverflowError a huge int raises."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _merge(defaults, loaded, path=()):
    if not isinstance(loaded, dict):
        raise ConfigError(f"config_object_expected:{_dotted(path) or 'root'}")
    allowed_extra = _REQUIRED_WITHOUT_DEFAULT.get(path, ()) + _OPTIONAL_WITHOUT_DEFAULT.get(path, ())
    merged = copy.deepcopy(defaults)
    for key, value in loaded.items():
        if key not in defaults and key not in allowed_extra:
            raise ConfigError(f"unknown_config_key:{_dotted(path + (key,))}")
        child = path + (key,)
        if key in defaults and isinstance(defaults[key], dict) and child not in _LEAF_OBJECTS:
            merged[key] = _merge(defaults[key], value, child)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path, *, paths):
    """Read, merge over DEFAULTS, validate; returns a validated deep copy.

    Only paths.config_path is accepted unless BETANGEL_BETFAIR_FORBID_NETWORK=1 is set in the process
    environment and in paths.env (the test harness, which also turns the network off).
    """
    path = Path(path)
    if (os.path.realpath(path) != os.path.realpath(paths.config_path)) and not overrides_allowed(paths.env):
        raise ConfigError("config_override_not_allowed")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError("config_missing") from None
    except (OSError, UnicodeDecodeError):
        raise ConfigError("config_unreadable") from None
    try:
        loaded = strict_json_loads(text)
    except ValueError:
        raise ConfigError("config_not_strict_json") from None
    return validate_config(_merge(DEFAULTS, loaded), paths=paths)


def _keys(section, expected, name):
    if not isinstance(section, dict):
        raise ConfigError(f"config_object_expected:{name}")
    if set(section) != set(expected):
        unknown = sorted(_safe_key(k) for k in set(section) - set(expected))
        missing = sorted(set(expected) - set(section))
        if unknown:
            raise ConfigError(f"unknown_config_key:{name}.{unknown[0]}")
        raise ConfigError(f"missing_config_key:{name}.{missing[0]}")


def _int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{name}_must_be_integer_{low}_to_{high}")
    return value


def _number(value, name, low, high, *, low_open=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
        raise ConfigError(f"{name}_must_be_finite_number")
    if (value <= low if low_open else value < low) or value > high:
        raise ConfigError(f"{name}_out_of_range")
    return value


def _choice(value, choices, name):
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(f"{name}_not_allowed")
    return value


def _bool(value, name):
    if type(value) is not bool:
        raise ConfigError(f"{name}_must_be_boolean")
    return value


def money_pence(value, *, allow_zero=False):
    """Finite, positive (or zero when allowed) money with at most 2 dp, as integer pence.

    Every failure is a ConfigError, never an ArithmeticError: values of 10**12 or more are refused
    before quantize, whose precision they would exceed.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
        raise ConfigError("money_must_be_finite_number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ConfigError("money_must_be_finite_number") from None
    if not amount.is_finite():
        raise ConfigError("money_must_be_finite_number")
    if abs(amount) >= _MONEY_LIMIT:
        raise ConfigError("money_out_of_range")
    try:
        quantized = amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ConfigError("money_out_of_range") from None
    if amount != quantized:
        raise ConfigError("money_must_have_at_most_2dp")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ConfigError("money_must_be_positive")
    return int(amount * 100)


def _money(value, name, *, allow_zero=False):
    try:
        money_pence(value, allow_zero=allow_zero)
    except ConfigError as exc:
        raise ConfigError(f"{name}:{exc}") from None
    return value


def _ceiling(config, key):
    value = config
    for part in CEILING_PATHS[key]:
        value = value[part]
    limit = ABSOLUTE_CEILINGS[key]
    if Decimal(str(value)) > Decimal(str(limit)):
        raise ConfigError(f"{key}_above_absolute_ceiling")


def _local_path(value, name, home, *, optional):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError(f"{name}_must_be_path_string")
    if value == "~" or value.startswith("~/"):
        value = str(Path(home) / value[2:]) if value != "~" else str(home)
    elif value.startswith("~"):
        raise ConfigError(f"{name}_other_user_home_not_allowed")
    if not os.path.isabs(value):
        raise ConfigError(f"{name}_must_be_absolute")
    require_outside_worktrees(value, what=name)
    return os.path.normpath(value)


def _str_list(value, name, pattern):
    if (not isinstance(value, list) or not value or len(value) != len(set(map(str, value)))
            or any(not isinstance(v, str) or not re.fullmatch(pattern, v) for v in value)):
        raise ConfigError(f"{name}_must_be_unique_string_list")
    return value


def _backoff_list(value, name, *, minimum, maximum, max_len):
    if not isinstance(value, list) or not 1 <= len(value) <= max_len:
        raise ConfigError(f"{name}_must_be_list")
    for item in value:
        _number(item, name, minimum, maximum)
    return value


def validate_config(config, *, paths):
    """Validate a complete config (every key present); returns a deep copy with "~" expanded."""
    if not isinstance(config, dict):
        raise ConfigError("config_object_expected:root")
    expected_paper = set(DEFAULTS["orders"]["paper"]) | set(_REQUIRED_WITHOUT_DEFAULT[("orders", "paper")])
    c = copy.deepcopy(config)
    _keys(c, DEFAULTS, "root")
    if type(c["schema_version"]) is not int or c["schema_version"] != 1:
        raise ConfigError("schema_version_must_be_1")
    validate_profile(c["profile"])
    if c["profile"] != paths.profile:
        raise ConfigError("config_profile_does_not_match_state_profile")
    jurisdiction = _choice(c["jurisdiction"], JURISDICTIONS, "jurisdiction")
    _choice(c["api_host"], API_HOSTS, "api_host")
    # Spec section 6, corrected (a deviation): api.betfair.com.au is the New Zealand API host (F12), and
    # Betfair documents NZ logins on the AUS/NZ (.com.au) SSO hosts, so only com.au may choose it;
    # every jurisdiction defaults to api.betfair.com and the rest may not change it. Live is com only.
    if c["api_host"] != DEFAULT_API_HOST and jurisdiction != "com.au":
        raise ConfigError("api_host_must_be_api_betfair_com_for_jurisdiction")
    if not isinstance(c["account_currency"], str) or not re.fullmatch(r"[A-Z]{3}", c["account_currency"]):
        raise ConfigError("account_currency_must_be_iso_code")

    _keys(c["coexistence"], DEFAULTS["coexistence"], "coexistence")
    _choice(c["coexistence"]["bet_angel"], COEXISTENCE_STATES, "coexistence.bet_angel")

    cred = c["credentials"]
    _keys(cred, DEFAULTS["credentials"], "credentials")
    _choice(cred["source"], ("keychain",), "credentials.source")
    method = _choice(cred["login_method"], ("cert", "interactive"), "credentials.login_method")
    for key in ("cert_path", "key_path"):
        cred[key] = _local_path(cred[key], key, paths.home, optional=method != "cert")
    _bool(cred["key_passphrase_in_keychain"], "credentials.key_passphrase_in_keychain")

    session = c["session"]
    _keys(session, DEFAULTS["session"], "session")
    low, high = KEEPALIVE_BOUNDS_S[jurisdiction]
    _int(session["keepalive_interval_s"], "session.keepalive_interval_s", low, high)
    _choice(session["sso_http_method"], ("POST", "GET"), "session.sso_http_method")
    _int(session["max_successful_logins_per_10min"], "session.max_successful_logins_per_10min", 1, 1000)
    _int(session["max_consecutive_failed_logins"], "session.max_consecutive_failed_logins", 1, 1000)
    _int(session["ban_lockout_s"], "session.ban_lockout_s", LOGIN_BAN_S, 86400)

    http = c["http"]
    _keys(http, DEFAULTS["http"], "http")
    _number(http["timeout_s"], "http.timeout_s", 0, 60, low_open=True)
    _number(http["order_deadline_s"], "http.order_deadline_s", 0, 1000, low_open=True)
    _int(http["max_response_bytes"], "http.max_response_bytes", 1024, 256 * 1024 * 1024)
    _number(http["idle_reconnect_s"], "http.idle_reconnect_s", 0, IDLE_CLOSE_S, low_open=True)
    if http["idle_reconnect_s"] >= IDLE_CLOSE_S:
        raise ConfigError("http.idle_reconnect_s_must_be_below_betfair_idle_close")

    read = c["read"]
    _keys(read, DEFAULTS["read"], "read")
    _choice(read["app_key_slot"], APP_KEY_SLOTS, "read.app_key_slot")
    _int(read["max_book_calls_per_second_per_market"], "read.max_book_calls_per_second_per_market", 1, 4)
    _int(read["order_projection_inflight"], "read.order_projection_inflight", 1, 2)
    _int(read["cleared_inflight"], "read.cleared_inflight", 1, 2)
    _backoff_list(read["retry_backoff_s"], "read.retry_backoff_s", minimum=0, maximum=300, max_len=5)
    read["out_root"] = _local_path(read["out_root"], "out_root", paths.home, optional=True)
    _int(read["catalogue_cache_max_age_s"], "read.catalogue_cache_max_age_s", 0, 7 * 86400)

    stream = c["stream"]
    _keys(stream, DEFAULTS["stream"], "stream")
    _choice(stream["host"], STREAM_HOSTS, "stream.host")
    if type(stream["port"]) is not int or stream["port"] != STREAM_PORT:
        raise ConfigError("stream.port_must_be_443")
    _choice(stream["app_key_slot"], APP_KEY_SLOTS, "stream.app_key_slot")
    _int(stream["heartbeat_ms"], "stream.heartbeat_ms", 500, 5000)
    if stream["conflate_ms"] is not None:
        _int(stream["conflate_ms"], "stream.conflate_ms", 0, 120000)
    _number(stream["connect_timeout_s"], "stream.connect_timeout_s", 0, 60, low_open=True)
    _int(stream["max_line_bytes"], "stream.max_line_bytes", 1024, 64 * 1024 * 1024)
    _backoff_list(stream["reconnect_backoff_s"], "stream.reconnect_backoff_s", minimum=1, maximum=3600, max_len=10)
    _int(stream["max_reconnects_per_hour"], "stream.max_reconnects_per_hour", 1, 60)

    rec = c["recorder"]
    _keys(rec, DEFAULTS["recorder"], "recorder")
    rec["root"] = _local_path(rec["root"], "root", paths.home, optional=True)
    rec["market_filter"] = validate_neutral_filter(rec["market_filter"])
    _bool(rec["motif_compatible"], "recorder.motif_compatible")
    rec["market_data_filter"] = validate_market_data_filter(rec["market_data_filter"],
                                                            motif_compatible=rec["motif_compatible"])
    _int(rec["max_markets"], "recorder.max_markets", 1, 1000)
    _int(rec["refresh_s"], "recorder.refresh_s", 60, 86400)
    _int(rec["max_duration_s"], "recorder.max_duration_s", 1, 7 * 86400)
    _int(rec["max_bytes"], "recorder.max_bytes", 1_000_000, 10 ** 13)
    _number(rec["fsync_interval_s"], "recorder.fsync_interval_s", 0, 60, low_open=True)
    _bool(rec["allow_live_key"], "recorder.allow_live_key")
    if stream["app_key_slot"] == "live" and not rec["allow_live_key"]:
        # Betfair does not permit read-only use of the live key.
        raise ConfigError("stream_live_key_needs_recorder_allow_live_key")

    orders = c["orders"]
    _keys(orders, set(DEFAULTS["orders"]) | ({"pilot"} if "pilot" in orders else set()), "orders")
    if "pilot" in orders:
        _pilot_section(orders["pilot"])
    if not isinstance(orders["customer_strategy_ref"], str) or not re.fullmatch(CUSTOMER_STRATEGY_REF_PATTERN,
                                                                                orders["customer_strategy_ref"]):
        raise ConfigError("orders.customer_strategy_ref_invalid")
    caps = orders["caps"]
    _keys(caps, DEFAULTS["orders"]["caps"], "orders.caps")
    for key in _MONEY_CAPS:
        _money(caps[key], f"orders.caps.{key}")
    _money(caps["funds_reserve"], "orders.caps.funds_reserve", allow_zero=True)
    for key in _COUNT_CAPS:
        _int(caps[key], f"orders.caps.{key}", 1, 10 ** 6)
    for key in _SECONDS_CAPS:
        _number(caps[key], f"orders.caps.{key}", 0, 10 ** 6, low_open=True)
    rules = orders["rules"]
    _keys(rules, DEFAULTS["orders"]["rules"], "orders.rules")
    _str_list(rules["market_types"], "orders.rules.market_types", r"[A-Z0-9_]{1,40}")
    _str_list(rules["event_type_ids"], "orders.rules.event_type_ids", r"[0-9]{1,12}")
    _money(rules["min_stake"], "orders.rules.min_stake")
    if money_pence(rules["min_stake"]) < 100:
        raise ConfigError("orders.rules.min_stake_below_1_00_unsupported")
    _money(rules["external_exposure_tolerance"], "orders.rules.external_exposure_tolerance", allow_zero=True)

    paper = orders["paper"]
    _keys(paper, expected_paper, "orders.paper")
    _choice(paper["model"], ("paper-v1",), "orders.paper.model")
    _int(paper["submit_latency_ms"], "orders.paper.submit_latency_ms", 0, 60000)
    _number(paper["displayed_size_fraction"], "orders.paper.displayed_size_fraction", 0, 1, low_open=True)
    _choice(paper["passive_fills"], ("trd_queue_conservative",), "orders.paper.passive_fills")
    _number(paper["traded_volume_divisor"], "orders.paper.traded_volume_divisor", 1, 100)
    _number(paper["max_snapshot_age_s"], "orders.paper.max_snapshot_age_s", 0, 86400, low_open=True)
    _number(paper["commission_rate"], "orders.paper.commission_rate", 0, 1)
    if paper["commission_rate"] >= 1:
        raise ConfigError("orders.paper.commission_rate_out_of_range")
    _choice(paper["commission_source"], COMMISSION_SOURCES, "orders.paper.commission_source")

    rec_cfg = orders["reconcile"]
    _keys(rec_cfg, DEFAULTS["orders"]["reconcile"], "orders.reconcile")
    _number(rec_cfg["poll_interval_s"], "orders.reconcile.poll_interval_s", 0, 10, low_open=True)
    _number(rec_cfg["window_s"], "orders.reconcile.window_s", ORDER_VISIBILITY_S, 600)
    _number(rec_cfg["grace_s"], "orders.reconcile.grace_s", 0, 600)
    _int(rec_cfg["min_successful_polls"], "orders.reconcile.min_successful_polls", 3, 100)
    _int(rec_cfg["cleared_lookback_s"], "orders.reconcile.cleared_lookback_s", 60, 7 * 86400)

    live = orders["live"]
    _keys(live, DEFAULTS["orders"]["live"], "orders.live")
    _bool(live["armed"], "orders.live.armed")
    acks = live["acknowledgements"]
    if (not isinstance(acks, list) or any(not isinstance(a, str) or a not in LIVE_ACK_IDS for a in acks)
            or len(set(acks)) != len(acks)):
        raise ConfigError("orders.live.acknowledgements_must_be_unique_known_ids")
    for key in ("operator_attests_uk_egress", "heartbeat_api_enabled", "kill_cancels_own_orders"):
        _bool(live[key], f"orders.live.{key}")
    if live["kill_cancels_own_orders"] is not True:
        # The key stays so older configs load, but a kill always sweeps our bets (amendment A3, D4).
        raise ConfigError("orders.live.kill_cancels_own_orders_must_be_true")

    for key in ABSOLUTE_CEILINGS:
        _ceiling(c, key)
    return c


def _pilot_section(pilot):
    _keys(pilot, PILOT_KEYS, "orders.pilot")
    if not isinstance(pilot["contract_path"], str) or not PILOT_CONTRACT_PATH_RE.fullmatch(pilot["contract_path"]):
        raise ConfigError("orders.pilot.contract_path_invalid")
    if not isinstance(pilot["contract_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", pilot["contract_sha256"]):
        raise ConfigError("orders.pilot.contract_sha256_invalid")
    _number(pilot["evidence_deadline_s"], "orders.pilot.evidence_deadline_s", 0, PILOT_EVIDENCE_DEADLINE_MAX_S,
            low_open=True)


def pilot_enabled(config):
    """Whether the config has an orders.pilot section (RS-19's registered executor may run on it)."""
    return "pilot" in config["orders"]


def order_strategy_ref(config, source):
    """The customerStrategyRef an order of this source carries: the config's own ref, except that under a
    pilot-enabled config only source "pilot" carries it and every other source the fixed MANUAL_STRATEGY_REF."""
    if pilot_enabled(config) and source != "pilot":
        return MANUAL_STRATEGY_REF
    return config["orders"]["customer_strategy_ref"]


def own_strategy_refs(config):
    """Every customerStrategyRef our own orders can carry under this config (sweeps, startup reconcile)."""
    refs = {config["orders"]["customer_strategy_ref"]}
    if pilot_enabled(config):
        refs.add(MANUAL_STRATEGY_REF)
    return sorted(refs)


def config_sha256(config):
    """canonical_sha256 of a validated config; key order in the file does not matter."""
    return canonical_sha256(config)


def hosts_for(config):
    jurisdiction = config["jurisdiction"]
    return {"sso": SSO_HOSTS[jurisdiction], "sso_cert": SSO_CERT_HOSTS[jurisdiction], "api": config["api_host"],
            "stream_host": config["stream"]["host"], "stream_port": config["stream"]["port"]}


def allowed_http_hosts(config):
    """The HttpTransport allowlist for this config: SSO, certificate SSO and API hosts only."""
    hosts = hosts_for(config)
    return frozenset({hosts["sso"], hosts["sso_cert"], hosts["api"]})
