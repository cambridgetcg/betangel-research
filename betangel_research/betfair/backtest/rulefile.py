"""Rule files of the research lane: the parser, the view a rule may read, and decide(view, rule).

A rule file (schema betangel.rule/v1, research/rules/) is a small frozen JSON file. It says, for one kind of
market at fixed times before the off: which runner to pick, which conditions must hold, and what to order. One
fixed function, decide, reads it, so no code is written per idea. An "order" here is a simulated intent, a plain
value handed back to the caller. Nothing in this module can reach an exchange.

The file. Exactly these keys: schema; id; lineage (the idea the file belongs to); status (must be "frozen");
parent_sha256 (the sha256 of the rule file this one was changed from, or null); claim and counter_claim (one
sentence each); market; times; select; when; intents. Decimal values are text with two decimals ("1.50") and are
read as exact Fractions; counts are JSON integers; a float is refused.
- market: countries, event_type_id, market_type, betting_type, number_of_winners, and three flags (bsp_market,
  turn_in_play_enabled, persistence_enabled), each true or null (null = not gated). It is parsed and kept on
  the Rule. decide does not read it: checking a market against it is the caller's job.
- times: {at_s, pull_s} rows in time order. at_s is whole seconds before the first-published off (-86,400 to
  -1). pull_s is when the order is taken back, later than at_s and at most -1, or null for an order that is kept.
- select: "favourite" (the only runner with the lowest best back among priced runners) or "each_active" (every
  runner in the view).
- when: {feature, op, value, code} rows; all must hold (a plain AND). feature is one of FEATURES, op one of
  >=, <=, ==, value one constant, code the text decide gives when the row fails. Rows on a market feature
  (market_open, active_runners) come before rows on a runner feature.
- intents: {side, price, stake, persist} rows. side is BACK or LAY; price is {"ref": "best_back"},
  {"ref": "best_lay"} or {"ref": "fixed", "value": "1.01"}; stake is GBP of backer's stake; persist is "lapse"
  or "keep" (keep is for simulation only).

The view. RuleView holds what a rule may read at one time t: at_s, market_open (1 or 0), active_runners,
ladder_malformed, and one RunnerView per runner ACTIVE and listed at t (selection_id, best_back,
best_back_size, best_lay, spread_ticks; None for a value the book does not have). Whoever builds the view fills
every field from messages published at or before t. The view has no field for BSP, a result, the last traded
price, traded volume or anything after t.

decide(view, rule) gives Decision(intents, code): one or more Intents and no code, or no Intent and one code.
In this order: time_not_in_rule (at_s is not one of the rule's times); ladder_malformed; the code of the first
failing market row of when; the selector's code (no_priced_active_runner or favourite_tie for favourite,
no_active_runner for each_active); then, per selected runner, the code of its first failing runner row, or
no_reference_price when an intent's price ref has no value. A feature with no value fails its row. Under
each_active a runner that fails gets no intent; when every runner fails, the code is the first runner's.

parse_rule takes the file's raw bytes and refuses any that are not exactly file_bytes(value) (sorted keys,
indent 2, ASCII, one final newline; so no duplicate key survives). RuleError's text is "rule:<place>:<reason>".

What this does not do: it reads no file and no clock, makes no draw and keeps no state; it does not check a
price against the price ladder, the market block against a market, or the view against the data. It imports
nothing from this package. Standard library only.
"""

from collections import namedtuple
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import operator
import re

