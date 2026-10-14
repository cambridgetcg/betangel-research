"""Experiment contracts for offline backtests (schema_version 1): strict validation and a canonical sha256.

A contract writes down, before any outcome is inspected, everything a backtest could otherwise tune after
seeing results: what it is for (registration, a typed financial rule and deterministic engineering acceptance
criteria), the population and its month splits (the holdout pinned to SEALED_HOLDOUT, recorded as sealed by
not being downloaded), the cohort and exclusions, a decision time anchored only to information known then
with its delay, equal-pt order and image handling, the data capabilities a run needs (decision-time inputs
apart from outcomes), features, whether anything is fitted, staking, settlement, costs, the primary and
secondary metrics, the uncertainty method, the variants and the features each may see, the stopping rule and
the seeds. validate_contract reports every problem, never just the first, as sorted "<path>:<reason>" codes;
an empty list means valid. Unknown keys are refused at every level, set-like lists must be sorted and unique
and features, criteria and variants sorted by name and id (so one meaning has one hash), a bool is never a
number, and a fraction field must be a JSON float other than -0.0 (so 0 and 0.0 cannot give two hashes).

Draft and frozen: a draft may leave exactly the DRAFT_NULLABLE fields null (the intent delay, the trial
budget, and a registered hypothesis's hypothesis, ROI threshold and decision rule) and must leave frozen_on
null; a frozen contract leaves nothing null and carries a frozen_on date no earlier than SCHEMA_V1_DATE. A
frozen contract also needs at least one exclusion, no placeholder text (PLACEHOLDER_WORDS, PLACEHOLDER_TEXTS)
in any text field, and at least MIN_FROZEN_TEXT characters in the rules a reader must be able to apply
(FROZEN_RULE_TEXTS); this is a heuristic against an empty freeze, not a check that the text is meaningful.

registration.kind says what the contract is for and fixes the kind of its financial_rule
(FINANCIAL_RULE_FOR_KIND). An engineering_check evaluates the engineering split only, supports no edge claim
and carries financial_rule not_applicable with a reason: no financial hypothesis, ROI threshold or decision
rule exists for it, and none is invented to pass the freeze. A registered_hypothesis evaluates the research
split and carries roi_threshold: its hypothesis, ROI threshold and decision rule, all required at freeze.
Every contract carries engineering_acceptance: each of the deterministic ENGINEERING_CRITERIA (known
accounting answers, prefix invariance, issuance/exposure reconciliation) with a definition, and a rule that
is one of ENGINEERING_RULES (every criterion must hold; outcome metrics such as ROI are reported, never
judged). The rule is a fixed token, so no wording can bring an ROI pass condition back through it. The
criteria's definitions are free text, checked only for placeholders and length, like feature definitions: the
freeze review, not this validator, keeps an outcome condition out of them. The operational choices (delay,
costs, cohort, variants, seeds, trial budget and stopping rule) are mandatory at freeze for both kinds;
nothing here lets a draft pass as frozen.

The holdout split must be exactly SEALED_HOLDOUT, no other split may overlap it, it may not contain an
already-inspected month (INSPECTED_MONTHS), and it cannot be evaluated under schema_version 1 at all; changing
any of that is a schema change. Outcome-bearing data downloaded before any freeze is recorded with its file
hash, the known minimum that was viewed and an unknown full inspection extent (deleting a file does not restore
untouched status): EXPOSED_MONTHS by month, and NOT_PROSPECTIVE_DATES by race date, never prospective evidence
(v1 has no prospective split, so it is the record later schemas and the trial ledger start from). No split may
hold an exposed month or the month of a not-prospective date (exposed_not_holdout for the holdout,
exposed_exploratory_only for the others): that data is exploratory only. Neither record shows that nobody saw
the sealed months: they are sealed because this project's record says they were never downloaded, not because
an audit proved it.

Decision time is first_published_scheduled_off plus a negative offset_s, over the observed prefix only; the
intent takes effect delay_s later, still before that scheduled off. A timer at time T sees the equal-pt batch
at T (timer_after_equal_pt_batch), and a message carrying img replaces the market state. Retrospective anchors
(actual off, the last pre-off update, closing, in-play, results, BSP reconciliation, a revised or latest
schedule) are refused: they are known only after the decision they would time. Every feature is computed as
of decision time with a declared lookback_s, and every variant names the declared features it uses
(uses_features), so a runner can expose only those. Free-text definitions and descriptions are not checked
for leakage: the runtime observed-prefix view, not this validator, is the guard. fitting.kind is none in v1
(no parameter is estimated from outcomes); walk-forward fitting is deferred.

Capabilities come in two machine-readable classes. capabilities.decision lists decision-time inputs
(DECISION_CAPABILITIES): one a market or runner lacks in its observed prefix at decision time becomes an
explicit unsupported/abstain reason (on_missing unsupported_abstain), never a zero, and features may draw only
on these. capabilities.outcome lists outcomes (OUTCOME_CAPABILITIES: final BSP and results), which only settle
bets: a missing or later-revised outcome never changes an earlier decision, so the bet stays issued with its
exposure and its settlement is unresolved (on_missing unresolved_settlement). Abstaining on a missing outcome
is refused because it would make the population depend on outcome availability. cohort.exclusions and the
variant descriptions are free text and cannot override this: a runner takes abstain and unsupported reasons
only from decision-time information (the cohort's fixed reasons in docs/backtest-plan.md section 6, a missing
capabilities.decision input, or a variant's own rule over its declared features), never from a missing or
revised outcome. A whole data product (a provider or tier) whose specification or audit shows a capability
absent across the product, judged without reference to which markets have outcomes, may be refused before the
freeze by choosing another in the contract; within an accepted product, one market's missing outcome never
becomes a strategy abstention.

P0 staking is fixed-stake BACK at BSP with unconditional pre-off acceptance and assumed negligible impact;
LAY and limit-on-close are deferred. Every settlement case P0 cannot price (void, a removal after the
decision, a dead heat, a result revised after close, a missing BSP or result) is unresolved, which is the only
rule v1 allows. Commission applies to positive net market winnings at a declared rate and rounding;
marketBaseRate alone does not establish an account's effective charge, so it is not a rate source. Metrics
are per variant; a candidate is never netted against a baseline. With N_R the net of resolved bets, S_R their
stakes and S_U the stakes of unresolved bets, the primary metric is resolved ROI = N_R / S_R, which is
conditional on settlement being available, so its sign alone says nothing about every issued bet. Beside it
coverage, unresolved exposure and roi_worst_case_unresolved_lost must be reported: the pessimistic all-issued
ROI (N_R - S_U) / (S_R + S_U), every unresolved bet lost, exact for P0's one BACK per market. A zero
denominator leaves a ROI undefined, never zero. Uncertainty is a block bootstrap over race days or longer
blocks after netting each market. CLV against BSP is refused as a metric: it is zero by construction for an
entry at BSP. Brier score and log loss are deferred: no v1 variant declares a forecast.

What a valid contract establishes: that these choices were written in this shape, with this hash. It does not
show when they were written relative to anyone looking at outcomes (frozen_on cannot be checked against a
clock), that the files exist or carry the declared capabilities, or that anything is profitable. The sha256
is over canonical JSON as Python's json module writes it (sorted keys in code-point order, compact
separators, UTF-8, floats by repr, no NaN), the same bytes receipts.canonical_json produces; it is not RFC 8785,
so a verifier in another language must reproduce Python's float formatting. receipts is not imported because
its git_state runs a subprocess, which the backtest boundary forbids. Only an invalid status's sha256 is not
for a ledger: ledger writers must check status == valid_frozen first. Standard library only.
"""

