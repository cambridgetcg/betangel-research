"""Betfair Betting and Accounts JSON-RPC: reads, order preparation and the single order send.

JsonRpcClient.call sends READ_METHODS only, retrying the transient categories and relogging in at
most once per call. prepare_order builds the exact bytes a capability will be bound to; send_order
needs the live app key and an existing session, consumes the capability against the sha256 of the
bytes it is about to send, and hands that digest to the transport, whose gate refuses any non-read
body without it. An order is sent once: no retry, no relogin, no resend. Any refusal inside
send_order burns the capability. A place-orders send needs an absolute send-by time and a send gate,
which go to the transport's first-byte check (amendment A3, C2); no other order method takes them.

ReadClient adds the request-weight planner, the per-market listMarketBook throttle, the in-flight
caps, the catalogue cache and book normalisation. Responses are Betfair data for personal use;
get_account_details returns whitelisted non-personal fields only. Error bodies are reduced to
(surface, code, requestUUID); errorDetails is never kept.
"""

from fractions import Fraction
import math
import re

from .capability import OrderCapability
from .config import hosts_for
from .constants import (ACCOUNT_METHOD_PREFIX, ACCOUNT_RPC_PATH, BET_STATUSES, BETTING_METHOD_PREFIX,
                        BETTING_RPC_PATH, BOOK_COMBINED_WEIGHTS, BOOK_PRICE_DATA_WEIGHTS, CATALOGUE_MAX_RESULTS,
                        CATALOGUE_PROJECTION_WEIGHTS, CUSTOMER_ORDER_REF_PATTERN, CUSTOMER_STRATEGY_REF_PATTERN,
                        GROUP_BY_VALUES, MARKET_SORTS, MAX_IDS_PER_REQUEST, ORDER_BY_VALUES, ORDER_PROJECTIONS,
                        PAGE_SIZE_MAX, PRICE_DATA, READ_METHODS, REQUEST_WEIGHT_LIMIT, REST_MARKET_FILTER_KEYS,
                        SORT_DIRS)
from .errors import ApiError, OrderRefused, TransportError, safe_code
from .filters import MARKET_ID_RE
from .receipts import (SYSTEM_CLOCK, atomic_write_json, bytes_sha256, canonical_json, parse_utc_ns,
                       strict_json_loads, utc_from_ns, utc_ms_from_ns)
from .schemas import validate_book_snapshot
from .secret import assert_clean, refuse_forbidden, scrub

EVENT_TYPES_METHOD = "SportsAPING/v1.0/listEventTypes"
CATALOGUE_METHOD = "SportsAPING/v1.0/listMarketCatalogue"
BOOK_METHOD = "SportsAPING/v1.0/listMarketBook"
CURRENT_ORDERS_METHOD = "SportsAPING/v1.0/listCurrentOrders"
CLEARED_ORDERS_METHOD = "SportsAPING/v1.0/listClearedOrders"
FUNDS_METHOD = "AccountAPING/v1.0/getAccountFunds"
DETAILS_METHOD = "AccountAPING/v1.0/getAccountDetails"

MARKET_PROJECTIONS = frozenset({"COMPETITION", "EVENT", "EVENT_TYPE", "MARKET_START_TIME", "MARKET_DESCRIPTION",
                                "RUNNER_DESCRIPTION", "RUNNER_METADATA"})
SNAPSHOT_PROJECTIONS = ("MARKET_DESCRIPTION", "RUNNER_DESCRIPTION", "EVENT_TYPE", "MARKET_START_TIME")
# The pilot decision's catalogue row (engineering note §4 step 2): EVENT carries event.countryCode; weight 1.
PILOT_ROW_PROJECTIONS = ("EVENT", "EVENT_TYPE", "MARKET_DESCRIPTION", "MARKET_START_TIME")
# The only methods call_raw sends: the pilot's one catalogue row and one decision book (P1-06 §5.1).
RAW_METHODS = (CATALOGUE_METHOD, BOOK_METHOD)
MATCH_PROJECTIONS = frozenset({"NO_ROLLUP", "ROLLED_UP_BY_PRICE", "ROLLED_UP_BY_AVG_PRICE"})
MAX_BEST_PRICES_DEPTH = 10
FUNDS_KEYS = ("availableToBetBalance", "exposure", "retainedCommission", "exposureLimit", "discountRate",
              "pointsBalance")
DETAILS_KEYS = ("currencyCode", "countryCode", "discountRate", "pointsBalance")
SNAPSHOT_FETCHERS = frozenset({"gateway_self_fetch", "read_command"})
ANGX_ERROR_CODE = -32099
TRANSIENT_CATEGORIES = frozenset({"unknown_outcome", "rate_limited"})
MAX_READ_ATTEMPTS = 3
MAX_PAGES = 1000
# Transport failures that a retry cannot change.
_DETERMINISTIC_TRANSPORT_CODES = frozenset({"redirect_refused", "response_too_large", "unsupported_content_encoding",
                                            "tls_verification_failed", "tls_context_unsafe", "invalid_deadline"})
_ORDER_METHOD_RE = re.compile(r"SportsAPING/v1\.0/[A-Za-z]{1,64}")
# The one order verb whose send is bounded and gated at its first byte (amendment A3, C2). Named by its verb,
# as risk._method_verb reads it: only gateway.py spells out the order method names.
_SEND_BOUND_VERB = "place"
_DSC_RE = re.compile(rb"DSC-[0-9]{4}")
_BET_ID_RE = re.compile(r"[0-9]{1,20}")
_SECOND_NS = 1_000_000_000
_MAX_SEARCH_DEPTH = 32
PREPARED_KEYS = frozenset({"method", "body", "body_sha256", "rpc_id"})


# --- Response parsing ------------------------------------------------------------------------


def _find_error_code(value, depth=0):
    """First dict holding an errorCode, searched depth-first (bounded)."""
    if depth > _MAX_SEARCH_DEPTH:
        return None
    if isinstance(value, dict):
        if "errorCode" in value:
            return value
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find_error_code(child, depth + 1)
        if found is not None:
            return found
    return None


