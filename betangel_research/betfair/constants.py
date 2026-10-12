"""Betfair facts the code depends on, each tied to a fact id in research/betfair-facts.json.

Values were read from official pages on 2026-10-10 and none has been checked against the live API.
This module deliberately names no order-submission method: those literals live in gateway.py only,
and the transport refuses any non-read JSON-RPC body that is not bound to a consumed capability.
Local caps sit below the vendor limits they cite; raising a ceiling needs a reviewed code change.
"""

# --- Hosts and paths -------------------------------------------------------------------------

JURISDICTIONS = ("com", "com.au", "it", "es", "ro")

SSO_HOSTS = {
    "com": "identitysso.betfair.com",
    "com.au": "identitysso.betfair.com.au",
    "it": "identitysso.betfair.it",
    "es": "identitysso.betfair.es",
    "ro": "identitysso.betfair.ro",
}
SSO_CERT_HOSTS = {
    "com": "identitysso-cert.betfair.com",
    "com.au": "identitysso-cert.betfair.com.au",
    "it": "identitysso-cert.betfair.it",
    "es": "identitysso-cert.betfair.es",
    "ro": "identitysso-cert.betfair.ro",
}
DEFAULT_API_HOST = "api.betfair.com"
API_HOSTS = frozenset({"api.betfair.com", "api.betfair.com.au"})
HTTP_HOSTS = frozenset(SSO_HOSTS.values()) | frozenset(SSO_CERT_HOSTS.values()) | API_HOSTS

STREAM_HOSTS = frozenset({"stream-api.betfair.com", "stream-api-integration.betfair.com"})
STREAM_PORT = 443

SSO_PATHS = {
    "certlogin": "/api/certlogin",
    "login": "/api/login",
    "keep_alive": "/api/keepAlive",
    "logout": "/api/logout",
}
BETTING_RPC_PATH = "/exchange/betting/json-rpc/v1"
ACCOUNT_RPC_PATH = "/exchange/account/json-rpc/v1"
# Exact allowlist. Every other Betfair path, including the Heartbeat API (which cancels every
# unmatched LIMIT bet on the account, Bet Angel's included), is unreachable through transport.py.
ALLOWED_PATHS = frozenset(SSO_PATHS.values()) | frozenset({BETTING_RPC_PATH, ACCOUNT_RPC_PATH})
# Which host family may serve each path ("sso_cert" | "sso" | "api").
PATH_HOST_KIND = {
    "/api/certlogin": "sso_cert",
    "/api/login": "sso",
    "/api/keepAlive": "sso",
    "/api/logout": "sso",
    BETTING_RPC_PATH: "api",
    ACCOUNT_RPC_PATH: "api",
}

BETTING_METHOD_PREFIX = "SportsAPING/v1.0/"
ACCOUNT_METHOD_PREFIX = "AccountAPING/v1.0/"
# The only JSON-RPC methods that may travel without a digest-bound capability.
READ_METHODS = frozenset({
    "SportsAPING/v1.0/listEventTypes",
    "SportsAPING/v1.0/listMarketCatalogue",
    "SportsAPING/v1.0/listMarketBook",
    "SportsAPING/v1.0/listCurrentOrders",
    "SportsAPING/v1.0/listClearedOrders",
    "AccountAPING/v1.0/getAccountFunds",
    "AccountAPING/v1.0/getAccountDetails",
})

# --- Session and login -----------------------------------------------------------------------

MAX_SUCCESSFUL_LOGINS_PER_MINUTE = 100
LOGIN_BAN_S = 1200
SESSION_EXPIRY_S = {"com_uk_ie": 86400, "com_other": 43200, "it": 1200, "es": 1200}
# keepAlive interval bounds per jurisdiction: always well inside the documented session length.
KEEPALIVE_BOUNDS_S = {"com": (60, 21599), "com.au": (60, 3600), "it": (60, 600), "es": (60, 600),
                      "ro": (60, 3600)}
IDLE_CLOSE_S = 180

# --- Request limits --------------------------------------------------------------------------

REQUEST_WEIGHT_LIMIT = 200
BOOK_PRICE_DATA_WEIGHTS = {"none": 2, "SP_AVAILABLE": 3, "SP_TRADED": 7, "EX_BEST_OFFERS": 5,
                           "EX_ALL_OFFERS": 17, "EX_TRADED": 17}
