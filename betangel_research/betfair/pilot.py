"""Pure Pilot 1 logic for RS-19 contract p1: decision, records, measurement, ledger, pause and analysis.

docs/betfair-pilot-policy.md (RS-19) is the policy, docs/betfair-pilot-engineering.md (the note) holds the detail,
and research/pilots/p1-fav-t5.json (contract p1) holds the frozen values; this module reads the values from the
parsed contract and cites the note by section. Nothing here reads a clock, a file, the network, the environment or
a random source: every time comes in as an argument (observed_wall_ns, now_ns, each event's recorded_at), every piece
of Betfair evidence comes in as the raw response bytes, and the journal comes in as the event dicts the caller read.
Callers (the gateway in P1-06, pilot-run and pilot-capture in P1-08 and P1-10) do the I/O and journal what this
module returns. Betfair numbers are parsed from the raw JSON text with Decimal and compared or summed exactly in
Fraction; floats appear only inside the Student t routine, whose result is turned into a Fraction before the CR1
decision is made exactly.

Pilot records are journal note events whose payload is {"pilot": {...}} (note §6). validate_pilot_payload pins
each type's exact keys. fold() rebuilds the whole pilot state from the journal in seq order, so a restart gives the
same answer: reservations and decisions (§4), slots and day counts (§6), stops and ACKs (§6, §10), the final
execution record, BSP and measurement class of every PLACE record (§11), the ledger with the pause (§10),
protocol violations and endings (§15). The analysis (sample, gates, CR1 and verdict) runs on a folded state.

What this cannot show: that the caller fetched fresh live-key evidence, journaled what it was given, or sent
nothing else. A fold sees only what was journaled.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from fractions import Fraction
import hashlib
import json
import math
import re

from .constants import CLASSIC_MAX_PRICE_H, CLASSIC_MIN_PRICE_H
from .ticks import is_on_ladder, tick_index

NS = 1_000_000_000
DAY_NS = 86_400 * NS
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# [0-9], never \d: \d also matches non-ASCII digits. Hour 24 is refused so every Python version agrees (3.14's
# fromisoformat reads 24:00 as the next midnight; 3.10-3.13 refuse it).
_TIMESTAMP_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,6}))?"
                           r"(Z|[+-][0-9]{2}:[0-9]{2})")
SHA_RE = re.compile(r"[0-9a-f]{64}")
ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
REF_RE = re.compile(r"[A-Za-z0-9-]{1,32}")
STRATEGY_REF_RE = re.compile(r"[A-Za-z0-9-]{1,15}")
MARKET_ID_RE = re.compile(r"1\.[0-9]{1,12}")
BET_ID_RE = re.compile(r"[0-9]{1,20}")
DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
STOP_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
CODE_RE = re.compile(r"[a-z0-9_:-]{1,80}")
DECIMAL_TEXT_RE = re.compile(r"-?[0-9]{1,40}(\.[0-9]{1,40})?")
RATE_RE = re.compile(r"[0-9]+\.[0-9]+")
COUNTRY_RE = re.compile(r"[A-Z]{2}")
_PAUSE_ID_RE = re.compile(r"pause-([1-9][0-9]{0,9})")
# One bound for evidence numbers, the same as DECIMAL_TEXT_RE's: at most 40 integer digits (adjusted() <= 39) and
# 40 decimal places, so every number an extractor returns can be journaled. The exponent text is bounded before
# Decimal sees it; JSON integers are bounded by their digit count.
_MAX_ADJUSTED, _MIN_EXPONENT, _MAX_DIGITS = 39, -40, 40
_NUMBER_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE]([+-]?[0-9]+))?")
_STRUCTURE_RE = re.compile(r'[\[\]{}"\\]')
MAX_JSON_DEPTH = 64
MAX_NS = 2 ** 62                      # every ns field and argument: 0 <= ns < 2**62 (to the year 2116)
# A matched price must lie within the CLASSIC bounds, 1.01 to 1000 inclusive; a decimal average such as 3.444 is
# kept exactly (no ladder membership, no rounding). Anything else leaves the price unresolved (Astra #5).
_MIN_PRICE, _MAX_PRICE = Fraction(CLASSIC_MIN_PRICE_H, 100), Fraction(CLASSIC_MAX_PRICE_H, 100)

CLASS_ORDER = ("protocol_violation", "definitely_unsent", "void", "post_decision_removal", "measurement_unresolved",
               "unmatched", "missing_bsp", "eligible")
# The SKIP codes in evaluation order: the first failure wins. Only window, pause, cap and interrupted are named by
# the note (§4, §6, §10); the others name one P1 predicate each (Nuance's decision Q3).
SKIP_CODES = ("evidence_invalid", "book_delayed", "off_missing", "window", "country_not_allowed",
              "event_type_not_allowed", "market_type_not_allowed", "betting_type_not_odds", "number_of_winners_not_1",
              "ladder_not_classic", "too_few_active_runners", "no_priced_active_runner", "favourite_tie",
              "best_back_off_ladder", "best_back_out_of_range", "depth_below_min", "best_lay_missing", "book_crossed",
              "best_lay_off_ladder", "spread_too_wide", "pause", "cap", "interrupted")
R13_CODES = ("r13_not_place", "r13_other_open", "r13_other_session", "r13_other_contract", "r13_digest_mismatch",
             "r13_market_differs", "r13_body_differs", "r13_ref_differs", "r13_strategy_ref_differs")
REMOVED = ("REMOVED", "REMOVED_VACANT")
LEDGER_GAP_REASONS = ("cleared_figure_gap", "commission_gap", "commission_mismatch", "ledger_contradiction")
RESULTS = ("WINNER", "LOSER")
# The 27 top-level keys of contract p1 (critique 4).
CONTRACT_KEYS = frozenset({
    "bsp", "caps", "commission_rate", "customer_strategy_ref", "endings", "engineering_note", "final_execution",
    "frozen_on", "id", "inference", "intent_source", "ledger", "measurement_classes", "metric",
    "money_set_by_yu_on", "pause", "placement_limits", "plan", "policy", "reporting", "rule", "sample", "schema",
    "status", "units", "validity_gates", "verdict"})
CURRENT_ROW_KEYS = ("status", "sizeMatched", "sizeCancelled", "sizeLapsed", "sizeVoided", "sizeRemaining",
                    "averagePriceMatched", "selectionId", "marketId", "customerOrderRef", "customerStrategyRef")
CURRENT_ROW_NUMBERS = ("sizeMatched", "sizeCancelled", "sizeLapsed", "sizeVoided", "sizeRemaining",
                       "averagePriceMatched")
CLEARED_ROW_KEYS = ("betStatus", "sizeSettled", "priceMatched", "priceReduced", "profit", "betOutcome",
                    "selectionId", "marketId", "customerOrderRef", "settledDate")
CLEARED_ROW_NUMBERS = ("sizeSettled", "priceMatched", "profit")
_NONFINITE_TEXT = ("NaN", "Infinity", "-Infinity")


class PilotError(ValueError):
    """A refusal with a code; the message is the code alone and never quotes raw text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class EvidenceError(PilotError):
    """Raw Betfair evidence that cannot be read as the rule needs it."""


# ---------------------------------------------------------------------------------------------------------------
# Pure helpers, mirrored from receipts.py (which this module may not import: it imports subprocess).