from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re

SCHEMA_VERSION = 1
SCHEMA_V1_DATE = "2026-10-14"
MAX_CONTRACT_BYTES = 1 << 20
JSON_INT_RANGE = (-(2 ** 63), 2 ** 63 - 1)
TOP_LEVEL_KEYS = ("schema_version", "experiment_id", "status", "frozen_on", "registration", "financial_rule",
                  "engineering_acceptance", "population", "cohort", "decision_timing", "capabilities", "features",
                  "fitting", "staking", "settlement", "costs", "primary_metric", "secondary_metrics", "uncertainty",
                  "variants", "stopping_rule", "seeds")
# Operational choices (delay_s, max_trials) stay nullable only in a draft; so do a registered hypothesis's
# financial fields. An engineering check has no financial fields to leave null: its rule is typed not_applicable.
DRAFT_NULLABLE = ("decision_timing.delay_s", "financial_rule.decision_rule", "financial_rule.decision_threshold",
                  "financial_rule.hypothesis", "stopping_rule.max_trials")
STATUSES = ("draft", "frozen")
RESULT_STATUSES = ("valid_draft", "valid_frozen", "invalid")
REGISTRATION_KINDS = ("engineering_check", "registered_hypothesis")
# Each registration kind evaluates exactly one split: engineering outcomes count as inspected from the first run.
SPLIT_FOR_KIND = {"engineering_check": "engineering", "registered_hypothesis": "research"}
# The financial rule each kind must carry, and the keys of each rule kind.
FINANCIAL_RULE_FOR_KIND = {"engineering_check": "not_applicable", "registered_hypothesis": "roi_threshold"}
FINANCIAL_RULE_KEYS = {"not_applicable": ("kind", "reason"),
                       "roi_threshold": ("kind", "hypothesis", "decision_threshold", "decision_rule")}
# Deterministic engineering acceptance every contract states, judged without looking at any observed ROI:
# hand-computed accounting answers reproduced exactly, earlier decisions unchanged by later or removed data,
# and the issuance ledger reconciling with issued, resolved and unresolved stakes.
ENGINEERING_CRITERIA = ("issuance_exposure_reconciliation", "known_accounting_answers", "prefix_invariance")
# How the criteria combine, as a token rather than text: every criterion must hold, and outcome metrics (ROI, net
# P&L, drawdown) are reported but can neither pass nor fail the run.
ENGINEERING_RULES = ("all_criteria_must_hold_outcome_metrics_not_judged",)

# Frozen-text heuristics: a word anywhere, or a whole text, that marks an unfinished field.
PLACEHOLDER_WORDS = ("fixme", "lorem", "placeholder", "tba", "tbc", "tbd", "todo", "xxx")
PLACEHOLDER_TEXTS = ("-", "--", ".", "...", "?", "n/a", "na", "none", "null", "x")
MIN_FROZEN_TEXT = 20
FROZEN_RULE_TEXTS = ("cohort.market_rule", "cohort.runner_rule", "engineering_acceptance.criteria[].definition",
                     "features[].definition", "financial_rule.decision_rule", "financial_rule.hypothesis",
                     "financial_rule.reason", "stopping_rule.rule", "variants[].description")

