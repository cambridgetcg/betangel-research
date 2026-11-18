"""Rule files of the research lane: the parser, the views a rule may read, and decide(view, rule).

A rule file (schema betangel.rule/v1 or betangel.rule/v2, research/rules/) is a small frozen JSON file. It says,
for one kind of market at fixed times before the off: which runner to pick, which conditions must hold, and what
to order. One fixed function, decide, reads it, so no code is written per idea. An "order" here is a simulated
intent, a plain value handed back to the caller. Nothing in this module can reach an exchange.

The file. Exactly these keys: schema; id; lineage (the idea the file belongs to); status (must be "frozen");
parent_sha256 (the sha256 of the rule file this one was changed from, or null); claim and counter_claim (one
sentence each); market; times; select; when; intents. Decimal values are text with two decimals ("1.50") and are
read as exact Fractions; counts are JSON integers; a float is refused.
- market: countries, event_type_id, market_type, betting_type, number_of_winners, and three flags (bsp_market,
  turn_in_play_enabled, persistence_enabled), each true or null (null = not gated). It is parsed and kept on
  the Rule. decide does not read it: checking a market against it is the caller's job.
- times: {at_s, pull_s} rows in time order. at_s is whole seconds before the first-published off (-86,400 to
  -1). pull_s is when the order is taken back, later than at_s and at most -1, or null for an order that is not
  taken back before the off.
- select: "favourite" (the only runner with the lowest best back among priced runners) or "each_active" (every
  runner in the view).
- when: {feature, op, value, code} rows; all must hold (a plain AND). feature is one of the schema's features
  (FEATURES for v1), op one of >=, <=, ==, value one constant, code the text decide gives when the row fails. A
  code may not be one of decide's own (EVALUATOR_CODES); nothing here knows the codes a caller adds, so a caller
  must refuse a clash with its own. Rows on a market feature (market_open, active_runners) come before rows on a
  runner feature.
- intents: {side, price, stake, persist} rows. side is BACK or LAY; price is {"ref": "best_back"},
  {"ref": "best_lay"} or {"ref": "fixed", "value": "1.01"}; stake is GBP of backer's stake; persist is "lapse"
  or "keep" (keep is for simulation only). decide hands pull_s and persist back unchanged; they are for the
  caller that walks the orders. The pull time alone says when an order is taken back. persist says what the
  in-play turn does to an order still resting then: "lapse" ends it, "keep" leaves it in the market. So "lapse"
  with a null pull_s is an order that rests until the market turns in play. "keep" in a rule where any time has
  a pull_s could never matter and is refused (keep_with_a_pull_time).

Version 2 (betangel.rule/v2). The same keys, grammar, operators, selectors, intents, limits and byte form as v1,
and six more features, each taking decimal text (COUNT_FEATURES is v1's; FEATURES_V2 is v1's six and these six).
Two are market features (added in MARKET_FEATURES_V2): back_book_pct and lay_book_pct, the field's book in
percent, 100 times the sum over the runners ACTIVE and listed at t (the whole field) of 1 / best back, or of
1 / best lay. Four are runner features (added in RUNNER_FEATURES_V2): best_lay_size, the size at the best lay;
back_depth_3 and lay_depth_3, the summed sizes at the first three price points shown on that side, best first, or
at fewer when fewer are shown; and mid_ratio, the runner's mid at the end of a window over its mid at the
start. Sizes are GBP of backer's stake. Whoever builds the view computes these values; this module only reads
them. A row on mid_ratio (MOVE_FEATURES) is a move row, with exactly the keys MOVE_KEYS:
{code, feature, from_s, op, to_s, value}. from_s is whole seconds before the first-published off (-86,400 to -1);
to_s is such an offset later than from_s (else to_not_after_from), or null for the cell's decision time t. The
parser reads a dict row's feature first and then wants that feature's exact keys, so a plain row on mid_ratio, or
a move row on any other feature, is keys_not_the_expected_set. Market rows come before runner rows by v2's own
sets, so a book row after a move row is market_feature_after_a_runner_feature. No declared window may end after a
decision: every integer to_s is at or before the file's first at_s (else move_ends_after_a_decision) and every
from_s is before it (else move_starts_at_or_after_a_decision), so each window lies at or before every t of the
file whatever the rows' order. These checks bound the declared windows only; that a view keeps to them is its
builder's job. A v1 file that names a v2 feature in a four-key row is feature_not_supported, as before; a v1 row
that carries from_s and to_s is keys_not_the_expected_set, because v1 checks a row's keys first.

The views. RuleView holds what a v1 rule may read at one time t: at_s, market_open (1 or 0), active_runners,
ladder_malformed, and one RunnerView per runner ACTIVE and listed at t (selection_id, best_back,
best_back_size, best_lay, spread_ticks; None for a value the book does not have). RuleView2 and RunnerView2 are
v2's: RuleView2 is RuleView's fields then back_book_pct and lay_book_pct, and holds RunnerView2s, which are
RunnerView's fields then best_lay_size, back_depth_3, lay_depth_3 and moves. moves is ((window, ratio or None),
...) with window = (from_s, to_s): one item per distinct window of the rule, in the order of its first row in the
file (windows(rule)), never sorted. A book percent is a market field, computed once over the whole field: it is
never recomputed from runners. Whoever builds a view fills every field from messages published at or before t.
No view has a field for BSP, a result, the last traded price, traded volume or anything after t.

decide(view, rule) gives Decision(intents, code): one or more Intents and no code, or no Intent and one code.
In this order: time_not_in_rule (at_s is not one of the rule's times); ladder_malformed; the code of the first
failing market row of when; the selector's code (no_priced_active_runner or favourite_tie for favourite,
no_active_runner for each_active); then, per selected runner, the code of its first failing runner row, or
no_reference_price when an intent's price ref has no value. A feature with no value fails its row. Under
each_active a runner that fails gets no intent; when every runner fails, the code is the first runner's. Which
rows are market rows and which are runner rows is the rule's schema's (market_features(rule),
runner_features(rule)). A move row reads its runner's moves by the exact key (from_s, to_s); a move computed
with no value is None and fails its row. Before anything else, decide raises
ValueError("decide:rule_not_its_schemas") unless the rule's schema is one of SCHEMAS and every row's feature is
one of that schema's features: a row in neither set would never be read, so a hand-built Rule (Rule.schema
defaults to SCHEMA) could otherwise pass with rows skipped. Then, for a v2 rule, it raises
ValueError("decide:view_not_v2") unless the view is a RuleView2 whose runners are RunnerView2s,
ValueError("decide:move_twice_in_view") when a runner's moves holds a window of the rule more than once, and
ValueError("decide:move_not_in_view") unless every runner's moves holds every window of the rule: a window that
was not computed is a wiring error, never read as None, and a window held twice would be read by whichever copy
came first. A window the rule does not have is never read. A v1 rule reads v1's fields only, so it decides alike
on a RuleView and on a RuleView2 with the same v1 values.

parse_rule takes the file's raw bytes and refuses any that are not exactly file_bytes(value) (sorted keys,
indent 2, ASCII, one final newline; so no duplicate key survives). That comparison comes last, when every value
is known to be small: indenting deeply nested lists would build a very large text. RuleError's text is
"rule:<place>:<reason>".

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
SCHEMA_V2 = "betangel.rule/v2"
SCHEMAS = (SCHEMA, SCHEMA_V2)
FROZEN = "frozen"
FAVOURITE, EACH_ACTIVE = SELECTORS = ("favourite", "each_active")
MARKET_FEATURES = ("market_open", "active_runners")
RUNNER_FEATURES = ("best_back", "best_back_size", "best_lay", "spread_ticks")
FEATURES = MARKET_FEATURES + RUNNER_FEATURES
# betangel.rule/v2 reads v1's six features and six more (module docstring). Per-schema sets are tuples, and
# market_features(rule) / runner_features(rule) pick them: no module-level dict but OPERATORS.
MARKET_FEATURES_V2 = MARKET_FEATURES + ("back_book_pct", "lay_book_pct")
RUNNER_FEATURES_V2 = RUNNER_FEATURES + ("best_lay_size", "back_depth_3", "lay_depth_3", "mid_ratio")
FEATURES_V2 = MARKET_FEATURES_V2 + RUNNER_FEATURES_V2
# A row on one of these is a move row: it carries its own window and has exactly MOVE_KEYS.
MOVE_FEATURES = ("mid_ratio",)
MOVE_KEYS = ("code", "feature", "from_s", "op", "to_s", "value")
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
# One when row: from_s and to_s are a move row's window (to_s None = the decision time t), None on a plain row.
Predicate = namedtuple("Predicate", ("feature", "op", "value", "code", "from_s", "to_s"), defaults=(None, None))
# One intents row: fixed is the exact price of a "fixed" ref, else None.
Template = namedtuple("Template", ("side", "ref", "fixed", "stake", "persist"))
# One runner in a view; a value the book does not have is None.
RunnerView = namedtuple("RunnerView", ("selection_id", "best_back", "best_back_size", "best_lay", "spread_ticks"))
# One runner in a v2 view: RunnerView's fields, then v2's. moves is ((window, ratio or None), ...), one item per
# window of windows(rule), in that order; window is (from_s, to_s).
RunnerView2 = namedtuple("RunnerView2", ("selection_id", "best_back", "best_back_size", "best_lay", "spread_ticks",
                                         "best_lay_size", "back_depth_3", "lay_depth_3", "moves"))


@dataclass(frozen=True)
class Rule:
    """The values of one rule file (parse_rule); sha256 is the sha256 of the file's raw bytes. times is
    ((at_s, pull_s or None), ...), when is Predicates and intents is Templates, all in file order. schema is the
    file's schema, SCHEMA or SCHEMA_V2; its default SCHEMA is for a v1 Rule built by hand, and decide refuses a
    Rule whose rows its schema does not name."""

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
    schema: str = SCHEMA


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
class RuleView2:
    """What a v2 rule may read at one time t (module docstring): RuleView's fields, then the field's book in
    percent on each side, computed over the whole field (None when some runner has no best price on that side, or
    no runner is ACTIVE and listed). runners holds one RunnerView2 per runner ACTIVE and listed at t."""

    at_s: int
    market_open: int
    active_runners: int
    ladder_malformed: bool
    runners: tuple
    back_book_pct: object
    lay_book_pct: object


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


def _feature_sets(schema):
    """(market features, runner features) of a schema; ValueError rule:schema_not_supported for a schema this
    module does not read, never v1's sets by default."""
    if schema == SCHEMA:
        return MARKET_FEATURES, RUNNER_FEATURES
    if schema == SCHEMA_V2:
        return MARKET_FEATURES_V2, RUNNER_FEATURES_V2
    raise ValueError("rule:schema_not_supported")