# Documented combinations that replace the sum of their two components.
BOOK_COMBINED_WEIGHTS = {frozenset({"EX_BEST_OFFERS", "EX_TRADED"}): 20,
                         frozenset({"EX_ALL_OFFERS", "EX_TRADED"}): 32}
CATALOGUE_PROJECTION_WEIGHTS = {"MARKET_DESCRIPTION": 1, "RUNNER_METADATA": 1}   # every other projection 0
PRICE_DATA = ("SP_AVAILABLE", "SP_TRADED", "EX_BEST_OFFERS", "EX_ALL_OFFERS", "EX_TRADED")
MAX_IDS_PER_REQUEST = 250
BOOK_CALLS_PER_SECOND_PER_MARKET_GUIDANCE = 5
ORDER_PROJECTION_CONCURRENCY_LIMIT = 3
INSTRUCTIONS_PER_SECOND_LIMIT = 1000
PLACE_INSTRUCTIONS_LIMIT = 200
PLACE_INSTRUCTIONS_LIMIT_IT = 50
CANCEL_INSTRUCTIONS_LIMIT = 60
REPLACE_INSTRUCTIONS_LIMIT = 60
PAGE_SIZE_MAX = 1000
CLEARED_DEFAULT_LOOKBACK_DAYS = 90
CATALOGUE_MAX_RESULTS = 1000
MARKET_SORTS = ("MINIMUM_TRADED", "MAXIMUM_TRADED", "MINIMUM_AVAILABLE", "MAXIMUM_AVAILABLE",
                "FIRST_TO_START", "LAST_TO_START")
TRANSACTION_CHARGE_THRESHOLD_PER_HOUR = 5000

# --- References ------------------------------------------------------------------------------

CUSTOMER_REF_MAX_LEN = 32
CUSTOMER_REF_PATTERN = r"^[A-Za-z0-9:\-._+*;~]{1,32}$"
CUSTOMER_REF_DEDUPE_S = 60
CUSTOMER_ORDER_REF_MAX_LEN = 32
CUSTOMER_ORDER_REF_PATTERN = r"^[A-Za-z0-9-]{1,32}$"      # charset is our choice; the docs state none
CUSTOMER_STRATEGY_REF_MAX_LEN = 15
CUSTOMER_STRATEGY_REF_PATTERN = r"^[A-Za-z0-9-]{1,15}$"

# --- Order outcomes and enums ----------------------------------------------------------------

ORDER_TIMEOUT_S = 5
ORDER_VISIBILITY_S = 15
IN_PLAY_DELAY_MAX_S = 12              # the longest in-play bet delay; bounds the kill's last re-sweep (F42)
SIDES = ("BACK", "LAY")
PERSISTENCE_TYPES = ("LAPSE", "PERSIST", "MARKET_ON_CLOSE")
SUPPORTED_PERSISTENCE_TYPE = "LAPSE"
ORDER_STATUSES = ("PENDING", "EXECUTION_COMPLETE", "EXECUTABLE", "EXPIRED")
EXECUTION_REPORT_STATUSES = ("SUCCESS", "FAILURE", "PROCESSED_WITH_ERRORS", "TIMEOUT")
INSTRUCTION_REPORT_STATUSES = ("SUCCESS", "FAILURE", "TIMEOUT")
ORDER_PROJECTIONS = ("ALL", "EXECUTABLE", "EXECUTION_COMPLETE")
ORDER_BY_VALUES = ("BY_MARKET", "BY_MATCH_TIME", "BY_PLACE_TIME", "BY_SETTLED_TIME", "BY_VOID_TIME")
SORT_DIRS = ("EARLIEST_TO_LATEST", "LATEST_TO_EARLIEST")
BET_STATUSES = ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
# The GroupBy enum (Betting Enums). EXCHANGE appears only in the commission field's description (F37),
# not in the enum, so it is never sent.
GROUP_BY_VALUES = ("EVENT_TYPE", "EVENT", "MARKET", "SIDE", "BET")

# --- Money, prices and markets ---------------------------------------------------------------