def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def canonical_sha256(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def bytes_sha256(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def parse_utc_ns(text):
    """Nanoseconds since the epoch; the same contract as receipts.parse_utc_ns (Z or an offset, 0-6 fraction
    digits, naive text refused)."""
    if not isinstance(text, str):
        raise PilotError("timestamp_invalid")
    match = _TIMESTAMP_RE.fullmatch(text)
    if not match:
        raise PilotError("timestamp_invalid")
    base, fraction, offset = match.groups()
    if offset == "Z":
        offset = "+00:00"
    try:
        parsed = datetime.fromisoformat(base + offset)
    except ValueError:
        raise PilotError("timestamp_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PilotError("timestamp_invalid")
    delta = parsed - _EPOCH
    seconds = delta.days * 86400 + delta.seconds
    fraction_ns = int(fraction.ljust(9, "0")) if fraction else 0
    return seconds * NS + fraction_ns


def utc_text_from_ns(ns):
    """'YYYY-MM-DDTHH:MM:SS.ffffffZ', the shape receipts.utc_from_ns gives."""
    if type(ns) is not int:
        raise PilotError("timestamp_invalid")
    value = _EPOCH + timedelta(microseconds=ns // 1000)
    return value.strftime("%Y-%m-%dT%H:%M:%S") + ".%06dZ" % value.microsecond


def utc_day(ns):
    """'YYYY-MM-DD' of the UTC day holding ns."""
    if type(ns) is not int:
        raise PilotError("timestamp_invalid")
    return (_EPOCH + timedelta(days=ns // DAY_NS)).strftime("%Y-%m-%d")


def _day_start_ns(day):
    if not isinstance(day, str) or not DAY_RE.fullmatch(day):
        raise PilotError("day_invalid")
    return parse_utc_ns(day + "T00:00:00Z")


@dataclass(frozen=True)
class NonFinite:
    """NaN, Infinity or -Infinity met in raw JSON; only BSP fields may carry one, as no BSP (critique 20)."""
    text: str


def _json_number(text):
    match = _NUMBER_RE.fullmatch(text)
    if not match or len(match.group(1) or "") > 6:
        raise ValueError("number out of range")
    try:
        value = Decimal(text)
    except DecimalException:
        raise ValueError("number out of range") from None
    if value.adjusted() > _MAX_ADJUSTED or value.as_tuple().exponent < _MIN_EXPONENT:
        raise ValueError("number out of range")
    return value


def _json_int(text):
    if len(text.lstrip("-")) > _MAX_DIGITS:
        raise ValueError("number out of range")
    return int(text)


def _nesting_within(text, limit):
    """Whether no array or object in the JSON text nests deeper than limit (strings and escapes skipped)."""
    depth, in_string, skip = 0, False, -1
    for match in _STRUCTURE_RE.finditer(text):
        at, char = match.start(), match.group()
        if at == skip:
            continue
        if in_string:
            if char == "\\":
                skip = at + 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return False
        elif char in "]}":
            depth -= 1
    return True


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def decimal_loads(text, *, allow_nonfinite=False):
    """json.loads with every non-integer number as an exact Decimal; duplicate keys, numbers beyond the evidence
    bound and nesting deeper than MAX_JSON_DEPTH refused; NaN and Infinity refused, or kept as NonFinite markers
    when allow_nonfinite. Every refusal is PilotError("json_invalid")."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError:
            raise PilotError("json_invalid") from None
    if not isinstance(text, str) or not _nesting_within(text, MAX_JSON_DEPTH):
        raise PilotError("json_invalid")

    def constant(name):
        if allow_nonfinite:
            return NonFinite(name)
        raise ValueError("non-finite constant")

    try:
        return json.loads(text, parse_float=_json_number, parse_int=_json_int, parse_constant=constant,
                          object_pairs_hook=_unique_pairs)
    except RecursionError:
        pass
    except (ValueError, DecimalException):
        raise PilotError("json_invalid") from None
    raise PilotError("json_invalid")


def money_text(value):
    """A finite Decimal as plain decimal text (no exponent): Decimal(money_text(d)) == d exactly."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PilotError("decimal_invalid")
    return format(value, "f")


def _decimal_of(value):
    """A JSON number (int, not bool, or Decimal) as a Decimal; anything else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal) and value.is_finite():
        return value
    return None


def _decimal_text_value(text):
    """Decimal from note text written by money_text; None for None or anything else."""
    if isinstance(text, str) and DECIMAL_TEXT_RE.fullmatch(text):
        return Decimal(text)
    return None


def _clean_text(value, limit=80):
    """value when it is printable text UTF-8 can encode (no lone surrogate), 1 to limit characters; else None."""
    if not isinstance(value, str) or not 0 < len(value) <= limit or not value.isprintable():
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _is_ns(value):
    return type(value) is int and 0 <= value < MAX_NS


def _ceil(value):
    return -((-value.numerator) // value.denominator)


def _floor(value):
    return value.numerator // value.denominator


# ---------------------------------------------------------------------------------------------------------------
# Contract p1 (no file I/O: the caller reads the bytes).

@dataclass(frozen=True)
class Contract:
    sha256: str
    id: str
    customer_strategy_ref: str
    intent_source: str
    commission_rate: Fraction
    country_codes: frozenset
    event_type_id: str
    market_type: str
    betting_type: str
    number_of_winners: int
    price_ladder: str
    min_active_runners: int
    window_from_offset_s: int
    window_length_s: int
    send_deadline_offset_s: int
    side: str
    order_type: str
    persistence_type: str
    size_p: int
    best_back_min_h: int
    best_back_max_h: int
    min_available_p: int
    max_lay_ticks: int
    lifetime_place_records: int
    per_utc_day: int
    per_pt4_session_day: int
    pause_step_p: int
    new_place_counted_at_p: int
    gap_tolerance_p: int
    deadline_s: int
    bsp_cutoff_s: int
    n_eligible: int
    alpha_one_sided: Fraction
    min_clusters: int
    fill_rate_min: Fraction
    missing_bsp_max: Fraction
    unresolved_max: Fraction
    final_date: str
    class_order: tuple

    @property
    def final_date_ns(self):
        return _day_start_ns(self.final_date)


def contract_sha256(raw):
    """The contract's identity: sha256 of the frozen file's raw bytes (note §14)."""
    if not isinstance(raw, bytes):
        raise PilotError("contract_invalid:raw")
    return hashlib.sha256(raw).hexdigest()


def _contract_value(parsed, path):
    value = parsed
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise PilotError(f"contract_invalid:{path}")
        value = value[part]
    return value


def _contract_int(parsed, path, *, minimum=None, maximum=None):
    value = _contract_value(parsed, path)
    if type(value) is not int or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise PilotError(f"contract_invalid:{path}")
    return value


def _contract_text(parsed, path, *, expected=None, pattern=None):
    value = _contract_value(parsed, path)
    if (not isinstance(value, str) or not value or (expected is not None and value != expected)
            or (pattern is not None and not pattern.fullmatch(value))):
        raise PilotError(f"contract_invalid:{path}")
    return value


def _contract_rate(parsed, path):
    text = _contract_text(parsed, path, pattern=RATE_RE)
    rate = Fraction(Decimal(text))
    if not 0 <= rate <= 1:
        raise PilotError(f"contract_invalid:{path}")
    return rate


def _contract_day(parsed, path):
    text = _contract_text(parsed, path, pattern=DAY_RE)
    try:
        _day_start_ns(text)
    except PilotError:
        raise PilotError(f"contract_invalid:{path}") from None
    return text


def _non_integer_number(value, path=""):
    """The path of the first float or Decimal anywhere in value, or None."""
    if isinstance(value, (float, Decimal)):
        return path or "root"
    if isinstance(value, dict):
        for key in sorted(value):
            found = _non_integer_number(value[key], f"{path}.{key}" if path else key)
            if found:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _non_integer_number(item, f"{path}[{index}]")
            if found:
                return found
    return None


def load_contract(parsed, *, raw_sha256):
    """Contract from the parsed frozen JSON. Only the consumed values are checked here; the raw-bytes sha256 pins
    the frozen text (formulas, tables) as a whole. Raises PilotError("contract_invalid:<path>")."""
    if not isinstance(raw_sha256, str) or not SHA_RE.fullmatch(raw_sha256):
        raise PilotError("contract_invalid:raw_sha256")
    if not isinstance(parsed, dict) or set(parsed) != CONTRACT_KEYS:
        raise PilotError("contract_invalid:keys")
    found = _non_integer_number(parsed)
    if found:
        raise PilotError(f"contract_invalid:{found}")
    _contract_text(parsed, "schema", expected="betangel.pilot-contract/v1")
    _contract_text(parsed, "status", expected="frozen")
    if parsed["frozen_on"] is None:
        raise PilotError("contract_invalid:frozen_on")
    _contract_day(parsed, "frozen_on")
    commission = _contract_rate(parsed, "commission_rate")
    if commission >= 1 or _contract_rate(parsed, "metric.one_minus_c") != 1 - commission:
        raise PilotError("contract_invalid:metric.one_minus_c")
    countries = _contract_value(parsed, "rule.market.country_codes")
    if (not isinstance(countries, list) or not countries or len(set(map(str, countries))) != len(countries)
            or not all(isinstance(c, str) and re.fullmatch(r"[A-Z]{2}", c) for c in countries)):
        raise PilotError("contract_invalid:rule.market.country_codes")
    order = _contract_value(parsed, "measurement_classes.order")
    if order != list(CLASS_ORDER):
        raise PilotError("contract_invalid:measurement_classes.order")
    _contract_int(parsed, "rule.order.instructions_per_request", minimum=1, maximum=1)
    _contract_int(parsed, "rule.decisions_per_market_for_the_contract_life", minimum=1, maximum=1)
    _contract_text(parsed, "rule.selection.tie", expected="SKIP")
    size_p = _contract_int(parsed, "rule.order.size_p", minimum=1)
    window_from = _contract_int(parsed, "rule.window.decision_book_observed_from_offset_s", maximum=-1)
    window_length = _contract_int(parsed, "rule.window.length_s", minimum=1)
    send_deadline = _contract_int(parsed, "rule.send_deadline_offset_s", maximum=-1)
    if not window_from + window_length < send_deadline:
        raise PilotError("contract_invalid:rule.send_deadline_offset_s")
    back_min = _contract_int(parsed, "rule.selection.best_back_min_h")
    back_max = _contract_int(parsed, "rule.selection.best_back_max_h")
    if not (is_on_ladder(back_min) and is_on_ladder(back_max) and back_min <= back_max):
        raise PilotError("contract_invalid:rule.selection")
    per_day = _contract_int(parsed, "placement_limits.place_records_per_utc_day", minimum=1)
    per_pt4 = _contract_int(parsed, "placement_limits.place_records_per_pt4_session_day", minimum=1, maximum=per_day)
    new_place = _contract_int(parsed, "pause.new_place_counted_at_p", maximum=-1)
    if new_place != -size_p:
        raise PilotError("contract_invalid:pause.new_place_counted_at_p")
    alpha = _contract_rate(parsed, "inference.alpha_one_sided")
    if not 0 < alpha < Fraction(1, 2):
        raise PilotError("contract_invalid:inference.alpha_one_sided")
    return Contract(
        sha256=raw_sha256,
        id=_contract_text(parsed, "id"),
        customer_strategy_ref=_contract_text(parsed, "customer_strategy_ref", pattern=STRATEGY_REF_RE),
        intent_source=_contract_text(parsed, "intent_source", expected="pilot"),
        commission_rate=commission,
        country_codes=frozenset(countries),
        event_type_id=_contract_text(parsed, "rule.market.event_type_id"),
        market_type=_contract_text(parsed, "rule.market.market_type"),
        betting_type=_contract_text(parsed, "rule.market.betting_type", expected="ODDS"),
        number_of_winners=_contract_int(parsed, "rule.market.number_of_winners", minimum=1, maximum=1),
        price_ladder=_contract_text(parsed, "rule.market.price_ladder", expected="CLASSIC"),
        min_active_runners=_contract_int(parsed, "rule.market.min_active_runners_in_decision_book", minimum=1),
        window_from_offset_s=window_from,
        window_length_s=window_length,
        send_deadline_offset_s=send_deadline,
        side=_contract_text(parsed, "rule.order.side", expected="BACK"),
        order_type=_contract_text(parsed, "rule.order.order_type", expected="LIMIT"),
        persistence_type=_contract_text(parsed, "rule.order.persistence_type", expected="LAPSE"),
        size_p=size_p,
        best_back_min_h=back_min,
        best_back_max_h=back_max,
        min_available_p=_contract_int(parsed, "rule.selection.min_available_at_best_back_p", minimum=0),
        max_lay_ticks=_contract_int(parsed, "rule.selection.max_best_lay_ticks_above_best_back", minimum=1),
        lifetime_place_records=_contract_int(parsed, "placement_limits.lifetime_place_records", minimum=1),
        per_utc_day=per_day,
        per_pt4_session_day=per_pt4,
        pause_step_p=_contract_int(parsed, "pause.step_p", minimum=1),
        new_place_counted_at_p=new_place,
        gap_tolerance_p=_contract_int(parsed, "ledger.gap_tolerance_p", minimum=0),
        deadline_s=_contract_int(parsed, "final_execution.deadline_days_after_off", minimum=1) * 86400,
        bsp_cutoff_s=_contract_int(parsed, "bsp.cutoff_hours_after_off", minimum=1) * 3600,
        n_eligible=_contract_int(parsed, "sample.n_eligible", minimum=1),
        alpha_one_sided=alpha,
        min_clusters=_contract_int(parsed, "inference.min_clusters", minimum=2),
        fill_rate_min=_contract_rate(parsed, "validity_gates.fill_rate.min"),
        missing_bsp_max=_contract_rate(parsed, "validity_gates.missing_bsp_share.max"),
        unresolved_max=_contract_rate(parsed, "validity_gates.measurement_unresolved_share.max"),
        final_date=_contract_day(parsed, "endings.final_date"),
        class_order=CLASS_ORDER)


def contract_from_bytes(raw):
    """load_contract on the strict parse of the frozen file's raw bytes, identified by their sha256."""
    sha = contract_sha256(raw)
    try:
        parsed = decimal_loads(raw)
    except PilotError:
        raise PilotError("contract_invalid:json") from None
    return load_contract(parsed, raw_sha256=sha)


# ---------------------------------------------------------------------------------------------------------------
# Raw evidence and the P1 predicates (note §4).

def parse_rpc_body(body):
    """The result of one raw JSON-RPC response body. NaN and Infinity are kept as markers for the fields that may
    carry them (critique 20); every consumer refuses a marker where it reads a number."""
    if not isinstance(body, bytes):
        raise EvidenceError("evidence_unparseable")
    try:
        envelope = decimal_loads(body, allow_nonfinite=True)
    except PilotError:
        raise EvidenceError("evidence_unparseable") from None
    if not isinstance(envelope, dict) or "result" not in envelope or envelope.get("error") is not None:
        raise EvidenceError("evidence_unparseable")
    return envelope["result"]


def parse_rpc_single(body, *, market_id):
    """The one market item of a listMarketCatalogue or listMarketBook body for market_id."""
    result = parse_rpc_body(body)
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise EvidenceError("evidence_not_single")
    if result[0].get("marketId") != market_id:
        raise EvidenceError("evidence_market_mismatch")
    return result[0]


@dataclass(frozen=True)
class RunnerView:
    selection_id: int
    status: str
    best_back: object
    back_size: object
    best_lay: object


@dataclass(frozen=True)
class Evidence:
    row_sha256: str
    book_sha256: str
    country: object
    off_text: object
    off_ns: object
    event_type_id: object
    market_type: object
    betting_type: object
    ladder_type: object
    number_of_winners: object
    is_delayed: object
    runners: tuple


def _ladder(levels):
    """[(price, size)] of one side; a level needs a price above 1 and a size of at least 0, both finite numbers,
    and one price at most once."""
    if levels is None:
        return []
    if not isinstance(levels, list):
        raise EvidenceError("evidence_ladder_invalid")
    found, prices = [], set()
    for level in levels:
        if not isinstance(level, dict):
            raise EvidenceError("evidence_ladder_invalid")
        price, size = _decimal_of(level.get("price")), _decimal_of(level.get("size"))
        if price is None or size is None or not price > 1 or size < 0 or price in prices:
            raise EvidenceError("evidence_ladder_invalid")
        prices.add(price)
        found.append((price, size))
    return found


def _projection(row, *path):
    value = row
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def decision_evidence(row_body, book_body, *, market_id):
    """What the predicates read from the fresh catalogue row and the decision book, as raw bytes. The off is the
    row's marketStartTime; event.openDate is never read. A missing projection leaves its field None (its predicate
    then SKIPs); a malformed ladder, runner or envelope raises EvidenceError."""
    row = parse_rpc_single(row_body, market_id=market_id)
    book = parse_rpc_single(book_body, market_id=market_id)
    off_text = row.get("marketStartTime")
    off_ns = None
    if isinstance(off_text, str) and len(off_text) <= 40:
        try:
            off_ns = parse_utc_ns(off_text)
        except PilotError:
            off_ns = None
    if off_ns is None or not _is_ns(off_ns):
        off_text = off_ns = None                        # kept only when it parses (so it is ASCII)
    country = _projection(row, "event", "countryCode")
    runners = book.get("runners")
    if not isinstance(runners, list):
        raise EvidenceError("evidence_runner_invalid")
    views, seen = [], set()
    for runner in runners:
        if not isinstance(runner, dict):
            raise EvidenceError("evidence_runner_invalid")
        selection, status = runner.get("selectionId"), runner.get("status")
        if type(selection) is not int or selection < 1 or not isinstance(status, str):
            raise EvidenceError("evidence_runner_invalid")
        if selection in seen:
            raise EvidenceError("evidence_duplicate_runner")
        seen.add(selection)
        exchange = runner.get("ex")
        if exchange is not None and not isinstance(exchange, dict):
            raise EvidenceError("evidence_ladder_invalid")
        back = _ladder(exchange.get("availableToBack")) if exchange else []
        lay = _ladder(exchange.get("availableToLay")) if exchange else []
        best_back, back_size = max(back) if back else (None, None)
        views.append(RunnerView(selection_id=selection, status=status, best_back=best_back, back_size=back_size,
                                best_lay=min(price for price, _ in lay) if lay else None))
    return Evidence(
        row_sha256=bytes_sha256(row_body), book_sha256=bytes_sha256(book_body),
        country=country if isinstance(country, str) and COUNTRY_RE.fullmatch(country) else None,
        off_text=off_text, off_ns=off_ns,
        event_type_id=_projection(row, "eventType", "id"),
        market_type=_projection(row, "description", "marketType"),
        betting_type=_projection(row, "description", "bettingType"),
        ladder_type=_projection(row, "description", "priceLadderDescription", "type"),
        number_of_winners=book.get("numberOfWinners"), is_delayed=book.get("isMarketDataDelayed"),
        runners=tuple(views))


def _hundredths(value):
    """int hundredths of an exact Decimal price, or None when it is not a whole number of hundredths."""
    scaled = Fraction(value) * 100
    return scaled.numerator if scaled.denominator == 1 else None


def evaluate_predicates(contract, ev, *, observed_wall_ns):
    """PLACE or SKIP(code) on one decision's evidence (note §4); the first failing predicate in SKIP_CODES order
    wins. The window is closed at both ends on the wall clock read before the book request (decision Q5)."""
    if type(observed_wall_ns) is not int:
        raise PilotError("observed_wall_ns_invalid")
    active = sorted(r.selection_id for r in ev.runners if r.status == "ACTIVE")
    deadline = None if ev.off_ns is None else ev.off_ns + contract.send_deadline_offset_s * NS

    def skip(code):
        return {"outcome": "SKIP", "code": code, "selection_id": None, "limit_price_h": None,
                "active_selection_ids": active, "send_deadline_ns": deadline}

    if ev.is_delayed is not False:
        return skip("book_delayed")
    if ev.off_ns is None:
        return skip("off_missing")
    start = ev.off_ns + contract.window_from_offset_s * NS
    if not start <= observed_wall_ns <= start + contract.window_length_s * NS:
        return skip("window")
    if not isinstance(ev.country, str) or ev.country not in contract.country_codes:
        return skip("country_not_allowed")
    if type(ev.event_type_id) is not str or ev.event_type_id != contract.event_type_id:
        return skip("event_type_not_allowed")
    if type(ev.market_type) is not str or ev.market_type != contract.market_type:
        return skip("market_type_not_allowed")
    if type(ev.betting_type) is not str or ev.betting_type != contract.betting_type:
        return skip("betting_type_not_odds")
    if type(ev.number_of_winners) is not int or ev.number_of_winners != contract.number_of_winners:
        return skip("number_of_winners_not_1")
    if type(ev.ladder_type) is not str or ev.ladder_type != contract.price_ladder:
        return skip("ladder_not_classic")
    if len(active) < contract.min_active_runners:
        return skip("too_few_active_runners")
    priced = [r for r in ev.runners if r.status == "ACTIVE" and r.best_back is not None]
    if not priced:
        return skip("no_priced_active_runner")
    lowest = min(r.best_back for r in priced)
    favourites = [r for r in priced if r.best_back == lowest]
    if len(favourites) != 1:
        return skip("favourite_tie")
    favourite = favourites[0]
    back_h = _hundredths(favourite.best_back)
    if back_h is None or not is_on_ladder(back_h):
        return skip("best_back_off_ladder")
    if not contract.best_back_min_h <= back_h <= contract.best_back_max_h:
        return skip("best_back_out_of_range")
    if Fraction(favourite.back_size) * 100 < contract.min_available_p:
        return skip("depth_below_min")
    if favourite.best_lay is None:
        return skip("best_lay_missing")
    if favourite.best_lay <= favourite.best_back:
        return skip("book_crossed")
    lay_h = _hundredths(favourite.best_lay)
    if lay_h is None or not is_on_ladder(lay_h):
        return skip("best_lay_off_ladder")
    if not 1 <= tick_index(lay_h) - tick_index(back_h) <= contract.max_lay_ticks:
        return skip("spread_too_wide")
    return {"outcome": "PLACE", "code": None, "selection_id": favourite.selection_id, "limit_price_h": back_h,
            "active_selection_ids": active, "send_deadline_ns": deadline}


# ---------------------------------------------------------------------------------------------------------------
# Pilot note payloads (note §6): exact keys per type; the market and order records also use a journal column.

def _is_sha(v):
    return isinstance(v, str) and SHA_RE.fullmatch(v) is not None


def _is_int(v, minimum=None):
    return type(v) is int and (minimum is None or v >= minimum)


def _opt(check):
    return lambda v: v is None or check(v)


def _matches(pattern):
    return lambda v: isinstance(v, str) and pattern.fullmatch(v) is not None


def _one_of(*values):
    return lambda v: isinstance(v, str) and v in values


def _is_day(v):
    if not isinstance(v, str) or not DAY_RE.fullmatch(v):
        return False
    try:
        _day_start_ns(v)
    except PilotError:
        return False
    return True


def _is_bool(v):
    return type(v) is bool


def _short_text(v):
    return _clean_text(v) is not None


def _row_check(keys, numbers, extra):
    def check(row):
        if not isinstance(row, dict) or set(row) != set(keys):
            return False
        for key in keys:
            value = row[key]
            if key in numbers:
                if value is not None and not (isinstance(value, str) and DECIMAL_TEXT_RE.fullmatch(value)):
                    return False
            elif key in extra:
                if not extra[key](value):
                    return False
            elif value is not None and not _short_text(value):
                return False
        return True
    return check


_CURRENT_ROW = _row_check(CURRENT_ROW_KEYS, CURRENT_ROW_NUMBERS, {"selectionId": _opt(lambda v: _is_int(v, 1))})
_CLEARED_ROW = _row_check(CLEARED_ROW_KEYS, CLEARED_ROW_NUMBERS,
                          {"selectionId": _opt(lambda v: _is_int(v, 1)), "priceReduced": _opt(_is_bool)})


def _book_runners(value):
    if not isinstance(value, dict) or len(value) > 200:
        return False
    for key, runner in value.items():
        if not re.fullmatch(r"[1-9][0-9]{0,19}", key) or not isinstance(runner, dict):
            return False
        if set(runner) != {"status", "actual_sp"} or not _opt(_short_text)(runner["status"]):
            return False
        sp = runner["actual_sp"]
        if not (sp is None or sp in _NONFINITE_TEXT or (isinstance(sp, str) and DECIMAL_TEXT_RE.fullmatch(sp))):
            return False
    return True


def _decision_fields():
    return {
        "registration_receipt_sha256": _is_sha, "session_id": _matches(ID_RE), "open_id": _matches(ID_RE),
        "reservation_seq": lambda v: _is_int(v, 1), "market_id": _matches(MARKET_ID_RE),
        "outcome": _one_of("PLACE", "SKIP"), "code": _opt(_one_of(*SKIP_CODES)),
        "selection_id": _opt(lambda v: _is_int(v, 1)), "side": _short_text, "order_type": _short_text,
        "limit_price_h": _opt(_is_int), "stake_p": _is_int, "persistence_type": _short_text,
        "customer_order_ref": _opt(_matches(REF_RE)), "country": _opt(_matches(COUNTRY_RE)),
        "off": _opt(lambda v: isinstance(v, str) and len(v) <= 40 and _parse_or_none(v) is not None),
        "off_ns": _opt(_is_ns), "decision_book_observed_at": lambda v: isinstance(v, str) and len(v) <= 40
                                                                      and _parse_or_none(v) is not None,
        "decision_book_wall_ns": _is_ns, "decision_book_monotonic_ns": _is_ns,
        "is_market_data_delayed": _opt(_is_bool), "send_deadline_ns": _opt(_is_ns), "row_sha256": _is_sha,
        "book_sha256": _is_sha,
        "active_selection_ids": _opt(lambda v: isinstance(v, list) and len(v) <= 200
                                     and all(_is_int(s, 1) for s in v) and v == sorted(set(v))),
        "utc_day": _is_day, "day_limit": lambda v: _is_int(v, 1), "lifetime_slot": _opt(lambda v: _is_int(v, 1)),
        "day_count": _opt(lambda v: _is_int(v, 1))}


NOTE_FIELDS = {
    "registration": {"registration_receipt_sha256": _is_sha, "predecessor_contract_sha256": _opt(_is_sha)},
    "session_start": {"session_id": _matches(ID_RE), "registration_receipt_sha256": _is_sha, "utc_day": _is_day,
                      "until_ns": _is_ns},
    "reservation": {"session_id": _matches(ID_RE), "open_id": _matches(ID_RE)},
    "decision": _decision_fields(),
    "stop": {"stop_id": _matches(STOP_ID_RE), "scope": _one_of("session", "review"), "reason": _matches(CODE_RE),
             "level_p": _opt(_is_int), "source_seq": _opt(lambda v: _is_int(v, 1))},
    "ack": {"stop_id": _matches(STOP_ID_RE), "cause_written": _is_bool},
    "capture_c": {"bet_id": _matches(BET_ID_RE), "raw_sha256": _is_sha, "row": _CURRENT_ROW},
    "capture_l": {"bet_id": _matches(BET_ID_RE), "raw_sha256": _is_sha, "row": _CLEARED_ROW},
    "capture_book": {"raw_sha256": _is_sha, "status": _opt(_short_text), "bsp_reconciled": _opt(_is_bool),
                     "runners": _book_runners},
    "capture_commission": {"raw_sha256": _is_sha,
                           "commission": _opt(lambda v: isinstance(v, str) and DECIMAL_TEXT_RE.fullmatch(v))},
    "commission_verified": {"market_id": _matches(MARKET_ID_RE), "our_commission_p": lambda v: _is_int(v, 0),
                            "website_commission_p": lambda v: _is_int(v, 0)},
    "pt_receipt": {"kind": _one_of("PT1", "PT4"), "receipt_sha256": _is_sha, "registration_receipt_sha256": _is_sha,
                   "l_fields_verified": _is_bool, "matched_settled_bets": lambda v: _is_int(v, 0),
                   "profit_basis": _opt(_one_of("gross", "net"))},
    "ending": {"reason": _one_of("yu_written", "breach"), "code": _matches(CODE_RE)},
    "violation": {"code": _matches(CODE_RE), "source_seq": _opt(lambda v: _is_int(v, 1))},
    # A classify refusal as journaled by the gateway: the fold derives its review stop from it (Astra #2).
    "refusal": {"session_id": _opt(_matches(ID_RE)), "open_id": _opt(_matches(ID_RE)),
                "reservation_seq": _opt(lambda v: _is_int(v, 1)), "code": _matches(CODE_RE)},
    # A ledger gap's resolution, separate from its stop (Astra #6); journaled by P1-08/P1-10.
    "gap_resolved": {"stop_id": _matches(STOP_ID_RE), "decision_seq": _opt(lambda v: _is_int(v, 1)),
                     "cause_written": _is_bool},
    # The durable finalization frontier of one PLACE record (Astra #7); journaled by P1-10.
    "finalized": {"final_at_ns": _is_ns, "row": lambda v: _is_int(v, 1) and v <= 5,
                  "class": _one_of(*CLASS_ORDER)},
}
MARKET_NOTES = ("reservation", "decision", "capture_book", "capture_commission")
ORDER_NOTES = ("capture_c", "capture_l", "finalized")


def validate_pilot_payload(payload):
    """The pilot record type of a note payload, None for a note that is not a pilot record, or
    PilotError("pilot_record_invalid"). No float may appear: every Betfair number is decimal text."""
    if not isinstance(payload, dict) or "pilot" not in payload:
        return None
    inner = payload["pilot"]
    if set(payload) != {"pilot"} or not isinstance(inner, dict):
        raise PilotError("pilot_record_invalid")
    kind = inner.get("type")
    fields = NOTE_FIELDS.get(kind) if isinstance(kind, str) else None
    if (fields is None or set(inner) != {"v", "type", "contract_sha256", *fields} or inner["v"] != 1
            or type(inner["v"]) is not int or not _is_sha(inner["contract_sha256"])
            or _non_integer_number(inner)):
        raise PilotError("pilot_record_invalid")
    if not all(check(inner[key]) for key, check in fields.items()):
        raise PilotError("pilot_record_invalid")
    if kind == "decision":
        place = inner["outcome"] == "PLACE"
        required = ("selection_id", "limit_price_h", "customer_order_ref", "lifetime_slot", "day_count",
                    "send_deadline_ns", "off_ns", "active_selection_ids")
        if place and (inner["code"] is not None or any(inner[key] is None for key in required)):
            raise PilotError("pilot_record_invalid")
        if not place and (inner["code"] is None or any(inner[key] is not None for key in required[:5])):
            raise PilotError("pilot_record_invalid")
    return kind


def pilot_note(record_type, contract_sha256, /, **fields):
    """{"pilot": {...}} for one record type, validated; the caller journals it as a note with the column the type
    needs (market_id for MARKET_NOTES, customer_order_ref for ORDER_NOTES)."""
    payload = {"pilot": dict(fields, v=1, type=record_type, contract_sha256=contract_sha256)}
    validate_pilot_payload(payload)
    return payload


def _validate_event_columns(kind, inner, event):
    if event.get("state_after") is not None:
        raise PilotError("pilot_record_invalid")
    if kind in MARKET_NOTES and not (isinstance(event.get("market_id"), str)
                                     and MARKET_ID_RE.fullmatch(event["market_id"])):
        raise PilotError("pilot_record_invalid")
    if kind == "decision" and inner["market_id"] != event["market_id"]:
        raise PilotError("pilot_record_invalid")
    if kind in ORDER_NOTES and not (isinstance(event.get("customer_order_ref"), str)
                                    and REF_RE.fullmatch(event["customer_order_ref"])):
        raise PilotError("pilot_record_invalid")


def stop_payload(contract, stop_id, scope, reason, *, level_p=None, source_seq=None):
    return {"v": 1, "type": "stop", "contract_sha256": contract.sha256, "stop_id": stop_id, "scope": scope,
            "reason": reason, "level_p": level_p, "source_seq": source_seq}


def stop_id_for(reason, source_seq):
    """'<reason with hyphens>-<source seq>': typable in PILOT ACK, unique per cause."""
    head = "-".join(reason.split("_"))
    return head if source_seq is None else f"{head}-{source_seq}"


def pause_stop_ids(level_p):
    """(review stop id, session stop id) of one pause level, e.g. ('pause-15000', 'pause-15000-session')."""
    return f"pause-{-level_p}", f"pause-{-level_p}-session"


def pause_stops(contract, level_p):
    review, session = pause_stop_ids(level_p)
    return [stop_payload(contract, session, "session", "pause", level_p=level_p),
            stop_payload(contract, review, "review", "pause", level_p=level_p)]


# ---------------------------------------------------------------------------------------------------------------
# Raw C, L and closed-book captures (note §11).

def _row_number(value):
    """Decimal text of a consumed Betfair number; None when absent; a marker or non-number is refused."""
    if value is None:
        return None
    number = _decimal_of(value)
    if number is None:
        raise EvidenceError("evidence_number_invalid")
    return money_text(number)


def _row_text(value):
    return _clean_text(value)


def _single_row(rows, bet_id):
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise EvidenceError("evidence_unparseable")
    found = [row for row in rows if row.get("betId") == bet_id]
    if len(found) != 1:
        raise EvidenceError("evidence_not_single")
    return found[0]


def extract_current_row(body, *, bet_id):
    """The C capture: the one listCurrentOrders row for bet_id, numbers as decimal text (None when absent)."""
    result = parse_rpc_body(body)
    row = _single_row(result.get("currentOrders") if isinstance(result, dict) else None, bet_id)
    selection = row.get("selectionId")
    found = {key: _row_number(row.get(key)) for key in CURRENT_ROW_NUMBERS}
    if execution_price(found["averagePriceMatched"]) is None:
        found["averagePriceMatched"] = None            # outside 1.01-1000: the price is unresolved
    found.update(status=_row_text(row.get("status")), marketId=_row_text(row.get("marketId")),
                 selectionId=selection if type(selection) is int and selection >= 1 else None,
                 customerOrderRef=_row_text(row.get("customerOrderRef")),
                 customerStrategyRef=_row_text(row.get("customerStrategyRef")))
    return found


def extract_cleared_row(body, *, bet_id, bet_status="SETTLED"):
    """The L capture: the one listClearedOrders row for bet_id; bet_status is the request's betStatus filter (a
    cleared row does not carry it). A missing priceReduced stays None, which never means False (critique 20)."""
    if bet_status not in ("SETTLED", "VOIDED", "LAPSED", "CANCELLED"):
        raise PilotError("bet_status_invalid")
    result = parse_rpc_body(body)
    row = _single_row(result.get("clearedOrders") if isinstance(result, dict) else None, bet_id)
    selection, reduced = row.get("selectionId"), row.get("priceReduced")
    found = {key: _row_number(row.get(key)) for key in CLEARED_ROW_NUMBERS}
    if execution_price(found["priceMatched"]) is None:
        found["priceMatched"] = None                   # outside 1.01-1000: the price is unresolved
    found.update(betStatus=bet_status, priceReduced=reduced if type(reduced) is bool else None,
                 betOutcome=_row_text(row.get("betOutcome")), marketId=_row_text(row.get("marketId")),
                 selectionId=selection if type(selection) is int and selection >= 1 else None,
                 customerOrderRef=_row_text(row.get("customerOrderRef")),
                 settledDate=_row_text(row.get("settledDate")))
    return found


def extract_closed_book(body, *, market_id):
    """The SP_TRADED book: {status, bsp_reconciled, runners: {str(selection): {status, actual_sp}}}; actualSP is
    decimal text, a NaN/Infinity marker text, or None. Duplicate runners are refused."""
    book = parse_rpc_single(body, market_id=market_id)
    runners = book.get("runners")
    if not isinstance(runners, list):
        raise EvidenceError("evidence_runner_invalid")
    found = {}
    for runner in runners:
        selection = runner.get("selectionId") if isinstance(runner, dict) else None
        if type(selection) is not int or selection < 1:
            raise EvidenceError("evidence_runner_invalid")
        if str(selection) in found:
            raise EvidenceError("evidence_duplicate_runner")
        sp = _projection(runner, "sp", "actualSP")
        if isinstance(sp, NonFinite):
            sp_text = sp.text
        elif _decimal_of(sp) is not None:
            sp_text = money_text(_decimal_of(sp))
        else:
            sp_text = None
        found[str(selection)] = {"status": _row_text(runner.get("status")), "actual_sp": sp_text}
    reconciled = book.get("bspReconciled")
    return {"status": _row_text(book.get("status")), "bsp_reconciled": reconciled if type(reconciled) is bool else None,
            "runners": found}


def execution_price(value):
    """A matched price within the CLASSIC bounds as an exact Decimal, else None (unresolved)."""
    price = _decimal_text_value(value) if isinstance(value, str) else value
    if not isinstance(price, Decimal) or not price.is_finite() or not _MIN_PRICE <= Fraction(price) <= _MAX_PRICE:
        return None
    return price


def parse_current_row(row):
    """A capture_c row with its numbers as Decimal (None stays None); a price outside 1.01-1000 is None."""
    found = {key: (_decimal_text_value(value) if key in CURRENT_ROW_NUMBERS else value) for key, value in row.items()}
    found["averagePriceMatched"] = execution_price(found.get("averagePriceMatched"))
    return found


def parse_cleared_row(row):
    found = {key: (_decimal_text_value(value) if key in ("sizeSettled", "priceMatched") else value)
             for key, value in row.items()}
    found["priceMatched"] = execution_price(found.get("priceMatched"))
    return found


# ---------------------------------------------------------------------------------------------------------------
# Final execution record, capture and consistency checks, BSP (note §11).

def _fraction_or_none(value):
    if isinstance(value, Decimal) and value.is_finite():
        return Fraction(value)
    if isinstance(value, str):
        parsed = _decimal_text_value(value)
        return None if parsed is None else Fraction(parsed)
    return None


def _four_sizes(row, size_p):
    """C's matched, cancelled, lapsed and voided sizes as Fractions, or None when one is missing, negative, or
    the matched size is above the stake."""
    parts = [_fraction_or_none(row.get(key)) for key in ("sizeMatched", "sizeCancelled", "sizeLapsed", "sizeVoided")]
    if any(part is None or part < 0 for part in parts) or parts[0] * 100 > size_p:
        return None
    return parts


def capture_check(c_row, *, place_report_matched_p, size_p=200):
    """Run when a C read showing EXECUTION_COMPLETE is captured (note §11): its sizeMatched is at least the place
    report's (no clause without a usable place report), and matched + cancelled + lapsed + voided equals the stake
    exactly. A missing or negative size, or more matched than the stake, fails; nothing is taken as 0."""
    parts = _four_sizes(c_row, size_p)
    if parts is None:
        return False
    if place_report_matched_p is not None and parts[0] * 100 < place_report_matched_p:
        return False
    return sum(parts) * 100 == size_p


def consistency_check(final_size, *, place_report_matched_p, c, size_p=200):
    """K:48: the final matched size is at least the place report's, and C's four sizes sum to the stake. A clause
    whose input is missing is not run (decision Q10)."""
    size = Fraction(final_size)
    if size < 0 or size * 100 > size_p:
        return False
    if place_report_matched_p is not None and size * 100 < place_report_matched_p:
        return False
    if c is not None:
        parts = _four_sizes(c, size_p)
        if parts is None or sum(parts) * 100 != size_p:
            return False
    return True


def canonical_c(c_reads):
    """The earliest read by seq that shows EXECUTION_COMPLETE (never by loading order); reads are
    {"seq", "recorded_ns", "row"}."""
    complete = [read for read in c_reads if read["row"].get("status") == "EXECUTION_COMPLETE"]
    return min(complete, key=lambda read: read["seq"]) if complete else None


def canonical_l(l_rows):
    """The earliest settled L row by seq."""
    settled = [read for read in l_rows if read["row"].get("betStatus") == "SETTLED"]
    return min(settled, key=lambda read: read["seq"]) if settled else None


def l_counts_at(l_row_ns, l_verified_ns):
    """When an L row counts as read: the later of its capture and the verifying PT receipt; None before one."""
    if l_verified_ns is None:
        return None
    return max(l_row_ns, l_verified_ns)


def _equal(a, b):
    return a is not None and b is not None and a == b


def finalize(c, l, *, c_ns, l_count_ns, deadline_ns, now_ns, removal_classed=False):
    """The finalization table (note §11; K:55-81) on the canonical C row and L row (Decimal values).

    A C read counts if it was recorded by the deadline (inclusive); an L row counts from l_count_ns if that is by
    the deadline and not after now_ns. Deadline-driven finality comes only when now_ns > deadline_ns, and is then
    reported at deadline_ns + 1 (critique 5). A removal-classed bet is not price-compared: its size alone is taken
    from the table (K:50). Returns {"final", "final_at_ns", "row", "size", "P", "unresolved"}; size None means the
    size is unresolved."""
    c_ok = c is not None and c_ns is not None and c_ns <= deadline_ns
    l_ok = l is not None and l_count_ns is not None and l_count_ns <= deadline_ns and l_count_ns <= now_ns

    def result(final, at, row, size=None, price=None):
        price = execution_price(price) if price is not None else None
        unresolved = size is None or (size > 0 and price is None and not removal_classed)
        return {"final": final, "final_at_ns": at, "row": row, "size": size if final else None,
                "P": price if final else None, "unresolved": unresolved if final else None}

    if l_ok:
        reduced = l.get("priceReduced")
        if c_ok:
            sizes_equal = _equal(c.get("sizeMatched"), l.get("sizeSettled"))
            if removal_classed:
                return result(True, l_count_ns, 1 if sizes_equal else 2,
                              l["sizeSettled"] if sizes_equal else None)
            if sizes_equal and _equal(c.get("averagePriceMatched"), l.get("priceMatched")) and reduced is False:
                return result(True, l_count_ns, 1, l["sizeSettled"], l["priceMatched"])
            return result(True, l_count_ns, 2)
        if removal_classed:
            return result(True, l_count_ns, 4, l.get("sizeSettled"))
        if reduced is False and l.get("sizeSettled") is not None:
            return result(True, l_count_ns, 4, l["sizeSettled"], l.get("priceMatched"))
        return result(True, l_count_ns, 4)
    if now_ns > deadline_ns:
        if c_ok:
            return result(True, deadline_ns + 1, 3, c.get("sizeMatched"), c.get("averagePriceMatched"))
        return result(True, deadline_ns + 1, 5)
    return result(False, None, None)


def freeze_bsp(book_captures, *, selection_id, off_ns, now_ns, cutoff_s=86400):
    """The frozen BSP (note §11): actualSP of the first capture by seq showing CLOSED, if recorded by off + cutoff
    (inclusive; decision Q25: the capture note's recorded_at), finite and above 1, our runner WINNER or LOSER in
    it, and bspReconciled true. Decided at that read, or once now_ns passes the cutoff with no CLOSED read.
    Captures are {"seq", "recorded_ns", "status", "bsp_reconciled", "runners"}."""
    cutoff = off_ns + cutoff_s * NS
    closed = [read for read in book_captures if read["status"] == "CLOSED"]
    first = min(closed, key=lambda read: read["seq"]) if closed else None
    if first is None:
        decided = now_ns > cutoff
        return {"decided": decided, "decided_at_ns": cutoff + 1 if decided else None, "bsp": None,
                "read_seq": None, "closed_read": None}
    runner = first["runners"].get(str(selection_id)) or {}
    sp = _decimal_text_value(runner.get("actual_sp"))
    valid = (first["recorded_ns"] <= cutoff and sp is not None and sp > 1 and runner.get("status") in RESULTS
             and first["bsp_reconciled"] is True)
    return {"decided": True, "decided_at_ns": min(first["recorded_ns"], cutoff + 1), "bsp": sp if valid else None,
            "read_seq": first["seq"], "closed_read": first}


# ---------------------------------------------------------------------------------------------------------------
# NCLV (K:149-159) and ledger arithmetic (note §10).

def _exact(value):
    if isinstance(value, (Fraction, int)) and not isinstance(value, bool):
        return Fraction(value)
    if isinstance(value, Decimal) and value.is_finite():
        return Fraction(value)
    raise PilotError("number_invalid")


def nclv(P, bsp, *, c):
    """(1 + (P - 1) * (1 - c)) / BSP - 1, exact. P is the final record's price at full precision (never the
    place report's floored average, note §11); P and BSP may be Decimal or Fraction."""
    price, base, rate = _exact(P), _exact(bsp), _exact(c)
    if base <= 0:
        raise PilotError("bsp_invalid")
    return (1 + (price - 1) * (1 - rate)) / base - 1


def decision_price_nclv(limit_price_h, bsp, *, c):
    """NCLV at the decision price, for the reporting series only (K:182)."""
    if type(limit_price_h) is not int:
        raise PilotError("price_invalid")
    return nclv(Fraction(limit_price_h, 100), bsp, c=c)


def stake_weighted_mean(pairs):
    """sum(s_i * x_i) / sum(s_i) for reporting (K:181); None when the sizes sum to 0."""
    total = sum((_exact(size) for size, _ in pairs), Fraction(0))
    if total == 0:
        return None
    return sum(_exact(size) * _exact(x) for size, x in pairs) / total


def loss_pence(size):
    """-ceil(size * 100): a non-penny size is rounded against us (decision Q11)."""
    return -_ceil(_exact(size) * 100)


def bf_profit_pence(text):
    """Betfair's per-bet profit in pence: None when absent; PilotError("profit_not_exact_pence") when present but
    not exact to the penny, which counts as a cleared-figure gap (decision Q11)."""
    if text is None:
        return None
    value = _decimal_text_value(text) if isinstance(text, str) else _decimal_of(text)
    if value is None:
        raise PilotError("profit_not_exact_pence")
    scaled = Fraction(value) * 100
    if scaled.denominator != 1:
        raise PilotError("profit_not_exact_pence")
    return scaled.numerator


def _pence_or_none(text):
    """A Betfair money figure in exact pence, or None when absent or not exact to the penny."""
    try:
        return bf_profit_pence(text)
    except PilotError:
        return None


def s_winner_value(final_size, P, bf_profit_p, *, c):
    """gross = floor(size * (P - 1)) to the penny, P at full precision; commission = ceil(c * max(gross, 0));
    net = gross - commission; value = min(net, Betfair profit - commission) (note §10)."""
    gross = _floor(_exact(final_size) * (_exact(P) - 1) * 100)
    commission = _ceil(_exact(c) * max(gross, 0))
    net = gross - commission
    return {"gross_p": gross, "commission_p": commission, "net_p": net, "gap_p": abs(gross - bf_profit_p),
            "value_p": min(net, bf_profit_p - commission)}


def pause_levels_hit(equity_p, *, step_p, acked):
    """Every level -step*k (k >= 1) at or above equity that is not acknowledged, -step first (note §10)."""
    if type(equity_p) is not int or type(step_p) is not int or step_p <= 0:
        raise PilotError("pause_input_invalid")
    deepest = (-equity_p) // step_p if equity_p < 0 else 0
    return [-step_p * k for k in range(1, deepest + 1) if -step_p * k not in acked]


# ---------------------------------------------------------------------------------------------------------------
# The decision record, capacity and the pause at classification (note §4, §6, §10).

def record_sha256(record):
    """The decision record's digest, bound into the risk decision and the capability (note §4)."""
    return canonical_sha256(record)


def expected_instruction(record):
    """The order body a PLACE record allows, in the keys of schemas.validate_instruction_v1."""
    return {"selection_id": record["selection_id"], "handicap": 0, "side": record["side"],
            "price_h": record["limit_price_h"], "size_p": record["stake_p"],
            "persistence_type": record["persistence_type"]}


def _same(a, b):
    return type(a) is type(b) and a == b


def check_binding(record, *, contract, market_id, instruction, customer_order_ref, customer_strategy_ref, open_id,
                  session_id, record_digest):
    """None, or the R13 code refusing an order (its market and body) against its PLACE record from this same open,
    field by field (note §4)."""
    if not isinstance(record, dict) or record.get("outcome") != "PLACE":
        return "r13_not_place"
    if record.get("open_id") != open_id:
        return "r13_other_open"
    if record.get("session_id") != session_id:
        return "r13_other_session"
    if record.get("contract_sha256") != contract.sha256:
        return "r13_other_contract"
    if not isinstance(record_digest, str) or record_sha256(record) != record_digest:
        return "r13_digest_mismatch"
    if market_id != record.get("market_id"):
        return "r13_market_differs"
    expected = expected_instruction(record)
    if (not isinstance(instruction, dict) or set(instruction) != set(expected)
            or not all(_same(instruction[key], expected[key]) for key in expected)):
        return "r13_body_differs"
    if customer_order_ref != record.get("customer_order_ref"):
        return "r13_ref_differs"
    if customer_strategy_ref != contract.customer_strategy_ref:
        return "r13_strategy_ref_differs"
    return None


def _capacity(contract, *, equity_p, acked, lifetime_used, day_used, pt4, slot_seq):
    """Pause first, then cap (decision Q7); one pure rule for classify and for the fold's after-the-fact check."""
    day_limit = contract.per_pt4_session_day if pt4 else contract.per_utc_day
    base = {"code": None, "level_p": None, "lifetime_slot": None, "day_count": None, "day_limit": day_limit,
            "stops": []}
    levels = pause_levels_hit(equity_p + contract.new_place_counted_at_p, step_p=contract.pause_step_p, acked=acked)
    if levels:
        stops = [stop for level in levels for stop in pause_stops(contract, level)]
        return dict(base, code="pause", level_p=levels[0], stops=stops)
    if lifetime_used >= contract.lifetime_place_records:
        return dict(base, code="cap")
    if day_used >= day_limit:
        stop = stop_payload(contract, stop_id_for("day_limit", slot_seq) + "-session", "session", "day_limit",
                            source_seq=slot_seq)
        return dict(base, code="cap", stops=[stop])
    return dict(base, lifetime_slot=lifetime_used + 1, day_count=day_used + 1)


def capacity_and_pause(contract, state, *, utc_day, session_id=None, source_seq=None):
    """For a would-be PLACE: SKIP(pause) with a session and a review stop per unacknowledged level the new bet's
    -200 would reach; else SKIP(cap) at the lifetime cap (the ending) or the day limit (a session stop, N:117);
    else the record's lifetime slot and day count. The day limit is the PT4 one in a PT4 session."""
    session = _session_of(state, session_id)
    return _capacity(contract, equity_p=state.equity_p, acked=set(state.pause_acked),
                     lifetime_used=state.lifetime_used, day_used=state.day_used.get(utc_day, 0),
                     pt4=bool(session and session["pt4"]), slot_seq=source_seq)


def _session_of(state, session_id):
    sessions = [s for s in state.sessions if session_id is None or s["session_id"] == session_id]
    return sessions[-1] if sessions else None


def session_end_ns(session):
    """A session ends at the first of UNTIL and 23:59:59Z of its UTC day (note §3)."""
    return min(session["until_ns"], _day_start_ns(session["utc_day"]) + DAY_NS - NS)


def decision_record(contract, *, outcome, code, registration_receipt_sha256, session_id, open_id, reservation_seq,
                    market_id, customer_order_ref, ev, row_body, book_body, predicates, observed_wall_ns,
                    observed_monotonic_ns, day_limit, lifetime_slot=None, day_count=None):
    """The decision record's exact keys (note §4 plus customer_order_ref, active_selection_ids, utc_day and
    day_limit). On SKIP the selection, price, ref, slot and count fields are None."""
    place = outcome == "PLACE"
    delayed = ev.is_delayed if ev is not None and type(ev.is_delayed) is bool else None
    return {
        "v": 1, "type": "decision", "contract_sha256": contract.sha256,
        "registration_receipt_sha256": registration_receipt_sha256, "session_id": session_id, "open_id": open_id,
        "reservation_seq": reservation_seq, "market_id": market_id, "outcome": outcome, "code": code,
        "selection_id": predicates["selection_id"] if place else None, "side": contract.side,
        "order_type": contract.order_type, "limit_price_h": predicates["limit_price_h"] if place else None,
        "stake_p": contract.size_p, "persistence_type": contract.persistence_type,
        "customer_order_ref": customer_order_ref if place else None,
        "country": ev.country if ev is not None else None, "off": ev.off_text if ev is not None else None,
        "off_ns": ev.off_ns if ev is not None else None,
        "decision_book_observed_at": utc_text_from_ns(observed_wall_ns),
        "decision_book_wall_ns": observed_wall_ns, "decision_book_monotonic_ns": observed_monotonic_ns,
        "is_market_data_delayed": delayed,
        "send_deadline_ns": predicates["send_deadline_ns"] if predicates else None,
        "row_sha256": bytes_sha256(row_body), "book_sha256": bytes_sha256(book_body),
        "active_selection_ids": list(predicates["active_selection_ids"]) if predicates else None,
        "utc_day": utc_day(observed_wall_ns), "day_limit": day_limit,
        "lifetime_slot": lifetime_slot if place else None, "day_count": day_count if place else None}


def classify(contract, state, *, reservation_seq, open_id, session_id, registration_receipt_sha256,
             customer_order_ref, row_body, book_body, observed_wall_ns, observed_monotonic_ns):
    """One classification of one reservation on one fetch (note §4 steps 3-4).

    Two times stay apart (Astra #4). The decision book's observed_wall_ns governs the predicates, the window, the
    day slot, the CR1 cluster and the send deadline. Placement authority (capacity, stops, the session's end or
    termination, every ending including a time-driven N, the final date and the lifetime cap) is judged at the
    state's head and clock: state must be folded at or after observed_wall_ns, and a benign head change is
    re-checked with the same bytes. If an ending, a stop or full capacity holds then, the result is never PLACE.
    Returns {"refusal": None | code, "record": decision payload | None, "stops": [stop payloads],
    "refusal_record": refusal payload | None}. A refusal makes no decision record, so the reservation folds as
    SKIP(interrupted); every refusal but placing_ended sets a review stop (N:117), which the fold also derives from
    the refusal record when that is journaled (Astra #2). There is no country, off, book or catalogue parameter:
    everything is read from the two bodies, and nothing is fetched again. Deterministic in its inputs."""
    def refuse(code):
        source = reservation_seq if _is_int(reservation_seq, 1) else None
        stops = [] if code == "placing_ended" else [
            stop_payload(contract, stop_id_for(code, source), "review", code, source_seq=source)]
        record = {"v": 1, "type": "refusal", "contract_sha256": contract.sha256,
                  "session_id": session_id if isinstance(session_id, str) and ID_RE.fullmatch(session_id) else None,
                  "open_id": open_id if isinstance(open_id, str) and ID_RE.fullmatch(open_id) else None,
                  "reservation_seq": source, "code": code}
        return {"refusal": code, "record": None, "stops": stops, "refusal_record": record}

    def decided(record, stops=()):
        return {"refusal": None, "record": record, "stops": list(stops), "refusal_record": None}

    if not (_is_int(reservation_seq, 1) and _is_ns(observed_wall_ns) and _is_ns(observed_monotonic_ns)
            and isinstance(open_id, str) and ID_RE.fullmatch(open_id)
            and isinstance(session_id, str) and ID_RE.fullmatch(session_id) and _is_sha(registration_receipt_sha256)
            and isinstance(customer_order_ref, str) and REF_RE.fullmatch(customer_order_ref)
            and isinstance(row_body, bytes) and isinstance(book_body, bytes)):
        return refuse("caller_input_invalid")
    if state.contract != contract:
        return refuse("contract_mismatch")
    if state.now_ns < observed_wall_ns:
        return refuse("state_before_observation")    # authority is judged at a head folded at or after the book
    now = state.now_ns
    ending = placing_ending(state, now_ns=now)
    if ending is not None and ending["reason"] != "lifetime_cap":
        return refuse("placing_ended")          # at the lifetime cap the record itself is SKIP(cap) (Q7)
    reservation = next((r for r in state.reservations.values() if r["seq"] == reservation_seq), None)
    if reservation is None:
        return refuse("reservation_unknown")
    if reservation["open_id"] != open_id:
        return refuse("reservation_other_open")
    if reservation["decision_seq"] is not None:
        return refuse("reservation_already_decided")
    if reservation["interrupted"]:
        return refuse("reservation_interrupted")       # its open ended: SKIP(interrupted), never re-decided
    session = _session_of(state, session_id)
    if session is None or reservation["session_id"] != session_id or state.l_fields_refuted:
        return refuse("session_not_armed")
    if session["registration_receipt_sha256"] != registration_receipt_sha256:
        return refuse("registration_mismatch")
    if session["ended_seq"] is not None:
        return refuse("session_ended")                 # a KILL, HALT or stop ended it for good (Astra #1)
    if now >= session_end_ns(session):
        return refuse("session_expired")
    if state.active_stops:
        return refuse("stop_active")
    market_id = reservation["market_id"]
    try:
        ev = decision_evidence(row_body, book_body, market_id=market_id)
    except EvidenceError:
        ev = None
    day = utc_day(observed_wall_ns)
    day_limit = contract.per_pt4_session_day if session["pt4"] else contract.per_utc_day
    common = dict(registration_receipt_sha256=registration_receipt_sha256, session_id=session_id, open_id=open_id,
                  reservation_seq=reservation_seq, market_id=market_id, customer_order_ref=customer_order_ref,
                  ev=ev, row_body=row_body, book_body=book_body, observed_wall_ns=observed_wall_ns,
                  observed_monotonic_ns=observed_monotonic_ns)
    if ev is None:
        return decided(decision_record(contract, outcome="SKIP", code="evidence_invalid", predicates=None,
                                       day_limit=day_limit, **common))
    predicates = evaluate_predicates(contract, ev, observed_wall_ns=observed_wall_ns)
    if predicates["outcome"] == "SKIP":
        return decided(decision_record(contract, outcome="SKIP", code=predicates["code"], predicates=predicates,
                                       day_limit=day_limit, **common))
    capacity = capacity_and_pause(contract, state, utc_day=day, session_id=session_id, source_seq=reservation_seq)
    if capacity["code"] is not None:
        return decided(decision_record(contract, outcome="SKIP", code=capacity["code"], predicates=predicates,
                                       day_limit=capacity["day_limit"], **common), capacity["stops"])
    return decided(decision_record(contract, outcome="PLACE", code=None, predicates=predicates,
                                   day_limit=capacity["day_limit"], lifetime_slot=capacity["lifetime_slot"],
                                   day_count=capacity["day_count"], **common))


# ---------------------------------------------------------------------------------------------------------------
# The fold (note §4, §6, §10, §11, §15).

@dataclass(frozen=True)
class RecordState:
    """One PLACE record as folded: its decision payload, the evidence the journal holds for it, and what the fold
    derived (frozen at its first final computation by seq)."""
    ref: str
    market_id: str
    selection_id: int
    reservation_seq: int
    decision_seq: int
    open_id: str
    session_id: str
    record: dict
    pt4: bool
    utc_day: str
    off_ns: int
    deadline_ns: int
    decision_wall_ns: int
    submit_seq: object
    failed_not_sent: bool
    unsent: bool
    violations: tuple
    place_report: object
    c_reads: tuple
    l_rows: tuple
    execution: dict
    bsp: dict
    measurement: dict
    ledger: dict
    contradicted_seq: object


@dataclass(frozen=True)
class PilotState:
    """The folded pilot state. Plain values only; equal folds of equal journals compare equal."""
    contract: Contract
    now_ns: int
    lineage: frozenset
    reservations: dict
    place_records: tuple
    skip_records: tuple
    lifetime_used: int
    day_used: dict
    sessions: tuple
    pt4_receipt_seq: object
    l_verified_seq: object
    l_fields_refuted: bool
    profit_basis: object
    commission_verified: bool
    equity_p: int
    pause_fired: dict
    pause_acked: dict
    stops: tuple
    due_stops: tuple
    active_stops: tuple
    violations: tuple
    invalid_records: tuple
    resolved_gaps: dict
    ending_notes: tuple
    ending: object


class _Rec:
    """Mutable working state of one PLACE record inside one fold; never returned (frozen as RecordState)."""

    def __init__(self, contract, record, seq, ns, pt4, index):
        self.record, self.decision_seq, self.decision_ns, self.pt4, self.index = record, seq, ns, pt4, index
        self.ref, self.market_id = record["customer_order_ref"], record["market_id"]
        self.selection_id, self.reservation_seq = record["selection_id"], record["reservation_seq"]
        self.open_id, self.session_id = record["open_id"], record["session_id"]
        self.active = frozenset(record["active_selection_ids"])
        self.off_ns = record["off_ns"]
        self.deadline_ns = self.off_ns + contract.deadline_s * NS
        self.cutoff_ns = self.off_ns + contract.bsp_cutoff_s * NS
        self.decision_wall_ns = record["decision_book_wall_ns"]
        self.triggers = sorted({self.cutoff_ns + 1, self.deadline_ns + 1})
        self.violations = []                 # (code, seq, ns)
        self.intent_seq = self.submit_seq = self.submit_ns = None
        self.failed_seq = self.failed_ns = None
        self.sent_seq = None                 # the first evidence that the order reached Betfair
        self.open_ended_ns = None
        self.bet_id = None
        self.place_report = None             # {"seq", "matched_p", "status"}; matched_p None means unusable
        self.c_reads, self.l_rows = [], []
        self.evidence = []                   # (kind, seq, ns, item): every execution observation, never dropped
        self.execution = self.bsp = None     # frozen once final / decided
        self.measurement = {"class": None, "final": False, "final_at_ns": None}
        self.basis = None
        self.contradicted_seq = None
        self.value_p = -contract.size_p
        self.ledger = {"part": "W", "value_p": self.value_p, "basis": None, "reason": "worst_case"}

    @property
    def unsent(self):
        """Definitely unsent: FAILED(transport_not_sent), or no submit and its open has ended, and no evidence
        that the order reached Betfair."""
        if self.sent_seq is not None:
            return False
        return self.failed_seq is not None or (self.submit_seq is None and self.open_ended_ns is not None)

    def report_matched_p(self):
        return None if self.place_report is None else self.place_report["matched_p"]


class _Folder:
    """One pass over the journal in seq order. Before each event, every time trigger it crosses (a BSP cutoff or a
    7-day deadline) is evaluated at its own time; then the records the event names are re-evaluated at the
    journal's time (the running maximum of recorded_at). So a record freezes at its first final computation, a
    deadline-final record is decided on evidence recorded by its deadline only, and the pause is checked after
    every ledger change (note §10, §11)."""

    def __init__(self, contract, live_open_id):
        self.contract, self.live_open_id = contract, live_open_id
        self.clock = self.last_seq = self.next_trigger = None
        self.registrations = {}
        self.sessions, self.session_days = [], set()
        self.reservations, self.reservation_by_seq = {}, {}
        self.opens, self.ended_opens = [], {}
        self.records, self.by_ref, self.by_market = [], {}, {}
        self.skips = []
        self.pilot_refs = {}
        self.lifetime_used, self.day_used = 0, {}
        self.books, self.commissions, self.settled = {}, {}, {}
        self.pt4_receipt = None
        self.l_verified = None               # (seq, ns) of the first receipt verifying the L fields
        self.l_fields_refuted = False
        self.profit_bases = []
        self.verified_commission = []        # (seq, market_id) of commission_verified notes that match
        self.stops, self.acks = [], []
        self.implied = {}                    # (stop_id, scope) -> stop payload implied by the journal
        self.pause_fired, self.pause_acked = {}, {}
        self.violations, self.invalid, self.ending_notes = [], [], []
        self.halts = []                      # (seq, market_id or None) of HALTs in force, as the core fold keeps them
        self.n_done = None                   # {"seq", "trigger_ns"}: where in the journal N was reached
        self.newly_final = False
        self.resolved_gaps = {}              # gap stop id -> seq of its resolution (Astra #6)
        self.at_seq = self.at_ns = None      # where in the journal the fold is: the event, or the trigger before it
        self.equity = 0

    # -- helpers -------------------------------------------------------------------------------------------------

    def violation(self, code, seq, ns, rec=None, ref=None):
        self.violations.append({"code": code, "seq": seq, "ns": ns, "ref": rec.ref if rec else ref})
        if rec is not None:
            rec.violations.append((code, seq, ns))

    def violation_once(self, code, seq, ns, rec):
        if code not in {found for found, _, _ in rec.violations}:
            self.violation(code, seq, ns, rec)

    def imply(self, stop):
        key = (stop["stop_id"], stop["scope"])
        if key not in self.implied:
            self.implied[key] = stop
            self.terminate(self.at_seq, self.at_ns, stop["reason"])

    def terminate(self, seq, ns, reason):
        """The session in progress ends for good at its first KILL, HALT or stop, journaled or implied (note §3;
        Astra #1); a later RESUME or ACK never re-opens it: placing needs a new ARM."""
        session = self.sessions[-1] if self.sessions else None
        if (session is not None and session["ended_seq"] is None and seq is not None and session["seq"] < seq
                and ns is not None and ns < session_end_ns(session)):
            session["ended_seq"], session["ended_reason"] = seq, reason

    def imply_review(self, reason, source_seq):
        self.imply(stop_payload(self.contract, stop_id_for(reason, source_seq), "review", reason,
                                source_seq=source_seq))

    def invalid_note(self, seq):
        self.invalid.append(seq)
        self.imply_review("pilot_record_invalid", seq)

    def session(self, session_id):
        return next((s for s in self.sessions if s["session_id"] == session_id), None)

    def acked_after(self, stop_id, seq):
        return next((at for at, acked in self.acks if acked == stop_id and at > seq), None)

    def journaled_active_stops(self, session_id=None):
        """Unacknowledged journaled review stops, and session stops journaled in the session (the latest one if
        none is named)."""
        current = self.session(session_id) if session_id else (self.sessions[-1] if self.sessions else None)
        active = []
        for stop in self.stops:
            if stop["scope"] == "review" and self.acked_after(stop["stop_id"], stop["seq"]) is None:
                active.append(dict(stop, acked_seq=None))
            elif stop["scope"] == "session" and current is not None and stop["seq"] > current["seq"]:
                active.append(dict(stop, acked_seq=None))
        return active

    def active_stops(self, session_id=None):
        """The journaled active stops, and the stops the journal implies that are not journaled yet."""
        return self.journaled_active_stops(session_id) + [dict(stop, seq=None, acked_seq=None)
                                                          for stop in self.due_stops()]

    def due_stops(self):
        """Stop payloads the journal implies and that are not journaled yet; the gateway journals them at once."""
        journaled = {(s["stop_id"], s["scope"]) for s in self.stops}
        due = [dict(stop) for key, stop in self.implied.items()
               if key not in journaled and stop["level_p"] not in self.pause_acked]
        return sorted(due, key=lambda s: (s["source_seq"] or 0, s["stop_id"], s["scope"]))

    def session_armed(self, ns):
        """Whether a session is armed at ns: the latest session, before its end and not terminated."""
        session = self.sessions[-1] if self.sessions else None
        return session is not None and ns < session_end_ns(session) and session["ended_seq"] is None

    def halted(self, market_id):
        return any(market is None or market == market_id for _, market in self.halts)

    def end_opens(self, keep, ns):
        """Every open but keep has ended (one placement open at a time, note §4): its records with no submit are
        definitely unsent and its reservations with no record are interrupted."""
        affected = set()
        for open_id in self.opens:
            if open_id != keep and open_id not in self.ended_opens:
                self.ended_opens[open_id] = ns
                for rec in self.records:
                    if rec.open_id == open_id and rec.open_ended_ns is None:
                        rec.open_ended_ns = ns
                        affected.add(rec.index)
        return affected

    def advance(self, until_ns, label):
        """Evaluate every time trigger due by until_ns at its own time, earliest first, before the event that
        crossed it (findings 3, 7, 10)."""
        while self.next_trigger is not None and self.next_trigger <= until_ns:
            at, due = self.next_trigger, set()
            self.at_seq, self.at_ns = label, at
            for rec in self.records:
                while rec.triggers and rec.triggers[0] <= at:
                    rec.triggers.pop(0)
                    due.add(rec.index)
            for index in sorted(due):
                self.evaluate(self.records[index], label, at)
            self.pause_check(label)
            self.n_check(label, at)
            self.next_trigger = min((rec.triggers[0] for rec in self.records if rec.triggers), default=None)

    def n_check(self, boundary, trigger_ns):
        """Record where N was first reached: after journal event `boundary`, and, when a time trigger completed it,
        at that trigger's time (finding 22: journal order, never a capture's recorded_at)."""
        if self.n_done is not None or not self.newly_final:
            return
        self.newly_final = False
        ordered = sorted(self.records, key=lambda rec: (rec.reservation_seq, rec.decision_seq))
        now = trigger_ns if trigger_ns is not None else self.clock
        if _n_walk(ordered, self.contract.n_eligible, now)["reached"]:
            self.n_done = {"seq": boundary, "trigger_ns": trigger_ns}

    # -- events --------------------------------------------------------------------------------------------------

    def apply(self, event):
        seq, ns = event["seq"], event["_ns"]
        clock = ns if self.clock is None else max(self.clock, ns)
        self.advance(clock, self.last_seq)
        self.clock, self.last_seq = clock, seq
        self.at_seq, self.at_ns = seq, ns
        kind, payload = event["kind"], event["payload"]
        if kind == "note" and "pilot" in payload:
            dirty = self.pilot_note(event, seq, ns)
        elif kind == "intent":
            dirty = self.intent(event, seq, ns)
        else:
            dirty = self.core(event, seq, ns)
        for index in sorted(set(dirty or ())):
            self.evaluate(self.records[index], seq, self.clock)
        self.pause_check(seq)
        self.n_check(seq, None)

    def pilot_note(self, event, seq, ns):
        payload = event["payload"]
        try:
            kind = validate_pilot_payload(payload)
            _validate_event_columns(kind, payload["pilot"], event)
        except PilotError:
            self.invalid_note(seq)
            return set()
        inner = payload["pilot"]
        if inner["contract_sha256"] != self.contract.sha256:
            raise PilotError("replacement_contract_not_supported_v1")
        return getattr(self, "note_" + kind)(inner, event, seq, ns)

    def note_registration(self, inner, event, seq, ns):
        if inner["predecessor_contract_sha256"] is not None:
            raise PilotError("replacement_contract_not_supported_v1")
        self.registrations.setdefault(inner["registration_receipt_sha256"], seq)

    def note_session_start(self, inner, event, seq, ns):
        day_start = _day_start_ns(inner["utc_day"])
        if (inner["registration_receipt_sha256"] not in self.registrations or utc_day(ns) != inner["utc_day"]
                or not ns < inner["until_ns"] < day_start + DAY_NS or inner["utc_day"] in self.session_days
                or self.session(inner["session_id"]) is not None or self.l_fields_refuted):
            self.invalid_note(seq)
            return
        self.session_days.add(inner["utc_day"])
        self.sessions.append({"seq": seq, "session_id": inner["session_id"], "utc_day": inner["utc_day"],
                              "until_ns": inner["until_ns"],
                              "registration_receipt_sha256": inner["registration_receipt_sha256"],
                              "pt4": self.pt4_receipt is None, "ended_seq": None, "ended_reason": None})

    def session_problem(self, session_id, ns, seq):
        """The armed-session checks for a reservation or decision at its own append (note §3; critique 15): the
        session exists, its time has not ended, and no KILL, HALT or stop has terminated it (Astra #1)."""
        session = self.session(session_id)
        if session is None:
            return "session_not_armed"
        if ns >= session_end_ns(session):
            return "session_expired"
        if session["ended_seq"] is not None and session["ended_seq"] < seq:
            return "session_stopped"
        return None

    def note_reservation(self, inner, event, seq, ns):
        market, open_id = event["market_id"], inner["open_id"]
        if open_id in self.ended_opens:
            self.violation("reservation_open_ended", seq, ns)
        if open_id not in self.opens:
            self.opens.append(open_id)
        affected = self.end_opens(open_id, ns)
        problem = self.session_problem(inner["session_id"], ns, seq)
        if problem:
            self.violation(problem, seq, ns)
        if market in self.reservations:
            self.violation("second_reservation", seq, ns)
            return affected
        entry = {"seq": seq, "market_id": market, "open_id": open_id, "session_id": inner["session_id"],
                 "decision_seq": None}
        self.reservations[market] = self.reservation_by_seq[seq] = entry
        return affected

    def note_decision(self, inner, event, seq, ns):
        reservation = self.reservation_by_seq.get(inner["reservation_seq"])
        problems = []
        if reservation is None or reservation["market_id"] != inner["market_id"]:
            problems.append("decision_reservation_unknown")
        else:
            if reservation["open_id"] != inner["open_id"]:
                problems.append("decision_other_open")
            if reservation["session_id"] != inner["session_id"]:
                problems.append("decision_other_session")
            if reservation["decision_seq"] is not None:
                problems.append("second_decision")
        if inner["open_id"] in self.ended_opens:
            problems.append("decision_open_ended")
        if inner["open_id"] not in self.opens:
            self.opens.append(inner["open_id"])
        session = self.session(inner["session_id"])
        if session is not None and session["registration_receipt_sha256"] != inner["registration_receipt_sha256"]:
            problems.append("decision_registration_differs")
        problem = self.session_problem(inner["session_id"], ns, seq)    # authority at its own append (Astra #4)
        if problem:
            problems.append(problem)
        if reservation is not None and reservation["decision_seq"] is None and not problems:
            reservation["decision_seq"] = seq
        if inner["outcome"] == "SKIP":
            self.skips.append({"seq": seq, "market_id": inner["market_id"], "code": inner["code"],
                               "reservation_seq": inner["reservation_seq"]})
            for code in problems:
                self.violation(code, seq, ns)
            self.skip_stops(inner, session, seq)
            return set()
        if inner["customer_order_ref"] in self.by_ref:
            problems.append("ref_reused")
        problems += self.place_rule_problems(inner, session, ns)
        rec = _Rec(self.contract, inner, seq, ns, bool(session and session["pt4"]), len(self.records))
        self.records.append(rec)
        self.by_ref.setdefault(rec.ref, rec)
        self.by_market.setdefault(rec.market_id, []).append(rec)
        for code in problems:
            self.violation(code, seq, ns, rec)
        self.lifetime_used += 1
        self.day_used[inner["utc_day"]] = self.day_used.get(inner["utc_day"], 0) + 1
        self.equity += rec.value_p
        if self.next_trigger is None or rec.triggers[0] < self.next_trigger:
            self.next_trigger = rec.triggers[0]
        return {rec.index}

    def skip_stops(self, inner, session, seq):
        """The stops a SKIP decision implies by itself, so they never depend on separate appends (Astra #2):
        SKIP(pause) fires the crossed levels with their session and review stops; SKIP(cap) at the day limit
        sets a session stop. Journaled stop notes with the same ids are idempotent with these."""
        c = self.contract
        if inner["code"] == "pause":
            acked = set(self.pause_acked)
            levels = pause_levels_hit(self.equity + c.new_place_counted_at_p, step_p=c.pause_step_p, acked=acked)
            if not levels:
                # Equity only rises between classify and this append, so the level classify crossed is the
                # highest one not acknowledged.
                k = 1
                while -c.pause_step_p * k in acked:
                    k += 1
                levels = [-c.pause_step_p * k]
            for level in levels:
                self.pause_fired.setdefault(level, seq)
                for stop in pause_stops(c, level):
                    self.imply(stop)
        elif inner["code"] == "cap" and self.lifetime_used < c.lifetime_place_records:
            source = inner["reservation_seq"]
            self.imply(stop_payload(c, stop_id_for("day_limit", source) + "-session", "session", "day_limit",
                                    source_seq=source))

    def place_rule_problems(self, inner, session, ns):
        """A PLACE record the rule does not allow, judged on the record itself and, for placement authority, on
        the journal at its own append (its recorded_at and seq; critique 15, 16; Astra #4): a bet RS-19 does not
        allow is a protocol violation."""
        c = self.contract
        problems = []
        wall, off = inner["decision_book_wall_ns"], inner["off_ns"]
        start = off + c.window_from_offset_s * NS
        if not start <= wall <= start + c.window_length_s * NS:
            problems.append("place_outside_window")
        price = inner["limit_price_h"]
        if not (is_on_ladder(price) and c.best_back_min_h <= price <= c.best_back_max_h):
            problems.append("place_price_not_allowed")
        if inner["send_deadline_ns"] != off + c.send_deadline_offset_s * NS:
            problems.append("place_send_deadline_differs")
        if (inner["stake_p"], inner["side"], inner["order_type"], inner["persistence_type"]) != (
                c.size_p, c.side, c.order_type, c.persistence_type):
            problems.append("place_order_not_allowed")
        if inner["country"] not in c.country_codes:
            problems.append("place_country_not_allowed")
        if inner["off"] is None or _parse_or_none(inner["off"]) != off:
            problems.append("place_off_differs")
        if inner["utc_day"] != utc_day(wall):
            problems.append("place_day_differs")
        if inner["is_market_data_delayed"] is not False:
            problems.append("place_book_delayed")
        if inner["selection_id"] not in inner["active_selection_ids"]:
            problems.append("place_selection_not_active")
        if len(inner["active_selection_ids"]) < c.min_active_runners:
            problems.append("place_too_few_active")
        if self.active_stops(inner["session_id"]):
            problems.append("place_while_stopped")
        if self.ending_notes:
            problems.append("place_after_ending")
        if ns >= c.final_date_ns or wall >= c.final_date_ns:
            problems.append("place_after_final_date")
        if self.n_done is not None:
            problems.append("place_after_n_reached")   # N reached before this append, by seq or by its time
        capacity = _capacity(c, equity_p=self.equity, acked=set(self.pause_acked),
                             lifetime_used=self.lifetime_used, day_used=self.day_used.get(inner["utc_day"], 0),
                             pt4=bool(session and session["pt4"]), slot_seq=None)
        if capacity["code"] == "pause":
            problems.append("place_not_allowed_pause")
        elif capacity["code"] == "cap":
            problems.append("place_beyond_cap")
        else:
            if inner["lifetime_slot"] != capacity["lifetime_slot"]:
                problems.append("place_slot_differs")
            if inner["day_count"] != capacity["day_count"]:
                problems.append("place_day_count_differs")
        if inner["day_limit"] != capacity["day_limit"]:
            problems.append("place_day_limit_differs")
        return problems

    def note_stop(self, inner, event, seq, ns):
        if inner["reason"] == "pause":
            level = inner["level_p"]
            valid = (type(level) is int and level < 0 and level % self.contract.pause_step_p == 0
                     and inner["stop_id"] == pause_stop_ids(level)[0 if inner["scope"] == "review" else 1])
            if not valid:
                self.invalid_note(seq)
                return
            self.pause_fired.setdefault(level, seq)
        self.stops.append({"stop_id": inner["stop_id"], "scope": inner["scope"], "reason": inner["reason"],
                           "level_p": inner["level_p"], "source_seq": inner["source_seq"], "seq": seq})
        self.terminate(seq, ns, inner["reason"])

    def note_ack(self, inner, event, seq, ns):
        """An ACK the rules allow (note §10, §12): a pending review stop; a pause level that fired and is not yet
        acknowledged; any other stop only with its cause written (finding 5)."""
        stop_id = inner["stop_id"]
        pause = _PAUSE_ID_RE.fullmatch(stop_id)
        level = -int(pause.group(1)) if pause else None
        pending = [s for s in self.stops if s["stop_id"] == stop_id and s["scope"] == "review"
                   and self.acked_after(stop_id, s["seq"]) is None]
        if (not pending or (level is not None and (level not in self.pause_fired or level in self.pause_acked))
                or (level is None and inner["cause_written"] is not True)):
            self.invalid_note(seq)
            return
        self.acks.append((seq, stop_id))
        if level is not None:
            self.pause_acked[level] = seq

    def capture_record(self, event, bet_id, row):
        rec = self.by_ref.get(event["customer_order_ref"])
        if rec is None or (rec.bet_id is not None and rec.bet_id != bet_id):
            return None
        if (row.get("customerOrderRef") not in (None, rec.ref) or row.get("marketId") not in (None, rec.market_id)
                or row.get("selectionId") not in (None, rec.selection_id)):
            return None
        rec.bet_id = bet_id
        return rec

    def note_capture_c(self, inner, event, seq, ns):
        row = parse_current_row(inner["row"])
        rec = self.capture_record(event, inner["bet_id"], row)
        if rec is None:
            self.invalid_note(seq)
            return
        rec.c_reads.append({"seq": seq, "recorded_ns": ns, "row": row})
        rec.evidence.append(("c", seq, ns, row))
        self.sending(rec, seq, ns)
        return {rec.index}

    def note_capture_l(self, inner, event, seq, ns):
        row = parse_cleared_row(inner["row"])
        rec = self.capture_record(event, inner["bet_id"], row)
        if rec is None:
            self.invalid_note(seq)
            return
        rec.l_rows.append({"seq": seq, "recorded_ns": ns, "row": row})
        rec.evidence.append(("l", seq, ns, row))
        self.sending(rec, seq, ns)
        return {rec.index}

    def note_capture_book(self, inner, event, seq, ns):
        read = {"seq": seq, "recorded_ns": ns, "status": inner["status"], "bsp_reconciled": inner["bsp_reconciled"],
                "runners": inner["runners"]}
        self.books.setdefault(event["market_id"], []).append(read)
        dirty = set()
        for rec in self.by_market.get(event["market_id"], []):
            rec.evidence.append(("book", seq, ns, read))
            dirty.add(rec.index)
        return dirty

    def note_capture_commission(self, inner, event, seq, ns):
        self.commissions.setdefault(event["market_id"], []).append({"seq": seq, "commission": inner["commission"]})
        dirty = set()
        for rec in self.by_market.get(event["market_id"], []):
            if inner["commission"] is not None:
                rec.evidence.append(("commission", seq, ns, inner["commission"]))
            dirty.add(rec.index)
        return dirty

    def note_commission_verified(self, inner, event, seq, ns):
        if inner["our_commission_p"] != inner["website_commission_p"]:
            self.imply_review("commission_mismatch", seq)
            return
        self.verified_commission.append((seq, inner["market_id"]))
        return {rec.index for rec in self.records}

    def note_pt_receipt(self, inner, event, seq, ns):
        if inner["kind"] == "PT4" and inner["matched_settled_bets"] >= 1 and self.pt4_receipt is None:
            self.pt4_receipt = {"seq": seq, "registration_receipt_sha256": inner["registration_receipt_sha256"]}
        if inner["l_fields_verified"] is False:
            self.l_fields_refuted = True
        elif self.l_verified is None and not self.l_fields_refuted:
            self.l_verified = (seq, ns)
        if inner["profit_basis"] is not None:
            self.profit_bases.append((seq, inner["profit_basis"]))
        return {rec.index for rec in self.records}

    def note_ending(self, inner, event, seq, ns):
        if inner["reason"] == "breach":
            self.violation(inner["code"], seq, ns)
        else:
            self.ending_notes.append({"reason": inner["reason"], "code": inner["code"], "seq": seq, "ns": ns})

    def note_refusal(self, inner, event, seq, ns):
        """A journaled classify refusal implies the review stop classify returned with it (Astra #2)."""
        if inner["code"] != "placing_ended":
            source = inner["reservation_seq"]
            self.imply(stop_payload(self.contract, stop_id_for(inner["code"], source), "review", inner["code"],
                                    source_seq=source))

    def gap_stop(self, stop_id):
        """The review stop of a ledger gap with this id, journaled or implied, or None."""
        stops = [s for s in self.stops if s["stop_id"] == stop_id and s["scope"] == "review"]
        stops += [s for (sid, scope), s in self.implied.items() if sid == stop_id and scope == "review"]
        return next((s for s in stops if s["reason"] in LEDGER_GAP_REASONS), None)

    def note_gap_resolved(self, inner, event, seq, ns):
        """An explicit resolution of one ledger gap, with its cause written (Astra #6): it names a known gap stop
        and, for a record's gap, that record's decision seq."""
        stop = self.gap_stop(inner["stop_id"])
        if (stop is None or inner["cause_written"] is not True
                or (inner["decision_seq"] is not None and inner["decision_seq"] != stop["source_seq"])):
            self.invalid_note(seq)
            return
        self.resolved_gaps.setdefault(inner["stop_id"], seq)

    def note_finalized(self, inner, event, seq, ns):
        """The durable finalization frontier (Astra #7): from here the record's execution record and class are
        frozen as the journal before this note gives them, and later evidence is only logged. A record that is
        not final here, or whose row or class differs from the note, gets the review stop finalized_mismatch."""
        rec = self.by_ref.get(event["customer_order_ref"])
        if rec is None or inner["final_at_ns"] > ns:
            self.invalid_note(seq)
            return
        self.evaluate(rec, seq, self.clock)
        execution, measurement = rec.execution, rec.measurement
        if (execution is None or not measurement["final"] or execution["row"] != inner["row"]
                or measurement["class"] != inner["class"]):
            self.imply_review("finalized_mismatch", seq)
        return {rec.index}

    def note_violation(self, inner, event, seq, ns):
        rec = next((r for r in self.records if r.decision_seq == inner["source_seq"]), None)
        self.violation(inner["code"], seq, ns, rec)
        return {rec.index} if rec else set()

    def intent(self, event, seq, ns):
        """Only an intent with source "pilot" is a pilot send (finding 19). Any other intent while a session is
        armed sets a review stop: RS-19 halts on a manual bet during a session; it does not end the pilot."""
        payload, ref = event["payload"], event.get("customer_order_ref")
        if payload.get("source") != self.contract.intent_source:
            if self.session_armed(ns):
                self.imply_review("manual_order_during_session", seq)
            return set()
        self.pilot_refs.setdefault(ref, seq)
        rec = self.by_ref.get(ref)
        if rec is None:
            self.violation("send_without_record", seq, ns, ref=ref)
            return set()
        if rec.intent_seq is not None:
            self.violation("second_intent", seq, ns, rec)
            return {rec.index}
        rec.intent_seq = seq
        code = check_binding(rec.record, contract=self.contract, market_id=event.get("market_id"),
                             instruction=payload.get("order"), customer_order_ref=ref,
                             customer_strategy_ref=payload.get("customer_strategy_ref"), open_id=rec.open_id,
                             session_id=rec.session_id, record_digest=record_sha256(rec.record))
        if code:
            self.violation(code, seq, ns, rec)
        if rec.open_id in self.ended_opens:
            self.violation("intent_after_open_ended", seq, ns, rec)
        return {rec.index}

    def core(self, event, seq, ns):
        """Core events: pilot place submits, place and cancel reports, every transition carrying a bet id or a
        matched size (reconcile and startup moves too), FAILED(transport_not_sent), settle, and HALT, kill and
        resume as the core fold applies them."""
        kind, payload = event["kind"], event["payload"]
        dirty = set()
        self.track_halts(kind, payload, event, seq)
        if kind == "settle" and isinstance(event.get("market_id"), str):
            self.settled.setdefault(event["market_id"], seq)
            dirty |= {rec.index for rec in self.by_market.get(event["market_id"], [])}
        moves = payload.get("transitions") if isinstance(payload.get("transitions"), list) else []
        moves = [move for move in moves if isinstance(move, dict)]
        if kind == "submit" and payload.get("action") == "place":
            for ref in sorted({m.get("customer_order_ref") for m in moves if isinstance(m.get("customer_order_ref"),
                                                                                         str)}):
                if ref in self.pilot_refs or ref in self.by_ref:
                    dirty |= self.place_submit(event, ref, seq, ns)
        report = payload.get("report") if kind == "report" and isinstance(payload.get("report"), dict) else None
        items = report.get("instructions") if report is not None else None
        items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if report is not None and payload.get("action") == "place":
            for item in items:
                rec = self.by_ref.get(item.get("customer_order_ref"))
                if rec is None:
                    continue
                matched = item.get("size_matched_p") if _is_int(item.get("size_matched_p"), 0) else None
                if rec.bet_id is None and isinstance(item.get("bet_id"), str):
                    rec.bet_id = item["bet_id"]
                if rec.place_report is None:
                    rec.place_report = {"seq": seq, "matched_p": matched, "status": item.get("status")}
                if matched is not None:
                    rec.evidence.append(("report", seq, ns, matched))
                self.sending(rec, seq, ns)
                dirty.add(rec.index)
        if report is not None and payload.get("action") == "cancel":
            for item in items:
                rec = next((r for r in self.records if r.bet_id is not None and r.bet_id == item.get("bet_id")), None)
                if rec is not None and _is_int(item.get("size_cancelled_p"), 0):
                    rec.evidence.append(("cancel", seq, ns, self.contract.size_p - item["size_cancelled_p"]))
                    dirty.add(rec.index)
        for move in moves:
            rec = self.by_ref.get(move.get("customer_order_ref"))
            if rec is None:
                continue
            fields = move.get("fields") if isinstance(move.get("fields"), dict) else {}
            if move.get("state") == "FAILED" and fields.get("error_code") == "transport_not_sent" \
                    and rec.failed_seq is None:
                rec.failed_seq, rec.failed_ns = seq, ns
            has_bet = isinstance(fields.get("bet_id"), str)
            if rec.bet_id is None and has_bet:
                rec.bet_id = fields["bet_id"]
            if _is_int(fields.get("size_matched_p"), 0):
                rec.evidence.append(("report", seq, ns, fields["size_matched_p"]))
            if has_bet or _is_int(fields.get("size_matched_p"), 0):
                self.sending(rec, seq, ns)
            dirty.add(rec.index)
        return dirty

    def track_halts(self, kind, payload, event, seq):
        """HALT, kill and resume as journal.py's fold applies them (journal.py:791-829)."""
        if kind in ("report", "risk_decision") and payload.get("halts"):
            self.halts.append((seq, None))
            self.terminate(seq, event["_ns"], "halt")
        if kind == "halt":
            market = event.get("market_id") if payload.get("scope") == "market" else None
            self.halts.append((seq, market))
            self.terminate(seq, event["_ns"], "halt")
        elif kind == "kill":
            self.halts.append((seq, None))
            self.terminate(seq, event["_ns"], "kill")
        elif kind == "resume":
            scope = payload.get("scope")
            if scope == "market" and event.get("market_id") is not None:
                self.halts = [(s, m) for s, m in self.halts if m != event["market_id"]]
            elif scope == "global" and type(payload.get("cleared_through_seq")) is int:
                bound = payload["cleared_through_seq"]
                self.halts = [(s, m) for s, m in self.halts if m is not None or s > bound]
            elif scope == "global":
                self.halts = [(s, m) for s, m in self.halts if m is not None]

    def place_submit(self, event, ref, seq, ns):
        """A pilot place submit. A journaled stop not acknowledged, a HALT or kill in force, or an ending already
        journaled is visible to the risk decision the submit is compare-and-appended on, so a submit after one is a
        violation at once (finding 18); the session end, the final date and the send deadline are judged when
        evidence of sending arrives, since the first-byte gate turns such a submit into FAILED(transport_not_sent)."""
        payload = event["payload"]
        rec = self.by_ref.get(ref)
        if rec is None:
            self.violation("send_without_record", seq, ns, ref=ref)
            return set()
        if rec.submit_seq is None:
            rec.submit_seq, rec.submit_ns = seq, ns
        else:
            self.violation("second_submit", seq, ns, rec)
        if rec.intent_seq is None:
            self.violation("submit_without_intent", seq, ns, rec)
        if payload.get("instruction_count") != 1:
            self.violation("more_than_one_instruction", seq, ns, rec)
        params = payload.get("params")
        if isinstance(params, dict) and params.get("customerStrategyRef") != self.contract.customer_strategy_ref:
            self.violation("strategy_ref_differs", seq, ns, rec)
        if rec.open_id in self.ended_opens:
            self.violation("submit_after_open_ended", seq, ns, rec)
        if self.journaled_active_stops(rec.session_id):
            self.violation("submit_while_stopped", seq, ns, rec)
        session = self.session(rec.session_id)
        if session is not None and session["ended_seq"] is not None and session["ended_seq"] < seq:
            self.violation("submit_after_session_end", seq, ns, rec)
        if self.halted(rec.market_id):
            self.violation("submit_while_halted", seq, ns, rec)
        if self.ending_notes:
            self.violation("submit_after_ending", seq, ns, rec)
        if rec.sent_seq is not None:
            self.sending(rec, rec.sent_seq, ns)
        return {rec.index}

    def sending(self, rec, seq, ns):
        """Evidence that the order reached Betfair (a place report, a bet id or matched size in a transition, a C
        or L row): a violation for a record with no submit, after FAILED(transport_not_sent), or whose submit came
        after the send deadline, the session's end or the final date (findings 20, 21)."""
        if rec.sent_seq is None:
            rec.sent_seq = seq
        if rec.submit_seq is None:
            self.violation_once("sent_without_submit", seq, ns, rec)
            return
        if rec.failed_seq is not None and rec.failed_seq < seq:
            self.violation_once("sent_after_not_sent", seq, ns, rec)
        if rec.submit_ns >= rec.record["send_deadline_ns"]:
            self.violation_once("sent_after_deadline", seq, ns, rec)
        session = self.session(rec.session_id)
        if session is not None and rec.submit_ns >= session_end_ns(session):
            self.violation_once("sent_after_session_end", seq, ns, rec)
        if rec.submit_ns >= self.contract.final_date_ns:
            self.violation_once("sent_after_final_date", seq, ns, rec)

    # -- evaluation ----------------------------------------------------------------------------------------------

    def first_closed(self, rec, until_ns=None):
        closed = [read for read in self.books.get(rec.market_id, []) if read["status"] == "CLOSED"
                  and (until_ns is None or read["recorded_ns"] <= until_ns)]
        return min(closed, key=lambda read: read["seq"]) if closed else None

    def void_or_removal(self, rec, until_ns=None):
        """(void, removal) from the first CLOSED read by seq, whatever its time (decision Q14), and the L rows;
        for a deadline-final record, only from evidence recorded by its deadline."""
        closed = self.first_closed(rec, until_ns)
        statuses = {} if closed is None else {key: (r or {}).get("status") for key, r in closed["runners"].items()}
        rows = [read for read in rec.l_rows if until_ns is None or read["recorded_ns"] <= until_ns]
        void = statuses.get(str(rec.selection_id)) in REMOVED or any(
            read["row"].get("betStatus") == "VOIDED" for read in rows)
        removal = any(statuses.get(str(s)) in REMOVED for s in rec.active if s != rec.selection_id) or any(
            read["row"].get("priceReduced") is True for read in rows)
        return void, removal

    def capture_status(self, rec, until_ns=None):
        """[(read, passed)] for every C read showing EXECUTION_COMPLETE, by seq. A read is held against the place
        report (even one journaled after it) and against every matched size our own reports and transitions showed
        before it: Betfair's matched size never falls, so a read showing less is never used (finding 6)."""
        result = []
        for read in rec.c_reads:
            if read["row"].get("status") != "EXECUTION_COMPLETE" or (until_ns is not None
                                                                       and read["recorded_ns"] > until_ns):
                continue
            seen = [item for kind, at, _, item in rec.evidence if kind == "report" and at < read["seq"]]
            if rec.report_matched_p() is not None:
                seen.append(rec.report_matched_p())
            result.append((read, capture_check(read["row"], place_report_matched_p=max(seen) if seen else None,
                                               size_p=self.contract.size_p)))
        return result

    def evaluate(self, rec, seq, now):
        c = self.contract
        was_final = rec.measurement["final"]
        self.contradiction(rec, rec.basis)
        for read, passed in self.capture_status(rec):
            if not passed:
                self.imply_review("capture_check_failed", read["seq"])
        if rec.execution is None:
            self.execution(rec, seq, now)
        bound = rec.deadline_ns if rec.execution is not None and rec.execution["row"] in (3, 5) else None
        void, removal = self.void_or_removal(rec, bound)
        if rec.bsp is None:
            bsp = freeze_bsp(self.books.get(rec.market_id, []), selection_id=rec.selection_id, off_ns=rec.off_ns,
                             now_ns=now, cutoff_s=c.bsp_cutoff_s)
            if bsp["decided"]:
                rec.bsp = bsp
        rec.measurement = self.measurement(rec, void, removal)
        if rec.measurement["final"] and not was_final:
            self.newly_final = True
        self.ledger(rec, void, removal)

    def execution(self, rec, seq, now):
        """The final execution record (note §11), frozen at its first final computation. Through the deadline
        (rows 3 and 5) only evidence recorded by the deadline counts; anything later is logged and never changes
        it (finding 7)."""
        c = self.contract
        settled_l = canonical_l(rec.l_rows)
        l_count = None if settled_l is None else l_counts_at(
            settled_l["recorded_ns"], None if self.l_verified is None else self.l_verified[1])
        l_on_time = l_count is not None and l_count <= rec.deadline_ns and l_count <= now
        bound = rec.deadline_ns if now > rec.deadline_ns and not l_on_time else None
        checks = self.capture_status(rec, bound)
        void, removal = self.void_or_removal(rec, bound)
        on_time = [read for read, _ in checks if read["recorded_ns"] <= rec.deadline_ns]
        canon = on_time[0] if on_time else None
        table = finalize(None if canon is None else canon["row"], None if settled_l is None else settled_l["row"],
                         c_ns=None if canon is None else canon["recorded_ns"], l_count_ns=l_count,
                         deadline_ns=rec.deadline_ns, now_ns=now, removal_classed=void or removal)
        if not table["final"]:
            return
        size, price, consistency_failed = table["size"], table["P"], False
        if not all(passed for _, passed in checks):
            size = price = None                          # decision Q10: a failed capture check before final
        elif size is not None and not consistency_check(
                size, place_report_matched_p=rec.report_matched_p(), c=None if canon is None else canon["row"],
                size_p=c.size_p):
            size = price = None
            consistency_failed = True
            self.imply_review("consistency_failed", rec.decision_seq)
        rec.execution = dict(table, size=size, P=price,
                             unresolved=size is None or (size > 0 and price is None and not (void or removal)),
                             consistency_failed=consistency_failed, final_seq=seq,
                             c_seq=None if canon is None else canon["seq"],
                             l_seq=None if settled_l is None or table["row"] in (3, 5) else settled_l["seq"])

    def measurement(self, rec, void, removal):
        """The record's class (note §11), frozen at its first final computation; a protocol violation found later,
        or a record found definitely unsent, still takes precedence."""
        if rec.measurement["final"] and not rec.violations and not rec.unsent:
            return rec.measurement
        bsp_at = None if rec.bsp is None else rec.bsp["decided_at_ns"]
        if rec.violations or rec.unsent:
            if rec.violations:
                cls, established = "protocol_violation", rec.violations[0][2]
            else:
                cls = "definitely_unsent"
                established = rec.failed_ns if rec.failed_ns is not None else rec.open_ended_ns
            final = bsp_at is not None
            return {"class": cls, "final": final, "final_at_ns": max(established, bsp_at) if final else None}
        execution = rec.execution
        if execution is None or rec.bsp is None:
            return {"class": None, "final": False, "final_at_ns": None}
        if void:
            cls = "void"
        elif removal:
            cls = "post_decision_removal"
        elif execution["unresolved"]:
            cls = "measurement_unresolved"
        elif execution["size"] == 0:
            cls = "unmatched"
        elif rec.bsp["bsp"] is None:
            cls = "missing_bsp"
        else:
            cls = "eligible"
        return {"class": cls, "final": True, "final_at_ns": max(execution["final_at_ns"], bsp_at)}

    def ledger_basis(self, rec, void, removal):
        """(part, value_p, basis, reason, stops) by the W and S rules, before contradiction (note §10; K:112). A
        basis names its source observation: every execution item journaled after it is compared with it."""
        if rec.unsent:
            return "W", 0, {"rule": "b", "size": Decimal(0), "source": 0}, "not_sent", []
        execution = rec.execution
        if execution is not None and execution["size"] is not None:
            in_s, reason, stops, value, extra = self.s_check(rec, void, removal)
            source = execution["c_seq"] if execution["c_seq"] is not None else execution["l_seq"]
            basis = {"rule": "c", "size": execution["size"], "source": source}
            if in_s:
                return "S", value, dict(basis, rule="s", **extra), None, stops
            return "W", loss_pence(execution["size"]), basis, reason, stops
        checks = self.capture_status(rec)
        if (execution is None and rec.report_matched_p() is not None and checks
                and all(passed for _, passed in checks)):
            read = checks[0][0]
            return ("W", loss_pence(read["row"]["sizeMatched"]),
                    {"rule": "d", "size": read["row"]["sizeMatched"], "source": read["seq"]}, "checked_current_read",
                    [])
        return "W", -self.contract.size_p, None, "worst_case", []

    def s_check(self, rec, void, removal):
        """(in S, W reason, stops, value, S basis) for a final record with a resolved size (note §10). The
        cleared-figure and commission gap checks run from the first bet, PT4 included, whenever the figures can be
        compared like for like; settlement, the commission method and the PT4 receipt gate S itself."""
        c = self.contract
        execution = rec.execution
        size, price = execution["size"], execution["P"]
        closed = self.first_closed(rec)
        if void:
            return False, "void", [], None, {}
        if removal:
            return False, "removal", [], None, {}
        if closed is None:
            return False, "result_unknown", [], None, {}
        statuses = [(r or {}).get("status") for r in closed["runners"].values()]
        ours = (closed["runners"].get(str(rec.selection_id)) or {}).get("status")
        if statuses.count("WINNER") != 1:
            return False, "dead_heat_or_no_winner", [], None, {}
        if ours not in RESULTS:
            return False, "result_unknown", [], None, {}
        if size > 0 and price is None:
            return False, "price_unknown", [], None, {}
        settled_l = canonical_l(rec.l_rows)
        if execution["l_seq"] is None or settled_l is None:
            return False, "profit_missing", [], None, {}
        if self.profit_basis() != "gross":
            return False, "profit_basis_unverified", [], None, {}
        bf = _pence_or_none(settled_l["row"].get("profit"))
        if bf is None and settled_l["row"].get("profit") is None:
            return False, "profit_missing", [], None, {}
        if ours == "WINNER":
            figures = s_winner_value(size, price, 0, c=c.commission_rate)
            gross, commission = figures["gross_p"], figures["commission_p"]
        else:
            gross, commission = loss_pence(size), 0
        if bf is None or abs(gross - bf) > c.gap_tolerance_p:
            self.gap(rec, "cleared_figure_gap", [_pence_or_none(r["row"].get("profit")) for r in rec.l_rows
                                                 if r["row"].get("betStatus") == "SETTLED"], gross)
            return False, "cleared_figure_gap", [], None, {}
        value = min(gross - commission, bf - commission) if ours == "WINNER" else gross
        captured = [_pence_or_none(x["commission"]) for x in self.commissions.get(rec.market_id, [])
                    if x["commission"] is not None]
        if captured:
            # Every API commission capture is held against ours, never just the first (Astra #3).
            if any(p is None or abs(commission - p) > c.gap_tolerance_p for p in captured):
                self.gap(rec, "commission_gap", captured, commission)
                return False, "commission_gap", [], None, {}
        elif not self.commission_method_verified():
            return False, "commission_unverified", [], None, {}
        if rec.market_id not in self.settled:
            return False, "not_settled", [], None, {}
        if self.pt4_receipt is None:
            return False, "no_pt4_receipt", [], None, {}
        row = settled_l["row"]
        return True, None, [], value, {"P": price, "profit": row.get("profit"), "result": ours,
                                       "outcome": row.get("betOutcome"), "commission_p": commission,
                                       "betfair_commission_p": captured[0] if captured else None}

    def gap(self, rec, reason, figures, ours):
        """Set a ledger gap's review stop; it is resolved when the latest captured figure agrees with ours within
        the tolerance (Astra #6). Resolution clears the gap for an ACK; it never moves the record into S."""
        stop_id = stop_id_for(reason, rec.decision_seq)
        self.imply_review(reason, rec.decision_seq)
        if len(figures) > 1 and figures[-1] is not None and abs(figures[-1] - ours) <= self.contract.gap_tolerance_p:
            self.resolved_gaps.setdefault(stop_id, self.at_seq)

    def profit_basis(self):
        """The sign and basis of Betfair's profit as the PT receipts recorded it; None until one does, or when
        they disagree."""
        bases = {basis for _, basis in self.profit_bases}
        return bases.pop() if len(bases) == 1 else None

    def commission_method_verified(self):
        """A commission_verified note that matched to the penny and names a pilot market whose first CLOSED read
        shows our runner WINNER (decision Q13); it validates the method from then on."""
        for _, market in self.verified_commission:
            for rec in self.by_market.get(market, []):
                closed = self.first_closed(rec)
                if closed and (closed["runners"].get(str(rec.selection_id)) or {}).get("status") == "WINNER":
                    return True
        return False

    def contradiction(self, rec, basis):
        """Every execution item journaled after the basis's own source observation that shows a different matched
        size, a larger one in a report, or for S a different price (a later EXECUTION_COMPLETE C or L), profit,
        result or API commission (more than 1p from the one S used or from ours), moves the record to W at -200
        for good and sets a review stop (note §10; decision Q12; finding 6; Astra #3). Only a size basis is
        contradicted (critique 11)."""
        if rec.contradicted_seq is not None or basis is None:
            return
        basis_p = Fraction(basis["size"]) * 100
        for kind, at, _, item in rec.evidence:
            if at <= basis["source"]:
                continue
            if kind == "c":
                found = item.get("sizeMatched") is None or item["sizeMatched"] != basis["size"]
                if basis["rule"] == "s" and item.get("status") == "EXECUTION_COMPLETE":
                    found = found or item.get("averagePriceMatched") != basis["P"]       # Astra #3
            elif kind == "commission":
                captured = _pence_or_none(item)
                found = basis["rule"] == "s" and (
                    captured is None or abs(captured - basis["commission_p"]) > self.contract.gap_tolerance_p
                    or (basis["betfair_commission_p"] is not None
                        and abs(captured - basis["betfair_commission_p"]) > self.contract.gap_tolerance_p))
            elif kind == "l":
                found = item.get("sizeSettled") is None or item["sizeSettled"] != basis["size"]
                if basis["rule"] == "s" and item.get("betStatus") == "SETTLED":
                    found = (found or item.get("priceMatched") != basis["P"]
                             or _decimal_text_value(item.get("profit")) != _decimal_text_value(basis["profit"])
                             or item.get("betOutcome") != basis["outcome"])
            elif kind in ("report", "cancel"):
                found = item > basis_p
            else:                                        # a book read
                runners = item["runners"] if item["status"] == "CLOSED" else None
                found = basis["rule"] == "s" and runners is not None and (
                    (runners.get(str(rec.selection_id)) or {}).get("status") != basis["result"]
                    or [(r or {}).get("status") for r in runners.values()].count("WINNER") != 1)
            if found:
                rec.contradicted_seq = at
                self.imply_review("ledger_contradiction", rec.decision_seq)
                return

    def ledger(self, rec, void, removal):
        part, value, basis, reason, stops = self.ledger_basis(rec, void, removal)
        for stop_reason, source in stops:
            self.imply_review(stop_reason, source)
        self.contradiction(rec, basis)                   # a new or changed basis meets everything after its source
        if rec.contradicted_seq is not None:
            part, value, basis, reason = "W", -self.contract.size_p, None, "contradiction"
        if part == "W":
            value = min(value, 0)                        # W is minus a stake, never a gain (finding 4)
        rec.basis = basis
        rec.ledger = {"part": part, "value_p": value, "basis": basis, "reason": reason}
        self.equity += value - rec.value_p
        rec.value_p = value

    def pause_check(self, seq):
        for level in pause_levels_hit(self.equity, step_p=self.contract.pause_step_p, acked=set(self.pause_acked)):
            if level not in self.pause_fired:
                self.pause_fired[level] = seq
                for stop in pause_stops(self.contract, level):
                    self.imply(stop)

    # -- finish --------------------------------------------------------------------------------------------------

    def finish(self, now_ns):
        """Close the opens that are not live, evaluate every time trigger up to the fold's now at its own time, then
        every record at now."""
        now = now_ns if self.clock is None else max(self.clock, now_ns)
        self.at_seq, self.at_ns = self.last_seq, self.clock
        for index in sorted(self.end_opens(self.live_open_id, now if self.clock is None else self.clock)):
            self.evaluate(self.records[index], self.last_seq, self.clock)
        self.advance(now, self.last_seq)
        self.at_seq, self.at_ns = self.last_seq, now
        for rec in self.records:
            self.evaluate(rec, self.last_seq, now)
        self.pause_check(self.last_seq)
        self.n_check(self.last_seq, None)
        return now


def _parse_or_none(text):
    try:
        return parse_utc_ns(text)
    except PilotError:
        return None


def _check_events(events):
    if not isinstance(events, list):
        raise PilotError("events_invalid")
    checked = []
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict) or type(event.get("seq")) is not int:
            raise PilotError("events_invalid")
        if event["seq"] != index:
            raise PilotError("events_not_in_seq_order")
        if event.get("mode") != "live":
            raise PilotError("event_mode_not_live")
        if not isinstance(event.get("kind"), str) or not isinstance(event.get("payload"), dict):
            raise PilotError("events_invalid")
        ns = _parse_or_none(event.get("recorded_at"))
        if ns is None:
            raise PilotError("events_invalid")
        checked.append(dict(event, _ns=ns))
    return checked


_NOT_FINAL_EXECUTION = {"final": False, "final_at_ns": None, "row": None, "size": None, "P": None,
                        "unresolved": None}
_NOT_DECIDED_BSP = {"decided": False, "decided_at_ns": None, "bsp": None, "read_seq": None, "closed_read": None}


def _freeze_record(rec):
    return RecordState(
        ref=rec.ref, market_id=rec.market_id, selection_id=rec.selection_id, reservation_seq=rec.reservation_seq,
        decision_seq=rec.decision_seq, open_id=rec.open_id, session_id=rec.session_id, record=rec.record,
        pt4=rec.pt4, utc_day=rec.record["utc_day"], off_ns=rec.off_ns, deadline_ns=rec.deadline_ns,
        decision_wall_ns=rec.decision_wall_ns, submit_seq=rec.submit_seq, failed_not_sent=rec.failed_seq is not None,
        unsent=rec.unsent, violations=tuple(code for code, _, _ in rec.violations),
        place_report=None if rec.place_report is None else dict(rec.place_report),
        c_reads=tuple(rec.c_reads), l_rows=tuple(rec.l_rows),
        execution=dict(rec.execution) if rec.execution else dict(_NOT_FINAL_EXECUTION),
        bsp=dict(rec.bsp) if rec.bsp else dict(_NOT_DECIDED_BSP), measurement=dict(rec.measurement),
        ledger=dict(rec.ledger), contradicted_seq=rec.contradicted_seq)


def fold(events, *, contract, now_ns, live_open_id=None):
    """The pilot state from the journal's events (OrderJournal.events() dicts: seq gap-free from 1, mode live, the
    columns and the parsed payload), in seq order only, never by recorded_at. live_open_id names the placement
    open in progress, if any: a reservation of any other open with no record is SKIP(interrupted), and a PLACE
    record of any other open with no submit is definitely unsent. Deterministic: re-folding the same events at
    the same now gives an equal state. More than one contract sha256 is refused in v1 (decision Q24)."""
    if not isinstance(contract, Contract) or not _is_ns(now_ns):
        raise PilotError("fold_input_invalid")
    folder = _Folder(contract, live_open_id)
    for event in _check_events(events):
        folder.apply(event)
    now = folder.finish(now_ns)
    reservations = {market: dict(entry, interrupted=entry["decision_seq"] is None and entry["open_id"] in
                                 folder.ended_opens)
                    for market, entry in folder.reservations.items()}
    records = sorted(folder.records, key=lambda rec: (rec.reservation_seq, rec.decision_seq))
    stops = tuple(dict(s, acked_seq=folder.acked_after(s["stop_id"], s["seq"]) if s["scope"] == "review" else None)
                  for s in folder.stops)
    state = PilotState(
        contract=contract, now_ns=now, lineage=frozenset({contract.sha256}), reservations=reservations,
        place_records=tuple(_freeze_record(rec) for rec in records), skip_records=tuple(folder.skips),
        lifetime_used=folder.lifetime_used, day_used=dict(folder.day_used), sessions=tuple(folder.sessions),
        pt4_receipt_seq=None if folder.pt4_receipt is None else folder.pt4_receipt["seq"],
        l_verified_seq=None if folder.l_verified is None else folder.l_verified[0],
        l_fields_refuted=folder.l_fields_refuted, profit_basis=folder.profit_basis(),
        commission_verified=folder.commission_method_verified(), equity_p=folder.equity,
        pause_fired=dict(folder.pause_fired), pause_acked=dict(folder.pause_acked), stops=stops,
        due_stops=tuple(folder.due_stops()), active_stops=tuple(folder.active_stops()),
        violations=tuple(dict(item) for item in folder.violations), invalid_records=tuple(folder.invalid),
        resolved_gaps=dict(folder.resolved_gaps),
        ending_notes=tuple(folder.ending_notes), ending=None)
    values = {name: getattr(state, name) for name in state.__dataclass_fields__}
    values["ending"] = placing_ending(state, now_ns=now)
    return PilotState(**values)


def equity(state):
    """The sum of one ledger value per PLACE record (K:121): private, for the stops and Yu's risk view (note §12)."""
    return sum(rec.ledger["value_p"] for rec in state.place_records)


def ledger_value(rec):
    """The record's ledger part and value as folded: {"part", "value_p", "basis", "reason"}."""
    return dict(rec.ledger)


def s_eligible(rec):
    """(True, None) when the record is in part S; else (False, the reason it stays in W)."""
    return (True, None) if rec.ledger["part"] == "S" else (False, rec.ledger["reason"])


def measurement_class(rec, *, now_ns):
    """{"class", "final", "final_at_ns"} as folded: final once its time is at or before now_ns, and never
    changing after that. A record not final in the fold is not final here either: fold again at a later now."""
    measurement = rec.measurement
    if measurement["final"] and measurement["final_at_ns"] <= now_ns:
        return dict(measurement)
    return {"class": None, "final": False, "final_at_ns": None}


def ack_check(state, stop_id, *, journal_reconciled, unresolved_order, cause_written):
    """None, or why PILOT ACK of this review stop is refused (note §6, §10, §12; critique 7). journal_reconciled
    and unresolved_order come from the core fold (the caller)."""
    if not isinstance(stop_id, str):
        return "ack_unknown_stop"
    pause = _PAUSE_ID_RE.fullmatch(stop_id)
    if pause:
        amount = int(pause.group(1))
        if amount % state.contract.pause_step_p:
            return "ack_unknown_stop"
        if -amount not in state.pause_fired:
            return "ack_level_not_fired"
    journaled = [s for s in state.stops if s["stop_id"] == stop_id and s["scope"] == "review"]
    if not journaled:
        return "ack_unknown_stop"
    if (pause and -int(pause.group(1)) in state.pause_acked) or all(s["acked_seq"] is not None for s in journaled):
        return "ack_already_acked"
    if not journal_reconciled:
        return "ack_journal_not_reconciled"
    if unresolved_order:
        return "ack_unresolved_order"
    if any(s["scope"] == "review" and s["reason"] in LEDGER_GAP_REASONS and s["stop_id"] not in state.resolved_gaps
           for s in state.active_stops):
        return "ack_ledger_gap_open"                     # a gap, this one included, not yet resolved (Astra #6)
    if not pause and not cause_written:
        return "ack_cause_required"
    return None



# ---------------------------------------------------------------------------------------------------------------
# PT4, the sample, endings and the data lock (K:31-41, K:240-252; note §15).

def pt4_membership(rec):
    """Whether a PLACE record is in the sample. PT4 records are always reported apart (Yu, 2026-10-21): they never
    join the primary sample or the analysis population, whatever sessions or registrations follow; they still
    use their slots, count in the money ledger and the pause, and count as earlier PLACE records for N."""
    return "apart" if rec.pt4 else "sample"


def pt4_partition(state):
    return {"pt4_refs": [rec.ref for rec in state.place_records if rec.pt4],
            "apart": [rec.ref for rec in state.place_records if pt4_membership(rec) == "apart"]}


def _n_walk(records, n_eligible, now_ns):
    counted, at = 0, None
    for index, rec in enumerate(records):
        measurement = measurement_class(rec, now_ns=now_ns)
        if not measurement["final"]:
            break
        at = measurement["final_at_ns"] if at is None else max(at, measurement["final_at_ns"])
        if pt4_membership(rec) == "sample" and measurement["class"] == "eligible":
            counted += 1
            if counted == n_eligible:
                return {"reached": True, "index_400th": index, "eligible_counted": counted, "at_ns": at}
    return {"reached": False, "index_400th": None, "eligible_counted": counted, "at_ns": None}


def n_status(state, *, now_ns):
    """Walk the PLACE records in decision order. A bet counts only once every earlier PLACE record, PT4 records
    included, has its final class (critique 10); eligible bets are counted among sample records only.
    {"reached", "index_400th" (in decision order), "eligible_counted", "at_ns" (when N was reached)}."""
    return _n_walk(state.place_records, state.contract.n_eligible, now_ns)


def placing_ending(state, *, now_ns):
    """The ending in force at now_ns (note §15), or None: a breach (any protocol violation, including a breach
    ending note) ends placing at once; otherwise the earliest of Yu ending it in writing, the lifetime cap, the
    final date (placing only before 2027-06-30T00:00:00Z, decision Q16) and N reached."""
    contract = state.contract
    if state.violations:
        first = min(state.violations, key=lambda v: v["seq"])
        return {"reason": "breach", "seq": first["seq"], "at_ns": first["ns"]}
    found = [{"reason": "yu_written", "seq": note["seq"], "at_ns": note["ns"]} for note in state.ending_notes]
    if state.lifetime_used >= contract.lifetime_place_records:
        last = sorted(state.place_records, key=lambda rec: rec.decision_seq)[contract.lifetime_place_records - 1]
        found.append({"reason": "lifetime_cap", "seq": last.decision_seq, "at_ns": last.decision_wall_ns})
    if now_ns >= contract.final_date_ns:
        found.append({"reason": "final_date", "seq": None, "at_ns": contract.final_date_ns})
    status = n_status(state, now_ns=now_ns)
    if status["reached"]:
        found.append({"reason": "n_reached", "seq": None, "at_ns": status["at_ns"]})
    return min(found, key=lambda e: (e["at_ns"], e["reason"])) if found else None


def analysis_population(state, *, now_ns):
    """{"apart", "population", "surplus", "n_reached"}: with N reached, the sample records up to and including the
    400th eligible; otherwise every sample record. PT4 records are in neither (pt4_membership)."""
    sample = [rec for rec in state.place_records if pt4_membership(rec) == "sample"]
    apart = [rec for rec in state.place_records if pt4_membership(rec) == "apart"]
    status = n_status(state, now_ns=now_ns)
    if not status["reached"]:
        return {"apart": apart, "population": sample, "surplus": [], "n_reached": False}
    cut = state.place_records[status["index_400th"]]
    position = next(i for i, rec in enumerate(sample) if rec is cut)
    return {"apart": apart, "population": sample[:position + 1], "surplus": sample[position + 1:],
            "n_reached": True}


def data_lock_reached(state, *, now_ns):
    """Placing has ended and every PLACE record has its final class (each is final just after off + 7 d at the
    latest); then the frozen analysis runs once (note §12; critique 6)."""
    if placing_ending(state, now_ns=now_ns) is None:
        return False
    return all(measurement_class(rec, now_ns=now_ns)["final"] for rec in state.place_records)


# ---------------------------------------------------------------------------------------------------------------
# Validity gates, CR1 with a stdlib Student t, and the verdict (K:85-100, K:256-279).

def _gate(num, den, *, minimum=None, maximum=None):
    if den == 0:
        return {"num": num, "den": den, "value": None, "pass": None}
    value = Fraction(num, den)
    passed = value >= minimum if minimum is not None else value <= maximum
    return {"num": num, "den": den, "value": value, "pass": passed}


def validity_gates(population, *, contract):
    """The three gates on the analysis population, exactly. "Sent" is every PLACE record except
    definitely_unsent (K:263, critique 9). With no eligible bet no gate is evaluated (K:270, decision Q17)."""
    classes = [rec.measurement["class"] for rec in population]
    sent = [rec for rec in population if rec.measurement["class"] != "definitely_unsent"]
    resolved = [rec for rec in sent if rec.execution.get("final") and rec.execution.get("size") is not None]
    eligible = classes.count("eligible")
    if eligible == 0:
        none = {"num": None, "den": None, "value": None, "pass": None}
        return {"fill_rate": dict(none), "missing_bsp_share": dict(none), "measurement_unresolved_share": dict(none)}
    filled = [rec for rec in resolved if rec.execution["size"] > 0]
    missing = classes.count("missing_bsp")
    return {
        "fill_rate": _gate(len(filled), len(resolved), minimum=contract.fill_rate_min),
        "missing_bsp_share": _gate(missing, missing + eligible, maximum=contract.missing_bsp_max),
        "measurement_unresolved_share": _gate(classes.count("measurement_unresolved"), len(sent),
                                              maximum=contract.unresolved_max)}


def _betacf(a, b, x):
    tiny, eps = 1e-300, 1e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (tiny if abs(d) < tiny else d)
    h = d
    for m in range(1, 301):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (tiny if abs(d) < tiny else d)
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            return h
    raise PilotError("betainc_not_converged")


def betainc_reg(a, b, x):
    """The regularised incomplete beta I_x(a, b) by the Lentz continued fraction (Numerical Recipes 6.4), with the
    symmetry switch at x >= (a + 1) / (a + b + 2)."""
    if not (a > 0 and b > 0):
        raise PilotError("betainc_domain")
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _df(df):
    if type(df) is not int or df < 1:
        raise PilotError("df_invalid")
    return df


def student_t_cdf(t, df):
    df = _df(df)
    x = df / (df + t * t)
    tail = 0.5 * betainc_reg(df / 2.0, 0.5, x)
    return 1.0 - tail if t >= 0 else tail


def student_t_quantile(p, df):
    """The p quantile of Student's t, 0.5 < p < 1, by bisection on [0, hi] (hi doubled until cdf(hi) >= p) until
    hi - lo <= 1e-12."""
    df = _df(df)
    if not (isinstance(p, float) and 0.5 < p < 1.0):
        raise PilotError("quantile_p_invalid")
    lo, hi = 0.0, 1.0
    while student_t_cdf(hi, df) < p:
        hi *= 2.0
        if hi > 1e300:
            raise PilotError("quantile_not_bracketed")
    for _ in range(400):
        if hi - lo <= 1e-12:
            break
        mid = (lo + hi) / 2.0
        if mid <= lo or mid >= hi:
            break
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def cr1(bets, *, min_clusters, alpha):
    """CR1 on [(UTC date, exact x)]: m = sum(x)/N; SE^2 = G/(G-1) * sum_g (sum_{i in g} (x_i - m))^2 / N^2; t the
    1 - alpha quantile with G - 1 df, turned into a Fraction; PASS iff m >= t*SE, FAIL iff m + t*SE < 0, both
    decided exactly; G < min_clusters is checked before se_undefined (decision Q19)."""
    xs = [(day, _exact(x)) for day, x in bets]
    n = len(xs)
    groups = {}
    for day, x in xs:
        groups.setdefault(day, []).append(x)
    g = len(groups)
    m = sum((x for _, x in xs), Fraction(0)) / n if n else None
    result = {"N": n, "G": g, "m": m, "se2": None, "t": None, "result": "INCONCLUSIVE", "reason": None,
              "near_tie": False}
    if g < min_clusters:
        return dict(result, reason="too_few_clusters")
    se2 = Fraction(g, g - 1) * sum((sum(x - m for x in group) ** 2 for group in groups.values()),
                                   Fraction(0)) / (n * n)
    result["se2"] = se2
    if se2 == 0:
        return dict(result, reason="se_undefined")
    t = student_t_quantile(float(1 - _exact(alpha)), g - 1)
    bound = Fraction(t) ** 2 * se2
    result.update(t=t, near_tie=abs(m * m - bound) <= Fraction(1, 10 ** 9) * bound)
    if m >= 0 and m * m >= bound:
        return dict(result, result="PASS")
    if m < 0 and m * m > bound:
        return dict(result, result="FAIL")
    return result


def verdict(state, *, contract, now_ns):
    """The frozen verdict, once, at the data lock (K:272-279): INVALID for any protocol violation in the lineage
    or a failed gate; else INCONCLUSIVE insufficient_eligible_sample below 400 eligible; else CR1 on the 400
    primary bets, clustered by the UTC date of the decision time (decision Q8)."""
    if contract != state.contract:
        raise PilotError("contract_mismatch")
    if not data_lock_reached(state, now_ns=now_ns):
        raise PilotError("data_lock_not_reached")
    split = analysis_population(state, now_ns=now_ns)
    population = split["population"]
    gates = validity_gates(population, contract=contract)
    counts = {name: 0 for name in CLASS_ORDER}
    for rec in population:
        counts[rec.measurement["class"]] += 1
    base = {"gates": gates, "inference": None, "counts": counts, "population_size": len(population),
            "surplus": len(split["surplus"]), "apart": len(split["apart"])}
    if state.violations or (state.ending and state.ending["reason"] == "breach") or any(
            gate["pass"] is False for gate in gates.values()):
        return dict(base, result="INVALID", reason="protocol_violation" if state.violations else "gate_failed")
    eligible = [rec for rec in population if rec.measurement["class"] == "eligible"]
    if len(eligible) < contract.n_eligible:
        return dict(base, result="INCONCLUSIVE", reason="insufficient_eligible_sample")
    bets = [(rec.utc_day, nclv(rec.execution["P"], rec.bsp["bsp"], c=contract.commission_rate))
            for rec in eligible[:contract.n_eligible]]
    inference = cr1(bets, min_clusters=contract.min_clusters, alpha=contract.alpha_one_sided)
    return dict(base, result=inference["result"], reason=inference["reason"], inference=inference)