PROVIDERS = ("betfair_historical_data",)
TIERS = ("BASIC", "ADVANCED", "PRO")
SPORTS = ("horse_racing",)
NOT_IN_SCHEMA_V1_SPORTS = ("greyhound_racing", "soccer")
COUNTRIES = ("GB", "IE")
MARKET_TYPES = ("WIN",)
SPLITS = ("engineering", "research", "holdout")
EVALUABLE_SPLITS = ("engineering", "research")
HOLDOUT_STATES = ("sealed_not_downloaded",)
# The months deliberately not downloaded (docs/backtest-plan.md section 3; Astra's technical decision of
# 2026-10-14, adopted by Claude, narrowed it from the superseded 2026-01..2026-09), and months already inspected.
SEALED_HOLDOUT = ("2026-01", "2026-08")
INSPECTED_MONTHS = ("2017-04",)
# Months whose outcome-bearing data was downloaded before any freeze: in no split, exploratory only. Deleting the
# file did not restore untouched status. Outcome rows are known to have been seen (known_minimum); how much more
# research read was not recorded (inspection_extent).
EXPOSED_MONTHS = {
    "2026-09": {"file": "uk_ie_2026-09.csv", "source": "Betfair Data Scientists hub, with WIN_BSP and WIN_RESULT",
                "sha256": "8e999e30a7fc17b2dd981cc8d223071b63d94797fbe08884d7ef67d0ec22ec92",
                "deleted_on": "2026-10-14", "inspection_extent": "unknown",
                "known_minimum": "header and first one to three rows viewed 2026-10-14 for the plan"},
}
# Race dates whose outcome-bearing data was downloaded before any freeze: never prospective evidence, and their
# month is in no split.
NOT_PROSPECTIVE_DATES = {
    "2026-10-12": {"file": "sample_ukwin_13102026.csv", "source": "Betfair BSP CSV (promo.betfair.com)",
                   "sha256": "e1bdc0816bac5ebac2a1fdefa4a46f661cfb65ab0fbbd080ab3dc3545c5e2244",
                   "deleted_on": "2026-10-14", "inspection_extent": "unknown",
                   "known_minimum": "header and first one to three rows viewed 2026-10-14 for the plan"},
}

ANCHORS = ("first_published_scheduled_off",)
# Substrings that mark an anchor known only after the decision it would time (refused as "retrospective").
RETROSPECTIVE_MARKERS = ("actual", "last", "latest", "final", "revised", "close", "suspend", "in_play", "inplay",
                         "turn", "result", "settle", "reconcil", "bsp", "winner", "post")
DECISION_VIEWS = ("observed_prefix_only",)
OFFSET_RANGE_S = (-86400, -1)
DELAY_RANGE_S = (0, 3600)
EQUAL_PT_ORDERS = ("timer_after_equal_pt_batch",)
IMAGE_HANDLING = ("img_replaces_market_state",)

# Decision-time inputs (missing in the observed prefix at decision time: unsupported/abstain) and outcomes
# (missing or revised: the earlier decision stands and its settlement is unresolved).
DECISION_CAPABILITIES = ("all_offers", "best_offers", "ltp", "market_definition", "sp_projected", "sp_traded",
                         "traded_ladder", "traded_volume")
OUTCOME_CAPABILITIES = ("final_bsp", "results")
CAPABILITIES = tuple(sorted(DECISION_CAPABILITIES + OUTCOME_CAPABILITIES))
CAPABILITY_CLASSES = ("decision", "outcome")
ON_MISSING = {"decision": ("unsupported_abstain",), "outcome": ("unresolved_settlement",)}
# The other class's policy, refused with its reason.
REFUSED_ON_MISSING = {"decision": {"unresolved_settlement": "not_for_decision_inputs"},
                      "outcome": {"unsupported_abstain": "would_change_an_earlier_decision"}}
# A capability listed in the wrong class.
MISCLASSED = {"decision": dict.fromkeys(OUTCOME_CAPABILITIES, "is_an_outcome"),
              "outcome": dict.fromkeys(DECISION_CAPABILITIES, "is_a_decision_input")}
FEATURE_AS_OF = ("decision_time",)
LOOKBACK_RANGE_S = (0, 86400)
FITTING_KINDS = ("none",)
DEFERRED_FITTING = ("walk_forward",)

SIDES = ("BACK",)
PRICES = ("BSP",)
DEFERRED_STAKING = {"side": ("LAY",), "price": ("LIMIT", "LIMIT_ON_CLOSE")}
ACCEPTANCE = ("unconditional_pre_off",)
MARKET_IMPACT = ("assumed_negligible",)
STAKE_RANGE_PENCE = (1, 100000)
SETTLEMENT_CASES = ("dead_heat", "market_void", "missing_bsp_or_result", "other_runner_removed_after_decision",
                    "own_runner_removed_after_decision", "result_revised_after_close")
SETTLEMENT_RULES = ("unresolved",)

RATE_SOURCES = ("declared_assumption", "account_statement_verified")
COMMISSION_BASES = ("positive_net_market_winnings",)
ROUNDING = ("per_market_ceil_penny", "per_market_half_up_penny")