SCHEMA = "betangel.rule/v1"
FROZEN = "frozen"
FAVOURITE, EACH_ACTIVE = SELECTORS = ("favourite", "each_active")
MARKET_FEATURES = ("market_open", "active_runners")
RUNNER_FEATURES = ("best_back", "best_back_size", "best_lay", "spread_ticks")
FEATURES = MARKET_FEATURES + RUNNER_FEATURES
# A row on one of these takes a JSON integer; a row on any other feature takes decimal text.
COUNT_FEATURES = ("market_open", "active_runners", "spread_ticks")
OPERATORS = {">=": operator.ge, "<=": operator.le, "==": operator.eq}
BEST_BACK, BEST_LAY, FIXED = PRICE_REFS = ("best_back", "best_lay", "fixed")
SIDES = ("BACK", "LAY")
LAPSE, KEEP = PERSISTS = ("lapse", "keep")
TIME_NOT_IN_RULE, LADDER_MALFORMED = "time_not_in_rule", "ladder_malformed"
NO_PRICED_ACTIVE_RUNNER, FAVOURITE_TIE = "no_priced_active_runner", "favourite_tie"
NO_ACTIVE_RUNNER, NO_REFERENCE_PRICE = "no_active_runner", "no_reference_price"
# decide's own codes; a when row may not use one of them as its code.
EVALUATOR_CODES = (TIME_NOT_IN_RULE, LADDER_MALFORMED, NO_PRICED_ACTIVE_RUNNER, FAVOURITE_TIE, NO_ACTIVE_RUNNER,
                   NO_REFERENCE_PRICE)
TOP_KEYS = ("claim", "counter_claim", "id", "intents", "lineage", "market", "parent_sha256", "schema", "select",
            "status", "times", "when")
MARKET_KEYS = ("betting_type", "bsp_market", "countries", "event_type_id", "market_type", "number_of_winners",
               "persistence_enabled", "turn_in_play_enabled")
FLAGS = ("bsp_market", "turn_in_play_enabled", "persistence_enabled")
MAX_RULE_BYTES = 1 << 20
MAX_OFFSET_S = 86400
MAX_ROWS = 64
MAX_SENTENCE = 400
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")                 # as the study ledger's study and lineage
CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DECIMAL_RE = re.compile(r"(0|[1-9][0-9]{0,5})\.[0-9]{2}")
COUNTRY_RE = re.compile(r"[A-Z]{2}")
EVENT_TYPE_RE = re.compile(r"[1-9][0-9]{0,8}")
TOKEN_RE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
MIN_PRICE, MAX_PRICE = Fraction(101, 100), Fraction(1000)


class RuleError(ValueError):
    """A rule file this module cannot use; str() is "rule:<place>:<reason>"."""


Market = namedtuple("Market", ("countries", "event_type_id", "market_type", "betting_type", "number_of_winners",
                               "bsp_market", "turn_in_play_enabled", "persistence_enabled"))
Predicate = namedtuple("Predicate", ("feature", "op", "value", "code"))
# One intents row: fixed is the exact price of a "fixed" ref, else None.
Template = namedtuple("Template", ("side", "ref", "fixed", "stake", "persist"))
# One runner in a view; a value the book does not have is None.
RunnerView = namedtuple("RunnerView", ("selection_id", "best_back", "best_back_size", "best_lay", "spread_ticks"))


@dataclass(frozen=True)
class Rule:
    """The values of one rule file (parse_rule); sha256 is the sha256 of the file's raw bytes. times is
    ((at_s, pull_s or None), ...), when is Predicates and intents is Templates, all in file order."""

    sha256: str
    id: str
    lineage: str
    parent_sha256: object
    claim: str
    counter_claim: str
    market: Market
    times: tuple
    select: str
    when: tuple
    intents: tuple


@dataclass(frozen=True)
class RuleView:
    """What a rule may read at one time t (module docstring). runners holds one RunnerView per runner ACTIVE and
    listed at t; prices and sizes are exact Fractions, counts are ints."""

    at_s: int
    market_open: int
    active_runners: int
    ladder_malformed: bool
    runners: tuple


@dataclass(frozen=True)
class Intent:
    """One simulated order decide asks for: price and stake are exact Fractions (stake in GBP of backer's stake),
    pull_s is the rule's pull time for this at_s or None, persist is "lapse" or "keep"."""

    selection_id: int
    side: str
    price: Fraction
    stake: Fraction
    pull_s: object
    persist: str


@dataclass(frozen=True)
class Decision:
    """decide's answer: intents and no code, or no intent and the one code that says why."""

    intents: tuple
    code: object


def file_bytes(value):
    """The one byte form a rule file may have: sorted keys, indent 2, ASCII, one final newline."""
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _fail(place, reason):
    raise RuleError(f"rule:{place}:{reason}")