ACCOUNT_CURRENCY = "GBP"
MIN_BET_PENCE_GBP = 100
PROFIT_RATIO_BOUNDS_PERCENT = (80, 125)
SIZE_DECIMAL_PLACES = 2
# CLASSIC ladder in integer hundredths. Each band is (from_inclusive, to_exclusive, step); the final
# upper bound (100000 = 1000.00) is itself on the ladder. range(lo, hi, step) per band plus the
# final bound gives exactly CLASSIC_LADDER_TICKS prices from 101 to 100000.
CLASSIC_BANDS = (
    (101, 200, 1),
    (200, 300, 2),
    (300, 400, 5),
    (400, 600, 10),
    (600, 1000, 20),
    (1000, 2000, 50),
    (2000, 3000, 100),
    (3000, 5000, 200),
    (5000, 10000, 500),
    (10000, 100000, 1000),
)
CLASSIC_LADDER_TICKS = 350
CLASSIC_MIN_PRICE_H = 101
CLASSIC_MAX_PRICE_H = 100000
PRICE_LADDER_TYPES = ("CLASSIC", "FINEST", "LINE_RANGE")
MARKET_STATUSES = ("INACTIVE", "OPEN", "SUSPENDED", "CLOSED")
RUNNER_STATUSES = ("ACTIVE", "WINNER", "LOSER", "PLACED", "REMOVED_VACANT", "REMOVED", "HIDDEN")

# --- App keys and the stream -----------------------------------------------------------------

APP_KEY_SLOTS = ("delayed", "live")
DELAYED_KEY_MAX_DELAY_S = 180
DELAYED_KEY_LADDER_LEVELS = 3
DELAYED_KEY_STREAM_CONNECTIONS = 2
DELAYED_KEY_CONFLATE_MS = 180000
STREAM_FIRST_MESSAGE_TIMEOUT_S = 15
STREAM_HEARTBEAT_MS_RANGE = (500, 5000)
STREAM_CONFLATE_MS_RANGE = (0, 120000)
STREAM_LADDER_LEVELS_RANGE = (1, 10)
STREAM_DEFAULT_SUBSCRIPTION_LIMIT = 200
STREAM_STATUS_ERROR_CODES = ("NO_APP_KEY", "INVALID_APP_KEY", "NO_SESSION", "INVALID_SESSION_INFORMATION",
                             "NOT_AUTHORIZED", "INVALID_INPUT", "INVALID_REQUEST", "INVALID_CLOCK",
                             "UNEXPECTED_ERROR", "TIMEOUT", "SUBSCRIPTION_LIMIT_EXCEEDED",
                             "MAX_CONNECTION_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS", "CONNECTION_FAILED")

# --- Filters ---------------------------------------------------------------------------------

REST_MARKET_FILTER_KEYS = ("eventTypeIds", "marketCountries", "marketTypeCodes", "marketIds", "marketStartTime")
STREAM_MARKET_FILTER_KEYS = ("eventTypeIds", "countryCodes", "marketTypes", "marketIds")

# --- Account, policy and acknowledgements ----------------------------------------------------

COMMISSION_SOURCES = ("user_package_verified", "user_package_unverified", "official_example_basic_2pct",
                      "community_secondary_rewards_5pct_conservative_unverified",
                      "community_secondary_rewards_plus_8pct_unverified")
COEXISTENCE_STATES = ("unverified", "not_running_on_account", "token_survival_verified")
# Live arming needs every id; loading a config needs only that each listed id is one of these.
LIVE_ACK_IDS = ("delayed_key_places_real_bets", "no_sandbox", "account_shared_with_bet_angel",
                "heartbeat_disabled_by_default", "transaction_charges_apply", "personal_use_only",
                "unknown_outcomes_block_until_reconciled", "kill_cannot_undo_matched_bets")