def market_features(rule):
    """The market features of the rule's schema: MARKET_FEATURES for v1, MARKET_FEATURES_V2 for v2, else
    ValueError."""
    return _feature_sets(rule.schema)[0]


def runner_features(rule):
    """The runner features of the rule's schema: RUNNER_FEATURES for v1, RUNNER_FEATURES_V2 for v2, else
    ValueError."""
    return _feature_sets(rule.schema)[1]


def windows(rule):
    """The rule's distinct move windows ((from_s, to_s), ...), in the order of their first row in the file: the keys
    of every RunnerView2.moves for this rule, in this order. Never sorted: (from_s, None) and (from_s, -1800) cannot
    be compared. () for a rule with no move row."""
    found = []
    for row in rule.when:
        if row.feature in MOVE_FEATURES and (row.from_s, row.to_s) not in found:
            found.append((row.from_s, row.to_s))
    return tuple(found)


def _window(item, place):
    """(from_s, to_s) of a move row: from_s a JSON int from -86,400 to -1; to_s one in the same range later than
    from_s, or null (the decision time t of the cell)."""
    from_s, to_s = _int(item["from_s"], place, -MAX_OFFSET_S, -1), item["to_s"]
    if to_s is not None and _int(to_s, place, -MAX_OFFSET_S, -1) <= from_s:
        _fail(place, "to_not_after_from")
    return from_s, to_s