def _rpc_error(error, surface):
    """ApiError for a JSON-RPC error member (spec section 7 steps 2-4). errorDetails is never read."""
    if not isinstance(error, dict):
        return ApiError("malformed_error", surface="jsonrpc")
    data = error.get("data")
    exception_name = data.get("exceptionname") if isinstance(data, dict) else None
    exception = None
    if isinstance(exception_name, str) and isinstance(data.get(exception_name), dict) \
            and "errorCode" in data[exception_name]:
        exception = data[exception_name]
    elif data is not None:
        exception = _find_error_code(data)
    if exception is not None and isinstance(exception.get("errorCode"), str):
        exception_surface = {"APINGException": "aping", "AccountAPINGException": "account_aping"}.get(
            exception_name, surface)
        uuid = exception.get("requestUUID")
        return ApiError(exception["errorCode"], surface=exception_surface,
                        request_uuid=uuid if isinstance(uuid, str) and uuid else None)
    code = error.get("code")
    if type(code) is int and code == ANGX_ERROR_CODE:
        # The message is ANGX-00nn; safe_code masks anything that is not plainly a code.
        return ApiError(error.get("message"), surface="angx")
    if type(code) is int:
        return ApiError(str(code), surface="jsonrpc")
    return ApiError("malformed_error", surface="jsonrpc")


def _attribute(envelope, rpc_id):
    """Orders: a JSON-RPC envelope (one carrying result or error) is read only when it echoes exactly
    our integer id and carries exactly one of the two. Otherwise nothing in it can be tied to this
    order, so it is an unknown outcome (local/rpc_id_mismatch or local/ambiguous_rpc_envelope, both
    unrecognized), never a success and never a definite rejection."""
    has_error = envelope.get("error") is not None
    if not has_error and "result" not in envelope:
        return
    response_id = envelope.get("id")
    if type(response_id) is not int or response_id != rpc_id:
        raise ApiError("rpc_id_mismatch", surface="local")
    if has_error and "result" in envelope:
        raise ApiError("ambiguous_rpc_envelope", surface="local")


def _decode_rpc(status, body, surface, *, rpc_id=None):
    """The result, or raises ApiError following spec section 7's precedence. With rpc_id (orders) the
    envelope is attributed to the request first (_attribute)."""
    if surface not in ("aping", "account_aping"):
        raise ValueError("surface must be aping or account_aping")
    raw = bytes(body or b"") if isinstance(body, (bytes, bytearray, memoryview)) else b""
    try:
        envelope = strict_json_loads(raw.decode("utf-8"))
    except ValueError:
        envelope = None   # the parser's message quotes the body; it is dropped, never chained
    if isinstance(envelope, dict) and rpc_id is not None:
        _attribute(envelope, rpc_id)
    if isinstance(envelope, dict) and envelope.get("error") is not None:
        raise _rpc_error(envelope["error"], surface)
    if not isinstance(envelope, dict) or "result" not in envelope:
        match = _DSC_RE.search(raw)
        if match:
            raise ApiError(match.group().decode("ascii"), surface="dsc")
        code = f"HTTP_{status}" if type(status) is int and 100 <= status <= 599 else "HTTP_invalid"
        raise ApiError(code, surface="http")
    return envelope["result"]


def parse_rpc_response(status, body, *, surface):
    """The JSON-RPC result, or ApiError: error.data[exceptionname] first, then any nested errorCode, then
    -32099 -> ANGX (unrecognized), then the generic JSON-RPC code; a body that is not a JSON-RPC
    envelope gives a DSC fault code if one appears, else HTTP_<status>."""
    return _decode_rpc(status, body, surface)


def _result(response, surface, *, rpc_id=None):
    """The result of one transport response; rpc_id (orders only) must be echoed exactly (see _attribute)."""
    if not isinstance(response, dict) or "status" not in response or "body" not in response:
        raise ApiError("malformed_transport_response", surface="local")
    return _decode_rpc(response["status"], response["body"], surface, rpc_id=rpc_id)


# --- Weight planner --------------------------------------------------------------------------


def _exact_weight(weight):
    """Weights are integers times depth/3; a float that is the nearest double to such a third is read
    as that third (so 5 x 10/3 allows 12 ids), any other float exactly as given."""
    if isinstance(weight, bool) or not isinstance(weight, (int, float, Fraction)):
        raise ValueError("weight must be a number")
    if isinstance(weight, float):
        if not math.isfinite(weight):
            raise ValueError("weight must be finite")
        third = Fraction(weight).limit_denominator(3)
        return third if float(third) == weight else Fraction(weight)
    return Fraction(weight)


class WeightPlanner:
    """Market data request weights (spec section 7): at most 200 points per request."""

    def book_weight(self, price_data, depth):
        """listMarketBook weight per market id; EX_ALL_OFFERS overrides EX_BEST_OFFERS, the documented
        combinations replace their two parts, and depth multiplies the total by max(depth, 3)/3."""
        selected = set(price_data)
        if not selected <= set(PRICE_DATA):
            raise ValueError("unknown priceData value")
        if "EX_ALL_OFFERS" in selected:
            selected.discard("EX_BEST_OFFERS")
        if not selected:
            total = Fraction(BOOK_PRICE_DATA_WEIGHTS["none"])
        else:
            total = Fraction(0)
            for combination, weight in BOOK_COMBINED_WEIGHTS.items():
                if combination <= selected:
                    total += weight
                    selected -= combination
            total += sum(BOOK_PRICE_DATA_WEIGHTS[name] for name in selected)
        if depth is not None:
            if type(depth) is not int or not 1 <= depth <= MAX_BEST_PRICES_DEPTH:
                raise ValueError("depth must be an integer from 1 to 10")
            total *= Fraction(max(depth, 3), 3)
        return float(total)

    def catalogue_weight(self, projections):
        selected = set(projections)
        if not selected <= MARKET_PROJECTIONS:
            raise ValueError("unknown marketProjection value")
        return sum(CATALOGUE_PROJECTION_WEIGHTS.get(name, 0) for name in selected)

    def chunk(self, market_ids, weight, *, max_ids=MAX_IDS_PER_REQUEST):
        """Split ids so weight x len(chunk) <= 200; weight 0 is limited only by max_ids (<= 250)."""
        exact = _exact_weight(weight)
        if exact < 0:
            raise ValueError("weight must not be negative")
        if type(max_ids) is not int or not 1 <= max_ids <= MAX_IDS_PER_REQUEST:
            raise ValueError("max_ids must be an integer from 1 to 250")
        per = max_ids if exact == 0 else min(math.floor(REQUEST_WEIGHT_LIMIT / exact), max_ids)
        if per < 1:
            raise ValueError("a single market exceeds the request weight limit")
        ids = list(market_ids)
        return [ids[start:start + per] for start in range(0, len(ids), per)]