# --- Absolute ceilings (local design, each below the vendor limit it cites) ------------------
# Keys are the §4 table names: order caps and rules bare, the rest dotted. CEILING_PATHS maps each
# key to its location in the config dict.
ABSOLUTE_CEILINGS = {
    "max_stake_per_order": 50,
    "max_liability_per_order": 100,
    "max_market_exposure": 200,
    "max_total_exposure": 500,
    "max_daily_loss": 200,
    "max_orders_per_minute": 30,
    "max_orders_per_hour": 600,
    "max_orders_per_day": 2000,
    "max_transactions_per_hour": 1000,
    "max_instructions_per_request": 10,
    "max_price_deviation_ticks": 50,
    "max_book_age_s": 5,
    "funds_max_age_s": 300,
    "max_minutes_before_off": 120,
    "external_exposure_tolerance": 200,
    "http.order_deadline_s": 12,
    "session.max_successful_logins_per_10min": 60,
    "session.max_consecutive_failed_logins": 3,
    "recorder.max_markets": 180,
}
CEILING_PATHS = {
    "max_stake_per_order": ("orders", "caps", "max_stake_per_order"),
    "max_liability_per_order": ("orders", "caps", "max_liability_per_order"),
    "max_market_exposure": ("orders", "caps", "max_market_exposure"),
    "max_total_exposure": ("orders", "caps", "max_total_exposure"),
    "max_daily_loss": ("orders", "caps", "max_daily_loss"),
    "max_orders_per_minute": ("orders", "caps", "max_orders_per_minute"),
    "max_orders_per_hour": ("orders", "caps", "max_orders_per_hour"),
    "max_orders_per_day": ("orders", "caps", "max_orders_per_day"),
    "max_transactions_per_hour": ("orders", "caps", "max_transactions_per_hour"),
    "max_instructions_per_request": ("orders", "caps", "max_instructions_per_request"),
    "max_price_deviation_ticks": ("orders", "caps", "max_price_deviation_ticks"),
    "max_book_age_s": ("orders", "caps", "max_book_age_s"),
    "funds_max_age_s": ("orders", "caps", "funds_max_age_s"),
    "max_minutes_before_off": ("orders", "caps", "max_minutes_before_off"),
    "external_exposure_tolerance": ("orders", "rules", "external_exposure_tolerance"),
    "http.order_deadline_s": ("http", "order_deadline_s"),
    "session.max_successful_logins_per_10min": ("session", "max_successful_logins_per_10min"),
    "session.max_consecutive_failed_logins": ("session", "max_consecutive_failed_logins"),
    "recorder.max_markets": ("recorder", "max_markets"),
}

# --- Provenance: every public constant above -> fact ids in research/betfair-facts.json -------