def _when(value, schema):
    market, runner = _feature_sets(schema)
    rows = []
    for index, row in enumerate(_rows(value, "when", least=0)):
        place = f"when[{index}]"
        # v2 reads a dict row's feature first, then wants that feature's exact keys (a v1 row is never a move row).
        move = schema == SCHEMA_V2 and isinstance(row, dict) and row.get("feature") in MOVE_FEATURES
        item = _object(row, place, MOVE_KEYS if move else ("code", "feature", "op", "value"))
        feature = _one_of(item["feature"], place, market + runner, "feature_not_supported")
        op = _one_of(item["op"], place, OPERATORS, "operator_not_supported")
        code = _match(item["code"], place, CODE_RE, "code_malformed")
        if code in EVALUATOR_CODES:
            _fail(place, "code_reserved")
        if feature in market and rows and rows[-1].feature in runner:
            _fail(place, "market_feature_after_a_runner_feature")
        constant = (_int(item["value"], place, 0, 10 ** 6) if feature in COUNT_FEATURES
                    else _decimal(item["value"], place))
        rows.append(Predicate(feature, op, constant, code, *(_window(item, place) if move else ())))
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
    schema = top["schema"]
    if schema not in SCHEMAS:
        _fail("schema", "not_supported")
    if top["status"] != FROZEN:
        _fail("status", "not_frozen")
    if top["parent_sha256"] is not None:
        _match(top["parent_sha256"], "parent_sha256", SHA256_RE, "not_a_sha256_or_null")
    rule = Rule(sha256=hashlib.sha256(raw).hexdigest(), id=_match(top["id"], "id", NAME_RE, "name_malformed"),
                lineage=_match(top["lineage"], "lineage", NAME_RE, "name_malformed"),
                parent_sha256=top["parent_sha256"], claim=_sentence(top["claim"], "claim"),
                counter_claim=_sentence(top["counter_claim"], "counter_claim"), market=_market(top["market"]),
                times=_times(top["times"]),
                select=_one_of(top["select"], "select", SELECTORS, "selector_not_supported"),
                when=_when(top["when"], schema), intents=_intents(top["intents"]), schema=schema)
    if any(pull_s is not None for _, pull_s in rule.times):
        for index, row in enumerate(rule.intents):
            if row.persist == KEEP:
                _fail(f"intents[{index}]", "keep_with_a_pull_time")
    # No look-ahead: every declared window lies at or before the first decision, so at or before every t of the
    # file, whatever the rows' order. Only a v2 file has move rows.
    first_at_s = rule.times[0][0]
    for index, row in enumerate(rule.when):
        if row.feature in MOVE_FEATURES:
            if row.to_s is not None and row.to_s > first_at_s:
                _fail(f"when[{index}]", "move_ends_after_a_decision")
            if row.from_s >= first_at_s:
                _fail(f"when[{index}]", "move_starts_at_or_after_a_decision")
    if file_bytes(value) != raw:                # last: every value is now known to be small (module docstring)
        _fail("file", "not_canonical")
    return rule