# --- JSON-RPC client -------------------------------------------------------------------------


def _inflight_group(method, params):
    if method == CURRENT_ORDERS_METHOD or (method == BOOK_METHOD and (params.get("orderProjection")
                                                                      or params.get("matchProjection"))):
        return "order_projection"
    if method == CLEARED_ORDERS_METHOD:
        return "cleared"
    return None


def _burn(capability):
    """Consume a capability with values that can never match, so a refused send leaves it spent."""
    try:
        capability.consume(method=None, body_sha256=None, now_monotonic_ns=0)
    except OrderRefused:
        pass


class JsonRpcClient:
    """One JSON-RPC endpoint pair for one session; transport defaults to the session's transport."""

    def __init__(self, config, session, *, transport=None, clock=SYSTEM_CLOCK):
        self.config = config
        self.session = session
        self.transport = transport if transport is not None else session.transport
        self.clock = clock
        self._api_host = hosts_for(config)["api"]
        self._next_id = 0
        self._book_calls = {}
        self._inflight = {"order_projection": 0, "cleared": 0}
        self._inflight_limits = {"order_projection": config["read"]["order_projection_inflight"],
                                 "cleared": config["read"]["cleared_inflight"]}
        self._book_limit = config["read"]["max_book_calls_per_second_per_market"]
        self._backoff = list(config["read"]["retry_backoff_s"])

    @property
    def app_key_slot(self):
        """The session's slot, read through so it cannot be relabelled on this object."""
        return self.session.app_key_slot

    def _endpoint(self, method):
        if method.startswith(BETTING_METHOD_PREFIX):
            return f"https://{self._api_host}{BETTING_RPC_PATH}", "aping"
        if method.startswith(ACCOUNT_METHOD_PREFIX):
            return f"https://{self._api_host}{ACCOUNT_RPC_PATH}", "account_aping"
        raise ValueError("unknown JSON-RPC method prefix")

    def _envelope(self, method, params):
        self._next_id += 1
        body = canonical_json({"jsonrpc": "2.0", "method": method, "params": params, "id": self._next_id})
        return self._next_id, body

    def _headers(self, token):
        return {"X-Application": self.session.app_key(), "X-Authentication": token, "Accept": "application/json",
                "Content-Type": "application/json"}

    def _throttle(self, method, params):
        """At most max_book_calls_per_second_per_market listMarketBook calls per market id in any
        sliding second; a call that would exceed it sleeps on the injected clock."""
        if method != BOOK_METHOD:
            return
        market_ids = params.get("marketIds") or []
        while True:
            now = self.clock.monotonic_ns()
            wait_ns = 0
            for market_id in market_ids:
                recent = [t for t in self._book_calls.get(market_id, ()) if now - t < _SECOND_NS]
                self._book_calls[market_id] = recent
                if len(recent) >= self._book_limit:
                    wait_ns = max(wait_ns, recent[-self._book_limit] + _SECOND_NS - now)
            if wait_ns <= 0:
                break
            self.clock.sleep(wait_ns / _SECOND_NS)
        for market_id in market_ids:
            self._book_calls[market_id].append(now)

    def _enter(self, group):
        if group is None:
            return
        if self._inflight[group] >= self._inflight_limits[group]:
            raise ApiError("inflight_limit_reached", surface="local")
        self._inflight[group] += 1

    def _leave(self, group):
        if group is not None:
            self._inflight[group] -= 1

    def call(self, method, params):
        """One read. unknown_outcome, rate_limited and transport failures are retried (3 attempts, read.retry_backoff_s
        between them); a session error gets one relogin and one retry; everything else raises at once."""
        if not isinstance(method, str) or method not in READ_METHODS:
            raise ValueError("call() sends read methods only")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        url, surface = self._endpoint(method)
        group = _inflight_group(method, params)
        self._enter(group)
        try:
            return self._call_with_retry(method, params, url, surface)
        finally:
            self._leave(group)

    def _call_with_retry(self, method, params, url, surface):
        max_attempts = min(MAX_READ_ATTEMPTS, len(self._backoff) + 1)
        attempts = 0
        relogged = False
        while True:
            attempts += 1
            self._throttle(method, params)
            _, body = self._envelope(method, params)
            # Outside the retried block: a failed (lazy) login raises here and is never retried as a read.
            headers = self._headers(self.session.token())
            try:
                response = self.transport.request("POST", url, headers, body,
                                                  deadline_s=self.config["http"]["timeout_s"], idempotent=True)
                return _result(response, surface)
            except ApiError as exc:
                if exc.category == "session" and not relogged:
                    relogged = True
                    attempts -= 1      # the session retry does not use a transient attempt
                    self.session.invalidate_and_relogin()
                    continue
                if exc.category not in TRANSIENT_CATEGORIES or attempts >= max_attempts:
                    raise
            except TransportError as exc:
                if exc.phase == "preflight" or exc.code in _DETERMINISTIC_TRANSPORT_CODES or attempts >= max_attempts:
                    raise
            self.clock.sleep(self._backoff[attempts - 1])

    def call_raw(self, method, params, *, deadline_s):
        """One raw read for the pilot decision (P1-06 §5.1): exactly one transport attempt (idempotent False), no
        retry, no relogin and no decoding. Returns {"method", "params", "status", "body", "error",
        "sent_wall_ns", "sent_monotonic_ns", "received_wall_ns", "received_monotonic_ns"}: body is the
        transport's response bytes unchanged (a non-200 status or an error envelope included), or None with
        error the TransportError's code. Both clocks are read after the throttle and the session token, right
        before the request, and again when it returns (Q10). The live app key only."""
        if method not in RAW_METHODS:
            raise ValueError("call_raw() sends the pilot's catalogue row and decision book only")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        if self.app_key_slot != "live":
            raise OrderRefused("pilot_read_requires_live_app_key")
        url, _ = self._endpoint(method)
        self._throttle(method, params)
        _, body = self._envelope(method, params)
        headers = self._headers(self.session.token())
        out = {"method": method, "params": params, "status": None, "body": None, "error": None,
               "sent_wall_ns": self.clock.time_ns(), "sent_monotonic_ns": self.clock.monotonic_ns()}
        try:
            response = self.transport.request("POST", url, headers, body, deadline_s=deadline_s, idempotent=False)
        except TransportError as exc:
            out["error"] = safe_code(exc.code)
        else:
            status = response.get("status") if isinstance(response, dict) else None
            raw = response.get("body") if isinstance(response, dict) else None
            out["status"] = status if type(status) is int else None
            if isinstance(raw, bytes):
                out["body"] = raw
            else:
                out["error"] = "response_body_not_bytes"
        out["received_wall_ns"] = self.clock.time_ns()
        out["received_monotonic_ns"] = self.clock.monotonic_ns()
        return out

    def prepare_order(self, method, params):
        """{"method", "body", "body_sha256", "rpc_id"} for one order call on the live key; nothing is sent."""
        if self.app_key_slot != "live":
            raise OrderRefused("order_requires_live_app_key")
        if (not isinstance(method, str) or method in READ_METHODS or not method.startswith(BETTING_METHOD_PREFIX)
                or not _ORDER_METHOD_RE.fullmatch(method)):
            raise OrderRefused("order_method_invalid")
        if not isinstance(params, dict):
            raise OrderRefused("order_params_invalid")
        refuse_forbidden(params, where="order_params")
        try:
            rpc_id, body = self._envelope(method, params)
        except (TypeError, ValueError):
            body = None
        if body is None:
            raise OrderRefused("order_params_invalid")
        assert_clean(body, where="order_body")
        return {"method": method, "body": body, "body_sha256": bytes_sha256(body), "rpc_id": rpc_id}

    def _order_parts(self, prepared):
        """(method, body, rpc_id) from a prepared order whose parts all agree, else OrderRefused."""
        if self.app_key_slot != "live":
            raise OrderRefused("order_requires_live_app_key")
        if not isinstance(prepared, dict) or set(prepared) != PREPARED_KEYS:
            raise OrderRefused("prepared_order_invalid")
        method, body, rpc_id = prepared["method"], prepared["body"], prepared["rpc_id"]
        if (not isinstance(method, str) or method in READ_METHODS or not _ORDER_METHOD_RE.fullmatch(method)
                or not isinstance(body, (bytes, bytearray)) or type(rpc_id) is not int):
            raise OrderRefused("prepared_order_invalid")
        body = bytes(body)
        try:
            envelope = strict_json_loads(body.decode("utf-8"))
        except ValueError:
            envelope = None
        if (not isinstance(envelope, dict) or set(envelope) != {"jsonrpc", "method", "params", "id"}
                or envelope["jsonrpc"] != "2.0" or envelope["method"] != method or envelope["id"] != rpc_id
                or type(envelope["id"]) is not int or not isinstance(envelope["params"], dict)
                or prepared["body_sha256"] != bytes_sha256(body)):
            raise OrderRefused("prepared_order_inconsistent")
        return method, body, rpc_id

    def send_order(self, capability, prepared, *, send_by_monotonic_ns=None, send_gate=None):
        """Send one prepared order exactly once. The capability is consumed against the digest of the
        bytes being sent and that digest goes to the transport gate. No retry and no relogin; raises
        ApiError or TransportError (may_have_arrived says whether it could have been processed).

        A place-orders body needs both send_by_monotonic_ns (int) and send_gate (callable): the transport
        gets min(capability expiry, send_by) and the gate, checked right before the first request byte.
        Any other order body takes neither. Both refusals come before the capability is consumed and
        burn it, so nothing is sent."""
        if type(capability) is not OrderCapability:
            raise OrderRefused("capability_invalid")
        try:
            method, body, rpc_id = self._order_parts(prepared)
            bounded = method == BETTING_METHOD_PREFIX + _SEND_BOUND_VERB + "Orders"
            if bounded and (type(send_by_monotonic_ns) is not int or not callable(send_gate)):
                raise OrderRefused("capability_send_bound_required")
            if not bounded and (send_by_monotonic_ns is not None or send_gate is not None):
                raise OrderRefused("send_bound_not_for_this_method")
            token = self.session.current_token()
            if token is None:
                raise OrderRefused("order_requires_logged_in_session")
            url = f"https://{self._api_host}{BETTING_RPC_PATH}"
            headers = self._headers(token)
        except BaseException:
            _burn(capability)
            raise
        expires = capability.expires_monotonic_ns
        digest = capability.consume(method=method, body_sha256=bytes_sha256(body),
                                    now_monotonic_ns=self.clock.monotonic_ns())
        bounds = {}
        if bounded:
            bounds = {"send_by_monotonic_ns": min(expires, send_by_monotonic_ns), "send_gate": send_gate}
        response = self.transport.request("POST", url, headers, body, deadline_s=self.config["http"]["order_deadline_s"],
                                          idempotent=False, order_digest=digest, **bounds)
        return _result(response, "aping", rpc_id=rpc_id)


