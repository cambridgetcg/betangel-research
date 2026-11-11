"""The data split of the research lane: which label a race date carries (research/partitions-v1.json).

A race date is the Europe/London calendar date of a market's first-published off, as ISO text
(movement.race_date). The split file holds an ordered list of date ranges. A race date gets the label of the
range that holds it; a date in no range has no label, and no pass may use it. A range is one of two kinds:
"fixed" gives every date one label; "salted_hash" gives a date one of two labels from
u = int(sha256(salt + "|" + race_date).hexdigest()[:8], 16): label_at_residue when u % modulus == residue,
else label_otherwise. Split.label is the only place that knows this rule.

Each label has a role: explore (look around), holdout (locked away, tested once), sealed or never. A label
may carry a budget: explore_budget (passes per lineage) for an explore label, alpha_lifetime (the total alpha
all tests may spend, decimal text) for a holdout label. Split.label_problem says why a registration may not
name a label: it is unknown, its role is sealed or never, or it has no budget. The file also holds the two
half-years (H1, H2), max_looks (blind passes per study) and prior_exposure (one sentence per earlier study
that read the same data; registrations cite the ids).

parse_split takes the file's raw bytes. It refuses bytes that are not exactly file_bytes(value) (sorted keys,
indent 2, ASCII, one final newline; so no duplicate key survives), any key set that is not the expected one,
ranges that overlap or are out of order, a label used by two ranges, a salt that is not 16 to 128 printable
ASCII characters without a space or "|", and a residue outside 0 .. modulus - 1. PartitionError's text is
"partition:<place>:<reason>".

What this does not do: it reads no file and no data, and it cannot stop a later split file with another
salt. The controls for that are the merged file, its sha256 pinned in tests/test_backtest_lane.py and named
in each registration, and the same sha256 in every study ledger entry. Standard library only.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import re

from .market_index import loads_strict
from .movement import CLUSTER, DATE_RE, RATE_RE

SCHEMA = "betangel.partitions/v1"
FIXED, SALTED_HASH = "fixed", "salted_hash"
EXPLORE, HOLDOUT, SEALED, NEVER = ROLES = ("explore", "holdout", "sealed", "never")
HALVES = ("H1", "H2")
TOP_KEYS = ("halves", "labels", "max_looks", "prior_exposure", "ranges", "schema", "split_by")
FIXED_KEYS = ("first", "label", "last", "rule")
SALTED_KEYS = ("first", "label_at_residue", "label_otherwise", "last", "modulus", "residue", "rule", "salt")
MAX_SPLIT_BYTES = 1 << 20
LABEL_RE = re.compile(r"[A-Z][A-Z0-9-]{0,31}")
SALT_RE = re.compile(r"[\x21-\x7b\x7d\x7e]{16,128}")
EXPOSURE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class PartitionError(ValueError):
    """A split file this module cannot use, or a race date that is not one; str() is "partition:<place>:<reason>"."""


@dataclass(frozen=True)
class Range:
    """One row of the split: first and last race date (inclusive) and its labels. A fixed row has one label and no
    salt; a salted_hash row has (label at the residue, label otherwise)."""

    first: str
    last: str
    labels: tuple
    salt: str = None
    modulus: int = None
    residue: int = None


@dataclass(frozen=True)
class Split:
    """The values of one split file (parse_split); sha256 is the sha256 of the file's raw bytes."""

    sha256: str
    ranges: tuple
    roles: dict
    explore_budgets: dict
    alpha_lifetimes: dict
    halves: tuple
    max_looks: int
    prior_exposure: dict

    def label(self, race_date):
        """The label of a race date (ISO text), or None when no range holds it."""
        day = _day(race_date, "race_date")
        for item in self.ranges:
            if item.first <= day <= item.last:
                if item.salt is None:
                    return item.labels[0]
                u = int(hashlib.sha256((item.salt + "|" + day).encode("ascii")).hexdigest()[:8], 16)
                return item.labels[0 if u % item.modulus == item.residue else 1]
        return None

    def half(self, race_date):
        """The half-year of a race date (ISO text), H1 or H2, or None outside both."""
        day = _day(race_date, "race_date")
        return next((name for name, first, last in self.halves if first <= day <= last), None)

    def role(self, label):
        """The role of a label, or None for a label the file does not hold."""
        return self.roles.get(label)

    def explore_budget(self, label):
        return self.explore_budgets.get(label)

    def alpha_lifetime(self, label):
        return self.alpha_lifetimes.get(label)

    def label_problem(self, label):
        """None when a registration may name this label, else why not: label_unknown, role_sealed, role_never or
        no_budget."""
        role = self.roles.get(label) if isinstance(label, str) else None
        if role is None:
            return "label_unknown"
        if role in (SEALED, NEVER):
            return f"role_{role}"
        if label not in self.explore_budgets and label not in self.alpha_lifetimes:
            return "no_budget"
        return None