PRIMARY_METRICS = ("roi",)
PRIMARY_DEFINITIONS = ("sum_net_over_sum_stakes_resolved",)
# A BACK bet loses at most its stake and commission only touches winnings, so ROI >= -1; prices stop at 1000.
ROI_THRESHOLD_RANGE = (-1.0, 999.0)
SECONDARY_METRICS = ("abstentions_by_reason", "bet_count", "coverage", "ltp_to_bsp_movement_non_executable",
                     "max_drawdown_global_chronology", "net_pnl", "roi_worst_case_unresolved_lost", "total_stakes",
                     "unresolved_exposure")
REQUIRED_SECONDARY = ("coverage", "roi_worst_case_unresolved_lost", "unresolved_exposure")
REFUSED_METRICS = {"clv_vs_bsp": "zero_by_construction_for_bsp_entry",
                   "brier_score": "deferred_no_forecast_output", "log_loss": "deferred_no_forecast_output"}

BOOTSTRAP_METHODS = ("block_bootstrap",)
NET_BY = ("market",)
BLOCKS = ("race_day", "iso_week", "calendar_month")
SHORT_BLOCKS = ("bet", "runner", "market", "race")
RESAMPLE_RANGE = (100, 1000000)

VARIANT_ROLES = ("candidate", "baseline")
MAX_TRIALS_RANGE = (1, 10000)
SEED_RANGE = (0, 2 ** 63 - 1)

_EXPERIMENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,63}")
_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MONTH_RE = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_KEY_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")
_INDEX_RE = re.compile(r"\[[0-9]+\]")
_PLACEHOLDER_RE = re.compile(r"\b(?:" + "|".join(PLACEHOLDER_WORDS) + r")\b", re.IGNORECASE)


class ContractFileError(ValueError):
    """A contract file that cannot be read or parsed; str() is a short code."""