# --- Read client -----------------------------------------------------------------------------


def _id_list(values, name, pattern, *, limit=MAX_IDS_PER_REQUEST, optional=True):
    if values is None and optional:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    values = list(values)
    if not values or len(values) > limit or len(set(map(str, values))) != len(values):
        raise ValueError(f"{name} must be a non-empty list of at most {limit} unique values")
    for value in values:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"{name} has an invalid value")
    return values


def _choice(value, choices, name):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} is not an allowed value")
    return value


def _rest_filter(rest_filter):
    """A REST MarketFilter: an object with REST key names only (stream names such as countryCodes are refused)."""
    if not isinstance(rest_filter, dict):
        raise ValueError("rest_filter must be an object")
    if not set(rest_filter) <= set(REST_MARKET_FILTER_KEYS):
        raise ValueError("rest_filter has a key that is not a REST MarketFilter key")
    return dict(rest_filter)


def _wire_time(value, name):
    try:
        return utc_ms_from_ns(parse_utc_ns(value))
    except ValueError:
        raise ValueError(f"{name} must be a UTC ISO timestamp with Z or an offset") from None


def _date_range(value, name):
    if not isinstance(value, dict) or not value or not set(value) <= {"from", "to"}:
        raise ValueError(f"{name} must be an object with from and/or to")
    out = {key: _wire_time(value[key], f"{name}.{key}") for key in ("from", "to") if key in value}
    if "from" in out and "to" in out and parse_utc_ns(out["from"]) > parse_utc_ns(out["to"]):
        raise ValueError(f"{name} is reversed")
    return out