def file_bytes(value):
    """The one byte form a split file may have: sorted keys, indent 2, ASCII, one final newline."""
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _fail(place, reason):
    raise PartitionError(f"partition:{place}:{reason}")


def _object(value, place, keys):
    if not isinstance(value, dict):
        _fail(place, "not_an_object")
    if set(value) != set(keys):
        _fail(place, "keys_not_the_expected_set")
    return value


def _day(value, place):
    try:
        if not isinstance(value, str) or not DATE_RE.fullmatch(value):
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        _fail(place, "not_a_date")
    return value


def _span(item, place, previous):
    """(first, last) of a row after checking both dates, their order, and that the row starts after previous."""
    first, last = _day(item["first"], place), _day(item["last"], place)
    if last < first:
        _fail(place, "ends_before_it_starts")
    if first <= previous:
        _fail(place, "not_after_the_one_before")
    return first, last


def _int(value, place, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail(place, "not_an_int_in_range")
    return value


def _label(value, place):
    if not isinstance(value, str) or not LABEL_RE.fullmatch(value):
        _fail(place, "label_malformed")
    return value


def _range(item, place, previous):
    rule = item.get("rule") if isinstance(item, dict) else None
    if rule == FIXED:
        _object(item, place, FIXED_KEYS)
        return Range(*_span(item, place, previous), (_label(item["label"], place),))
    if rule != SALTED_HASH:
        _fail(place, "rule_not_supported")
    _object(item, place, SALTED_KEYS)
    if not isinstance(item["salt"], str) or not SALT_RE.fullmatch(item["salt"]):
        _fail(place, "salt_malformed")
    modulus = _int(item["modulus"], place, 2, 64)
    if type(item["residue"]) is not int or not 0 <= item["residue"] < modulus:
        _fail(place, "residue_not_below_modulus")
    labels = (_label(item["label_at_residue"], place), _label(item["label_otherwise"], place))
    return Range(*_span(item, place, previous), labels, item["salt"], modulus, item["residue"])


def parse_split(raw):
    """The Split of one split file's raw bytes after every check in the module docstring; PartitionError
    otherwise."""
    if not isinstance(raw, bytes) or len(raw) > MAX_SPLIT_BYTES:
        _fail("file", "not_bytes_or_too_large")
    try:
        value = loads_strict(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        _fail("file", "not_json")
    top = _object(value, "top", TOP_KEYS)
    if file_bytes(value) != raw:
        _fail("file", "not_canonical")
    for key, expected in (("schema", SCHEMA), ("split_by", CLUSTER)):
        if top[key] != expected:
            _fail(key, "not_supported")
    if not isinstance(top["ranges"], list) or not top["ranges"]:
        _fail("ranges", "not_a_list")
    ranges, previous = [], ""
    for index, item in enumerate(top["ranges"]):
        ranges.append(_range(item, f"ranges[{index}]", previous))
        previous = ranges[-1].last
    used = [name for item in ranges for name in item.labels]
    if len(set(used)) != len(used):
        _fail("ranges", "label_used_twice")
    roles, budgets, lifetimes = {}, {}, {}
    for name, item in sorted(_object(top["labels"], "labels", used).items()):
        place = f"labels.{name}"
        if not isinstance(item, dict) or item.get("role") not in ROLES:
            _fail(place, "role_not_supported")
        roles[name] = item["role"]
        budget = {EXPLORE: "explore_budget", HOLDOUT: "alpha_lifetime"}.get(item["role"])
        if set(item) not in ({"role"}, {"role", budget}):
            _fail(place, "keys_not_the_expected_set")
        if budget == "explore_budget" and budget in item:
            budgets[name] = _int(item[budget], place, 1, 1000)
        elif budget in item:
            if not isinstance(item[budget], str) or not RATE_RE.fullmatch(item[budget]):
                _fail(place, "not_a_decimal_rate")
            lifetimes[name] = Fraction(Decimal(item[budget]))
            if not lifetimes[name]:
                _fail(place, "not_a_decimal_rate")
    halves, previous = [], ""
    for name in HALVES:
        place = f"halves.{name}"
        item = _object(_object(top["halves"], "halves", HALVES)[name], place, ("first", "last"))
        halves.append((name, *_span(item, place, previous)))
        previous = halves[-1][2]
    exposure = top["prior_exposure"]
    if not isinstance(exposure, dict) or not exposure or any(
            not EXPOSURE_ID_RE.fullmatch(key) or not isinstance(text, str) or not text.strip()
            for key, text in exposure.items()):
        _fail("prior_exposure", "not_ids_with_a_sentence_each")
    return Split(sha256=hashlib.sha256(raw).hexdigest(), ranges=tuple(ranges), roles=roles, explore_budgets=budgets,
                 alpha_lifetimes=lifetimes, halves=tuple(halves), max_looks=_int(top["max_looks"], "max_looks", 1, 100),
                 prior_exposure=dict(exposure))