PROVENANCE = {
    "JURISDICTIONS": ("F01", "F06", "F12"),
    "SSO_HOSTS": ("F06", "F07"),
    "SSO_CERT_HOSTS": ("F01",),
    "DEFAULT_API_HOST": ("F11", "F12"),
    "API_HOSTS": ("F11", "F12"),
    "HTTP_HOSTS": ("F01", "F06", "F07", "F11", "F12"),
    "STREAM_HOSTS": ("F48",),
    "STREAM_PORT": ("F48",),
    "SSO_PATHS": ("F01", "F06", "F07"),
    "BETTING_RPC_PATH": ("F11",),
    "ACCOUNT_RPC_PATH": ("F11",),
    "ALLOWED_PATHS": ("F01", "F06", "F07", "F11", "F58"),
    "PATH_HOST_KIND": ("F01", "F06", "F07", "F11"),
    "BETTING_METHOD_PREFIX": ("F11",),
    "ACCOUNT_METHOD_PREFIX": ("F11",),
    "READ_METHODS": ("F11", "F17", "F34", "F35", "F65", "F66", "F70", "F72"),
    "MAX_SUCCESSFUL_LOGINS_PER_MINUTE": ("F10",),
    "LOGIN_BAN_S": ("F10",),
    "SESSION_EXPIRY_S": ("F09",),
    "KEEPALIVE_BOUNDS_S": ("F09",),
    "IDLE_CLOSE_S": ("F13",),
    "REQUEST_WEIGHT_LIMIT": ("F16",),
    "BOOK_PRICE_DATA_WEIGHTS": ("F16",),
    "BOOK_COMBINED_WEIGHTS": ("F16",),
    "CATALOGUE_PROJECTION_WEIGHTS": ("F16",),
    "PRICE_DATA": ("F16",),
    "MAX_IDS_PER_REQUEST": ("F17", "F34"),
    "BOOK_CALLS_PER_SECOND_PER_MARKET_GUIDANCE": ("F17",),
    "ORDER_PROJECTION_CONCURRENCY_LIMIT": ("F18",),
    "INSTRUCTIONS_PER_SECOND_LIMIT": ("F19",),
    "PLACE_INSTRUCTIONS_LIMIT": ("F20",),
    "PLACE_INSTRUCTIONS_LIMIT_IT": ("F20",),
    "CANCEL_INSTRUCTIONS_LIMIT": ("F20",),
    "REPLACE_INSTRUCTIONS_LIMIT": ("F20",),
    "PAGE_SIZE_MAX": ("F34", "F35"),
    "CLEARED_DEFAULT_LOOKBACK_DAYS": ("F35",),
    "CATALOGUE_MAX_RESULTS": ("F70",),
    "MARKET_SORTS": ("F70",),
    "TRANSACTION_CHARGE_THRESHOLD_PER_HOUR": ("F61",),
    "CUSTOMER_REF_MAX_LEN": ("F21",),
    "CUSTOMER_REF_PATTERN": ("F21",),
    "CUSTOMER_REF_DEDUPE_S": ("F21",),
    "CUSTOMER_ORDER_REF_MAX_LEN": ("F23",),
    "CUSTOMER_ORDER_REF_PATTERN": ("F23",),
    "CUSTOMER_STRATEGY_REF_MAX_LEN": ("F24",),
    "CUSTOMER_STRATEGY_REF_PATTERN": ("F24",),
    "ORDER_TIMEOUT_S": ("F28",),
    "ORDER_VISIBILITY_S": ("F28",),
    "IN_PLAY_DELAY_MAX_S": ("F42",),
    "SIDES": ("F29",),
    "PERSISTENCE_TYPES": ("F29",),
    "SUPPORTED_PERSISTENCE_TYPE": ("F29", "F42"),
    "ORDER_STATUSES": ("F29",),
    "EXECUTION_REPORT_STATUSES": ("F29",),
    "INSTRUCTION_REPORT_STATUSES": ("F29",),
    "ORDER_PROJECTIONS": ("F34",),
    "ORDER_BY_VALUES": ("F34",),
    "SORT_DIRS": ("F34",),
    "BET_STATUSES": ("F35",),
    "GROUP_BY_VALUES": ("F35", "F37"),
    "ACCOUNT_CURRENCY": ("F38", "F66"),
    "MIN_BET_PENCE_GBP": ("F38",),
    "PROFIT_RATIO_BOUNDS_PERCENT": ("F39",),
    "SIZE_DECIMAL_PLACES": ("F40",),
    "CLASSIC_BANDS": ("F41",),
    "CLASSIC_LADDER_TICKS": ("F41",),
    "CLASSIC_MIN_PRICE_H": ("F41",),
    "CLASSIC_MAX_PRICE_H": ("F41",),
    "PRICE_LADDER_TYPES": ("F41",),
    "MARKET_STATUSES": ("F43",),
    "RUNNER_STATUSES": ("F43",),
    "APP_KEY_SLOTS": ("F44", "F46"),
    "DELAYED_KEY_MAX_DELAY_S": ("F44",),
    "DELAYED_KEY_LADDER_LEVELS": ("F44",),
    "DELAYED_KEY_STREAM_CONNECTIONS": ("F44",),
    "DELAYED_KEY_CONFLATE_MS": ("F44", "F51"),
    "STREAM_FIRST_MESSAGE_TIMEOUT_S": ("F48",),
    "STREAM_HEARTBEAT_MS_RANGE": ("F50",),
    "STREAM_CONFLATE_MS_RANGE": ("F51",),
    "STREAM_LADDER_LEVELS_RANGE": ("F48", "F54"),
    "STREAM_DEFAULT_SUBSCRIPTION_LIMIT": ("F44", "F55"),
    "STREAM_STATUS_ERROR_CODES": ("F49",),
    "REST_MARKET_FILTER_KEYS": ("F75",),
    "STREAM_MARKET_FILTER_KEYS": ("F75",),
    "COMMISSION_SOURCES": ("F62",),
    "COEXISTENCE_STATES": ("F67",),
    "LIVE_ACK_IDS": ("F28", "F44", "F58", "F61", "F63", "F67"),
    "ABSOLUTE_CEILINGS": ("F10", "F19", "F20", "F28", "F55", "F61"),
    "CEILING_PATHS": ("F10", "F19", "F20", "F28", "F55", "F61"),
}