def _page_args(from_record, record_count):
    if type(from_record) is not int or from_record < 0:
        raise ValueError("from_record must be a non-negative integer")
    if type(record_count) is not int or not 1 <= record_count <= PAGE_SIZE_MAX:
        raise ValueError("record_count must be an integer from 1 to 1000")


def _list_result(result):
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ApiError("unexpected_result_shape", surface="local")
    return result


def _page_result(result, key):
    """A page report. moreAvailable is a required field: without it the page cannot be known to be the
    last, and these pages feed the kill switch and reconciliation, so its absence fails closed."""
    if (not isinstance(result, dict) or not isinstance(result.get(key), list)
            or any(not isinstance(item, dict) for item in result[key])
            or type(result.get("moreAvailable")) is not bool):
        raise ApiError("unexpected_result_shape", surface="local")
    return result


_MARKET_ID = MARKET_ID_RE
_STRATEGY_REF = re.compile(CUSTOMER_STRATEGY_REF_PATTERN)
_ORDER_REF = re.compile(CUSTOMER_ORDER_REF_PATTERN)


class ReadClient:
    """Read operations over one JsonRpcClient (delayed key for read commands, the live key in the gateway)."""

    def __init__(self, rpc, *, paths, clock=SYSTEM_CLOCK, planner=None, catalogue_cache_max_age_s=21600):
        if type(catalogue_cache_max_age_s) is not int or catalogue_cache_max_age_s < 0:
            raise ValueError("catalogue_cache_max_age_s must be a non-negative integer")
        self.rpc = rpc
        self.paths = paths
        self.clock = clock
        self.planner = planner if planner is not None else WeightPlanner()
        self.catalogue_cache_max_age_s = catalogue_cache_max_age_s

    @property
    def app_key_slot(self):
        return self.rpc.app_key_slot

    @property
    def key_type(self):
        return "live" if self.rpc.app_key_slot == "live" else "delayed"

    def list_event_types(self, rest_filter=None):
        """The filter parameter is required by Betfair, so an absent filter is sent as {}."""
        params = {"filter": _rest_filter(rest_filter) if rest_filter is not None else {}}
        return _list_result(self.rpc.call(EVENT_TYPES_METHOD, params))

    def list_market_catalogue(self, rest_filter, projections=SNAPSHOT_PROJECTIONS, max_results=100, sort=None):
        """maxResults is capped at min(1000, floor(200 / weight)) (1000 for weight 0); RANK is never sent."""
        if rest_filter is None:
            raise ValueError("listMarketCatalogue needs a filter")
        params = {"filter": _rest_filter(rest_filter)}
        projections = sorted(set(projections))
        weight = self.planner.catalogue_weight(projections)
        cap = min(CATALOGUE_MAX_RESULTS, REQUEST_WEIGHT_LIMIT // weight) if weight > 0 else CATALOGUE_MAX_RESULTS
        if type(max_results) is not int or not 1 <= max_results <= cap:
            raise ValueError(f"max_results must be an integer from 1 to {cap} for these projections")
        if sort is not None:
            _choice(sort, MARKET_SORTS, "sort")     # RANK is not a MarketSort member and is refused here
            params["sort"] = sort
        if projections:
            params["marketProjection"] = projections
        params["maxResults"] = max_results
        return _list_result(self.rpc.call(CATALOGUE_METHOD, params))

    def precount_markets(self, rest_filter):
        """(market id, marketStartTime text or None) pairs matching the filter, first to start first (up to
        1000), for the recorder pre-count; the recorder keeps them as its structural snapshot (C4)."""
        rows = self.list_market_catalogue(rest_filter, projections=("MARKET_START_TIME",), max_results=1000,
                                          sort="FIRST_TO_START")
        ids = [row.get("marketId") for row in rows]
        if any(not isinstance(i, str) or not _MARKET_ID.fullmatch(i) for i in ids) or len(set(ids)) != len(ids):
            raise ApiError("unexpected_result_shape", surface="local")
        starts = [row.get("marketStartTime") for row in rows]
        return [(market_id, start if isinstance(start, str) else None) for market_id, start in zip(ids, starts)]

    def count_markets(self, rest_filter):
        """Market ids matching the filter, first to start first (up to 1000), for the recorder pre-count."""
        return [market_id for market_id, _ in self.precount_markets(rest_filter)]

    def list_market_book(self, market_ids, price_data=("EX_BEST_OFFERS",), depth=None, order_projection=None,
                         match_projection=None, customer_strategy_refs=None):
        """MarketBook rows in request order. Chunked by request weight; ids missing from a multi-id response
        (CLOSED markets are left out of mixed requests) are re-requested one per call."""
        ids = _id_list(market_ids, "market_ids", _MARKET_ID, limit=10_000, optional=False)
        selected = set(price_data)
        if not selected <= set(PRICE_DATA):
            raise ValueError("unknown priceData value")
        if "EX_ALL_OFFERS" in selected:
            selected.discard("EX_BEST_OFFERS")
        if depth is not None and "EX_BEST_OFFERS" not in selected:
            raise ValueError("depth applies only to EX_BEST_OFFERS")
        base = {}
        if selected:
            projection = {"priceData": sorted(selected)}
            if depth is not None:
                projection["exBestOffersOverrides"] = {"bestPricesDepth": depth}
            base["priceProjection"] = projection
        if order_projection is not None:
            base["orderProjection"] = _choice(order_projection, ORDER_PROJECTIONS, "order_projection")
        if match_projection is not None:
            base["matchProjection"] = _choice(match_projection, MATCH_PROJECTIONS, "match_projection")
        refs = _id_list(customer_strategy_refs, "customer_strategy_refs", _STRATEGY_REF)
        if refs is not None:
            base["customerStrategyRefs"] = refs
        weight = self.planner.book_weight(selected, depth)
        found = {}
        for chunk in self.planner.chunk(ids, weight):
            for row in self._book_rows(chunk, base):
                found[row["marketId"]] = row
            if len(chunk) > 1:
                for market_id in chunk:
                    if market_id not in found:
                        for row in self._book_rows([market_id], base):
                            found[row["marketId"]] = row
        return [found[market_id] for market_id in ids if market_id in found]

    def _book_rows(self, chunk, base):
        rows = _list_result(self.rpc.call(BOOK_METHOD, dict(base, marketIds=list(chunk))))
        seen = set()
        for row in rows:
            market_id = row.get("marketId")
            if market_id not in chunk or market_id in seen:
                raise ApiError("unrequested_or_duplicate_market_in_response", surface="local")
            seen.add(market_id)
        return rows

    def _cache_path(self, market_id):
        return self.paths.catalogue_cache_dir / f"{market_id}.json"

    def _cached_row(self, market_id):
        """(cached_at_ns, row) from the cache file, or None when absent or unusable (then refetched)."""
        try:
            data = strict_json_loads(self._cache_path(market_id).read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) != {"schema_version", "market_id", "cached_at", "row"}
                    or data["schema_version"] != 1 or data["market_id"] != market_id
                    or not isinstance(data["row"], dict) or data["row"].get("marketId") != market_id):
                return None
            return parse_utc_ns(data["cached_at"]), data["row"]
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    def catalogue_row(self, market_id):
        """The catalogue row for one market: the cache file when fresh, else listMarketCatalogue (cached).

        CLOSED markets leave the catalogue, so when a refresh finds nothing the last cached row is
        kept (it is needed for settlement); with no cached row either, the result is None.
        """
        if not isinstance(market_id, str) or not _MARKET_ID.fullmatch(market_id):
            raise ValueError("market_id must look like 1.<digits>")
        cached = self._cached_row(market_id)
        now_ns = self.clock.time_ns()
        if cached is not None and 0 <= now_ns - cached[0] <= self.catalogue_cache_max_age_s * _SECOND_NS:
            return cached[1]
        rows = self.list_market_catalogue({"marketIds": [market_id]}, projections=SNAPSHOT_PROJECTIONS, max_results=1)
        row = next((r for r in rows if r.get("marketId") == market_id), None)
        if row is None:
            return cached[1] if cached is not None else None
        row = scrub(row)
        self.paths.ensure()
        atomic_write_json(self._cache_path(market_id), {"schema_version": 1, "market_id": market_id,
                                                        "cached_at": utc_from_ns(now_ns), "row": row})
        return row

    def raw_catalogue_row(self, market_id, *, deadline_s):
        """The pilot decision's one uncached catalogue row (note §4 step 2) as call_raw returns it: one market, the
        four PILOT_ROW_PROJECTIONS, maxResults 1. The cache, catalogue_row and its scrub are never used."""
        if not isinstance(market_id, str) or not _MARKET_ID.fullmatch(market_id):
            raise ValueError("market_id must look like 1.<digits>")
        params = {"filter": _rest_filter({"marketIds": [market_id]}), "marketProjection": list(PILOT_ROW_PROJECTIONS),
                  "maxResults": 1}
        return self.rpc.call_raw(CATALOGUE_METHOD, params, deadline_s=deadline_s)

    def raw_decision_book(self, market_id, *, deadline_s):
        """The pilot decision's one book (note §4 step 2) as call_raw returns it: listMarketBook, EX_BEST_OFFERS."""
        if not isinstance(market_id, str) or not _MARKET_ID.fullmatch(market_id):
            raise ValueError("market_id must look like 1.<digits>")
        params = {"marketIds": [market_id], "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}}
        return self.rpc.call_raw(BOOK_METHOD, params, deadline_s=deadline_s)

    def store_catalogue_row(self, market_id, row):
        """Refresh the catalogue cache from a row the caller fetched (P1-06 Q9: the pilot's fresh row, so the send
        book's market_time is read from it), scrubbed and stamped now, as catalogue_row writes it."""
        if not isinstance(market_id, str) or not _MARKET_ID.fullmatch(market_id):
            raise ValueError("market_id must look like 1.<digits>")
        if not isinstance(row, dict) or row.get("marketId") != market_id:
            raise ValueError("catalogue row is for a different market")
        self.paths.ensure()
        atomic_write_json(self._cache_path(market_id), {"schema_version": 1, "market_id": market_id,
                                                        "cached_at": utc_from_ns(self.clock.time_ns()),
                                                        "row": scrub(row)})

    def market_snapshot(self, market_id, *, price_data=("EX_BEST_OFFERS", "EX_TRADED"), fetched_by):
        """Catalogue row joined with a fresh listMarketBook, as a validated book snapshot (spec section 3).

        observed_at and monotonic_ns are taken before the book request, so the snapshot's age is
        never understated.
        """
        if fetched_by not in SNAPSHOT_FETCHERS:
            raise ValueError("fetched_by must be gateway_self_fetch or read_command")
        row = self.catalogue_row(market_id)
        observed_at = self.clock.utc_now()
        monotonic_ns = self.clock.monotonic_ns()
        books = self.list_market_book([market_id], price_data=price_data)
        if len(books) != 1:
            raise ApiError("market_book_missing", surface="local")
        return normalise_market_book(books[0], catalogue_item=row, key_type=self.key_type,
                                     app_key_slot=self.app_key_slot, observed_at=observed_at,
                                     monotonic_ns=monotonic_ns, fetched_by=fetched_by)

    def list_current_orders(self, *, bet_ids=None, market_ids=None, customer_order_refs=None,
                            customer_strategy_refs=None, order_projection="ALL", date_range=None,
                            order_by="BY_PLACE_TIME", sort_dir="EARLIEST_TO_LATEST", include_source_id=False,
                            from_record=0, record_count=1000):
        """One page of CurrentOrderSummaryReport {currentOrders, moreAvailable}. Uses dateRange, never the
        legacy placedDateRange. sourceIdKey is returned only when include_source_id is True, for an
        in-memory comparison; callers strip it before storing anything (writers refuse it)."""
        params = {}
        bets = _id_list(bet_ids, "bet_ids", _BET_ID_RE)
        markets = _id_list(market_ids, "market_ids", _MARKET_ID)
        if len(bets or ()) + len(markets or ()) > MAX_IDS_PER_REQUEST:
            raise ValueError("bet_ids and market_ids together are limited to 250")
        for key, value in (("betIds", bets), ("marketIds", markets),
                           ("customerOrderRefs", _id_list(customer_order_refs, "customer_order_refs", _ORDER_REF)),
                           ("customerStrategyRefs", _id_list(customer_strategy_refs, "customer_strategy_refs",
                                                             _STRATEGY_REF))):
            if value is not None:
                params[key] = value
        params["orderProjection"] = _choice(order_projection, ORDER_PROJECTIONS, "order_projection")
        if date_range is not None:
            params["dateRange"] = _date_range(date_range, "date_range")
        params["orderBy"] = _choice(order_by, ORDER_BY_VALUES, "order_by")
        params["sortDir"] = _choice(sort_dir, SORT_DIRS, "sort_dir")
        if type(include_source_id) is not bool:
            raise ValueError("include_source_id must be boolean")
        if include_source_id:
            params["includeSourceId"] = True
        _page_args(from_record, record_count)
        params["fromRecord"] = from_record
        params["recordCount"] = record_count
        result = _page_result(self.rpc.call(CURRENT_ORDERS_METHOD, params), "currentOrders")
        return result if include_source_id else scrub(result)

    def iter_current_orders(self, **kw):
        """Every order across pages, following moreAvailable (fromRecord += page length)."""
        return self._iterate(self.list_current_orders, "currentOrders", kw)

    def list_cleared_orders(self, bet_status, *, settled_from_utc, settled_to_utc=None, market_ids=None, bet_ids=None,
                            customer_order_refs=None, customer_strategy_refs=None, group_by="BET",
                            include_source_id=False, from_record=0, record_count=1000):
        """One page of ClearedOrderSummaryReport. settled_from_utc is required; groupBy is sent only for
        SETTLED (LAPSED/CANCELLED/VOIDED are bet-level, so any other group_by is refused for them)."""
        _choice(bet_status, BET_STATUSES, "bet_status")
        if settled_from_utc is None:
            raise ValueError("settled_from_utc is required")
        settled = {"from": _wire_time(settled_from_utc, "settled_from_utc")}
        if settled_to_utc is not None:
            settled["to"] = _wire_time(settled_to_utc, "settled_to_utc")
            if parse_utc_ns(settled["from"]) > parse_utc_ns(settled["to"]):
                raise ValueError("settled date range is reversed")
        _choice(group_by, GROUP_BY_VALUES, "group_by")
        if bet_status != "SETTLED" and group_by != "BET":
            raise ValueError("group_by applies only to SETTLED")
        params = {"betStatus": bet_status, "settledDateRange": settled}
        bets = _id_list(bet_ids, "bet_ids", _BET_ID_RE)
        markets = _id_list(market_ids, "market_ids", _MARKET_ID)
        for key, value in (("marketIds", markets), ("betIds", bets),
                           ("customerOrderRefs", _id_list(customer_order_refs, "customer_order_refs", _ORDER_REF)),
                           ("customerStrategyRefs", _id_list(customer_strategy_refs, "customer_strategy_refs",
                                                             _STRATEGY_REF))):
            if value is not None:
                params[key] = value
        if bet_status == "SETTLED":
            params["groupBy"] = group_by
        if type(include_source_id) is not bool:
            raise ValueError("include_source_id must be boolean")
        if include_source_id:
            params["includeSourceId"] = True
        _page_args(from_record, record_count)
        params["fromRecord"] = from_record
        params["recordCount"] = record_count
        result = _page_result(self.rpc.call(CLEARED_ORDERS_METHOD, params), "clearedOrders")
        return result if include_source_id else scrub(result)

    def iter_cleared_orders(self, bet_status, **kw):
        def page(**args):
            return self.list_cleared_orders(bet_status, **args)
        return self._iterate(page, "clearedOrders", kw)

    def _iterate(self, fetch, key, kw):
        kw = dict(kw)
        start = kw.pop("from_record", 0)
        for _ in range(MAX_PAGES):
            page = fetch(from_record=start, **kw)
            items = page[key]
            yield from items
            if not page["moreAvailable"]:
                return
            if not items:
                raise ApiError("pagination_stalled", surface="local")
            start += len(items)
        raise ApiError("pagination_limit_reached", surface="local")

    def get_account_funds(self):
        """{availableToBetBalance, exposure, retainedCommission, exposureLimit, discountRate, pointsBalance};
        an absent field is None. The sign convention of exposure is unverified (T7)."""
        result = self.rpc.call(FUNDS_METHOD, {})
        if not isinstance(result, dict):
            raise ApiError("unexpected_result_shape", surface="local")
        out = {}
        for key in FUNDS_KEYS:
            out[key] = _number_or_none(result.get(key), integer=key == "pointsBalance")
        return out

    def get_account_details(self):
        """Whitelisted {currencyCode, countryCode, discountRate, pointsBalance}; names and other personal
        fields are never returned. An absent field is None."""
        result = self.rpc.call(DETAILS_METHOD, {})
        if not isinstance(result, dict):
            raise ApiError("unexpected_result_shape", surface="local")
        currency = result.get("currencyCode")
        country = result.get("countryCode")
        if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
            raise ApiError("unexpected_result_shape", surface="local")
        if country is not None and (not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country)):
            raise ApiError("unexpected_result_shape", surface="local")
        return {"currencyCode": currency, "countryCode": country,
                "discountRate": _number_or_none(result.get("discountRate")),
                "pointsBalance": _number_or_none(result.get("pointsBalance"), integer=True)}