def canonical_json(value):
    """Sorted keys, compact separators, UTF-8, NaN and infinity refused (receipts.canonical_json's bytes)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def contract_sha256(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _refuse_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ContractFileError("duplicate_key")
        seen[key] = value
    return seen


def _refuse_constant(name):
    raise ContractFileError("non_finite_number")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ContractFileError("non_finite_number")
    return value


def _bounded_int(text):
    """A JSON integer literal within JSON_INT_RANGE; longer literals are refused before int() parses them."""
    if len(text) > 20:
        raise ContractFileError("number_too_large")
    value = int(text)
    if not JSON_INT_RANGE[0] <= value <= JSON_INT_RANGE[1]:
        raise ContractFileError("number_too_large")
    return value


def load_contract(path):
    """The parsed JSON value of one local regular file; ContractFileError(code) if unreadable, not a regular file
    (a FIFO or device would block or never end; checked before opening), too large or not strict JSON.

    Strict means UTF-8, no duplicate keys, no NaN or Infinity, no float that overflows and no integer outside
    JSON_INT_RANGE."""
    try:
        target = Path(path)
        if not target.exists():
            raise ContractFileError("unreadable")
        if not target.is_file():
            raise ContractFileError("not_a_regular_file")
        with open(target, "rb") as handle:
            data = handle.read(MAX_CONTRACT_BYTES + 1)
    except OSError:
        raise ContractFileError("unreadable") from None
    if len(data) > MAX_CONTRACT_BYTES:
        raise ContractFileError("too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ContractFileError("not_utf8") from None
    try:
        return json.loads(text, object_pairs_hook=_refuse_duplicates, parse_constant=_refuse_constant,
                          parse_float=_finite_float, parse_int=_bounded_int)
    except ContractFileError:
        raise
    except (ValueError, RecursionError):
        raise ContractFileError("not_json") from None


def _join(path, key):
    key = key if isinstance(key, str) and _KEY_RE.fullmatch(key) else "<malformed_key>"
    return f"{path}.{key}" if path else key


def _pattern(path):
    """A field path with list indices dropped (features[0].definition -> features[].definition)."""
    return _INDEX_RE.sub("[]", path)


def _is_int(value):
    return type(value) is int


def _month(text):
    """(year, month) of a YYYY-MM text, or None; year 0000 is not a calendar year, so it is refused."""
    match = _MONTH_RE.fullmatch(text) if isinstance(text, str) else None
    if not match or int(match.group(1)) < 1:
        return None
    return int(match.group(1)), int(match.group(2))


def _placeholder(text):
    core = text.strip().casefold()
    return core in PLACEHOLDER_TEXTS or not any(c.isalnum() for c in core) or bool(_PLACEHOLDER_RE.search(core))


class _Checker:
    """Collects "<path>:<reason>" codes for one contract. A status other than "draft" is judged as frozen."""

    def __init__(self, contract):
        self.problems = set()
        self.draft = isinstance(contract, dict) and contract.get("status") == "draft"

    def add(self, path, reason):
        self.problems.add(f"{path}:{reason}")

    def fields(self, value, path, keys):
        """value if it is an object (its unknown and missing keys recorded), else None."""
        if not isinstance(value, dict):
            self.add(path or "contract", "not_an_object")
            return None
        for key in value:
            if key not in keys:
                self.add(_join(path, key), "unknown_key")
        for key in keys:
            if key not in value:
                self.add(_join(path, key), "missing")
        return value

    def get(self, obj, path, key):
        """(True, value) when there is a non-null value to check; a null is judged here against DRAFT_NULLABLE."""
        if obj is None or key not in obj:
            return False, None
        full = _join(path, key)
        if obj[key] is None:
            if _pattern(full) not in DRAFT_NULLABLE:
                self.add(full, "null")
            elif not self.draft:
                self.add(full, "null_in_frozen")
            return False, None
        return True, obj[key]

    def text(self, obj, path, key):
        ok, value = self.get(obj, path, key)
        if not ok:
            return None
        full = _join(path, key)
        if not (isinstance(value, str) and value.strip()):
            self.add(full, "not_text")
            return None
        self.frozen_text(full, value)
        return value

    def frozen_text(self, full, value):
        if self.draft:
            return
        if _placeholder(value):
            self.add(full, "placeholder_in_frozen")
        elif _pattern(full) in FROZEN_RULE_TEXTS and len(value.strip()) < MIN_FROZEN_TEXT:
            self.add(full, "too_short_in_frozen")

    def choice(self, obj, path, key, allowed, refused=None):
        ok, value = self.get(obj, path, key)
        if not ok:
            return None
        if isinstance(value, str) and value in allowed:
            return value
        reason = (refused or {}).get(value) if isinstance(value, str) else None
        self.add(_join(path, key), reason or "not_allowed")
        return None

    def integer(self, obj, path, key, bounds):
        ok, value = self.get(obj, path, key)
        if not ok:
            return None
        if not _is_int(value):
            self.add(_join(path, key), "not_integer")
            return None
        if not bounds[0] <= value <= bounds[1]:
            self.add(_join(path, key), "out_of_range")
            return None
        return value

    def fraction(self, obj, path, key, low, high, *, low_inclusive=True):
        """A JSON float, not -0.0, with low <= value < high (low < value when not low_inclusive)."""
        ok, value = self.get(obj, path, key)
        if not ok:
            return None
        full = _join(path, key)
        if type(value) is not float or not math.isfinite(value):
            self.add(full, "not_float")
            return None
        if value == 0 and math.copysign(1.0, value) < 0:
            self.add(full, "negative_zero")
            return None
        if value < low or (value == low and not low_inclusive) or value >= high:
            self.add(full, "out_of_range")
            return None
        return value

    def name_set(self, obj, path, key, allowed, refused=None, *, allow_empty, unknown="not_allowed"):
        """A sorted, duplicate-free list of allowed names; the valid names found, or None if there is no list."""
        ok, value = self.get(obj, path, key)
        if not ok:
            return None
        full = _join(path, key)
        if not isinstance(value, list):
            self.add(full, "not_a_list")
            return None
        if not value and not allow_empty:
            self.add(full, "empty")
        names = []
        for item in value:
            if isinstance(item, str) and item in allowed:
                names.append(item)
            else:
                reason = (refused or {}).get(item) if isinstance(item, str) else None
                named = isinstance(item, str) and _NAME_RE.fullmatch(item)
                self.add(full, f"{item}_{reason}" if reason else unknown if named else "not_allowed")
        if all(isinstance(item, str) for item in value) and value != sorted(set(value)):
            self.add(full, "not_sorted_unique")
        return names

    def objects(self, obj, path, key, keys, *, allow_empty):
        """[(path, object)] for a list of objects with exactly keys."""
        ok, value = self.get(obj, path, key)
        if not ok:
            return []
        full = _join(path, key)
        if not isinstance(value, list):
            self.add(full, "not_a_list")
            return []
        if not value and not allow_empty:
            self.add(full, "empty")
        found = []
        for index, item in enumerate(value):
            item_path = f"{full}[{index}]"
            if self.fields(item, item_path, keys) is not None:
                found.append((item_path, item))
        return found

    def identifiers(self, found, path, key, sort_reason):
        """The valid, unique identifiers under key in found, in order; refuses a list not sorted by them."""
        idents = []
        for item_path, item in found:
            ok, ident = self.get(item, item_path, key)
            if ok and not (isinstance(ident, str) and _NAME_RE.fullmatch(ident)):
                self.add(f"{item_path}.{key}", "malformed")
            elif ok and ident in idents:
                self.add(f"{item_path}.{key}", "duplicate")
            elif ok:
                idents.append(ident)
        if idents != sorted(idents):
            self.add(path, sort_reason)
        return idents

    # Sections.

    def contract(self, contract):
        top = self.fields(contract, "", TOP_LEVEL_KEYS)
        if top is None:
            return
        ok, version = self.get(top, "", "schema_version")
        if ok and not (_is_int(version) and version == SCHEMA_VERSION):
            self.add("schema_version", "unsupported")
        ok, experiment_id = self.get(top, "", "experiment_id")
        if ok and not (isinstance(experiment_id, str) and _EXPERIMENT_ID_RE.fullmatch(experiment_id)):
            self.add("experiment_id", "malformed")
        self.choice(top, "", "status", STATUSES)
        self.frozen_on(top)
        registration = self.section(top, "registration", ("kind", "note"))
        kind = self.choice(registration, "registration", "kind", REGISTRATION_KINDS)
        self.text(registration, "registration", "note")
        self.financial_rule(top, kind)
        self.engineering_acceptance(top)
        self.population(top, kind)
        self.cohort(top)
        self.decision_timing(top)
        decision, outcome = self.capabilities(top)
        features = self.features(top, decision)
        fitting = self.section(top, "fitting", ("kind",))
        self.choice(fitting, "fitting", "kind", FITTING_KINDS, dict.fromkeys(DEFERRED_FITTING, "deferred"))
        self.staking(top, outcome)
        settlement = self.section(top, "settlement", SETTLEMENT_CASES)
        for case in SETTLEMENT_CASES:
            self.choice(settlement, "settlement", case, SETTLEMENT_RULES)
        self.costs(top)
        self.metrics(top)
        self.uncertainty(top)
        variants = self.variants(top, features)
        self.stopping_rule(top, variants)
        seeds = self.section(top, "seeds", ("selection", "bootstrap"))
        for key in ("selection", "bootstrap"):
            self.integer(seeds, "seeds", key, SEED_RANGE)

    def section(self, top, key, keys):
        """The named sub-object if present and an object (null and shape problems recorded), else None."""
        ok, value = self.get(top, "", key)
        return self.fields(value, key, keys) if ok else None

    def frozen_on(self, top):
        if "frozen_on" not in top:
            return
        value = top["frozen_on"]
        if self.draft:
            if value is not None:
                self.add("frozen_on", "not_null_in_draft")
            return
        if value is None:
            self.add("frozen_on", "null_in_frozen")
            return
        try:
            valid = isinstance(value, str) and bool(_DATE_RE.fullmatch(value)) and bool(date.fromisoformat(value))
        except ValueError:
            valid = False
        if not valid:
            self.add("frozen_on", "malformed")
        elif value < SCHEMA_V1_DATE:
            self.add("frozen_on", "before_schema_v1")

    def financial_rule(self, top, kind):
        """The typed financial rule: its keys follow its kind, and its kind must be the registration kind's."""
        ok, rule = self.get(top, "", "financial_rule")
        if not ok:
            return
        if not isinstance(rule, dict):
            self.add("financial_rule", "not_an_object")
            return
        if "kind" not in rule:
            self.add("financial_rule.kind", "missing")
        rule_kind = self.choice(rule, "financial_rule", "kind", tuple(FINANCIAL_RULE_KEYS))
        if rule_kind is None:                                   # keys cannot be judged; only refuse unknown ones
            known = {key for keys in FINANCIAL_RULE_KEYS.values() for key in keys}
            for key in rule:
                if key not in known:
                    self.add(_join("financial_rule", key), "unknown_key")
            return
        self.fields(rule, "financial_rule", FINANCIAL_RULE_KEYS[rule_kind])
        if kind is not None and FINANCIAL_RULE_FOR_KIND[kind] != rule_kind:
            self.add("financial_rule.kind", f"{FINANCIAL_RULE_FOR_KIND[kind]}_required_for_{kind}")
        if rule_kind == "not_applicable":
            self.text(rule, "financial_rule", "reason")
            return
        self.text(rule, "financial_rule", "hypothesis")
        self.fraction(rule, "financial_rule", "decision_threshold", *ROI_THRESHOLD_RANGE)
        self.text(rule, "financial_rule", "decision_rule")

    def engineering_acceptance(self, top):
        """Every ENGINEERING_CRITERIA name once, sorted, each with a definition, and an ENGINEERING_RULES token."""
        path = "engineering_acceptance"
        section = self.section(top, path, ("criteria", "rule"))
        found = self.objects(section, path, "criteria", ("name", "definition"), allow_empty=False)
        names = self.identifiers(found, f"{path}.criteria", "name", "not_sorted_by_name")
        for item_path, criterion in found:
            name = criterion.get("name")
            if isinstance(name, str) and _NAME_RE.fullmatch(name) and name not in ENGINEERING_CRITERIA:
                self.add(f"{item_path}.name", "not_allowed")
            self.text(criterion, item_path, "definition")
        if section is not None and isinstance(section.get("criteria"), list):
            for needed in ENGINEERING_CRITERIA:
                if needed not in names:
                    self.add(f"{path}.criteria", f"missing_{needed}")
        self.choice(section, path, "rule", ENGINEERING_RULES)

    def population(self, top, kind):
        keys = ("provider", "tier", "sport", "countries", "market_types", "splits", "evaluate_split")
        population = self.section(top, "population", keys)
        path = "population"
        self.choice(population, path, "provider", PROVIDERS)
        self.choice(population, path, "tier", TIERS)
        self.choice(population, path, "sport", SPORTS, dict.fromkeys(NOT_IN_SCHEMA_V1_SPORTS, "not_in_schema_v1"))
        self.name_set(population, path, "countries", COUNTRIES, allow_empty=False)
        self.name_set(population, path, "market_types", MARKET_TYPES, allow_empty=False)
        split = self.choice(population, path, "evaluate_split", EVALUABLE_SPLITS, {"holdout": "sealed"})
        if kind is not None and split is not None and SPLIT_FOR_KIND[kind] != split:
            self.add("population.evaluate_split", f"not_{SPLIT_FOR_KIND[kind]}_for_{kind}")
        ok, splits = self.get(population, path, "splits")
        splits = self.fields(splits, "population.splits", SPLITS) if ok else None
        ranges = {}
        for name in SPLITS:
            span = self.split(splits, name)
            if span is not None:
                ranges[name] = span
        names = [name for name in SPLITS if name in ranges]
        for i, first in enumerate(names):
            for second in names[i + 1:]:
                (a0, a1), (b0, b1) = ranges[first], ranges[second]
                if a0 <= b1 and b0 <= a1:
                    self.add("population.splits", f"{first}_overlaps_{second}")
        if "holdout" in ranges and any(ranges[name][1] >= ranges["holdout"][0] for name in names if name != "holdout"):
            self.add("population.splits.holdout", "not_after_other_splits")
        sealed = tuple(_month(month) for month in SEALED_HOLDOUT)
        exposed = {_month(month) for month in EXPOSED_MONTHS} | {_month(day[:7]) for day in NOT_PROSPECTIVE_DATES}
        for name, (start, end) in ranges.items():
            if any(start <= month <= end for month in exposed):
                self.add(f"population.splits.{name}",
                         "exposed_not_holdout" if name == "holdout" else "exposed_exploratory_only")
            if name == "holdout":
                if (start, end) != sealed:
                    self.add("population.splits.holdout", "not_the_sealed_holdout")
                if any(start <= _month(month) <= end for month in INSPECTED_MONTHS):
                    self.add("population.splits.holdout", "inspected_not_holdout")
            elif start <= sealed[1] and sealed[0] <= end:
                self.add(f"population.splits.{name}", "overlaps_sealed_holdout")

    def split(self, splits, name):
        """(start, end) months of one split, or None; the holdout also records its sealed state."""
        path = f"population.splits.{name}"
        ok, value = self.get(splits, "population.splits", name)
        keys = ("start_month", "end_month", "state") if name == "holdout" else ("start_month", "end_month")
        span = self.fields(value, path, keys) if ok else None
        if span is None:
            return None
        if name == "holdout":
            self.choice(span, path, "state", HOLDOUT_STATES)
        months = []
        for key in ("start_month", "end_month"):
            ok, text = self.get(span, path, key)
            month = _month(text) if ok else None
            if ok and month is None:
                self.add(_join(path, key), "malformed")
            months.append(month)
        if None in months:
            return None
        if months[0] > months[1]:
            self.add(path, "start_after_end")
            return None
        return months[0], months[1]

    def cohort(self, top):
        cohort = self.section(top, "cohort", ("market_rule", "runner_rule", "exclusions"))
        self.text(cohort, "cohort", "market_rule")
        self.text(cohort, "cohort", "runner_rule")
        ok, exclusions = self.get(cohort, "cohort", "exclusions")
        if not ok:
            return
        if not isinstance(exclusions, list):
            self.add("cohort.exclusions", "not_a_list")
            return
        if not exclusions and not self.draft:
            self.add("cohort.exclusions", "empty_in_frozen")
        for index, item in enumerate(exclusions):
            if not (isinstance(item, str) and item.strip()):
                self.add(f"cohort.exclusions[{index}]", "not_text")
            else:
                self.frozen_text(f"cohort.exclusions[{index}]", item)

    def decision_timing(self, top):
        keys = ("anchor", "offset_s", "view", "delay_s", "equal_pt_order", "image_handling")
        timing = self.section(top, "decision_timing", keys)
        ok, anchor = self.get(timing, "decision_timing", "anchor")
        if ok and not (isinstance(anchor, str) and anchor in ANCHORS):
            retrospective = isinstance(anchor, str) and any(m in anchor.lower() for m in RETROSPECTIVE_MARKERS)
            self.add("decision_timing.anchor", "retrospective" if retrospective else "not_allowed")
        ok, offset = self.get(timing, "decision_timing", "offset_s")
        if ok:
            if not _is_int(offset):
                self.add("decision_timing.offset_s", "not_integer")
                offset = None
            elif offset > OFFSET_RANGE_S[1]:
                self.add("decision_timing.offset_s", "not_negative")
                offset = None
            elif offset < OFFSET_RANGE_S[0]:
                self.add("decision_timing.offset_s", "out_of_range")
                offset = None
        self.choice(timing, "decision_timing", "view", DECISION_VIEWS)
        delay = self.integer(timing, "decision_timing", "delay_s", DELAY_RANGE_S)
        if offset is not None and delay is not None and offset + delay >= 0:
            self.add("decision_timing.delay_s", "not_before_scheduled_off")
        self.choice(timing, "decision_timing", "equal_pt_order", EQUAL_PT_ORDERS)
        self.choice(timing, "decision_timing", "image_handling", IMAGE_HANDLING)

    def capabilities(self, top):
        """(decision names, outcome names), each None when its list cannot be read."""
        section = self.section(top, "capabilities", CAPABILITY_CLASSES)
        found = {}
        for cls, allowed in zip(CAPABILITY_CLASSES, (DECISION_CAPABILITIES, OUTCOME_CAPABILITIES)):
            path = f"capabilities.{cls}"
            ok, value = self.get(section, "capabilities", cls)
            value = self.fields(value, path, ("required", "on_missing")) if ok else None
            self.choice(value, path, "on_missing", ON_MISSING[cls], REFUSED_ON_MISSING[cls])
            names = self.name_set(value, path, "required", allowed, MISCLASSED[cls], allow_empty=False)
            found[cls] = None if names is None else set(names)
        # The decision anchor needs market definitions; settlement needs results.
        for cls, needed in (("decision", "market_definition"), ("outcome", "results")):
            if found[cls] is not None and needed not in found[cls]:
                self.add(f"capabilities.{cls}.required", f"missing_{needed}")
        return found["decision"], found["outcome"]

    def features(self, top, decision):
        """The valid declared feature names; each feature draws on one required decision-time capability."""
        keys = ("name", "capability", "as_of", "lookback_s", "definition")
        found = self.objects(top, "", "features", keys, allow_empty=True)
        names = self.identifiers(found, "features", "name", "not_sorted_by_name")
        for path, feature in found:
            capability = self.choice(feature, path, "capability", CAPABILITIES)
            if capability in OUTCOME_CAPABILITIES:
                self.add(f"{path}.capability", "outcome_not_a_feature")
            elif capability is not None and decision is not None and capability not in decision:
                self.add(f"{path}.capability", "not_required")
            self.choice(feature, path, "as_of", FEATURE_AS_OF)
            self.integer(feature, path, "lookback_s", LOOKBACK_RANGE_S)
            self.text(feature, path, "definition")
        return set(names)

    def staking(self, top, outcome):
        staking = self.section(top, "staking", ("side", "price", "acceptance", "market_impact", "stake_pence"))
        self.choice(staking, "staking", "side", SIDES, dict.fromkeys(DEFERRED_STAKING["side"], "deferred"))
        price = self.choice(staking, "staking", "price", PRICES, dict.fromkeys(DEFERRED_STAKING["price"], "deferred"))
        self.choice(staking, "staking", "acceptance", ACCEPTANCE)
        self.choice(staking, "staking", "market_impact", MARKET_IMPACT)
        self.integer(staking, "staking", "stake_pence", STAKE_RANGE_PENCE)
        if price == "BSP" and outcome is not None and "final_bsp" not in outcome:
            self.add("capabilities.outcome.required", "missing_final_bsp")

    def costs(self, top):
        costs = self.section(top, "costs", ("commission_rate", "rate_source", "basis", "rounding", "note"))
        self.fraction(costs, "costs", "commission_rate", 0.0, 1.0)
        self.choice(costs, "costs", "rate_source", RATE_SOURCES, {"market_base_rate": "not_effective_charge"})
        self.choice(costs, "costs", "basis", COMMISSION_BASES)
        self.choice(costs, "costs", "rounding", ROUNDING)
        self.text(costs, "costs", "note")

    def metrics(self, top):
        primary = self.section(top, "primary_metric", ("name", "definition"))
        self.choice(primary, "primary_metric", "name", PRIMARY_METRICS)
        self.choice(primary, "primary_metric", "definition", PRIMARY_DEFINITIONS)
        names = self.name_set(top, "", "secondary_metrics", SECONDARY_METRICS, REFUSED_METRICS, allow_empty=False)
        for needed in REQUIRED_SECONDARY if names is not None else ():
            if needed not in names:
                self.add("secondary_metrics", f"missing_{needed}")

    def uncertainty(self, top):
        keys = ("method", "net_by", "block", "resamples", "confidence_level")
        section = self.section(top, "uncertainty", keys)
        self.choice(section, "uncertainty", "method", BOOTSTRAP_METHODS)
        self.choice(section, "uncertainty", "net_by", NET_BY)
        self.choice(section, "uncertainty", "block", BLOCKS, dict.fromkeys(SHORT_BLOCKS, "shorter_than_race_day"))
        self.integer(section, "uncertainty", "resamples", RESAMPLE_RANGE)
        self.fraction(section, "uncertainty", "confidence_level", 0.0, 1.0, low_inclusive=False)

    def variants(self, top, features):
        found = self.objects(top, "", "variants", ("id", "role", "uses_features", "description"), allow_empty=False)
        ids = self.identifiers(found, "variants", "id", "not_sorted_by_id")
        for path, variant in found:
            self.choice(variant, path, "role", VARIANT_ROLES)
            self.name_set(variant, path, "uses_features", features, allow_empty=True, unknown="undeclared_feature")
            self.text(variant, path, "description")
        return ids

    def stopping_rule(self, top, variants):
        section = self.section(top, "stopping_rule", ("max_trials", "rule"))
        max_trials = self.integer(section, "stopping_rule", "max_trials", MAX_TRIALS_RANGE)
        if max_trials is not None and max_trials < len(variants):
            self.add("stopping_rule.max_trials", "fewer_than_variants")
        self.text(section, "stopping_rule", "rule")