def _move(runner, window):
    """The move runner.moves holds at exactly window, (from_s, to_s): a ratio, or None for a move computed with no
    value. ValueError decide:move_not_in_view when runner.moves has no such window: never read as None."""
    for key, ratio in runner.moves:
        if key == window:
            return ratio
    raise ValueError("decide:move_not_in_view")


def _failing(rule, features, source):
    """The code of the first when row on one of features that does not hold for source (a view or a runner), else
    None. A feature with no value fails its row. A move row reads source.moves by its window."""
    for row in rule.when:
        if row.feature in features:
            value = (_move(source, (row.from_s, row.to_s)) if row.feature in MOVE_FEATURES
                     else getattr(source, row.feature))
            if value is None or not OPERATORS[row.op](value, row.value):
                return row.code
    return None


def _for_runner(runner, rule, pull_s):
    """(intents, None) for a runner that passes every runner row and has every price asked for, else ((), code)."""
    code = _failing(rule, runner_features(rule), runner)
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
    Pure: the answer depends on the two arguments alone, and neither is changed. ValueError
    decide:rule_not_its_schemas first when the rule's schema is not one of SCHEMAS or names not every row's feature;
    then, for a v2 rule, decide:view_not_v2, decide:move_twice_in_view or decide:move_not_in_view when the view is
    not one built for it."""
    if rule.schema not in SCHEMAS:
        raise ValueError("decide:rule_not_its_schemas")
    market = market_features(rule)
    known = market + runner_features(rule)
    if any(row.feature not in known for row in rule.when):
        raise ValueError("decide:rule_not_its_schemas")         # _failing would skip that row, never read it
    if rule.schema == SCHEMA_V2:
        if not isinstance(view, RuleView2) or not all(isinstance(item, RunnerView2) for item in view.runners):
            raise ValueError("decide:view_not_v2")
        wanted = windows(rule)
        for item in view.runners:
            for window in wanted:
                if sum(key == window for key, _ in item.moves) > 1:
                    raise ValueError("decide:move_twice_in_view")
                _move(item, window)
    pulls = dict(rule.times)
    if view.at_s not in pulls:
        return Decision((), TIME_NOT_IN_RULE)
    if view.ladder_malformed:
        return Decision((), LADDER_MALFORMED)
    code = _failing(rule, market, view)
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