def _refuse(text):
    raise ValueError("not_strict_json")


def _object(value, place, keys):
    if not isinstance(value, dict):
        _fail(place, "not_an_object")
    if set(value) != set(keys):
        _fail(place, "keys_not_the_expected_set")
    return value


def _rows(value, place, least=1):
    if not isinstance(value, list) or not least <= len(value) <= MAX_ROWS:
        _fail(place, "not_a_list")
    return value


def _match(value, place, pattern, reason):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(place, reason)
    return value


def _one_of(value, place, allowed, reason):
    if not isinstance(value, str) or value not in allowed:
        _fail(place, reason)
    return value


def _int(value, place, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail(place, "not_an_int_in_range")
    return value


def _decimal(value, place):
    return Fraction(Decimal(_match(value, place, DECIMAL_RE, "not_decimal_text")))


def _sentence(value, place):
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or len(value) > MAX_SENTENCE:
        _fail(place, "not_a_sentence")
    return value


def _market(value):
    item = _object(value, "market", MARKET_KEYS)
    countries = [_match(country, "market.countries", COUNTRY_RE, "country_malformed")
                 for country in _rows(item["countries"], "market.countries")]
    if countries != sorted(set(countries)):
        _fail("market.countries", "not_sorted_and_unique")
    for name in FLAGS:
        if item[name] is not True and item[name] is not None:
            _fail(f"market.{name}", "not_true_or_null")
    return Market(countries=tuple(countries),
                  event_type_id=_match(item["event_type_id"], "market.event_type_id", EVENT_TYPE_RE, "not_an_id"),
                  market_type=_match(item["market_type"], "market.market_type", TOKEN_RE, "not_a_token"),
                  betting_type=_match(item["betting_type"], "market.betting_type", TOKEN_RE, "not_a_token"),
                  number_of_winners=_int(item["number_of_winners"], "market.number_of_winners", 1, 100),
                  **{name: item[name] for name in FLAGS})


def _times(value):
    times = []
    for index, row in enumerate(_rows(value, "times")):
        place = f"times[{index}]"
        item = _object(row, place, ("at_s", "pull_s"))
        at_s, pull_s = _int(item["at_s"], place, -MAX_OFFSET_S, -1), item["pull_s"]
        if times and at_s <= times[-1][0]:
            _fail(place, "not_after_the_one_before")
        if pull_s is not None and _int(pull_s, place, -MAX_OFFSET_S, -1) <= at_s:
            _fail(place, "pull_not_after_at")
        times.append((at_s, pull_s))
    return tuple(times)


def _when(value):
    rows = []
    for index, row in enumerate(_rows(value, "when", least=0)):
        place = f"when[{index}]"
        item = _object(row, place, ("code", "feature", "op", "value"))
        feature = _one_of(item["feature"], place, FEATURES, "feature_not_supported")
        op = _one_of(item["op"], place, OPERATORS, "operator_not_supported")
        code = _match(item["code"], place, CODE_RE, "code_malformed")
        if code in EVALUATOR_CODES:
            _fail(place, "code_reserved")
        if feature in MARKET_FEATURES and rows and rows[-1].feature in RUNNER_FEATURES:
            _fail(place, "market_feature_after_a_runner_feature")
        constant = (_int(item["value"], place, 0, 10 ** 6) if feature in COUNT_FEATURES
                    else _decimal(item["value"], place))
        rows.append(Predicate(feature, op, constant, code))
    return tuple(rows)


def _intents(value):
    rows = []
    for index, row in enumerate(_rows(value, "intents")):
        place = f"intents[{index}]"
        item = _object(row, place, ("persist", "price", "side", "stake"))
        price = item["price"]
        ref = _one_of(price.get("ref") if isinstance(price, dict) else None, f"{place}.price", PRICE_REFS,
                      "price_ref_not_supported")
        _object(price, f"{place}.price", ("ref", "value") if ref == FIXED else ("ref",))
        fixed = _decimal(price["value"], f"{place}.price") if ref == FIXED else None
        if fixed is not None and not MIN_PRICE <= fixed <= MAX_PRICE:
            _fail(f"{place}.price", "fixed_price_out_of_range")
        stake = _decimal(item["stake"], place)
        if not stake:
            _fail(place, "stake_not_positive")
        rows.append(Template(_one_of(item["side"], place, SIDES, "side_not_supported"), ref, fixed, stake,
                             _one_of(item["persist"], place, PERSISTS, "persist_not_supported")))
    if len(set(rows)) != len(rows):
        _fail("intents", "row_used_twice")
    return tuple(rows)


def parse_rule(raw):
    """The Rule of one rule file's raw bytes after every check in the module docstring; RuleError otherwise."""
    if not isinstance(raw, bytes) or len(raw) > MAX_RULE_BYTES:
        _fail("file", "not_bytes_or_too_large")
    try:
        value = json.loads(raw, parse_constant=_refuse, parse_float=_refuse)
    except (ValueError, RecursionError):
        _fail("file", "not_strict_json")
    top = _object(value, "top", TOP_KEYS)
    if file_bytes(value) != raw:
        _fail("file", "not_canonical")
    if top["schema"] != SCHEMA:
        _fail("schema", "not_supported")
    if top["status"] != FROZEN:
        _fail("status", "not_frozen")
    if top["parent_sha256"] is not None:
        _match(top["parent_sha256"], "parent_sha256", SHA256_RE, "not_a_sha256_or_null")
    return Rule(sha256=hashlib.sha256(raw).hexdigest(), id=_match(top["id"], "id", NAME_RE, "name_malformed"),
                lineage=_match(top["lineage"], "lineage", NAME_RE, "name_malformed"),
                parent_sha256=top["parent_sha256"], claim=_sentence(top["claim"], "claim"),
                counter_claim=_sentence(top["counter_claim"], "counter_claim"), market=_market(top["market"]),
                times=_times(top["times"]),
                select=_one_of(top["select"], "select", SELECTORS, "selector_not_supported"),
                when=_when(top["when"]), intents=_intents(top["intents"]))


def _failing(rule, features, source):
    """The code of the first when row on one of features that does not hold for source (a view or a runner), else
    None. A feature with no value fails its row."""
    for row in rule.when:
        if row.feature in features:
            value = getattr(source, row.feature)
            if value is None or not OPERATORS[row.op](value, row.value):
                return row.code
    return None


def _for_runner(runner, rule, pull_s):
    """(intents, None) for a runner that passes every runner row and has every price asked for, else ((), code)."""
    code = _failing(rule, RUNNER_FEATURES, runner)
    if code is not None:
        return (), code
    intents = []
    for row in rule.intents:
        price = row.fixed if row.ref == FIXED else getattr(runner, row.ref)
        if price is None:
            return (), NO_REFERENCE_PRICE
        intents.append(Intent(runner.selection_id, row.side, price, row.stake, pull_s, row.persist))
    return tuple(intents), None


def decide(view, rule):
    """What the rule orders for this view: Decision(intents, None), or Decision((), code) (module docstring).
    Pure: the answer depends on the two arguments alone, and neither is changed."""
    pulls = dict(rule.times)
    if view.at_s not in pulls:
        return Decision((), TIME_NOT_IN_RULE)
    if view.ladder_malformed:
        return Decision((), LADDER_MALFORMED)
    code = _failing(rule, MARKET_FEATURES, view)
    if code is not None:
        return Decision((), code)
    chosen = view.runners
    if rule.select == FAVOURITE:
        priced = [runner for runner in view.runners if runner.best_back is not None]
        if not priced:
            return Decision((), NO_PRICED_ACTIVE_RUNNER)
        lowest = min(runner.best_back for runner in priced)
        chosen = [runner for runner in priced if runner.best_back == lowest]
        if len(chosen) != 1:
            return Decision((), FAVOURITE_TIE)
    elif not chosen:
        return Decision((), NO_ACTIVE_RUNNER)
    intents, first_code = [], None
    for runner in chosen:
        made, code = _for_runner(runner, rule, pulls[view.at_s])
        intents.extend(made)
        first_code = first_code or code
    return Decision(tuple(intents), None if intents else first_code)