def validate_contract(contract):
    """(problems, sha256) for one parsed contract value.

    problems: sorted "<path>:<reason>" codes, [] when valid. sha256: the canonical-JSON digest of the whole
    value, or None (with the single problem "contract:not_canonical_json") when it holds NaN, infinity or a
    type JSON cannot carry."""
    try:
        digest = contract_sha256(contract)
    except (TypeError, ValueError, RecursionError):
        return ["contract:not_canonical_json"], None
    checker = _Checker(contract)
    checker.contract(contract)
    return sorted(checker.problems), digest


def result_status(contract, problems):
    """valid_draft, valid_frozen or invalid."""
    if problems:
        return "invalid"
    return "valid_draft" if contract["status"] == "draft" else "valid_frozen"


def check_contract_file(path):
    """{"status", "contract_sha256", "problems"} for one local contract file.

    A file that cannot be read or parsed strictly is invalid with a null sha256 and one "contract:<code>". An
    invalid contract that parsed still carries its sha256, to name the file; it is not for a trial ledger."""
    try:
        contract = load_contract(path)
    except ContractFileError as error:
        return {"status": "invalid", "contract_sha256": None, "problems": [f"contract:{error}"]}
    problems, digest = validate_contract(contract)
    return {"status": result_status(contract, problems), "contract_sha256": digest, "problems": problems}