def _number_or_none(value, *, integer=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
        raise ApiError("unexpected_result_shape", surface="local")
    if integer and (isinstance(value, float) and not value.is_integer()):
        raise ApiError("unexpected_result_shape", surface="local")
    return int(value) if integer else value


# --- Book normalisation ----------------------------------------------------------------------


def _finite(value):
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and _finite(value)


def _levels(levels, *, descending):
    """[[price, size], ...] best first, plus whether anything non-finite was seen (size -> None)."""
    if levels is None:
        return [], False
    if not isinstance(levels, list):
        raise ValueError("ladder levels must be a list")
    out = []
    bad = False
    for level in levels:
        if not isinstance(level, dict):
            raise ValueError("ladder levels must be objects with price and size")
        price, size = level.get("price"), level.get("size")
        if not _is_number(price) or not price > 1:
            bad = True        # an unusable price drops the level and marks the snapshot invalid
            continue
        if not _is_number(size) or size < 0:
            bad = True
            size = None
        out.append([price, size])
    out.sort(key=lambda pair: pair[0], reverse=descending)
    return out, bad


def _traded(volumes):
    if volumes is None:
        return None, False
    if not isinstance(volumes, list):
        raise ValueError("tradedVolume must be a list")
    out = {}
    bad = False
    for level in volumes:
        if not isinstance(level, dict):
            raise ValueError("tradedVolume levels must be objects with price and size")
        price, size = level.get("price"), level.get("size")
        if not _is_number(price) or not price > 1:
            bad = True
            continue
        key = repr(float(price))
        if key in out:
            raise ValueError("duplicate traded price")
        if not _is_number(size) or size < 0:
            bad = True
            size = None
        out[key] = size
    return out, bad


def _text_or_none(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and value else None


def _market_time(value):
    if not isinstance(value, str):
        return None
    try:
        parse_utc_ns(value)
    except ValueError:
        return None
    return value


def normalise_market_book(book_item, *, catalogue_item, key_type, app_key_slot, observed_at, monotonic_ns, fetched_by):
    """REST MarketBook (+ catalogue row) -> book snapshot (spec section 3), validated.

    REST MarketBook carries no betting type, market type, event type, ladder type or start time,
    so those come from the catalogue row; without one they are null and invalid_reason is
    catalogue_missing. Non-finite sizes become null with invalid_reason non_finite_size.
    """
    if not isinstance(book_item, dict):
        raise ValueError("market book must be an object")
    market_id = book_item.get("marketId")
    if catalogue_item is not None and (not isinstance(catalogue_item, dict)
                                       or catalogue_item.get("marketId") != market_id):
        raise ValueError("catalogue row is for a different market")
    for key, check in (("status", lambda v: isinstance(v, str)), ("inplay", lambda v: type(v) is bool),
                       ("betDelay", lambda v: type(v) is int and v >= 0)):
        if not check(book_item.get(key)):
            raise ValueError(f"market book {key} is missing or invalid")
    version = book_item.get("version")
    if version is not None and (type(version) is not int or version < 0):
        raise ValueError("market book version must be a non-negative integer")
    delayed = book_item.get("isMarketDataDelayed")
    if delayed is not None and type(delayed) is not bool:
        raise ValueError("isMarketDataDelayed must be boolean")
    winners = book_item.get("numberOfWinners")
    if winners is not None and (type(winners) is not int or winners < 0):
        raise ValueError("numberOfWinners must be a non-negative integer")
    runners_in = book_item.get("runners")
    if runners_in is None:
        runners_in = []
    if not isinstance(runners_in, list):
        raise ValueError("market book runners must be a list")
    invalid = False
    runners = {}
    for runner in runners_in:
        if not isinstance(runner, dict):
            raise ValueError("runner must be an object")
        selection = runner.get("selectionId")
        if type(selection) is not int or selection <= 0:
            raise ValueError("runner selectionId must be a positive integer")
        if str(selection) in runners:
            raise ValueError("duplicate selection id (handicap markets are not supported)")
        handicap = runner.get("handicap")
        if not _is_number(handicap) or not isinstance(runner.get("status"), str) or not runner["status"]:
            raise ValueError("runner handicap and status are required")
        exchange = runner.get("ex") or {}
        if not isinstance(exchange, dict):
            raise ValueError("runner ex must be an object")
        back, bad_back = _levels(exchange.get("availableToBack"), descending=True)
        lay, bad_lay = _levels(exchange.get("availableToLay"), descending=False)
        traded, bad_traded = _traded(exchange.get("tradedVolume"))
        ltp = runner.get("lastPriceTraded")
        total = runner.get("totalMatched")
        bad_total = total is not None and (not _is_number(total) or total < 0)
        invalid |= bad_back or bad_lay or bad_traded or bad_total
        runners[str(selection)] = {
            "status": runner["status"], "handicap": handicap,
            "ltp": ltp if _is_number(ltp) and ltp > 1 else None,
            "back": back, "lay": lay, "traded": traded,
            "total_matched": None if bad_total else total}
    catalogue = catalogue_item if catalogue_item is not None else {}
    description = catalogue.get("description") if isinstance(catalogue.get("description"), dict) else {}
    event_type = catalogue.get("eventType") if isinstance(catalogue.get("eventType"), dict) else {}
    ladder = description.get("priceLadderDescription")
    if invalid:
        reason = "non_finite_size"
    elif catalogue_item is None:
        reason = "catalogue_missing"
    else:
        reason = None
    snapshot = {
        "market_id": market_id, "source": "rest_listMarketBook", "fetched_by": fetched_by, "key_type": key_type,
        "app_key_slot": app_key_slot, "observed_at": observed_at, "monotonic_ns": monotonic_ns, "pt": None,
        "status": book_item["status"], "inplay": book_item["inplay"], "version": version,
        "bet_delay": book_item["betDelay"], "is_market_data_delayed": delayed,
        "catalogue_present": catalogue_item is not None,
        "market_time": _market_time(description.get("marketTime")),
        "number_of_winners": winners,
        "betting_type": _text_or_none(description.get("bettingType")),
        "market_type": _text_or_none(description.get("marketType")),
        "event_type_id": _text_or_none(event_type.get("id")),
        "price_ladder_type": _text_or_none(ladder.get("type")) if isinstance(ladder, dict) else None,
        "invalid_reason": reason, "runners": runners}
    return validate_book_snapshot(snapshot)
